"""FastAPI server: health, dashboard, call-log API, real-time voice WebSocket
(barge-in), and a live voice console for testing the bot without a PBX.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response, JSONResponse, FileResponse

from agent.config import Config
from agent.service import build_stasis_handler


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Run once at boot: set the serving mode and restore call history."""
    global MODE
    MODE = "streaming" if CONFIG.tts_provider in ("elevenlabs", "kokoro") else "turn-based"
    _load_call_log()   # restore persisted call history after a restart
    yield


app = FastAPI(title="AI Voice Agent", lifespan=lifespan)
CONFIG = Config.load()
MODE = "turn-based"
DEBUG = os.environ.get("DEBUG", "false").lower() in ("1", "true", "yes")

# ---------------- QA reports (backend/scripts/qa_runner.py) ----------------
# Mount the QA report/artifact router (shared with the local-debug backend) and serve
# the static viewer, so http://<host>/qa_reports.html works against this server too.
_QA_ENDPOINTS = Path(__file__).resolve().parents[1] / "backend" / "app" / "qa_endpoints.py"
_spec = importlib.util.spec_from_file_location("qa_endpoints", _QA_ENDPOINTS)
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
# Set WEB_PASSWORD in .env whenever the dashboard is reachable by anyone but the
# machine itself. Every page, API and recording is then behind HTTP Basic auth.
# Two deliberate details:
#   * /health stays open, so monitoring (and scripts/healthcheck.py) still works;
#   * a successful login also sets a cookie, because a browser cannot put an
#     Authorization header on a WebSocket handshake — the live voice socket would
#     otherwise be impossible to use from the dashboard.
# An empty WEB_PASSWORD (the default) leaves the server open, as before.
_AUTH_COOKIE = "bpo_auth"
_AUTH_OPEN_PATHS = ("/health",)
_AUTH_REALM = 'Basic realm="BPO Adriene"'


def _web_password() -> str:
    """Read at request time, so setting it (or the studio's Use) applies at once."""
    return (getattr(CONFIG, "web_password", "") or "").strip()


def _auth_token(password: str) -> str:
    return hashlib.sha256(f"bpo-adriene:{password}".encode("utf-8")).hexdigest()[:32]


def _basic_credentials(header: str) -> "tuple[str, str] | None":
    if not header.lower().startswith("basic "):
        return None
    try:
        raw = base64.b64decode(header.split(None, 1)[1].strip()).decode("utf-8")
    except Exception:                                            # noqa: BLE001
        return None
    user, _, password = raw.partition(":")
    return user, password


def _authorised(request: Request) -> bool:
    password = _web_password()
    creds = _basic_credentials(request.headers.get("authorization", ""))
    if creds and secrets.compare_digest(creds[1], password):
        return True
    cookie = request.cookies.get(_AUTH_COOKIE, "")
    return bool(cookie) and secrets.compare_digest(cookie, _auth_token(password))


