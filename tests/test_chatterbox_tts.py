"""ChatterBox TTS provider: model loading, switching, cloning, disk guard, failover.

These tests never download model weights or need a GPU. The ChatterBox engine is
stubbed at the `chatterbox` package boundary (a fake module injected into
`sys.modules`), so the code path that *would* load a model is exercised exactly
as written — including the ImportError path used when the package is absent.
"""
from __future__ import annotations

import asyncio
import io
import sys
import types
import wave

import pytest

from agent.config import Config
from agent.tts import (
    CHATTERBOX_LANGUAGES,
    CHATTERBOX_MODELS,
    TTS,
    _chatterbox_unavailable,
    is_provider_error,
    wav_from_pcm16,
)


async def collect(agen) -> bytes:
    """Drain an async audio generator into one buffer."""
    return b"".join([chunk async for chunk in agen])


def cfg(**kw) -> Config:
    base = dict(
        tts_provider="chatterbox",
        tts_standby_provider="",
        chatterbox_model="turbo",
        chatterbox_device="cpu",
        chatterbox_language="en",
        chatterbox_voice_path="",
    )
    base.update(kw)
    return Config(**base)


@pytest.fixture(autouse=True)
def _clean_health():
    TTS.reset_health()
    yield
    TTS.reset_health()


@pytest.fixture()
def fake_chatterbox(monkeypatch):
    """Install fake `chatterbox.*` modules that report which engine loaded."""
    loaded: list[str] = []

    class _FakeModel:
        def __init__(self, engine: str):
            self.engine = engine
            self.calls: list[dict] = []

        def generate(self, text, **kw):
            self.calls.append({"text": text, **kw})
            import numpy as np

            # 0.25 s of 24 kHz audio, as ChatterBox emits
            t = np.linspace(0, 0.25, 6000, endpoint=False)
            return (0.5 * np.sin(2 * 3.14159 * 220 * t)).astype("float32")

    made: dict[str, _FakeModel] = {}

    def _factory(engine: str):
        def from_pretrained(device=None, **kw):
            loaded.append(engine)
            made[engine] = _FakeModel(engine)
            return made[engine]

        return staticmethod(from_pretrained).__func__

    pkg = types.ModuleType("chatterbox")
    mtl = types.ModuleType("chatterbox.mtl_tts")
    turbo = types.ModuleType("chatterbox.tts_turbo")
    orig = types.ModuleType("chatterbox.tts")

    class ChatterboxMultilingualTTS:
        from_pretrained = _factory("multilingual")

    class ChatterboxTurboTTS:
        from_pretrained = _factory("turbo")

    class ChatterboxTTS:
        from_pretrained = _factory("original")

    mtl.ChatterboxMultilingualTTS = ChatterboxMultilingualTTS
    turbo.ChatterboxTurboTTS = ChatterboxTurboTTS
    orig.ChatterboxTTS = ChatterboxTTS
    pkg.mtl_tts, pkg.tts_turbo, pkg.tts = mtl, turbo, orig

    # _chatterbox_sync imports torch to detect a tensor; a minimal stand-in is
    # enough since the fake engine returns a float32 numpy array.
    fake_torch = types.ModuleType("torch")

    class Tensor:                                     # noqa: D401 - marker type
        pass

    fake_torch.Tensor = Tensor
    fake_torch.from_numpy = lambda a: a

    for name, mod in (
        ("torch", fake_torch),
        ("chatterbox", pkg),
        ("chatterbox.mtl_tts", mtl),
        ("chatterbox.tts_turbo", turbo),
        ("chatterbox.tts", orig),
    ):
        monkeypatch.setitem(sys.modules, name, mod)

    # numpy is present in the test env; make sure the model cache starts empty
    TTS._chatterbox_model_cache.clear()
    yield types.SimpleNamespace(loaded=loaded, made=made)
    TTS._chatterbox_model_cache.clear()


class _ImportBlocker:
    """Makes the named top-level packages unimportable, as if not installed."""

    def __init__(self, names):
        self.names = set(names)

    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in self.names:
            raise ModuleNotFoundError(f"No module named '{fullname}'", name=fullname)
        return None

    # legacy protocol, harmless to keep for older importlib paths
    def find_module(self, fullname, path=None):
        self.find_spec(fullname)
        return None


