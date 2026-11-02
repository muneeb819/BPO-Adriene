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


@app.get("/api/tts/voices")
def tts_voices():
    """List available voices for the configured TTS engine."""
    c = CONFIG
    from agent.tts import TTS
    tts = TTS(c)
    result = {"provider": tts.active, "primary": tts.primary, "standby": tts.standby}
    if tts.active != tts.primary:
        # Never make the dashboard guess why the configured engine is silent.
        # `status()` also consults the class-level record left by the request
        # that actually failed — a fresh TTS instance has no reason of its own.
        reason = tts.status()["failover_reason"]
        if reason:
            result["failover_reason"] = reason

    if tts.active == "kokoro":
        import requests
        try:
            r = requests.get(f"{c.kokoro_base_url.rstrip('/')}/v1/audio/voices", timeout=5)
            if r.status_code == 200:
                data = r.json()
                voices = data.get("voices", [])
                result["voices"] = [{"id": v, "name": v} for v in voices] if isinstance(voices, list) else voices
                result["reachable"] = True
                result["current_voice"] = c.kokoro_voice
                result["speed"] = c.kokoro_speed
            else:
                result["voices"] = []
                result["reachable"] = False
                result["error"] = f"Kokoro returned {r.status_code}"
        except Exception as e:
            result["voices"] = []
            result["reachable"] = False
            result["error"] = str(e)
        result["known_voices"] = [
            {"id": "af_bella", "name": "Bella (warm female)"},
            {"id": "af_heart", "name": "Heart (warm)"},
            {"id": "af_nicole", "name": "Nicole"},
            {"id": "af_sarah", "name": "Sarah"},
            {"id": "af_sky", "name": "Sky"},
            {"id": "am_adam", "name": "Adam"},
            {"id": "am_michael", "name": "Michael"},
        ]
    elif tts.active == "chatterbox":
        result["model_info"] = tts.chatterbox_model_info()
        result["voices"] = [{"id": "default", "name": "Default (cloned from reference)"}]
        result["current_voice"] = c.chatterbox_voice_path or "default"
        result["available_models"] = ["turbo", "nano", "multilingual", "original"]
    elif tts.active == "elevenlabs":
        result["voices"] = [{"id": c.elevenlabs_voice, "name": c.elevenlabs_voice}]
        result["model"] = c.elevenlabs_model
        result["current_voice"] = c.elevenlabs_voice
    elif tts.active == "piper":
        # Local neural voices: ask the offline server what it has installed.
        import requests

        result["model"] = c.piper_model
        result["current_voice"] = c.piper_voice or "(server default)"
        try:
            r = requests.get(f"{c.piper_base_url.rstrip('/')}/v1/audio/voices", timeout=3)
            payload = r.json() if r.status_code == 200 else {}
        except Exception:
            payload = {}
        details = payload.get("voice_details") or []
        result["voices"] = ([{"id": v["id"], "name": v.get("name") or v["id"],
                              "language": v.get("language"), "kind": v.get("kind"),
                              "neural": True} for v in details]
                            or [{"id": v, "name": v} for v in payload.get("voices", [])])
        result["reachable"] = bool(result["voices"])
        result["known_voices"] = result["voices"]
        result["engine"] = "piper"
        result["neural"] = True
    else:
        result["voices"] = []

    # A failed-over ChatterBox deployment still needs its full model metadata.
    # The dashboard's voice panel renders its model / language / tag controls
    # from `model_info` and `available_models`, so keying those on the *active*
    # engine left every dropdown empty (and the status line reading
    # "out NaNkHz") after a failover — precisely when an operator needs to
    # re-route the engine.
    if tts.primary == "chatterbox" and "model_info" not in result:
        from agent.tts import CHATTERBOX_MODELS

        result["model_info"] = tts.chatterbox_model_info()
        result["available_models"] = list(CHATTERBOX_MODELS)
    return result


