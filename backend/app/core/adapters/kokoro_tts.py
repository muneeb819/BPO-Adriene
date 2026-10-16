"""Kokoro-82M local neural TTS with the full American English voice pack.

All 20 US-English voices (lang_code='a'), with quality/training grades from the
Kokoro model card so the UI can present each voice's "nature":

    af_heart   A   (top female, warm expressive)      am_puck   C+  (male, balanced)
    af_bella   A-  (female 🔥 energetic)              am_fenrir C+  (male)
    af_nicole  B-  (female 🎧 calm/narrator)          am_michael C+ (male)
    ...

Design notes
------------
- Model weights (~330 MB) and per-voice embeddings download from HF on first
  use and are cached; ``warm()`` pre-fetches everything in a background thread
  so live calls never pay the download cost.
- Kokoro does its best work on 100-200 token chunks: we split replies into
  sentences, bundle very short fragments (<10 tokens) with their neighbour,
  and cap long chunks so the model neither rushes nor drags.
- CPU RTF is ~1.0 (synth time ~= audio length), so callers should pipeline at
  sentence granularity (play chunk N while synthesizing chunk N+1).
- The whole module degrades to ``available() == False`` when kokoro/torch are
  missing or the model can't be fetched — the browser voice engine then takes
  over with zero user impact.
"""
from __future__ import annotations

import io
import os
import re
import threading
import wave
from collections import OrderedDict
from typing import Dict, List, Optional, Tuple

# Xet chunk-downloader reconstructs files in temp space (multi-GB spikes on
# Windows); plain HTTP keeps disk usage equal to the final file size.
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

SAMPLE_RATE = 24000

# fmt: off
# The20 American English voices (misaki lang_code='a'), with Kokoro model-card grades.
VOICES: List[Dict[str, str]] = [
    {"name": "af_heart",   "gender": "f", "traits": "\U0001F6BA\u2764\ufe0f",    "quality": "A",  "training": "\u2014",      "grade": "A",  "sha": "0ab5709b", "blurb": "Warm & heartfelt \u2014 our default Adriene"},
    {"name": "af_bella",   "gender": "f", "traits": "\U0001F6BA\U0001F525",      "quality": "A",  "training": "HH hours",    "grade": "A-", "sha": "8cb64e02", "blurb": "Energetic, bright, expressive"},
    {"name": "af_nicole",  "gender": "f", "traits": "\U0001F6BA\U0001F3A7",      "quality": "B",  "training": "HH hours",    "grade": "B-", "sha": "c5561808", "blurb": "Soft headphone narrator, calm & reassuring"},
    {"name": "af_kore",    "gender": "f", "traits": "\U0001F6BA",                "quality": "B",  "training": "H hours",     "grade": "C+", "sha": "8bfbc512", "blurb": "Steady, friendly, professional"},
    {"name": "af_sarah",   "gender": "f", "traits": "\U0001F6BA",                "quality": "B",  "training": "H hours",     "grade": "C+", "sha": "49bd364e", "blurb": "Clear & composed"},
    {"name": "af_aoede",   "gender": "f", "traits": "\U0001F6BA",                "quality": "B",  "training": "H hours",     "grade": "C+", "sha": "c03bd1a4", "blurb": "Warm mid-range"},
    {"name": "af_alloy",   "gender": "f", "traits": "\U0001F6BA",                "quality": "B",  "training": "MM minutes",  "grade": "C",  "sha": "6d877149", "blurb": "Neutral & even"},
    {"name": "af_nova",    "gender": "f", "traits": "\U0001F6BA",                "quality": "B",  "training": "MM minutes",  "grade": "C",  "sha": "e0233676", "blurb": "Light & quick"},
    {"name": "af_jessica", "gender": "f", "traits": "\U0001F6BA",                "quality": "C",  "training": "MM minutes",  "grade": "D",  "sha": "cdfdccb8", "blurb": "Casual, a touch quirky"},
    {"name": "af_river",   "gender": "f", "traits": "\U0001F6BA",                "quality": "C",  "training": "MM minutes",  "grade": "D",  "sha": "e149459b", "blurb": "Youthful, laid-back"},
    {"name": "af_sky",     "gender": "f", "traits": "\U0001F6BA",                "quality": "B",  "training": "M minutes \U0001F90F", "grade": "C-", "sha": "c799548a", "blurb": "Airy, very short training"},
    {"name": "am_puck",    "gender": "m", "traits": "\U0001F464",                "quality": "B",  "training": "H hours",     "grade": "C+", "sha": "dd1d8973", "blurb": "Bright, upbeat male"},
    {"name": "am_fenrir",  "gender": "m", "traits": "\U0001F464",                "quality": "B",  "training": "H hours",     "grade": "C+", "sha": "98e507ec", "blurb": "Confident, grounded"},
    {"name": "am_michael", "gender": "m", "traits": "\U0001F464",                "quality": "B",  "training": "H hours",     "grade": "C+", "sha": "9a443b79", "blurb": "Classic announcer"},
    {"name": "am_echo",    "gender": "m", "traits": "\U0001F464",                "quality": "C",  "training": "MM minutes",  "grade": "D",  "sha": "8bcfdc85", "blurb": "Mellow"},
    {"name": "am_eric",    "gender": "m", "traits": "\U0001F464",                "quality": "C",  "training": "MM minutes",  "grade": "D",  "sha": "ada66f0e", "blurb": "Relaxed baritone"},
    {"name": "am_liam",    "gender": "m", "traits": "\U0001F464",                "quality": "C",  "training": "MM minutes",  "grade": "D",  "sha": "c8255075", "blurb": "Smooth, modern"},
    {"name": "am_onyx",    "gender": "m", "traits": "\U0001F464",                "quality": "C",  "training": "MM minutes",  "grade": "D",  "sha": "e8452be1", "blurb": "Deep & direct"},
    {"name": "am_adam",    "gender": "m", "traits": "\U0001F464",                "quality": "D",  "training": "H hours",     "grade": "F+", "sha": "ced7e284", "blurb": "Thin training, use as flavor test"},
    {"name": "am_santa",   "gender": "m", "traits": "\U0001F464",                "quality": "C",  "training": "M minutes \U0001F90F", "grade": "D-", "sha": "7f2f7582", "blurb": "Seasonal novelty"},
]
# fmt: on