@pytest.fixture()
def standby_server():
    """Minimal Kokoro-wire-format standby (24 kHz PCM) for failover tests."""
    import json as _json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = _json.dumps({"voices": ["af_bella"]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0) or 0)
            self.rfile.read(n)
            body = b"\x00\x11" * 12000            # 0.5 s of 24 kHz PCM16
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv.url = f"http://127.0.0.1:{srv.server_address[1]}"
    yield srv
    srv.shutdown()
    srv.server_close()


@pytest.fixture()
def no_chatterbox(monkeypatch):
    """Simulate the engine (and PyTorch) not being installed."""
    names = ("chatterbox", "torch", "torchaudio", "soundfile")
    for name in list(sys.modules):
        if name.split(".")[0] in names:
            monkeypatch.delitem(sys.modules, name, raising=False)
    blocker = _ImportBlocker(names)
    monkeypatch.setattr(sys, "meta_path", [blocker, *sys.meta_path])


# ------------------------------------------------------------- metadata

def test_four_models_are_selectable():
    assert CHATTERBOX_MODELS == ("turbo", "nano", "multilingual", "original")


def test_multilingual_language_table_covers_23_languages():
    assert len(CHATTERBOX_LANGUAGES) == 23
    for code in ("en", "es", "fr", "de", "zh", "ar", "hi", "sw"):
        assert code in CHATTERBOX_LANGUAGES


@pytest.mark.parametrize("model,multilingual,tags,lang_count", [
    ("turbo", False, True, 1),
    ("nano", False, True, 1),
    ("multilingual", True, False, 23),
    ("original", False, False, 1),
])
def test_model_info_describes_each_engine(model, multilingual, tags, lang_count):
    info = TTS(cfg(chatterbox_model=model)).chatterbox_model_info()
    assert info["model"] == model
    assert info["multilingual"] is multilingual
    assert info["paralinguistic_tags"] is tags
    assert len(info["languages"]) == lang_count
    assert info["sample_rate"] == 24000        # engine native
    assert info["output_rate"] == 16000        # pipeline format
    assert info["available_models"] == list(CHATTERBOX_MODELS)


def test_model_info_reports_available_languages_and_settings():
    info = TTS(cfg()).chatterbox_model_info()
    assert info["available_languages"]["es"] == "Spanish"
    assert set(info) >= {"exaggeration", "cfg_weight", "temperature"}


# ------------------------------------------------------------- settings resolver

def test_settings_default_to_multilingual_when_nothing_named():
    out = TTS.chatterbox_settings({})
    assert out["model"] == "multilingual"
    assert out["changed"] is False


def test_settings_apply_all_three_fields_in_one_request():
    """The documented curl sends model + language + voice together."""
    out = TTS.chatterbox_settings(
        {"model": "multilingual", "language": "es", "voice": "/tmp/ref.wav"})
    assert out["model"] == "multilingual"
    assert out["language"] == "es"
    assert out["voice"] == "/tmp/ref.wav"
    assert out["changed"] is True
    assert out["message"] == "Switched to Chatterbox-multilingual"


def test_settings_reject_unknown_model():
    with pytest.raises(ValueError, match="invalid model 'ghost'"):
        TTS.chatterbox_settings({"model": "ghost"})


def test_settings_reject_unknown_language():
    with pytest.raises(ValueError, match="unsupported language 'klingon'"):
        TTS.chatterbox_settings({"language": "klingon"})


def test_settings_reject_empty_language():
    with pytest.raises(ValueError, match="empty language"):
        TTS.chatterbox_settings({"language": "   "})


def test_settings_language_implies_multilingual_engine():
    out = TTS.chatterbox_settings({"language": "fr"})
    assert out["model"] == "multilingual"


def test_settings_reject_language_on_an_english_only_engine():
    """Silently ignoring it would leave the operator believing Spanish was set."""
    with pytest.raises(ValueError, match="English-only"):
        TTS.chatterbox_settings({"model": "turbo", "language": "es"})


def test_settings_reject_language_on_nano_and_original():
    for model in ("nano", "original"):
        with pytest.raises(ValueError, match="English-only"):
            TTS.chatterbox_settings({"model": model, "language": "fr"})


