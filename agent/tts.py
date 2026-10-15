"""Text-to-speech providers with streaming and automatic failover.

Engines
-------
elevenlabs   cloud, expressive streaming (primary today)
kokoro       self-hosted, free, OpenAI-compatible (standby / alternative)
deepgram / openai / azure   additional cloud engines
console      prints the line, no audio (tests / no keys)

Every engine can be reached through `synthesize_stream_pcm()`, which yields raw
PCM16 @16 kHz mono — the one format the voice pipeline, Deepgram and the call
recorder all agree on. Engines that synthesise at another rate (Kokoro is
24 kHz) or ship a container (WAV) are converted on the way out.

Failover
--------
The engine named in TTS_PROVIDER is primary; TTS_STANDBY_PROVIDER is standby.
If the primary fails *before any audio has been played*, the standby is used
for that utterance and promoted for subsequent ones, so a mid-call outage of
one engine does not drop the call. Health is remembered briefly so repeated
calls do not keep re-hitting a dead endpoint.
"""
from __future__ import annotations

import asyncio
import contextlib
import threading
import time

from .config import Config
from .resample import PCM16Resampler, TARGET_RATE, resample_pcm16, wav_to_pcm16

# Errors that mean "the engine is unreachable/broken", i.e. worth failing over.
# Deliberately excludes our own bugs (ValueError, KeyError, ...) so a real
# programming error is never swallowed by a silent provider switch.
_NETWORK_NAMES = (
    "connection", "timeout", "apitimeout", "apierror", "httpstatus",
    "remoteprotocol", "protocol", "readerror", "writeerror", "ssl", "proxy",
)

# Packages owned by a TTS engine itself. If one of these is missing the engine
# cannot run at all — the same practical situation as an outage — so it is worth
# failing over to the standby rather than going silent. Anything else that fails
# to import is our own bug and must keep surfacing loudly.
_ENGINE_PACKAGES = (
    "chatterbox", "torch", "torchaudio", "kokoro", "misaki", "soundfile",
    "transformers", "diffusers", "piper", "onnxruntime",
)

#: Providers served by a **self-hosted OpenAI-compatible HTTP server**. Kokoro
#: and the bundled Piper/ONNX server speak the same wire format (see
#: scripts/neural_tts_server.py), so they share one code path — only the base URL,
#: the voice name and the model's sample rate differ. Piper is the neural,
#: dependency-light option: real ONNX voices on CPU, no torch and no cloud key.
LOCAL_HTTP_PROVIDERS = ("kokoro", "piper")


def _missing_engine_package(exc: ImportError) -> str:
    name = (getattr(exc, "name", "") or "").split(".")[0]
    return name if name in _ENGINE_PACKAGES else ""


def is_provider_error(exc: BaseException) -> bool:
    """True when `exc` looks like an upstream outage rather than our bug."""
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return True
    if isinstance(exc, ImportError) and _missing_engine_package(exc):
        return True
    module = type(exc).__module__ or ""
    if any(m in module for m in ("requests", "urllib3", "httpx", "elevenlabs", "openai")):
        return True
    name = type(exc).__name__.lower()
    return any(token in name for token in _NETWORK_NAMES)


def streaming_wav_header(sample_rate: int = TARGET_RATE, channels: int = 1, bits: int = 16) -> bytes:
    """RIFF header with 0xFFFFFFFF sizes, so concatenated chunks form a WAV."""
    byte_rate = sample_rate * channels * bits // 8
    block_align = channels * bits // 8
    hdr = b"RIFF" + (0xFFFFFFFF).to_bytes(4, "little") + b"WAVE"
    hdr += b"fmt " + (16).to_bytes(4, "little")
    hdr += (1).to_bytes(2, "little")                 # PCM
    hdr += channels.to_bytes(2, "little")
    hdr += sample_rate.to_bytes(4, "little")
    hdr += byte_rate.to_bytes(4, "little")
    hdr += block_align.to_bytes(2, "little")
    hdr += bits.to_bytes(2, "little")
    hdr += b"data" + (0xFFFFFFFF).to_bytes(4, "little")
    return hdr


