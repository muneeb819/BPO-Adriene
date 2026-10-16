from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends
from fastapi.responses import JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from .core.state_machine import CallSession
from .core.orchestrator import get_stt, get_media, get_llm, get_tts, get_transfer, get_runtime_config, set_runtime_config
from .core.brain import get_memory, get_brain, normalize as brain_normalize, PROFILE as BRAIN_PROFILE
from .db import init_db, get_session
from .models import CallRecord, Transcript
from .qa_endpoints import router as qa_router
from sqlmodel import Session, select
from datetime import datetime, timezone
import uuid

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
    return get_runtime_config()


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
                        # trigger transfer event via adapter
                        transfer_adapter.transfer_call(session_id)
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


if FRONTEND_BUILD.is_dir():
    # Registered last, so /api/* and /ws/* above win. Serves the built React app
    # (index.html + assets) — the browser then talks to the same origin it loaded
    # from, which is what makes the WebSocket call flow work without a dev proxy.
    app.mount('/', StaticFiles(directory=str(FRONTEND_BUILD), html=True), name='frontend')