def test_settings_language_is_case_insensitive():
    assert TTS.chatterbox_settings({"language": "ES"})["language"] == "es"


def test_settings_empty_voice_clears_the_clone():
    out = TTS.chatterbox_settings({"voice": ""})
    assert out["voice"] == ""
    assert out["changed"] is True


# ------------------------------------------------------------- voice reference validation

def test_voice_reference_accepts_a_real_wav(tmp_path):
    ref = tmp_path / "ref.wav"
    ref.write_bytes(wav_from_pcm16(b"\x00\x01" * 1600))
    assert TTS.validate_voice_reference(str(ref)) == ""


def test_voice_reference_rejects_missing_file(tmp_path):
    problem = TTS.validate_voice_reference(str(tmp_path / "nope.wav"))
    assert "not found on the server" in problem


def test_voice_reference_rejects_wrong_extension(tmp_path):
    bad = tmp_path / "notes.txt"
    bad.write_text("not audio")
    assert "must be an audio file" in TTS.validate_voice_reference(str(bad))


def test_voice_reference_rejects_empty_file(tmp_path):
    empty = tmp_path / "empty.wav"
    empty.touch()
    assert "is empty" in TTS.validate_voice_reference(str(empty))


def test_voice_reference_empty_path_is_no_problem():
    assert TTS.validate_voice_reference("") == ""


def test_model_info_flags_an_invalid_clone_reference(tmp_path):
    info = TTS(cfg(chatterbox_voice_path=str(tmp_path / "ghost.wav"))).chatterbox_model_info()
    assert info["voice_cloned"] is False, "a missing file must not report as cloned"
    assert "not found" in info["voice_reference_problem"]


def test_model_info_confirms_a_valid_clone_reference(tmp_path):
    ref = tmp_path / "ref.wav"
    ref.write_bytes(wav_from_pcm16(b"\x00\x01" * 1600))
    info = TTS(cfg(chatterbox_voice_path=str(ref))).chatterbox_model_info()
    assert info["voice_cloned"] is True
    assert info["voice_reference_problem"] == ""
    assert info["voice_path"] == str(ref)


# ------------------------------------------------------------- model switching

def test_switch_model_updates_config_and_clears_cache(fake_chatterbox):
    tts = TTS(cfg())
    TTS._chatterbox_model_cache[("turbo", "cpu", "en")] = object()
    msg = tts.switch_chatterbox_model("multilingual")
    assert tts.config.chatterbox_model == "multilingual"
    assert msg == "Switched to Chatterbox-multilingual"
    assert TTS._chatterbox_model_cache == {}, "cached weights must be dropped"


def test_switch_model_rejects_unknown_model(fake_chatterbox):
    with pytest.raises(ValueError, match="Invalid model 'ghost'"):
        TTS(cfg()).switch_chatterbox_model("ghost")


@pytest.mark.parametrize("model,engine", [
    ("multilingual", "multilingual"),
    ("turbo", "turbo"),
    ("nano", "turbo"),        # nano maps onto the published Turbo engine
    ("original", "original"),
])
def test_each_model_loads_its_engine(fake_chatterbox, model, engine):
    """Model-loading paths: verify the right ChatterBox class is instantiated."""
    TTS(cfg(chatterbox_model=model))._get_chatterbox_model()
    assert fake_chatterbox.loaded == [engine]


def test_model_is_cached_between_calls(fake_chatterbox):
    tts = TTS(cfg())
    a = tts._get_chatterbox_model()
    b = tts._get_chatterbox_model()
    assert a is b
    assert len(fake_chatterbox.loaded) == 1, "weights must not reload per utterance"


def test_multilingual_passes_language_id(fake_chatterbox):
    tts = TTS(cfg(chatterbox_model="multilingual", chatterbox_language="es"))
    tts._chatterbox_sync("hola")
    call = fake_chatterbox.made["multilingual"].calls[0]
    assert call["language_id"] == "es"
    assert call["text"] == "hola"


def test_voice_clone_path_is_passed_to_the_engine(fake_chatterbox, tmp_path):
    ref = tmp_path / "ref.wav"
    ref.write_bytes(wav_from_pcm16(b"\x00\x01" * 1600))
    tts = TTS(cfg(chatterbox_voice_path=str(ref)))
    tts._chatterbox_sync("clone me")
    assert fake_chatterbox.made["turbo"].calls[0]["audio_prompt_path"] == str(ref)


