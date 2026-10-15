"""Local **neural** speech synthesis (Piper / ONNX) — real voices, no cloud.

Everything in this module runs on CPU inside the project directory. It exists to
close the two gaps the voice studio used to have:

1. **Judgeable audio without a cloud key.** A formant synthesiser (espeak-ng)
   is fine for testing plumbing and useless for judging what a clone or a blend
   actually sounds like. These are full neural models (VITS exported to ONNX),
   ~60 MB each, loaded straight from ``data/models/piper`` — no torch, no cloud.

2. **Prompt-to-voice.** ``design_from_text()`` turns a description such as
   *"warm older British man, speaks slowly"* into a concrete neural voice plus
   tuning, and can explain every choice it made.

How the pieces fit together::

    reference wav ──analyze_voice──┐
                                   ├─ match_reference ─► model + pitch + speed
    model characteristics ─────────┘                        │
                                                            ▼
    description ──design_from_text──► model + tuning ──► synthesize ──► 16 kHz wav

The voices themselves are **not** vendored in git (60 MB each). Fetch them with
``python scripts/fetch_neural_voices.py``; until you do, :func:`available` is
``False`` and the studio says so instead of pretending.
"""
from __future__ import annotations

import json
import math
import os
import threading
from array import array

from . import audio_dsp as dsp

#: Where the .onnx voices live. Override with ``PIPER_MODELS_DIR``.
DEFAULT_MODELS_DIR = os.path.join("data", "models", "piper")
#: Rate every engine in this project speaks at the boundary (call pipeline).
PIPELINE_RATE = 16000
#: The pitch slider is 0-99 with 50 = the voice's own pitch (same scale the
#: Kokoro backend uses, so saved presets keep their meaning).
NEUTRAL_PITCH = 50
#: Slider units per semitone. 1 step ~ 0.12 st, so the full 0-99 travel is ±6 st.
SEMITONES_PER_STEP = 0.12
#: Never shift further than this when matching a reference — resampling artefacts
#: become obvious past about half an octave.
MAX_MATCH_SEMITONES = 4.0

#: One sentence with plenty of voiced sounds, used to measure a voice's pitch and
#: brightness so models can be compared objectively.
PROBE_TEXT = ("The quick brown fox jumps over the lazy dog while we measure "
              "the colour of this voice.")

_LOCK = threading.RLock()
_VOICES: dict = {}          # model id -> PiperVoice
_CHARACTERS: dict = {}      # model id -> measured characteristics


class NeuralTTSError(RuntimeError):
    """Raised when a neural voice cannot be produced (missing model, etc.)."""


# --------------------------------------------------------------------------- #
# model discovery
# --------------------------------------------------------------------------- #
def models_dir() -> str:
    return os.environ.get("PIPER_MODELS_DIR") or DEFAULT_MODELS_DIR


def piper_importable() -> bool:
    try:
        import piper.voice  # noqa: F401
    except Exception:
        return False
    return True


def available() -> bool:
    """True when piper is installed *and* at least one voice file is on disk."""
    return piper_importable() and bool(list_models())


def _model_paths() -> list[tuple[str, str, str]]:
    """(model_id, onnx path, json path) for every voice in the models directory."""
    root = models_dir()
    if not os.path.isdir(root):
        return []
    found = []
    for name in sorted(os.listdir(root)):
        if not name.endswith(".onnx"):
            continue
        model_id = name[: -len(".onnx")]
        path = os.path.join(root, name)
        cfg = os.path.join(root, f"{name}.json")
        if os.path.isfile(cfg) and os.path.getsize(path) > 1_000_000:
            found.append((model_id, path, cfg))
    return found


