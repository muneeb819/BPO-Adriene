"""Real-time voice pipeline over WebSocket with barge-in support.

Flow:
  Browser mic (16kHz PCM16, binary frames) --> WebSocket --> Deepgram streaming STT
      --> final utterance --> Brain (Gemini LLM or semantic fallback)
      --> reply text --> ElevenLabs streaming TTS (PCM16) --> WebSocket --> Browser playback

Barge-in:
  A persistent reader task reads the browser WebSocket the whole call, so a
  {"type":"barge"} message sent *while the bot is speaking* is caught instantly.
  We cancel the TTS task (asyncio), tell the browser to stop playback, and keep
  listening — the customer can interrupt the bot mid-sentence, like a human.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json

import websockets

from agent.brain import Brain
from agent.config import Config
from agent.conversation import Conversation
from agent.llm import LLM
from agent.recorder import CallRecorder
from agent.tts import TTS

DEEPGRAM_SAMPLE_RATE = 16000


class VoiceSession:
    """One live call handled over a WebSocket connection."""

    def __init__(self, config: Config, send):
        self.config = config
        self.send = send                # async fn(dict) -> None
        self.llm = self._build_llm()
        self.convo = Conversation(config, self.llm)
        self.brain = Brain(config, self.llm)
        self.tts = TTS(config)

        self._ws = None                 # starlette WebSocket (browser)
        self._dg = None                 # Deepgram streaming socket (websockets client)
        self._speak_task: asyncio.Task | None = None
        self.speaking = False
        self.ended = False

        self._utterance: asyncio.Queue[str] = asyncio.Queue()  # final text (voice or typed)
        self._tasks: list[asyncio.Task] = []
        self._dg_current = ""           # accumulated final words for current utterance
        self._closing = False
        self.recorder: CallRecorder | None = None
        self._result_disposition = ""

    @staticmethod
    def _build_llm():
        cfg = Config.load()
        llm = LLM(cfg)
        try:
            if llm.models_available():
                return llm
        except Exception:
            pass
        from agent.mock_llm import MockLLM
        return MockLLM(cfg)

    # ---------------- Deepgram streaming STT ----------------

    async def _connect_deepgram(self):
        url = (
            "wss://api.deepgram.com/v1/listen?"
            f"model={self.config.deepgram_model}&encoding=linear16"
            f"&sample_rate={DEEPGRAM_SAMPLE_RATE}&channels=1"
            "&interim_results=true&endpointing=300&utterance_end_ms=1000"
            "&smart_format=true"
        )
        headers = {"Authorization": f"Token {self.config.deepgram_api_key}"}
        # websockets >= 13 renamed extra_headers -> additional_headers
        try:
            return await websockets.connect(url, additional_headers=headers, max_size=None)
        except TypeError:
            return await websockets.connect(url, extra_headers=headers, max_size=None)

    async def _read_deepgram(self):
        """Persistent reader with auto-reconnect.

        Deepgram closes the socket after ~12s with no audio (error 1011). With a
        working mic this never happens (silence still streams constantly), but if
        the mic drops or the network blips we reconnect quietly instead of
        killing transcription for the rest of the call.
        """
        if self._dg is None:
            return                        # typed-only session (no STT configured)
        failures = 0
        while not self._closing:
            try:
                async for msg in self._dg:
                    failures = 0          # healthy — we read at least one message
                    await self._handle_dg(msg)
                if self._closing:
                    return
            except asyncio.CancelledError:
                raise
            except Exception:
                if self._closing:
                    return

            # Connection closed or errored -> try to reconnect.
            failures += 1
            if failures > 5:
                with contextlib.suppress(Exception):
                    await self.send({
                        "type": "error",
                        "message": "Live transcription paused (no audio detected) — you can still type.",
                    })
                return
            await asyncio.sleep(min(6.0, 0.5 * failures))
            try:
                self._dg = await self._connect_deepgram()
            except Exception:
                continue

    async def _handle_dg(self, msg):
        data = json.loads(msg)
        if data.get("type") == "UtteranceEnd":
            if self._dg_current:
                await self._flush_utterance()
            return
        alt = (data.get("channel") or {}).get("alternatives") or [{}]
        text = (alt[0].get("transcript") or "").strip()
        if not text:
            return
        if data.get("is_final"):
            self._dg_current = (self._dg_current + " " + text).strip()
            await self.send({
                "type": "transcript",
                "text": self._dg_current,
                "final": True,
            })
            await self._flush_utterance()
        else:
            await self.send({
                "type": "transcript",
                "text": (self._dg_current + " " + text).strip(),
                "final": False,
            })

    async def _flush_utterance(self):
        text = self._dg_current.strip()
        self._dg_current = ""
        if text:
            await self._utterance.put(text)

    # ---------------- browser reader (runs the whole call) ----------------

    async def _read_browser(self):
        """Persistent: forward binary audio to Deepgram, handle control JSON.

        Because this runs for the whole call (including while the bot speaks),
        a barge message is caught immediately and cancels the TTS task.
        """
        try:
            while True:
                msg = await self._ws.receive()
                mtype = msg.get("type")
                if mtype == "websocket.disconnect":
                    break
                if msg.get("bytes") is not None:
                    # raw PCM16 audio -> record it and stream to Deepgram
                    if self.recorder is not None:
                        with contextlib.suppress(Exception):
                            self.recorder.add_customer(msg["bytes"])
                    if self._dg is not None:
                        with contextlib.suppress(Exception):
                            await self._dg.send(msg["bytes"])
                    continue
                text = msg.get("text")
                if text is None:
                    continue
                try:
                    data = json.loads(text)
                except Exception:
                    continue
                kind = data.get("type")
                if kind == "barge":
                    self.interrupt_speaking()
                elif kind == "text":
                    typed = (data.get("text") or "").strip()
                    if typed:
                        await self._utterance.put(typed)
                elif kind == "stop":
                    self._closing = True
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            self._closing = True

    # ---------------- speaking (with barge-in) ----------------

    async def speak(self, text: str):
        """Stream ElevenLabs PCM to the browser. Cancelled instantly on barge-in."""
        self.speaking = True
        if self.recorder is not None:
            with contextlib.suppress(Exception):
                self.recorder.speech_start()
        await self.send({"type": "speech_start", "text": text})
        try:
            async for chunk in self.tts.synthesize_stream_pcm(text):
                if self.recorder is not None:
                    with contextlib.suppress(Exception):
                        self.recorder.add_agent(chunk)
                await self.send({
                    "type": "audio",
                    "data": base64.b64encode(chunk).decode(),
                })
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await self.send({"type": "speech_cancel"})
            raise
        finally:
            self.speaking = False
            if self.recorder is not None:
                with contextlib.suppress(Exception):
                    self.recorder.speech_end()
            with contextlib.suppress(Exception):
                await self.send({"type": "speech_end"})

    def interrupt_speaking(self):
        """Barge-in: stop the bot mid-sentence right now."""
        if self._speak_task and not self._speak_task.done():
            self._speak_task.cancel()

    # ---------------- main loop ----------------

    async def run(self, ws_from_browser):
        self._ws = ws_from_browser
        self.recorder = CallRecorder()   # one recording per call

        # Live transcription is optional. With no DEEPGRAM_API_KEY (plain debug
        # mode) the console must still work on typed input, so an absent or
        # unreachable STT degrades to "typed-only" instead of ending the call
        # before the greeting is even spoken.
        if self.config.stt_provider == "deepgram" and self.config.deepgram_api_key:
            try:
                self._dg = await self._connect_deepgram()
            except Exception as e:
                reason = str(e) or type(e).__name__
                await self.send({
                    "type": "notice",
                    "message": f"Live mic transcription is unavailable ({reason}). "
                               "Type your replies below — everything else works normally.",
                })
        else:
            await self.send({
                "type": "notice",
                "message": "Mic transcription is off (STT_PROVIDER=console). "
                           "Type your replies — everything else works normally.",
            })

        # Start persistent readers (they run for the whole call).
        self._tasks = [
            asyncio.create_task(self._read_browser()),
            asyncio.create_task(self._read_deepgram()),
        ]

        # Opening line.
        greeting = self.convo.start()
        await self.send({"type": "agent_say", "text": greeting})
        await self._speak_guarded(greeting)

        for _ in range(12):
            if self.ended or self._closing:
                break
            utterance = await self._next_utterance()
            if utterance is None:
                break
            if not utterance:
                continue

            result = await self._handle(utterance)
            if result.get("end_call"):
                self.ended = True
                self._result_disposition = result.get("disposition", "")
                saved = self._save_recording()
                await self.send({
                    "type": "result",
                    "action": result.get("action", "hangup"),
                    "disposition": result.get("disposition", ""),
                })
                if saved:
                    with contextlib.suppress(Exception):
                        await self.send({
                            "type": "recording_saved",
                            "id": saved.get("id"),
                            "duration_sec": saved.get("duration_sec"),
                        })
                break

    async def _next_utterance(self) -> str | None:
        """Wait for the customer to say/typed something. None => call over."""
        while not self._closing:
            try:
                return await asyncio.wait_for(self._utterance.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
        return None

    async def _speak_guarded(self, text: str):
        """Speak, staying responsive to barge-in (which cancels this task)."""
        self._speak_task = asyncio.create_task(self.speak(text))
        try:
            await self._speak_task
        except asyncio.CancelledError:
            # Barge-in: the customer cut us off. Their speech is still being
            # captured by the persistent Deepgram reader.
            raise
        except Exception:
            # Both TTS engines failed. The line was already reported to the
            # dashboard as agent_say, so log and carry on — the customer simply
            # hears nothing for this turn instead of the call dropping.
            print(f"[voice] TTS failed on both engines: {text[:60]!r}")

    async def _handle(self, utterance: str) -> dict:
        """Run the conversation engine on the customer's utterance."""
        result = await asyncio.to_thread(self.convo.hear, utterance)

        if result.ai_speech:
            await self.send({"type": "agent_say", "text": result.ai_speech})
            await self._speak_guarded(result.ai_speech)

        return {
            "end_call": result.end_call,
            "action": "transfer" if result.disposition == "AI-TRANSFER" else "hangup",
            "disposition": result.disposition,
        }

    async def close(self):
        self._closing = True
        for t in self._tasks:
            t.cancel()
        if self._tasks:
            with contextlib.suppress(Exception):
                await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._speak_task and not self._speak_task.done():
            self._speak_task.cancel()
        if self._dg is not None:
            with contextlib.suppress(Exception):
                await self._dg.close()
        # Idempotent: no-op if run() already saved on a normal end; catches
        # dropped/disconnected calls so no recording is ever lost.
        self._save_recording()

    def _save_recording(self) -> dict | None:
        """Persist the mixed audio + transcript for this call (idempotent)."""
        if self.recorder is None:
            return None
        ctx = getattr(self.convo, "ctx", None)
        camp = getattr(self.convo, "campaign", None)
        meta = {
            "transcript": getattr(ctx, "transcript", []),
            "disposition": getattr(ctx, "disposition", "") or self._result_disposition,
            "campaign": getattr(camp, "id", ""),
            "campaign_name": getattr(camp, "name", ""),
            "age": getattr(ctx, "age", None),
            "answers": getattr(ctx, "answers", {}),
            "sentiment": getattr(ctx, "sentiment", "neutral"),
            "ended_normally": self.ended,
        }
        with contextlib.suppress(Exception):
            return self.recorder.save(meta)
        return None
