"""Local neural voices (Piper/ONNX): synthesis, measurement, matching, design.

The heavy part — the 60 MB voice files — is not vendored, so the whole module is
skipped unless ``python scripts/fetch_neural_voices.py`` has been run. On a machine
that has them, these tests prove the claims the studio makes:

* the pitch slider moves pitch *and leaves the pace alone* (measured on real
  speech, against synthetic tones as ground truth);
* the speed slider hits the requested pace exactly;
* ``match_reference`` picks the voice whose measured register is closest;
* ``design_from_text`` is explainable — every word that changed the outcome is
  reported back.
"""
from __future__ import annotations

import math
import os

import numpy as np
import pytest

from agent import audio_dsp as dsp
from agent import neural_tts as nt

pytestmark = pytest.mark.skipif(
    not nt.available(),
    reason="no local neural voices installed — run scripts/fetch_neural_voices.py",
)

TEXT = "Thanks for calling EverTrust, this is Lina speaking on a recorded line."
#: Deterministic durations: Piper samples its noise, so pinning the scales makes
#: the pace measurements below exact rather than ±10 %.
DET = {"noise_scale": 0.0, "noise_w_scale": 0.0}


def f0_of(samples, rate: int) -> float:
    """Autocorrelation F0 with parabolic refinement (matches the module's own)."""
    a = np.asarray(samples, dtype=np.float64) / 32768.0
    a = a - a.mean()
    if not len(a):
        return 0.0
    ac = np.correlate(a, a, mode="full")[len(a) - 1:]
    ac /= ac[0]
    lo, hi = int(rate / 500), min(int(rate / 60), len(ac) - 2)
    lag = int(np.argmax(ac[lo:hi])) + lo
    y0, y1, y2 = ac[lag - 1], ac[lag], ac[lag + 1]
    den = y0 - 2 * y1 + y2
    return rate / (lag + (0.5 * (y0 - y2) / den if den else 0))


def wav_of(text: str = TEXT, model: str = "en_US-lessac-medium", **kw):
    return dsp.load_audio(nt.synthesize(text, model, **kw))


# --------------------------------------------------------------------------- #
# model discovery
# --------------------------------------------------------------------------- #
def test_models_are_listed_with_language_and_rate():
    models = nt.list_models()
    assert models, "expected at least one installed voice"
    for model in models:
        assert model["id"] and model["language"]
        assert model["bytes"] > 1_000_000
        assert model["sample_rate"] in (16000, 22050)
        assert model["kind"] in ("feminine", "masculine", "neutral")


def test_describe_models_measures_every_voice():
    models = nt.describe_models()
    measured = [m for m in models if m.get("characterised")]
    assert measured, "characterisation should succeed for the installed voices"
    assert all(m["f0_hz"] > 50 for m in measured), "F0 must be a plausible voice pitch"
    assert models == sorted(models, key=lambda m: (not m.get("characterised"),
                                                   m.get("f0_hz") or 0)), \
        "the list is ordered low pitch -> high pitch so the designer can index it"


def test_characterise_is_cached_on_disk():
    model = nt.list_models()[0]["id"]
    first = nt.characterize(model, refresh=True)
    assert first["f0_hz"] > 0
    cache = os.path.join(nt.models_dir(), f"{model}.voice.json")
    assert os.path.isfile(cache), "measurements are cached next to the model"
    nt._CHARACTERS.clear()
    assert nt.characterize(model) == pytest.approx(first, rel=1e-6)


def test_unknown_voice_names_run_the_fetch_script():
    with pytest.raises(nt.NeuralTTSError, match="fetch_neural_voices"):
        nt.load("no-such-voice")


# --------------------------------------------------------------------------- #
# synthesis
# --------------------------------------------------------------------------- #
def test_synthesize_returns_16k_audio():
    samples, rate = wav_of()
    assert rate == nt.PIPELINE_RATE
    assert dsp.duration_seconds(samples, rate) > 1.0
    assert not dsp.describe(samples, rate)["silent"]


def test_empty_text_is_rejected():
    with pytest.raises(nt.NeuralTTSError, match="empty"):
        nt.synthesize("   ", nt.list_models()[0]["id"])


