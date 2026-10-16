from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends
from fastapi.responses import JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from .core.state_machine import CallSession
from .core.orchestrator import get_stt, get_media, get_llm, get_tts, get_transfer, get_runtime_config, set_runtime_config
from .core.brain import get_memory, get_brain, normalize as brain_normalize, PROFILE as BRAIN_PROFILE
from .core.adapters import kokoro_tts
from .db import init_db, get_session
from .models import CallRecord, Transcript
from .qa_endpoints import router as qa_router
from sqlmodel import Session, select
from datetime import datetime, timezone
import json
import uuid

from fastapi import HTTPException

app = FastAPI(title="BPO-Adriene AI Voice Bot (local-debug)")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# QA reports & artifacts (produced by backend/scripts/qa_runner.py)
app.include_router(qa_router)

# Initialize DB
init_db()

# Runtime-configured adapters (local-first)
stt = get_stt()
media = get_media()
llm = get_llm()
tts = get_tts()
transfer_adapter = get_transfer()

# Pre-load the Kokoro model in the background so the first live call is fast.
if kokoro_tts.available():
    import threading
    threading.Thread(target=kokoro_tts.warm, daemon=True, name="kokoro-warm").start()

# In-memory sessions (for prototype only)
sessions = {}

@app.post("/api/config")
async def update_config(payload: dict):
    # allow setting STT_PROVIDER, TTS_PROVIDER, LLM_PROVIDER, MEDIA_BRIDGE, TRANSFER_PROVIDER
    set_runtime_config(payload)
    # refresh adapters
    global stt, media, llm, tts, transfer_adapter
    stt = get_stt()
    media = get_media()
    llm = get_llm()
    tts = get_tts()
    transfer_adapter = get_transfer()
    return JSONResponse({"status": "ok", "config": get_runtime_config()})

@app.get('/api/config')
async def get_config():
    cfg = get_runtime_config()
    # what is ACTUALLY running (adapter classes resolve at call time; a
    # cloud provider without a key still runs the local fallback)
    def _name(adapter):
        return type(adapter).__name__
    cfg['_active'] = {
        'stt': _name(get_stt()),
        'tts': _name(get_tts()),
        'llm': _name(get_llm()),
        'media': _name(get_media()),
        'transfer': _name(get_transfer()),
        'tts_engine': 'kokoro-local' if kokoro_tts.available() else 'browser-client',
        'llm_engine': (get_brain().llm_provider or 'local-brain'),
    }
    return cfg


# ---------------- self-learning brain -------------------------------------
@app.get("/api/brain/status")
def brain_status():
    """What the brain knows right now: persona, memory counts, LLM upgrade path."""
    mem = get_memory()
    brain = get_brain()
    return {
        "persona": BRAIN_PROFILE,
        "llm_provider": brain.llm_provider or "local-brain",
        "llm_upgrade_path": "set OPENAI_API_KEY / ANTHROPIC_API_KEY / GEMINI_API_KEY",
        "memory": mem.status(),
    }


@app.get("/api/brain/memory")
def brain_memory():
    """Full memory dump: learned phrasings, queued unknowns, feedback log."""
    return get_memory().dump()


@app.post("/api/brain/feedback")
def brain_feedback(payload: dict):
    """👍/👎 from the dashboard. 'bad' + correct_intent makes the brain learn."""
    utterance = payload.get("utterance") or ""
    record = {
        "utterance": utterance,
        "utterance_norm": brain_normalize(utterance),
        "verdict": payload.get("verdict") or "",
        "intent": payload.get("intent") or "",
        "bot_reply": payload.get("bot_reply") or "",
        "state": payload.get("state") or "",
        "correct_intent": payload.get("correct_intent") or "",
        "correct_polarity": payload.get("correct_polarity"),
        "corrected_reply": payload.get("corrected_reply") or "",
        "topic": payload.get("topic") or "",
        "session_id": payload.get("session_id") or "",
    }
    saved = get_memory().feedback(record)
    return {"status": "learned" if record.get("correct_intent") else "recorded",
            "record": saved, "memory": get_memory().status()}


@app.post("/api/brain/learn")
def brain_learn(payload: dict):
    """Direct learning: tell the brain a phrasing and what it means."""
    utterance = brain_normalize(payload.get("utterance") or "")
    intent = (payload.get("intent") or "").strip()
    if not utterance or not intent:
        return JSONResponse({"error": "utterance and intent are required"},
                            status_code=400)
    ok = get_memory().learn(
        utterance, intent,
        payload.get("polarity") if payload.get("polarity") is None
        else bool(payload.get("polarity")),
        "api",
    )
    return {"learned": ok, "utterance": utterance, "intent": intent,
            "memory": get_memory().status()}