def wav_from_pcm16(pcm: bytes, sample_rate: int = TARGET_RATE, channels: int = 1) -> bytes:
    """Wrap complete PCM16 audio in a WAV container with real (not sentinel) sizes.

    Used wherever a whole utterance is already in memory, so the browser gets a
    strictly valid file rather than the streaming header's 0xFFFFFFFF sizes.
    """
    import struct

    bits = 16
    byte_rate = sample_rate * channels * bits // 8
    block_align = channels * bits // 8
    fmt = struct.pack("<HHIIHH", 1, channels, sample_rate, byte_rate, block_align, bits)
    data = pcm
    if len(data) % block_align:                     # pad to a whole frame
        data += b"\x00" * (block_align - len(data) % block_align)
    return (
        b"RIFF" + struct.pack("<I", 4 + 8 + len(fmt) + 8 + len(data)) + b"WAVE"
        + b"fmt " + struct.pack("<I", len(fmt)) + fmt
        + b"data" + struct.pack("<I", len(data)) + data
    )


# ---------------- ChatterBox metadata (shared by engine + API) ----------------

# Selectable ChatterBox engines, in dashboard order.
CHATTERBOX_MODELS = ("turbo", "nano", "multilingual", "original")

# Languages Chatterbox-Multilingual accepts as `language_id` (23 + English).
CHATTERBOX_LANGUAGES = {
    "ar": "Arabic", "da": "Danish", "de": "German", "el": "Greek",
    "en": "English", "es": "Spanish", "fi": "Finnish", "fr": "French",
    "he": "Hebrew", "hi": "Hindi", "it": "Italian", "ja": "Japanese",
    "ko": "Korean", "ms": "Malay", "nl": "Dutch", "no": "Norwegian",
    "pl": "Polish", "pt": "Portuguese", "ru": "Russian", "sv": "Swedish",
    "sw": "Swahili", "tr": "Turkish", "zh": "Chinese",
}

_CHATTERBOX_INSTALL_HINT = (
    "ChatterBox is not installed. Install it with `pip install chatterbox-tts` "
    "(it pulls in PyTorch; the first synthesis downloads ~2-3 GB of model "
    "weights). Meanwhile, set TTS_PROVIDER=elevenlabs, or give the engine a "
    "standby with TTS_STANDBY_PROVIDER=kokoro so it fails over automatically."
)


def _chatterbox_unavailable(exc: ImportError) -> ImportError:
    """Turn \"No module named 'torch'\" into an actionable, failable error.

    Returned as a ModuleNotFoundError named after the engine so
    `is_provider_error()` treats it as an unavailable engine (failover) instead
    of an internal bug.
    """
    missing = (getattr(exc, "name", "") or "chatterbox").split(".")[0]
    return ModuleNotFoundError(
        f"{_CHATTERBOX_INSTALL_HINT} (missing dependency: {missing})",
        name="chatterbox",
    )


