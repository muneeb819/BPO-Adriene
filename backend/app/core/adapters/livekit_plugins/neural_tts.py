"""The project's local neural voice as a LiveKit ``tts.TTS``.

LiveKit normally expects a cloud TTS plugin. Adriene already has a better fit for
a local-first bot: real Piper/ONNX voices in ``data/models/piper`` rendered by
``agent/neural_tts.py`` (the same engine the browser console uses). This adapter
bridges them, so a LiveKit call speaks with the exact same voice as the local
console — no key, no per-character cost, nothing leaving the machine.

Pipeline::

    AgentSession -> synth(text) -> ChunkedStream._run()
                 -> agent.neural_tts.synthesize(..., response_format="pcm")
                 -> AudioEmitter (s16le, 16 kHz mono) -> WebRTC

Voices are ~60 MB each and are not committed; fetch them with
``python scripts/fetch_neural_voices.py``. Until then ``available()`` is False
and the factory simply runs the session without TTS instead of crashing.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Optional

from livekit.agents import tts, utils
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions

logger = logging.getLogger("adriene.livekit.tts")

#: Sample rate the local engine renders at for the call pipeline (mono, s16le).
PIPELINE_RATE = 16000

_REPO_ROOT = Path(__file__).resolve().parents[5]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_LOAD_LOCK = threading.Lock()


def _engine():
    """Import ``agent.neural_tts`` lazily (it needs piper + a voice on disk)."""
    # agent.neural_tts defaults to the *relative* path data/models/piper, which
    # breaks when the backend runs from backend/. Pin it to this checkout.
    voices = _REPO_ROOT / "data" / "models" / "piper"
    if voices.is_dir() and not os.environ.get("PIPER_MODELS_DIR"):
        os.environ["PIPER_MODELS_DIR"] = str(voices)
    try:
        from agent import neural_tts  # noqa: PLC0415  (repo-root module)
        return neural_tts
    except Exception as exc:  # pragma: no cover - depends on install
        logger.debug("local neural TTS unavailable: %s", exc)
        return None


def available() -> bool:
    """True when piper is importable *and* at least one voice is installed."""
    engine = _engine()
    if engine is None:
        return False
    try:
        return bool(engine.available())
    except Exception:
        return False


def voice_ids() -> list[str]:
    engine = _engine()
    if engine is None:
        return []
    try:
        return [m["id"] for m in engine.list_models()]
    except Exception:
        return []


def pick_voice(preferred: str = "") -> Optional[str]:
    """Resolve the configured voice, falling back to the middle register."""
    ids = voice_ids()
    if not ids:
        return None
    if preferred:
        if preferred in ids:
            return preferred
        for vid in ids:                      # allow "amy" -> en_US-amy-low
            if preferred.lower() in vid.lower():
                return vid
        logger.warning("voice %r is not installed; using a default", preferred)
    return ids[len(ids) // 2]


class LocalNeuralTTS(tts.TTS):
    """Piper/ONNX voices rendered in-process, streamed to the room as PCM."""

    def __init__(
        self,
        *,
        voice: str = "",
        speed: float = 1.0,
        pitch: int = 50,
        sample_rate: int = PIPELINE_RATE,
    ) -> None:
        engine = _engine()
        if engine is None or not available():
            raise RuntimeError(
                "local neural TTS is not installed — run "
                "`python scripts/fetch_neural_voices.py` and `pip install piper-tts`"
            )
        resolved = pick_voice(voice)
        if resolved is None:
            raise RuntimeError("no neural voices found in data/models/piper")

        self._engine = engine
        self._voice = resolved
        self._speed = max(0.5, min(2.0, float(speed or 1.0)))
        self._pitch = max(0, min(99, int(pitch)))
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=int(sample_rate),
            num_channels=1,
        )

    # ------------------------------------------------------------------ meta
    @property
    def model(self) -> str:
        return self._voice

    @property
    def provider(self) -> str:
        return "piper-local"

    # ------------------------------------------------------------------- api
    def synthesize(
        self,
        text: str,
        *,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> "LocalNeuralTTSStream":
        return LocalNeuralTTSStream(
            tts=self, input_text=text, conn_options=conn_options
        )

    # -------------------------------------------------------------- internals
    def _render(self, text: str) -> bytes:
        """Blocking render: 16 kHz mono s16le PCM."""
        with _LOAD_LOCK:
            return self._engine.synthesize(
                text,
                self._voice,
                pitch_level=self._pitch,
                speed=self._speed,
                rate=self.sample_rate,
                response_format="pcm",
            )

    def prewarm(self) -> None:
        """Load the ONNX voice before the first caller speaks."""
        try:
            engine = self._engine
            if hasattr(engine, "load"):
                engine.load(self._voice)
        except Exception:                     # prewarm is best-effort
            logger.debug("voice prewarm failed", exc_info=True)


class LocalNeuralTTSStream(tts.ChunkedStream):
    """One sentence in, one PCM buffer out."""

    def __init__(
        self,
        *,
        tts: LocalNeuralTTS,
        input_text: str,
        conn_options: APIConnectOptions,
    ) -> None:
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._tts = tts

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        text = (self.input_text or "").strip()

        # Even an empty request must initialize the emitter: LiveKit flushes it
        # on the way out and raises if it was never started.
        request_id = utils.shortuuid("piper-")
        if not text:
            output_emitter.initialize(
                request_id=request_id,
                sample_rate=self._tts.sample_rate,
                num_channels=1,
                mime_type="audio/pcm",
            )
            output_emitter.flush()
            return
        try:
            pcm = await asyncio.to_thread(self._tts._render, text)
        except Exception as exc:              # never leave a call silent
            logger.error("local TTS failed for %r: %s", text[:80], exc)
            raise

        output_emitter.initialize(
            request_id=request_id,
            sample_rate=self._tts.sample_rate,
            num_channels=1,
            mime_type="audio/pcm",
        )
        output_emitter.push(pcm)
        output_emitter.flush()


def from_env() -> Optional[LocalNeuralTTS]:
    """Build from ``LIVEKIT_TTS_VOICE`` / ``LIVEKIT_TTS_SPEED``; None if absent."""
    if not available():
        return None
    try:
        return LocalNeuralTTS(
            voice=os.getenv("LIVEKIT_TTS_VOICE") or os.getenv("PIPER_VOICE", ""),
            speed=float(os.getenv("LIVEKIT_TTS_SPEED", "1.0")),
            pitch=int(os.getenv("LIVEKIT_TTS_PITCH", "50")),
        )
    except Exception as exc:
        logger.warning("could not start the local neural voice: %s", exc)
        return None
