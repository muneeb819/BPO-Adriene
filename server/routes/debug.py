"""The debug console's server side.

/api/debug/*: start a scripted conversation, send a turn to it, read the
transcript back, synthesise a line, transcribe an uploaded clip, and report
which voice features are live.

The session store and the LLM-with-fallback helper live here because nothing
else uses them.
"""
from __future__ import annotations

import os
import uuid

from fastapi import APIRouter, File, Request, Response, UploadFile
from fastapi.responses import JSONResponse

from agent.config import Config
from server.context import CONFIG

router = APIRouter()


def _log_call(entry: dict) -> dict:
    """Append to the persisted call log.

    Imported lazily because server/app.py imports this module: a top-level
    import would be a cycle. Going through app_mod also means the call log the
    app owns — including a list a test has swapped in — is the one written to.
    """
    from server import app as app_mod

    return app_mod.log_call(entry)


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


@router.post("/api/debug/start")
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


@router.post("/api/debug/chat")
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
        _log_call({
            "lead_id": f"debug-{sid}",
            "action": action,
            "disposition": result.disposition,
            "facts": {**(result.classification or {}), "age": convo.ctx.age},
        })
    return payload


@router.get("/api/debug/transcript")
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


@router.post("/api/debug/speak")
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


@router.post("/api/debug/transcribe")
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


@router.get("/api/debug/voice")
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