class TTS:
    """TTS facade with primary/standby failover."""

    # Class level: shared by every call session, so an outage discovered during
    # one call is remembered by the next instead of re-failing each time.
    _health: dict[str, tuple[float, bool]] = {}
    _promoted: str | None = None
    _health_ttl = 60.0
    # Why the most recent failover happened. Class level on purpose: /health
    # builds a throwaway TTS instance and must still be able to show it.
    _last_failover: str = ""

    def __init__(self, config: Config):
        self.config = config
        self.primary = (config.tts_provider or "console").strip().lower()
        self.standby = (config.tts_standby_provider or "").strip().lower()
        if self.standby == self.primary:
            self.standby = ""
        self.provider = self.primary       # kept for backwards compatibility
        self._elevenlabs = None
        self.failover_reason = ""
        # What the server said it was (X-Voice-Engine / X-Voice-Model). Empty
        # until something has actually spoken through a local HTTP server.
        self.engine_reported = ""
        self.model_reported = ""

    # ---------------- provider bookkeeping ----------------

    @property
    def active(self) -> str:
        """The engine currently in use.

        The standby stays promoted only for as long as the primary is marked
        down — once that health record goes stale the primary is retried, so a
        recovered engine takes its role back instead of flapping forever.
        """
        promoted = type(self)._promoted
        if (
            promoted
            and promoted in (self.primary, self.standby)
            and promoted != self.primary
            and not self._healthy(self.primary)
        ):
            return promoted
        return self.primary

    def _candidates(self) -> list[str]:
        """Try order: active engine, then primary, then standby (de-duplicated)."""
        order: list[str] = []
        for name in (self.active, self.primary, self.standby):
            if name and name not in order:
                order.append(name)
        healthy = [p for p in order if self._healthy(p)]
        return healthy or order          # if everything looks down, try anyway

    @classmethod
    def _healthy(cls, provider: str) -> bool:
        rec = cls._health.get(provider)
        if not rec:
            return True                  # unknown -> give it a chance
        ts, ok = rec
        if time.time() - ts > cls._health_ttl:
            return True                  # stale -> re-test
        return ok

    @classmethod
    def _mark(cls, provider: str, ok: bool) -> None:
        cls._health[provider] = (time.time(), ok)

    @classmethod
    def reset_health(cls) -> None:
        """Forget every outage (used by tests / by the dashboard's Retry)."""
        cls._health = {}
        cls._promoted = None
        cls._last_failover = ""

    def _promote(self, provider: str) -> None:
        type(self)._promoted = provider if provider in (self.primary, self.standby) else None

    # ---------------- public API ----------------

    def synthesize(self, text: str) -> bytes:
        """Return one complete audio payload from the active engine."""
        if self.primary == "console":
            print(f"AGENT: {text}")
            return b""
        if self.primary in LOCAL_HTTP_PROVIDERS:
            return self._local_audio(text, "wav", self.primary)
        if self.primary == "chatterbox":
            return self._chatterbox_sync(text)
        if self.primary == "elevenlabs":
            return self._elevenlabs_sync(text)
        if self.primary == "deepgram":
            return self._deepgram(text)
        if self.primary == "openai":
            return self._openai(text)
        if self.primary == "azure":
            return self._azure(text)
        return b""

    def synthesize_stream(self, text: str):
        """Yield audio chunks for streaming playback (container-consistent)."""
        if self.primary == "console":
            print(f"AGENT: {text}")
            return
        if self.primary in LOCAL_HTTP_PROVIDERS:
            # Streaming WAV: sentinel header, then bare 16 kHz PCM chunks.
            yield streaming_wav_header(TARGET_RATE)
            yield from self._local_stream_sync(text, self.primary)
            return
        if self.primary == "chatterbox":
            yield self._chatterbox_sync(text)
            return
        if self.primary == "elevenlabs":
            yield from self._elevenlabs_stream(text)
            return
        audio = self.synthesize(text)
        if audio:
            yield audio

    def _kokoro_stream_sync(self, text: str):
        """Blocking stream of resampled PCM chunks (for synthesize_stream)."""
        import requests

        rs = PCM16Resampler(24000, TARGET_RATE)
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
                pcm = rs.feed(chunk)
                if pcm:
                    yield pcm
        tail = rs.flush()
        if tail:
            yield tail

    async def synthesize_stream_pcm(self, text: str):
        """Async generator of raw PCM16 @16 kHz mono, with failover.

        This is what the live voice pipeline consumes. If the active engine
        fails before producing audio we retry on the standby; if it fails
        halfway through we stop rather than replaying the line from the top
        (hearing a sentence restart is worse than a short silence).
        """
        last: BaseException | None = None
        failures: list[str] = []
        for provider in self._candidates():
            produced = 0
            try:
                async for chunk in self._pcm_from(provider, text):
                    produced += 1
                    yield chunk
                self._mark(provider, True)
                if provider != self.active:
                    self._promote(provider)
                return
            except asyncio.CancelledError:
                raise
            except BaseException as exc:      # noqa: BLE001 - classified below
                if not is_provider_error(exc) or produced:
                    raise                     # our bug, or audio already played
                last = exc
                failures.append(f"{provider}: {exc}")
                self._mark(provider, False)
                promoted = type(self)._promoted
                if promoted and provider == promoted:
                    # The standby we were relying on is down too — stop preferring
                    # it so the next call starts from the configured primary.
                    self._promote(None)
                    self.failover_reason = f"both engines unavailable: {exc}"
                    type(self)._last_failover = self.failover_reason
                elif provider == self.primary and self.standby:
                    self._promote(self.standby)
                    self.failover_reason = f"{provider} unavailable: {exc}"
                    type(self)._last_failover = self.failover_reason
        if last is not None:
            if len(failures) > 1:
                # Both engines died. Reporting only the second one loses the
                # more actionable first error (e.g. "install chatterbox-tts"),
                # so keep every cause in the message the operator will read.
                raise type(last)(" | ".join(failures)) from last
            raise last
        return

    def synthesize_pcm(self, text: str) -> bytes:
        """Blocking variant: raw PCM16 @16 kHz (used by tests and tooling)."""
        if self.primary == "console":
            return b""
        if self.primary in LOCAL_HTTP_PROVIDERS:
            return self._local_pcm(text, self.primary)
        if self.primary == "chatterbox":
            return self._chatterbox_sync(text)
        if self.primary == "elevenlabs":
            pcm, rate = _pcm_of(self._elevenlabs_sync(text))
            return resample_pcm16(pcm, rate) if rate else pcm
        pcm, rate = wav_to_pcm16(self.synthesize(text))
        return resample_pcm16(pcm, rate) if rate else pcm

    def status(self) -> dict:
        """Cheap health snapshot for /health and the dashboard."""
        engines = {}
        for name, entry in (
            ("elevenlabs", ("configured", bool(self.config.elevenlabs_api_key))),
            ("kokoro", ("url", True)),
            ("piper", ("url", True)),
            ("chatterbox", ("model", self.config.chatterbox_model)),
            ("deepgram", ("configured", bool(self.config.deepgram_api_key))),
            ("openai", ("configured", bool(self.config.openai_tts_api_key))),
            ("azure", ("configured", bool(self.config.azure_speech_key))),
        ):
            reachable = None
            if name in LOCAL_HTTP_PROVIDERS:
                reachable = self.probe_local(name)
            elif name == "elevenlabs":
                reachable = None if not entry[1] else self._healthy("elevenlabs")
            engines[name] = {
                "configured": bool(entry[1]),
                "reachable": reachable,
                "healthy": self._healthy(name),
            }
        return {
            "primary": self.primary,
            "standby": self.standby,
            "active": self.active,
            "failover_reason": self.failover_reason or type(self)._last_failover,
            "engines": engines,
        }

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

    # ---------------- provider: kokoro ----------------

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

