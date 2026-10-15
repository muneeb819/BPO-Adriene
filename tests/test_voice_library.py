"""Voice studio: library, design, remix and the HTTP contract behind them.

Everything here runs without a TTS model: the audio work (decode, trim, cap,
level, resample, blend, stitch, pitch) is pure Python, and the API tests use a
fake engine so no weights or keys are needed.
"""
from __future__ import annotations

import base64
import io
import os
import math
import wave
from array import array

import pytest
from fastapi.testclient import TestClient

from agent import audio_dsp as dsp
from agent.config import Config
from agent.voice_library import (
    AudioError,
    VoiceLibrary,
    clean_tags,
    clean_tuning,
    decode_base64,
)


# --------------------------------------------------------------------------- #
# helpers / fixtures
# --------------------------------------------------------------------------- #
def tone(seconds: float = 3.0, rate: int = 24000, freq: float = 220.0,
         amp: int = 9000) -> array:
    return array("h", (int(amp * math.sin(2 * math.pi * freq * i / rate))
                       for i in range(int(rate * seconds))))


def tone_wav(seconds: float = 3.0, rate: int = 24000, **kw) -> bytes:
    return dsp.wav_bytes(tone(seconds, rate, **kw), rate)


def with_silence(seconds: float = 3.0, pad: float = 1.0, rate: int = 24000) -> array:
    gap = array("h", bytes(2 * int(rate * pad)))
    return gap + tone(seconds, rate) + gap


@pytest.fixture()
def lib(tmp_path) -> VoiceLibrary:
    return VoiceLibrary(str(tmp_path / "voices"))


@pytest.fixture()
def ref_file(tmp_path) -> str:
    path = tmp_path / "ref.wav"
    path.write_bytes(tone_wav(3.0))
    return str(path)


# --------------------------------------------------------------------------- #
# audio_dsp
# --------------------------------------------------------------------------- #
def test_decode_wav_reports_rate_and_mono_samples():
    samples, rate = dsp.load_audio(tone_wav(0.5, 16000))
    assert rate == 16000
    assert len(samples) == 8000


def test_stereo_is_mixed_down_to_mono():
    left, right = tone(0.2, 24000, amp=8000), tone(0.2, 24000, amp=4000)
    inter = array("h", bytes(2 * len(left) * 2))
    for i in range(len(left)):
        inter[2 * i], inter[2 * i + 1] = left[i], right[i]

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(inter.tobytes())
    samples, rate = dsp.load_audio(buf.getvalue())

    assert rate == 24000
    assert len(samples) == len(left)
    for i in (10, 1000, 4000):                   # each mono sample is the channel mean
        assert samples[i] == pytest.approx((left[i] + right[i]) / 2, abs=1)


def test_resample_halves_and_doubles_length():
    samples = tone(1.0, 24000)
    assert len(dsp.resample(samples, 24000, 16000)) == pytest.approx(16000, abs=2)
    assert len(dsp.resample(samples, 24000, 48000)) == pytest.approx(48000, abs=2)


def test_trim_silence_keeps_a_margin_and_the_speech():
    trimmed = dsp.trim_silence(with_silence(2.0, 1.0), 24000)
    assert dsp.duration_seconds(trimmed, 24000) == pytest.approx(2.24, abs=0.15)


def test_limit_seconds_keeps_the_loudest_window_not_the_first():
    quiet = array("h", (200 for _ in range(24000)))
    loud = tone(1.0, 24000, amp=12000)
    long_clip = quiet * 3 + loud + quiet * 3
    capped = dsp.limit_seconds(long_clip, 24000, 1.0)
    assert len(capped) == 24000
    assert dsp.peak(capped) > 10000, "the loud stretch must survive the cap"


def test_normalize_lifts_a_quiet_reference_and_never_clips():
    quiet = tone(0.5, 24000, amp=300)
    boosted = dsp.normalize(quiet)
    assert dsp.peak(boosted) >= 6000
    assert dsp.peak(boosted) <= 32767


def test_silence_and_garbage_are_rejected_with_readable_errors():
    with pytest.raises(AudioError, match="empty"):
        dsp.load_audio(b"")
    with pytest.raises(AudioError, match="could not decode"):
        dsp.load_audio(b"definitely not audio")


def test_prepare_reference_requires_more_than_a_blip():
    with pytest.raises(AudioError, match="at least 1s"):
        dsp.prepare_reference(array("h", bytes(400)), 24000)
    with pytest.raises(AudioError, match="silent"):
        dsp.prepare_reference(array("h", bytes(24000 * 2)), 24000)


def test_prepare_reference_outputs_mono_24k_at_a_useful_level():
    samples, info = dsp.prepare_reference(with_silence(2.0, 0.5), 24000)
    assert info["sample_rate"] == dsp.TARGET_RATE
    assert info["duration_sec"] == pytest.approx(2.24, abs=0.15)
    assert info["peak"] >= 20000


def test_blend_is_a_weighted_average_and_cannot_clip():
    a = array("h", (4000 for _ in range(1000)))
    b = array("h", (8000 for _ in range(1000)))
    assert dsp.blend([(a, 1.0), (b, 1.0)])[0] == 6000
    assert dsp.blend([(a, 3.0), (b, 1.0)])[0] == 5000
    loud = dsp.blend([(array("h", (32767 for _ in range(50))), 1.0),
                      (array("h", (32767 for _ in range(50))), 1.0)])
    assert dsp.peak(loud) <= 32767


def test_blend_of_nothing_is_an_error():
    with pytest.raises(AudioError, match="nothing to blend"):
        dsp.blend([])


