"""FastAPI server: health, dashboard, call-log API, real-time voice WebSocket
(barge-in), and a live voice console for testing the bot without a PBX.
"""
from __future__ import annotations

import importlib.util
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response, FileResponse

from agent.logging_config import configure as configure_logging
from agent.logging_config import get_logger
from agent.service import build_stasis_handler

from server import auth as _auth
from server import call_log
from server.context import CONFIG
from server.pages import CAMPAIGN_MANAGER, DASHBOARD, DEBUG_CONSOLE
from server.routes import campaigns, debug, recordings, voice_ws, voices


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

app.include_router(voices.router)
app.include_router(debug.router)
app.include_router(campaigns.router)
app.include_router(recordings.router)
app.include_router(voice_ws.router)


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