# ---------------- provider: chatterbox ----------------
    # ChatterBox: Resemble AI open-source TTS. Runs in-process (no server needed).
    # Models: turbo (350M, English, paralinguistic tags), nano (110M, English, CPU),
    #         multilingual (500M, 23+ languages). Output @24 kHz → resampled to 16 kHz.
    _chatterbox_model_cache: dict = {}

    # Rough HF download sizes per engine — used only for a fast, friendly
    # pre-flight disk check so a doomed download fails in seconds.
    _chatterbox_model_gb = {"turbo": 2.5, "nano": 2.0, "multilingual": 3.0, "original": 3.0}

    def _chatterbox_disk_ok(self, need_gb: float) -> None:
        import os
        import shutil

        cache = os.environ.get("HF_HOME") or os.path.join(
            os.path.expanduser("~"), ".cache", "huggingface"
        )
        # The cache directory may not exist yet on a first run (or HF_HOME may
        # point somewhere not created), and disk_usage() raises
        # FileNotFoundError for a missing path — which would turn this friendly
        # pre-flight check into a crash. Measure the nearest existing parent,
        # i.e. the drive the weights would actually land on.
        probe = os.path.abspath(cache)
        while probe and not os.path.isdir(probe):
            parent = os.path.dirname(probe)
            if parent == probe:
                break
            probe = parent
        free = shutil.disk_usage(probe or os.path.abspath(os.sep)).free / (1024 ** 3)
        if free < need_gb:
            raise RuntimeError(
                f"ChatterBox needs ~{need_gb:.1f} GB free to download its model "
                f"weights, but only {free:.1f} GB is available on this drive. "
                f"Free up space (for example delete unused downloaded models) "
                f"and try again — or set HF_HOME to a drive with more room."
            )

    def _get_chatterbox_model(self):
        """Load and cache the ChatterBox model in-process."""
        key = (self.config.chatterbox_model, self.config.chatterbox_device,
               self.config.chatterbox_language)
        cached = self._chatterbox_model_cache.get(key)
        if cached is not None:
            return cached

        self._chatterbox_disk_ok(
            self._chatterbox_model_gb.get(self.config.chatterbox_model, 2.5)
        )

        try:
            if self.config.chatterbox_model == "multilingual":
                from chatterbox.mtl_tts import ChatterboxMultilingualTTS
                model = ChatterboxMultilingualTTS.from_pretrained(
                    device=self.config.chatterbox_device,
                )
            elif self.config.chatterbox_model in ("turbo", "nano"):
                # The published chatterbox-tts wheel ships a single Turbo engine;
                # the smaller "nano" variant only exists in the development
                # source tree. Map "nano" onto the same engine so the
                # switch-voice API keeps working on any installation.
                from chatterbox.tts_turbo import ChatterboxTurboTTS
                model = ChatterboxTurboTTS.from_pretrained(
                    device=self.config.chatterbox_device,
                )
            else:
                from chatterbox.tts import ChatterboxTTS
                model = ChatterboxTTS.from_pretrained(
                    device=self.config.chatterbox_device)
        except ImportError as exc:
            raise _chatterbox_unavailable(exc) from exc
        self._chatterbox_model_cache[key] = model
        return model

    def _chatterbox_sync(self, text: str) -> bytes:
        """Generate full audio from ChatterBox, return PCM16 @16 kHz."""
        try:
            import numpy as np
            import torch
        except ImportError as exc:
            raise _chatterbox_unavailable(exc) from exc

        model = self._get_chatterbox_model()
        voice_path = self.config.chatterbox_voice_path or None
        audio_prompt = voice_path if voice_path else None

        if self.config.chatterbox_model == "multilingual":
            wav = model.generate(
                text,
                language_id=self.config.chatterbox_language,
                audio_prompt_path=audio_prompt,
                exaggeration=self.config.chatterbox_exaggeration,
                cfg_weight=self.config.chatterbox_cfg_weight,
                temperature=self.config.chatterbox_temperature,
            )
        elif self.config.chatterbox_model in ("turbo", "nano"):
            wav = model.generate(
                text,
                audio_prompt_path=audio_prompt,
                temperature=self.config.chatterbox_temperature,
            )
        else:
            wav = model.generate(
                text,
                audio_prompt_path=audio_prompt,
                exaggeration=self.config.chatterbox_exaggeration,
                cfg_weight=self.config.chatterbox_cfg_weight,
                temperature=self.config.chatterbox_temperature,
            )

        if isinstance(wav, torch.Tensor):
            wav = wav.squeeze(0).cpu().numpy()

        # Normalize to int16
        peak = abs(wav).max() if len(wav) > 0 else 1
        if peak > 0:
            wav = (wav / peak * 0.95 * 32767).astype(np.int16)
        else:
            wav = (wav * 32767).astype(np.int16)
        pcm = wav.tobytes()

        # ChatterBox outputs at 24 kHz -> resample to 16 kHz
        return resample_pcm16(pcm, 24000, TARGET_RATE)

    async def _pcm_chatterbox(self, text: str):
        """Async: generate audio in a worker thread, resample to 16 kHz."""
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, lambda: self._chatterbox_sync(text)
        )
        if result:
            yield result

    def switch_chatterbox_model(self, model: str) -> str:
        """Switch to a different ChatterBox model at runtime."""
        if model not in CHATTERBOX_MODELS:
            raise ValueError(
                f"Invalid model '{model}'. Use one of: {', '.join(CHATTERBOX_MODELS)}")
        self.config.chatterbox_model = model
        self._chatterbox_model_cache.clear()
        return f"Switched to Chatterbox-{model}"

    @staticmethod
    def chatterbox_settings(body: dict) -> dict:
        """Validate + resolve a ChatterBox switch request into config changes.

        A single POST may carry several fields at once (that is what the
        documented curl does), so the precedence is explicit rather than
        accidental — and identical for the API and for callers that edit the
        config directly:

            model      → selects the engine
            voice      → reference wav for cloning (empty string clears it)
            language   → only meaningful for the multilingual engine, and only
                         the engine switch counts as "switched"

        Returns a dict with the resolved model / voice / language plus a
        `changed` flag and a human-readable `message`. Raises ValueError with a
        friendly message when a value is rejected.
        """
        model_given = "model" in body
        model = str(body.get("model") or "").strip()
        if model_given and not model:
            raise ValueError("empty model")
        if model and model not in CHATTERBOX_MODELS:
            raise ValueError(
                f"invalid model '{model}'. Use one of: {', '.join(CHATTERBOX_MODELS)}")

        voice_given = "voice" in body
        voice = str(body.get("voice") or "").strip()

        lang_given = "language" in body
        language = str(body.get("language") or "").strip().lower()
        if lang_given and not language:
            raise ValueError(
                "empty language. Use one of: " + ", ".join(CHATTERBOX_LANGUAGES))
        if language and language not in CHATTERBOX_LANGUAGES:
            raise ValueError(
                f"unsupported language '{language}'. Chatterbox-Multilingual "
                f"speaks: {', '.join(CHATTERBOX_LANGUAGES)}")

        # Precedence: the model asked for in this request, else the model class
        # implied by the language, else whatever is already configured.
        effective_model = model or ("multilingual" if language else "")
        if not effective_model:
            effective_model = "multilingual"

        # Changing/replacing the engine, cloning the voice, or re-targeting a
        # language all change how the line sounds — remember that something moved.
        changed = bool(model) or voice_given or bool(language)

        if language and model and model != "multilingual":
            # Accepting this silently would be the very bug this resolver exists
            # to prevent: the operator would believe Spanish was set while the
            # English-only engine ignores it.
            raise ValueError(
                f"the '{model}' engine is English-only, so it cannot speak "
                f"'{CHATTERBOX_LANGUAGES[language]}'. Use "
                f"model='multilingual' for {', '.join(CHATTERBOX_LANGUAGES)}."
            )

        message = f"Switched to Chatterbox-{effective_model}"
        return {
            "model": effective_model,
            "voice": voice if voice_given else None,
            "language": language if language else None,
            "changed": changed,
            "message": message,
        }

    @staticmethod
    def validate_voice_reference(path: str) -> str:
        """Check a voice-clone reference file is actually usable.

        Returns an empty string when it is fine, else a human-readable problem.
        ChatterBox loads this file from the *server's* filesystem, so a path
        that does not resolve there (a typo, or another machine's drive letter)
        must be reported now rather than blowing up mid-call.
        """
        import os

        if not path:
            return ""
        ext = os.path.splitext(path)[1].lower()
        audio_ext = (".wav", ".mp3", ".flac", ".m4a", ".ogg", ".opus", ".aiff", ".aif")
        if ext not in audio_ext:
            return (f"voice reference must be an audio file "
                    f"({', '.join(audio_ext)}), got '{path}'")
        if not os.path.isfile(path):
            return (
                f"voice reference not found on the server: {os.path.abspath(path)}. "
                f"ChatterBox reads this file from the machine running the bot — "
                f"upload/copy it there (10-15 s of clean speech works best), or "
                f"clear the field to use the built-in voice."
            )
        try:
            if os.path.getsize(path) == 0:
                return f"voice reference is empty: {path}"
        except OSError as exc:
            return f"voice reference is not readable ({exc}): {path}"
        return ""

    def chatterbox_model_info(self) -> dict:
        """Return info about the active ChatterBox model.

        `voice_cloned` only claims a clone when the reference file is really
        usable — reporting True for a typo'd path hid a broken setup behind a
        green tick. `voice_reference_problem` carries the reason when it is not.
        """
        model = self.config.chatterbox_model
        ref = (self.config.chatterbox_voice_path or "").strip()
        problem = self.validate_voice_reference(ref) if ref else ""
        info = {
            "model": model,
            "device": self.config.chatterbox_device,
            "voice_path": ref,
            "voice_cloned": bool(ref) and not problem,
            "voice_reference_problem": problem,
            "language": self.config.chatterbox_language,
            "sample_rate": 24000,
            "output_rate": TARGET_RATE,
            "paralinguistic_tags": model in ("turbo", "nano"),
            "multilingual": model == "multilingual",
            "available_models": list(CHATTERBOX_MODELS),
            "available_languages": dict(CHATTERBOX_LANGUAGES),
            "exaggeration": self.config.chatterbox_exaggeration,
            "cfg_weight": self.config.chatterbox_cfg_weight,
            "temperature": self.config.chatterbox_temperature,
        }
        if model == "turbo":
            info["description"] = "Chatterbox-Turbo (350M) — paralinguistic tags, low latency"
            info["languages"] = ["en"]
        elif model == "nano":
            info["description"] = "Chatterbox-Nano (110M) — CPU 3x realtime, smallest"
            info["languages"] = ["en"]
        elif model == "multilingual":
            info["description"] = "Chatterbox-Multilingual V3 (500M) — 23+ languages"
            info["languages"] = list(CHATTERBOX_LANGUAGES)
        else:
            info["description"] = "Chatterbox Original (500M) — English"
            info["languages"] = ["en"]
        return info

    # ---------------- provider: elevenlabs ----------------

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
        from .voice_ids import resolve_voice_id

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
        from .voice_ids import resolve_voice_id

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
        from .voice_ids import resolve_voice_id

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

    # ---------------- other providers ----------------

    def _pcm_from(self, provider: str, text: str):
        if provider == "console":
            print(f"AGENT: {text}")
            return _empty_aiter()
        if provider in LOCAL_HTTP_PROVIDERS:
            return self._pcm_local(provider, text)
        if provider == "chatterbox":
            return self._pcm_chatterbox(text)
        if provider == "elevenlabs":
            return self._pcm_elevenlabs(text)
        return self._pcm_via_container(provider, text)

    async def _pcm_via_container(self, provider: str, text: str):
        """Fallback: synthesise whole, strip the container, resample to 16 kHz."""
        def _synth():
            original, self.primary = self.primary, provider
            try:
                return self.synthesize(text)
            finally:
                self.primary = original

        raw = await asyncio.to_thread(_synth)
        pcm, rate = wav_to_pcm16(raw)
        if not pcm:
            raise ConnectionError(f"{provider} returned no usable audio")
        yield resample_pcm16(pcm, rate) if rate != TARGET_RATE else pcm

    def _deepgram(self, text: str) -> bytes:
        import requests

        url = (f"https://api.deepgram.com/v1/speak"
               f"?model={self.config.tts_voice}&container=wav")
        r = requests.post(
            url,
            headers={"Authorization": f"Token {self.config.deepgram_api_key}"},
            json={"text": text},
            timeout=60,
        )
        r.raise_for_status()
        return r.content

    def _openai(self, text: str) -> bytes:
        from openai import OpenAI

        client = OpenAI(api_key=self.config.openai_tts_api_key)
        resp = client.audio.speech.create(
            model=self.config.openai_tts_model,
            voice=self.config.openai_tts_voice,
            response_format="wav",
            input=text,
        )
        return resp.content

    def _azure(self, text: str) -> bytes:
        import requests

        endpoint = (
            f"https://{self.config.azure_speech_region}.tts.speech.microsoft.com/"
            f"cognitiveservices/v1"
        )
        ssml = (
            "<speak version='1.0' xml:lang='en-US'>"
            f"<voice xml:lang='en-US' name='{self.config.azure_voice}'>{text}</voice>"
            "</speak>"
        )
        r = requests.post(
            endpoint,
            headers={
                "Ocp-Apim-Subscription-Key": self.config.azure_speech_key,
                "Content-Type": "application/ssml+xml",
                "X-Microsoft-OutputFormat": "riff-16khz-16bit-mono-pcm",
            },
            data=ssml.encode("utf-8"),
            timeout=60,
        )
        r.raise_for_status()
        return r.content


async def _empty_aiter():
    """Async generator that yields nothing (console / silent engines)."""
    if False:                                     # noqa: SIM108 - keeps it a generator
        yield b""


def _pcm_of(container: bytes) -> tuple[bytes, int]:
    """Return raw s16le samples and their rate; containers are unwrapped."""
    if container[:4] == b"RIFF":
        return wav_to_pcm16(container)
    return container, TARGET_RATE