def test_stitch_joins_with_a_crossfade():
    a, b = tone(0.5, 24000), tone(0.5, 24000, freq=440)
    joined = dsp.stitch([a, b], 24000, crossfade_ms=50)
    assert len(joined) == pytest.approx(len(a) + len(b) - 1200, abs=50)


def test_pitch_shift_moves_the_octave_and_is_labeled_as_changing_pace():
    up = dsp.pitch_shift(tone(1.0, 24000), 24000, 12.0)
    assert len(up) == pytest.approx(12000, abs=5)
    assert len(dsp.pitch_shift(tone(1.0, 24000), 24000, 0.0)) == 24000


def test_compressed_formats_are_supported_when_soundfile_is_installed():
    sf = pytest.importorskip("soundfile")
    import numpy as np

    buf = io.BytesIO()
    sf.write(buf, np.zeros(24000, dtype="float32"), 24000, format="FLAC")
    samples, rate = dsp.load_audio(buf.getvalue())
    assert rate == 24000 and len(samples) == 24000


# --------------------------------------------------------------------------- #
# library CRUD
# --------------------------------------------------------------------------- #
def test_add_from_path_stores_a_usable_normalised_reference(lib, ref_file):
    profile = lib.create_from_path("Lina", ref_file, model="multilingual")
    assert profile.source == "imported"
    assert profile.duration_sec == pytest.approx(3.0, abs=0.2)
    stored = lib.ref_path(profile.id)
    samples, rate = dsp.load_audio(stored)
    assert rate == dsp.TARGET_RATE and dsp.peak(samples) > 20000
    assert lib.get(profile.id).ref == f"{profile.id}.wav"


def test_add_from_uploaded_bytes(lib):
    profile = lib.create_from_bytes("Uploaded", tone_wav(2.0), "voice.wav")
    assert profile.source == "uploaded"
    assert "voice.wav" in profile.notes


def test_add_from_microphone_pcm(lib):
    pcm = tone(2.0, 16000).tobytes()
    profile = lib.create_from_pcm("Mic", pcm, 16000)
    assert profile.source == "recorded"
    assert profile.duration_sec == pytest.approx(2.0, abs=0.2)


def test_upload_with_a_data_url_is_accepted(lib):
    data = "data:audio/wav;base64," + base64.b64encode(tone_wav(2.0)).decode()
    profile = lib.create_from_bytes("Data URL", decode_base64(data, what="file"), "x.wav")
    assert profile.duration_sec > 1


def test_oversized_upload_is_rejected(lib, monkeypatch):
    monkeypatch.setattr("agent.voice_library.MAX_UPLOAD_BYTES", 100)
    with pytest.raises(AudioError, match="MB — the limit is"):
        lib.create_from_bytes("big", tone_wav(3.0), "big.wav")


def test_design_creates_a_voice_without_reference(lib):
    profile = lib.design("Warm", engine="kokoro", voice="af_heart",
                         tuning={"speed": 0.9, "pitch": 46}, tags=["laugh"])
    assert profile.source == "designed" and not profile.ref
    assert profile.tuning == {"speed": 0.9, "pitch": 46}
    assert profile.tags == ["[laugh]"]


def test_design_defaults_the_local_voice(lib):
    assert lib.design("Plain", engine="kokoro").voice == "af_bella"


def test_design_rejects_english_only_models_with_another_language(lib):
    with pytest.raises(AudioError, match="English-only"):
        lib.design("Spanish", engine="chatterbox", model="turbo", language="es")


def test_design_rejects_unknown_languages_and_engines(lib):
    with pytest.raises(AudioError, match="unknown language"):
        lib.design("Nope", engine="chatterbox", model="multilingual", language="klingon")
    with pytest.raises(AudioError, match="unknown engine"):
        lib.design("Nope", engine="klingon")


def test_design_requires_a_name(lib):
    with pytest.raises(AudioError, match="name"):
        lib.design("   ")


def test_every_voice_gets_its_own_id_even_with_the_same_name(lib):
    ids = {lib.design("Same name").id for _ in range(5)}
    assert len(ids) == 5


def test_list_reports_the_active_voice(lib, ref_file):
    a = lib.create_from_path("A", ref_file)
    b = lib.design("B", engine="kokoro")
    lib.apply(a.id, Config.load())
    listed = {v["id"]: v["active"] for v in lib.list()}
    assert listed[a.id] is True and listed[b.id] is False
    assert lib.active_id() == a.id


def test_update_renames_and_merges_tuning(lib):
    profile = lib.design("Before", engine="kokoro", tuning={"speed": 0.9})
    lib.update(profile.id, name="After", notes="hi", tuning={"pitch": 40})
    stored = lib.get(profile.id)
    assert stored.name == "After" and stored.notes == "hi"
    assert stored.tuning == {"speed": 0.9, "pitch": 40}


def test_update_rejects_an_empty_name_and_unknown_update_field(lib):
    profile = lib.design("Keep", engine="kokoro")
    with pytest.raises(AudioError, match="cannot be empty"):
        lib.update(profile.id, name="  ")
    lib.update(profile.id, nonsense="ignored")          # unknown keys are ignored
    assert lib.get(profile.id).name == "Keep"


def test_delete_removes_the_entry_and_the_audio_file(lib, ref_file):
    profile = lib.create_from_path("Gone", ref_file)
    stored = lib.ref_path(profile.id)
    assert lib.delete(profile.id) is True
    assert lib.find(profile.id) is None
    assert not (lib.root and __import__("os").path.exists(stored))
    assert lib.delete(profile.id) is False               # second delete is a no-op