@app.post("/api/tts/switch-voice")
async def tts_switch_voice(request: Request):
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    from agent.tts import TTS
    tts = TTS(CONFIG)
    provider = tts.active
    if tts.primary == "chatterbox":
        # This endpoint configures the *selected* engine, not whichever engine
        # happens to be speaking at this instant. After a failover `active`
        # becomes the standby, which used to send every model/language/voice
        # request down the kokoro branch (or 400 "empty voice") — exactly when
        # an operator is most likely to be re-routing the ChatterBox engine.
        provider = "chatterbox"
    voice = data.get("voice", "").strip()
    if not voice and provider != "chatterbox":
        return JSONResponse({"error": "empty voice"}, status_code=400)
    if provider == "kokoro":
        CONFIG.kokoro_voice = voice
        return {"ok": True, "provider": provider, "voice": voice}
    elif provider == "piper":
        # Changing the voice keeps the current tuning; pitch/speed arrive through
        # the voice library (a library voice carries its own measured tuning).
        CONFIG.piper_voice = voice
        tune = data.get("tuning") or {}
        if "pitch" in tune:
            CONFIG.piper_pitch = int(float(tune["pitch"]))
        if "speed" in tune:
            CONFIG.piper_speed = float(tune["speed"])
        if "volume" in tune:
            CONFIG.piper_volume = float(tune["volume"])
        return {"ok": True, "provider": provider, "voice": voice,
                "pitch": CONFIG.piper_pitch, "speed": CONFIG.piper_speed,
                "volume": CONFIG.piper_volume}
    elif provider == "elevenlabs":
        CONFIG.elevenlabs_voice = voice
        return {"ok": True, "provider": provider, "voice": voice}
    elif provider == "chatterbox":
        # One request may carry model + language + voice together (that is what
        # the documented curl does), and every field must actually stick.
        # Previously the chain returned after the first branch, so "language"
        # and "voice" were silently dropped and an unknown model raised
        # ValueError → HTTP 500 instead of a friendly 400.
        from agent.tts import CHATTERBOX_LANGUAGES, CHATTERBOX_MODELS

        try:
            resolved = tts.chatterbox_settings(data)
        except ValueError as e:
            return JSONResponse(
                {"error": str(e), "available_models": list(CHATTERBOX_MODELS),
                 "available_languages": list(CHATTERBOX_LANGUAGES)},
                status_code=400,
            )

        if "model" in data and resolved["model"] != CONFIG.chatterbox_model:
            try:
                tts.switch_chatterbox_model(resolved["model"])
            except ValueError as e:
                return JSONResponse(
                    {"error": str(e), "available_models": list(CHATTERBOX_MODELS)},
                    status_code=400,
                )
        elif resolved["model"] in CHATTERBOX_MODELS:
            # Keep the config in step even when the engine is unchanged, so
            # model_info never reports a different engine than the one in use.
            CONFIG.chatterbox_model = resolved["model"]

        if resolved["language"]:
            CONFIG.chatterbox_language = resolved["language"]

        voice_error = None
        if resolved["voice"] is not None:
            if resolved["voice"]:
                voice_error = tts.validate_voice_reference(resolved["voice"])
                if voice_error:
                    return JSONResponse({"error": voice_error}, status_code=400)
            CONFIG.chatterbox_voice_path = resolved["voice"] or ""
        elif CONFIG.chatterbox_voice_path:
            # A different engine was selected; a reference cloned for the old
            # one may no longer be valid, so re-check it rather than pretend.
            voice_error = tts.validate_voice_reference(CONFIG.chatterbox_voice_path)

        return {
            "ok": True,
            "provider": provider,
            "result": resolved["message"],
            "model_info": tts.chatterbox_model_info(),
            "available_models": list(CHATTERBOX_MODELS),
            "available_languages": dict(CHATTERBOX_LANGUAGES),
            "applied": {
                "model": CONFIG.chatterbox_model,
                "language": CONFIG.chatterbox_language,
                "voice": CONFIG.chatterbox_voice_path,
            },
            **({"warning": voice_error} if voice_error else {}),
        }
    return JSONResponse({"error": f"unsupported provider {provider}"}, status_code=400)


# ---------------- VOICE STUDIO (library / design / remix) ----------------
DEFAULT_PREVIEW_TEXT = (
    "Hi, this is Lina from EverTrust. I'm calling about your Medicare benefits, "
    "and I wanted to see how you're doing today."
)


def _library():
    from agent.voice_library import get_library

    return get_library(CONFIG.voice_library_dir)


