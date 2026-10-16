from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from .core.state_machine import CallSession
from .core.adapters.stt_adapter import get_stt_adapter
from .core.adapters.media_bridge import get_media_bridge
from .db import init_db, get_session
from .models import CallRecord, Transcript
from sqlmodel import Session, select
import uuid

app = FastAPI(title="BPO-Adriene AI Voice Bot (local-debug)")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize DB
init_db()

stt = get_stt_adapter()
media = get_media_bridge()

# In-memory sessions (for prototype only)
sessions = {}

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
    # Placeholder: In production this triggers ViciDial API / SIP transfer
    db_rec = db.exec(select(CallRecord).where(CallRecord.session_id == session_id)).first()
    if not db_rec:
        return JSONResponse({"error": "session not found"}, status_code=404)
    db_rec.state = "transferred"
    db_rec.updated_at = __import__('datetime').datetime.utcnow()
    db.add(db_rec)
    db.commit()
    return JSONResponse({"status": "transferring", "session_id": session_id})

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
                        db_rec.updated_at = __import__('datetime').datetime.utcnow()
                        dbconn.add(db_rec)
                        dbconn.commit()
                        # trigger transfer event (noop for mock)
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