# ---------------- neural voice (Kokoro-82M local TTS) -----------------------
@app.get("/api/tts/voices")
def tts_voices():
    """The20 US-English Kokoro voices with model-card grades + load state."""
    return kokoro_tts.status()


@app.post("/api/tts")
def tts_synth(payload: dict):
    """Synthesize a reply to WAV bytes using the chosen Kokoro voice.

    Body: {text, voice?, speed?}. The frontend pipelines at sentence level
    (plays chunk N while pre-fetching chunk N+1) so first audio lands fast.
    """
    text = (payload.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "text is required"}, status_code=400)
    voice = (payload.get("voice") or get_runtime_config().get("TTS_VOICE")
             or kokoro_tts.DEFAULT_VOICE)
    speed = payload.get("speed")
    if speed is None:
        try:
            speed = float(get_runtime_config().get("TTS_SPEED") or kokoro_tts.DEFAULT_SPEED)
        except ValueError:
            speed = kokoro_tts.DEFAULT_SPEED
    try:
        wav = kokoro_tts.synthesize(text, voice=voice, speed=speed)
    except Exception as e:
        return JSONResponse({"error": f"synthesis failed: {e}", "voice": voice},
                            status_code=503)
    from fastapi.responses import Response
    return Response(content=wav, media_type="audio/wav",
                    headers={"X-Kokoro-Voice": voice,
                             "Cache-Control": "no-store"})


@app.post("/api/tts/warm")
def tts_warm():
    """Kick a background warm-up (model + voices) and report state."""
    import threading
    if kokoro_tts.available() and not kokoro_tts.is_warmed():
        threading.Thread(target=kokoro_tts.warm, daemon=True).start()
    return kokoro_tts.status()

@app.post("/api/call/start")
async def start_call(session: Session = Depends(get_session)):
    session_id = str(uuid.uuid4())
    cs = CallSession(session_id=session_id)
    sessions[session_id] = cs
    # create DB call record
    record = CallRecord(session_id=session_id, state=cs.state)
    session.add(record)
    session.commit()
    session.refresh(record)
    first_prompt = cs.get_next_bot_prompt()
    # store initial bot transcript
    t = Transcript(call_id=record.id, role='bot', text=first_prompt)
    session.add(t)
    session.commit()
    return JSONResponse({"session_id": session_id, "bot_prompt": first_prompt})

@app.post("/api/call/transfer")
async def transfer_call(session_id: str, db: Session = Depends(get_session)):
    db_rec = db.exec(select(CallRecord).where(CallRecord.session_id == session_id)).first()
    if not db_rec:
        return JSONResponse({"error": "session not found"}, status_code=404)
    # call transfer adapter
    ok = transfer_adapter.transfer_call(session_id, target=None)
    if ok:
        db_rec.state = "transferred"
        db_rec.updated_at = datetime.now(timezone.utc)
        db.add(db_rec)
        db.commit()
        return JSONResponse({"status": "transferring", "session_id": session_id})
    else:
        return JSONResponse({"error": "transfer failed"}, status_code=500)

@app.get("/api/calls")
def list_calls(db: Session = Depends(get_session)):
    rows = db.exec(select(CallRecord).order_by(CallRecord.created_at.desc())).all()
    out = []
    for r in rows:
        out.append({"id": r.id, "session_id": r.session_id, "state": r.state, "created_at": r.created_at.isoformat()})
    return out

@app.get("/api/calls/{call_id}")
def get_call(call_id: int, db: Session = Depends(get_session)):
    rec = db.exec(select(CallRecord).where(CallRecord.id == call_id)).first()
    if not rec:
        return JSONResponse({"error": "call not found"}, status_code=404)
    transcripts = db.exec(select(Transcript).where(Transcript.call_id == rec.id).order_by(Transcript.timestamp)).all()
    tx = [{"role": t.role, "text": t.text, "timestamp": t.timestamp.isoformat()} for t in transcripts]
    return {"id": rec.id, "session_id": rec.session_id, "state": rec.state, "transcripts": tx}