def _voice_error(e) -> JSONResponse:
    from agent.audio_dsp import AudioError

    if isinstance(e, AudioError):
        return JSONResponse({"error": str(e)}, status_code=400)
    if isinstance(e, KeyError):
        return JSONResponse({"error": f"unknown voice '{e.args[0]}'"}, status_code=404)
    raise e


def _designer_options() -> dict:
    """Everything the dashboard panels need to build their controls."""
    from agent.tts import CHATTERBOX_LANGUAGES, CHATTERBOX_MODELS

    backend_voices = []
    backend_engine = ""
    try:
        import requests

        r = requests.get(f"{CONFIG.kokoro_base_url.rstrip('/')}/v1/audio/voices", timeout=3)
        if r.status_code == 200:
            data = r.json()
            # The same wire format serves both local engines, so report which one
            # is really behind the URL (the label "kokoro" is historical).
            backend_engine = str(data.get("engine") or data.get("model") or "")
            details = data.get("voice_details")
            if isinstance(details, list) and details:
                backend_voices = details
            else:
                backend_voices = [{"id": v, "name": v} for v in data.get("voices", [])
                                  if isinstance(v, str)]
    except Exception:
        backend_voices = []
        backend_engine = ""
    from agent import elevenlabs_design as el
    from agent import neural_tts
    from agent.voice_library import ENGINES

    try:
        neural_voices = neural_tts.describe_models()
    except Exception as exc:                                  # noqa: BLE001
        neural_voices = []
        neural_error = str(exc)
    else:
        neural_error = ""
    return {
        "engines": list(ENGINES),
        "neural_voices": neural_voices,
        "neural_error": neural_error,
        "design_providers": el.providers(CONFIG),
        "chatterbox_models": list(CHATTERBOX_MODELS),
        "chatterbox_languages": dict(CHATTERBOX_LANGUAGES),
        "chatterbox_tags": ["[laugh]", "[chuckle]", "[cough]", "[sigh]",
                            "[gasp]", "[yawn]", "[um]"],
        "backend_voices": backend_voices,
        "backend_engine": backend_engine,
        "presets": [
            {"id": "warm", "name": "Warm & calm",
             "tuning": {"exaggeration": 0.35, "cfg_weight": 0.5, "temperature": 0.7, "speed": 0.95, "pitch": 48}},
            {"id": "bright", "name": "Bright & upbeat",
             "tuning": {"exaggeration": 0.75, "cfg_weight": 0.4, "temperature": 0.9, "speed": 1.08, "pitch": 60}},
            {"id": "serious", "name": "Serious & steady",
             "tuning": {"exaggeration": 0.2, "cfg_weight": 0.7, "temperature": 0.6, "speed": 0.98, "pitch": 38}},
            {"id": "energetic", "name": "Energetic",
             "tuning": {"exaggeration": 0.9, "cfg_weight": 0.35, "temperature": 1.0, "speed": 1.15, "pitch": 56}},
        ],
        "max_reference_seconds": 30,
        "max_upload_mb": 25,
    }


@app.get("/api/voices")
def voices_list():
    """The saved voice library (distinct from /api/tts/voices = engine voices)."""
    lib = _library()
    return {
        "ok": True,
        "voices": lib.list(),
        "active_id": lib.active_id(),
        "designer": _designer_options(),
        "remix_modes": ["blend", "stitch"],
    }


