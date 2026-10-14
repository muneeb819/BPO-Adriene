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