VOICE_NAMES = [v["name"] for v in VOICES]
DEFAULT_VOICE = "af_heart"
DEFAULT_SPEED = 1.0

# Kokoro's sweet spot is 100-200 tokens: below ~10 it is weak, above ~400 it
# rushes. We chunk at sentence boundaries and bundle tiny fragments.
_MIN_CHUNK_WORDS = 4      # bundle fragments shorter than this into the neighbour
_MAX_CHUNK_WORDS = 60     # hard-split longer sentences at clause boundaries
_GAP_SECONDS = 0.14       # natural breath between sentences

_lock = threading.Lock()
_pipeline = None          # kokoro.KPipeline once loaded
_load_error: Optional[str] = None
_cache: "OrderedDict[Tuple[str, str, float], bytes]" = OrderedDict()
_CACHE_MAX = 96


def available() -> bool:
    """True when kokoro is importable (model may still be downloading)."""
    try:
        import kokoro  # noqa: F401
        import torch  # noqa: F401
        return True
    except Exception:
        return False


def _get_pipeline():
    """Lazy-load the KPipeline singleton (thread-safe)."""
    global _pipeline, _load_error
    if _pipeline is not None:
        return _pipeline
    with _lock:
        if _pipeline is None:
            try:
                from kokoro import KPipeline
                _pipeline = KPipeline(lang_code="a")
                _load_error = None
            except Exception as e:  # missing model / offline / disk
                _load_error = f"{type(e).__name__}: {e}"
                raise
    return _pipeline


def warm(voices: Optional[List[str]] = None) -> bool:
    """Pre-load the model and pre-fetch voice embeddings (call from a thread)."""
    if not available():
        return False
    try:
        pipe = _get_pipeline()
        targets = voices or VOICE_NAMES
        for name in targets:
            # A tiny run per voice downloads its .bin and exercises the path.
            _ = list(pipe("Warm.", voice=name, speed=1.0))
        return True
    except Exception:
        return False


def _split_sentences(text: str) -> List[str]:
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text.strip()) if p.strip()]
    return parts or ([text.strip()] if text.strip() else [])


def _words(s: str) -> int:
    return len(s.split())