def test_deleting_the_active_voice_clears_the_active_id(lib, ref_file):
    profile = lib.create_from_path("Active", ref_file)
    lib.apply(profile.id, Config.load())
    lib.delete(profile.id)
    assert lib.active_id() == ""


def test_library_persists_across_instances(lib, tmp_path, ref_file):
    profile = lib.create_from_path("Saved", ref_file, tags=["um"])
    reopened = VoiceLibrary(lib.root)
    stored = reopened.get(profile.id)
    assert stored.name == "Saved" and stored.tags == ["[um]"]
    assert reopened.active_id() == "" and len(reopened.list()) == 1


def test_a_corrupt_index_does_not_break_the_library(lib, tmp_path):
    (tmp_path / "voices").mkdir(parents=True, exist_ok=True)
    (tmp_path / "voices" / "library.json").write_text("{not json")
    assert VoiceLibrary(lib.root).list() == []


# --------------------------------------------------------------------------- #
# apply / preview config
# --------------------------------------------------------------------------- #
def test_apply_chatterbox_voice_sets_model_language_reference_and_tuning(lib, ref_file):
    profile = lib.create_from_path("Clone", ref_file, model="multilingual", language="es",
                                   tuning={"exaggeration": 0.8, "temperature": 1.2})
    config = Config.load()
    applied = lib.apply(profile.id, config)
    assert config.tts_provider == "chatterbox"
    assert config.chatterbox_model == "multilingual"
    assert config.chatterbox_language == "es"
    assert config.chatterbox_voice_path == lib.ref_path(profile.id)
    assert config.chatterbox_exaggeration == 0.8
    assert applied["voice"].endswith(".wav")


def test_apply_local_voice_sets_voice_speed_and_pitch(lib):
    profile = lib.design("Local", engine="kokoro", voice="af_sarah",
                         tuning={"speed": 1.2, "pitch": 60})
    config = Config.load()
    lib.apply(profile.id, config)
    assert config.tts_provider == "kokoro"
    assert (config.kokoro_voice, config.kokoro_speed, config.kokoro_pitch) == ("af_sarah", 1.2, 60)


def test_apply_does_not_touch_the_other_engine_settings(lib, ref_file):
    config = Config.load()
    config.kokoro_voice = "af_heart"
    config.chatterbox_model = "turbo"
    profile = lib.create_from_path("Clone", ref_file, model="original")
    lib.apply(profile.id, config)
    assert config.kokoro_voice == "af_heart"          # untouched
    assert config.chatterbox_model == "original"


def test_apply_rejects_a_voice_whose_model_became_invalid(lib, ref_file):
    profile = lib.create_from_path("Clone", ref_file, model="original")
    lib.update(profile.id, model="klingon")
    with pytest.raises(AudioError, match="unknown ChatterBox model"):
        lib.apply(profile.id, Config.load())


def test_apply_rejects_a_voice_whose_reference_vanished(lib, ref_file):
    import os

    profile = lib.create_from_path("Clone", ref_file)
    os.remove(lib.ref_path(profile.id))
    with pytest.raises(AudioError, match="missing"):
        lib.apply(profile.id, Config.load())


def test_preview_config_uses_the_profile_settings_without_mutating_live_config(lib):
    profile = lib.design("Local", engine="kokoro", voice="af_sky", tuning={"speed": 0.8, "pitch": 30})
    config = Config.load()
    before = (config.tts_provider, config.kokoro_voice)
    preview = lib.preview_config(profile.id, config)
    assert (preview.tts_provider, preview.kokoro_voice, preview.kokoro_speed) == ("kokoro", "af_sky", 0.8)
    assert (config.tts_provider, config.kokoro_voice) == before


def test_save_from_current_captures_the_live_voice(lib, ref_file):
    config = Config.load()
    config.tts_provider = "chatterbox"
    config.chatterbox_voice_path = ref_file
    config.chatterbox_model = "multilingual"
    profile = lib.save_from_current("Snapshot", config)
    assert profile.source == "captured"
    assert profile.ref, "the live reference file must be copied into the library"
    assert lib.get(profile.id).model == "multilingual"
    assert lib.get(profile.id).to_dict()["has_reference"] is True


def test_save_from_current_without_reference_still_works(lib):
    config = Config.load()
    config.tts_provider = "chatterbox"
    config.chatterbox_voice_path = ""
    profile = lib.save_from_current("Built-in", config)
    assert profile.ref == "" and "built-in" in profile.notes


def test_save_from_current_rejects_an_unsupported_engine(lib):
    config = Config.load()
    config.tts_provider = "deepgram"
    with pytest.raises(AudioError, match="deepgram"):
        lib.save_from_current("Cloud", config)


def test_save_from_current_keeps_the_engine_it_came_from(lib):
    """A kokoro/piper snapshot must not silently become a chatterbox profile."""
    for engine, attr, value in (("kokoro", "kokoro_voice", "af_heart"),
                                ("piper", "piper_voice", "en_US-lessac-medium")):
        config = Config.load()
        config.tts_provider = engine
        setattr(config, attr, value)
        profile = lib.save_from_current(f"Snapshot {engine}", config)
        assert profile.engine == engine, profile.engine
        assert profile.voice == value


def test_save_from_current_captures_a_designed_elevenlabs_voice(lib):
    config = Config.load()
    config.tts_provider = "elevenlabs"
    config.elevenlabs_voice = "N2lVS1w4EtoT3dr4eOWO"
    profile = lib.save_from_current("Designed in the cloud", config)
    assert profile.engine == "elevenlabs" and profile.voice == "N2lVS1w4EtoT3dr4eOWO"


