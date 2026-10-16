from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from .core.state_machine import CallSession
import uuid

app = FastAPI(title="BPO-Adriene AI Voice Bot (local-debug)")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory sessions (for prototype only)
sessions = {}

@app.post("/api/call/start")
async def start_call():
    session_id = str(uuid.uuid4())
    sessions[session_id] = CallSession(session_id=session_id)
    first_prompt = sessions[session_id].get_next_bot_prompt()
    return JSONResponse({"session_id": session_id, "bot_prompt": first_prompt})

@app.post("/api/call/transfer")
async def transfer_call(session_id: str):
    # Placeholder: In production this triggers ViciDial API / SIP transfer
    session = sessions.get(session_id)
    if not session:
        return JSONResponse({"error": "session not found"}, status_code=404)
    session.state = "transferred"
    return JSONResponse({"status": "transferring", "session_id": session_id})

@app.websocket("/ws/call/{session_id}")
async def websocket_call(websocket: WebSocket, session_id: str):
    await websocket.accept()
    session = sessions.get(session_id)
    if not session:
        await websocket.send_json({"type": "error", "message": "session not found"})
        await websocket.close()
        return
    try:
        await websocket.send_json({"type": "bot_prompt", "text": session.get_next_bot_prompt()})
        while True:
            data = await websocket.receive_json()
            # Expect messages like {type: "user_utterance", text: "yes"}
            if data.get("type") == "user_utterance":
                user_text = data.get("text", "")
                bot_reply, should_transfer = session.process_user_utterance(user_text)
                await websocket.send_json({"type": "bot_reply", "text": bot_reply, "should_transfer": should_transfer})
                if should_transfer:
                    session.state = "transferring"
                    # In real system, trigger transfer via ViciDial API / SIP
                    await websocket.send_json({"type": "transfer", "message": "transfer_triggered"})
                    # end session for prototype
                    await websocket.close()
                    break
            else:
                await websocket.send_json({"type": "error", "message": "unknown message type"})
    except WebSocketDisconnect:
        print(f"Websocket disconnected for session {session_id}")