@app.post("/api/voices/upload")
async def voices_upload(request: Request):
    """Add a voice from an uploaded file, a server path, or a mic recording."""
    from agent.voice_library import decode_base64

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    name = (data.get("name") or "").strip()
    meta = {k: data.get(k) for k in ("engine", "model", "language", "voice",
                                     "tuning", "tags", "notes", "preview_text")}
    # match=1 stores the recording *and* points the voice at the resident neural
    # voice closest to it, so the clone can be heard immediately (see
    # VoiceLibrary.create_matched). Without it, the recording is just stored.
    want_match = bool(data.get("match"))
    match: dict = {}
    try:
        lib = _library()
        if want_match:
            meta.pop("engine", None)             # the matcher decides the engine
            if data.get("pcm_base64"):
                profile, match = lib.create_matched(
                    name, pcm=decode_base64(data["pcm_base64"], what="recording"),
                    sample_rate=int(data.get("sample_rate") or 16000), **meta)
            elif data.get("data_base64"):
                profile, match = lib.create_matched(
                    name, data=decode_base64(data["data_base64"], what="file"), **meta)
            elif data.get("path"):
                profile, match = lib.create_matched(name, path=data["path"].strip(), **meta)
            else:
                return JSONResponse(
                    {"error": "send one of: data_base64, pcm_base64 or path"},
                    status_code=400)
            return {"ok": True, "voice": profile.to_dict(), "match": match}
        if data.get("pcm_base64"):
            profile = lib.create_from_pcm(
                name, decode_base64(data["pcm_base64"], what="recording"),
                int(data.get("sample_rate") or 16000), **meta)
        elif data.get("data_base64"):
            profile = lib.create_from_bytes(
                name, decode_base64(data["data_base64"], what="file"),
                data.get("filename") or "", **meta)
        elif data.get("path"):
            profile = lib.create_from_path(name, data["path"].strip(), **meta)
        else:
            return JSONResponse(
                {"error": "send one of: data_base64 (a file), pcm_base64 "
                          "(a mic recording) or path (a file on the server)"},
                status_code=400)
    except Exception as e:                                    # noqa: BLE001
        return _voice_error(e)
    return {"ok": True, "voice": profile.to_dict()}


@app.post("/api/voices/match")
async def voices_match(request: Request):
    """Measure a recording and report which resident neural voice it matches.

    Read-only: nothing is created, so the dashboard can show "your recording is
    closest to Amy (pitch +1.3 semitones)". Send the audio as ``data_base64``
    (file), ``pcm_base64`` (mic) or ``path`` (already on the server).
    """
    from agent import audio_dsp as dsp
    from agent import neural_tts
    from agent.voice_library import decode_base64

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    try:
        if data.get("pcm_base64"):
            samples, rate = dsp.load_audio(decode_base64(data["pcm_base64"], what="recording"))
            if not rate:
                rate = int(data.get("sample_rate") or 16000)
        elif data.get("data_base64"):
            samples, rate = dsp.load_audio(decode_base64(data["data_base64"], what="file"))
        elif data.get("path"):
            samples, rate = dsp.load_audio(data["path"].strip())
            if len(samples) == 0:
                raise dsp.AudioError("that file has no audio in it")
        else:
            return JSONResponse({"error": "send data_base64, pcm_base64 or path"},
                                status_code=400)
        prepared, _ = dsp.prepare_reference(samples, rate or 16000)
        match = neural_tts.match_reference(prepared, dsp.TARGET_RATE,
                                          language=data.get("language"))
    except neural_tts.NeuralTTSError as e:
        return JSONResponse({"error": str(e), "hint": "python scripts/fetch_neural_voices.py"},
                            status_code=503)
    except Exception as e:                                    # noqa: BLE001
        return _voice_error(e)
    return {"ok": True, "match": match}


@app.post("/api/voices/describe")
async def voices_describe(request: Request):
    """**Prompt to voice**: turn a description into a voice.

    Local neural engine (default): ``{"description": "warm older British man,
    speaks slowly"}`` picks the resident voice that fits and stores the measured
    tuning. ``dry_run`` returns the plan without creating anything.
    """
    from agent import neural_tts

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    description = (data.get("description") or data.get("prompt") or "").strip()
    if not description:
        return JSONResponse(
            {"error": "describe the voice first, e.g. 'warm older British man, "
                      "speaks slowly'",
             "examples": ["warm older British man, speaks slowly",
                          "young energetic American woman",
                          "deep serious male narrator",
                          "bright cheerful female, fast"]},
            status_code=400)
    try:
        if data.get("dry_run"):
            plan = neural_tts.design_from_text(
                description, language=data.get("language"),
                pitch_level=(data.get("tuning") or {}).get("pitch"),
                speed=(data.get("tuning") or {}).get("speed"))
            return {"ok": True, "dry_run": True, "plan": plan}
        lib = _library()
        profile, plan = lib.describe(
            data.get("name") or description[:40], description,
            language=data.get("language"), tuning=data.get("tuning"),
            tags=data.get("tags"), notes=data.get("notes"),
            preview_text=data.get("preview_text"))
    except Exception as e:                                    # noqa: BLE001
        return _voice_error(e)
    return {"ok": True, "voice": profile.to_dict(), "plan": plan}