def test_save_from_current_needs_a_voice_id_for_elevenlabs(lib):
    config = Config.load()
    config.tts_provider = "elevenlabs"
    config.elevenlabs_voice = ""
    with pytest.raises(AudioError, match="ElevenLabs voice id"):
        lib.save_from_current("Cloud", config)


# --------------------------------------------------------------------------- #
# remix
# --------------------------------------------------------------------------- #
def test_remix_blends_two_references(lib, ref_file, tmp_path):
    other = tmp_path / "other.wav"
    other.write_bytes(tone_wav(3.0, freq=440))
    a = lib.create_from_path("A", ref_file)
    b = lib.create_from_path("B", str(other))
    mixed = lib.remix("Blend", [{"id": a.id, "weight": 2}, {"id": b.id, "weight": 1}])
    assert mixed.source == "remixed" and mixed.ref
    assert "blend of" in mixed.notes and "pitch" not in mixed.notes
    samples, rate = dsp.load_audio(lib.ref_path(mixed.id))
    assert rate == dsp.TARGET_RATE and dsp.peak(samples) > 10000


def test_remix_stitches_end_to_end_and_records_the_pitch_shift(lib, ref_file, tmp_path):
    other = tmp_path / "other.wav"
    other.write_bytes(tone_wav(2.0, freq=440))
    a = lib.create_from_path("A", ref_file)
    b = lib.create_from_path("B", str(other))
    joined = lib.remix("Stitch", [{"id": a.id}, {"id": b.id}], mode="stitch", pitch_semitones=-2.0)
    assert joined.duration_sec > a.duration_sec
    assert "pitch -2.0" in joined.notes


def test_remix_inherits_the_engine_of_the_first_source(lib, ref_file):
    a = lib.create_from_path("A", ref_file, model="original")
    mixed = lib.remix("Blend", [{"id": a.id}])
    assert mixed.engine == "chatterbox" and mixed.model == "original"


def test_remix_requires_a_name_and_known_sources(lib, ref_file):
    a = lib.create_from_path("A", ref_file)
    with pytest.raises(AudioError, match="name"):
        lib.remix("", [{"id": a.id}])
    with pytest.raises(AudioError, match="unknown voice id"):
        lib.remix("X", [{"id": "ghost"}])
    with pytest.raises(AudioError, match="unknown remix mode"):
        lib.remix("X", [{"id": a.id}], mode="mash")


def test_remix_cannot_use_a_designed_voice(lib):
    designed = lib.design("No audio", engine="kokoro")
    with pytest.raises(AudioError, match="at least one voice that has a reference"):
        lib.remix("X", [{"id": designed.id}])


# --------------------------------------------------------------------------- #
# validation helpers
# --------------------------------------------------------------------------- #
def test_clean_tuning_clamps_and_ignores_unknown_keys():
    assert clean_tuning({"exaggeration": 9, "pitch": 150, "speed": 1.3, "nope": 1}) == {
        "exaggeration": 1.0, "pitch": 99, "speed": 1.3}
    assert clean_tuning(None) == {}
    with pytest.raises(AudioError, match="must be a number"):
        clean_tuning({"speed": "fast"})


def test_clean_tags_normalises_brackets_and_deduplicates():
    assert clean_tags("laugh, [sigh] cough cough") == ["[laugh]", "[sigh]", "[cough]"]
    assert clean_tags(None) == []
    assert clean_tags("[um] [um]") == ["[um]"]


def test_decode_base64_handles_data_urls_and_garbage():
    assert decode_base64("data:audio/wav;base64," + base64.b64encode(b"abc").decode()) == b"abc"
    with pytest.raises(AudioError, match="no audio data"):
        decode_base64("")


# --------------------------------------------------------------------------- #
# HTTP API
# --------------------------------------------------------------------------- #
@pytest.fixture()
def studio(monkeypatch, tmp_path):
    """TestClient whose voice library + data live in tmp_path."""
    import server.app as appmod

    monkeypatch.setattr(appmod.CONFIG, "voice_library_dir", str(tmp_path / "voices"))
    monkeypatch.setattr(appmod.CONFIG, "tts_provider", "chatterbox")
    monkeypatch.setattr(appmod.CONFIG, "tts_standby_provider", "")
    import agent.voice_library as vl

    vl._LIBRARY = None
    with TestClient(appmod.app) as client:
        yield client
    vl._LIBRARY = None


def test_api_lists_the_library_and_designer_options(studio):
    body = studio.get("/api/voices").json()
    assert body["ok"] is True and body["voices"] == []
    designer = body["designer"]
    assert designer["chatterbox_models"] == ["turbo", "nano", "multilingual", "original"]
    assert len(designer["chatterbox_languages"]) == 23
    assert "[laugh]" in designer["chatterbox_tags"]
    assert {p["id"] for p in designer["presets"]} >= {"warm", "bright"}


def test_api_adds_a_voice_from_a_server_path(studio, ref_file):
    r = studio.post("/api/voices/upload", json={"name": "Lina", "path": ref_file,
                                                "model": "multilingual"})
    assert r.status_code == 200, r.text
    voice = r.json()["voice"]
    assert voice["has_reference"] is True and voice["sample_rate"] == 24000
    assert voice["model"] == "multilingual"


def test_api_adds_a_voice_from_uploaded_bytes(studio):
    payload = base64.b64encode(tone_wav(2.0)).decode()
    r = studio.post("/api/voices/upload", json={"name": "Upload", "filename": "a.wav",
                                                "data_base64": payload})
    assert r.status_code == 200 and r.json()["voice"]["source"] == "uploaded"


