"""The voice record, the engine constants, and the validation helpers.

Pure data and pure functions: no disk access and no engine calls beyond the
ChatterBox language table, so everything here can be imported (and tested)
without a voice server or a library on disk.
"""
from __future__ import annotations

import base64
import binascii
import re
import time
from array import array
from dataclasses import dataclass, field

from ..audio_dsp import AudioError

LIBRARY_VERSION = 1

#: engines a profile can target; "kokoro" covers the OpenAI-compatible local
#: server (Kokoro, or the bundled neural Piper voices).
#: Engines a profile can target. "kokoro" and "piper" are both served by a
#: self-hosted OpenAI-compatible HTTP server; piper is the neural ONNX one that
#: needs no torch and no cloud key (see agent/neural_tts.py).
#: "elevenlabs" profiles point at a voice id on ElevenLabs — that is where a
#: Voice Design result lives, and the reference sample we download with it makes
#: the voice usable by the local cloner/matcher too.
ENGINES = ("chatterbox", "kokoro", "piper", "elevenlabs")
REMIX_MODES = ("blend", "stitch")
TUNING_KEYS = ("exaggeration", "cfg_weight", "temperature", "speed", "pitch",
               "volume")
MAX_UPLOAD_BYTES = 25 * 1024 * 1024


def validate_engine_language(engine: str, model: str, language: str) -> None:
    """Mirror the /api/tts/switch-voice rules so a saved voice cannot be invalid."""
    if (engine or "").lower() != "chatterbox" or not language or language == "en":
        return
    from ..tts import CHATTERBOX_LANGUAGES

    if model != "multilingual":
        raise AudioError(
            f"the '{model or 'turbo'}' ChatterBox model is English-only — use "
            f"'multilingual' to speak {language}")
    if language not in CHATTERBOX_LANGUAGES:
        raise AudioError(f"unknown language '{language}' — use one of: "
                         f"{', '.join(CHATTERBOX_LANGUAGES)}")


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return slug[:40] or "voice"


def _default_piper_voice() -> str:
    """A middle-register resident voice, or the first one installed."""
    try:
        from .. import neural_tts

        models = neural_tts.describe_models()
        if not models:
            return ""
        return models[len(models) // 2]["id"] if len(models) > 1 else models[0]["id"]
    except Exception:
        return ""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


@dataclass
class VoiceProfile:
    """A saved voice. `ref` is relative to the library directory."""

    id: str
    name: str
    engine: str = "chatterbox"
    model: str = "turbo"
    language: str = "en"
    voice: str = ""                      # engine voice id (kokoro/piper backends)
    ref: str = ""                        # clone reference file name, "" = built-in
    tuning: dict = field(default_factory=dict)
    tags: list = field(default_factory=list)
    notes: str = ""
    source: str = "designed"             # uploaded | recorded | imported | designed | remixed | captured
    created: str = ""
    last_used: str = ""
    duration_sec: float = 0.0
    sample_rate: int = 0
    preview_text: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "engine": self.engine,
            "model": self.model, "language": self.language, "voice": self.voice,
            "ref": self.ref, "has_reference": bool(self.ref),
            "tuning": dict(self.tuning), "tags": list(self.tags),
            "notes": self.notes, "source": self.source, "created": self.created,
            "last_used": self.last_used, "duration_sec": self.duration_sec,
            "sample_rate": self.sample_rate, "preview_text": self.preview_text,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "VoiceProfile":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


def clean_tuning(tuning: "dict | None") -> dict:
    """Keep only known numeric settings, clamped to sane ranges."""
    limits = {
        "exaggeration": (0.0, 1.0), "cfg_weight": (0.0, 1.0),
        "temperature": (0.05, 2.0), "speed": (0.5, 2.0), "pitch": (0, 99),
        "volume": (0.2, 2.0),
    }
    out: dict = {}
    for key, value in (tuning or {}).items():
        if key not in TUNING_KEYS or value in (None, ""):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise AudioError(f"{key} must be a number, got {value!r}") from None
        low, high = limits[key]
        out[key] = float(max(low, min(high, number)))
        if key == "pitch":
            out[key] = int(out[key])
    return out


def clean_tags(tags) -> list:
    if not tags:
        return []
    if isinstance(tags, str):
        tags = [t for t in re.split(r"[,\s]+", tags) if t]
    cleaned = []
    for tag in tags:
        tag = str(tag).strip()
        if not tag:
            continue
        if not tag.startswith("["):
            tag = f"[{tag.strip('[]')}]"
        if tag not in cleaned:
            cleaned.append(tag)
    return cleaned


def _samples(pcm: bytes) -> "array":
    """Raw little-endian s16le bytes -> int16 sample array."""
    if not pcm:
        raise AudioError("no audio data received")
    body = pcm[: len(pcm) - (len(pcm) % 2)]
    samples = array("h")
    samples.frombytes(body)
    return samples


def decode_base64(payload: str, *, what: str = "audio") -> bytes:
    """Decode a data-url or bare base64 string from the browser."""
    if not payload:
        raise AudioError(f"no {what} data received")
    text = payload.split(",", 1)[1] if payload.startswith("data:") else payload
    try:
        return base64.b64decode(text, validate=False)
    except (binascii.Error, ValueError) as e:
        raise AudioError(f"could not decode the uploaded {what}: {e}") from e
