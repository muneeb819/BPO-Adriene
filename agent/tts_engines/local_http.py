"""The shared local-HTTP TTS transport (Kokoro and Piper).

Both self-hosted engines speak the same OpenAI-compatible wire protocol —
``POST /v1/audio/speech`` returning a container or raw PCM, plus a
``GET /v1/audio/voices`` health probe — so the client lives here once instead of
in each engine module. Anything engine-specific (payload shape, base URL,
defaults) stays in that engine's module.
"""
from __future__ import annotations

import asyncio
import time

from ..resample import TARGET_RATE, resample_pcm16


class LocalHttpEngine:
    """OpenAI-compatible local TTS transport, mixed into :class:`agent.tts.TTS`."""

    def probe_local(self, provider: str = "kokoro", timeout: float = 2.0) -> bool:
        """Is a self-hosted OpenAI-compatible TTS server up? Cached for /health."""
        provider = (provider or "kokoro").lower()
        # Keyed by URL as well as engine: the endpoint can be re-pointed at runtime
        # (a different port, a restarted server), and a cached "up" for the old
        # address would keep lying about the new one for the cache window.
        url = self._local_url(provider, "/v1/audio/voices")
        key = f"{provider}:probe:{url}"
        cache = type(self)._health.get(key)
        now = time.time()
        if cache and now - cache[0] < 5.0:
            return cache[1]
        import requests

        try:
            r = requests.get(url, timeout=timeout)
            ok = r.status_code == 200
        except Exception:
            ok = False
        type(self)._health[key] = (now, ok)
        return ok

    def probe_kokoro(self, timeout: float = 2.0) -> bool:
        """Backwards-compatible alias (dashboard and tests call this name)."""
        return self.probe_local("kokoro", timeout)

    # ------- providers: self-hosted OpenAI-compatible servers (kokoro/piper) -------

    def _local_settings(self, provider: str) -> dict:
        """Where a local server lives and how it wants to be spoken to."""
        c = self.config
        if (provider or "").lower() == "piper":
            return {
                "base_url": getattr(c, "piper_base_url", "http://127.0.0.1:8880"),
                "api_key": getattr(c, "piper_api_key", "not-needed"),
                "model": getattr(c, "piper_model", "piper"),
                "voice": getattr(c, "piper_voice", ""),
                "speed": float(getattr(c, "piper_speed", 1.0) or 1.0),
                "pitch": int(getattr(c, "piper_pitch", 50)),
                "volume": float(getattr(c, "piper_volume", 1.0) or 1.0),
                "timeout": float(getattr(c, "piper_timeout", 30.0) or 30.0),
                "rate": int(getattr(c, "piper_sample_rate", TARGET_RATE) or TARGET_RATE),
            }
        return {
            "base_url": c.kokoro_base_url, "api_key": c.kokoro_api_key,
            "model": c.kokoro_model, "voice": c.kokoro_voice,
            "speed": float(c.kokoro_speed or 1.0),
            "pitch": int(getattr(c, "kokoro_pitch", 0) or 0),
            "volume": 1.0, "timeout": float(c.kokoro_timeout), "rate": 24000,
        }

    def _local_url(self, provider: str, path: str = "/v1/audio/speech") -> str:
        base = str(self._local_settings(provider)["base_url"]).rstrip("/")
        return f"{base}{path}"

    def _local_payload(self, text: str, response_format: str, provider: str) -> dict:
        if (provider or "").lower() == "kokoro":
            return self._kokoro_payload(text, response_format)
        s = self._local_settings("piper")
        # pitch is the studio's 0-99 scale (50 = the voice's own pitch)
        return {"model": s["model"], "input": text, "voice": s["voice"],
                "response_format": response_format, "speed": s["speed"],
                "pitch": s["pitch"], "volume": s["volume"]}

    def _note_engine(self, headers) -> None:
        """Remember the engine/model the server claims, for honest reporting.

        ``kokoro`` and ``piper`` are both just "a local OpenAI-compatible server",
        and in the default setup both names point at the same URL — so the only
        trustworthy answer to "who actually spoke?" is the one the server sends
        back in ``X-Voice-Engine``. Falls back to the provider nickname when a
        server does not send it.
        """
        try:
            engine = (headers.get("X-Voice-Engine") or "").strip()
            model = (headers.get("X-Voice-Model") or "").strip()
        except Exception:                                          # noqa: BLE001
            return
        if engine:
            self.engine_reported = engine
        if model:
            self.model_reported = model

    def _local_audio(self, text: str, response_format: str, provider: str) -> bytes:
        if (provider or "").lower() == "kokoro":
            return self._kokoro(text, response_format=response_format)
        import requests

        s = self._local_settings("piper")
        r = requests.post(
            self._local_url("piper"),
            json=self._local_payload(text, response_format, "piper"),
            headers={"Authorization": f"Bearer {s['api_key']}"},
            timeout=s["timeout"],
        )
        r.raise_for_status()
        self._note_engine(r.headers)
        return r.content

    def _local_pcm(self, text: str, provider: str) -> bytes:
        if (provider or "").lower() == "kokoro":
            return self._kokoro_pcm(text)
        raw = self._local_audio(text, "pcm", "piper")
        rate = self._local_settings("piper")["rate"]
        return resample_pcm16(raw, rate, TARGET_RATE) if rate != TARGET_RATE else raw

    async def _pcm_local(self, provider: str, text: str):
        """Async PCM16@16k generator for a local server.

        Kokoro streams as it synthesises; the Piper server returns the finished
        line in one piece (it renders a sentence in ~0.3 s, faster than it takes
        to speak it), so it is awaited in a worker thread and yielded whole.
        """
        if (provider or "").lower() == "kokoro":
            async for chunk in self._pcm_kokoro(text):
                yield chunk
            return
        pcm = await asyncio.to_thread(self._local_pcm, text, "piper")
        if pcm:
            yield pcm

    def _local_stream_sync(self, text: str, provider: str):
        if (provider or "").lower() == "kokoro":
            yield from self._kokoro_stream_sync(text)
            return
        pcm = self._local_pcm(text, "piper")
        if pcm:
            yield pcm

    # ---------------- other providers ----------------