@app.websocket("/ws/call/{session_id}")
async def websocket_call(websocket: WebSocket, session_id: str):
    await websocket.accept()
    session_obj = sessions.get(session_id)
    if not session_obj:
        await websocket.send_json({"type": "error", "message": "session not found"})
        await websocket.close()
        return
    # Start media bridge (noop in mock)
    media.start_call(session_id)
    try:
        # send initial prompt from state machine
        await websocket.send_json({"type": "bot_prompt", "text": session_obj.get_next_bot_prompt()})
        # DB session per connection
        with __import__('sqlmodel').Session(__import__('sqlmodel').create_engine("sqlite:///./dev.db", connect_args={"check_same_thread": False})) as dbconn:
            # find call record
            db_rec = dbconn.exec(select(CallRecord).where(CallRecord.session_id == session_id)).first()
            while True:
                data = await websocket.receive_json()
                if data.get("type") == "user_utterance":
                    user_text = data.get("text", "")
                    # Transcribe via STT adapter (mock returns same text)
                    transcript_text = stt.transcribe_text(user_text)
                    # store user transcript
                    ut = Transcript(call_id=db_rec.id, role='user', text=transcript_text)
                    dbconn.add(ut)
                    dbconn.commit()
                    # process via state machine
                    bot_reply, should_transfer = session_obj.process_user_utterance(transcript_text)
                    # store bot reply
                    bt = Transcript(call_id=db_rec.id, role='bot', text=bot_reply)
                    dbconn.add(bt)
                    dbconn.commit()
                    await websocket.send_json({"type": "bot_reply", "text": bot_reply, "should_transfer": should_transfer})
                    if should_transfer:
                        session_obj.state = "transferring"
                        db_rec.state = "transferring"
                        db_rec.updated_at = datetime.now(timezone.utc)
                        dbconn.add(db_rec)
                        dbconn.commit()
                        # trigger transfer event via adapter (never let
                        # transfer wiring kill a live call)
                        try:
                            transfer_adapter.transfer_call(session_id)
                        except Exception as e:
                            print(f"[transfer] adapter error (call continues): {e}")
                        media.stop_call(session_id)
                        await websocket.send_json({"type": "transfer", "message": "transfer_triggered"})
                        await websocket.close()
                        break
                else:
                    await websocket.send_json({"type": "error", "message": "unknown message type"})
    except WebSocketDisconnect:
        print(f"Websocket disconnected for session {session_id}")
    finally:
        media.stop_call(session_id)


# ---------------- UI: served by the API server (one origin for HTTP + WebSocket) ----------------
FRONTEND_PUBLIC = Path(__file__).resolve().parents[2] / 'frontend' / 'public'
FRONTEND_BUILD = Path(__file__).resolve().parents[2] / 'frontend' / 'build'


@app.get('/qa_reports.html')
def qa_reports_page():
    """Static QA Reports viewer (frontend/public/qa_reports.html)."""
    return FileResponse(FRONTEND_PUBLIC / 'qa_reports.html', media_type='text/html')


# ---------------- Recordings: mock-call turns + QA scenario audio ----------------
_QA_DIR = Path(__file__).resolve().parents[1] / 'qa_outputs'
_REC_DIR = _QA_DIR / 'recordings'


@app.get('/recordings.html')
def recordings_page():
    """Call-recording player (frontend/public/recordings.html)."""
    return FileResponse(FRONTEND_PUBLIC / 'recordings.html', media_type='text/html')


def _media_info(path: Path) -> dict:
    info = {'name': path.name, 'bytes': path.stat().st_size,
            'url': f'/media/{path.name}'}
    if path.suffix.lower() == '.wav':
        try:
            import wave as _wave
            with _wave.open(str(path), 'rb') as w:
                info['duration'] = round(w.getnframes() / float(w.getframerate()), 1)
        except Exception:
            info['duration'] = None
    return info


@app.get('/api/recordings')
async def list_recordings():
    """Everything recorded so far: real mock-call turns + QA scenario audio."""
    def _ls(d: Path):
        if not d.is_dir():
            return []
        return [_media_info(p) for p in sorted(d.glob('*.wav'))]

    transcripts = {}
    if _REC_DIR.is_dir():
        for p in sorted(_REC_DIR.glob('*_transcript.json')):
            try:
                transcripts[p.stem.replace('_transcript', '')] = json.loads(
                    p.read_text(encoding='utf-8'))
            except Exception:
                pass
    return {'mock_calls': _ls(_REC_DIR), 'qa_scenarios': _ls(_QA_DIR),
            'transcripts': transcripts}


@app.get('/media/{name}')
async def media_file(name: str):
    """Serve one recorded WAV/transcript — flat filenames only, no traversal."""
    if Path(name).name != name:
        raise HTTPException(status_code=404, detail='not found')
    for d in (_REC_DIR, _QA_DIR):
        p = d / name
        if p.is_file():
            media = 'audio/wav' if p.suffix.lower() == '.wav' else 'application/json'
            return FileResponse(p, media_type=media)
    raise HTTPException(status_code=404, detail='not found')


if FRONTEND_BUILD.is_dir():
    # Registered last, so /api/* and /ws/* above win. Serves the built React app
    # (index.html + assets) — the browser then talks to the same origin it loaded
    # from, which is what makes the WebSocket call flow work without a dev proxy.
    app.mount('/', StaticFiles(directory=str(FRONTEND_BUILD), html=True), name='frontend')
