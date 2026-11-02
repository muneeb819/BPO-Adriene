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
import time

from .config import Config
from .resample import PCM16Resampler, TARGET_RATE, resample_pcm16, wav_to_pcm16
# Re-exported for voice_library and the API, which import these from agent.tts.
from .tts_engines.chatterbox import (  # noqa: F401
    CHATTERBOX_LANGUAGES,
    CHATTERBOX_MODELS,
    ChatterboxEngine,
    _chatterbox_unavailable,
)
from .tts_engines.elevenlabs import ElevenLabsEngine
from .tts_engines.kokoro import KokoroEngine
from .tts_engines.local_http import LocalHttpEngine

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

class TTS(ElevenLabsEngine, KokoroEngine, ChatterboxEngine, LocalHttpEngine):
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
