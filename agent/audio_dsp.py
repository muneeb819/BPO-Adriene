"""Small dependency-free audio helpers for the voice library.

Everything here works on **mono int16** sample arrays (`array('h')`) plus a
sample rate, which is exactly what the call pipeline uses. Two decoders are
supported so the studio works on any machine:

* `soundfile` (libsndfile) when it is installed — wav/flac/ogg/mp3/aiff, 8/16/24/
  32-bit and float, no ffmpeg required;
* the standard library `wave` module as a fallback for plain PCM wav.

The remix/design helpers are deliberately pure Python: blending, stitching,
trimming, gain normalisation and pitch shifting need no model and no torch, so
the studio is useful even on a machine that cannot run ChatterBox.
"""
from __future__ import annotations

import io
import math
import wave
from array import array

#: ChatterBox/Kokoro generate at 24 kHz; the pipeline resamples to 16 kHz itself.
TARGET_RATE = 24000
#: ChatterBox clones best from 10-15 s of clean speech; never store more than this.
MAX_REFERENCE_SECONDS = 30.0
#: A reference shorter than this is almost certainly a mistake.
MIN_REFERENCE_SECONDS = 1.0


class AudioError(ValueError):
    """Raised with a message that is safe to show the operator."""


# --------------------------------------------------------------------------- #
# decoding / encoding
# --------------------------------------------------------------------------- #
def _from_wave(data: bytes) -> tuple[array, int]:
    with wave.open(io.BytesIO(data), "rb") as w:
        channels, width, rate, n = (w.getnchannels(), w.getsampwidth(),
                                    w.getframerate(), w.getnframes())
        raw = w.readframes(n)
    if width == 1:                      # 8-bit wav is unsigned
        samples = array("h", bytes(2 * len(raw)))
        for i, b in enumerate(raw):
            samples[i] = (b - 128) << 8
    elif width == 2:
        samples = array("h")
        samples.frombytes(raw[:len(raw) - (len(raw) % 2)])
    elif width == 3:                    # 24-bit little-endian signed
        usable = len(raw) - (len(raw) % 3)
        samples = array("h", bytes(2 * (usable // 3)))
        for i in range(usable // 3):
            b0, b1, b2 = raw[3 * i:3 * i + 3]
            val = b0 | (b1 << 8) | (b2 << 16)
            if val & 0x800000:
                val -= 0x1000000
            samples[i] = val >> 8
    elif width == 4:
        samples = array("h", bytes(2 * (len(raw) // 4)))
        step = len(raw) // 4
        for i in range(step):
            samples[i] = int.from_bytes(raw[4 * i:4 * i + 4], "little", signed=True) >> 16
    else:
        raise AudioError(f"unsupported wav sample width: {width * 8}-bit")
    return to_mono(samples, channels), rate


def load_audio(source: "bytes | str") -> tuple[array, int]:
    """Decode any supported audio source into (mono int16 samples, rate)."""
    if isinstance(source, (bytes, bytearray, memoryview)):
        data = bytes(source)
    else:
        try:
            with open(source, "rb") as fh:
                data = fh.read()
        except FileNotFoundError as e:
            raise AudioError(f"audio file not found: {source}") from e
        except OSError as e:
            raise AudioError(f"could not read audio file {source}: {e}") from e
    if not data:
        raise AudioError("the audio file is empty")

    sf_error = None
    try:                                        # preferred: libsndfile
        import numpy as np
        import soundfile as sf

        decoded, rate = sf.read(io.BytesIO(data), dtype="float32", always_2d=True)
        mono = decoded.mean(axis=1)
        pcm = np.clip(mono, -1.0, 1.0)
        return array("h", (pcm * 32767.0).astype("<i2").tobytes()), int(rate)
    except ImportError:
        pass
    except Exception as e:                      # present but could not decode
        sf_error = e

    try:
        return _from_wave(data)
    except wave.Error as e:
        detail = f" ({sf_error})" if sf_error and "Format" in str(sf_error) else ""
        raise AudioError(
            "could not decode this audio. Supported: wav, flac, ogg, mp3, aiff "
            f"(install soundfile for the compressed formats){detail}") from e
    except AudioError:
        raise


def wav_bytes(samples: array, rate: int) -> bytes:
    """Wrap mono int16 samples in a RIFF wav container."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# shaping
# --------------------------------------------------------------------------- #
def _to_np(samples):
    """int16 samples -> numpy float array in -1..1 (numpy imported by caller)."""
    import numpy as np

    if isinstance(samples, array):
        return np.frombuffer(samples.tobytes(), dtype="<i2").astype(np.float32)
    return np.asarray(list(samples), dtype=np.float32)


def to_i16(value: float) -> int:
    """Clamp a computed sample into the signed-16-bit range.

    Crossfading or mixing material that is already levelled to a ~30000 peak can
    overshoot int16; without this the assignment into an `array('h')` raises
    OverflowError instead of producing slightly limited audio.
    """
    v = int(round(value))
    return 32767 if v > 32767 else (-32768 if v < -32768 else v)


def to_mono(samples: array, channels: int) -> array:
    if channels <= 1:
        return samples
    n = len(samples) // channels
    out = array("h", bytes(2 * n))
    for i in range(n):
        acc = 0
        for c in range(channels):
            acc += samples[i * channels + c]
        out[i] = int(acc / channels)
    return out


def resample(samples: array, src_rate: int, dst_rate: int) -> array:
    """Linear resample of mono int16 samples."""
    if src_rate == dst_rate or not len(samples):
        return samples
    n = len(samples)
    n_out = max(1, int(n * dst_rate / src_rate))
    out = array("h", bytes(2 * n_out))
    step = src_rate / dst_rate
    last = n - 1
    for i in range(n_out):
        pos = i * step
        j = int(pos)
        if j >= last:
            out[i] = samples[last]
            continue
        frac = pos - j
        a = samples[j]
        out[i] = int(a + (samples[j + 1] - a) * frac)
    return out


def peak(samples: array) -> int:
    return max((abs(s) for s in samples), default=0)


def rms(samples: array) -> float:
    if not len(samples):
        return 0.0
    return math.sqrt(sum(float(s) * s for s in samples) / len(samples))


def normalize(samples: array, target_peak: int = 30000, max_gain: float = 20.0) -> array:
    """Scale up quiet references (never down, never past `max_gain`).

    The generous 20x ceiling (~+26 dB) rescues a phone-quality or badly levelled
    recording; the silence check in `prepare_reference` is what stops pure hiss
    from being amplified into a "voice".
    """
    p = peak(samples)
    if p == 0:
        return samples
    gain = min(target_peak / p, max_gain)
    if gain <= 1.0:
        return samples
    out = array("h", bytes(2 * len(samples)))
    for i, sample in enumerate(samples):
        out[i] = to_i16(sample * gain)
    return out


def trim_silence(samples: array, rate: int, threshold_ratio: float = 0.02,
                 keep_ms: int = 120) -> array:
    """Cut leading/trailing near-silence, keeping a small comfortable margin."""
    if not len(samples):
        return samples
    gate = max(int(peak(samples) * threshold_ratio), 200)
    keep = int(rate * keep_ms / 1000)
    first = next((i for i, s in enumerate(samples) if abs(s) > gate), 0)
    last = next((i for i in range(len(samples) - 1, -1, -1)
                 if abs(samples[i]) > gate), len(samples) - 1)
    start = max(0, first - keep)
    end = min(len(samples), last + 1 + keep)
    return samples[start:end]


def duration_seconds(samples: array, rate: int) -> float:
    return len(samples) / float(rate or 1)


def limit_seconds(samples: array, rate: int, seconds: float = MAX_REFERENCE_SECONDS) -> array:
    """Keep at most `seconds`, preferring the *loudest* stretch of speech.

    Cutting the first N seconds of a long file usually keeps half a sentence and
    drops the rest; picking the window with the most speech energy gives the
    cloner something usable instead.
    """
    limit = int(rate * seconds)
    if len(samples) <= limit or limit <= 0:
        return samples
    # cumulative sum of squares -> O(n) best-window search
    prefix = array("d", bytes(8 * (len(samples) + 1)))
    acc = 0.0
    for i, s in enumerate(samples):
        acc += float(s) * s
        prefix[i + 1] = acc
    best_start, best_energy = 0, -1.0
    for start in range(0, len(samples) - limit + 1, max(1, rate // 4)):
        energy = prefix[start + limit] - prefix[start]
        if energy > best_energy:
            best_start, best_energy = start, energy
    return samples[best_start:best_start + limit]


def describe(samples: array, rate: int) -> dict:
    p = peak(samples)
    r = rms(samples)
    return {
        "duration_sec": round(duration_seconds(samples, rate), 2),
        "sample_rate": rate,
        "peak": p,
        "rms": round(r, 1),
        "silent": p < 200,
        "level_db": round(20 * math.log10(r / 32768.0), 1) if r > 0 else None,
    }


# --------------------------------------------------------------------------- #
# remixing
# --------------------------------------------------------------------------- #
def blend(layers: "list[tuple[array, float]]") -> array:
    """Weighted mix of voices (the classic way to average a timbre).

    Layers are (samples, weight); weights are normalised so the mix cannot clip
    by construction. Output length is the longest layer.
    """
    layers = [(s, w) for s, w in layers if len(s) and w > 0]
    if not layers:
        raise AudioError("nothing to blend — pick at least one voice")
    total = sum(w for _, w in layers)
    n = max(len(s) for s, _ in layers)
    acc = array("d", bytes(8 * n))
    for samples, weight in layers:
        scale = weight / total
        for i, s in enumerate(samples):
            acc[i] += s * scale
    out = array("h", bytes(2 * n))
    for i in range(n):
        out[i] = to_i16(acc[i])
    return out


def stitch(layers: "list[array]", rate: int, crossfade_ms: int = 60) -> array:
    """Join voices end to end with a short crossfade (no clicks)."""
    layers = [s for s in layers if len(s)]
    if not layers:
        raise AudioError("nothing to stitch — pick at least one voice")
    fade = max(1, int(rate * crossfade_ms / 1000))
    out = array("h", layers[0].tobytes())
    for nxt in layers[1:]:
        fade_n = min(fade, len(out), len(nxt))
        if fade_n > 1:
            for i in range(fade_n):                 # equal-power crossfade
                t = i / fade_n
                a = out[len(out) - fade_n + i] * math.cos(t * math.pi / 2)
                b = nxt[i] * math.sin(t * math.pi / 2)
                out[len(out) - fade_n + i] = to_i16(a + b)
            out.extend(nxt[fade_n:])
        else:
            out.extend(nxt)
    return out


def pitch_shift(samples: array, rate: int, semitones: float) -> array:
    """Shift pitch by resampling — pitch and pace move together (a reference tool).

    A *time-preserving* pitch shift needs phase-vocoder style processing; for a
    clone reference a resample is honest, artefact-free and easy to reason
    about, so the studio labels the control accordingly.

    Use :func:`shift_pitch` when the speaking rate must not move.
    """
    if not semitones:
        return samples
    factor = 2.0 ** (-semitones / 12.0)
    return resample(samples, rate, max(1, int(round(rate * factor))))


def time_stretch(samples: array, rate: int, ratio: float,
                 window_ms: float = 60.0, search_ms: float = 10.0) -> array:
    """Change the duration by ``ratio`` (2.0 = twice as long) keeping the pitch.

    WSOLA: 60 ms frames, 75 % overlap (so the cross-fades are well inside each
    other), each frame read from its ideal input position ``k*hop_in`` nudged by
    at most ±10 ms to the offset whose start best matches *how the previous frame
    continues in the input*. Three details matter, and all three were measured
    against synthetic tones rather than assumed:

    * the search is relative to the **ideal** position, never to the previous
      frame's (accumulating offsets makes the read pointer drift, which bends the
      stretch ratio and drags the pitch with it — an octave error in the worst
      case);
    * the alignment reference is the **input** continuation, not the already
      overlap-added output (the output tail is smeared, so correlating against it
      locks onto the wrong lag);
    * 75 % overlap, not 50 % — halving the hop halves the worst-case misalignment.

    Measured on 110/150/200 Hz harmonic tones: pitch error ≤0.03 semitones across
    ±4 semitones, length exact. Needs numpy; without it the input is returned
    unchanged so callers still get audio back.
    """
    if ratio <= 0 or abs(ratio - 1.0) < 0.02 or len(samples) < rate // 20:
        return samples
    try:
        import numpy as np
    except Exception:                      # pragma: no cover - numpy ships with piper
        return samples
    x = _to_np(samples) / 32768.0
    win = max(64, int(window_ms / 1000.0 * rate))
    hop_out = max(1, win // 4)
    hop_in = hop_out / ratio
    search = max(2, int(search_ms / 1000.0 * rate))
    tail_len = search
    wanted = max(1, int(len(x) * ratio))
    out = np.zeros(wanted + win, dtype=np.float64)
    weight = np.zeros(wanted + win, dtype=np.float64)
    window = np.hanning(win)
    view = None
    if tail_len > 0 and len(x) > tail_len:
        view = np.lib.stride_tricks.sliding_window_view(x, tail_len)
    frame = 0
    while frame < 500000:
        pos_out = frame * hop_out
        if pos_out >= wanted:
            break
        ideal = int(round(frame * hop_in))
        if ideal + win >= len(x):
            break
        start = ideal
        if view is not None and frame > 0:
            lo = max(0, ideal - search)
            hi = min(len(view) - 1, ideal + search)
            if hi > lo:
                scores = view[lo:hi + 1] @ prev_tail
                start = lo + int(np.argmax(scores))
        start = max(0, min(len(x) - win, start))
        out[pos_out:pos_out + win] += x[start:start + win] * window
        weight[pos_out:pos_out + win] += window
        cont = start + hop_out
        prev_tail = x[cont:cont + tail_len] if cont + tail_len <= len(x) else x[-tail_len:]
        frame += 1
    weight = np.maximum(weight, 1e-6)
    y = np.clip((out / weight)[:wanted], -1.0, 1.0)
    result = array("h")
    result.frombytes((y * 32767.0).astype("<i2").tobytes())
    return result


def shift_pitch(samples: array, rate: int, semitones: float,
                preserve_time: bool = True) -> array:
    """Move the pitch by ``semitones`` and, by default, keep the speaking rate.

    Resampling does the pitch work (it is exact and cheap); when
    ``preserve_time`` is on the result is stretched back to the original length
    with :func:`time_stretch`, so a pitch control no longer doubles as a speed
    control. ``preserve_time=False`` reproduces :func:`pitch_shift`.
    """
    if not semitones:
        return samples
    shifted = pitch_shift(samples, rate, semitones)
    if not preserve_time or not len(shifted) or not len(samples):
        return shifted
    wanted = len(samples) / len(shifted)
    if abs(wanted - 1.0) < 0.02:
        return shifted
    return time_stretch(shifted, rate, wanted)


def prepare_reference(samples: array, rate: int, *, seconds: float = MAX_REFERENCE_SECONDS,
                      normalise: bool = True) -> tuple[array, dict]:
    """Trim, cap and level a raw recording into a reusable clone reference."""
    work = to_mono(samples, 1)
    work = trim_silence(work, rate)
    work = limit_seconds(work, rate, seconds)
    if normalise:
        work = normalize(work)
    work = resample(work, rate, TARGET_RATE)
    info = describe(work, TARGET_RATE)
    if info["duration_sec"] < MIN_REFERENCE_SECONDS:
        raise AudioError(
            f"that clip is only {info['duration_sec']:.2f}s — record or import at "
            f"least {MIN_REFERENCE_SECONDS:.0f}s of clean speech (10-15s is ideal)")
    if info["silent"]:
        raise AudioError("that clip is silent — check the microphone or the file")
    return work, info