def _ws_authorised(websocket: WebSocket) -> bool:
    password = _web_password()
    if not password:
        return True
    cookie = websocket.cookies.get(_AUTH_COOKIE, "")
    if cookie and secrets.compare_digest(cookie, _auth_token(password)):
        return True
    creds = _basic_credentials(websocket.headers.get("authorization", ""))
    if creds and secrets.compare_digest(creds[1], password):
        return True
    supplied = websocket.query_params.get("password", "")
    return bool(supplied) and secrets.compare_digest(supplied, password)


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
    try:
        with open(CALL_LOG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        CALL_LOG = data if isinstance(data, list) else []
    except (OSError, ValueError):
        CALL_LOG = []


def _save_call_log() -> None:
    """Atomically persist the call log (idempotent, crash-safe)."""
    try:
        os.makedirs(os.path.dirname(CALL_LOG_PATH) or ".", exist_ok=True)
        tmp = CALL_LOG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(CALL_LOG[-_MAX_CALL_LOG:], f, ensure_ascii=False, indent=1)
        os.replace(tmp, CALL_LOG_PATH)
    except OSError:
        pass


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
        return {"ok": True, "saved": saved, "voice": profile.to_dict(), "info": info}
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
                  else (CONFIG.piper_voice or CONFIG.piper_model) if tts.active == "piper"
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


DASHBOARD = """
<!doctype html><html><head><meta charset="utf-8"><title>AI Voice Agent</title>
<style>body{font-family:system-ui;margin:2rem;background:#0f172a;color:#e2e8f0}
h1{color:#38bdf8}h2{color:#94a3b8;font-size:.9rem;font-weight:normal;margin-top:.2rem}
table{border-collapse:collapse;width:100%;margin-top:1rem}
th,td{border:1px solid #334155;padding:.5rem;text-align:left;font-size:.85rem}
th{background:#1e293b}.tag{padding:.1rem .4rem;border-radius:.3rem;background:#334155}
.tag.ok{background:#166534}.tag.hangup{background:#991b1b}
.badge{display:inline-block;padding:.2rem .6rem;border-radius:1rem;font-size:.75rem;font-weight:600}
.badge.live{background:#166534;color:#bbf7d0}
a{color:#38bdf8}.meta{display:flex;gap:2rem;margin:1rem 0;flex-wrap:wrap}
.meta div{background:#1e293b;padding:.8rem 1.2rem;border-radius:.5rem}
.meta .label{font-size:.7rem;text-transform:uppercase;color:#64748b;letter-spacing:.05em}
.meta .val{font-size:1.1rem;font-weight:600;color:#e2e8f0}
.nav{margin:1rem 0;display:flex;gap:1rem}
.nav a{background:#1e293b;padding:.6rem 1.2rem;border-radius:.5rem;text-decoration:none;font-weight:600}
.nav a:hover{background:#334155}</style></head><body>
<h1>AI Voice Agent</h1>
<h2>Real-time voice + barge-in &bull; Lina</h2>
<div class="nav"><a href="/">&#128202; Dashboard</a><a href="/campaigns">&#9881;&#65039; Campaigns</a><a href="/debug">&#127908; Live Voice Console</a></div>
<div class="meta">
<div><div class="label">Pipeline</div><div class="val" id="mode">...</div></div>
<div><div class="label">TTS</div><div class="val" id="tts">...</div></div>
<div><div class="label">Voice</div><div class="val" id="voice">...</div></div>
<div><div class="label">STT</div><div class="val" id="stt">...</div></div>
<div><div class="label">Brain</div><div class="val" id="llm">...</div></div>
<div><div class="label">Status</div><div class="val" id="status">...</div></div>
</div>
<p>Live call qualification log.
  <span style="float:right;font-size:.8rem">
    &#8681; <a href="/api/export/calls">Calls CSV</a> &middot;
    <a href="/api/export/transcripts">Transcripts CSV</a> &middot;
    <a href="/api/export/insights">Insights JSON</a>
  </span>
</p>
<table id="t"><thead><tr><th>Time</th><th>Lead</th><th>Action</th><th>Disposition</th><th>Age</th></tr></thead>
<tbody></tbody></table>

<h2 style="margin-top:1.6rem;color:#38bdf8;font-size:1rem">&#127909; Call recordings <a href="#" onclick="loadRecs();return false;" style="font-size:.75rem;font-weight:400">&#8635; refresh</a></h2>
<p style="color:#64748b;font-size:.85rem;margin:.2rem 0 0">Every call is saved automatically — the full mixed audio (caller + Lina) plus the transcript.</p>
<div id="recordings">Loading…</div>
<h2 style="margin-top:1.6rem;color:#38bdf8;font-size:1rem">&#127897; Voice studio <a href="#" onclick="vsLoad();return false;" style="font-size:.75rem;font-weight:400">&#8635; refresh</a></h2>
<p style="color:#64748b;font-size:.85rem;margin:.2rem 0 .6rem">Save, design and remix voices. A saved voice keeps its engine, model, language, clone reference and tuning — press <b>Use</b> to make the bot speak with it.</p>
<div id="vsPanel" style="background:#1e293b;border:1px solid #334155;border-radius:.5rem;padding:1rem;margin:.6rem 0 1.4rem">
<div style="display:flex;gap:.5rem;flex-wrap:wrap;margin-bottom:.8rem">
<button id="vsTabLib" class="vsTab" onclick="vsTab('lib')" style="background:#0e7490;color:#fff;border:0;border-radius:.4rem;padding:.4rem .9rem;font-weight:600;cursor:pointer;font-size:.82rem">&#128218; Library</button>
<button id="vsTabAdd" class="vsTab" onclick="vsTab('add')" style="background:#334155;color:#e2e8f0;border:0;border-radius:.4rem;padding:.4rem .9rem;font-weight:600;cursor:pointer;font-size:.82rem">&#10133; Add voice</button>
<button id="vsTabDesign" class="vsTab" onclick="vsTab('design')" style="background:#334155;color:#e2e8f0;border:0;border-radius:.4rem;padding:.4rem .9rem;font-weight:600;cursor:pointer;font-size:.82rem">&#127912; Design</button>
<button id="vsTabRemix" class="vsTab" onclick="vsTab('remix')" style="background:#334155;color:#e2e8f0;border:0;border-radius:.4rem;padding:.4rem .9rem;font-weight:600;cursor:pointer;font-size:.82rem">&#127925; Remix</button>
<button id="vsTabSay" class="vsTab" onclick="vsTab('say')" style="background:#334155;color:#e2e8f0;border:0;border-radius:.4rem;padding:.4rem .9rem;font-weight:600;cursor:pointer;font-size:.82rem">&#128266; Test line</button>
</div>

<!-- ---------------- library ---------------- -->
<div id="vsLib">
<div id="vsGrid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:.6rem">Loading&hellip;</div>
<div style="display:flex;gap:.5rem;flex-wrap:wrap;align-items:center;margin-top:.8rem">
<label style="font-size:.7rem;color:#64748b;text-transform:uppercase">Save current voice as</label>
<input id="vsSaveName" placeholder="Lina — warm" style="flex:1;min-width:160px;background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:.4rem;padding:.4rem .6rem;font-size:.85rem">
<button onclick="vsSaveCurrent()" style="background:#0e7490;color:#fff;border:0;border-radius:.4rem;padding:.45rem 1rem;font-weight:600;cursor:pointer;font-size:.82rem">Save</button>
</div>
</div>

<!-- ---------------- add ---------------- -->
<div id="vsAdd" hidden>
<div style="display:flex;gap:.5rem;flex-wrap:wrap;align-items:center;margin-bottom:.5rem">
<label style="font-size:.7rem;color:#64748b;text-transform:uppercase">Name</label>
<input id="vsAddName" placeholder="My cloned voice" style="flex:1;min-width:180px;background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:.4rem;padding:.4rem .6rem;font-size:.85rem">
<label style="font-size:.7rem;color:#64748b;text-transform:uppercase">Engine</label>
<select id="vsAddEngine" style="background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:.4rem;padding:.4rem .5rem;font-size:.82rem"><option value="chatterbox">ChatterBox</option><option value="kokoro">Local server</option><option value="piper">Local neural (Piper)</option></select>
</div>
<div style="display:flex;gap:.6rem;flex-wrap:wrap;align-items:center;margin-bottom:.5rem;background:#0b1220;border:1px solid #1e3a8a;border-radius:.45rem;padding:.45rem .6rem">
<label style="font-size:.78rem;color:#93c5fd;display:flex;gap:.35rem;align-items:center;cursor:pointer">
<input type="checkbox" id="vsMatch" checked> Render on a neural voice (hear it straight away)</label>
<button onclick="vsCheckMatch()" style="background:#334155;color:#fff;border:0;border-radius:.4rem;padding:.3rem .7rem;font-weight:600;cursor:pointer;font-size:.75rem">Check match</button>
<span id="vsMatchOut" style="font-size:.74rem;color:#94a3b8"></span>
</div>
<div style="display:flex;gap:.5rem;flex-wrap:wrap;align-items:center;margin-bottom:.5rem">
<span style="font-size:.7rem;color:#64748b;text-transform:uppercase;min-width:5.5rem">1. Upload file</span>
<input type="file" id="vsFile" accept=".wav,.flac,.ogg,.mp3,.m4a,.aiff" style="font-size:.8rem;color:#94a3b8">
<button onclick="vsUploadFile()" style="background:#0e7490;color:#fff;border:0;border-radius:.4rem;padding:.4rem .9rem;font-weight:600;cursor:pointer;font-size:.8rem">Add from file</button>
</div>
<div style="display:flex;gap:.5rem;flex-wrap:wrap;align-items:center;margin-bottom:.5rem">
<span style="font-size:.7rem;color:#64748b;text-transform:uppercase;min-width:5.5rem">2. Server path</span>
<input id="vsPath" placeholder="C:/voices/agent_ref.wav" style="flex:1;min-width:200px;background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:.4rem;padding:.4rem .6rem;font-size:.85rem">
<button onclick="vsImportPath()" style="background:#0e7490;color:#fff;border:0;border-radius:.4rem;padding:.4rem .9rem;font-weight:600;cursor:pointer;font-size:.8rem">Import</button>
</div>
<div style="display:flex;gap:.5rem;flex-wrap:wrap;align-items:center">
<span style="font-size:.7rem;color:#64748b;text-transform:uppercase;min-width:5.5rem">3. Record mic</span>
<select id="vsRecSecs" style="background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:.4rem;padding:.4rem .5rem;font-size:.82rem"><option>8</option><option selected>12</option><option>15</option><option>20</option></select>
<button id="vsRecBtn" onclick="vsRecord()" style="background:#b91c1c;color:#fff;border:0;border-radius:.4rem;padding:.4rem .9rem;font-weight:600;cursor:pointer;font-size:.8rem">&#9210; Record &amp; add</button>
<span id="vsRecMeter" style="font-size:.75rem;color:#94a3b8"></span>
</div>
<p style="font-size:.75rem;color:#64748b;margin:.6rem 0 0">Tips: 10-15 s of clean speech, one speaker, no music. Files are trimmed, levelled and converted to mono 24 kHz automatically. Compressed formats (mp3/ogg/flac) need <code>pip install soundfile</code>.</p>
</div>

<!-- ---------------- design ---------------- -->
<div id="vsDesign" hidden>
<div style="background:#0b1220;border:1px solid #1e3a8a;border-radius:.45rem;padding:.6rem;margin-bottom:.7rem">
<div style="font-size:.78rem;color:#93c5fd;font-weight:600;margin-bottom:.35rem">Describe a voice &mdash; prompt to voice</div>
<div style="display:flex;gap:.5rem;flex-wrap:wrap;align-items:center">
<input id="vdDescribe" placeholder="warm older British man, speaks slowly" style="flex:1;min-width:230px;background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:.4rem;padding:.45rem .6rem;font-size:.85rem">
<select id="vdProvider" style="background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:.4rem;padding:.4rem .5rem;font-size:.82rem"></select>
<button onclick="vdDescribeGo(true)" style="background:#334155;color:#fff;border:0;border-radius:.4rem;padding:.45rem .9rem;font-weight:600;cursor:pointer;font-size:.8rem">Try (no save)</button>
<button onclick="vdDescribeGo(false)" style="background:#7c3aed;color:#fff;border:0;border-radius:.4rem;padding:.45rem 1rem;font-weight:600;cursor:pointer;font-size:.8rem">&#10024; Create voice</button>
</div>
<div id="vdDescribeOut" style="font-size:.76rem;color:#94a3b8;margin-top:.4rem;min-height:1em"></div>
<div id="vdPreviews" style="display:flex;gap:.5rem;flex-wrap:wrap;margin-top:.4rem"></div>
</div>
<div style="display:flex;gap:.5rem;flex-wrap:wrap;align-items:center;margin-bottom:.5rem">
<label style="font-size:.7rem;color:#64748b;text-transform:uppercase">Name</label>
<input id="vdName" placeholder="Upbeat closer" style="flex:1;min-width:160px;background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:.4rem;padding:.4rem .6rem;font-size:.85rem">
<label style="font-size:.7rem;color:#64748b;text-transform:uppercase">Preset</label>
<select id="vdPreset" onchange="vdApplyPreset()" style="background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:.4rem;padding:.4rem .5rem;font-size:.82rem"></select>
</div>
<div id="vdControls" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:.5rem .9rem;margin-bottom:.6rem"></div>
<div style="display:flex;gap:.5rem;flex-wrap:wrap;align-items:center;margin-bottom:.5rem">
<label style="font-size:.7rem;color:#64748b;text-transform:uppercase">Tags</label>
<div id="vdTags" style="display:flex;gap:.35rem;flex-wrap:wrap"></div>
</div>
<div style="display:flex;gap:.5rem;flex-wrap:wrap">
<button onclick="vdPreview()" style="background:#334155;color:#fff;border:0;border-radius:.4rem;padding:.45rem 1rem;font-weight:600;cursor:pointer;font-size:.82rem">&#9654; Preview</button>
<button onclick="vdSave()" style="background:#0e7490;color:#fff;border:0;border-radius:.4rem;padding:.45rem 1rem;font-weight:600;cursor:pointer;font-size:.82rem">Save to library</button>
</div>
</div>

<!-- ---------------- remix ---------------- -->
<div id="vsRemix" hidden>
<p style="font-size:.78rem;color:#94a3b8;margin:.1rem 0 .5rem">Pick two or more saved voices and mix them into a new one. <b>Blend</b> averages the timbres (weights matter), <b>stitch</b> joins them end to end.</p>
<div id="vrSources" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:.4rem;margin-bottom:.6rem"></div>
<div style="display:flex;gap:.5rem;flex-wrap:wrap;align-items:center;margin-bottom:.5rem">
<label style="font-size:.7rem;color:#64748b;text-transform:uppercase">Name</label>
<input id="vrName" placeholder="Blended voice" style="flex:1;min-width:150px;background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:.4rem;padding:.4rem .6rem;font-size:.85rem">
<label style="font-size:.7rem;color:#64748b;text-transform:uppercase">Mode</label>
<select id="vrMode" style="background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:.4rem;padding:.4rem .5rem;font-size:.82rem"><option value="blend">Blend (mix)</option><option value="stitch">Stitch (join)</option></select>
<label style="font-size:.7rem;color:#64748b;text-transform:uppercase">Pitch</label>
<input id="vrPitch" type="number" step="0.5" min="-12" max="12" value="0" title="Shifts pitch and pace together (reference only)" style="width:5rem;background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:.4rem;padding:.4rem .5rem;font-size:.82rem">
<label style="font-size:.76rem;color:#93c5fd;display:flex;gap:.35rem;align-items:center;cursor:pointer">
<input type="checkbox" id="vrNeural" checked> Render on a neural voice</label>
<button onclick="vrSave()" style="background:#0e7490;color:#fff;border:0;border-radius:.4rem;padding:.45rem 1rem;font-weight:600;cursor:pointer;font-size:.82rem">Remix &amp; save</button>
</div>
</div>

<!-- ---------------- test line ---------------- -->
<div id="vsSay" hidden>
<div style="display:flex;gap:.5rem;flex-wrap:wrap;align-items:center">
<select id="vsSayVoice" style="background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:.4rem;padding:.4rem .5rem;font-size:.82rem;min-width:170px"></select>
<input id="vsSayText" value="Hello, this is Lina from Medicare Marketplace. How can I help you today?" style="flex:1;min-width:220px;background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:.4rem;padding:.45rem .6rem;font-size:.85rem">
<label style="font-size:.72rem;color:#94a3b8"><input type="checkbox" id="vsSayTags"> include tags</label>
<button onclick="vsSay()" style="background:#16a34a;color:#fff;border:0;border-radius:.4rem;padding:.45rem 1.1rem;font-weight:700;cursor:pointer;font-size:.82rem">&#128266; Speak</button>
</div>
</div>

<audio id="vsAudio" controls preload="none" style="width:100%;margin-top:.7rem"></audio>
<div id="vsMsg" style="font-size:.8rem;margin-top:.4rem;min-height:1.2em;color:#7dd3fc"></div>
</div>
<script>
let VS={voices:[],active:'',designer:{},remixModes:['blend','stitch']};
function vsSay(msg,color){const m=document.getElementById('vsMsg');m.textContent=msg;m.style.color=color||'#7dd3fc';}
function vsTab(which){
 ['lib','add','design','remix','say'].forEach(k=>{
  document.getElementById('vs'+k[0].toUpperCase()+k.slice(1)).hidden=(k!==which);
  const b=document.getElementById('vsTab'+k[0].toUpperCase()+k.slice(1));
  b.style.background=(k===which)?'#0e7490':'#334155';});
}
const VS_TUNE=[['exaggeration','Expressiveness',0,1,0.05],['cfg_weight','Guidance',0,1,0.05],
 ['temperature','Variation',0.05,2,0.05],['speed','Pace',0.5,2,0.05],['pitch','Pitch',0,99,1]];
function vsEsc(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
async function vsLoad(){
 try{
  const r=await fetch('/api/voices'); const d=await r.json();
  VS.voices=d.voices||[]; VS.active=d.active_id||''; VS.designer=d.designer||{}; VS.remixModes=d.remix_modes||VS.remixModes;
  vsRenderGrid(); vsFillSelectors(); vsBuildDesigner();
  const grid=document.getElementById('vsGrid');
  if(grid && !grid.dataset.bound){grid.addEventListener('click',vsCardClick);grid.dataset.bound='1';}
  const n=VS.voices.length;
  vsSay(n?('Library: '+n+' voice'+(n===1?'':'s')+(VS.active?' — active: '+vsName(VS.active):'')):'Library is empty — add, design or remix a voice.');
 }catch(e){vsSay('Could not load the library: '+e,'#fecaca');}}
function vsName(id){const v=VS.voices.find(x=>x.id===id);return v?v.name:id;}
function vsRenderGrid(){
 const g=document.getElementById('vsGrid');
 if(!VS.voices.length){g.innerHTML='<div style="color:#64748b;font-size:.85rem">No voices yet.</div>';return;}
 g.innerHTML=VS.voices.map(v=>{
  const t=Object.entries(v.tuning||{}).map(([k,x])=>k+' '+x).join(', ');
  return '<div style="background:#0f172a;border:1px solid '+(v.active?'#22c55e':'#334155')+';border-radius:.5rem;padding:.6rem">'
   +'<div style="display:flex;justify-content:space-between;gap:.4rem;align-items:start">'
   +'<b style="font-size:.85rem;color:#e2e8f0">'+vsEsc(v.name)+'</b>'
   +(v.active?'<span style="font-size:.62rem;background:#166534;color:#dcfce7;border-radius:1rem;padding:.1rem .45rem">ACTIVE</span>':'')
   +'</div>'
   +'<div style="font-size:.72rem;color:#94a3b8;margin:.25rem 0">'+vsEsc(v.engine)+(v.model?' / '+vsEsc(v.model):'')+(v.language?' / '+vsEsc(v.language):'')
   +(v.has_reference?' / clone '+(v.duration_sec||0)+'s':' / built-in')+'</div>'
   +(t?'<div style="font-size:.7rem;color:#64748b">'+vsEsc(t)+'</div>':'')
   +(v.notes?'<div style="font-size:.7rem;color:#64748b;font-style:italic">'+vsEsc(v.notes)+'</div>':'')
   +'<div data-id="'+v.id+'" style="display:flex;gap:.35rem;flex-wrap:wrap;margin-top:.5rem">'
   +'<button data-act="preview" style="background:#334155;color:#e2e8f0;border:0;border-radius:.35rem;padding:.25rem .6rem;font-size:.72rem;cursor:pointer">&#9654; Preview</button>'
   +'<button data-act="apply" style="background:#0e7490;color:#fff;border:0;border-radius:.35rem;padding:.25rem .6rem;font-size:.72rem;cursor:pointer">Use</button>'
   +'<button data-act="rename" style="background:#334155;color:#e2e8f0;border:0;border-radius:.35rem;padding:.25rem .6rem;font-size:.72rem;cursor:pointer">Rename</button>'
   +'<button data-act="delete" style="background:#7f1d1d;color:#fee2e2;border:0;border-radius:.35rem;padding:.25rem .6rem;font-size:.72rem;cursor:pointer">Delete</button>'
   +'</div></div>';}).join('');
}
function vsCardClick(ev){
 const btn=ev.target.closest('button[data-act]'); if(!btn)return;
 const card=btn.closest('[data-id]'); if(!card)return;
 const id=card.dataset.id, act=btn.dataset.act;
 if(act==='preview')vsPreview(id);
 else if(act==='apply')vsApply(id);
 else if(act==='rename')vsRename(id);
 else if(act==='delete')vsDelete(id);
}
function vsFillSelectors(){
 const opts=VS.voices.map(v=>'<option value="'+v.id+'">'+vsEsc(v.name)+'</option>').join('');
 document.getElementById('vsSayVoice').innerHTML=opts||'<option value="">(no voices)</option>';
 if(VS.active)document.getElementById('vsSayVoice').value=VS.active;
 document.getElementById('vrSources').innerHTML=VS.voices.length?VS.voices.map(v=>
  '<label style="display:flex;gap:.4rem;align-items:center;font-size:.78rem;color:#cbd5e1;background:#0f172a;border:1px solid #334155;border-radius:.4rem;padding:.3rem .5rem">'
  +'<input type="checkbox" class="vrPick" value="'+v.id+'"'+(v.has_reference?'':' disabled title="no reference audio"')+'>'
  +vsEsc(v.name)+(v.has_reference?'':' <span style="color:#64748b">(built-in)</span>')
  +'<input type="number" class="vrW" value="1" min="0" step="0.5" style="width:3.4rem;margin-left:auto;background:#1e293b;color:#e2e8f0;border:1px solid #334155;border-radius:.3rem;padding:.1rem .3rem;font-size:.72rem"></label>').join('')
  :'<span style="font-size:.8rem;color:#64748b">Save a voice with a reference first.</span>';
}
function vsBuildDesigner(){
 const d=VS.designer;
 document.getElementById('vdPreset').innerHTML='<option value="">— custom —</option>'
  +(d.presets||[]).map(p=>'<option value="'+p.id+'">'+vsEsc(p.name)+'</option>').join('');
 const provEl=document.getElementById('vdProvider');
 if(provEl){provEl.innerHTML=vdProviderOptions();
  provEl.onchange=()=>{const p=(VS.designer.design_providers||[]).find(x=>x.id===provEl.value);
   const o=document.getElementById('vdDescribeOut');
   if(o)o.textContent=(p&&p.detail)||'';};}
 const voices=(d.backend_voices||[]);
 document.getElementById('vdControls').innerHTML=[
  '<label style="font-size:.72rem;color:#94a3b8">Engine<select id="vdEngine" style="width:100%;background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:.4rem;padding:.35rem">'
   +'<option value="chatterbox">ChatterBox</option><option value="kokoro">Local server (kokoro)</option>'
   +'<option value="piper">Local neural voices (Piper)</option></select></label>',
  '<label style="font-size:.72rem;color:#94a3b8">Model<select id="vdModel" style="width:100%;background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:.4rem;padding:.35rem">'
   +(d.chatterbox_models||[]).map(m=>'<option value="'+m+'">'+m+'</option>').join('')+'</select></label>',
  '<label style="font-size:.72rem;color:#94a3b8">Language<select id="vdLanguage" style="width:100%;background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:.4rem;padding:.35rem">'
   +Object.entries(d.chatterbox_languages||{}).map(([k,v])=>'<option value="'+k+'">'+k+' — '+vsEsc(v)+'</option>').join('')+'</select></label>',
  '<label style="font-size:.72rem;color:#94a3b8">Backend voice<select id="vdVoice" style="width:100%;background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:.4rem;padding:.35rem">'
   +vsVoiceOptions()+'</select></label>',
 ].join('')+VS_TUNE.map(([k,label,lo,hi,step])=>
  '<label style="font-size:.72rem;color:#94a3b8">'+label+' <span id="vdv_'+k+'" style="color:#7dd3fc"></span>'
  +'<input id="vd_'+k+'" data-tune="'+k+'" type="range" min="'+lo+'" max="'+hi+'" step="'+step+'" style="width:100%"></label>').join('');
 document.getElementById('vdControls').oninput=ev=>{
  const key=ev.target.dataset?ev.target.dataset.tune:null; if(!key)return;
  const lbl=document.getElementById('vdv_'+key); if(lbl)lbl.textContent=ev.target.value;};
 document.getElementById('vdTags').innerHTML=(d.chatterbox_tags||[]).map(t=>
  '<span data-tag="'+t+'" style="background:#334155;padding:.2rem .5rem;border-radius:1rem;font-size:.72rem;cursor:pointer;font-family:monospace">'+t+'</span>').join('')
  +'<input id="vdTagList" placeholder="[laugh] [sigh]" style="background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:.4rem;padding:.3rem .5rem;font-size:.78rem;min-width:11rem">';
 const dflt=Object.fromEntries(VS_TUNE.map(([k,,lo])=>[k,lo]));
 Object.entries(dflt).forEach(([k,v])=>{const el=document.getElementById('vd_'+k);if(el){el.value=v;document.getElementById('vdv_'+k).textContent=v;}});
 document.getElementById('vdEngine').onchange=vdEngineSwap;
 document.getElementById('vdTags').addEventListener('click',ev=>{
  const t=ev.target.dataset?ev.target.dataset.tag:null; if(!t)return;
  const box=document.getElementById('vdTagList');
  box.value=(box.value?box.value+' ':'' )+t; box.focus();});
 vdEngineSwap();
}
function vsVoiceOptions(){
 const d=VS.designer||{};
 const engine=document.getElementById('vdEngine').value;
 if(engine==='piper'){
  const nv=d.neural_voices||[];
  if(!nv.length)return '<option value="">(no local voices — run scripts/fetch_neural_voices.py)</option>';
  return nv.map(v=>'<option value="'+v.id+'">'+vsEsc(v.name||v.id)
   +(v.f0_hz?' — '+Math.round(v.f0_hz)+' Hz':'')+'</option>').join('');
 }
 const voices=(d.backend_voices||[]);
 return voices.length?voices.map(v=>'<option value="'+v.id+'">'+vsEsc(v.name||v.id)+'</option>').join('')
  :'<option value="af_bella">af_bella</option>';
}
function vdEngineSwap(){
 const engine=document.getElementById('vdEngine').value, isCb=engine==='chatterbox';
 document.getElementById('vdModel').parentElement.style.display=isCb?'':'none';
 document.getElementById('vdLanguage').parentElement.style.display=isCb?'':'none';
 document.getElementById('vdVoice').parentElement.style.display=isCb?'none':'';
 const sel=document.getElementById('vdVoice');
 if(!isCb&&sel){
  const keep=sel.value; sel.innerHTML=vsVoiceOptions();
  if(keep&&[...sel.options].some(o=>o.value===keep))sel.value=keep;
 }
}
function vdProviderOptions(){
 const d=VS.designer||{}, ps=d.design_providers||[];
 if(!ps.length)return '<option value="local">Local neural voices</option>';
 return ps.map(p=>'<option value="'+p.id+'"'+(p.available?'':' disabled')+'>'
  +vsEsc(p.name||p.id)+(p.available?'':' — needs a key')+'</option>').join('');
}
async function vdDescribeGo(dry){
 const description=document.getElementById('vdDescribe').value.trim();
 const out=document.getElementById('vdDescribeOut');
 if(!description){out.textContent='Describe the voice you want, e.g. "warm older British man, speaks slowly".';return;}
 const provider=document.getElementById('vdProvider').value;
 const name=document.getElementById('vdName').value.trim()||description.slice(0,40);
 out.textContent=(dry?'Measuring which voice fits…':'Creating the voice…');
 try{
  if(provider==='elevenlabs'){
   const r=await fetch('/api/voices/design/elevenlabs',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({description,name,text:vsText()})});
   const d=await r.json();
   if(!r.ok||d.error){out.textContent=d.error+(d.hint?' — '+d.hint:'');return;}
   const box=document.getElementById('vdPreviews'); box.innerHTML='';
   (d.previews||[]).forEach((p,i)=>{
    const card=document.createElement('div');
    card.style.cssText='display:flex;gap:.4rem;align-items:center;background:#0f172a;border:1px solid #334155;border-radius:.4rem;padding:.35rem .5rem';
    const au=document.createElement('audio'); au.controls=true; au.style.height='2rem';
    au.src='data:'+(p.media_type||'audio/mpeg')+';base64,'+p.audio_base64;
    const btn=document.createElement('button');
    btn.textContent='Keep #'+(i+1);
    btn.style.cssText='background:#7c3aed;color:#fff;border:0;border-radius:.4rem;padding:.3rem .7rem;font-weight:600;cursor:pointer;font-size:.75rem';
    btn.onclick=()=>vdKeepPreview(p.generated_voice_id,name,description);
    card.appendChild(au); card.appendChild(btn); box.appendChild(card);
   });
   out.textContent='Generated '+((d.previews||[]).length)+' candidate voice(s) — listen and press Keep.';
   return;
  }
  const r=await fetch('/api/voices/describe',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({description,name,dry_run:dry,tuning:null,tags:document.getElementById('vdTagList')?document.getElementById('vdTagList').value:''})});
  const d=await r.json();
  if(!r.ok||d.error){out.textContent=d.error;return;}
  const plan=d.plan||{};
  out.innerHTML='<b>'+vsEsc(plan.model_name||plan.model||'')+'</b> · pitch '+plan.pitch_level+'/99 · speed '
   +(plan.speed||1).toFixed(2)+'x · mood '+vsEsc(plan.mood||'')+'<br><span style="color:#64748b">'+vsEsc(plan.why||'')+'</span>';
  if(d.voice){
   vsSay('Created "'+d.voice.name+'" from your description. Playing it…','#86efac');
   document.getElementById('vdName').value=''; document.getElementById('vdDescribe').value='';
   await vsLoad();
   vsPreview(d.voice.id);
  }
 }catch(e){out.textContent='Could not design that voice: '+e.message;}
}
async function vdKeepPreview(preview_id,name,description){
 try{
  const r=await fetch('/api/voices/design/elevenlabs',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({description,name,generated_voice_id:preview_id,text:vsText()})});
  const d=await r.json();
  if(!r.ok||d.error)throw new Error(d.error||('HTTP '+r.status));
  vsSay('Saved "'+d.voice.name+'" (ElevenLabs '+d.info.voice_id+') to the library.','#86efac');
  document.getElementById('vdPreviews').innerHTML=''; await vsLoad();
 }catch(e){vsSay('Could not keep that voice: '+e.message,'#fecaca');}
}
function vdApplyPreset(){
 const p=(VS.designer.presets||[]).find(x=>x.id===document.getElementById('vdPreset').value);
 if(!p)return;
 Object.entries(p.tuning||{}).forEach(([k,v])=>{
  const el=document.getElementById('vd_'+k); if(!el)return;
  el.value=v; const lbl=document.getElementById('vdv_'+k); if(lbl)lbl.textContent=v;});
 vsSay('Preset applied: '+p.name);
}
function vdCollect(){
 const num=id=>{const el=document.getElementById(id);return el?parseFloat(el.value):undefined;};
 const tuning={exaggeration:num('vd_exaggeration'),cfg_weight:num('vd_cfg_weight'),
  temperature:num('vd_temperature'),speed:num('vd_speed'),pitch:num('vd_pitch')};
 return {name:document.getElementById('vdName').value.trim(),
  engine:document.getElementById('vdEngine').value,
  model:document.getElementById('vdModel').value,
  language:document.getElementById('vdLanguage').value,
  voice:document.getElementById('vdVoice').value,
  tags:document.getElementById('vdTagList').value,
  tuning};
}
async function vdPreview(){
 vsSay('Rendering a preview…');
 try{
  const r=await fetch('/api/voices/preview-draft',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify(Object.assign(vdCollect(),{text:vsText()}))});
  if(!r.ok){const d=await r.json().catch(()=>({}));throw new Error(d.error||('HTTP '+r.status));}
  const blob=await r.blob(); const eng=r.headers.get('X-Voice-Engine')||'',mode=r.headers.get('X-Voice-Mode')||'';
  const a=document.getElementById('vsAudio'); a.src=URL.createObjectURL(blob);
  try{await a.play();}catch(e){}
  vsSay('▶ preview on '+eng+(mode==='standby'?' (standby — the profile engine is unavailable here)':'')+' — '+((blob.size/1024)|0)+' KB','#86efac');
 }catch(e){vsSay('Preview failed: '+e.message,'#fecaca');}
}
async function vdSave(){
 vsSay('Saving…');
 try{
  const r=await fetch('/api/voices/design',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(vdCollect())});
  const d=await r.json();
  if(!r.ok||d.error)throw new Error(d.error||('HTTP '+r.status));
  vsSay('Saved "'+d.voice.name+'" to the library.','#86efac'); vsLoad();
 }catch(e){vsSay('Could not save: '+e.message,'#fecaca');}
}
function vsText(){return document.getElementById('vsSayText').value.trim()||'Hello, this is Lina from Medicare Marketplace. How can I help you today?';}
async function vsPreview(id){
 vsSay('Rendering "'+vsName(id)+'"…');
 await vsSpeakUrl('/api/voices/'+encodeURIComponent(id)+'/preview',{text:vsText()});
}
async function vsSay(){
 const id=document.getElementById('vsSayVoice').value;
 if(!id){vsSay('Save a voice first.','#fecaca');return;}
 vsSay('Rendering…');
 await vsSpeakUrl('/api/voices/'+encodeURIComponent(id)+'/preview',
  {text:vsText(),include_tags:document.getElementById('vsSayTags').checked});
}
async function vsSpeakUrl(url,body){
 try{
  const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if(!r.ok){const d=await r.json().catch(()=>({}));throw new Error(d.error||('HTTP '+r.status));}
  const blob=await r.blob();
  const eng=r.headers.get('X-Voice-Engine')||'',mode=r.headers.get('X-Voice-Mode')||'';
  const a=document.getElementById('vsAudio'); a.src=URL.createObjectURL(blob);
  try{await a.play();}catch(e){}
  vsSay('▶ '+(mode==='standby'?'standby ('+eng+') — the engine for this profile is unavailable here':'speaking with '+eng)+' — '+((blob.size/1024)|0)+' KB of 16 kHz audio','#86efac');
 }catch(e){vsSay('Could not render audio: '+e.message,'#fecaca');}
}
async function vsApply(id){
 try{
  const r=await fetch('/api/voices/'+encodeURIComponent(id)+'/apply',{method:'POST'});
  const d=await r.json();
  if(!r.ok||d.error)throw new Error(d.error||('HTTP '+r.status));
  const a=d.applied||{};
  vsSay('Now speaking with "'+d.voice.name+'" ('+a.engine+(a.model?' / '+a.model:'')+(a.language?' / '+a.language:'')+(a.voice?' / '+String(a.voice).split(/[\\\\/]/).pop():'')+')','#86efac');
  vsLoad();
 }catch(e){vsSay('Could not apply: '+e.message,'#fecaca');}
}
async function vsDelete(id){
 if(!confirm('Delete "'+vsName(id)+'" from the library?'))return;
 try{
  const r=await fetch('/api/voices/'+encodeURIComponent(id),{method:'DELETE'});
  const d=await r.json();
  if(!r.ok||d.error)throw new Error(d.error||('HTTP '+r.status));
  vsSay('Deleted "'+vsName(id)+'".','#fecaca'); vsLoad();
 }catch(e){vsSay('Could not delete: '+e.message,'#fecaca');}
}
async function vsRename(id){
 const name=prompt('New name:',vsName(id)); if(!name)return;
 try{
  const r=await fetch('/api/voices/'+encodeURIComponent(id),{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
  const d=await r.json();
  if(!r.ok||d.error)throw new Error(d.error||('HTTP '+r.status));
  vsSay('Renamed to "'+d.voice.name+'".','#86efac'); vsLoad();
 }catch(e){vsSay('Could not rename: '+e.message,'#fecaca');}
}
async function vsSaveCurrent(){
 const name=document.getElementById('vsSaveName').value.trim();
 if(!name){vsSay('Give the snapshot a name first.','#fecaca');return;}
 try{
  const r=await fetch('/api/voices/current',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
  const d=await r.json();
  if(!r.ok||d.error)throw new Error(d.error||('HTTP '+r.status));
  vsSay('Saved the current voice as "'+d.voice.name+'" ('+d.voice.source+').','#86efac');
  document.getElementById('vsSaveName').value=''; vsLoad();
 }catch(e){vsSay('Could not save: '+e.message,'#fecaca');}
}
function vsAddMeta(){
 // When matching is on, the matcher picks the engine — sending one would override
 // it, so the engine select is simply left out of the payload.
 const match=!!(document.getElementById('vsMatch')||{}).checked;
 const meta={engine:document.getElementById('vsAddEngine').value};
 if(match){meta.match=true; delete meta.engine;}
 return meta;
}
function vsMatchNote(d){
 if(!d.match)return '';
 const m=d.match;
 return ' Matched to '+m.model+' (~'+Math.round(m.voice_stats.f0_hz)+' Hz, pitch '
  +m.pitch_level+'/99, '+(m.speed).toFixed(2)+'x, confidence '+(m.confidence*100).toFixed(0)+'%).';
}
async function vsCheckMatch(){
 const f=document.getElementById('vsFile').files[0];
 const path=document.getElementById('vsPath').value.trim();
 const out=document.getElementById('vsMatchOut');
 const body={};
 if(f){body.data_base64=await new Promise((res,rej)=>{const fr=new FileReader();fr.onload=()=>res(fr.result);fr.onerror=rej;fr.readAsDataURL(f);});}
 else if(path){body.path=path;}
 else{out.textContent='Pick a file or enter a server path first.';return;}
 out.textContent='measuring…';
 try{
  const r=await fetch('/api/voices/match',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const d=await r.json();
  if(!r.ok||d.error)throw new Error(d.error||('HTTP '+r.status));
  const m=d.match;
  out.innerHTML='closest: <b>'+vsEsc(m.model)+'</b> — pitch '+m.pitch_level+'/99, '+(m.speed).toFixed(2)
   +'x, confidence '+(m.confidence*100).toFixed(0)+'%. <span style="color:#64748b">'+vsEsc(m.why)+'</span>';
 }catch(e){out.textContent='match failed: '+e.message;}
}
async function vsUploadFile(){
 const f=document.getElementById('vsFile').files[0];
 if(!f){vsSay('Choose a file first.','#fecaca');return;}
 const name=document.getElementById('vsAddName').value.trim();
 if(!name){vsSay('Give the voice a name first.','#fecaca');return;}
 vsSay('Uploading '+f.name+'…');
 try{
  const data_base64=await new Promise((res,rej)=>{const fr=new FileReader();fr.onload=()=>res(fr.result);fr.onerror=rej;fr.readAsDataURL(f);});
  const r=await fetch('/api/voices/upload',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify(Object.assign(vsAddMeta(),{name,filename:f.name,data_base64}))});
  const d=await r.json();
  if(!r.ok||d.error)throw new Error(d.error||('HTTP '+r.status));
  vsSay('Added "'+d.voice.name+'" — '+(d.voice.duration_sec||0)+'s reference.'+vsMatchNote(d),'#86efac');
  document.getElementById('vsAddName').value=''; document.getElementById('vsFile').value=''; vsLoad();
 }catch(e){vsSay('Upload failed: '+e.message,'#fecaca');}
}
async function vsImportPath(){
 const path=document.getElementById('vsPath').value.trim();
 const name=document.getElementById('vsAddName').value.trim()||(path.split(/[\\\\/]/).pop()||'').replace(/\\.[a-z0-9]+$/i,'');
 if(!path){vsSay('Enter a path to a wav file on the server.','#fecaca');return;}
 try{
  const r=await fetch('/api/voices/upload',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(Object.assign(vsAddMeta(),{name,path}))});
  const d=await r.json();
  if(!r.ok||d.error)throw new Error(d.error||('HTTP '+r.status));
  vsSay('Imported "'+d.voice.name+'" — '+(d.voice.duration_sec||0)+'s reference.'+vsMatchNote(d),'#86efac');
  document.getElementById('vsPath').value=''; document.getElementById('vsAddName').value=''; vsLoad();
 }catch(e){vsSay('Import failed: '+e.message,'#fecaca');}
}
let vsRecorder=null;
async function vsRecord(){
 const btn=document.getElementById('vsRecBtn'), meter=document.getElementById('vsRecMeter');
 if(vsRecorder){vsRecorder.stop();return;}
 const name=document.getElementById('vsAddName').value.trim();
 if(!name){vsSay('Give the voice a name before recording.','#fecaca');return;}
 const secs=parseInt(document.getElementById('vsRecSecs').value,10);
 let stream;
 try{stream=await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:true,noiseSuppression:true}});}
 catch(e){vsSay('Microphone unavailable: '+(e.message||e.name),'#fecaca');return;}
 const ctx=new (window.AudioContext||window.webkitAudioContext)({sampleRate:16000});
 const src=ctx.createMediaStreamSource(stream);
 const proc=ctx.createScriptProcessor(4096,1,1);
 const parts=[]; let loudest=0;
 proc.onaudioprocess=e=>{
  const f=e.inputBuffer.getChannelData(0);
  const i16=new Int16Array(f.length);
  for(let i=0;i<f.length;i++){const v=Math.max(-1,Math.min(1,f[i]));i16[i]=v<0?v*32768:v*32767;if(Math.abs(v)>loudest)loudest=Math.abs(v);}
  parts.push(i16);
  meter.textContent=Math.max(0,loudest*100).toFixed(0)+'%';
 };
 src.connect(proc); proc.connect(ctx.destination);
 const chunks=[];
 vsRecorder={
  stop:async()=>{
   try{proc.disconnect();src.disconnect();}catch(e){}
   stream.getTracks().forEach(t=>t.stop());
   try{proc.onaudioprocess=null; await ctx.close();}catch(e){}
   vsRecorder=null; btn.textContent='⏺ Record & add'; meter.textContent='';
   let total=0; parts.forEach(p=>total+=p.length);
   const all=new Int16Array(total); let off=0; parts.forEach(p=>{all.set(p,off);off+=p.length;});
   if(loudest<0.01){vsSay('That recording was silent — check the mic and try again.','#fecaca');return;}
   const bytes=new Uint8Array(all.buffer);
   let bin=''; for(let i=0;i<bytes.length;i++)bin+=String.fromCharCode(bytes[i]);
   const pcm_base64=btoa(bin);
   vsSay('Uploading '+secs+'s recording…');
   try{
    const r=await fetch('/api/voices/upload',{method:'POST',headers:{'Content-Type':'application/json'},
     body:JSON.stringify(Object.assign(vsAddMeta(),{name,pcm_base64,sample_rate:16000}))});
    const d=await r.json();
    if(!r.ok||d.error)throw new Error(d.error||('HTTP '+r.status));
    vsSay('Recorded and saved "'+d.voice.name+'" — '+(d.voice.duration_sec||0)+'s reference.'+vsMatchNote(d),'#86efac');
    document.getElementById('vsAddName').value=''; vsLoad();
   }catch(e){vsSay('Could not save the recording: '+e.message,'#fecaca');}
  }};
 btn.textContent='⏹ Stop ('+secs+'s max)';
 vsSay('Recording… speak naturally, then press Stop (auto-stops at '+secs+'s).');
 setTimeout(()=>{if(vsRecorder)vsRecorder.stop();},secs*1000);
}
async function vrSave(){
 const name=document.getElementById('vrName').value.trim();
 const picks=[...document.querySelectorAll('.vrPick:checked')].map(cb=>({
  id:cb.value, weight:parseFloat(cb.parentElement.querySelector('.vrW').value)||1}));
 if(!name){vsSay('Give the remix a name first.','#fecaca');return;}
 if(picks.length<1){vsSay('Tick at least one voice to remix.','#fecaca');return;}
 try{
  const r=await fetch('/api/voices/remix',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({name,sources:picks,mode:document.getElementById('vrMode').value,
    pitch_semitones:parseFloat(document.getElementById('vrPitch').value)||0,
    match:!!(document.getElementById('vrNeural')||{}).checked})});
  const d=await r.json();
  if(!r.ok||d.error)throw new Error(d.error||('HTTP '+r.status));
  vsSay('Remixed into "'+d.voice.name+'" ('+d.voice.duration_sec+'s). '+d.voice.notes,'#86efac');
  document.getElementById('vrName').value=''; vsLoad();
 }catch(e){vsSay('Remix failed: '+e.message,'#fecaca');}
}
vsLoad();
</script>
<h2 style="margin-top:1.6rem;color:#38bdf8;font-size:1rem" id="cbHeading" hidden>&#127925; ChatterBox voice control</h2>
<div id="cbPanel" style="background:#1e293b;border:1px solid #334155;border-radius:.5rem;padding:1rem;margin:.6rem 0 1.4rem" hidden>
<div id="cbInfo" style="font-size:.8rem;color:#94a3b8;margin-bottom:.7rem">Loading ChatterBox status&hellip;</div>
<div style="display:flex;gap:.6rem;flex-wrap:wrap;align-items:center;margin-bottom:.7rem">
<label style="font-size:.7rem;color:#64748b;text-transform:uppercase;letter-spacing:.05em">Model</label>
<select id="cbModel" style="background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:.4rem;padding:.4rem .5rem;font-size:.85rem"></select>
<button id="cbModelBtn" style="background:#0e7490;color:#fff;border:0;border-radius:.4rem;padding:.45rem 1.1rem;font-weight:600;cursor:pointer;font-size:.85rem">Switch model</button>
<label id="cbLangLabel" style="font-size:.7rem;color:#64748b;text-transform:uppercase;letter-spacing:.05em;display:none">Language</label>
<select id="cbLang" style="background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:.4rem;padding:.4rem .5rem;font-size:.85rem;display:none"></select>
<button id="cbLangBtn" style="background:#0e7490;color:#fff;border:0;border-radius:.4rem;padding:.45rem 1.1rem;font-weight:600;cursor:pointer;font-size:.85rem;display:none">Set</button>
</div>
<div style="display:flex;gap:.6rem;flex-wrap:wrap;align-items:center;margin-bottom:.7rem">
<label style="font-size:.7rem;color:#64748b;text-transform:uppercase;letter-spacing:.05em">Voice clone reference (wav)</label>
<input id="cbVoice" placeholder="C:/voices/agent_ref.wav" style="flex:1;min-width:220px;background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:.4rem;padding:.4rem .6rem;font-size:.85rem">
<button id="cbVoiceBtn" style="background:#0e7490;color:#fff;border:0;border-radius:.4rem;padding:.45rem 1.1rem;font-weight:600;cursor:pointer;font-size:.85rem">Clone voice</button>
</div>
<div id="cbTags" style="margin-bottom:.7rem" hidden>
<label style="font-size:.7rem;color:#64748b;text-transform:uppercase;letter-spacing:.05em">Paralinguistic tags</label><br>
<span style="font-size:.8rem;color:#94a3b8">Click to insert into the test line:</span>
<div id="cbChips" style="display:flex;gap:.4rem;flex-wrap:wrap;margin-top:.4rem"></div>
</div>
<div style="display:flex;gap:.6rem;flex-wrap:wrap;align-items:center">
<input id="cbText" value="Hello, I'm Lina from Medicare Marketplace. How can I help you?" style="flex:1;min-width:240px;background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:.4rem;padding:.45rem .6rem;font-size:.85rem">
<button id="cbSpeakBtn" style="background:#16a34a;color:#fff;border:0;border-radius:.4rem;padding:.45rem 1.1rem;font-weight:700;cursor:pointer;font-size:.85rem">&#128266; Test voice</button>
</div>
<audio id="cbAudio" controls preload="none" style="width:100%;margin-top:.6rem"></audio>
<div id="cbMsg" style="font-size:.8rem;margin-top:.5rem;min-height:1.2em;color:#7dd3fc"></div>
</div>
<script>
const CB_TAGS=['[laugh]','[chuckle]','[cough]','[sigh]','[gasp]','[yawn]','[um]'];
function cbSay(msg,color){const m=document.getElementById('cbMsg');m.textContent=msg;m.style.color=color||'#7dd3fc';}
async function cbLoad(){
 try{
  const r=await fetch('/api/tts/voices');const d=await r.json();
  // Show the panel when ChatterBox is the configured engine OR currently active.
  // Using only `provider` (the *active* engine) hid the panel after a failover,
  // leaving the operator unable to see status or switch models.
  if((d.provider||'')!=='chatterbox' && (d.primary||'')!=='chatterbox'){document.getElementById('cbPanel').hidden=true;document.getElementById('cbHeading').hidden=true;return;}
  document.getElementById('cbPanel').hidden=false;document.getElementById('cbHeading').hidden=false;
  const info=d.model_info||{};
  if(d.failover_reason) cbSay('\u26a0 '+d.failover_reason,'#fecaca');
  const parts=[info.description||'ChatterBox'];
  parts.push((info.languages||[]).length + ' language' + (((info.languages||[]).length>1)?'s':''));
  if(info.paralinguistic_tags)parts.push('paralinguistic tags');
  parts.push(info.voice_cloned?'voice cloned':(info.voice_path?'voice reference INVALID':'built-in voice'));
  parts.push('out '+(info.output_rate/1000)+'kHz');
  document.getElementById('cbInfo').textContent=parts.join(' · ');
  if(info.voice_reference_problem){
   cbSay('\u26a0 '+info.voice_reference_problem,'#fecaca');
  } else if(info.voice_cloned){
   cbSay('Voice clone reference: '+info.voice_path,'#86efac');
  }
  const models=d.available_models||info.available_models||[];
  const sel=document.getElementById('cbModel');
  sel.innerHTML=models.map(m=>'<option value="'+m+'"'+(m===info.model?' selected':'')+'>'+m+'</option>').join('');
  const allLangs=info.available_languages||{};
  const langs=info.multilingual?Object.keys(allLangs):(info.languages||[]);
  const langSel=document.getElementById('cbLang');
  const multi=langs.length>1;
  document.getElementById('cbLangLabel').style.display=multi?'':'none';
  document.getElementById('cbLangBtn').style.display=multi?'':'none';
  langSel.style.display=multi?'':'none';
  langSel.innerHTML=langs.map(l=>'<option value="'+l+'"'+(l===info.language?' selected':'')+'>'+l+(allLangs[l]?' \u2014 '+allLangs[l]:'')+'</option>').join('');
  document.getElementById('cbTags').hidden=!info.paralinguistic_tags;
  document.getElementById('cbChips').innerHTML=CB_TAGS.map(t=>
    '<span class="btn" onclick="cbInsert(this.textContent)" style="background:#334155;padding:.25rem .6rem;border-radius:1rem;font-size:.75rem;cursor:pointer;font-family:monospace">'+t+'</span>').join('');
 }catch(e){cbSay('Could not load ChatterBox status: '+e,'#fecaca');}}
function cbInsert(t){const el=document.getElementById('cbText');el.value=el.value.trimEnd()+' '+t;el.focus();}
async function cbPost(body){
 const r=await fetch('/api/tts/switch-voice',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
 const d=await r.json().catch(()=>({error:'bad response'}));
 if(!r.ok||d.error)throw new Error(d.error||('HTTP '+r.status));
 return d;}
document.getElementById('cbModelBtn').onclick=async()=>{
 try{cbSay('Switching model…');const d=await cbPost({model:document.getElementById('cbModel').value});
  if(d.warning)cbSay('⚠ '+d.warning,'#fecaca');
  else cbSay('Switched to ChatterBox-'+d.applied.model+' — loading weights on first use.','#86efac');
  cbLoad();}
 catch(e){cbSay('Model switch failed: '+e.message,'#fecaca');}};
document.getElementById('cbLangBtn').onclick=async()=>{
 try{cbSay('Setting language…');const d=await cbPost({language:document.getElementById('cbLang').value});
  cbSay('Language set to '+d.applied.language+'.','#86efac');cbLoad();}
 catch(e){cbSay('Language change failed: '+e.message,'#fecaca');}};
document.getElementById('cbVoiceBtn').onclick=async()=>{
 const v=document.getElementById('cbVoice').value.trim();
 if(!v){cbSay('Enter a .wav path to clone (or clear it to use the built-in voice).','#fecaca');return;}
 try{const d=await cbPost({voice:v});cbSay('Voice clone reference set: '+d.applied.voice,'#86efac');cbLoad();}
 catch(e){cbSay('Voice clone failed: '+e.message,'#fecaca');}};
document.getElementById('cbSpeakBtn').onclick=async()=>{
 const text=document.getElementById('cbText').value.trim();
 if(!text){cbSay('Enter some text first.','#fecaca');return;}
 cbSay('Generating audio…');
 try{
  const r=await fetch('/api/debug/speak',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text})});
  if(!r.ok){const d=await r.json().catch(()=>({}));throw new Error(d.error||('HTTP '+r.status));}
  const blob=await r.blob();
  const a=document.getElementById('cbAudio');
  a.src=URL.createObjectURL(blob);
  // Play straight away — the click that got us here is the required gesture,
  // so waiting for a second click was pointless (and looked like "no audio").
  const secs=await new Promise(res=>{a.onloadedmetadata=()=>res(a.duration);setTimeout(()=>res(null),2500);});
  try{
   await a.play();
   cbSay('▶ playing '+(secs?secs.toFixed(1)+'s':'')+' of audio ('+(blob.size/1024).toFixed(0)+' KB). If you still hear nothing, check your system output device and the tab volume.','#86efac');
  }catch(perr){
   cbSay('Audio ready — press ▶ on the player below ('+(perr.message||'the browser blocked autoplay')+').','#fecaca');
  }
 }catch(e){cbSay('TTS failed: '+e.message+' (model weights may still be downloading, or disk space is short).','#fecaca');}};
cbLoad();</script>

<script>function esc2(s){return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
async function loadRecs(){
try{
const r=await fetch('/api/recordings');const d=await r.json();const box=document.getElementById('recordings');
const recs=d.recordings||[];
if(!recs.length){box.innerHTML='<p style="color:#64748b">No recordings yet — place a call from the <a href="/debug">Voice Console</a>.</p>';return;}
box.innerHTML=recs.map(x=>{
const turns=x.transcript||[];
const body=turns.map(t=>`<div style="margin:.15rem 0"><b style="color:${t.role==='ai'?'#7dd3fc':'#a5f3fc'}">${t.role==='ai'?'Lina':'Caller'}:</b> ${esc2(t.text)}</div>`).join('');
const ok=x.disposition==='AI-TRANSFER';
return `<div style="background:#1e293b;border:1px solid #334155;border-radius:.5rem;padding:.8rem;margin:.7rem 0">
<div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:.5rem;align-items:center">
<div style="font-size:.85rem"><b>${esc2((x.started||'').replace('T',' ').slice(0,19))} UTC</b> &middot; ${x.duration_sec}s &middot;
<span class="tag ${ok?'ok':'hangup'}">${esc2(x.disposition||'-')}</span> &middot; ${esc2(x.campaign_name||x.campaign||'')}${x.age?' &middot; age '+x.age:''}</div>
<div style="font-size:.8rem"><a href="/api/recordings/${x.id}" target="_blank">JSON</a></div>
</div>
<audio controls preload="none" style="width:100%;margin-top:.5rem" src="/recordings/${x.id}"></audio>
<details style="margin-top:.4rem"><summary style="cursor:pointer;color:#94a3b8;font-size:.8rem">Transcript (${turns.length} turns)</summary>
<div style="margin-top:.4rem;font-size:.85rem">${body}</div></details>
</div>`;
}).join('');
}catch(e){document.getElementById('recordings').innerHTML='<p style="color:#64748b">Could not load recordings.</p>';}}
loadRecs();</script>
<script>async function load(){
const [cr,cl]=await Promise.all([fetch('/api/config'),fetch('/api/calls')]);
const c=await cr.json();const d=await cl.json();
document.getElementById('mode').textContent=c.mode||'turn-based';
document.getElementById('tts').textContent=c.tts_active||c.tts_provider||'console';
document.getElementById('voice').textContent=c.tts_provider==='chatterbox'?('ChatterBox/'+(c.chatterbox_model||'turbo')):(c.elevenlabs_voice||c.kokoro_voice||c.tts_provider||'-');
document.getElementById('stt').textContent=c.stt_provider||'console';
document.getElementById('llm').textContent=c.llm_provider||'-';
document.getElementById('status').innerHTML='<span class="badge live">LIVE</span>';
const tb=document.querySelector('#t tbody');tb.innerHTML='';for(const x of d.calls){const f=x.facts||{};
const tag=x.action==='transfer'?'tag ok':'tag hangup';
tb.insertAdjacentHTML('beforeend',`<tr><td>${x.ts}</td><td>${x.lead_id||'-'}</td>
<td><span class='${tag}'>${x.action}</span></td><td>${x.disposition}</td>
<td>${f.age??'-'}</td></tr>`)}}
load();setInterval(load,4000);</script></body></html>
"""


DEBUG_CONSOLE = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Live Voice Call — talk to Lina</title>
<style>
body{font-family:system-ui;margin:0;background:#0f172a;color:#e2e8f0}
header{background:#1e293b;padding:1rem 2rem;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:.5rem}
header h1{margin:0;font-size:1.2rem;color:#38bdf8}
header a{color:#38bdf8;text-decoration:none;font-weight:600}
main{max-width:920px;margin:0 auto;padding:1.5rem}
#chat{background:#1e293b;border-radius:.75rem;padding:1rem;min-height:280px;max-height:44vh;overflow-y:auto;margin-bottom:1rem;display:flex;flex-direction:column}
.msg{margin:.6rem 0;padding:.6rem .9rem;border-radius:.6rem;max-width:82%;line-height:1.4}
.msg.agent{background:#0e7490}
.msg.caller{background:#334155;margin-left:auto}
.msg.sys{background:transparent;border:1px dashed #475569;color:#94a3b8;font-size:.8rem;max-width:100%}
.msg.partial{background:#334155;margin-left:auto;opacity:.6;font-style:italic}
.row{display:flex;gap:.5rem;margin-bottom:1rem;flex-wrap:wrap}
.big{padding:1rem 1.6rem;font-size:1.05rem;border-radius:.6rem;border:0;font-weight:700;cursor:pointer}
.big.start{background:#16a34a;color:#fff}.big.start:hover{background:#15803d}
.big.stop{background:#dc2626;color:#fff}.big.stop:hover{background:#b91c1c}
.big:disabled{background:#475569;cursor:default}
#status{display:flex;gap:1rem;flex-wrap:wrap;margin-bottom:1rem}
.card{background:#1e293b;padding:.7rem 1.1rem;border-radius:.5rem;font-size:.85rem;min-width:130px}
.card .label{font-size:.65rem;text-transform:uppercase;color:#64748b;letter-spacing:.05em}
.card .val{font-weight:700;margin-top:.15rem}
.badge{display:inline-block;padding:.2rem .6rem;border-radius:1rem;font-size:.72rem;font-weight:600}
.badge.on{background:#166534;color:#bbf7d0}.badge.off{background:#991b1b;color:#fecaca}
.badge.warn{background:#854d0e;color:#fef08a}
#panel{background:#1e293b;border-radius:.75rem;padding:1rem;margin-top:1rem;font-size:.8rem}
#panel h3{margin:.2rem 0 .6rem;color:#94a3b8;font-size:.75rem;text-transform:uppercase;letter-spacing:.05em}
#panel pre{background:#0f172a;padding:.7rem;border-radius:.5rem;overflow:auto;color:#a5f3fc;white-space:pre-wrap;max-height:200px}
.meter{height:8px;background:#334155;border-radius:4px;overflow:hidden;margin-top:.4rem;min-width:110px}
.meter>div{height:100%;width:0;background:#22c55e;transition:width .08s}
.meter.speaking>div{background:#f59e0b}
.pills{display:flex;gap:.5rem;flex-wrap:wrap;margin:.6rem 0}
.pill{background:#334155;padding:.3rem .7rem;border-radius:1rem;font-size:.75rem;cursor:pointer}
.pill:hover{background:#475569}
#text{flex:1;padding:.7rem;border-radius:.5rem;border:1px solid #334155;background:#0f172a;color:#e2e8f0;font-size:1rem}
</style></head><body>
<header><h1>&#127908; Live Voice Call — Lina</h1><div><a href="/">&#8592; Dashboard</a> &nbsp; <span class="badge on">LIVE VOICE</span> &nbsp; <span id="llmBadge" class="badge warn">brain: ...</span></div></header>
<main>
<div id="status">
  <div class="card"><div class="label">Connection</div><div class="val" id="conn">offline</div></div>
  <div class="card"><div class="label">State</div><div class="val" id="state">idle</div></div>
  <div class="card"><div class="label">Your mic level</div><div class="val"><div class="meter" id="micMeter"><div></div></div></div></div>
  <div class="card"><div class="label">Lina speaking</div><div class="val"><div class="meter speaking" id="spkMeter"><div></div></div></div></div>
</div>
<div class="row">
  <button class="big start" id="startBtn" onclick="startCall()">&#127908; Start live call</button>
  <button class="big stop" id="endBtn" onclick="endCall()" disabled>&#128308; End call</button>
</div>
<div id="chat"></div>
<div class="pills">
  <span class="pill" onclick="sendText('yes')">"yes"</span>
  <span class="pill" onclick="sendText('no')">"no"</span>
  <span class="pill" onclick="sendText('maybe, I think so')">"maybe"</span>
  <span class="pill" onclick="sendText('what do you mean by medicare?')">"what do you mean?"</span>
  <span class="pill" onclick="sendText('I have private insurance only')">"private insurance"</span>
  <span class="pill" onclick="sendText('none of your business')">refuse</span>
  <span class="pill" onclick="sendText('I am 67 years old')">"I'm 67"</span>
  <span class="pill" onclick="sendText('I am 35 years old')">"I'm 35" (too young)</span>
  <span class="pill" onclick="sendText('take me off your list')">DNC</span>
</div>
<div class="row">
  <input id="text" placeholder="Type instead (works without speaking)..." autocomplete="off"/>
  <button class="big" style="background:#0284c7;color:#fff;padding:.7rem 1.2rem;font-size:.95rem" onclick="sendText()">Send</button>
</div>
<div id="panel"><h3>Live state</h3><pre id="dbg">Click "Start live call" — Lina speaks, and you can talk back OR interrupt her mid-sentence (barge-in).</pre></div>
</main>
<script>
let ws=null, capCtx=null, playCtx=null, micStream=null, processor=null, source=null;
let activeSources=[], nextPlayTime=0, speaking=false, listening=false;
let bargeOk=0;
const chat=document.getElementById('chat'), dbg=document.getElementById('dbg');
const SAMPLE_RATE=16000;

function bubble(cls,html){const d=document.createElement('div');d.className='msg '+cls;d.innerHTML=html;chat.appendChild(d);chat.scrollTop=chat.scrollHeight;return d;}
function esc(s){return (s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function setConn(t,c){const e=document.getElementById('conn');e.textContent=t;e.className='val '+(c||'');}
function setState(t){document.getElementById('state').textContent=t;}
// Any click unlocks Web Audio — the reliable way out of a strict autoplay block.
document.addEventListener('click',()=>{if(playCtx&&playCtx.state!=='running')playCtx.resume().catch(()=>{});},{passive:true});

function playPCM(int16){
    if(!playCtx) return;
    if(playCtx.state!=='running') playCtx.resume().catch(()=>{});
    const f=new Float32Array(int16.length);
    for(let i=0;i<int16.length;i++) f[i]=int16[i]/32768;
    const buf=playCtx.createBuffer(1,f.length,SAMPLE_RATE);
    buf.getChannelData(0).set(f);
    const s=playCtx.createBufferSource();
    s.buffer=buf; s.connect(playCtx.destination);
    const start=Math.max(playCtx.currentTime,nextPlayTime);
    s.start(start);
    nextPlayTime=start+buf.duration;
    activeSources.push(s);
    s.onended=()=>{activeSources=activeSources.filter(x=>x!==s);};
    speaking=true; updateMeter(true);
}
function stopPlayback(){
    activeSources.forEach(s=>{try{s.stop()}catch(e){}});
    activeSources=[]; nextPlayTime=playCtx?playCtx.currentTime:0; speaking=false; updateMeter(false);
}
function updateMeter(on){
    document.getElementById('spkMeter').firstElementChild.style.width=on?'100%':'0%';
}

// Resample captured audio to the 16 kHz Deepgram expects (browsers that ignore
// our requested sample rate would otherwise send audio at the wrong speed).
function resampleLinear(f32, fromRate, toRate){
    if(fromRate===toRate) return f32;
    const ratio=fromRate/toRate;
    const out=new Float32Array(Math.max(1,Math.round(f32.length/ratio)));
    for(let i=0;i<out.length;i++){
        const pos=i*ratio, i0=Math.floor(pos), i1=Math.min(i0+1,f32.length-1), frac=pos-i0;
        out[i]=f32[i0]*(1-frac)+f32[i1]*frac;
    }
    return out;
}

async function startMic(){
    // 1. Make sure a microphone actually exists before asking for it.
    let devices=[];
    try{ devices=await navigator.mediaDevices.enumerateDevices(); }catch(e){}
    if(devices.length && !devices.some(d=>d.kind==='audioinput')){
        const e=new Error('No microphone detected on this device.');
        e.name='NotFoundError'; throw e;
    }
    // 2. Ask for the mic, relaxing constraints if the exact set isn't supported.
    const attempts=[
        {audio:{channelCount:1,echoCancellation:true,noiseSuppression:true,autoGainControl:true}},
        {audio:{echoCancellation:true}},
        {audio:true}
    ];
    let stream=null, err=null;
    for(const c of attempts){
        try{ stream=await navigator.mediaDevices.getUserMedia(c); break; }
        catch(e){ err=e; if(e&&(e.name==='NotAllowedError'||e.name==='SecurityError')) throw e; }
    }
    if(!stream) throw (err||new Error('Could not open the microphone.'));
    micStream=stream;

    // 3. Capture context at 16 kHz. If the browser ignores it, resample per block.
    capCtx=new (window.AudioContext||window.webkitAudioContext)({sampleRate:SAMPLE_RATE});
    const ctxRate=capCtx.sampleRate||SAMPLE_RATE;
    source=capCtx.createMediaStreamSource(micStream);
    processor=capCtx.createScriptProcessor(4096,1,1);
    source.connect(processor); processor.connect(capCtx.destination);
    processor.onaudioprocess=(e)=>{
        let f=e.inputBuffer.getChannelData(0);
        if(ctxRate!==SAMPLE_RATE) f=resampleLinear(f, ctxRate, SAMPLE_RATE);
        let sum=0; for(let i=0;i<f.length;i++) sum+=f[i]*f[i];
        const rms=Math.sqrt(sum/f.length);
        document.getElementById('micMeter').firstElementChild.style.width=Math.min(100,rms*400)+'%';
        const pcm=new Int16Array(f.length);
        for(let i=0;i<f.length;i++){const s=Math.max(-1,Math.min(1,f[i]));pcm[i]=s<0?s*0x8000:s*0x7FFF;}
        if(ws&&ws.readyState===1) ws.send(pcm.buffer);
        const now=Date.now();
        if(speaking && rms>0.02 && now>bargeOk){
            bargeOk=now+900;
            if(ws&&ws.readyState===1) ws.send(JSON.stringify({type:'barge'}));
            stopPlayback();
            bubble('sys','&#9995; <b>You interrupted Lina</b> (barge-in) — she stopped talking and is now listening.');
        }
    };
}

function startCall(){
    if(!window.isSecureContext && location.hostname!=='localhost' && location.hostname!=='127.0.0.1'){
        bubble('sys','&#9888; mic requires localhost or HTTPS');return;
    }
    chat.innerHTML='';
    const proto=location.protocol==='https:'?'wss':'ws';
    ws=new WebSocket(proto+'://'+location.host+'/ws/voice');
    setConn('connecting...','warn');
    ws.onopen=async()=>{
        setConn('connected','on'); setState('starting');
        document.getElementById('startBtn').disabled=true;
        document.getElementById('endBtn').disabled=false;
        playCtx=new (window.AudioContext||window.webkitAudioContext)({sampleRate:SAMPLE_RATE});
        // Autoplay policy: an AudioContext created in a callback (not a click)
        // starts "suspended", which plays the streamed frames *silently* — the
        // call looked dead even when the audio was arriving. Resume it and say
        // so if the browser still refuses.
        if(playCtx.state!=='running'){try{await playCtx.resume();}catch(e){}}
        if(playCtx.state!=='running')
            bubble('sys','&#128263; <b>Audio is muted by the browser.</b> Click anywhere on this page to allow sound, then start the call again.');
        bubble('sys','&#128251; Live call started. Lina is speaking — just talk to interrupt her (barge-in).');
        try{
            await startMic();
            bubble('sys','&#127908; Microphone live — go ahead and speak.');
        }catch(err){
            let msg=esc(err.message||'unknown error');
            if(err.name==='NotAllowedError'||err.name==='SecurityError')
                msg='<b>Microphone permission denied.</b> Click the &#127908; icon in the address bar, choose <b>Allow</b>, then Start the call again.';
            else if(err.name==='NotFoundError')
                msg='<b>No microphone found.</b> Plug in or enable a mic (Windows Sound settings) and use Chrome.';
            else if(err.name==='NotReadableError')
                msg='<b>Microphone is in use by another app.</b> Close it (e.g. Teams/Zoom) and Start again.';
            bubble('sys','&#10060; '+msg+' You can still type below.');
        }
    };
    ws.onmessage=(m)=>handle(JSON.parse(m.data));
    ws.onclose=()=>{setConn('offline','off');setState('ended');resetBtns();stopPlayback();};
    ws.onerror=()=>{setConn('error','off');};
}
function endCall(){ if(ws) ws.close(); resetBtns(); stopPlayback(); }
function resetBtns(){document.getElementById('startBtn').disabled=false;document.getElementById('endBtn').disabled=true;}

function handle(m){
    if(m.type==='agent_say'){ bubble('agent','<b>Lina:</b> '+esc(m.text)); dbg.textContent=JSON.stringify(m,null,1); }
    else if(m.type==='audio'){ const i16=base64ToInt16(m.data); if(i16) playPCM(i16); }
    else if(m.type==='speech_start'){ speaking=true; setState('Lina speaking...'); updateMeter(true); }
    else if(m.type==='speech_end'){ speaking=false; updateMeter(false); setState('listening'); }
    else if(m.type==='speech_cancel'){ stopPlayback(); setState('interrupted'); }
    else if(m.type==='listening'){ listening=m.state==='on'; setState(listening?'listening — go ahead':'thinking...'); }
    else if(m.type==='transcript'){
        let el=document.getElementById('partial');
        if(!m.final){ if(!el){el=bubble('partial','<b>You:</b> '+esc(m.text));el.id='partial';} else el.innerHTML='<b>You:</b> '+esc(m.text); }
        else { if(el) el.remove(); if(m.text) bubble('caller','<b>You:</b> '+esc(m.text)); }
    }
    else if(m.type==='result'){
        const t=m.action==='transfer'?'&#128222; TRANSFERRED to licensed agent':'&#128308; call ended';
        bubble('sys',t+' &nbsp; disposition=<b>'+esc(m.disposition||'')+'</b>');
        setState('ended'); dbg.textContent=JSON.stringify(m,null,1);
    }
    else if(m.type==='recording_saved'){
        bubble('sys','&#127909; <b>Recording saved</b> ('+(m.duration_sec||0)+'s) &rarr; <a href="/recordings/'+esc(m.id)+'" target="_blank" style="color:#7dd3fc">play it</a> &middot; <a href="/#recordings" style="color:#7dd3fc">all recordings</a>');
    }
    else if(m.type==='notice'){ bubble('sys','&#8505;&#65039; '+esc(m.message)); }
    else if(m.type==='error'){ bubble('sys','&#10060; '+esc(m.message)); }
}
function base64ToInt16(b64){
    try{
        const bin=atob(b64); const n=bin.length; const out=new Int16Array(n>>1);
        for(let i=0,j=0;j<out.length;i+=2,j++) out[j]=(bin.charCodeAt(i)|(bin.charCodeAt(i+1)<<8));
        return out;
    }catch(e){return null;}
}
function sendText(t){
    const inp=document.getElementById('text');
    const text=(t!==undefined?t:inp.value).trim(); if(!text) return; inp.value='';
    bubble('caller','<b>You:</b> '+esc(text)+' <span style="opacity:.5">(typed)</span>');
    if(ws&&ws.readyState===1) ws.send(JSON.stringify({type:'text',text}));
    else bubble('sys','&#9888; Call not active — click "Start live call" first.');
}
document.getElementById('text').addEventListener('keydown',e=>{if(e.key==='Enter')sendText();});
fetch('/api/debug/voice').then(r=>r.json()).then(v=>{
    const b=document.getElementById('llmBadge');
    b.textContent='brain: '+(v.llm||'?');
    if(v.warning) bubble('sys','&#9888; '+v.warning);
    else if(!v.realtime) bubble('sys','&#9888; real-time voice unavailable — see /api/debug/voice');
}).catch(()=>{});
</script></body></html>
"""


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return HTMLResponse(DASHBOARD)


@app.get("/debug", response_class=HTMLResponse)
def debug_console():
    return HTMLResponse(DEBUG_CONSOLE)


CAMPAIGN_MANAGER = """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Campaigns — build & deploy</title>
<style>
:root{--bg:#0f172a;--card:#1e293b;--line:#334155;--txt:#e2e8f0;--mut:#94a3b8;--acc:#38bdf8;--ok:#16a34a;--bad:#dc2626}
*{box-sizing:border-box}
body{font-family:system-ui;margin:0;background:var(--bg);color:var(--txt)}
header{background:var(--card);padding:1rem 2rem;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:.5rem}
header h1{margin:0;font-size:1.2rem;color:var(--acc)}
nav a{color:var(--acc);text-decoration:none;font-weight:600;margin-right:1rem}
main{max-width:1100px;margin:0 auto;padding:1.5rem}
.banner{background:#0e7490;padding:.8rem 1.2rem;border-radius:.6rem;margin-bottom:1.2rem;font-weight:600}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:1rem;margin-bottom:1.5rem}
.ccard{background:var(--card);border:1px solid var(--line);border-radius:.7rem;padding:1rem;display:flex;flex-direction:column;gap:.4rem}
.ccard.active{border:2px solid var(--ok)}
.ccard h3{margin:0;font-size:1rem}
.ccard .desc{color:var(--mut);font-size:.78rem;flex:1}
.badge{align-self:flex-start;background:var(--ok);color:#fff;font-size:.68rem;padding:.15rem .5rem;border-radius:1rem;font-weight:700}
.crow{display:flex;gap:.4rem;margin-top:.4rem}
button{border:0;border-radius:.5rem;padding:.5rem .8rem;font-weight:700;cursor:pointer;font-size:.8rem}
.b-edit{background:var(--line);color:var(--txt)}.b-edit:hover{background:#475569}
.b-deploy{background:var(--ok);color:#fff}.b-deploy:hover{background:#15803d}
.b-sm{padding:.3rem .6rem;font-size:.72rem}
.b-danger{background:#7f1d1d;color:#fecaca}
.b-add{background:var(--acc);color:#082f49}
.panel{background:var(--card);border-radius:.7rem;padding:1.2rem;margin-bottom:1.2rem}
.panel h2{margin:.1rem 0 1rem;font-size:1.05rem;color:var(--acc)}
label{display:block;font-size:.7rem;text-transform:uppercase;letter-spacing:.04em;color:var(--mut);margin:.7rem 0 .2rem}
input,select,textarea{width:100%;background:var(--bg);border:1px solid var(--line);color:var(--txt);border-radius:.4rem;padding:.5rem;font-size:.9rem;font-family:inherit}
textarea{resize:vertical;min-height:2.4rem}
.f2{display:grid;grid-template-columns:1fr 1fr;gap:.8rem}
.f3{display:grid;grid-template-columns:2fr 1fr 1fr;gap:.8rem}
.q{border:1px solid var(--line);border-radius:.6rem;padding:1rem;margin:.8rem 0}
.qhead{display:flex;justify-content:space-between;align-items:center;margin-bottom:.2rem}
.qhead .qid{font-weight:800;color:var(--acc)}
.kv{display:grid;grid-template-columns:150px 1fr;gap:.4rem .6rem;align-items:center;margin-top:.4rem}
.kv .k{font-size:.72rem;color:var(--mut)}
.sticky{position:sticky;bottom:0;background:var(--card);padding:.8rem 0;display:flex;gap:.6rem;border-top:1px solid var(--line);margin-top:1rem}
.b-save{background:var(--acc);color:#082f49;padding:.7rem 1.4rem;font-size:.9rem}
.insight{background:var(--bg);border-radius:.5rem;padding:.8rem;font-size:.82rem;color:#a5f3fc;white-space:pre-wrap}
#toast{position:fixed;bottom:20px;right:20px;background:var(--ok);color:#fff;padding:.7rem 1.2rem;border-radius:.5rem;font-weight:700;opacity:0;transition:opacity .3s;pointer-events:none}
.muted{color:var(--mut);font-size:.78rem}
</style></head><body>
<header><h1>&#9881;&#65039; Campaigns</h1><nav><a href="/">&#128202; Dashboard</a><a href="/campaigns">&#9881;&#65039; Campaigns</a><a href="/debug">&#127908; Voice Console</a></nav></header>
<main>
<div class="banner" id="banner">Loading…</div>
<div class="crow" style="margin-bottom:1rem;align-items:center">
  <button class="b-add" onclick="newCampaign()">&#65291; New campaign</button>
  <span class="muted">Creates an empty campaign — or clone an existing script to start faster.</span>
</div>
<div class="grid" id="cards"></div>

<div class="panel" id="editor" style="display:none">
  <h2 id="edTitle">Edit campaign</h2>
  <div id="edBody"></div>
  <div class="sticky">
    <button class="b-save" onclick="saveCampaign()">💾 Save script &amp; compliance</button>
    <button class="b-deploy" onclick="deployCurrent()">🚀 Deploy this campaign</button>
    <button class="b-edit" onclick="hideEditor()">Cancel</button>
    <span class="muted" style="align-self:center">Saving updates the script the bot speaks. Deploy makes it the live campaign.</span>
  </div>
</div>

<div class="panel">
  <h2>&#129504; What the bot has learned (self-learning)</h2>
  <div class="insight" id="insight">Loading…</div>
  <div class="crow" style="margin-top:.8rem">
    <button class="b-sm b-danger" onclick="resetAll()">Reset ALL campaigns to defaults</button>
  </div>
</div>
</main>
<div id="toast"></div>

<script>
let list=[], active=null, cur=null;

function toast(msg){const t=document.getElementById('toast');t.textContent=msg;t.style.opacity=1;clearTimeout(t._h);t._h=setTimeout(()=>t.style.opacity=0,1800);}
function esc(s){return (s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}

async function loadAll(){
  const [cr,ir]=await Promise.all([fetch('/api/campaigns'),fetch('/api/insights')]);
  const cd=await cr.json(); list=cd.campaigns; active=cd.active;
  document.getElementById('banner').innerHTML='&#127919; Active campaign: <b>'+esc((list.find(c=>c.active)||{}).name||active)+'</b> — the bot is running this script right now.';
  renderCards();
  document.getElementById('insight').textContent=JSON.stringify(await ir.json(),null,1);
}

function renderCards(){
  const g=document.getElementById('cards'); g.innerHTML='';
  for(const c of list){
    const el=document.createElement('div');
    el.className='ccard'+(c.active?' active':'');
    el.innerHTML=`<h3>${esc(c.name)}</h3>
      <span class="muted">${c.questions} verification step(s)</span>
      <div class="desc">${esc(c.description||'')}</div>
      ${c.active?'<span class="badge">LIVE</span>':''}
      <div class="crow">
        <button class="b-edit" onclick="editCampaign('${esc(c.id)}')">&#9998; Edit</button>
        ${c.active?'':'<button class="b-deploy" onclick="deploy('+JSON.stringify(c.id)+')">&#9654; Deploy</button>'}
        ${(!c.active&&!c.builtin)?'<button class="b-sm b-danger" onclick="deleteCampaign('+JSON.stringify(c.id)+','+JSON.stringify(c.name)+')">&#128465; Delete</button>':''}
      </div>`;
    g.appendChild(el);
  }
}

async function newCampaign(){
  const name=prompt('Name for the new campaign:','My New Campaign');
  if(name===null) return;
  const trimmed=(name||'').trim();
  if(!trimmed){toast('Name required');return;}
  let base=null;
  if(confirm('Clone an existing campaign\\'s script?\\n\\nOK = choose a campaign to clone, Cancel = start empty.')){
    const choices=list.filter(c=>!c.active);
    if(choices.length){
      const pick=prompt('Type the ID of the campaign to clone:\\n\\n'+choices.map(c=>c.id+'  ('+c.name+')').join('\\n'), choices[0].id);
      if(pick!==null && pick.trim()) base=pick.trim();
    }
  }
  const r=await fetch('/api/campaigns',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:trimmed,base})});
  const d=await r.json();
  if(d.ok){toast('Created "'+d.id+'" — opening editor…');loadAll();editCampaign(d.id);}
  else toast('Error: '+d.error);
}

async function deleteCampaign(id,name){
  if(!confirm('Delete campaign "'+name+'"?\\n\\nThis can\\'t be undone.'))return;
  const r=await fetch('/api/campaigns/'+encodeURIComponent(id),{method:'DELETE'});
  const d=await r.json();
  if(d.ok){toast('Deleted "'+id+'"');if(cur&&cur.id===id){cur=null;hideEditor();}loadAll();}
  else toast('Error: '+d.error);
}

async function deploy(id){
  const r=await fetch('/api/campaigns/active',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
  const d=await r.json();
  if(d.ok){toast('Deployed! Bot is now running as '+id);loadAll();}
  else toast('Error: '+d.error);
}
async function deployCurrent(){ if(cur) deploy(cur.id); }

async function editCampaign(id){
  const r=await fetch('/api/campaigns/'+id); cur=await r.json();
  document.getElementById('edTitle').textContent='Edit: '+cur.name;
  renderEditor(); document.getElementById('editor').style.display='block';
  document.getElementById('editor').scrollIntoView({behavior:'smooth'});
}
function hideEditor(){document.getElementById('editor').style.display='none';cur=null;}

function renderEditor(){
  const b=document.getElementById('edBody'); if(!cur)return;
  let h='';
  h+=`<div class="f2"><div><label>Campaign name</label><input data-f="name" value="${esc(cur.name)}"></div>
     <div><label>ID (fixed)</label><input value="${esc(cur.id)}" disabled></div></div>`;
  h+=`<label>Description</label><input data-f="description" value="${esc(cur.description||'')}">`;
  h+=`<div class="f2"><div><label>Agent name (voice persona)</label><input data-f="agent_name" value="${esc(cur.agent_name||'Lina')}"></div>
     <div><label>Business / brand</label><input data-f="business_name" value="${esc(cur.business_name||'')}"></div></div>`;

  h+='<h2 style="margin-top:1.2rem">Opening &amp; closing lines</h2>';
  for(const k of ['greet','transfer_handoff','busy_close','not_interested_close','hello_checkin']){
    h+=`<label>${k.replace('_',' ')}</label><textarea data-line="${k}">${esc((cur.lines||{})[k]||'')}</textarea>`;
  }

  h+=`<label>&#128274; Compliance: recorded-call disclosure (spoken immediately after the greeting — leave blank to speak none)</label>
      <textarea data-f="disclosure">${esc(cur.disclosure||'')}</textarea>`;

  h+='<h2 style="margin-top:1.2rem">Verification flow &amp; compliance</h2><div class="muted">Each step is asked in order. "Age range" is your eligibility compliance. The bot only proceeds on a clear pass.</div>';
  (cur.questions||[]).forEach((q,i)=>{ h+=qEditor(q,i); });

  h+=`<button class="b-add b-sm" style="margin-top:.6rem" onclick="addQ()">+ Add verification step</button>`;
  b.innerHTML=h;
}

function qEditor(q,i){
  const isAge=q.kind==='age';
  let h=`<div class="q"><div class="qhead"><span class="qid">Step ${i+1}: ${esc(q.id)}</span>
    <span><button class="b-sm b-edit" onclick="moveQ(${i},-1)">&#8593;</button>
    <button class="b-sm b-edit" onclick="moveQ(${i},1)">&#8595;</button>
    <button class="b-sm b-danger" onclick="delQ(${i})">Delete</button></span></div>`;
  h+=`<div class="f3">
    <div><label>Question you ask</label><input data-q="${i}" data-qf="ask_line" value="${esc(q.ask_line||'')}"></div>
    <div><label>Type</label><select data-q="${i}" data-qf="kind" onchange="renderEditor()">
      <option value="yes_no" ${q.kind==='yes_no'?'selected':''}>Yes / No</option>
      <option value="age" ${q.kind==='age'?'selected':''}>Age range</option>
      <option value="keyword" ${q.kind==='keyword'?'selected':''}>Keyword</option></select></div>
    <div><label>Max re-asks</label><input type="number" min="0" max="3" data-q="${i}" data-qf="max_clarify" value="${q.max_clarify??2}"></div>
  </div>`;
  if(isAge){
    h+=`<div class="f2"><div><label>&#9935; Compliance: minimum age</label><input type="number" data-q="${i}" data-qf="age_min" value="${q.age_min??''}"></div>
      <div><label>&#9935; Compliance: maximum age</label><input type="number" data-q="${i}" data-qf="age_max" value="${q.age_max??''}"></div></div>`;
  }
  if(q.kind==='keyword'){
    h+=`<label>Pass keywords (comma separated)</label><input data-q="${i}" data-qf="pass_keywords" value="${esc((q.pass_keywords||[]).join(', '))}">`;
  }
  // response lines
  h+=`<label>Response lines (the bot speaks these; it rewords them naturally)</label><div class="kv">`;
  for(const k of Object.keys(q.lines||{})){
    h+=`<span class="k">${esc(k)}</span><textarea data-ql="${i}" data-qlk="${k}">${esc(q.lines[k])}</textarea>`;
  }
  h+=`</div>`;
  // routes
  h+=`<label>Routing (verdict = action). Actions: next | transfer | clarify | end:DISPOSITION</label>
      <textarea data-qr="${i}" style="font-family:monospace;font-size:.78rem">${esc(routesToText(q.routes||{}))}</textarea>`;
  h+=`</div>`;
  return h;
}

function routesToText(r){return Object.entries(r).map(([k,v])=>k+'='+v).join('\\n');}
function textToRoutes(t){const o={};for(const line of (t||'').split('\\n')){const i=line.indexOf('=');if(i>0)o[line.slice(0,i).trim().toUpperCase()]=line.slice(i+1).trim();}return o;}

function addQ(){ if(!cur)return; cur.questions.push({id:'step'+(cur.questions.length+1),kind:'yes_no',ask_line:'',max_clarify:2,age_min:null,age_max:null,pass_keywords:[],routes:{YES:'next',NO:'end:AI-NO',UNCLEAR:'clarify'},lines:{no:'I understand. Thank you. Have a great day.',repeat:'I\\'m sorry, can you say that again?'}}); renderEditor(); }
function delQ(i){ if(!cur)return; cur.questions.splice(i,1); renderEditor(); }
function moveQ(i,d){ if(!cur)return; const j=i+d; if(j<0||j>=cur.questions.length)return; const t=cur.questions[i];cur.questions[i]=cur.questions[j];cur.questions[j]=t; renderEditor(); }

function collect(){
  // read basic + lines + questions from DOM into cur
  document.querySelectorAll('#edBody [data-f]').forEach(el=>{cur[el.dataset.f]=el.value;});
  const lines={}; document.querySelectorAll('#edBody [data-line]').forEach(el=>{lines[el.dataset.line]=el.value;}); cur.lines=lines;
  (cur.questions||[]).forEach((q,i)=>{
    document.querySelectorAll(`#edBody [data-q="${i}"]`).forEach(el=>{
      const f=el.dataset.qf;
      if(f==='max_clarify')q.max_clarify=parseInt(el.value||'0',10);
      else if(f==='age_min'||f==='age_max')q[f]=el.value===''?null:parseInt(el.value,10);
      else if(f==='pass_keywords')q.pass_keywords=el.value.split(',').map(s=>s.trim()).filter(Boolean);
      else q[f]=el.value;
    });
    const lns={}; document.querySelectorAll(`#edBody [data-ql="${i}"]`).forEach(el=>{lns[el.dataset.qlk]=el.value;}); q.lines=lns;
    const rt=document.querySelector(`#edBody [data-qr="${i}"]`); if(rt)q.routes=textToRoutes(rt.value);
  });
  return cur;
}

async function saveCampaign(){
  if(!cur)return; const body=collect();
  const r=await fetch('/api/campaigns/'+cur.id,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const d=await r.json();
  if(d.ok){toast('Saved! Redeploy to apply if live.');loadAll();} else toast('Error: '+d.error);
}

async function resetAll(){ if(!confirm('Reset every campaign to its default script?'))return; await fetch('/api/campaigns/reset-all',{method:'POST'}); toast('All campaigns reset'); cur=null;hideEditor();loadAll(); }

loadAll();
</script></body></html>
"""


@app.get("/campaigns", response_class=HTMLResponse)
def campaigns_page():
    return HTMLResponse(CAMPAIGN_MANAGER)


def run_ari():
    from agent.ari import ARI

    handler = build_stasis_handler(CONFIG, CALL_LOG)
    ARI(CONFIG).run_forever(CONFIG.ari_app, handler)
