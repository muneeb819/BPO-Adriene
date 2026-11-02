"""Call recordings and data exports.

Serving a recorded turn (WAV + its metadata), deleting one, and exporting the
call log, transcripts and learned insights as CSV or JSON.

These routes read the call log the server app owns. They reach it through the
app module rather than importing the list, because the app replaces that list
on load and the test fixtures swap it out — a copied reference would silently
go stale. ``_call_log()`` is the one accessor for it.
"""
from __future__ import annotations

import csv
import io
import json
import os

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse, Response

router = APIRouter()


def _rec_dir() -> str:
    # Resolve to <project root>/data/recordings regardless of the working dir.
    return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "data", "recordings")


def _safe_id(rec_id: str) -> str:
    return os.path.basename(rec_id).replace(".wav", "").replace(".json", "")


def _call_log() -> list:
    """The live call log (never a copy — the app rebinds it on load)."""
    from server import app as app_mod

    return app_mod.CALL_LOG


@router.get("/api/recordings")
def recordings_list():
    from agent.recorder import list_recordings

    return {"recordings": list_recordings(_rec_dir())}


@router.get("/api/recordings/{rec_id}")
def recording_meta(rec_id: str):
    import json as _json

    rid = _safe_id(rec_id)
    path = os.path.join(_rec_dir(), f"{rid}.json")
    if not os.path.isfile(path):
        return JSONResponse({"error": "not found"}, status_code=404)
    with open(path, encoding="utf-8") as f:
        return _json.load(f)


@router.get("/recordings/{rec_id}")
def recording_audio(rec_id: str):
    rid = _safe_id(rec_id)
    if rid.endswith(".wav"):
        rid = rid[:-4]
    path = os.path.join(_rec_dir(), f"{rid}.wav")
    if not os.path.isfile(path):
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(path, media_type="audio/wav", filename=f"{rid}.wav")


@router.delete("/api/recordings/{rec_id}")
def recording_delete(rec_id: str):
    rid = _safe_id(rec_id)
    removed = []
    for ext in (".wav", ".json"):
        p = os.path.join(_rec_dir(), f"{rid}{ext}")
        if os.path.isfile(p):
            os.remove(p)
            removed.append(ext)
    if not removed:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"deleted": rid}


# ---------------- EXPORTS (CSV / JSON downloads) ----------------

def _csv_response(header: list[str], rows: list[list], filename: str) -> Response:

    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(header)
    w.writerows(rows)
    # BOM so Excel opens UTF-8 correctly
    data = "\ufeff" + buf.getvalue()
    return Response(
        content=data.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/api/export/calls")
def export_calls(fmt: str = "csv"):
    """Download the full call log (all dispositions, facts)."""
    if fmt == "json":
        return Response(
            content=json.dumps(_call_log(), ensure_ascii=False, indent=1),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="call_log.json"'},
        )
    header = ["id", "ts", "lead_id", "channel", "action", "disposition", "campaign", "age", "sentiment", "turns"]
    rows = []
    for c in _call_log():
        facts = c.get("facts") or {}
        rows.append([
            c.get("id", ""), c.get("ts", ""), c.get("lead_id", ""), c.get("channel", "debug"),
            c.get("action", ""), c.get("disposition", ""), c.get("campaign", ""),
            facts.get("age", ""), facts.get("sentiment", ""), facts.get("turns", ""),
        ])
    return _csv_response(header, rows, "call_log.csv")


@router.get("/api/export/transcripts")
def export_transcripts(fmt: str = "csv"):
    """Download every recording's transcript (one row per speaker turn)."""
    from agent.recorder import list_recordings

    recs = list_recordings(_rec_dir())
    if fmt == "json":
        return Response(
            content=json.dumps(recs, ensure_ascii=False, indent=1),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="transcripts.json"'},
        )
    header = ["call_id", "started", "duration_sec", "campaign", "disposition", "age", "turn", "role", "text"]
    rows = []
    for r in recs:
        turns = r.get("transcript") or []
        if not turns:
            rows.append([r.get("id", ""), r.get("started", ""), r.get("duration_sec", ""),
                         r.get("campaign_name", r.get("campaign", "")), r.get("disposition", ""),
                         r.get("age", ""), "", "", ""])
        for i, t in enumerate(turns, 1):
            rows.append([
                r.get("id", ""), r.get("started", ""), r.get("duration_sec", ""),
                r.get("campaign_name", r.get("campaign", "")), r.get("disposition", ""),
                r.get("age", ""), i, t.get("role", ""), t.get("text", ""),
            ])
    return _csv_response(header, rows, "transcripts.csv")


@router.get("/api/export/insights")
def export_insights():
    """Download the self-learning analysis as JSON."""
    from agent.memory import get_memory

    return Response(
        content=json.dumps(get_memory().insights(), ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="insights.json"'},
    )


# ---------------- REAL-TIME VOICE WEBSOCKET (barge-in) ----------------