def test_pcm_format_has_no_container():
    raw = nt.synthesize(TEXT, "en_US-amy-low", response_format="pcm")
    assert len(raw) % 2 == 0
    assert not raw.startswith(b"RIFF"), "raw output must not carry a wav header"


def test_pitch_slider_moves_pitch_and_not_pace():
    """The claim the studio makes: pitch and pace are independent controls."""
    neutral, rate = wav_of(pitch_level=50, **DET)
    base_f0 = f0_of(neutral, rate)
    base_secs = dsp.duration_seconds(neutral, rate)
    assert base_f0 > 50
    for level, want in ((80, nt.pitch_level_to_semitones(80)),
                        (20, nt.pitch_level_to_semitones(20))):
        shifted, rate = wav_of(pitch_level=level, **DET)
        got = 12 * math.log2(f0_of(shifted, rate) / base_f0)
        assert abs(got - want) < 0.45, f"pitch {level}: wanted {want:+.2f} st, got {got:+.2f}"
        drift = abs(dsp.duration_seconds(shifted, rate) / base_secs - 1.0)
        assert drift < 0.03, f"pitch {level} changed the pace by {drift:.1%}"


def test_speed_slider_hits_the_requested_pace_exactly():
    neutral, rate = wav_of(**DET)
    base = dsp.duration_seconds(neutral, rate)
    for speed in (0.6, 0.8, 1.25, 1.6):
        samples, rate = wav_of(speed=speed, **DET)
        got = dsp.duration_seconds(samples, rate)
        assert got == pytest.approx(base / speed, rel=0.01), f"speed {speed}"


def test_speed_does_not_move_the_pitch():
    neutral, rate = wav_of(**DET)
    base_f0 = f0_of(neutral, rate)
    slow, rate = wav_of(speed=0.7, **DET)
    assert abs(12 * math.log2(f0_of(slow, rate) / base_f0)) < 0.5


def test_volume_scales_the_signal():
    quiet, rate = dsp.load_audio(nt.synthesize(TEXT, "en_US-amy-low", volume=0.5, **DET))
    loud, _ = dsp.load_audio(nt.synthesize(TEXT, "en_US-amy-low", volume=1.0, **DET))
    assert dsp.peak(quiet) < dsp.peak(loud)


# --------------------------------------------------------------------------- #
# measurement, matching, design
# --------------------------------------------------------------------------- #
def test_analyze_voice_reads_back_a_known_pitch():
    rate = 16000
    seconds = 1.5
    t = np.arange(int(rate * seconds)) / rate
    tone = 0.55 * np.sin(2 * np.pi * 140 * t) + 0.2 * np.sin(2 * np.pi * 280 * t)
    samples = dsp.array("h", (tone * 11000).astype("<i2").tobytes())
    info = nt.analyze_voice(samples, rate)
    assert info["f0_hz"] == pytest.approx(140, rel=0.03)
    assert info["brightness_hz"] > 0
    assert info["voiced_ratio"] > 0.5


def test_analyze_voice_reports_nothing_for_silence():
    samples = dsp.array("h", bytes(16000 * 2))
    info = nt.analyze_voice(samples, 16000)
    assert info["f0_hz"] == 0 and info["voiced_ratio"] == 0


def test_matching_prefers_the_voice_with_the_closest_register():
    """Take each resident voice, move it away from the others, and check the
    matcher finds its way back — with real speech, so the spectrum is realistic."""
    models = [m for m in nt.describe_models() if m.get("characterised")]
    lowest = min(models, key=lambda m: m["f0_hz"])
    highest = max(models, key=lambda m: m["f0_hz"])
    if lowest["id"] == highest["id"]:
        pytest.skip("only one voice installed")
    rate = nt.PIPELINE_RATE
    checked = 0
    for model, semitones in ((highest, -9.0), (lowest, +9.0)):
        speech = dsp.load_audio(nt.synthesize(TEXT, model["id"], **DET))[0]
        moved = dsp.shift_pitch(speech, rate, semitones)
        measured = nt.analyze_voice(moved, rate)["f0_hz"]
        # independent expectation, from the measured pitches alone
        ranked = sorted(models, key=lambda m: abs(math.log(measured / m["f0_hz"])))
        best, runner = ranked[0], ranked[1]
        if abs(math.log(measured / runner["f0_hz"])) < 1.3 * abs(math.log(measured / best["f0_hz"])):
            continue                      # too close to call on register — timbre may tip it
        match = nt.match_reference(moved, rate)
        assert match["model"] == best["id"], (model["id"], measured, match)
        assert 0 <= match["pitch_level"] <= 99
        assert 0.5 <= match["speed"] <= 1.5
        assert match["why"] and "pitch" in match["why"]
        assert match["confidence"] > 0.5
        assert len(match["alternatives"]) >= 1
        assert match["alternatives"][0]["model"] == best["id"], "closest must rank first"
        checked += 1
    assert checked >= 1, "the installed voices were too close together to test matching"