@app.get("/api/voices/design/providers")
async def voices_design_providers():
    """Which prompt-to-voice engines are usable right now (and how to unlock them)."""
    from agent import elevenlabs_design as el
    from agent import neural_tts

    return {"ok": True, "providers": el.providers(CONFIG),
            "local_models": neural_tts.describe_models()}


@app.post("/api/voices/design/elevenlabs")
async def voices_design_elevenlabs(request: Request):
    """ElevenLabs Voice Design: generate voices from a description, keep one.

    ``{"description": "..."}`` returns playable previews. Add
    ``{"keep": true, "generated_voice_id": "..."}`` to save the chosen one as a
    real voice; it is then stored in the library (engine ``elevenlabs``) with a
    rendered sample as its reference, so the local cloner/matcher can use it too.
    """
    from agent import elevenlabs_design as el

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not el.available(CONFIG):
        return JSONResponse(
            {"error": "Voice Design from a description needs an ElevenLabs key",
             "hint": el.hint(),
             "alternative": {"endpoint": "/api/voices/describe",
                             "detail": "design a voice from the resident neural voices "
                                       "without any key"}},
            status_code=400)
    try:
        description = (data.get("description") or "").strip()
        if not data.get("generated_voice_id"):
            result = el.design(description, (data.get("text") or "").strip(),
                               config=CONFIG,
                               loudness=float(data.get("loudness") or 0.5),
                               guidance=float(data.get("guidance") or 0.5),
                               seed=data.get("seed"), enhance=bool(data.get("enhance", True)))
            return {"ok": True, **result}
        # keeping one: persist it on ElevenLabs and in the local library
        saved = el.save(data["generated_voice_id"], data.get("name") or description[:40],
                        description, config=CONFIG)
        profile, info = el.save_locally(
            saved["voice_id"], data.get("name") or saved["name"], description,
            (data.get("text") or "").strip(), config=CONFIG, samples=_library())
        voice: dict = profile.to_dict()  # type: ignore[attr-defined]
        return {"ok": True, "saved": saved, "voice": voice, "info": info}
    except el.DesignUnavailable as e:
        return JSONResponse({"error": str(e), "hint": el.hint()}, status_code=400)
    except el.DesignError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    except Exception as e:                                    # noqa: BLE001
        return _voice_error(e)


@app.post("/api/voices/design")
async def voices_design(request: Request):
    """Create a voice by tuning an engine (no reference audio needed)."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    try:
        lib = _library()
        profile = lib.design(
            data.get("name"), engine=data.get("engine") or "chatterbox",
            model=data.get("model"), language=data.get("language"),
            voice=data.get("voice"), tuning=data.get("tuning"),
            tags=data.get("tags"), notes=data.get("notes"),
            preview_text=data.get("preview_text"))
    except Exception as e:                                    # noqa: BLE001
        return _voice_error(e)
    return {"ok": True, "voice": profile.to_dict()}


@app.post("/api/voices/remix")
async def voices_remix(request: Request):
    """Blend or stitch existing voices' references into a new voice."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    try:
        lib = _library()
        profile = lib.remix(
            data.get("name"), data.get("sources"), mode=data.get("mode") or "blend",
            pitch_semitones=data.get("pitch_semitones") or 0.0,
            match=bool(data.get("match")),
            engine=data.get("engine"), model=data.get("model"),
            language=data.get("language"), tuning=data.get("tuning"),
            tags=data.get("tags"), notes=data.get("notes"))
    except Exception as e:                                    # noqa: BLE001
        return _voice_error(e)
    return {"ok": True, "voice": profile.to_dict()}