def test_api_adds_a_voice_from_microphone_pcm(studio):
    r = studio.post("/api/voices/upload", json={
        "name": "Mic", "pcm_base64": base64.b64encode(tone(2.0, 16000).tobytes()).decode(),
        "sample_rate": 16000})
    assert r.status_code == 200 and r.json()["voice"]["source"] == "recorded"


def test_api_upload_without_audio_is_a_friendly_400(studio):
    r = studio.post("/api/voices/upload", json={"name": "Nothing"})
    assert r.status_code == 400
    assert "data_base64" in r.json()["error"]


def test_api_designs_a_voice(studio):
    r = studio.post("/api/voices/design", json={"name": "Warm", "engine": "kokoro",
                                                "voice": "af_heart", "tuning": {"speed": 0.9}})
    assert r.status_code == 200
    assert r.json()["voice"]["tuning"] == {"speed": 0.9}


def test_api_design_validation_errors_are_400(studio):
    assert studio.post("/api/voices/design", json={"name": ""}).status_code == 400
    assert studio.post("/api/voices/design", json={"name": "x", "engine": "turbo"}).status_code == 400
    r = studio.post("/api/voices/design", json={"name": "x", "engine": "chatterbox",
                                                "model": "turbo", "language": "es"})
    assert r.status_code == 400 and "English-only" in r.json()["error"]


def test_api_remixes_and_reports_it(studio, ref_file):
    a = studio.post("/api/voices/upload", json={"name": "A", "path": ref_file}).json()["voice"]
    b = studio.post("/api/voices/upload", json={"name": "B", "path": ref_file}).json()["voice"]
    r = studio.post("/api/voices/remix", json={"name": "Mix", "mode": "blend",
                                               "sources": [{"id": a["id"], "weight": 1},
                                                           {"id": b["id"], "weight": 3}]})
    assert r.status_code == 200
    assert r.json()["voice"]["source"] == "remixed"


def test_api_apply_switches_the_live_voice(studio, ref_file, monkeypatch):
    import server.app as appmod

    voice = studio.post("/api/voices/upload", json={"name": "Clone", "path": ref_file,
                                                    "model": "multilingual"}).json()["voice"]
    r = studio.post(f"/api/voices/{voice['id']}/apply")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["applied"]["engine"] == "chatterbox"
    assert appmod.CONFIG.tts_provider == "chatterbox"
    assert appmod.CONFIG.chatterbox_voice_path.endswith(".wav")


def test_api_apply_unknown_voice_is_404(studio):
    assert studio.post("/api/voices/ghost/apply").status_code == 404


def test_api_rename_and_delete_round_trip(studio):
    voice = studio.post("/api/voices/design", json={"name": "Old"}).json()["voice"]
    r = studio.put(f"/api/voices/{voice['id']}", json={"name": "New"})
    assert r.status_code == 200 and r.json()["voice"]["name"] == "New"
    assert studio.delete(f"/api/voices/{voice['id']}").status_code == 200
    assert studio.get(f"/api/voices/{voice['id']}").status_code == 404


def test_api_detail_includes_active_flag_and_path(studio, ref_file):
    voice = studio.post("/api/voices/upload", json={"name": "A", "path": ref_file}).json()["voice"]
    studio.post(f"/api/voices/{voice['id']}/apply")
    body = studio.get(f"/api/voices/{voice['id']}").json()
    assert body["voice"]["active"] is True and body["voice"]["ref_path"].endswith(".wav")


def test_api_saves_the_current_voice(studio, monkeypatch):
    import server.app as appmod

    appmod.CONFIG.tts_provider = "chatterbox"
    appmod.CONFIG.chatterbox_voice_path = ""
    r = studio.post("/api/voices/current", json={"name": "Now"})
    assert r.status_code == 200 and r.json()["voice"]["name"] == "Now"
    assert studio.post("/api/voices/current", json={}).status_code == 400


def test_api_preview_reports_the_engine_that_actually_spoke(studio, monkeypatch, ref_file):
    """With only a broken chatterbox configured, the preview must say so."""
    voice = studio.post("/api/voices/upload", json={"name": "Clone", "path": ref_file}).json()["voice"]

    class FakeTTS:
        def __init__(self, config):
            self.config = config
            self.active = config.tts_provider
            self.primary = config.tts_provider
            self.standby = ""

        async def synthesize_stream_pcm(self, text):
            yield b"\x00\x01" * 800

        def status(self):
            return {"failover_reason": ""}

    monkeypatch.setattr("agent.tts.TTS", FakeTTS)
    r = studio.post(f"/api/voices/{voice['id']}/preview", json={"text": "hi"})
    assert r.status_code == 200, r.text
    assert r.headers["X-Voice-Engine"] == "chatterbox"
    assert r.headers["X-Voice-Mode"] == "profile"
    assert r.headers["content-type"] == "audio/wav"
    samples, rate = dsp.load_audio(r.content)
    assert rate == 16000 and len(samples) == 800


