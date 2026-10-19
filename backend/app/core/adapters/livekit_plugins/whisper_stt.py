"""Optional local speech recognition for LiveKit (``faster-whisper``).

Cloud STT is the best experience (Deepgram is what the LiveKit examples use), but
a local-first bot should still work with no key. ``faster-whisper`` runs Whisper on
CPU through CTranslate2 — no torch, ~75 MB for ``base.en`` — which is plenty for a
two-question Medicare qualification call.

The model is downloaded from Hugging Face on first use and cached in
``~/.cache/huggingface``. Install with::

    pip install faster-whisper

and select it with ``LIVEKIT_STT_PROVIDER=faster_whisper``. If the package or
model is missing, :func:`from_env` returns ``None`` and the agent keeps running
(the browser console and the room's text channel can still drive the dialogue).
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Optional

import numpy as np
from livekit import rtc
from livekit.agents import stt
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions, NOT_GIVEN, NotGivenOr

logger = logging.getLogger("adriene.livekit.stt")

_MODEL_LOCK = threading.Lock()


def _flatten(buffer) -> tuple[np.ndarray, int]:
    """``utils.AudioBuffer`` -> (mono float32 in [-1, 1], sample_rate)."""
    frames = buffer if isinstance(buffer, list) else [buffer]
    if not frames:
        return np.zeros(0, dtype=np.float32), 16000
    if len(frames) == 1:
        frame = frames[0]
        pcm = np.frombuffer(frame.data, dtype=np.int16)
        if frame.num_channels > 1:
            pcm = pcm.reshape(-1, frame.num_channels).mean(axis=1).astype(np.int16)
        rate = frame.sample_rate
    else:
        merged = rtc.combine_audio_frames(frames)
        pcm = np.frombuffer(merged.data, dtype=np.int16)
        if merged.num_channels > 1:
            pcm = pcm.reshape(-1, merged.num_channels).mean(axis=1).astype(np.int16)
        rate = merged.sample_rate
    return (pcm.astype(np.float32) / 32768.0), rate


def _resample(samples: np.ndarray, src: int, dst: int) -> np.ndarray:
    if src == dst or samples.size == 0:
        return samples
    count = int(round(samples.size * dst / float(src)))
    if count <= 0:
        return samples
    # linear interpolation is enough for 16 kHz speech recognition input
    x_old = np.linspace(0.0, 1.0, samples.size, endpoint=False)
    x_new = np.linspace(0.0, 1.0, count, endpoint=False)
    return np.interp(x_new, x_old, samples).astype(np.float32)


class FasterWhisperSTT(stt.STT):
    """Batch recognizer: VAD closes the utterance, Whisper transcribes it."""

    def __init__(
        self,
        *,
        model: str = "base.en",
        device: str = "cpu",
        compute_type: str = "int8",
        language: str = "en",
        beam_size: int = 1,
    ) -> None:
        super().__init__(
            capabilities=stt.STTCapabilities(streaming=False, interim_results=False)
        )
        self._model_name = model
        self._device = device
        self._compute_type = compute_type
        self._language = language
        self._beam_size = beam_size
        self._model = None

    # ------------------------------------------------------------------ meta
    @property
    def model(self) -> str:
        return f"faster-whisper/{self._model_name}"

    @property
    def provider(self) -> str:
        return "faster-whisper"

    # ------------------------------------------------------------------- api
    def prewarm(self) -> None:
        try:
            self._ensure_model()
        except Exception as exc:            # offline / model not cached yet
            logger.warning(
                "faster-whisper '%s' is not ready yet (%s); it will retry on the "
                "first utterance. The first run downloads the model.",
                self._model_name, exc,
            )

    def _ensure_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel  # noqa: PLC0415

            with _MODEL_LOCK:
                if self._model is None:
                    logger.info(
                        "loading faster-whisper %s (%s/%s) — first run downloads it",
                        self._model_name, self._device, self._compute_type,
                    )
                    self._model = WhisperModel(
                        self._model_name,
                        device=self._device,
                        compute_type=self._compute_type,
                    )
        return self._model

    async def _recognize_impl(
        self,
        buffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> stt.SpeechEvent:
        samples, rate = _flatten(buffer)
        if samples.size == 0:
            return stt.SpeechEvent(
                type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                alternatives=[stt.SpeechData(language=self._language, text="")],
            )

        audio = _resample(samples, rate, 16000)
        lang = language if isinstance(language, str) and language else self._language

        def _transcribe() -> str:
            model = self._ensure_model()
            segments, _info = model.transcribe(
                audio,
                language=lang or None,
                beam_size=self._beam_size,
                vad_filter=True,
                condition_on_previous_text=False,
            )
            return " ".join(seg.text.strip() for seg in segments).strip()

        try:
            text = await asyncio.to_thread(_transcribe)
        except Exception as exc:            # never kill a call over STT
            logger.error("speech recognition failed: %s", exc)
            text = ""
        logger.debug("stt: %r", text)
        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            alternatives=[stt.SpeechData(language=lang or "en", text=text or "")],
        )


def available() -> bool:
    try:
        import faster_whisper  # noqa: F401, PLC0415
        return True
    except Exception:
        return False


def from_env() -> Optional[FasterWhisperSTT]:
    if not available():
        logger.warning(
            "LIVEKIT_STT_PROVIDER=faster_whisper but faster-whisper is not "
            "installed (`pip install faster-whisper`)"
        )
        return None
    try:
        return FasterWhisperSTT(
            model=os.getenv("LIVEKIT_STT_MODEL", "base.en"),
            device=os.getenv("LIVEKIT_STT_DEVICE", "cpu"),
            compute_type=os.getenv("LIVEKIT_STT_COMPUTE", "int8"),
            language=os.getenv("LIVEKIT_STT_LANGUAGE", "en"),
        )
    except Exception as exc:
        logger.warning("could not start faster-whisper: %s", exc)
        return None