def test_no_reference_means_no_audio_prompt(fake_chatterbox):
    TTS(cfg())._chatterbox_sync("built-in voice")
    assert fake_chatterbox.made["turbo"].calls[0]["audio_prompt_path"] is None


def test_paralinguistic_tags_are_passed_through_verbatim(fake_chatterbox):
    TTS(cfg())._chatterbox_sync("Well [laugh] that's [sigh] something.")
    text = fake_chatterbox.made["turbo"].calls[0]["text"]
    assert "[laugh]" in text and "[sigh]" in text


# ------------------------------------------------------------- audio format

def test_synthesis_is_resampled_24k_to_16k(fake_chatterbox):
    """Engine native 24 kHz must come out of the pipeline as 16 kHz PCM16."""
    pcm = TTS(cfg())._chatterbox_sync("rate check")
    # fake engine emits 6000 samples @24 kHz = 0.25 s -> 4000 samples @16 kHz
    assert len(pcm) == 4000 * 2, f"expected 4000 int16 samples, got {len(pcm)} bytes"


def test_synthesize_pcm_returns_int16(fake_chatterbox):
    pcm = TTS(cfg()).synthesize_pcm("hi")
    assert len(pcm) % 2 == 0
    assert len(pcm) > 0


def test_async_pcm_stream_yields_audio(fake_chatterbox):
    async def collect():
        return [c async for c in TTS(cfg())._pcm_chatterbox("hi")]

    chunks = asyncio.run(collect())
    assert len(chunks) == 1 and len(chunks[0]) > 0


def test_wav_from_pcm16_is_parseable():
    pcm = b"\x10\x20" * 800
    with wave.open(io.BytesIO(wav_from_pcm16(pcm))) as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getframerate() == 16000
        assert w.readframes(w.getnframes()) == pcm


def test_wav_from_pcm16_pads_a_partial_frame():
    wav = wav_from_pcm16(b"\x00\x01\x02")            # 1.5 samples
    with wave.open(io.BytesIO(wav)) as w:
        assert w.getnframes() == 2                   # padded to a whole frame


# ------------------------------------------------------------- disk guard

def test_disk_guard_passes_when_there_is_room(monkeypatch):
    import shutil

    class FakeUsage:
        free = 100 * 1024 ** 3

    monkeypatch.setattr(shutil, "disk_usage", lambda _p: FakeUsage())
    TTS(cfg())._chatterbox_disk_ok(2.5)          # must not raise


def test_disk_guard_fails_fast_with_a_friendly_message(monkeypatch):
    import shutil

    class FakeUsage:
        free = 1 * 1024 ** 3                     # only 1 GB free

    monkeypatch.setattr(shutil, "disk_usage", lambda _p: FakeUsage())
    with pytest.raises(RuntimeError) as err:
        TTS(cfg())._chatterbox_disk_ok(2.5)
    msg = str(err.value)
    assert "2.5 GB" in msg and "1.0 GB" in msg
    assert "HF_HOME" in msg, "the message should say how to fix it"


def test_disk_guard_runs_before_any_download(monkeypatch, fake_chatterbox):
    """A doomed download must be refused before the engine is touched."""
    import shutil

    class FakeUsage:
        free = 0

    monkeypatch.setattr(shutil, "disk_usage", lambda _p: FakeUsage())
    with pytest.raises(RuntimeError, match="free"):
        TTS(cfg())._get_chatterbox_model()
    assert fake_chatterbox.loaded == [], "must fail before loading weights"


# ------------------------------------------------------------- missing dependency

def test_missing_package_becomes_an_actionable_error(no_chatterbox):
    with pytest.raises(ImportError) as err:
        TTS(cfg())._get_chatterbox_model()
    msg = str(err.value)
    assert "pip install chatterbox-tts" in msg
    assert "TTS_STANDBY_PROVIDER=kokoro" in msg


def test_missing_package_is_classified_as_a_provider_error(no_chatterbox):
    """A missing engine must be failable-over, not treated as our own bug."""
    with pytest.raises(ImportError) as err:
        TTS(cfg())._get_chatterbox_model()
    assert is_provider_error(err.value) is True


