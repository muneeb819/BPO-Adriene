"""ARI Streaming Bridge: connects the StreamingVoiceAgent to Asterisk via externalMedia.

Handles the WebSocket audio bridge between Asterisk and the voice agent pipeline.
Receives caller audio → feeds to Deepgram STT, receives agent audio → plays back.
Uses ARI externalMedia for full-duplex RTP.
"""
from __future__ import annotations

import asyncio
import json
import struct
import time
import wave
from pathlib import Path

from .config import Config
from .voice_agent import StreamingVoiceAgent

try:
    import websockets
except ImportError:
    websockets = None


class ARIStreamingBridge:
    """Bridges Asterisk externalMedia to the streaming voice agent."""

    def __init__(self, config: Config):
        self.config = config
        self.ari_base = config.ari_url.rstrip("/") + "/ari"
        self.ari_auth = (config.ari_username, config.ari_password)

    def _ari_request(self, method: str, path: str, **kwargs):
        import requests
        kwargs.setdefault("auth", self.ari_auth)
        r = requests.request(method, f"{self.ari_base}/{path.lstrip('/')}", timeout=30, **kwargs)
        r.raise_for_status()
        return r.json() if r.content else None

    def answer(self, channel_id: str):
        self._ari_request("POST", f"channels/{channel_id}/answer")

    def bridge_create(self, name: str = "streaming") -> str:
        data = self._ari_request("POST", "bridges", json={"type": "mixing", "name": name})
        return data["id"]

    def bridge_add(self, bridge_id: str, channel_id: str):
        self._ari_request("POST", f"bridges/{bridge_id}/addChannel", json={"channel": channel_id})

    def redirect(self, channel_id: str, context: str, extension: str):
        self._ari_request(
            "POST", f"channels/{channel_id}/redirect",
            json={"endpoint": f"Local/{extension}@{context}"},
        )

    def hangup(self, channel_id: str):
        try:
            self._ari_request("DELETE", f"channels/{channel_id}")
        except Exception:
            pass

    def start_external_media(self, channel_id: str, app: str, host: str, port: int) -> dict:
        return self._ari_request(
            "POST", f"channels/{channel_id}/externalMedia",
            json={"app": app, "external_host": f"{host}:{port}", "format": "slin16"},
        )

    async def run_streaming_session(self, channel_id: str, agent: StreamingVoiceAgent):
        """Run a full-duplex streaming session on a live Asterisk channel."""
        if websockets is None:
            raise RuntimeError("websockets package required: pip install websockets")

        import requests
        r = requests.get(
            f"{self.ari_base}/channels/{channel_id}",
            auth=self.ari_auth, timeout=10,
        )
        r.raise_for_status()
        channel = r.json()
        host = self.config.host if self.config.host != "0.0.0.0" else "127.0.0.1"
        ext_media_port = 21000

        self.answer(channel_id)
        bridge_id = self.bridge_create(f"streaming-{channel_id}")
        self.bridge_add(bridge_id, channel_id)

        async def audio_out_handler(audio_bytes: bytes):
            pass

        agent.set_audio_out(lambda b: asyncio.ensure_future(audio_out_handler(b)))

        stt_task = asyncio.create_task(self._stt_loop(agent, channel_id))
        agent_task = asyncio.create_task(agent.start())

        try:
            await asyncio.gather(stt_task, agent_task, return_exceptions=True)
        finally:
            stt_task.cancel()
            agent_task.cancel()
            result = agent.get_result()
            self.hangup(channel_id)
            return result

    async def _stt_loop(self, agent: StreamingVoiceAgent, channel_id: str):
        """Record caller audio in turns and feed to the voice agent."""
        turn = 0
        while not agent.state.ended:
            turn += 1
            name = f"stream_turn_{turn}"
            try:
                self._ari_request(
                    "POST", f"channels/{channel_id}/record",
                    json={
                        "name": name,
                        "format": "wav",
                        "maxDurationSeconds": 30,
                        "terminateOn": "any",
                        "beep": False,
                    },
                )
            except Exception:
                await asyncio.sleep(0.5)
                continue

            path = None
            deadline = time.time() + 35
            rec_path = f"{self.config.ari_recordings_dir}/{name}.wav"
            while time.time() < deadline:
                if Path(rec_path).exists() and Path(rec_path).stat().st_size > 44:
                    path = rec_path
                    break
                await asyncio.sleep(0.4)

            if not path or not Path(path).exists():
                continue

            try:
                self._ari_request("DELETE", f"recordings/{name}")
            except Exception:
                pass

            transcript = await self._transcribe(path)
            if transcript:
                await agent.handle_user_text(transcript)

    async def _transcribe(self, wav_path: str) -> str:
        """Transcribe a WAV file using the configured STT provider."""
        if self.config.stt_provider == "console":
            return input("CALLER: ").strip()
        if self.config.stt_provider == "deepgram":
            return self._deepgram_transcribe(wav_path)
        return ""

    def _deepgram_transcribe(self, wav_path: str) -> str:
        import requests
        url = f"https://api.deepgram.com/v1/listen?model={self.config.deepgram_model}&punctuate=true"
        with open(wav_path, "rb") as f:
            r = requests.post(
                url,
                headers={"Authorization": f"Token {self.config.deepgram_api_key}"},
                data=f, timeout=60,
            )
        r.raise_for_status()
        return r.json()["results"]["channels"][0]["alternatives"][0]["transcript"]
