"""ElevenLabs — cloud streaming voice.

The expressive cloud engine: lowest latency of the hosted options, streams per
chunk, and is the one that needs a key (ELEVENLABS_API_KEY). Voice character is
tuned with the stability / similarity / style / speaker-boost settings.
"""
from __future__ import annotations

from ._contract import EngineMixin

import asyncio


class ElevenLabsEngine(EngineMixin):
    """ElevenLabs synthesis, mixed into :class:`agent.tts.TTS`."""

    def _voice_settings(self) -> dict:
        return {
            "stability": self.config.elevenlabs_stability,
            "similarity_boost": self.config.elevenlabs_similarity_boost,
            "style": self.config.elevenlabs_style,
            "use_speaker_boost": self.config.elevenlabs_speaker_boost,
        }

    def _get_elevenlabs(self):
        if self._elevenlabs is None:
            from elevenlabs.client import ElevenLabs

            if not self.config.elevenlabs_api_key:
                raise ConnectionError("ELEVENLABS_API_KEY is not set")
            self._elevenlabs = ElevenLabs(api_key=self.config.elevenlabs_api_key)
        return self._elevenlabs

    def _elevenlabs_sync(self, text: str) -> bytes:
        from ..voice_ids import resolve_voice_id

        audio = self._get_elevenlabs().text_to_speech.convert(
            voice_id=resolve_voice_id(self.config.elevenlabs_voice),
            text=text,
            model_id=self.config.elevenlabs_model,
            voice_settings=self._voice_settings(),
        )
        if hasattr(audio, "__iter__") and not isinstance(audio, (bytes, bytearray)):
            return b"".join(bytes(a) for a in audio)
        return audio

    def _elevenlabs_stream(self, text: str):
        from ..voice_ids import resolve_voice_id

        stream = self._get_elevenlabs().text_to_speech.convert(
            voice_id=resolve_voice_id(self.config.elevenlabs_voice),
            text=text,
            model_id=self.config.elevenlabs_model,
            voice_settings=self._voice_settings(),
        )
        for chunk in stream:
            if chunk:
                yield chunk

    async def _pcm_elevenlabs(self, text: str):
        """Async: raw PCM16 @16 kHz from ElevenLabs, cancellable mid-stream."""
        from ..voice_ids import resolve_voice_id

        client = self._get_elevenlabs()

        def _run():
            return client.text_to_speech.convert(
                voice_id=resolve_voice_id(self.config.elevenlabs_voice),
                text=text,
                model_id=self.config.elevenlabs_model,
                output_format="pcm_16000",
                voice_settings=self._voice_settings(),
            )

        stream = await asyncio.to_thread(_run)
        for chunk in stream:
            if chunk:
                yield bytes(chunk)
                await asyncio.sleep(0)         # yield control so barge-in works