def test_unrelated_import_error_is_still_our_bug(monkeypatch):
    """Only engine-owned packages count — anything else must surface loudly."""
    monkeypatch.setitem(sys.modules, "torch", None)
    exc = ModuleNotFoundError("No module named 'notes_helper'", name="notes_helper")
    assert is_provider_error(exc) is False


@pytest.mark.parametrize("name", ["torch", "torchaudio", "chatterbox", "soundfile"])
def test_engine_owned_missing_packages_are_failable(name):
    exc = ModuleNotFoundError(f"No module named '{name}'", name=name)
    assert is_provider_error(exc) is True


def test_chatterbox_unavailable_keeps_the_original_dependency_named():
    exc = _chatterbox_unavailable(
        ModuleNotFoundError("No module named 'torch'", name="torch"))
    assert "missing dependency: torch" in str(exc)
    assert exc.name == "chatterbox"


# ------------------------------------------------------------- failover integration

def test_chatterbox_primary_fails_over_to_standby(no_chatterbox, standby_server):
    """The real production path: ChatterBox down -> standby speaks the line."""
    tts = TTS(cfg(tts_standby_provider="kokoro",
                  kokoro_base_url=standby_server.url,
                  chatterbox_model="turbo"))

    chunks = asyncio.run(collect(tts.synthesize_stream_pcm("hello there")))

    assert chunks, "the standby should have produced audio"
    assert tts.active == "kokoro", "the standby is promoted after a failover"
    assert "unavailable" in tts.failover_reason
    assert TTS(cfg()).status()["failover_reason"], "reason survives a new instance"


def test_failover_reason_is_reported_by_a_fresh_instance(no_chatterbox, standby_server):
    """/health builds a throwaway TTS, so the reason must be class level."""
    asyncio.run(collect(TTS(cfg(tts_standby_provider="kokoro",
                                kokoro_base_url=standby_server.url)
                            ).synthesize_stream_pcm("x")))
    assert TTS(cfg()).status()["failover_reason"]


def test_healthy_chatterbox_retakes_primary_after_ttl(standby_server, monkeypatch):
    """Once the outage record goes stale the primary is retried, not stuck."""
    kwargs = dict(tts_standby_provider="kokoro", kokoro_base_url=standby_server.url)
    tts = TTS(cfg(**kwargs))

    # A real failing attempt: the primary is marked down and the standby promoted.
    assert asyncio.run(collect(tts.synthesize_stream_pcm("hello")))
    assert tts.active == "kokoro", "standby takes over while the primary is down"

    # Age the outage record past its TTL -> the primary is trusted again.
    ts, ok = TTS._health["chatterbox"]
    monkeypatch.setattr("agent.tts.time.time", lambda: ts + TTS._health_ttl + 1)
    assert TTS._healthy("chatterbox") is True
    assert TTS(cfg(**kwargs)).active == "chatterbox", \
        "a recovered engine must get its role back rather than flap forever"


# ------------------------------------------------------------- HTTP API contract

@pytest.fixture()
def cb_client(monkeypatch, standby_server):
    """TestClient with ChatterBox configured as the live engine."""
    from fastapi.testclient import TestClient

    import server.app as appmod

    monkeypatch.setattr(appmod.CONFIG, "tts_provider", "chatterbox")
    monkeypatch.setattr(appmod.CONFIG, "tts_standby_provider", "kokoro")
    monkeypatch.setattr(appmod.CONFIG, "chatterbox_model", "turbo")
    monkeypatch.setattr(appmod.CONFIG, "chatterbox_language", "en")
    monkeypatch.setattr(appmod.CONFIG, "chatterbox_voice_path", "")
    monkeypatch.setattr(appmod.CONFIG, "kokoro_base_url", standby_server.url)
    with TestClient(appmod.app) as c:
        yield c
    TTS.reset_health()


