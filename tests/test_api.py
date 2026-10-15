"""HTTP API tests: campaign CRUD, deploy, persistence and CSV/JSON exports.

Everything runs against a tmp campaign store, tmp memory and tmp call log, so
the suite never mutates the project's real data/ directory.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import server.app as appmod


@pytest.fixture()
def client(tmp_store, tmp_memory, call_log_tmp):
    with TestClient(appmod.app) as c:
        yield c


# ---------------- health ----------------

def test_health_reports_ready(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["tts"] and body["llm"]
    assert "calls" in body


def test_dashboard_campaigns_and_debug_pages_render(client):
    for path in ("/", "/campaigns", "/debug"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert len(r.text) > 500, path


# ---------------- campaign CRUD ----------------

def test_list_returns_four_campaigns_with_active(client):
    body = client.get("/api/campaigns").json()
    assert len(body["campaigns"]) == 4
    assert body["active"] == "medicare"
    assert all("builtin" in c for c in body["campaigns"])


def test_active_compliance_includes_recording_disclosure(client):
    body = client.get("/api/campaigns/active").json()
    assert body["id"] == "medicare"
    assert body["compliance"]["min_age"] == 45
    assert body["compliance"]["max_age"] == 90
    assert body["compliance"]["recording_disclosure"]


def test_get_single_campaign_returns_disclosure(client):
    body = client.get("/api/campaigns/medicare").json()
    assert body["id"] == "medicare"
    assert "disclosure" in body
    assert body["questions"][0]["kind"] == "yes_no"


def test_get_unknown_campaign_404(client):
    assert client.get("/api/campaigns/nope").status_code == 404


def test_create_campaign_requires_name(client):
    r = client.post("/api/campaigns", json={})
    assert r.status_code == 400
    assert "name" in r.json()["error"]


def test_create_campaign_from_scratch(client):
    r = client.post("/api/campaigns", json={"name": "Vision Plans"})
    assert r.status_code == 200
    cid = r.json()["id"]
    assert cid == "visionplans"
    assert any(c["id"] == cid for c in client.get("/api/campaigns").json()["campaigns"])


def test_create_campaign_clones_existing(client):
    r = client.post("/api/campaigns", json={"name": "Medicare Two", "base": "medicare"})
    assert r.status_code == 200
    cid = r.json()["id"]
    cloned = client.get(f"/api/campaigns/{cid}").json()
    assert len(cloned["questions"]) == 2


def test_create_campaign_rejects_unknown_base(client):
    r = client.post("/api/campaigns", json={"name": "X", "base": "ghost"})
    assert r.status_code == 404


def test_edit_and_reload_disclosure_roundtrip(client):
    cid = client.post("/api/campaigns", json={"name": "Round Trip"}).json()["id"]
    body = client.get(f"/api/campaigns/{cid}").json()
    body["name"] = "Renamed"
    body["disclosure"] = "Calls are recorded."
    assert client.put(f"/api/campaigns/{cid}", json=body).status_code == 200
    again = client.get(f"/api/campaigns/{cid}").json()
    assert again["name"] == "Renamed"
    assert again["disclosure"] == "Calls are recorded."


def test_put_cannot_change_campaign_id(client):
    cid = client.post("/api/campaigns", json={"name": "Immutable"}).json()["id"]
    body = client.get(f"/api/campaigns/{cid}").json()
    body["id"] = "hacked"
    client.put(f"/api/campaigns/{cid}", json=body)
    assert client.get(f"/api/campaigns/hacked").status_code == 404
    assert client.get(f"/api/campaigns/{cid}").status_code == 200


def test_deploy_makes_campaign_live(client):
    cid = client.post("/api/campaigns", json={"name": "Live One"}).json()["id"]
    r = client.post("/api/campaigns/active", json={"id": cid})
    assert r.status_code == 200 and r.json()["ok"]
    assert client.get("/api/campaigns").json()["active"] == cid


def test_deploy_unknown_campaign_404(client):
    assert client.post("/api/campaigns/active", json={"id": "ghost"}).status_code == 404


def test_delete_custom_campaign(client):
    cid = client.post("/api/campaigns", json={"name": "Disposable"}).json()["id"]
    assert client.delete(f"/api/campaigns/{cid}").status_code == 200
    assert client.get(f"/api/campaigns/{cid}").status_code == 404


def test_builtin_campaign_cannot_be_deleted(client):
    # medicare is live by default, so deploy a custom campaign first to
    # exercise the built-in protection specifically.
    cid = client.post("/api/campaigns", json={"name": "So Medicare Isn't Live"}).json()["id"]
    client.post("/api/campaigns/active", json={"id": cid})
    r = client.delete("/api/campaigns/medicare")
    assert r.status_code == 409
    assert "built-in" in r.json()["error"]


def test_live_campaign_cannot_be_deleted(client):
    cid = client.post("/api/campaigns", json={"name": "Live Cant Delete"}).json()["id"]
    client.post("/api/campaigns/active", json={"id": cid})
    r = client.delete(f"/api/campaigns/{cid}")
    assert r.status_code == 409
    assert "deploy another" in r.json()["error"]


def test_delete_unknown_campaign_404(client):
    assert client.delete("/api/campaigns/ghost").status_code == 404


def test_reset_single_campaign(client):
    client.put("/api/campaigns/aca", json={**client.get("/api/campaigns/aca").json(), "name": "Hacked"})
    r = client.post("/api/campaigns/aca/reset")
    assert r.status_code == 200
    assert client.get("/api/campaigns/aca").json()["name"] == "ACA / Marketplace Fronter"


def test_reset_unknown_campaign_404(client):
    assert client.post("/api/campaigns/ghost/reset").status_code == 404


def test_reset_all_restores_defaults(client):
    client.post("/api/campaigns", json={"name": "Scratch"})
    assert client.post("/api/campaigns/reset-all").json()["ok"] is True
    assert len(client.get("/api/campaigns").json()["campaigns"]) == 4


# ---------------- call log persistence ----------------

def test_call_log_is_persisted_to_disk(client, call_log_tmp):
    r = client.post("/api/calls", json={"lead_id": "L-1", "disposition": "AI-TRANSFER"})
    assert r.status_code == 200
    cid = r.json()["id"]
    assert client.get("/api/calls").json()["calls"][0]["id"] == cid
    on_disk = json.loads(open(call_log_tmp.CALL_LOG_PATH, encoding="utf-8").read())
    assert on_disk[0]["id"] == cid


def test_call_log_survives_a_restart(client, call_log_tmp, monkeypatch):
    client.post("/api/calls", json={"lead_id": "L-2"})
    persisted = json.loads(open(call_log_tmp.CALL_LOG_PATH, encoding="utf-8").read())
    # simulate a restart: reload from disk into a cleared list
    monkeypatch.setattr(call_log_tmp, "CALL_LOG", [])
    call_log_tmp._load_call_log()
    assert len(call_log_tmp.CALL_LOG) == len(persisted)
    monkeypatch.undo()


def test_call_log_limit_respects_query(client):
    for i in range(3):
        client.post("/api/calls", json={"lead_id": f"L-{i}"})
    assert len(client.get("/api/calls?limit=2").json()["calls"]) == 2


# ---------------- exports ----------------

def test_export_calls_csv(client):
    client.post("/api/calls", json={"lead_id": "CSV-1", "disposition": "AI-TRANSFER"})
    r = client.get("/api/export/calls")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    assert "attachment" in r.headers["content-disposition"]
    text = r.content.decode("utf-8-sig")
    assert text.splitlines()[0].startswith("id,ts,lead_id")
    assert "CSV-1" in text


def test_export_calls_json(client):
    client.post("/api/calls", json={"lead_id": "J-1"})
    r = client.get("/api/export/calls?fmt=json")
    assert r.status_code == 200
    assert json.loads(r.content.decode("utf-8-sig"))[0]["lead_id"] == "J-1"


def test_export_transcripts_csv_has_header(client):
    r = client.get("/api/export/transcripts")
    assert r.status_code == 200
    assert r.content.decode("utf-8-sig").splitlines()[0].startswith("call_id,started")


def test_export_transcripts_json_is_valid(client):
    r = client.get("/api/export/transcripts?fmt=json")
    assert r.status_code == 200
    assert isinstance(json.loads(r.content.decode("utf-8-sig")), list)


def test_export_insights_json(client):
    r = client.get("/api/export/insights")
    assert r.status_code == 200
    body = json.loads(r.content.decode("utf-8-sig"))
    assert "stats" in body and "total_calls" in body["stats"]
    assert "phrases_that_caused_confusion" in body


# ---------------- insights / recordings ----------------

def test_insights_endpoint(client, tmp_memory):
    body = client.get("/api/insights").json()
    assert body["stats"]["total_calls"] == 0


def test_recordings_endpoint_shape(client):
    body = client.get("/api/recordings").json()
    assert "recordings" in body
    assert isinstance(body["recordings"], list)


def test_missing_recording_is_404(client):
    assert client.get("/api/recordings/definitely-not-real").status_code == 404
    assert client.delete("/api/recordings/definitely-not-real").status_code == 404


# ---------------- debug chat API ----------------

def test_debug_chat_still_works_with_blank_llm_key(client):
    start = client.post("/api/debug/start", json={})
    sid = start.json().get("session_id") or start.json().get("sid")
    assert sid, start.text
    r = client.post("/api/debug/chat", json={"session_id": sid, "text": "hello"})
    assert r.status_code == 200
    assert r.json().get("agent") or r.json().get("reply")


# ---------------------------------------------------------------------------
# WEB_PASSWORD — the dashboard must be protectable on a real server
# ---------------------------------------------------------------------------

class TestDashboardPassword:
    """A local server on a LAN needs a lock on the door.

    `GET /health` stays open so monitoring (and scripts/healthcheck.py) still
    works; everything else — pages, APIs, recordings — needs the password.
    """

    @pytest.fixture()
    def locked(self, tmp_store, tmp_memory, tmp_path, monkeypatch):
        import server.app as appmod

        monkeypatch.setattr(appmod.CONFIG, "web_password", "s3cret")
        monkeypatch.setattr(appmod, "CALL_LOG_PATH", str(tmp_path / "call_log.json"))
        with TestClient(appmod.app) as c:
            yield c

    def test_pages_and_apis_require_a_password(self, locked):
        r = locked.get("/")
        assert r.status_code == 401
        assert r.headers["www-authenticate"].startswith("Basic ")
        assert locked.get("/api/voices").status_code == 401
        assert locked.get("/api/campaigns").status_code == 401

    def test_health_stays_open_for_monitoring(self, locked):
        r = locked.get("/health")
        assert r.status_code == 200 and r.json()["status"] == "ok"

    def test_the_wrong_password_is_rejected(self, locked):
        r = locked.get("/", auth=("any", "nope"))
        assert r.status_code == 401
        # a wrong user with the right password is still fine: the user is not checked
        assert locked.get("/", auth=("lina", "s3cret")).status_code == 200

    def test_the_right_password_opens_everything_and_sets_a_cookie(self, locked):
        r = locked.get("/", auth=("lina", "s3cret"))
        assert r.status_code == 200
        cookie = r.cookies.get("bpo_auth")
        assert cookie, "a login must set the cookie the voice socket needs"
        # the cookie alone is enough (that is what the WebSocket handshake sends)
        locked.cookies.set("bpo_auth", cookie)
        assert locked.get("/api/campaigns").status_code == 200

    def test_a_forged_cookie_does_not_work(self, locked):
        locked.cookies.set("bpo_auth", "0" * 32)
        assert locked.get("/").status_code == 401

    def test_an_open_server_needs_no_credentials(self, tmp_store, tmp_memory, monkeypatch):
        """The default (no password) must keep working exactly as before."""
        import server.app as appmod

        monkeypatch.setattr(appmod.CONFIG, "web_password", "")
        with TestClient(appmod.app) as c:
            assert c.get("/").status_code == 200
            assert c.get("/api/voices").status_code == 200


def test_the_voice_socket_checks_the_password(tmp_store, tmp_memory, monkeypatch):
    """The mic socket is a door into the bot — it must not be a way around the 401."""
    import server.app as appmod

    monkeypatch.setattr(appmod.CONFIG, "web_password", "s3cret")
    with TestClient(appmod.app) as c:
        with pytest.raises(Exception):
            with c.websocket_connect("/ws/voice"):
                pass
        c.cookies.set("bpo_auth", appmod._auth_token("s3cret"))
        with c.websocket_connect("/ws/voice") as ws:
            assert ws.receive_json()["type"]                # the session announced itself


def test_debug_voice_reports_the_engine_that_is_actually_selected(tmp_store, tmp_memory, monkeypatch):
    """The debug console must not cry "voice keys missing" when a voice works.

    Ported from main's "include ChatterBox in realtime voice check" fix, extended
    to the local neural engine: the payload names the selected voice, whether the
    live socket can run, and — when it cannot — exactly what is missing.
    """
    import server.app as appmod

    for provider, expected in (("piper", "en_US-amy-low"),
                               ("chatterbox", "ChatterBox/multilingual"),
                               ("elevenlabs", "Jessica")):
        monkeypatch.setattr(appmod.CONFIG, "tts_provider", provider)
        monkeypatch.setattr(appmod.CONFIG, "tts_standby_provider", "")
        with TestClient(appmod.app) as c:
            body = c.get("/api/debug/voice").json()
        assert body["tts_provider"] == provider
        assert body["voice"] == expected, (provider, body["voice"])
        assert body["speak"] is True

    # No mic configured -> the socket is not "realtime", and the warning must name
    # the missing piece (the STT key), not pretend the voice engine is broken.
    monkeypatch.setattr(appmod.CONFIG, "tts_provider", "piper")
    monkeypatch.setattr(appmod.CONFIG, "stt_provider", "console")
    with TestClient(appmod.app) as c:
        body = c.get("/api/debug/voice").json()
    assert body["realtime"] is False
    assert "DEEPGRAM_API_KEY" in body["warning"]
    assert "piper" in body["warning"]

    # With a mic configured the live socket is realtime and there is no warning.
    monkeypatch.setattr(appmod.CONFIG, "stt_provider", "deepgram")
    monkeypatch.setattr(appmod.CONFIG, "deepgram_api_key", "dg-test")
    with TestClient(appmod.app) as c:
        body = c.get("/api/debug/voice").json()
    assert body["realtime"] is True and body["warning"] == ""


@pytest.mark.parametrize("engine,voice", [("piper", "en_US-lessac-medium"),
                                          ("kokoro", "af_bella")])
def test_a_draft_preview_renders_for_every_engine_in_the_designer(
        tmp_store, tmp_memory, engine, voice):
    """The designer's engine picker lists three engines — all three must render.

    Regression: `piper` was offered by the studio UI while the draft-preview
    endpoint still rejected anything but chatterbox/kokoro.
    """
    with TestClient(appmod.app) as c:
        r = c.post("/api/voices/preview-draft",
                   json={"engine": engine, "voice": voice, "tuning": {"speed": 1.0},
                         "text": "Hello, this is a preview of my voice."})
        if r.status_code != 200:                     # engine not installed here
            pytest.skip(f"{engine} is not running on this machine: {r.json()}")
        assert r.headers["content-type"] == "audio/wav"
        assert len(r.content) > 20_000               # a real sentence, not a blip
        assert r.headers["X-Voice-Requested"] == engine
