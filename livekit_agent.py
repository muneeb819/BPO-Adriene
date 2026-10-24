#!/usr/bin/env python3
"""Adriene on LiveKit — the Medicare voice bot as a LiveKit agent.

This is the LiveKit entrypoint for the project. It takes the same dialogue brain,
persona, compliance layer and local neural voice that the browser console uses
and runs them inside a LiveKit room, so the bot can be reached from a web page,
a mobile client, or a SIP trunk.

Run it (debug/development mode):

    python livekit_agent.py dev          # hot reload + DEBUG logs, LiveKit Cloud
    python livekit_agent.py console      # local terminal, no LiveKit server
    python livekit_agent.py start        # production worker

Environment (see .env.example / docs/LIVEKIT.md):

    LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET   required
    LIVEKIT_AGENT_NAME     default "" = automatic dispatch (join every room)
    LIVEKIT_STT_PROVIDER   auto | deepgram | openai | faster_whisper | none
    LIVEKIT_TTS_PROVIDER   auto | local | openai | elevenlabs | none
    LIVEKIT_LLM_PROVIDER   brain | openai | anthropic | gemini
    LIVEKIT_TRANSFER_NUMBER  SIP number for the warm transfer (optional)

Design: the brain owns every decision (script, compliance, DNC). The LLM adapter
emits its reply, LiveKit handles turn-taking, interruption and transcripts, and
the local Piper voice speaks it. No cloud key is required for any of that.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# --- environment first: everything below reads it ---------------------------
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent / ".env")
except Exception:  # python-dotenv is optional at runtime
    pass

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from livekit import rtc                                    # noqa: E402
from livekit.agents import (                               # noqa: E402
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    cli,
)

from backend.app.core.adapters.livekit_plugins import (     # noqa: E402
    build_llm,
    build_stt,
    build_tts,
    describe_stack,
)

logger = logging.getLogger("adriene.livekit")

CALL_LOG_DIR = Path(os.getenv("LIVEKIT_CALL_LOG_DIR", REPO_ROOT / "data" / "livekit_calls"))
AGENT_NAME = os.getenv("LIVEKIT_AGENT_NAME", "").strip()
GREETING_WAIT = float(os.getenv("LIVEKIT_GREETING_WAIT", "8"))
DEBUG = os.getenv("LOG_LEVEL", "").upper() == "DEBUG" or os.getenv("LIVEKIT_DEBUG") == "1"


# --------------------------------------------------------------------------- #
#                                 instructions
# --------------------------------------------------------------------------- #
INSTRUCTIONS = """\
You are Adriene, a 26-year-old licensed benefits specialist with the Senior
Resource Center. You are on a warm outbound call about Medicare options.

Your script, in order:
  1. Greet, identify yourself and the company, and ask if now is a good time.
  2. Ask whether the person currently has Medicare.
  3. Ask whether they are between 45 and 80 (or their age).
  4. If they have Medicare and are in range, warm-transfer them to a licensed
     specialist. Otherwise thank them and end the call politely.

Rules you never break:
  * Never claim to be Medicare, the government, or a robocall.
  * If the caller asks to stop calling / be removed, confirm the do-not-call
    request once, add nothing else, and end the call.
  * One question at a time. Two short sentences maximum per turn.
  * Plain, warm, conversational English — contractions, no jargon.