def test_api_preview_labels_standby_audio_as_a_fallback(studio, monkeypatch, ref_file, tmp_path):
    """A silent engine must not look like the profile's own voice."""
    import server.app as appmod

    voice = studio.post("/api/voices/upload", json={"name": "Clone", "path": ref_file}).json()["voice"]
    monkeypatch.setattr(appmod.CONFIG, "tts_standby_provider", "kokoro")

    class FailingChatterbox:
        def __init__(self, config):
            self.config = config
            self.active = config.tts_provider
            self.primary = config.tts_provider
            self.standby = ""

        async def synthesize_stream_pcm(self, text):
            if self.config.tts_provider == "chatterbox":
                raise RuntimeError("ChatterBox is not installed")
            if False:
                yield b""                        # pragma: no cover
            yield b"\x00\x01" * 800

        def status(self):
            return {"failover_reason": "chatterbox unavailable"}

    monkeypatch.setattr("agent.tts.TTS", FailingChatterbox)
    r = studio.post(f"/api/voices/{voice['id']}/preview", json={"text": "hi"})
    assert r.status_code == 200, r.text
    assert r.headers["X-Voice-Mode"] == "standby"
    assert r.headers["X-Voice-Engine"] == "kokoro"
    assert r.headers["X-Voice-Fallback"] == "1"
    assert "chatterbox" in r.headers["X-Voice-Note"]


def test_api_preview_of_every_engine_down_is_a_502(studio, monkeypatch, ref_file):
    voice = studio.post("/api/voices/upload", json={"name": "Clone", "path": ref_file}).json()["voice"]

    class DeadTTS:
        def __init__(self, config):
            self.config = config
            self.active = config.tts_provider
            self.primary = config.tts_provider
            self.standby = ""

        async def synthesize_stream_pcm(self, text):
            raise RuntimeError("no engine here")
            yield b""                            # pragma: no cover

        def status(self):
            return {"failover_reason": ""}

    monkeypatch.setattr("agent.tts.TTS", DeadTTS)
    r = studio.post(f"/api/voices/{voice['id']}/preview", json={"text": "hi"})
    assert r.status_code == 502
    assert "could not render this voice" in r.json()["error"]


def test_api_draft_preview_validates_the_language(studio):
    r = studio.post("/api/voices/preview-draft", json={"engine": "chatterbox",
                                                       "model": "turbo", "language": "es"})
    assert r.status_code == 400 and "English-only" in r.json()["error"]


def test_api_invalid_json_is_rejected(studio):
    for path in ("/api/voices/design", "/api/voices/remix", "/api/voices/upload",
                 "/api/voices/current"):
        r = studio.post(path, content=b"not json",
                        headers={"Content-Type": "application/json"})
        assert r.status_code == 400, path


def test_apply_swaps_the_engines_instead_of_dropping_the_standby(lib):
    """Picking the standby engine must not leave the bot without a fallback."""
    config = Config.load()
    config.tts_provider = "chatterbox"
    config.tts_standby_provider = "kokoro"
    profile = lib.design("Local", engine="kokoro", voice="af_heart")
    applied = lib.apply(profile.id, config)
    assert config.tts_provider == "kokoro"
    assert config.tts_standby_provider == "chatterbox"
    assert applied["standby"] == "chatterbox"


def test_apply_leaves_an_unrelated_standby_alone(lib, ref_file):
    config = Config.load()
    config.tts_provider = "elevenlabs"
    config.tts_standby_provider = "kokoro"
    profile = lib.create_from_path("Clone", ref_file)
    lib.apply(profile.id, config)
    assert config.tts_provider == "chatterbox"
    assert config.tts_standby_provider == "kokoro"


# --------------------------------------------------------------------------- #
# local neural voices: describe (prompt to voice), match, remix-through-neural
# --------------------------------------------------------------------------- #
from agent import neural_tts as nt  # noqa: E402

needs_voices = pytest.mark.skipif(
    not nt.available(),
    reason="no local neural voices installed — run scripts/fetch_neural_voices.py")


@needs_voices
def test_describe_turns_words_into_a_local_neural_voice(lib):
    profile, plan = lib.describe("Warm British narrator",
                                 "warm older British man, speaks slowly")
    assert profile.engine == "piper"
    assert profile.voice == plan["model"]
    assert profile.tuning["pitch"] == plan["pitch_level"]
    assert profile.tuning["speed"] < 1.0, "slowly must reach the pace control"
    assert plan["matched_words"], "the words that steered the design are reported"
    assert "register" in plan["why"]
    assert profile.notes.startswith("designed from:")


@needs_voices
def test_describe_needs_words(lib):
    with pytest.raises(AudioError, match="describe the voice"):
        lib.describe("Nameless", "   ")


@needs_voices
def test_describe_lets_explicit_tuning_win(lib):
    profile, plan = lib.describe("Loud", "bright female voice",
                                 tuning={"speed": 1.4, "volume": 1.3})
    assert profile.tuning["speed"] == pytest.approx(1.4)
    assert profile.tuning["volume"] == pytest.approx(1.3)
    assert profile.tuning["pitch"] == plan["pitch_level"]


@needs_voices
def test_create_matched_keeps_the_recording_and_picks_a_neural_voice(lib):
    profile, match = lib.create_matched("Clone", data=tone_wav(3.0, freq=210.0))
    assert profile.ref and profile.duration_sec > 0, "the reference is still stored"
    assert profile.engine == "piper" and profile.voice == match["model"]
    assert profile.tuning["pitch"] == match["pitch_level"]
    assert match["model"] in profile.notes
    assert os.path.isfile(lib.ref_path(profile.id))


@needs_voices
def test_create_matched_needs_exactly_one_source(lib):
    with pytest.raises(AudioError, match="exactly one"):
        lib.create_matched("Nothing")
    with pytest.raises(AudioError, match="exactly one"):
        lib.create_matched("Both", data=tone_wav(3.0), pcm=b"\x00\x00")


@needs_voices
def test_create_matched_works_from_a_server_path(lib, ref_file):
    profile, match = lib.create_matched("From path", path=ref_file)
    assert profile.source == "imported" and profile.engine == "piper"
    assert match["model"]


