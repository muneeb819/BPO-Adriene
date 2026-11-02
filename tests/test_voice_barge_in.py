"""Barge-in: interrupting the bot must stop its speech, not end the call.

Why this file exists separately from the end-to-end test: the e2e socket test
only reproduces the defect when the speech engine is still *talking* when the
barge lands. With no TTS engine installed the speak task finishes instantly, the
cancel is a no-op, and the bug hides. These tests make the race deterministic by
holding the speech open on purpose.
"""
from __future__ import annotations

import asyncio

import pytest

from server.voice_ws import VoiceSession


def _session() -> VoiceSession:
    """A VoiceSession with no engine, no keys and no network."""
    from agent.config import Config

    cfg = Config.load()
    session = VoiceSession(cfg, send=_noop)
    session.speak = _speak_forever.__get__(session, VoiceSession)  # type: ignore[method-assign]
    return session


async def _noop(_payload: dict) -> None:
    return None


async def _speak_forever(self, _text: str) -> None:
    """Stand-in for TTS that is still mid-sentence when the barge arrives."""
    await asyncio.sleep(30)


@pytest.mark.asyncio
async def test_a_barge_stops_speech_and_the_caller_survives():
    """The regression: awaiting a cancelled task raised CancelledError in run().

    That unwound VoiceSession.run() and dropped the whole call — the customer
    interrupted the bot and the line went dead, which is the opposite of what
    barge-in is documented to do ("stops talking instantly, then listens").
    """
    session = _session()

    speaking = asyncio.create_task(session._speak_guarded("a long reply"))
    await asyncio.sleep(0.01)          # let the speak task start
    assert not speaking.done(), "the speech should still be in flight"

    session.interrupt_speaking()

    # Must return normally. Before the fix this raised CancelledError.
    await asyncio.wait_for(speaking, timeout=2)


@pytest.mark.asyncio
async def test_the_speech_task_really_is_cancelled():
    """Barge-in must actually stop the audio, not merely survive it."""
    session = _session()

    speaking = asyncio.create_task(session._speak_guarded("a long reply"))
    await asyncio.sleep(0.01)
    session.interrupt_speaking()
    await asyncio.wait_for(speaking, timeout=2)

    assert session._speak_task is not None
    assert session._speak_task.cancelled(), "the speech task should be cancelled"


@pytest.mark.asyncio
async def test_an_outer_cancellation_still_propagates():
    """Shutdown must not be swallowed by the barge handling.

    The fix absorbs a cancellation only when this session's own barge caused it.
    If the server is going down, the CancelledError has to keep travelling or
    the task would linger.
    """
    session = _session()

    speaking = asyncio.create_task(session._speak_guarded("a long reply"))
    await asyncio.sleep(0.01)

    speaking.cancel()                  # not a barge
    with pytest.raises(asyncio.CancelledError):
        await speaking


@pytest.mark.asyncio
async def test_a_barge_after_speech_has_finished_is_harmless():
    """Customers hit the button late; that must not raise or rewind anything."""
    session = _session()
    session.speak = _speak_briefly.__get__(session, VoiceSession)  # type: ignore[method-assign]

    await asyncio.wait_for(session._speak_guarded("short"), timeout=2)
    session.interrupt_speaking()        # nothing is playing
    session.interrupt_speaking()        # and again
    assert not session._barged, "a late barge must not leave the flag set"


async def _speak_briefly(self, _text: str) -> None:
    return None