"""


# --------------------------------------------------------------------------- #
#                                    server
# --------------------------------------------------------------------------- #
server = AgentServer()


def _save_transcript(brain, room_name: str) -> Path:
    """Persist the call: transcript + disposition + qualification fields."""
    CALL_LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_room = "".join(c if c.isalnum() or c in "-_" else "_" for c in room_name)[:60]
    path = CALL_LOG_DIR / f"{stamp}-{safe_room}.json"
    payload = {
        "room": room_name,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        **(brain.result() if brain else {"transcript": []}),
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("call saved: %s (%s)", path, payload.get("disposition"))
    return path


@server.rtc_session(agent_name=AGENT_NAME)
async def adriene(ctx: JobContext) -> None:
    """One LiveKit job == one call."""
    room_name = ctx.room.name
    logger.info("=== call start | room=%s job=%s ===", room_name, ctx.job.id)

    await ctx.connect()

    brain = None

    async def _on_shutdown(reason: str) -> None:
        """Persist the call whatever ended it (hangup, transfer, error)."""
        try:
            _save_transcript(brain, room_name)
        except Exception:
            logger.exception("could not save the call transcript")

    ctx.add_shutdown_callback(_on_shutdown)

    def on_turn(role: str, text: str) -> None:
        logger.info("TURN %-9s %s", role.upper() + ":", text)

    def make_llm():
        nonlocal brain
        choice = build_llm(session_id=room_name, on_turn=on_turn)
        brain = choice.brain
        return choice.llm

    stt_impl = build_stt()
    tts_impl = build_tts()
    vad_impl = None
    try:
        from backend.app.core.adapters.livekit_plugins.factory import build_vad

        vad_impl = build_vad()
    except Exception as exc:                     # VAD is a nice-to-have
        logger.warning("VAD unavailable: %s", exc)

    logger.info(
        "stack | stt=%s | tts=%s | llm=brain | vad=%s",
        type(stt_impl).__name__ if stt_impl else "none",
        type(tts_impl).__name__ if tts_impl else "none",
        bool(vad_impl),
    )

    session = AgentSession(
        stt=stt_impl,
        tts=tts_impl,
        llm=make_llm(),
        vad=vad_impl,
        # Barge-in: callers can talk over Adriene, exactly like the browser console.
        turn_handling={"interruption": {"enabled": True, "min_duration": 0.4}},
    )

    # --------------------------------------------------------- room input
    @ctx.room.on("data_received")
    def _on_data(packet: rtc.DataPacket) -> None:
        """Typed input from the debug console (and any client) drives the brain."""
        try:
            payload = json.loads(packet.data.decode("utf-8"))
        except Exception:
            return
        if payload.get("type") in ("user_text", "text", "say") and payload.get("text"):
            text = str(payload["text"]).strip()
            if not text:
                return
            logger.info("TEXT  input: %s", text)
            session.generate_reply(user_input=text)

    # -------------------------------------------------------- call actions
    async def _finish(reason: str) -> None:
        """Let the current sentence finish, then leave the room."""
        speech = session.current_speech
        if speech is not None:
            try:
                await speech.wait_for_playout()
            except Exception:
                pass
        await asyncio.sleep(0.4)               # a breath after the last word
        logger.info("=== call end | room=%s reason=%s ===", room_name, reason)
        ctx.shutdown()

    async def _transfer() -> None:
        number = os.getenv("LIVEKIT_TRANSFER_NUMBER", "").strip()
        speech = session.current_speech
        if speech is not None:
            try:
                await speech.wait_for_playout()
            except Exception:
                pass
        if number:
            sip_peer = next(
                (p for p in ctx.room.remote_participants.values()
                 if p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP),
                None,
            )
            if sip_peer is not None:
                try:
                    from livekit import api

                    await ctx.api.sip.transfer_sip_participant(
                        api.TransferSIPParticipantRequest(
                            room_name=ctx.room.name,
                            participant_identity=sip_peer.identity,
                            transfer_to=number,
                            play_dialtone=False,
                        )
                    )
                    logger.info("SIP transfer to %s requested", number)
                    await asyncio.sleep(2.0)
                    ctx.shutdown()
                    return
                except Exception:
                    logger.exception("SIP transfer failed — ending the call instead")
            else:
                logger.warning(
                    "LIVEKIT_TRANSFER_NUMBER is set but the caller is not a SIP "
                    "participant — ending the call instead"
                )
        await _finish("transfer")

    @session.on("agent_state_changed")
    def _on_state(ev) -> None:
        if ev.new_state != "listening" or brain is None:
            return
        action, brain.pending_action = brain.pending_action, None
        if action == "transfer":
            asyncio.create_task(_transfer())
        elif action == "end":
            asyncio.create_task(_finish("hangup"))

    @session.on("error")
    def _on_error(ev) -> None:
        logger.error("session error: %s", getattr(ev, "error", ev))

    # ------------------------------------------------------------- start
    await session.start(
        room=ctx.room,
        agent=Agent(instructions=INSTRUCTIONS),
    )

    # Wait (briefly) for a caller so the greeting isn't spoken into an empty room.
    if not ctx.room.remote_participants:
        try:
            await asyncio.wait_for(ctx.wait_for_participant(), timeout=GREETING_WAIT)
        except Exception:
            logger.warning("no caller joined within %.0fs — greeting anyway", GREETING_WAIT)

    await session.generate_reply()             # the brain returns the greeting

    # Keep the job alive until LiveKit ends it.
    while ctx.room.connection_state == rtc.ConnectionState.CONN_CONNECTED:
        await asyncio.sleep(0.5)
    logger.info("room disconnected | room=%s", room_name)


def _require_credentials() -> bool:
    missing = [k for k in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET")
               if not os.getenv(k)]
    if missing:
        print("\n  Missing LiveKit credentials: " + ", ".join(missing))
        print("  Put them in .env (see .env.example) or export them, then retry.\n")
        return False
    return True


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] in ("dev", "start", "console"):
        stack = describe_stack()
        print(f"\n  Adriene → LiveKit  ({sys.argv[1]} mode)")
        print(f"  url      {os.getenv('LIVEKIT_URL', '(unset)')}")
        print(f"  agent    {AGENT_NAME or '(automatic dispatch)'}")
        print(f"  stack    stt={stack['stt']}  tts={stack['tts']}  llm={stack['llm']}  vad={stack['vad']}")
        for warning in stack["warnings"]:
            print(f"  warning  {warning}")
        print("")
        if os.getenv("LIVEKIT_URL") and not _require_credentials():
            return
    cli.run_app(server)


if __name__ == "__main__":
    main()
