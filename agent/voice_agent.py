"""Streaming voice agent: chains STT → LLM → ElevenLabs TTS with beat-based state machine.

Replaces the turn-based approach for live telephony. Connects to Asterisk via
ARI externalMedia or any custom WebSocket transport.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field

from .config import Config
from .conversation import Conversation, BeatResult
from .llm import LLM


@dataclass
class VoiceAgentState:
    ended: bool = False
    disposition: str = ""
    action: str = ""
    is_speaking: bool = False
    should_interrupt: bool = False
    last_user_speech: float = 0.0
    last_agent_speech: float = 0.0
    transcript: list[dict] = field(default_factory=list)


class StreamingVoiceAgent:
    def __init__(self, config: Config):
        self.config = config
        self.state = VoiceAgentState()
        self._llm = None
        self._tts = None
        self._convo = None
        self._audio_out_callback = None

    def set_audio_out(self, callback):
        self._audio_out_callback = callback

    def _get_llm(self):
        if self._llm is None:
            self._llm = LLM(self.config)
        return self._llm

    def _get_tts(self):
        if self._tts is None:
            from .tts import TTS
            self._tts = TTS(self.config)
        return self._tts

    def _get_convo(self):
        if self._convo is None:
            self._convo = Conversation(self.config, self._get_llm())
        return self._convo

    def _send_audio(self, audio_bytes: bytes):
        if self._audio_out_callback and audio_bytes:
            self._audio_out_callback(audio_bytes)

    async def start(self):
        convo = self._get_convo()
        greeting = convo.start()
        self.state.transcript.append({"role": "ai", "text": greeting})
        await self._speak_streaming(greeting)

    async def handle_user_text(self, text: str):
        if self.state.ended or not text.strip():
            return
        self.state.last_user_speech = time.time()
        self.state.transcript.append({"role": "customer", "text": text})
        convo = self._get_convo()
        result = convo.hear(text)
        if result.ai_speech:
            self.state.transcript.append({"role": "ai", "text": result.ai_speech})
            await self._speak_streaming(result.ai_speech)
        if result.end_call:
            self.state.ended = True
            self.state.disposition = result.disposition
            self.state.action = "transfer" if result.disposition == "AI-TRANSFER" else "hangup"

    async def _speak_streaming(self, text: str):
        self.state.is_speaking = True
        tts = self._get_tts()
        try:
            for chunk in tts.synthesize_stream(text):
                if self.state.should_interrupt:
                    break
                self._send_audio(chunk)
                await asyncio.sleep(0.01)
        finally:
            self.state.is_speaking = False
            self.state.should_interrupt = False
            self.state.last_agent_speech = time.time()

    def interrupt(self):
        if self.state.is_speaking:
            self.state.should_interrupt = True

    def get_result(self) -> dict:
        return {
            "action": self.state.action or "hangup",
            "disposition": self.state.disposition,
            "transcript": self.state.transcript,
        }
