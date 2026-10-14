"""Call channel abstraction and session runner.

ConsoleChannel: interactive terminal testing (no PBX/keys needed).
AriChannel: wires to a live Asterisk channel via ARI (turn-based).
run_session: drives the beat-based conversation state machine.
"""
from __future__ import annotations

import os
import wave

from .ari import ARI
from .config import Config
from .conversation import Conversation
from .llm import LLM
from .stt import STT
from .tts import TTS


class CallChannel:
    def say(self, text: str) -> None:
        raise NotImplementedError

    def listen(self) -> str:
        raise NotImplementedError

    def transfer(self) -> None:
        raise NotImplementedError

    def hangup(self) -> None:
        raise NotImplementedError


class ConsoleChannel(CallChannel):
    def say(self, text: str) -> None:
        print(f"AGENT: {text}")

    def listen(self) -> str:
        return input("CALLER: ").strip()

    def transfer(self) -> None:
        print("[CONSOLE] >>> transferring to closer (simulated) <<<")

    def hangup(self) -> None:
        print("[CONSOLE] >>> call ended <<<")


class AriChannel(CallChannel):
    def __init__(self, config: Config, ari: ARI, tts: TTS, stt: STT, channel_id: str):
        self.config = config
        self.ari = ari
        self.tts = tts
        self.stt = stt
        self.channel_id = channel_id
        self._seq = 0

    def say(self, text: str) -> None:
        self._seq += 1
        name = f"ai_reply_{self._seq}"
        wav = self.tts.synthesize(text)
        if not wav:
            return
        path = os.path.join(self.config.asterisk_sounds_dir, "ai_fronter")
        os.makedirs(path, exist_ok=True)
        out = os.path.join(path, f"{name}.wav")
        self._write_wav(out, wav)
        self.ari.play(self.channel_id, f"sound:ai_fronter/{name}")

    @staticmethod
    def _write_wav(out_path: str, wav_bytes: bytes) -> None:
        with open(out_path, "wb") as f:
            f.write(wav_bytes)

    def listen(self) -> str:
        self._seq += 1
        name = f"ai_listen_{self._seq}"
        self.ari.record(self.channel_id, name, max_seconds=30)
        path = self.ari.wait_for_recording(name, timeout=40)
        if not path:
            return ""
        return self.stt.transcribe_file(path)

    def transfer(self) -> None:
        self.ari.transfer_to_closer(self.channel_id)

    def hangup(self) -> None:
        self.ari.hangup(self.channel_id)


def run_session(channel: CallChannel, config: Config, max_turns: int = 10) -> dict:
    """Run the beat-based conversation state machine."""
    try:
        llm = LLM(config)
        if not llm.models_available():
            raise RuntimeError("LLM not available")
    except Exception:
        from .mock_llm import MockLLM
        llm = MockLLM(config)
    convo = Conversation(config, llm)
    channel.say(convo.start())
    turns = 0
    while turns < max_turns:
        turns += 1
        user_text = channel.listen()
        if not user_text:
            continue
        result = convo.hear(user_text)
        if result.ai_speech:
            channel.say(result.ai_speech)
        if result.end_call:
            if result.disposition == "AI-TRANSFER":
                channel.transfer()
            else:
                channel.hangup()
            return {
                "action": "transfer" if result.disposition == "AI-TRANSFER" else "hangup",
                "disposition": result.disposition,
                "facts": result.classification,
            }
    channel.say("Hello? Just checking you're still there.")
    channel.hangup()
    return {"action": "hangup", "disposition": "TIMEOUT", "facts": {}}


async def run_streaming_session(channel_id: str, config) -> dict:
    """Run a full-duplex streaming session using the beat-based pipeline."""
    from .ari_streaming import ARIStreamingBridge
    from .voice_agent import StreamingVoiceAgent

    bridge = ARIStreamingBridge(config)
    agent = StreamingVoiceAgent(config)
    result = await bridge.run_streaming_session(channel_id, agent)
    return result