def test_matching_language_filter_keeps_the_locale():
    rate = 16000
    t = np.arange(int(rate * 1.5)) / rate
    wave = 0.6 * np.sin(2 * np.pi * 120 * t) + 0.25 * np.sin(2 * np.pi * 240 * t)
    samples = dsp.array("h", (wave * 10000).astype("<i2").tobytes())
    match = nt.match_reference(samples, rate, language="en-GB")
    assert match["model"].startswith("en_GB")


def test_design_reads_gender_accent_and_pace():
    man = nt.design_from_text("warm older British man, speaks slowly")
    woman = nt.design_from_text("bright young American woman, fast")
    assert man["model"].split("-")[0] == "en_GB"
    assert woman["model"].split("-")[0] == "en_US"
    women = [m for m in nt.describe_models() if m.get("characterised")]
    assert man["model"] != woman["model"], "different registers must reach different voices"
    assert man["speed"] < 1.0 < woman["speed"]
    assert man["matched_words"] and woman["matched_words"]
    assert "register" in man["why"]


def test_design_without_words_falls_back_to_the_middle_voice():
    plan = nt.design_from_text("")
    assert plan["model"]
    assert plan["speed"] == pytest.approx(1.0)
    assert plan["pitch_level"] == nt.NEUTRAL_PITCH


def test_design_honours_explicit_sliders_over_the_words():
    plan = nt.design_from_text("deep male narrator", pitch_level=90, speed=1.4)
    assert plan["speed"] == pytest.approx(1.4)
    assert plan["pitch_level"] > 60


def test_pitch_level_round_trips():
    for level in (0, 25, 50, 75, 99):
        st = nt.pitch_level_to_semitones(level)
        assert nt.semitones_to_pitch_level(st) == level


def test_health_reports_the_engine_and_models():
    info = nt.health()
    assert info["engine"] == "piper" and info["available"] is True
    assert info["voices"] == len(nt.list_models())
    assert os.path.isdir(info["models_dir"])


# --------------------------------------------------------------------------- #
# the DSP the pitch control is built on
# --------------------------------------------------------------------------- #
def test_time_stretch_preserves_pitch_and_length_is_exact():
    rate = 16000
    t = np.arange(int(rate * 1.5)) / rate
    tone = dsp.array("h", ((0.6 * np.sin(2 * np.pi * 150 * t)
                            + 0.3 * np.sin(2 * np.pi * 300 * t)) * 11000).astype("<i2").tobytes())
    for ratio in (0.8, 1.25, 1.6):
        out = dsp.time_stretch(tone, rate, ratio)
        assert len(out) == pytest.approx(int(len(tone) * ratio), rel=0.01)
        assert f0_of(out, rate) == pytest.approx(150, rel=0.03)
    assert dsp.time_stretch(tone, rate, 1.0) is tone, "a no-op must not touch the audio"


def test_shift_pitch_keeps_the_duration():
    rate = 16000
    t = np.arange(int(rate * 1.5)) / rate
    tone = dsp.array("h", ((0.6 * np.sin(2 * np.pi * 180 * t)
                            + 0.3 * np.sin(2 * np.pi * 360 * t)) * 11000).astype("<i2").tobytes())
    for semitones in (-4.0, -2.0, 2.0, 4.0):
        out = dsp.shift_pitch(tone, rate, semitones)
        assert len(out) == len(tone)
        want = 180 * 2 ** (semitones / 12)
        assert f0_of(out, rate) == pytest.approx(want, rel=0.03), semitones
    coupled = dsp.shift_pitch(tone, rate, 4.0, preserve_time=False)
    assert len(coupled) < len(tone), "the resample-only mode keeps its pace coupling"
