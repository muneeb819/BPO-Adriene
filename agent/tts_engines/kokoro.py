"""Kokoro-82M — the in-process neural voice.

Serves an OpenAI-compatible ``POST /v1/audio/speech`` endpoint and is the
default engine: it runs locally, needs no key, and ships 20 US-English voices.
The transport it shares with the other local HTTP engines (Piper) lives in
:mod:`agent.tts_engines.local_http`; this module owns only Kokoro's payload,
URL and the streaming reader.
"""
from __future__ import annotations

import asyncio
import contextlib
import threading

from ..resample import PCM16Resampler, TARGET_RATE, resample_pcm16


class KokoroEngine:
    """Kokoro synthesis, mixed into :class:`agent.tts.TTS`."""

    def _kokoro_payload(self, text: str, response_format: str) -> dict:
        payload = {
            "model": self.config.kokoro_model,
            "input": text,
            "voice": self.config.kokoro_voice,
            "response_format": response_format,
            "speed": self.config.kokoro_speed,
        }
        # Voice-studio pitch control. Unknown keys are ignored by a real Kokoro
        # server; the bundled Piper server honours it.
        pitch = int(getattr(self.config, "kokoro_pitch", 0) or 0)
        if pitch:
            payload["pitch"] = pitch
        return payload

    def _kokoro_url(self) -> str:
        return f"{self.config.kokoro_base_url.rstrip('/')}/v1/audio/speech"

    def _kokoro(self, text: str, response_format: str = "wav") -> bytes:
        import requests

        r = requests.post(
            self._kokoro_url(),
            json=self._kokoro_payload(text, response_format),
            headers={"Authorization": f"Bearer {self.config.kokoro_api_key}"},
            timeout=self.config.kokoro_timeout,
        )
        r.raise_for_status()
        self._note_engine(r.headers)
        return r.content

    def _kokoro_pcm(self, text: str) -> bytes:
        raw = self._kokoro(text, response_format="pcm")
        return resample_pcm16(raw, 24000, TARGET_RATE)

    async def _pcm_kokoro(self, text: str):
        """Stream Kokoro PCM in a worker thread, resampling 24 kHz -> 16 kHz.

        The HTTP read blocks, so it runs off the event loop; chunks are handed
        back via call_soon_threadsafe so barge-in can still cancel instantly.
        """
        import requests

        loop = asyncio.get_running_loop()
        out: asyncio.Queue = asyncio.Queue()
        stop = threading.Event()

        def emit(item):
            if stop.is_set():
                return
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(out.put_nowait, item)

        def worker():
            rs = PCM16Resampler(24000, TARGET_RATE)
            try:
                with requests.post(
                    self._kokoro_url(),
                    json=self._kokoro_payload(text, "pcm"),
                    headers={"Authorization": f"Bearer {self.config.kokoro_api_key}"},
                    stream=True,
                    timeout=(5.0, self.config.kokoro_timeout),
                ) as resp:
                    resp.raise_for_status()
                    self._note_engine(resp.headers)
                    for chunk in resp.iter_content(chunk_size=8192):
                        if stop.is_set():
                            return
                        pcm = rs.feed(chunk)
                        if pcm:
                            emit(pcm)
                    tail = rs.flush()
                    if tail:
                        emit(tail)
                emit(None)                     # sentinel: finished cleanly
            except BaseException as exc:       # noqa: BLE001 - re-raised by consumer
                emit(exc)

        threading.Thread(target=worker, daemon=True, name="kokoro-tts").start()
        try:
            while True:
                item = await out.get()
                if item is None:
                    return
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            stop.set()                         # tell the worker to stop generating

