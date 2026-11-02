"""FastAPI server: health, dashboard, call-log API, real-time voice WebSocket
(barge-in), and a live voice console for testing the bot without a PBX.
"""
from __future__ import annotations

import importlib.util
import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response, JSONResponse, FileResponse

from agent.config import Config
from agent.logging_config import configure as configure_logging
from agent.logging_config import get_logger
from agent.service import build_stasis_handler

from server import auth as _auth
from server import call_log
from server.context import CONFIG
from server.pages import CAMPAIGN_MANAGER, DASHBOARD, DEBUG_CONSOLE
from server.routes import voices


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Run once at boot: set the serving mode and restore call history."""
    global MODE
    MODE = "streaming" if CONFIG.tts_provider in ("elevenlabs", "kokoro") else "turn-based"
    _load_call_log()   # restore persisted call history after a restart
    log.info("server ready", extra={"mode": MODE, "tts_provider": CONFIG.tts_provider,
                                    "calls_loaded": len(CALL_LOG),
                                    "auth": "on" if _web_password() else "off"})
    yield


configure_logging()
log = get_logger("server")

app = FastAPI(title="AI Voice Agent", lifespan=lifespan)

MODE = "turn-based"
DEBUG = os.environ.get("DEBUG", "false").lower() in ("1", "true", "yes")

# ---------------- QA reports (backend/scripts/qa_runner.py) ----------------
# Mount the QA report/artifact router (shared with the local-debug backend) and serve
# the static viewer, so http://<host>/qa_reports.html works against this server too.
_QA_ENDPOINTS = Path(__file__).resolve().parents[1] / "backend" / "app" / "qa_endpoints.py"
_spec = importlib.util.spec_from_file_location("qa_endpoints", _QA_ENDPOINTS)
if _spec is None or _spec.loader is None:          # pragma: no cover - packaging error
    raise ImportError(f"could not load the QA router from {_QA_ENDPOINTS}")
_qa = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_qa)
app.include_router(_qa.router)

_FRONTEND_PUBLIC = Path(__file__).resolve().parents[1] / "frontend" / "public"


@app.get("/qa_reports.html")
def qa_reports_page():
    """Static QA Reports viewer (frontend/public/qa_reports.html)."""
    return FileResponse(_FRONTEND_PUBLIC / "qa_reports.html", media_type="text/html")

_DATA_DIR = os.environ.get("DATA_DIR", "data")
CALL_LOG_PATH = os.path.join(_DATA_DIR, "call_log.json")
CALL_LOG: list = []          # persistent call log (loaded from / saved to disk)
_MAX_CALL_LOG = 5000         # keep the newest N on disk


# ---------------- access control (WEB_PASSWORD) ----------------
# The implementation lives in server/auth.py; these aliases keep the existing
# call sites (and the middleware below) reading naturally, and keep the names
# tests/tools import from this module.
_auth.configure(lambda: getattr(CONFIG, "web_password", ""))

_AUTH_COOKIE = _auth.AUTH_COOKIE
_AUTH_OPEN_PATHS = _auth.AUTH_OPEN_PATHS
_AUTH_REALM = _auth.AUTH_REALM
_web_password = _auth.web_password
_auth_token = _auth.auth_token
_basic_credentials = _auth.basic_credentials
_authorised = _auth.authorised
_ws_authorised = _auth.ws_authorised


@app.middleware("http")
async def _require_password(request: Request, call_next):
    password = _web_password()
    if not password or request.url.path in _AUTH_OPEN_PATHS:
        return await call_next(request)
    if not _authorised(request):
        return Response(status_code=401, content="Authentication required",
                        headers={"WWW-Authenticate": _AUTH_REALM})
    response = await call_next(request)
    if not request.cookies.get(_AUTH_COOKIE):
        response.set_cookie(_AUTH_COOKIE, _auth_token(password),
                            httponly=True, samesite="lax")
    return response


def _load_call_log() -> None:
    """Load the persisted call log so restarts keep history."""
    global CALL_LOG
    CALL_LOG = call_log.load(CALL_LOG_PATH)


def _save_call_log() -> None:
    """Atomically persist the call log (crash-safe; see server/call_log.py)."""
    if not call_log.save(CALL_LOG, CALL_LOG_PATH, _MAX_CALL_LOG):
        # Used to be swallowed entirely: calls would vanish on restart with no
        # trace of why. Still non-fatal — a full disk must not drop the call.
        log.error("could not persist the call log", extra={"path": CALL_LOG_PATH})


def log_call(entry: dict) -> dict:
    """Append a call to the log AND persist it to disk."""
    entry.setdefault("id", os.urandom(8).hex())
    entry.setdefault("ts", datetime.now(timezone.utc).isoformat())
    CALL_LOG.append(entry)
    _save_call_log()
    return entry

_DEBUG_SESSIONS: dict = {}


def _debug_llm(config: Config):
    """LLM with MockLLM fallback so debug mode works with zero keys."""
    from agent.llm import LLM

    llm = LLM(config)
    try:
        if llm.models_available():
            return llm, config.llm_provider
    except Exception:
        pass
    from agent.mock_llm import MockLLM

    return MockLLM(config), f"{config.llm_provider} (mock fallback)"


@app.get("/health")
def health():
    from agent.tts import TTS

    tts = TTS(CONFIG)
    status = tts.status()
    return {
        "status": "ok",
        "mode": MODE,
        "debug": DEBUG,
        "tts": tts.active,
        "tts_primary": status["primary"],
        "tts_standby": status["standby"],
        "tts_failover_reason": status["failover_reason"],
        "tts_engines": status["engines"],
        "llm": CONFIG.llm_provider,
        "calls": len(CALL_LOG),
        "time": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/api/calls")
async def add_call(request: Request):
    data = await request.json()
    entry = log_call(data)
    return {"ok": True, "id": entry["id"]}


@app.get("/api/calls")
def list_calls(limit: int = 50):
    return {"calls": CALL_LOG[-limit:]}


@app.get("/api/config")
def config_view():
    from agent.tts import TTS

    c = CONFIG
    return {
        "mode": MODE,
        "debug": DEBUG,
        "llm_provider": c.llm_provider,
        "stt_provider": c.stt_provider,
        "tts_provider": c.tts_provider,
        "tts_standby_provider": c.tts_standby_provider,
        "tts_active": TTS(c).active,
        "elevenlabs_voice": c.elevenlabs_voice if c.tts_provider == "elevenlabs" else None,
        "elevenlabs_model": c.elevenlabs_model if c.tts_provider == "elevenlabs" else None,
        "kokoro_voice": c.kokoro_voice if c.tts_provider == "kokoro" else None,
        "chatterbox_model": c.chatterbox_model if c.tts_provider == "chatterbox" else None,
        "chatterbox_language": c.chatterbox_language if c.tts_provider == "chatterbox" else None,
        "chatterbox_voice_path": c.chatterbox_voice_path if c.tts_provider == "chatterbox" else None,
        "agent_name": c.agent_name,
        "business_name": c.business_name,
        "min_age": c.min_age,
        "max_age": c.max_age,
        "closer_queue": c.closer_queue,
        "vicidial_enabled": bool(c.vicidial_url and c.vicidial_user),
    }


# ---------------- DEBUG API (bot testing without PBX) ----------------

@app.post("/api/debug/start")
def debug_start():
    from agent.conversation import Conversation

    llm, llm_used = _debug_llm(CONFIG)
    convo = Conversation(CONFIG, llm)
    greeting = convo.start()
    sid = uuid.uuid4().hex[:12]
    _DEBUG_SESSIONS[sid] = convo
    return {
        "session_id": sid,
        "agent": greeting,
        "state": convo.ctx.state,
        "llm": llm_used,
        "ended": False,
    }


@app.post("/api/debug/chat")
async def debug_chat(request: Request):
    from agent.qualifier import extract_age

    data = await request.json()
    sid = data.get("session_id", "")
    text = (data.get("text") or "").strip()
    convo = _DEBUG_SESSIONS.get(sid)
    if convo is None:
        return {"error": "unknown session_id — POST /api/debug/start first"}
    if not text:
        return {"error": "empty text"}

    state_before = convo.ctx.state
    age_dbg = extract_age(text)
    result = convo.hear(text)

    payload = {
        "session_id": sid,
        "agent": result.ai_speech,
        "state_before": state_before,
        "state": convo.ctx.state,
        "ended": result.end_call,
        "disposition": result.disposition,
        "classification": result.classification,
        "debug": {
            "age_detected": age_dbg.detected,
            "age": age_dbg.age,
            "age_eligible": age_dbg.eligible,
            "ctx_age": convo.ctx.age,
            "medicare": convo.ctx.medicare_status,
            "transcript_len": len(convo.ctx.transcript),
        },
    }
    if result.end_call:
        action = "transfer" if result.disposition == "AI-TRANSFER" else "hangup"
        payload["action"] = action
        log_call({
            "lead_id": f"debug-{sid}",
            "action": action,
            "disposition": result.disposition,
            "facts": {**(result.classification or {}), "age": convo.ctx.age},
        })
    return payload


@app.get("/api/debug/transcript")
def debug_transcript(session_id: str = ""):
    convo = _DEBUG_SESSIONS.get(session_id)
    if convo is None:
        return {"error": "unknown session_id"}
    return {
        "session_id": session_id,
        "state": convo.ctx.state,
        "ended": convo.ended,
        "disposition": convo.ctx.disposition,
        "age": convo.ctx.age,
        "transcript": convo.ctx.transcript,
    }


@app.post("/api/debug/speak")
async def debug_speak(request: Request):
    from agent.tts import TTS
    from agent.tts import wav_from_pcm16

    data = await request.json()
    text = (data.get("text") or "").strip()[:800]
    if not text:
        return JSONResponse({"error": "empty text"}, status_code=400)
    tts = TTS(CONFIG)
    if not tts.active or tts.active == "console":
        return JSONResponse(
            {"error": "voice not configured — set TTS_PROVIDER=elevenlabs (with ELEVENLABS_API_KEY) "
                       "or TTS_PROVIDER=kokoro (see start-kokoro.ps1) in .env"},
            status_code=400,
        )
    # Speak through the SAME path a live call uses (synthesize_stream_pcm), so the
    # dashboard's "Test voice" button also exercises primary → standby failover
    # instead of hitting the primary engine directly and erroring out.
    chunks: list[bytes] = []
    try:
        async for chunk in tts.synthesize_stream_pcm(text):
            chunks.append(chunk)
    except Exception as e:
        return JSONResponse(
            {"error": f"TTS failed on {tts.active} (primary={tts.primary}, "
                      f"standby={tts.standby or 'none'}): {e}"},
            status_code=502,
        )
    audio = b"".join(chunks)
    if not audio:
        return JSONResponse({"error": "TTS returned no audio"}, status_code=502)
    # The pipeline always yields raw PCM16 @16 kHz, so wrap it in a fully
    # formed WAV (real sizes, not sentinel values) for the browser.
    return Response(content=wav_from_pcm16(audio), media_type="audio/wav")


@app.post("/api/debug/transcribe")
async def debug_transcribe(file: UploadFile = File(...)):
    import tempfile

    from agent.stt import STT

    raw = await file.read()
    if not raw:
        return JSONResponse({"error": "empty audio"}, status_code=400)
    if CONFIG.stt_provider != "deepgram" or not CONFIG.deepgram_api_key:
        return JSONResponse(
            {"error": "mic STT not configured — set STT_PROVIDER=deepgram and DEEPGRAM_API_KEY in .env"},
            status_code=400,
        )
    suffix = ".webm"
    ct = (file.content_type or "").lower()
    if "wav" in ct:
        suffix = ".wav"
    elif "mpeg" in ct or "mp3" in ct:
        suffix = ".mp3"
    elif "ogg" in ct:
        suffix = ".ogg"
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(raw)
            path = tmp.name
        text = STT(CONFIG).transcribe_file(path)
    except Exception as e:
        return JSONResponse({"error": f"STT failed: {e}"}, status_code=502)
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass
    return {"text": text or ""}


app.include_router(voices.router)


@app.get("/api/debug/voice")
def debug_voice_status():
    """Report which voice features are available (for the debug console UI)."""
    from agent.tts import TTS

    tts = TTS(CONFIG)
    mic = bool(CONFIG.stt_provider == "deepgram" and CONFIG.deepgram_api_key)
    # Every engine the live voice socket can drive. The socket needs a mic only
    # when the caller is speaking into it, so a missing Deepgram key is a *mic*
    # problem, not a "this voice cannot work" problem — which is why the warning
    # below names the piece that is actually missing instead of blaming keys in
    # general (that message used to appear even with a working local voice).
    realtime = bool(tts.active in ("elevenlabs", "kokoro", "piper", "chatterbox") and mic)
    warning = ""
    if not realtime:
        if tts.active in ("", "console"):
            warning = ("no voice engine selected — set TTS_PROVIDER=piper "
                       "(local neural voices, no key) or add a cloud key")
        elif not mic:
            warning = (f"mic input needs STT_PROVIDER=deepgram + DEEPGRAM_API_KEY — "
                       f"the voice engine ({tts.active}) is ready")
    return {
        "speak": bool(tts.active and tts.active != "console"),
        "mic": mic,
        "tts_provider": tts.active,
        "tts_primary": tts.primary,
        "tts_standby": tts.standby,
        "tts_failover_reason": tts.failover_reason,
        "stt_provider": CONFIG.stt_provider,
        "voice": (CONFIG.elevenlabs_voice if tts.active == "elevenlabs"
                  # PIPER_VOICE="" means the voice server picks the middle voice;
                  # report that as "auto" rather than echoing the model name.
                  else (CONFIG.piper_voice or "auto") if tts.active == "piper"
                  else f"ChatterBox/{CONFIG.chatterbox_model}" if tts.active == "chatterbox"
                  else CONFIG.kokoro_voice),
        "llm": CONFIG.llm_provider,
        "realtime": realtime,
        "warning": warning,
    }


# ---------------- CAMPAIGNS (multi-campaign: select / edit script / deploy) ----------------

@app.get("/api/campaigns")
def campaigns_list():
    from agent.campaign import get_store

    store = get_store()
    return {"campaigns": store.list(), "active": store.active_id()}


@app.post("/api/campaigns")
async def campaign_create(request: Request):
    """Create a brand-new campaign (optionally cloning an existing script)."""
    from agent.campaign import get_store

    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)
    store = get_store()
    base = (body.get("base") or "").strip() or None
    if base and store.get(base) is None:
        return JSONResponse({"error": f"base campaign '{base}' not found"}, status_code=404)
    c = store.create(name=name, description=(body.get("description") or "").strip(), base=base)
    return {"ok": True, "id": c.id, "name": c.name}


@app.delete("/api/campaigns/{cid}")
def campaign_delete(cid: str):
    """Delete a custom campaign. Refuses the live one and the built-in defaults."""
    from agent.campaign import DEFAULTS, get_store

    store = get_store()
    if cid == store.active_id():
        return JSONResponse({"error": "can't delete the live campaign — deploy another one first"}, status_code=409)
    if cid in DEFAULTS:
        return JSONResponse({"error": "built-in campaigns can't be deleted (use Reset instead)"}, status_code=409)
    if not store.delete(cid):
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"ok": True, "deleted": cid}


@app.get("/api/campaigns/active")
def campaign_active():
    from agent.campaign import get_store

    c = get_store().active()
    return {"id": c.id, "name": c.name, "compliance": c.compliance()}


@app.post("/api/campaigns/active")
async def campaign_deploy(request: Request):
    from agent.campaign import get_store

    data = await request.json()
    cid = (data.get("id") or "").strip()
    ok = get_store().set_active(cid)
    if not ok:
        return JSONResponse({"error": f"unknown campaign '{cid}'"}, status_code=404)
    return {"ok": True, "active": cid}


@app.get("/api/campaigns/{cid}")
def campaign_get(cid: str):
    from agent.campaign import get_store

    c = get_store().get(cid)
    if c is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    from dataclasses import asdict

    return asdict(c)


@app.put("/api/campaigns/{cid}")
async def campaign_save(cid: str, request: Request):
    from agent.campaign import get_store, _from_dict

    body = await request.json()
    body["id"] = cid  # id is immutable; path wins
    try:
        campaign = _from_dict(body)
    except Exception as e:
        return JSONResponse({"error": f"invalid campaign: {e}"}, status_code=400)
    get_store().save(campaign)
    return {"ok": True, "id": cid, "updated": campaign.updated}


@app.post("/api/campaigns/{cid}/reset")
def campaign_reset(cid: str):
    from agent.campaign import DEFAULTS, get_store

    factory = DEFAULTS.get(cid)
    if factory is None:
        return JSONResponse({"error": "no default for this campaign"}, status_code=404)
    get_store().save(factory())
    return {"ok": True, "id": cid}


@app.post("/api/campaigns/reset-all")
def campaigns_reset_all():
    from agent.campaign import get_store

    get_store().reset_defaults()
    return {"ok": True}


# ---------------- SELF-LEARNING INSIGHTS ----------------

@app.get("/api/insights")
def insights():
    from agent.memory import get_memory

    return get_memory().insights()


# ---------------- CALL RECORDINGS ----------------

def _rec_dir() -> str:
    # Resolve to <project root>/data/recordings regardless of the working dir.
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "recordings")


def _safe_id(rec_id: str) -> str:
    return os.path.basename(rec_id).replace(".wav", "").replace(".json", "")


@app.get("/api/recordings")
def recordings_list():
    from agent.recorder import list_recordings

    return {"recordings": list_recordings(_rec_dir())}


@app.get("/api/recordings/{rec_id}")
def recording_meta(rec_id: str):
    import json as _json

    rid = _safe_id(rec_id)
    path = os.path.join(_rec_dir(), f"{rid}.json")
    if not os.path.isfile(path):
        return JSONResponse({"error": "not found"}, status_code=404)
    with open(path, encoding="utf-8") as f:
        return _json.load(f)


@app.get("/recordings/{rec_id}")
def recording_audio(rec_id: str):
    rid = _safe_id(rec_id)
    if rid.endswith(".wav"):
        rid = rid[:-4]
    path = os.path.join(_rec_dir(), f"{rid}.wav")
    if not os.path.isfile(path):
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(path, media_type="audio/wav", filename=f"{rid}.wav")


@app.delete("/api/recordings/{rec_id}")
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
    import csv
    import io

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


@app.get("/api/export/calls")
def export_calls(fmt: str = "csv"):
    """Download the full call log (all dispositions, facts)."""
    if fmt == "json":
        return Response(
            content=json.dumps(CALL_LOG, ensure_ascii=False, indent=1),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="call_log.json"'},
        )
    header = ["id", "ts", "lead_id", "channel", "action", "disposition", "campaign", "age", "sentiment", "turns"]
    rows = []
    for c in CALL_LOG:
        facts = c.get("facts") or {}
        rows.append([
            c.get("id", ""), c.get("ts", ""), c.get("lead_id", ""), c.get("channel", "debug"),
            c.get("action", ""), c.get("disposition", ""), c.get("campaign", ""),
            facts.get("age", ""), facts.get("sentiment", ""), facts.get("turns", ""),
        ])
    return _csv_response(header, rows, "call_log.csv")


@app.get("/api/export/transcripts")
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


@app.get("/api/export/insights")
def export_insights():
    """Download the self-learning analysis as JSON."""
    from agent.memory import get_memory

    return Response(
        content=json.dumps(get_memory().insights(), ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="insights.json"'},
    )


# ---------------- REAL-TIME VOICE WEBSOCKET (barge-in) ----------------

@app.websocket("/ws/voice")
async def ws_voice(websocket: WebSocket):
    """Live voice call: mic -> Deepgram STT -> LLM brain -> ElevenLabs TTS -> speaker.

    Supports barge-in: the customer can interrupt the bot mid-sentence and it
    stops talking instantly, then listens to what they said.
    """
    if not _ws_authorised(websocket):
        # 4401 = unauthorised (custom close code, mirrors HTTP 401)
        await websocket.close(code=4401)
        return

    await websocket.accept()

    async def send(payload: dict):
        await websocket.send_json(payload)

    from server.voice_ws import VoiceSession

    session = VoiceSession(CONFIG, send)
    try:
        await session.run(websocket)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[WS] session error: {e}")
        try:
            await send({"type": "error", "message": str(e)})
        except Exception:
            pass
    finally:
        await session.close()
        # Persist the finished voice call to the call log (disposition + facts).
        try:
            convo = getattr(session, "convo", None)
            if convo is not None and getattr(convo, "ctx", None) is not None:
                ctx = convo.ctx
                disposition = ctx.disposition or getattr(session, "_result_disposition", "")
                if disposition:
                    action = "transfer" if disposition == "AI-TRANSFER" else "hangup"
                    log_call({
                        "lead_id": f"voice-{uuid.uuid4().hex[:10]}",
                        "channel": "voice",
                        "action": action,
                        "disposition": disposition,
                        "campaign": getattr(getattr(convo, "campaign", None), "id", ""),
                        "duration_sec": getattr(getattr(session, "recorder", None), "duration_sec", None),
                        "facts": {
                            "age": ctx.age,
                            "answers": dict(ctx.answers),
                            "sentiment": ctx.sentiment,
                            "turns": len(ctx.transcript),
                        },
                    })
        except Exception:
            pass






@app.get("/", response_class=HTMLResponse)
def dashboard():
    return HTMLResponse(DASHBOARD)


@app.get("/debug", response_class=HTMLResponse)
def debug_console():
    return HTMLResponse(DEBUG_CONSOLE)




@app.get("/campaigns", response_class=HTMLResponse)
def campaigns_page():
    return HTMLResponse(CAMPAIGN_MANAGER)


def run_ari():
    from agent.ari import ARI

    handler = build_stasis_handler(CONFIG, CALL_LOG)
    ARI(CONFIG).run_forever(CONFIG.ari_app, handler)
