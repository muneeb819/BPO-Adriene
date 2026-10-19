"""Provider selection for the LiveKit agent (env-driven, local-first).

One place decides which STT / LLM / TTS the LiveKit session runs with, so the
worker, the HTTP status endpoint and the tests always agree.

Environment
-----------
======================  =====================================================
LIVEKIT_STT_PROVIDER    auto | deepgram | openai | faster_whisper | none
LIVEKIT_TTS_PROVIDER    auto | local | openai | elevenlabs | none
LIVEKIT_LLM_PROVIDER    brain | openai | anthropic | gemini
LIVEKIT_TTS_VOICE       any installed Piper voice id (local engine)
LIVEKIT_STT_MODEL       faster-whisper size (base.en, small.en, …)
======================  =====================================================

``auto`` picks the best available: a cloud key wins, otherwise the local engine.
Nothing here ever raises because of a missing key or package — a debug run must
start and say what it is missing.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("adriene.livekit.factory")


def preload() -> None:
    """Import optional LiveKit plugins **on the main thread**.

    LiveKit's plugin registry refuses registration from worker threads, and
    FastAPI runs sync endpoints in a threadpool — so a lazy import inside a
    request would fail with "Plugins must be registered on the main thread".
    Warming the modules at startup makes every later import a cache hit.
    """
    for name in ("silero", "openai", "deepgram", "elevenlabs", "anthropic", "google"):
        try:
            __import__(f"livekit.plugins.{name}")
        except Exception:
            pass


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _has(name: str) -> bool:
    return bool(_env(name))


# --------------------------------------------------------------------------- #
#                         what each leg can actually run
# --------------------------------------------------------------------------- #
def _deepgram_ready() -> bool:
    if not _has("DEEPGRAM_API_KEY"):
        return False
    try:
        from livekit.plugins import deepgram  # noqa: F401, PLC0415
        return True
    except Exception:
        return False


def _openai_ready() -> bool:
    if not _has("OPENAI_API_KEY"):
        return False
    try:
        from livekit.plugins import openai  # noqa: F401, PLC0415
        return True
    except Exception:
        return False


def _elevenlabs_ready() -> bool:
    if not _has("ELEVENLABS_API_KEY"):
        return False
    try:
        from livekit.plugins import elevenlabs  # noqa: F401, PLC0415
        return True
    except Exception:
        return False


_silero_error: str = ""


def _silero_ready() -> bool:
    global _silero_error
    try:
        from livekit.plugins import silero  # noqa: F401, PLC0415
        _silero_error = ""
        return True
    except Exception as exc:
        _silero_error = f"{type(exc).__name__}: {exc}"
        logger.warning("silero VAD unavailable: %s", _silero_error)
        return False


# --------------------------------------------------------------------------- #
#                                  builders
# --------------------------------------------------------------------------- #
def build_stt():
    """Return an ``stt.STT`` or ``None`` (text-only / typed input still works)."""
    provider = _env("LIVEKIT_STT_PROVIDER", "auto").lower()

    if provider == "none":
        return None

    if provider in ("auto", "deepgram") and _deepgram_ready():
        from livekit.plugins import deepgram  # noqa: PLC0415

        logger.info("STT: Deepgram %s", _env("DEEPGRAM_MODEL", "nova-3"))
        return deepgram.STT(model=_env("DEEPGRAM_MODEL", "nova-3"))

    if provider in ("auto", "openai") and _openai_ready():
        from livekit.plugins import openai  # noqa: PLC0415

        logger.info("STT: OpenAI %s", _env("OPENAI_STT_MODEL", "whisper-1"))
        return openai.STT(model=_env("OPENAI_STT_MODEL", "whisper-1"))

    if provider in ("auto", "faster_whisper", "whisper", "local"):
        from . import whisper_stt  # noqa: PLC0415

        local = whisper_stt.from_env()
        if local is not None:
            logger.info("STT: %s (local)", local.model)
            return local
        if provider != "auto":
            logger.warning(
                "no STT available — install faster-whisper or set DEEPGRAM_API_KEY"
            )
        return None

    logger.warning("unknown LIVEKIT_STT_PROVIDER=%r — running without STT", provider)
    return None


def build_tts():
    """Return a ``tts.TTS`` or ``None`` (the caller still gets transcripts)."""
    provider = _env("LIVEKIT_TTS_PROVIDER", "auto").lower()

    if provider == "none":
        return None

    if provider in ("auto", "elevenlabs") and _elevenlabs_ready():
        from livekit.plugins import elevenlabs  # noqa: PLC0415

        logger.info("TTS: ElevenLabs %s", _env("ELEVENLABS_VOICE", "Rachel"))
        return elevenlabs.TTS(
            voice_id=_env("ELEVENLABS_VOICE_ID") or None,
            model=_env("ELEVENLABS_MODEL", "eleven_turbo_v2_5"),
        )

    if provider in ("auto", "openai") and _openai_ready():
        from livekit.plugins import openai  # noqa: PLC0415

        logger.info("TTS: OpenAI %s", _env("OPENAI_TTS_VOICE", "alloy"))
        return openai.TTS(
            model=_env("OPENAI_TTS_MODEL", "gpt-4o-mini-tts"),
            voice=_env("OPENAI_TTS_VOICE", "alloy"),
        )

    if provider in ("auto", "local", "piper", "kokoro"):
        from . import neural_tts  # noqa: PLC0415

        local = neural_tts.from_env()
        if local is not None:
            logger.info("TTS: local neural voice %s", local.model)
            return local
        logger.warning(
            "no local voice installed — run `python scripts/fetch_neural_voices.py` "
            "(the bot will be silent until then)"
        )
        return None

    logger.warning("unknown LIVEKIT_TTS_PROVIDER=%r — running without TTS", provider)
    return None


@dataclass
class LLMChoice:
    llm: object
    brain: Optional[object] = None          # BrainLLM when the script layer is on
    provider: str = "brain"


def build_llm(*, session_id: str = "", on_turn=None) -> LLMChoice:
    """The dialogue brain by default; an optional cloud model on request."""
    from .brain_llm import BrainLLM  # noqa: PLC0415

    provider = _env("LIVEKIT_LLM_PROVIDER", "brain").lower()
    brain = BrainLLM(session_id=session_id or None, on_turn=on_turn)

    if provider == "brain":
        return LLMChoice(llm=brain, brain=brain, provider="brain")

    try:
        if provider == "openai" and _openai_ready():
            from livekit.plugins import openai  # noqa: PLC0415

            logger.info("LLM: OpenAI %s (script held by the prompt)",
                        _env("OPENAI_MODEL", "gpt-4o-mini"))
            return LLMChoice(
                llm=openai.LLM(model=_env("OPENAI_MODEL", "gpt-4o-mini")),
                brain=None, provider="openai",
            )
        if provider == "anthropic" and _has("ANTHROPIC_API_KEY"):
            from livekit.plugins import anthropic  # noqa: PLC0415

            return LLMChoice(
                llm=anthropic.LLM(model=_env("ANTHROPIC_MODEL", "claude-sonnet-4-5")),
                brain=None, provider="anthropic",
            )
        if provider == "gemini" and (_has("GEMINI_API_KEY") or _has("GOOGLE_API_KEY")):
            from livekit.plugins import google  # noqa: PLC0415

            return LLMChoice(
                llm=google.LLM(model=_env("GEMINI_MODEL", "gemini-2.0-flash")),
                brain=None, provider="gemini",
            )
        logger.warning(
            "LIVEKIT_LLM_PROVIDER=%s is not usable (missing key or plugin) — "
            "falling back to the local brain", provider,
        )
    except Exception as exc:
        logger.warning("cloud LLM %s failed to start (%s) — using the brain",
                       provider, exc)

    return LLMChoice(llm=brain, brain=brain, provider="brain")


def build_vad():
    """Silero VAD ships its own ONNX model inside the wheel — always offline."""
    if not _silero_ready():
        logger.warning("livekit-plugins-silero is not installed — no VAD/turn detection")
        return None
    from livekit.plugins import silero  # noqa: PLC0415

    return silero.VAD.load()


# --------------------------------------------------------------------------- #
#                                status report
# --------------------------------------------------------------------------- #
@dataclass
class StackStatus:
    stt: str = "none"
    tts: str = "none"
    llm: str = "brain"
    vad: bool = False
    voices: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "stt": self.stt, "tts": self.tts, "llm": self.llm, "vad": self.vad,
            "voices": self.voices, "warnings": self.warnings,
        }


def describe_stack() -> dict:
    """What a LiveKit call would run with right now (no side effects)."""
    from . import neural_tts, whisper_stt  # noqa: PLC0415

    status = StackStatus()
    status.vad = _silero_ready()
    if not status.vad:
        status.warnings.append(
            "livekit-plugins-silero missing — install it for VAD"
            + (f" ({_silero_error})" if _silero_error else "")
        )

    stt_env = _env("LIVEKIT_STT_PROVIDER", "auto").lower()
    if stt_env == "none":
        status.stt = "none"
    elif _deepgram_ready():
        status.stt = "deepgram"
    elif _openai_ready() and stt_env in ("auto", "openai"):
        status.stt = "openai"
    elif whisper_stt.available():
        status.stt = "faster-whisper"
    else:
        status.stt = "none"
        status.warnings.append(
            "no speech recognition: set DEEPGRAM_API_KEY / OPENAI_API_KEY or "
            "`pip install faster-whisper` (typed input still works)"
        )

    tts_env = _env("LIVEKIT_TTS_PROVIDER", "auto").lower()
    if tts_env == "none":
        status.tts = "none"
    elif _elevenlabs_ready() and tts_env in ("auto", "elevenlabs"):
        status.tts = "elevenlabs"
    elif _openai_ready() and tts_env in ("auto", "openai"):
        status.tts = "openai"
    elif neural_tts.available():
        status.tts = "local-neural"
    else:
        status.tts = "none"
        status.warnings.append(
            "no voice: run `python scripts/fetch_neural_voices.py` and "
            "`pip install piper-tts`, or set ELEVENLABS_API_KEY / OPENAI_API_KEY"
        )
    status.voices = neural_tts.voice_ids()

    llm_env = _env("LIVEKIT_LLM_PROVIDER", "brain").lower()
    if llm_env != "brain" and not (
        (llm_env == "openai" and _openai_ready())
        or (llm_env == "anthropic" and _has("ANTHROPIC_API_KEY"))
        or (llm_env == "gemini" and (_has("GEMINI_API_KEY") or _has("GOOGLE_API_KEY")))
    ):
        status.warnings.append(
            f"LIVEKIT_LLM_PROVIDER={llm_env} is not usable — the local brain runs instead"
        )
        status.llm = "brain"
    else:
        status.llm = llm_env

    return status.as_dict()