@needs_voices
def test_remix_can_render_the_blend_on_a_neural_voice(lib, ref_file, tmp_path):
    other = tmp_path / "other.wav"
    other.write_bytes(tone_wav(3.0, freq=430.0))
    a = lib.create_from_path("A", ref_file)
    b = lib.create_from_path("B", str(other))
    blended = lib.remix("Blend", [{"id": a.id}, {"id": b.id}], match=True)
    assert blended.engine == "piper" and blended.voice
    assert "matched to" in blended.notes


def test_remix_inherits_voice_and_tuning_from_its_source(lib, ref_file):
    source = lib.create_from_path("Local", ref_file, engine="piper",
                                  voice="en_US-amy-low",
                                  tuning={"pitch": 62, "speed": 0.9})
    plain = lib.remix("Copy", [{"id": source.id}], mode="blend")
    assert plain.engine == "piper" and plain.voice == "en_US-amy-low"
    assert plain.tuning["pitch"] == 62


def test_piper_apply_and_preview_write_piper_settings(lib):
    config = Config.load()
    profile = lib.design("Local", engine="piper", voice="en_GB-alan-medium",
                         tuning={"pitch": 70, "speed": 0.85, "volume": 1.2})
    applied = lib.apply(profile.id, config)
    assert config.tts_provider == "piper"
    assert config.piper_voice == "en_GB-alan-medium"
    assert config.piper_pitch == 70 and config.piper_speed == pytest.approx(0.85)
    assert applied["voice"] == "en_GB-alan-medium"
    preview = lib.preview_config(profile.id, Config.load())
    assert preview.tts_provider == "piper" and preview.piper_voice == "en_GB-alan-medium"
    assert preview.piper_pitch == 70


def test_design_rejects_an_elevenlabs_voice_without_an_id(lib):
    with pytest.raises(AudioError, match="voice id"):
        lib.design("Cloud", engine="elevenlabs")


# --------------------------------------------------------------------------- #
# HTTP: the prompt-to-voice and matching endpoints
# --------------------------------------------------------------------------- #
@needs_voices
def test_api_describe_dry_run_returns_the_plan_only(studio):
    r = studio.post("/api/voices/describe",
                    json={"description": "deep serious male narrator", "dry_run": True})
    assert r.status_code == 200
    body = r.json()
    assert body["dry_run"] is True
    assert body["plan"]["model"]
    assert studio.get("/api/voices").json()["voices"] == []


@needs_voices
def test_api_describe_creates_the_voice(studio):
    r = studio.post("/api/voices/describe",
                    json={"name": "Narrator", "description": "warm British man, slow"})
    assert r.status_code == 200
    voice = r.json()["voice"]
    assert voice["engine"] == "piper" and voice["voice"]
    assert [v["name"] for v in studio.get("/api/voices").json()["voices"]] == ["Narrator"]


def test_api_describe_without_words_explains_itself(studio):
    r = studio.post("/api/voices/describe", json={})
    assert r.status_code == 400
    assert "describe the voice" in r.json()["error"]
    assert r.json()["examples"], "the 400 shows usable examples"


@needs_voices
def test_api_match_reports_the_closest_voice(studio):
    payload = {"data_base64": base64.b64encode(tone_wav(3.0, freq=215.0)).decode()}
    r = studio.post("/api/voices/match", json=payload)
    assert r.status_code == 200
    match = r.json()["match"]
    assert match["model"] and 0 <= match["pitch_level"] <= 99
    assert match["why"] and match["alternatives"]
    assert studio.get("/api/voices").json()["voices"] == [], "matching creates nothing"


def test_api_match_asks_for_audio(studio):
    r = studio.post("/api/voices/match", json={})
    assert r.status_code == 400
    assert "data_base64" in r.json()["error"]


def test_api_match_reports_missing_voices_as_setup_not_a_crash(studio, monkeypatch):
    def boom(*a, **kw):
        raise nt.NeuralTTSError("no measured local neural voice is available")

    monkeypatch.setattr("agent.neural_tts.match_reference", boom)
    r = studio.post("/api/voices/match",
                    json={"data_base64": base64.b64encode(tone_wav(3.0)).decode()})
    assert r.status_code == 503
    assert "fetch_neural_voices" in r.json()["hint"]


@needs_voices
def test_api_upload_with_match_renders_on_a_neural_voice(studio):
    r = studio.post("/api/voices/upload", json={
        "name": "Matched", "match": True,
        "data_base64": base64.b64encode(tone_wav(3.0, freq=200.0)).decode()})
    assert r.status_code == 200
    body = r.json()
    assert body["voice"]["engine"] == "piper"
    assert body["voice"]["voice"] == body["match"]["model"]
    assert body["voice"]["has_reference"] is True


def test_api_upload_without_match_keeps_the_chosen_engine(studio):
    r = studio.post("/api/voices/upload", json={
        "name": "Plain", "engine": "kokoro",
        "data_base64": base64.b64encode(tone_wav(3.0)).decode()})
    assert r.status_code == 200
    assert r.json()["voice"]["engine"] == "kokoro"
    assert "match" not in r.json()


@needs_voices
def test_api_remix_with_match_renders_the_blend(studio):
    a = studio.post("/api/voices/upload", json={
        "name": "A", "data_base64": base64.b64encode(tone_wav(3.0, freq=200.0)).decode()}).json()["voice"]
    b = studio.post("/api/voices/upload", json={
        "name": "B", "data_base64": base64.b64encode(tone_wav(3.0, freq=250.0)).decode()}).json()["voice"]
    r = studio.post("/api/voices/remix", json={"name": "Mix", "match": True,
                                               "sources": [{"id": a["id"]}, {"id": b["id"]}]})
    assert r.status_code == 200
    voice = r.json()["voice"]
    assert voice["engine"] == "piper" and voice["voice"]
    assert "matched to" in voice["notes"]


