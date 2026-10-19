"""Tests for the LiveKit integration: brain-as-LLM, local voice, provider stack.

These run without a LiveKit server, a microphone or any cloud key — they exercise
the exact adapter classes the worker uses. Run them the same way as the rest of
the suite:

    cd backend && python -m pytest tests -q
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

# The token test needs the project's credentials; load ./.env like the app does.
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
except Exception:
    pass

from app.core.adapters.livekit_plugins.brain_llm import BrainLLM


def _turn(brain: BrainLLM, text: str | None) -> str:
    """Run one chat turn through the adapter and collect the reply."""
    from livekit.agents import llm

    async def run() -> str:
        chat_ctx = llm.ChatContext.empty()
        if text:
            chat_ctx.add_message(role="user", content=text)
        parts = []
        async for chunk in brain.chat(chat_ctx=chat_ctx):
            if chunk.delta and chunk.delta.content:
                parts.append(chunk.delta.content)
        return "".join(parts)

    return asyncio.run(run())


# --------------------------------------------------------------------------- #
#                          the scripted dialogue brain
# --------------------------------------------------------------------------- #
def test_brain_greeting_asks_about_medicare():
    brain = BrainLLM(session_id="test-greeting")
    reply = _turn(brain, None)
    assert "Medicare" in reply
    assert "Adriene" in reply
    assert brain.call.state == "ask_medicare"


def test_brain_qualifies_and_requests_transfer():
    brain = BrainLLM(session_id="test-qualify")
    _turn(brain, None)

    reply = _turn(brain, "Yes, I have Medicare.")
    assert "between" in reply.lower() or "age" in reply.lower()

    reply = _turn(brain, "I'm sixty-seven.")
    assert "transfer" in reply.lower()
    assert brain.pending_action == "transfer"
    assert brain.disposition == "AI-TRANSFER"
    assert brain.call.data["has_medicare"] is True
    assert brain.call.data["age"] == 67


def test_brain_not_eligible_ends_call():
    brain = BrainLLM(session_id="test-not-eligible")
    _turn(brain, None)
    _turn(brain, "No, I don't have Medicare.")
    _turn(brain, "I'm 32.")
    assert brain.pending_action == "end"
    assert brain.call.state == "not_eligible"


def test_brain_dnc_uses_compliance_wording():
    brain = BrainLLM(session_id="test-dnc")
    _turn(brain, None)
    reply = _turn(brain, "Stop calling me. Put me on your do not call list.")
    lowered = reply.lower().replace("-", " ")
    assert "do not call" in lowered or "remove" in lowered
    assert brain.pending_action == "end"
    assert brain.disposition == "DNC"
    assert brain.call.data["dnc"] is True


def test_brain_transcript_is_recorded():
    brain = BrainLLM(session_id="test-transcript")
    _turn(brain, None)
    _turn(brain, "yes")
    roles = [t["role"] for t in brain.turns]
    assert roles[0] == "assistant"
    assert "user" in roles
    assert len(brain.turns) >= 3


# --------------------------------------------------------------------------- #
#                                  the voice
# --------------------------------------------------------------------------- #
def _local_tts():
    from app.core.adapters.livekit_plugins import neural_tts

    if not neural_tts.available():
        pytest.skip("no local Piper voice installed")
    return neural_tts.LocalNeuralTTS()


def test_local_tts_renders_audible_pcm():
    tts = _local_tts()

    async def run():
        frames = []
        async for audio in tts.synthesize("Hello, this is a short test."):
            frames.append(audio.frame)
        return frames

    frames = asyncio.run(run())
    assert frames, "the voice produced no audio"
    assert frames[0].sample_rate == 16000
    assert frames[0].num_channels == 1
    assert sum(f.duration for f in frames) > 0.5


def test_local_tts_empty_text_is_a_noop():
    tts = _local_tts()

    async def run():
        return [a async for a in tts.synthesize("   ")]

    assert asyncio.run(run()) == []


# --------------------------------------------------------------------------- #
#                              provider selection
# --------------------------------------------------------------------------- #
def test_describe_stack_reports_every_leg():
    from app.core.adapters.livekit_plugins.factory import describe_stack

    stack = describe_stack()
    for key in ("stt", "tts", "llm", "vad", "voices", "warnings"):
        assert key in stack
    assert stack["llm"] in ("brain", "openai", "anthropic", "gemini")


def test_build_llm_defaults_to_the_brain():
    from app.core.adapters.livekit_plugins.factory import build_llm

    choice = build_llm(session_id="builder")
    assert choice.provider == "brain"
    assert choice.brain is choice.llm
    assert isinstance(choice.llm, BrainLLM)


# --------------------------------------------------------------------------- #
#                                 room tokens
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(
    not (os.getenv("LIVEKIT_API_KEY") and os.getenv("LIVEKIT_API_SECRET")),
    reason="LiveKit credentials are not configured",
)
def test_room_token_carries_join_grant():
    import jwt
    from livekit import api

    key = os.environ["LIVEKIT_API_KEY"]
    secret = os.environ["LIVEKIT_API_SECRET"]
    token = (
        api.AccessToken(key, secret)
        .with_identity("pytest")
        .with_grants(api.VideoGrants(room_join=True, room="adriene-test"))
        .to_jwt()
    )
    claims = jwt.decode(token, secret, algorithms=["HS256"])
    assert claims["sub"] == "pytest"
    assert claims["video"]["room"] == "adriene-test"
    assert claims["video"]["roomJoin"] is True
    assert claims["iss"] == key