def _config(model_id: str, json_path: str) -> dict:
    try:
        with open(json_path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def list_models() -> "list[dict]":
    """Every usable local voice, cheapest fields only (no model loading)."""
    out = []
    for model_id, path, cfg_path in _model_paths():
        cfg = _config(model_id, cfg_path)
        lang = str(cfg.get("language", {}).get("code") or cfg.get("language") or "")
        out.append({
            "id": model_id,
            "name": _pretty_name(model_id),
            "language": lang.replace("_", "-"),
            "sample_rate": int(cfg.get("audio", {}).get("sample_rate") or 0),
            "num_speakers": int(cfg.get("num_speakers") or 1),
            "bytes": os.path.getsize(path),
            "kind": _kind(model_id),
        })
    return out


def _pretty_name(model_id: str) -> str:
    """``en_US-lessac-medium`` -> ``Lessac (US English, medium)``."""
    parts = model_id.split("-")
    if len(parts) < 3:
        return model_id
    locale, speaker, quality = parts[0], parts[1], parts[2]
    region = {"en_US": "US English", "en_GB": "British English",
              "es_ES": "Spanish", "fr_FR": "French", "de_DE": "German"}.get(
                  locale, locale.replace("_", " "))
    name = speaker.replace("_", " ").title()
    return f"{name} ({region}, {quality})"


def _kind(model_id: str) -> str:
    """A rough gender hint from the speaker name, for the designer's menu."""
    tokens = set(model_id.lower().split("-")[1].split("_"))
    if tokens & {"amy", "jenny", "kristin", "cori", "alba", "siwis", "kathleen",
                 "southern", "female", "libritts"}:
        return "feminine"
    if tokens & {"lessac", "alan", "ryan", "joe", "davefx", "northern", "male"}:
        return "masculine"
    return "neutral"


# --------------------------------------------------------------------------- #
# measurement (this is what makes matching and design honest)
# --------------------------------------------------------------------------- #
def _as_ints(samples) -> "list[int]":
    if isinstance(samples, array):
        return samples
    return array("h", [dsp.to_i16(s) for s in samples])


def _np():
    import numpy as np

    return np


def analyze_voice(samples, rate: int, *, max_seconds: float = 8.0) -> dict:
    """Pitch, brightness and level of a recording — the numbers matching uses.

    F0 comes from per-frame autocorrelation (median over voiced frames, which is
    far more stable than a mean), brightness from the spectral centroid, both
    restricted to frames loud enough to be speech.
    """
    np = _np()
    data = _as_ints(samples)
    if rate <= 0 or len(data) < rate // 10:
        return {"f0_hz": 0.0, "brightness_hz": 0.0, "level": 0.0,
                "voiced_ratio": 0.0, "seconds": 0.0}
    x = np.asarray(data, dtype=np.float32) / 32768.0
    cap = int(max_seconds * rate)
    if len(x) > cap:
        x = x[:cap]
    frame = max(256, int(rate * 0.04))          # 40 ms — a couple of glottal cycles
    hop = frame // 2
    floor_hz, ceil_hz = 70.0, 400.0
    min_lag, max_lag = int(rate / ceil_hz), min(int(rate / floor_hz), frame - 2)
    f0s, centroids = [], []
    for start in range(0, len(x) - frame, hop):
        chunk = x[start:start + frame]
        energy = float(np.sqrt(np.mean(np.square(chunk))))
        if energy < 0.02:                        # silence / unvoiced
            continue
        chunk = chunk - float(chunk.mean())
        ac = np.correlate(chunk, chunk, mode="full")[frame - 1:]
        if ac[0] <= 0:
            continue
        ac = ac / ac[0]
        window = ac[min_lag:max_lag]
        if not len(window):
            continue
        lag = int(np.argmax(window)) + min_lag
        if ac[lag] < 0.3:                        # no clear periodicity
            continue
        f0s.append(rate / lag)
        spectrum = np.abs(np.fft.rfft(chunk * np.hanning(len(chunk))))
        freqs = np.fft.rfftfreq(len(chunk), 1.0 / rate)
        total = float(spectrum.sum())
        if total > 0:
            centroids.append(float((spectrum * freqs).sum() / total))
    peak = dsp.peak(data) or 1
    return {
        "f0_hz": float(np.median(f0s)) if f0s else 0.0,
        "brightness_hz": float(np.median(centroids)) if centroids else 0.0,
        "level": peak / 32768.0,
        "voiced_ratio": len(f0s) / max(1, (len(x) - frame) // hop),
        "seconds": len(x) / rate,
    }


def characterize(model_id: str, *, refresh: bool = False) -> dict:
    """Measure a voice by synthesising :data:`PROBE_TEXT` once and analysing it.

    Cached next to the model (``<voice>.voice.json``) so matching stays instant.
    """
    with _LOCK:
        if not refresh and model_id in _CHARACTERS:
            return dict(_CHARACTERS[model_id])
        cache_path = os.path.join(models_dir(), f"{model_id}.voice.json")
        if not refresh and os.path.isfile(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as fh:
                    cached = json.load(fh)
                _CHARACTERS[model_id] = cached
                return dict(cached)
            except Exception:
                pass
        wav = synthesize(PROBE_TEXT, model_id, rate=PIPELINE_RATE)
        samples, rate = dsp.load_audio(wav)
        stats = analyze_voice(samples, rate)
        stats["model"] = model_id
        stats["probe_seconds"] = round(dsp.duration_seconds(samples, rate), 2)
        # words per second, from the probe sentence (10 words)
        stats["words_per_second"] = round(10 / stats["probe_seconds"], 2) if stats["probe_seconds"] else 0.0
        _CHARACTERS[model_id] = stats
        try:
            with open(cache_path, "w", encoding="utf-8") as fh:
                json.dump(stats, fh, indent=2)
        except OSError:
            pass
        return dict(stats)


def describe_models(*, characterise: bool = True) -> "list[dict]":
    """Model list plus the measured numbers, ordered from lowest pitch upward."""
    models = list_models()
    if not characterise:
        return models
    for entry in models:
        try:
            stats = characterize(entry["id"])
            entry["f0_hz"] = stats.get("f0_hz", 0.0)
            entry["brightness_hz"] = stats.get("brightness_hz", 0.0)
            entry["words_per_second"] = stats.get("words_per_second", 0.0)
            entry["characterised"] = bool(stats.get("f0_hz"))
        except Exception as exc:                      # a broken model must not kill the list
            entry["characterised"] = False
            entry["error"] = str(exc)[:200]
    usable = [m for m in models if m.get("characterised")]
    if usable:
        return sorted(models, key=lambda m: (not m.get("characterised"), m.get("f0_hz") or 0))
    return models


# --------------------------------------------------------------------------- #
# synthesis
# --------------------------------------------------------------------------- #
def load(model_id: str):
    """Load (and cache) a voice. Loading a 60 MB ONNX graph takes ~0.2 s."""
    from piper import PiperVoice

    with _LOCK:
        voice = _VOICES.get(model_id)
        if voice is not None:
            return voice
        match = [p for p in _model_paths() if p[0] == model_id]
        if not match:
            known = ", ".join(m["id"] for m in list_models()) or "none installed"
            raise NeuralTTSError(
                f"unknown local neural voice '{model_id}' (installed: {known}). "
                f"Run: python scripts/fetch_neural_voices.py")
        _, path, cfg_path = match[0]
        voice = PiperVoice.load(path, config_path=cfg_path)
        _VOICES[model_id] = voice
        return voice


def unload(model_id: "str | None" = None) -> None:
    with _LOCK:
        if model_id:
            _VOICES.pop(model_id, None)
        else:
            _VOICES.clear()


def pitch_level_to_semitones(level: float) -> float:
    """Studio pitch slider (0-99, 50 = neutral) to semitones."""
    return (float(level) - NEUTRAL_PITCH) * SEMITONES_PER_STEP


def semitones_to_pitch_level(semitones: float) -> int:
    """The inverse of :func:`pitch_level_to_semitones`, clamped to the slider."""
    return int(round(max(0.0, min(99.0, NEUTRAL_PITCH + semitones / SEMITONES_PER_STEP))))


def _length_scale(semitones: float, speed: float) -> float:
    """Piper's duration factor.

    Left at 1.0: the voice renders at the pace it was trained to speak, and
    ``speed`` is applied afterwards as a measured time-stretch (see
    :func:`synthesize`). Piper's own ``length_scale`` was the obvious place for
    the speed control, but its duration response is affine-ish — it includes a
    fixed per-utterance overhead — so a requested 0.75x came out around 1.15x,
    and "slow" was only ever vaguely slow. Measuring and stretching instead makes
    the slider mean exactly what it says.
    """
    return 1.0


def synthesize(text: str, model_id: str, *, pitch_level: float = NEUTRAL_PITCH,
               speed: float = 1.0, volume: float = 1.0,
               rate: int = PIPELINE_RATE, response_format: str = "wav",
               noise_scale: "float | None" = None,
               noise_w_scale: "float | None" = None) -> bytes:
    """Render ``text`` with a local neural voice. Returns wav or raw s16le PCM.

    ``pitch_level`` is the studio's 0-99 slider and ``speed`` the pace multiplier.
    Both are applied so that they do not interfere: the model speaks at its own
    pace, then the audio is time-stretched to the requested pace (measured, so
    the result is exact by construction) and finally pitch-shifted with the
    time-preserving :func:`audio_dsp.shift_pitch`. ``noise_scale`` /
    ``noise_w_scale`` default to what Piper ships with; pass 0 to get repeatable
    audio out of a model whose durations are otherwise sampled.
    """
    text = (text or "").strip()
    if not text:
        raise NeuralTTSError("nothing to say — the text was empty")
    from piper import SynthesisConfig

    voice = load(model_id)
    config = SynthesisConfig(
        length_scale=_length_scale(pitch_level_to_semitones(pitch_level), speed),
        volume=max(0.05, min(4.0, float(volume or 1.0))),
        normalize_audio=True,
    )
    if noise_scale is not None:
        config.noise_scale = float(noise_scale)
    if noise_w_scale is not None:
        config.noise_w_scale = float(noise_w_scale)
    pcm = bytearray()
    src_rate = 0
    for chunk in voice.synthesize(text, config):
        src_rate = chunk.sample_rate or src_rate
        pcm.extend(chunk.audio_int16_bytes)
    if not pcm:
        raise NeuralTTSError(f"the voice '{model_id}' produced no audio for that text")

    samples = array("h")
    samples.frombytes(bytes(pcm))
    src_rate = src_rate or 22050
    pace = max(0.25, min(4.0, float(speed or 1.0)))
    if abs(pace - 1.0) > 0.02:
        # exact pace: the length of this very render is what gets scaled
        samples = dsp.time_stretch(samples, src_rate, 1.0 / pace)
    semitones = max(-12.0, min(12.0, pitch_level_to_semitones(pitch_level)))
    if semitones:
        samples = dsp.shift_pitch(samples, src_rate, semitones)
    out_rate = int(rate or PIPELINE_RATE)
    if src_rate != out_rate:
        samples = dsp.resample(samples, src_rate, out_rate)
    if (response_format or "wav").lower() == "pcm":
        return samples.tobytes()
    return dsp.wav_bytes(samples, out_rate)


# --------------------------------------------------------------------------- #
# matching a clone reference onto a neural voice
# --------------------------------------------------------------------------- #
def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def match_reference(samples, rate: int, *, language: "str | None" = None,
                    candidates: "list[str] | None" = None) -> dict:
    """Pick the neural voice closest to a reference recording and say why.

    This is *voice matching*, not speaker-embedding cloning: the reference is
    measured (pitch, brightness, level) and the nearest resident neural voice is
    chosen, then its pitch is nudged toward the reference. It is deliberately
    explainable — every number below is returned to the studio.
    """
    wanted = analyze_voice(samples, rate)
    pool = describe_models()
    if language:
        pref = language.lower().replace("_", "-")
        short = pref.split("-")[0]
        exact = [m for m in pool if (m.get("language") or "").lower().replace("_", "-") == pref]
        same = [m for m in pool if (m.get("language") or "").lower().startswith(short)]
        pool = exact or same or pool
    if candidates:
        wanted_ids = set(candidates)
        pool = [m for m in pool if m["id"] in wanted_ids] or pool
    usable = [m for m in pool if m.get("characterised") and (m.get("f0_hz") or 0) > 0]
    if not usable:
        raise NeuralTTSError(
            "no measured local neural voice is available — run "
            "python scripts/fetch_neural_voices.py")
    ref_f0 = wanted["f0_hz"] or 0.0
    if not ref_f0:                                  # unvoiced/noisy reference
        chosen = usable[len(usable) // 2]
        return {
            "model": chosen["id"], "pitch_level": NEUTRAL_PITCH, "speed": 1.0,
            "volume": 1.0, "confidence": 0.0, "reference": wanted,
            "voice_stats": {k: chosen.get(k) for k in ("f0_hz", "brightness_hz")},
            "why": "the reference had no clearly voiced speech, so the middle "
                   "voice was used with neutral tuning",
        }

    def distance(entry: dict) -> float:
        """Register first, timbre as the tie-breaker.

        Perceived pitch is logarithmic, and getting the register wrong is what
        makes a match sound like "somebody else", so the pitch term is unweighted.
        Brightness is capped at a quarter of the pitch gap: a recording with an
        unusual spectrum (a synthetic tone, a phone line, heavy compression) may
        tip a close call but can never outvote a clearly better register.
        """
        pitch_term = abs(math.log(ref_f0 / float(entry["f0_hz"])))
        b_ref = wanted["brightness_hz"] or 1500.0
        b_mod = float(entry.get("brightness_hz") or 1500.0)
        bright_term = abs(math.log(b_ref / b_mod)) if b_mod > 0 else 0.0
        return pitch_term + 0.25 * min(bright_term, 0.4 + pitch_term)

    chosen = min(usable, key=distance)
    semitones = _clamp(12.0 * math.log2(ref_f0 / float(chosen["f0_hz"])),
                       -MAX_MATCH_SEMITONES, MAX_MATCH_SEMITONES)
    pitch_level = semitones_to_pitch_level(semitones)
    # A reference that is much slower than the model gets a gentle pace match;
    # pace estimated from a clip is unreliable, so it is deliberately mild.
    speed = 1.0
    model_wps = float(chosen.get("words_per_second") or 0.0)
    if model_wps and wanted["seconds"] > 0:
        # Only nudge when the recording is obviously stretched or rushed.
        if wanted["voiced_ratio"] < 0.25:
            speed = 0.95
        elif wanted["voiced_ratio"] > 0.75:
            speed = 1.05
    level = _clamp((wanted["level"] or 0.0) / 0.8, 0.6, 1.4)
    conf = _clamp(1.0 - distance(chosen), 0.0, 1.0)
    return {
        "model": chosen["id"],
        "pitch_level": pitch_level,
        "speed": round(speed, 3),
        "volume": round(level, 3),
        "confidence": round(conf, 3),
        "reference": {k: round(v, 3) if isinstance(v, float) else v
                      for k, v in wanted.items()},
        "voice_stats": {k: chosen.get(k) for k in ("f0_hz", "brightness_hz", "words_per_second")},
        "why": (f"reference pitch ~{ref_f0:.0f} Hz, brightness ~{b_ref_of(wanted):.0f} Hz; "
                f"closest resident voice is {chosen['id']} "
                f"(~{chosen['f0_hz']:.0f} Hz, ~{b_ref_of(chosen):.0f} Hz) — "
                f"pitch moved {semitones:+.1f} semitones to close the gap"),
        "alternatives": [
            {"model": m["id"], "f0_hz": m.get("f0_hz"), "distance": round(distance(m), 3)}
            for m in sorted(usable, key=distance)[:3]
        ],
    }


def b_ref_of(stats: dict) -> float:
    """Tiny helper so the evidence sentence reads the same for both inputs."""
    return float(stats.get("brightness_hz") or 1500.0)


# --------------------------------------------------------------------------- #
# prompt-to-voice (local, explainable)
# --------------------------------------------------------------------------- #
#: Words that steer the voice. Each hit is recorded so the studio can show the
#: exact reason a voice came out the way it did.
_VOCAB: "dict[str, tuple[str, float]]" = {
    # gender / register
    "man": ("gender", -1.0), "male": ("gender", -1.0), "guy": ("gender", -1.0),
    "masculine": ("gender", -1.0), "gentleman": ("gender", -1.0),
    "woman": ("gender", 1.0), "female": ("gender", 1.0), "lady": ("gender", 1.0),
    "feminine": ("gender", 1.0), "girl": ("gender", 1.0),
    # pitch
    "deep": ("pitch", -1.0), "low": ("pitch", -0.8), "bass": ("pitch", -1.0),
    "gravelly": ("pitch", -0.7), "husky": ("pitch", -0.6), "booming": ("pitch", -0.8),
    "high": ("pitch", 0.8), "bright": ("pitch", 0.6), "light": ("pitch", 0.6),
    "airy": ("pitch", 0.5), "soprano": ("pitch", 1.0),
    # age
    "old": ("age", 1.0), "older": ("age", 1.0), "elderly": ("age", 1.2),
    "senior": ("age", 1.0), "aged": ("age", 1.0), "veteran": ("age", 0.7),
    "young": ("age", -1.0), "youthful": ("age", -0.9), "teen": ("age", -1.0),
    # pace
    "slow": ("pace", -1.0), "slowly": ("pace", -1.0), "calm": ("pace", -0.6),
    "relaxed": ("pace", -0.6), "measured": ("pace", -0.5), "patient": ("pace", -0.5),
    "fast": ("pace", 1.0), "quickly": ("pace", 1.0), "energetic": ("pace", 1.0),
    "brisk": ("pace", 0.8), "upbeat": ("pace", 0.7), "rapid": ("pace", 1.0),
    # accent
    "british": ("accent", 1.0), "uk": ("accent", 1.0), "london": ("accent", 1.0),
    "england": ("accent", 1.0), "posh": ("accent", 0.8),
    "american": ("accent", -1.0), "us": ("accent", -1.0), "usa": ("accent", -1.0),
    # mood (volume + a touch of pace)
    "warm": ("mood", 1.0), "friendly": ("mood", 1.0), "kind": ("mood", 0.8),
    "soothing": ("mood", 1.0), "professional": ("mood", 0.4),
    "serious": ("mood", -0.8), "authoritative": ("mood", -0.6),
    "confident": ("mood", -0.3), "flat": ("mood", -0.9), "monotone": ("mood", -1.0),
    "cheerful": ("mood", 0.9), "excited": ("mood", 0.8), "whisper": ("mood", -1.0),
}

def _tokenize(description: str) -> "list[str]":
    out = []
    for raw in (description or "").lower().replace(",", " ").replace(".", " ").split():
        token = "".join(ch for ch in raw if ch.isalnum() or ch == "-")
        if token:
            out.append(token)
    return out


def design_from_text(description: str, *, language: "str | None" = None,
                     pitch_level: "float | None" = None,
                     speed: "float | None" = None) -> dict:
    """Turn a voice description into a resident neural voice + tuning.

    Returns ``{model, pitch_level, speed, volume, mood, why, tokens}``. Anything
    the caller passes explicitly (``pitch_level``/``speed``) wins over the words,
    so the studio's sliders can still fine-tune a described voice.
    """
    tokens = _tokenize(description)
    hits: "list[dict]" = []
    score = {"gender": 0.0, "pitch": 0.0, "age": 0.0, "pace": 0.0,
             "accent": 0.0, "mood": 0.0}
    for token in tokens:
        # a trailing plural ("voices", "men") should still hit the base word
        for candidate in (token, token.rstrip("s"), token.rstrip("e")):
            if candidate in _VOCAB:
                axis, weight = _VOCAB[candidate]
                score[axis] += weight
                hits.append({"word": token, "axis": axis, "weight": weight})
                break

    pool = describe_models()
    if language:
        pref = language.lower().replace("_", "-")
        short = pref.split("-")[0]
        exact = [m for m in pool if (m.get("language") or "").lower().replace("_", "-") == pref]
        same = [m for m in pool if (m.get("language") or "").lower().startswith(short)]
        pool = exact or same or pool
    usable = [m for m in pool if m.get("characterised")]
    if not usable:
        raise NeuralTTSError(
            "no measured local neural voice is available — run "
            "python scripts/fetch_neural_voices.py")

    # Nothing in the description says where the voice should sit and no slider was
    # given: use a voice at its own pitch. Bending it toward an arbitrary target
    # register would add artefacts to answer a question nobody asked.
    asked_for_register = bool(pitch_level is not None
                              or score["gender"] or score["pitch"] or score["age"])
    if not asked_for_register:
        chosen = min(usable, key=lambda m: abs((m.get("f0_hz") or 0) - _target_f0(NEUTRAL_PITCH)))
        final_speed = 1.0 if speed is None else float(speed)
        if speed is None:
            final_speed += score["pace"] * 0.12 + score["mood"] * 0.02
        return {
            "model": chosen["id"],
            "model_name": chosen.get("name") or chosen["id"],
            "pitch_level": NEUTRAL_PITCH,
            "speed": round(_clamp(final_speed, 0.6, 1.6), 3),
            "volume": round(_clamp(1.0 + score["mood"] * 0.15, 0.6, 1.4), 3),
            "mood": "warm" if score["mood"] > 0.3 else ("flat" if score["mood"] < -0.3 else "neutral"),
            "scores": {k: round(v, 2) for k, v in score.items()},
            "tokens": tokens,
            "matched_words": hits,
            "voice_stats": {k: chosen.get(k) for k in ("f0_hz", "brightness_hz", "words_per_second")},
            "why": (f"no register was requested, so {chosen['id']} "
                    f"(~{chosen.get('f0_hz', 0):.0f} Hz) is used at its natural pitch"
                    + (f"; pace {final_speed:.2f}x" if abs(final_speed - 1.0) > 0.02 else "")),
        }

    # 1. work out the register the description is asking for, on the same 0-99
    #    scale the studio slider uses (50 = neutral, higher = higher pitch).
    register = NEUTRAL_PITCH if pitch_level is None else float(pitch_level)
    register += score["gender"] * 10.0      # "man" -> lower register
    register += score["pitch"] * 12.0       # "deep" -> lower, "bright" -> higher
    register += score["age"] * -2.0         # a voice that has aged sits a little lower
    accent_pref = "en-GB" if score["accent"] > 0 else ("en-US" if score["accent"] < 0 else "")
    if accent_pref and language is None:
        accented = [m for m in usable
                    if (m.get("language") or "").lower().replace("_", "-") == accent_pref.lower()]
        if accented:
            usable = accented
    register = _clamp(register, 0, 99)

    # 2. pick the resident voice. The heavy lifting is done by *choosing* a voice
    #    rather than by bending one, so the result stays natural.
    chosen = min(usable, key=lambda m: abs((m.get("f0_hz") or 0) - _target_f0(register)))
    if len(usable) > 1:
        # a gender hint outranks the arithmetic: a "woman" description must not
        # land on the lowest-pitched voice just because the register maths did
        if score["gender"] > 0 and chosen["f0_hz"] <= usable[0]["f0_hz"] + 1:
            chosen = usable[-1]
        elif score["gender"] < 0 and chosen["f0_hz"] >= usable[-1]["f0_hz"] - 1:
            chosen = usable[0]

    # 3. bend the chosen voice to close whatever register gap is left, capped so
    #    it never sounds like a cartoon.
    gap_st = 12.0 * math.log2(_target_f0(register) / (chosen.get("f0_hz") or _target_f0(register)))
    gap_st = _clamp(gap_st, -MAX_MATCH_SEMITONES, MAX_MATCH_SEMITONES)
    final_pitch = semitones_to_pitch_level(gap_st)
    final_speed = 1.0 if speed is None else float(speed)
    if speed is None:
        final_speed += score["pace"] * 0.12          # "slow" -> 0.88 words/sec
        final_speed += score["age"] * -0.05          # older speaks a little slower
    final_speed = _clamp(final_speed, 0.6, 1.6)
    volume = _clamp(1.0 + score["mood"] * 0.15, 0.6, 1.4)
    mood = "warm" if score["mood"] > 0.3 else ("flat" if score["mood"] < -0.3 else "neutral")

    why_bits = [f"{len(tokens)} word(s) read"]
    for hit in hits[:6]:
        why_bits.append(f"'{hit['word']}' -> {hit['axis']} {hit['weight']:+.1f}")
    why_bits.append(f"description asks for a register of {register:.0f}/99 "
                    f"(~{_target_f0(register):.0f} Hz)")
    why_bits.append(f"closest resident voice is {chosen['id']} "
                    f"(~{chosen.get('f0_hz', 0):.0f} Hz)")
    if accent_pref:
        why_bits.append(f"accent filtered to {accent_pref}")
    why_bits.append(f"so pitch {final_pitch}/99 ({gap_st:+.1f} semitones), "
                    f"speed {final_speed:.2f}x")
    return {
        "model": chosen["id"],
        "model_name": chosen.get("name") or chosen["id"],
        "pitch_level": final_pitch,
        "speed": round(final_speed, 3),
        "volume": round(volume, 3),
        "mood": mood,
        "scores": {k: round(v, 2) for k, v in score.items()},
        "tokens": tokens,
        "matched_words": hits,
        "voice_stats": {k: chosen.get(k) for k in ("f0_hz", "brightness_hz", "words_per_second")},
        "why": "; ".join(why_bits),
    }


def _target_f0(level: float) -> float:
    """The pitch (Hz) a register level is asking for, on a musical scale.

    Neutral (50) is 150 Hz and every 10 slider steps is a major third, so the
    scale spans roughly 75-300 Hz — wider than the resident voices on purpose, so
    the same number can describe a whispery female and a booming male.
    """
    return 150.0 * (2.0 ** ((float(level) - NEUTRAL_PITCH) / 10.0 * 4.0 / 12.0))


def health() -> dict:
    """What the offline server and /health report."""
    models = list_models()
    return {
        "engine": "piper",
        "provider": "piper",
        "available": available(),
        "piper_importable": piper_importable(),
        "models_dir": os.path.abspath(models_dir()),
        "models": [m["id"] for m in models],
        "voices": len(models),
        "sample_rate": PIPELINE_RATE,
        "loaded": sorted(_VOICES),
    }