@needs_voices
def test_api_design_providers_lists_local_and_cloud(studio):
    r = studio.get("/api/voices/design/providers")
    assert r.status_code == 200
    providers = {p["id"]: p for p in r.json()["providers"]}
    assert providers["local"]["available"] is True
    assert providers["local"]["needs_key"] is False
    assert providers["elevenlabs"]["needs_key"] is True
    assert providers["elevenlabs"]["hint"], "the key-gated provider explains how to unlock"
    assert r.json()["local_models"], "the measured local voices are listed"


@needs_voices
def test_designer_options_expose_the_neural_voices(studio):
    designer = studio.get("/api/voices").json()["designer"]
    assert "piper" in designer["engines"]
    assert designer["neural_voices"], "the designer can offer local neural voices"
    assert any(m.get("f0_hz") for m in designer["neural_voices"])
    assert {p["id"] for p in designer["design_providers"]} >= {"local", "elevenlabs"}


def test_api_preview_falls_back_to_a_local_server_when_the_standby_is_the_same_engine(
        studio, monkeypatch, ref_file):
    """The standby slot can hold the engine the profile asks for.

    apply() swaps the primary/standby pair, so a ChatterBox profile can be
    previewed while ``TTS_STANDBY_PROVIDER`` *is* chatterbox. The preview must then
    reach for a reachable local server instead of giving up with a 502 — and it
    must still label the audio as a fallback.
    """
    import server.app as appmod

    voice = studio.post("/api/voices/upload", json={"name": "Clone", "path": ref_file}).json()["voice"]
    monkeypatch.setattr(appmod.CONFIG, "tts_standby_provider", "chatterbox")
    monkeypatch.setattr(appmod.CONFIG, "tts_provider", "piper")

    class LocalOnly:
        def __init__(self, config):
            self.config = config
            self.active = config.tts_provider
            self.primary = config.tts_provider
            self.standby = ""

        def probe_local(self, provider, timeout=2.0):
            return self.config.__class__(**{**self.config.__dict__,
                                            "tts_provider": provider}).tts_provider == "piper"

        async def synthesize_stream_pcm(self, text):
            if self.config.tts_provider != "piper":
                raise RuntimeError("ChatterBox is not installed")
            if False:
                yield b""                        # pragma: no cover
            yield b"\x00\x01" * 800

        def status(self):
            return {"failover_reason": "chatterbox unavailable"}

    monkeypatch.setattr("agent.tts.TTS", LocalOnly)
    r = studio.post(f"/api/voices/{voice['id']}/preview", json={"text": "hi"})
    assert r.status_code == 200, r.text
    assert r.headers["X-Voice-Engine"] == "piper"
    assert r.headers["X-Voice-Requested"] == "chatterbox"
    assert r.headers["X-Voice-Mode"] == "standby"
    assert r.headers["X-Voice-Fallback"] == "1"
    assert dsp.load_audio(r.content)[0]


def test_api_preview_of_a_piper_voice_needs_no_fallback(studio, monkeypatch):
    """The happy path: the requested engine speaks, fallback flag stays 0."""
    import server.app as appmod

    monkeypatch.setattr(appmod.CONFIG, "tts_provider", "piper")
    monkeypatch.setattr(appmod.CONFIG, "tts_standby_provider", "chatterbox")
    voice = studio.post("/api/voices/describe",
                        json={"name": "Narrator",
                              "description": "warm British man, slow"}).json()["voice"]

    class FakeLocal:
        def __init__(self, config):
            self.config = config
            self.active = config.tts_provider
            self.primary = config.tts_provider
            self.standby = "chatterbox"

        def probe_local(self, provider, timeout=2.0):
            return True

        async def synthesize_stream_pcm(self, text):
            yield b"\x00\x01" * 800

        def status(self):
            return {"failover_reason": ""}

    monkeypatch.setattr("agent.tts.TTS", FakeLocal)
    r = studio.post(f"/api/voices/{voice['id']}/preview", json={"text": "hi"})
    assert r.status_code == 200, r.text
    assert r.headers["X-Voice-Engine"] == "piper"
    assert r.headers["X-Voice-Mode"] == "profile"
    assert r.headers["X-Voice-Fallback"] == "0"


def test_designer_options_name_the_engine_behind_the_local_url(studio, monkeypatch):
    """The label "kokoro" no longer implies kokoro is what is answering."""
    import server.app as appmod

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"engine": "piper", "voices": ["en_US-amy-low"],
                    "voice_details": [{"id": "en_US-amy-low", "name": "Amy",
                                       "kind": "feminine", "gender": "feminine"}]}

    monkeypatch.setattr("requests.get", lambda *a, **kw: FakeResponse())
    designer = studio.get("/api/voices").json()["designer"]
    assert designer["backend_engine"] == "piper"
    assert designer["backend_voices"][0]["id"] == "en_US-amy-low"


def test_designer_options_survive_a_dead_local_server(studio, monkeypatch):
    import server.app as appmod

    def boom(*a, **kw):
        raise ConnectionError("nothing listening")

    monkeypatch.setattr("requests.get", boom)
    designer = studio.get("/api/voices").json()["designer"]
    assert designer["backend_voices"] == [] and designer["backend_engine"] == ""
    assert designer["engines"], "the rest of the designer still works"
