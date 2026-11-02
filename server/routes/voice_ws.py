"""The live voice WebSocket (/ws/voice) and the SIP bridge entry point.

A browser mic streams audio up, Deepgram transcribes it, the brain decides, and
TTS streams audio back — the whole real-time call loop. It is one route but a
substantial one, and it is the only place the server drives VoiceSession, so it
lives on its own rather than among the recordings endpoints.

Like the other route modules it reaches the app's shared state lazily:
_ws_authorised and log_call are owned by server/app.py, and a top-level import
would be a cycle.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from server.context import CONFIG

router = APIRouter()


def _ws_authorised(websocket: WebSocket) -> bool:
    from server import auth

    return auth.ws_authorised(websocket)


def _log_call(entry: dict) -> dict:
    from server import app as app_mod

    return app_mod.log_call(entry)


@router.websocket("/ws/voice")
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
                    _log_call({
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