def chunk_text(text: str) -> List[str]:
    """Split a reply into Kokoro-friendly chunks.

    - sentences are the natural boundary
    - fragments under ~4 words are bundled with their neighbour (Kokoro is
      weak on very short utterances)
    - long sentences hard-split at clause boundaries under ~60 words
    """
    chunks: List[str] = []
    for sent in _split_sentences(text):
        w = _words(sent)
        if w > _MAX_CHUNK_WORDS:
            clauses = [c.strip() for c in re.split(r"(?<=[,;:])\s+", sent) if c.strip()]
            buf = ""
            for cl in clauses:
                if buf and _words(buf) + _words(cl) > _MAX_CHUNK_WORDS:
                    chunks.append(buf)
                    buf = cl
                else:
                    buf = f"{buf} {cl}".strip()
            # word-level fallback for long sentences with no clause punctuation
            while buf and _words(buf) > _MAX_CHUNK_WORDS:
                words = buf.split()
                cut = " ".join(words[:_MAX_CHUNK_WORDS])
                # end the chunk at the last comma inside the window if present
                if "," in cut:
                    cut = cut[: cut.rfind(",") + 1]
                chunks.append(cut.strip())
                buf = buf[len(cut):].strip()
            if buf:
                chunks.append(buf)
        else:
            chunks.append(sent)
    # bundle tiny fragments into a neighbour (Kokoro is weak under ~10 tokens)
    bundled: List[str] = []
    for ch in chunks:
        if _words(ch) < _MIN_CHUNK_WORDS and bundled:
            # attach to previous (already-spoken context reads naturally)
            bundled[-1] = f"{bundled[-1]} {ch}"
        else:
            bundled.append(ch)
    if len(bundled) > 1 and _words(bundled[-1]) < _MIN_CHUNK_WORDS:
        # tiny trailing fragment → fold back into the previous chunk
        bundled[-2] = f"{bundled[-2]} {bundled[-1]}"
        bundled.pop()
    if len(bundled) > 1 and _words(bundled[0]) < _MIN_CHUNK_WORDS:
        # tiny leading fragment (e.g. "Hey!") → fold forward
        bundled[1] = f"{bundled[0]} {bundled[1]}"
        bundled.pop(0)
    return [c for c in bundled if c]


def _to_wav_bytes(audio_chunks: List) -> bytes:
    """Concatenate float32 chunks (+ breath gaps) into 16-bit PCM WAV bytes."""
    import numpy as np

    gap = np.zeros(int(_GAP_SECONDS * SAMPLE_RATE), dtype=np.float32)
    pieces = []
    for i, a in enumerate(audio_chunks):
        if i:
            pieces.append(gap)
        pieces.append(np.asarray(a, dtype=np.float32))
    audio = np.concatenate(pieces) if pieces else np.zeros(SAMPLE_RATE // 10, dtype=np.float32)
    audio = np.clip(audio, -1.0, 1.0)
    pcm = (audio * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def synthesize(text: str, voice: str = DEFAULT_VOICE, speed: float = DEFAULT_SPEED) -> bytes:
    """Synthesize ``text`` to WAV bytes. Raises when kokoro is unavailable."""
    text = (text or "").strip()
    if not text:
        raise ValueError("empty text")
    if voice not in VOICE_NAMES:
        voice = DEFAULT_VOICE
    try:
        speed = max(0.5, min(2.0, float(speed)))
    except (TypeError, ValueError):
        speed = DEFAULT_SPEED

    key = (text, voice, round(speed, 2))
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]

    chunks = chunk_text(text)
    pipe = _get_pipeline()
    audio_parts = []
    with _lock:  # kokoro inference is not re-entrant; serialize voices
        for ch in chunks:
            for _gs, _ps, audio in pipe(ch, voice=voice, speed=speed):
                audio_parts.append(audio)
    wav = _to_wav_bytes(audio_parts)

    with _lock:
        _cache[key] = wav
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)
    return wav


def is_warmed() -> bool:
    """True once the model has finished loading (pipeline singleton exists)."""
    return _pipeline is not None


def status() -> Dict[str, object]:
    """Snapshot for /api/tts/voices."""
    return {
        "installed": available(),
        "warmed": is_warmed(),
        "load_error": _load_error,
        "default_voice": DEFAULT_VOICE,
        "default_speed": DEFAULT_SPEED,
        "voices": VOICES,
    }