def test_api_switch_voice_applies_model_language_and_voice_together(cb_client, tmp_path):
    """The documented request must set all three fields, not just the first."""
    ref = tmp_path / "ref.wav"
    ref.write_bytes(wav_from_pcm16(b"\x00\x01" * 3200))
    r = cb_client.post("/api/tts/switch-voice", json={
        "model": "multilingual", "language": "es", "voice": str(ref)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["result"] == "Switched to Chatterbox-multilingual"
    assert body["applied"] == {"model": "multilingual", "language": "es",
                               "voice": str(ref)}
    assert body["model_info"]["language"] == "es"
    assert body["model_info"]["voice_cloned"] is True
    assert body["model_info"]["multilingual"] is True
    assert len(body["available_languages"]) == 23


def test_api_settings_persist_across_requests(cb_client):
    cb_client.post("/api/tts/switch-voice",
                   json={"model": "multilingual", "language": "fr"})
    info = cb_client.get("/api/tts/voices").json()["model_info"]
    assert info["model"] == "multilingual"
    assert info["language"] == "fr"


def test_api_unknown_model_is_a_friendly_400_not_a_500(cb_client):
    r = cb_client.post("/api/tts/switch-voice", json={"model": "nonexistent"})
    assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text}"
    body = r.json()
    assert "available_models" in body
    assert body["available_models"] == list(CHATTERBOX_MODELS)
    assert "nonexistent" in body["error"]


def test_api_unknown_language_is_rejected(cb_client):
    r = cb_client.post("/api/tts/switch-voice", json={"language": "klingon"})
    assert r.status_code == 400
    assert "klingon" in r.json()["error"]


def test_api_language_on_english_only_engine_is_rejected(cb_client):
    r = cb_client.post("/api/tts/switch-voice",
                       json={"model": "turbo", "language": "es"})
    assert r.status_code == 400
    assert "English-only" in r.json()["error"]


def test_api_missing_voice_file_is_rejected(cb_client, tmp_path):
    r = cb_client.post("/api/tts/switch-voice",
                       json={"voice": str(tmp_path / "ghost.wav")})
    assert r.status_code == 400
    assert "not found on the server" in r.json()["error"]


def test_api_voice_clone_can_be_cleared(cb_client, tmp_path):
    ref = tmp_path / "ref.wav"
    ref.write_bytes(wav_from_pcm16(b"\x00\x01" * 3200))
    cb_client.post("/api/tts/switch-voice", json={"voice": str(ref)})
    assert cb_client.get("/api/tts/voices").json()["model_info"]["voice_cloned"] is True
    r = cb_client.post("/api/tts/switch-voice", json={"voice": ""})
    assert r.status_code == 200
    assert r.json()["applied"]["voice"] == ""
    assert cb_client.get("/api/tts/voices").json()["model_info"]["voice_cloned"] is False


def test_api_setting_a_missing_clone_is_rejected(cb_client, tmp_path):
    r = cb_client.post("/api/tts/switch-voice",
                       json={"voice": str(tmp_path / "gone.wav")})
    assert r.status_code == 400
    assert "not found" in r.json()["error"]


def test_api_switching_model_warns_about_a_now_stale_clone(cb_client, tmp_path):
    """A reference that worked earlier can vanish; switching engine must say so."""
    ref = tmp_path / "ref.wav"
    ref.write_bytes(wav_from_pcm16(b"\x00\x01" * 3200))
    assert cb_client.post("/api/tts/switch-voice",
                          json={"voice": str(ref)}).status_code == 200
    ref.unlink()                                   # the operator's file went away

    # Switching engine must NOT be blocked by a stale reference (the operator
    # may be switching away from a broken setup) — but it must be reported.
    r = cb_client.post("/api/tts/switch-voice", json={"model": "original"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["warning"], "the stale reference must be surfaced"
    assert "not found" in body["warning"]
    assert body["model_info"]["voice_cloned"] is False
    assert "not found" in body["model_info"]["voice_reference_problem"]


def test_api_voices_lists_models_and_languages(cb_client):
    body = cb_client.get("/api/tts/voices").json()
    assert body["provider"] == "chatterbox"
    assert body["available_models"] == list(CHATTERBOX_MODELS)
    assert body["model_info"]["available_languages"]["de"] == "German"
    assert "default" == body["voices"][0]["id"]


def test_api_invalid_json_body(cb_client):
    r = cb_client.post("/api/tts/switch-voice", content=b"not json",
                       headers={"Content-Type": "application/json"})
    assert r.status_code == 400


def test_api_speak_returns_502_when_both_engines_are_down(cb_client, no_chatterbox,
                                                        monkeypatch):
    """Primary and standby both unreachable -> 502 naming both, with advice."""
    import server.app as appmod

    monkeypatch.setattr(appmod.CONFIG, "kokoro_base_url",
                        "http://127.0.0.1:9")        # closed port
    r = cb_client.post("/api/debug/speak", json={"text": "hello [laugh]"})
    assert r.status_code == 502, r.text
    err = r.json()["error"]
    assert "chatterbox-tts" in err, "must explain how to install the engine"
    assert "primary=chatterbox" in err and "standby=kokoro" in err


def test_api_speak_uses_the_failover_chain(cb_client, no_chatterbox):
    """ChatterBox down but Kokoro standby up -> playable audio, HTTP 200."""
    r = cb_client.post("/api/debug/speak", json={"text": "failover please"})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("audio/wav")
    with wave.open(io.BytesIO(r.content)) as w:
        assert w.getframerate() == 16000          # pipeline format
        assert w.getnchannels() == 1
        assert w.getnframes() > 0


def test_api_switch_voice_still_works_while_chatterbox_is_failed_over(cb_client, monkeypatch):
    import server.app as appmod

    """A failover must not turn every voice request into "empty voice".

    `active` becomes the standby engine once the primary is marked down, and
    gating the request on it made the documented model-switch curl return 400
    at precisely the moment an operator needs to re-route the engine.
    """
    from agent.tts import TTS

    # Simulate the real failover state: the primary is marked down and the
    # standby has been promoted, so `active` is now kokoro.
    TTS._mark("chatterbox", False)
    TTS._promoted = "kokoro"
    assert TTS(appmod.CONFIG).active == "kokoro", "test must run failed-over"
    assert cb_client.get("/api/tts/voices").json()["primary"] == "chatterbox"

    r = cb_client.post("/api/tts/switch-voice", json={"model": "multilingual"})
    assert r.status_code == 200, r.text
    assert r.json()["provider"] == "chatterbox"
    assert r.json()["result"] == "Switched to Chatterbox-multilingual"

    # A voice request is still a ChatterBox clone (and may be cleared), never
    # silently stored as kokoro's voice.
    assert cb_client.post("/api/tts/switch-voice",
                          json={"voice": ""}).status_code == 200
    assert appmod.CONFIG.chatterbox_voice_path == ""
    TTS.reset_health()


def test_api_kokoro_deployment_still_requires_a_voice(monkeypatch, standby_server):
    """The guard must stay in place for a kokoro-only deployment."""
    from fastapi.testclient import TestClient

    import server.app as appmod
    from agent.tts import TTS

    monkeypatch.setattr(appmod.CONFIG, "tts_provider", "kokoro")
    monkeypatch.setattr(appmod.CONFIG, "tts_standby_provider", "")
    monkeypatch.setattr(appmod.CONFIG, "kokoro_base_url", standby_server.url)
    with TestClient(appmod.app) as c:
        r = c.post("/api/tts/switch-voice", json={"model": "multilingual"})
    assert r.status_code == 400
    assert r.json()["error"] == "empty voice"


def test_api_voices_keeps_chatterbox_metadata_after_failover(cb_client, no_chatterbox):
    """The dashboard panel must stay usable while the engine is failed over.

    Its model / language / tag controls are rendered from `model_info` and
    `available_models`; returning only the kokoro payload left every dropdown
    empty and the status line reading "out NaNkHz".
    """
    from agent.tts import CHATTERBOX_MODELS

    # A real synth attempt drives the genuine failover path: it marks the
    # primary down, promotes the standby and records the reason.
    assert cb_client.post("/api/debug/speak",
                          json={"text": "hi"}).status_code == 200
    body = cb_client.get("/api/tts/voices").json()
    assert body["provider"] == "kokoro", "the speaking engine is reported truthfully"
    assert body["primary"] == "chatterbox"
    assert body["failover_reason"], "the reason must be surfaced to the UI"
    info = body["model_info"]
    assert info["output_rate"] == 16000 and info["sample_rate"] == 24000
    assert info["description"] and info["languages"]
    assert body["available_models"] == list(CHATTERBOX_MODELS)
    assert info["available_languages"]["es"] == "Spanish"
    TTS.reset_health()
    assert "chatterbox" in body["failover_reason"].lower()
