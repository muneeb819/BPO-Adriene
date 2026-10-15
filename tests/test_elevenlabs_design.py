"""ElevenLabs Voice Design: the cloud half of prompt-to-voice.

No network and no key are used. A fake client stands in for the SDK and the tests
pin down the parts that matter:

* without a key the endpoint answers 400 with a setup hint — never a crash, and
  it points at the local alternative that *does* work keyless;
* a description produces previews (base64 audio) and keeping one stores a real
  library entry whose reference can also feed the local cloner;
* API failures surface as 502, not as a silent success.

The fakes mirror the installed SDK's shape (``elevenlabs`` 2.73.0):
``client.text_to_voice.create_previews`` → ``VoiceDesignPreviewResponse``,
``.create`` → ``Voice``, ``client.text_to_speech.convert`` → bytes.
"""
from __future__ import annotations

import base64
import io
import math
import wave
from array import array
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agent import elevenlabs_design as el

DESCRIPTION = "a warm, confident middle-aged woman with a slight British accent"


def wav_bytes(seconds: float = 2.0, rate: int = 24000) -> bytes:
    samples = array("h", (int(9000 * math.sin(2 * math.pi * 220 * i / rate))
                          for i in range(int(rate * seconds))))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())
    return buf.getvalue()


class FakeTextToVoice:
    def __init__(self, outer):
        self.outer = outer

    def create_previews(self, *, voice_description, **kw):
        # record under the SDK's own parameter name so the tests assert the real
        # API contract rather than our internal wording
        self.outer.preview_calls.append({"voice_description": voice_description, **kw})
        if self.outer.boom:
            raise RuntimeError("api says no")
        audio = base64.b64encode(b"fake-mp3-bytes").decode()
        return SimpleNamespace(
            text=kw.get("text") or "auto generated",
            previews=[SimpleNamespace(generated_voice_id=f"preview-{i}",
                                      media_type="audio/mpeg",
                                      duration_secs=4.5,
                                      language="en",
                                      audio_base_64=audio)
                      for i in range(3)])

    def create(self, *, voice_name, voice_description, generated_voice_id, **kw):
        self.outer.save_calls.append({"name": voice_name, "description": voice_description,
                                      "preview": generated_voice_id})
        if self.outer.boom:
            raise RuntimeError("api says no")
        return SimpleNamespace(voice_id="N2lVS1w4EtoT3dr4eOWO", name=voice_name,
                               description=voice_description, category="generated")


class FakeTextToSpeech:
    def convert(self, *, voice_id, model_id, text, **kw):
        return iter([wav_bytes()])


class FakeClient:
    def __init__(self, outer):
        self.text_to_voice = FakeTextToVoice(outer)
        self.text_to_speech = FakeTextToSpeech()


@pytest.fixture()
def fake(monkeypatch):
    """A configured key plus a fake SDK client, and a temp library to save into."""
    outer = SimpleNamespace(preview_calls=[], save_calls=[], boom=False)
    monkeypatch.setattr(el, "_client", lambda config=None: FakeClient(outer))
    yield outer


@pytest.fixture()
def configured(monkeypatch):
    import server.app as appmod

    monkeypatch.setattr(appmod.CONFIG, "elevenlabs_api_key", "sk-test")


@pytest.fixture()
def studio(monkeypatch, tmp_path):
    import server.app as appmod

    monkeypatch.setattr(appmod.CONFIG, "voice_library_dir", str(tmp_path / "voices"))
    import agent.voice_library as vl

    vl._LIBRARY = None
    with TestClient(appmod.app) as client:
        yield client


# --------------------------------------------------------------------------- #
# availability
# --------------------------------------------------------------------------- #
def test_no_key_means_unavailable_with_a_usable_hint(monkeypatch):
    monkeypatch.setattr(el, "api_key", lambda config=None: "")
    assert el.available(None) is False
    assert "ELEVENLABS_API_KEY" in el.hint()
    assert ".env" in el.hint()
    with pytest.raises(el.DesignUnavailable):
        el.design(DESCRIPTION, "hello", config=None)


def test_providers_explain_both_routes(monkeypatch):
    monkeypatch.setattr(el, "api_key", lambda config=None: "")
    providers = {p["id"]: p for p in el.providers(None)}
    assert providers["local"]["needs_key"] is False
    assert providers["elevenlabs"]["available"] is False
    assert "ELEVENLABS_API_KEY" in providers["elevenlabs"]["detail"]


# --------------------------------------------------------------------------- #
# the API wrapper
# --------------------------------------------------------------------------- #
def test_design_returns_playable_previews(fake, monkeypatch):
    monkeypatch.setattr(el, "api_key", lambda config=None: "sk-test")
    result = el.design(DESCRIPTION, "say something", config=None)
    assert len(result["previews"]) == 3
    preview = result["previews"][0]
    assert preview["generated_voice_id"] == "preview-0"
    assert base64.b64decode(preview["audio_base64"])
    assert mock_payload(fake)["should_enhance"] is True


def mock_payload(fake) -> dict:
    return fake.preview_calls[-1]