@app.post("/api/voices/current")
async def voices_save_current(request: Request):
    """Save whatever voice the bot is configured with right now."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    try:
        lib = _library()
        profile = lib.save_from_current(data.get("name"), CONFIG,
                                        tags=data.get("tags"), notes=data.get("notes"))
    except Exception as e:                                    # noqa: BLE001
        return _voice_error(e)
    return {"ok": True, "voice": profile.to_dict()}


@app.get("/api/voices/{vid}")
def voices_detail(vid: str):
    from agent.voice_library import get_library  # noqa: F401  (keeps import local)

    lib = _library()
    profile = lib.find(vid)
    if profile is None:
        return JSONResponse({"error": f"unknown voice '{vid}'"}, status_code=404)
    item = profile.to_dict()
    item["active"] = lib.active_id() == vid
    item["ref_path"] = lib.ref_path(vid)
    return {"ok": True, "voice": item}


@app.put("/api/voices/{vid}")
async def voices_update(vid: str, request: Request):
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    try:
        lib = _library()
        profile = lib.update(vid, **data)
    except Exception as e:                                    # noqa: BLE001
        return _voice_error(e)
    return {"ok": True, "voice": profile.to_dict()}


@app.delete("/api/voices/{vid}")
def voices_delete(vid: str):
    lib = _library()
    if not lib.delete(vid):
        return JSONResponse({"error": f"unknown voice '{vid}'"}, status_code=404)
    return {"ok": True, "deleted": vid}


@app.post("/api/voices/{vid}/apply")
async def voices_apply(vid: str):
    """Make this voice the one the bot speaks with."""
    from agent.tts import TTS

    try:
        lib = _library()
        applied = lib.apply(vid, CONFIG)
    except Exception as e:                                    # noqa: BLE001
        return _voice_error(e)
    # An explicit operator choice deserves a fresh attempt at the engine.
    TTS.reset_health()
    return {"ok": True, "applied": applied, "active_id": vid,
            "voice": lib.get(vid).to_dict()}


@app.post("/api/voices/{vid}/preview")
async def voices_preview(vid: str, request: Request):
    """Speak a phrase with this voice and return it as wav audio.

    Falls back to the standby engine when the profile's own engine is not
    runnable here (no weights, no key), so the operator always hears something —
    the fallback is reported in the response headers.
    """
    body = {}
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            body = await request.json()
        except Exception:
            body = {}
    try:
        lib = _library()
        profile = lib.get(vid)
    except KeyError as e:
        return JSONResponse({"error": f"unknown voice '{e.args[0]}'"}, status_code=404)

    text = (body.get("text") or "").strip() or profile.preview_text or DEFAULT_PREVIEW_TEXT
    if body.get("include_tags") and profile.tags:
        text = f"{text} {' '.join(profile.tags)}"

    cfg = lib.preview_config(vid, CONFIG)
    return await _render_preview(cfg, text)


def _draft_config(data: dict):
    """Build a config for an unsaved voice definition (designer preview)."""
    from dataclasses import replace

    from agent.audio_dsp import AudioError
    from agent.voice_library import clean_tags, clean_tuning, validate_engine_language

    engine = (data.get("engine") or "chatterbox").strip().lower()
    if engine not in ("chatterbox", "kokoro", "piper"):
        raise AudioError("unknown engine '%s' — use one of: chatterbox, kokoro, piper"
                         % engine)
    overrides: dict = {"tts_provider": engine}
    tuning = clean_tuning(data.get("tuning") or {})
    if engine == "piper":
        # The offline neural engine: pick a resident voice and pass the studio
        # sliders through to it (pitch/speed are applied by the voice server).
        if data.get("voice"):
            overrides["piper_voice"] = str(data["voice"]).strip()
        if "speed" in tuning:
            overrides["piper_speed"] = float(tuning["speed"])
        if "pitch" in tuning:
            overrides["piper_pitch"] = int(tuning["pitch"])
        if "volume" in tuning:
            overrides["piper_volume"] = float(tuning["volume"])
    elif engine == "chatterbox":
        model = (data.get("model") or CONFIG.chatterbox_model or "turbo").strip()
        language = (data.get("language") or "en").strip().lower()
        validate_engine_language(engine, model, language)
        overrides.update({"chatterbox_model": model, "chatterbox_language": language})
        for key in ("exaggeration", "cfg_weight", "temperature"):
            if key in tuning:
                overrides[f"chatterbox_{key}"] = float(tuning[key])
    else:
        if data.get("voice"):
            overrides["kokoro_voice"] = str(data["voice"]).strip()
        if "speed" in tuning:
            overrides["kokoro_speed"] = float(tuning["speed"])
        if "pitch" in tuning:
            overrides["kokoro_pitch"] = int(tuning["pitch"])
    return replace(CONFIG, **overrides), tuning, clean_tags(data.get("tags"))


async def _render_preview(cfg, text: str):
    """Speak `text` with `cfg`, falling back to the standby engine if it fails.

    The operator must always hear something: if the profile's own engine cannot
    run on this machine (no weights, no key) the standby speaks instead and the
    response headers say which engine and whether it was a fallback.
    """
    from dataclasses import replace

    from agent.tts import LOCAL_HTTP_PROVIDERS, TTS, wav_from_pcm16

    #: what the *profile* asked for, kept separate from the engine a given attempt
    #: uses — the operator cares "I asked ChatterBox, piper spoke", not the internals
    requested_engine = str(cfg.tts_provider)
    attempts = [("profile", cfg)]
    # Order of the fallbacks: the configured standby first, then any other local
    # server that is actually answering. That last part matters because the
    # standby *can* be the same engine the profile asks for (apply() swaps the two
    # slots around), and a machine without ChatterBox weights or an API key should
    # still let the operator hear the voice — labelled as a fallback, never passed
    # off as the profile's own engine.
    order: "list[str]" = []
    standby = (CONFIG.tts_standby_provider or "").strip().lower()
    if standby:
        order.append(standby)
    order.extend(name for name in LOCAL_HTTP_PROVIDERS if name not in order)
    seen = {cfg.tts_provider}
    for name in order:
        if not name or name in seen:
            continue
        seen.add(name)
        if name in LOCAL_HTTP_PROVIDERS:
            try:
                if not TTS(replace(cfg, tts_provider=name)).probe_local(name):
                    continue             # not running: do not make the user wait on it
            except Exception:            # noqa: BLE001 - a probe must never block a preview
                pass
        attempts.append(("standby", replace(cfg, tts_provider=name)))
    problems = []
    for label, attempt_cfg in attempts:
        try:
            tts = TTS(attempt_cfg)
            audio = b"".join([c async for c in tts.synthesize_stream_pcm(text)])
            if not audio:
                raise RuntimeError("the engine returned no audio")
        except Exception as e:                                  # noqa: BLE001
            problems.append(f"{attempt_cfg.tts_provider}: {e}")
            continue
        # The engine has its own failover chain, so the engine that *actually*
        # spoke can differ from the one we asked for. Report the truth rather
        # than labelling standby audio as the profile's own voice.
        used = tts.active
        # Prefer what the server itself reported (X-Voice-Engine): the provider
        # nicknames "kokoro" and "piper" can point at the same URL, and only the
        # server knows which model actually produced the audio.
        reported = (getattr(tts, "engine_reported", "") or "").strip() or str(used)
        reason = (tts.status().get("failover_reason") or "")[:180]
        mode = label if used == attempt_cfg.tts_provider else "standby"
        headers = {"X-Voice-Engine": reported, "X-Voice-Mode": mode,
                   "X-Voice-Requested": requested_engine,
                   "X-Voice-Fallback": "0" if mode == "profile" else "1"}
        model = (getattr(tts, "model_reported", "") or "").strip()
        if model:
            headers["X-Voice-Model"] = model
        if reason:
            headers["X-Voice-Note"] = "".join(c if 32 <= ord(c) < 127 else " "
                                              for c in reason)
        return Response(content=wav_from_pcm16(audio), media_type="audio/wav",
                        headers=headers)
    return JSONResponse(
        {"error": "could not render this voice: " + " | ".join(problems),
         "hint": "install the engine (pip install chatterbox-tts) or set "
                 "TTS_STANDBY_PROVIDER to a running local voice server"},
        status_code=502)


@app.post("/api/voices/preview-draft")
async def voices_preview_draft(request: Request):
    """Preview a voice that has not been saved yet (the designer's preview button)."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    text = (data.get("text") or "").strip() or DEFAULT_PREVIEW_TEXT
    try:
        cfg, _tuning, tags = _draft_config(data)
    except Exception as e:                                        # noqa: BLE001
        return _voice_error(e)
    if data.get("include_tags") and tags:
        text = f"{text} {' '.join(tags)}"
    return await _render_preview(cfg, text)


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