def test_design_forwards_loudness_guidance_and_seed(fake, monkeypatch):
    monkeypatch.setattr(el, "api_key", lambda config=None: "sk-test")
    el.design(DESCRIPTION, "say something", config=None, loudness=0.9, guidance=0.2, seed=7)
    payload = mock_payload(fake)
    assert payload["loudness"] == pytest.approx(0.9)
    assert payload["guidance_scale"] == pytest.approx(0.2)
    assert payload["seed"] == 7


def test_design_without_text_asks_the_api_to_write_it(fake, monkeypatch):
    monkeypatch.setattr(el, "api_key", lambda config=None: "sk-test")
    el.design(DESCRIPTION, "", config=None)
    assert mock_payload(fake)["auto_generate_text"] is True


def test_empty_or_too_long_descriptions_are_refused(fake, monkeypatch):
    monkeypatch.setattr(el, "api_key", lambda config=None: "sk-test")
    with pytest.raises(el.DesignError, match="describe the voice"):
        el.design("   ", "hi", config=None)
    with pytest.raises(el.DesignError, match="600 characters"):
        el.design("x" * 700, "hi", config=None)
    assert fake.preview_calls == [], "nothing reached the API"


def test_api_errors_become_design_errors(fake, monkeypatch):
    monkeypatch.setattr(el, "api_key", lambda config=None: "sk-test")
    fake.boom = True
    with pytest.raises(el.DesignError, match="Voice Design failed"):
        el.design(DESCRIPTION, "hi", config=None)
    with pytest.raises(el.DesignError, match="saving the voice failed"):
        el.save("preview-0", "Name", DESCRIPTION, config=None)


def test_save_returns_the_new_voice_id(fake, monkeypatch):
    monkeypatch.setattr(el, "api_key", lambda config=None: "sk-test")
    saved = el.save("preview-0", "My voice", DESCRIPTION, config=None)
    assert saved["voice_id"] == "N2lVS1w4EtoT3dr4eOWO"
    assert fake.save_calls[-1]["preview"] == "preview-0"


def test_save_locally_stores_a_usable_library_entry(fake, monkeypatch, tmp_path):
    monkeypatch.setattr(el, "api_key", lambda config=None: "sk-test")
    from agent.voice_library import VoiceLibrary

    lib = VoiceLibrary(str(tmp_path / "voices"))
    profile, info = el.save_locally("N2lVS1w4EtoT3dr4eOWO", "Designed", DESCRIPTION,
                                    "say this", config=None, samples=lib)
    assert profile.engine == "elevenlabs"
    assert profile.voice == "N2lVS1w4EtoT3dr4eOWO"
    assert profile.ref and lib.ref_path(profile.id).endswith(".wav")
    assert "Voice Design" in profile.notes
    assert info["reference"] is True


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def test_endpoint_400s_with_a_hint_when_no_key(studio, monkeypatch):
    import server.app as appmod

    monkeypatch.setattr(appmod.CONFIG, "elevenlabs_api_key", "")
    r = studio.post("/api/voices/design/elevenlabs", json={"description": DESCRIPTION})
    assert r.status_code == 400
    body = r.json()
    assert "ElevenLabs key" in body["error"]
    assert "ELEVENLABS_API_KEY" in body["hint"]
    assert body["alternative"]["endpoint"] == "/api/voices/describe"


def test_endpoint_generates_previews_with_a_key(studio, configured, fake):
    r = studio.post("/api/voices/design/elevenlabs", json={"description": DESCRIPTION})
    assert r.status_code == 200
    body = r.json()
    assert len(body["previews"]) == 3
    assert base64.b64decode(body["previews"][1]["audio_base64"])
    assert fake.preview_calls[-1]["voice_description"] == DESCRIPTION


def test_endpoint_keeps_the_chosen_preview_in_the_library(studio, configured, fake):
    r = studio.post("/api/voices/design/elevenlabs",
                    json={"description": DESCRIPTION, "name": "Keeper",
                          "generated_voice_id": "preview-1", "text": "say this"})
    assert r.status_code == 200
    body = r.json()
    assert body["saved"]["voice_id"] == "N2lVS1w4EtoT3dr4eOWO"
    assert body["voice"]["engine"] == "elevenlabs"
    assert body["voice"]["voice"] == "N2lVS1w4EtoT3dr4eOWO"
    listed = studio.get("/api/voices").json()["voices"]
    assert [v["name"] for v in listed] == ["Keeper"]
    assert listed[0]["has_reference"] is True, "the sample is kept for the local cloner"


def test_endpoint_reports_api_failures_as_502(studio, configured, fake):
    fake.boom = True
    r = studio.post("/api/voices/design/elevenlabs", json={"description": DESCRIPTION})
    assert r.status_code == 502
    assert "Voice Design failed" in r.json()["error"]


def test_the_designed_voice_keeps_its_id_end_to_end(studio, configured, fake):
    """A kept voice must apply as that voice, not silently fall back to Jessica."""
    from agent.voice_ids import resolve_voice_id

    r = studio.post("/api/voices/design/elevenlabs",
                    json={"description": DESCRIPTION, "name": "Keeper",
                          "generated_voice_id": "preview-0"})
    voice_id = r.json()["voice"]["voice"]
    assert resolve_voice_id(voice_id) == voice_id
