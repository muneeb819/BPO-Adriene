"""Medicare flagship gate — the compliance rules the bot may never break.

Rule under test: ask "Do you currently have Medicare?" and act ONLY on a clear
YES/NO. Never guess or assume coverage. Age gate is 45-90.

All tests run with ScriptedLLM so the exact scripted line is asserted rather
than whatever an LLM happens to phrase.
"""
from __future__ import annotations

import pytest

from agent.campaign import get_store
from agent.config import Config
from agent.conversation import Conversation

FRIENDLY = "I'm doing well, thanks for asking"


def make(cid: str = "medicare", llm=None) -> Conversation:
    """Build a conversation on an explicit campaign with a deterministic LLM."""
    from tests.conftest import ScriptedLLM

    return Conversation(
        Config.load(),
        llm or ScriptedLLM(),
        campaign=get_store().get(cid),
    )


def open_gate(c: Conversation) -> str:
    """Speak the greeting, answer it pleasantly, and return the asked question."""
    c.start()
    beat = c.hear(FRIENDLY)
    assert beat.end_call is False, "gate must not end on a friendly greeting"
    assert beat.ai_speech, "bot must ask the first verification question"
    return beat.ai_speech


def transcript(c: Conversation) -> str:
    return " ".join(t["text"] for t in c.ctx.transcript)


# ---------------- greeting / disclosure ----------------

def test_greeting_is_spoken(tmp_store, tmp_memory):
    c = make()
    line = c.start()
    assert line
    assert "Lina" in line or "from" in line
    assert c.ctx.state == "WAIT_FOR_RESPONSE"


def test_recording_disclosure_is_spoken_after_greeting(tmp_store, tmp_memory):
    c = make()
    line = c.start()
    assert c.campaign.disclosure, "medicare default must carry a disclosure"
    assert c.campaign.disclosure in line, "disclosure must be spoken verbatim"


def test_empty_disclosure_is_skipped_not_crashed(tmp_store, tmp_memory):
    c = make()
    c.campaign.disclosure = ""
    line = c.start()
    assert line and "recorded" not in line.lower()


# ---------------- the Medicare gate ----------------

def test_friendly_greeting_is_asked_the_medicare_question(tmp_store, tmp_memory):
    c = make()
    asked = open_gate(c)
    assert "medicare" in asked.lower()
    assert "do you currently have medicare" in asked.lower()


def test_clear_no_ends_call_without_asking_age(tmp_store, tmp_memory):
    c = make()
    open_gate(c)
    beat = c.hear("No, I don't have Medicare")
    assert beat.end_call is True
    assert beat.disposition == "AI-NO-MEDICARE"
    assert "how old are you" not in transcript(c).lower()


def test_clear_no_never_assumes_coverage(tmp_store, tmp_memory):
    c = make()
    open_gate(c)
    c.hear("Nope")
    assert c.ctx.medicare_status == "NO"
    assert c.ctx.age is None, "coverage must never be assumed after a NO"
    assert c.ended is True


def test_clear_yes_advances_to_age_question(tmp_store, tmp_memory):
    c = make()
    open_gate(c)
    beat = c.hear("Yes, I have Medicare")
    assert beat.end_call is False
    assert "how old are you" in beat.ai_speech.lower()
    assert c.ctx.medicare_status == "YES"


def test_unsure_is_confirmed_not_guessed(tmp_store, tmp_memory):
    c = make()
    open_gate(c)
    beat = c.hear("I'm not sure")
    assert beat.end_call is False, "uncertain must be re-confirmed, not guessed"
    assert "yes or no" in beat.ai_speech.lower()


def test_what_does_medicare_mean_is_clarified_then_reasked(tmp_store, tmp_memory):
    c = make()
    open_gate(c)
    beat = c.hear("What does Medicare mean?")
    assert beat.end_call is False
    assert "medicare" in beat.ai_speech.lower()
    # it must re-ask rather than ending or skipping the gate
    assert c.ctx.q_index == 0
    assert c.ctx.clarify_attempts == 1


def test_private_insurance_only_triggers_do_you_also_have_medicare(tmp_store, tmp_memory):
    c = make()
    open_gate(c)
    beat = c.hear("Just my private insurance through work")
    assert beat.end_call is False, "private insurance must not be read as Medicare"
    assert "also" in beat.ai_speech.lower() and "medicare" in beat.ai_speech.lower()


def test_refusal_ends_the_call(tmp_store, tmp_memory):
    c = make()
    open_gate(c)
    beat = c.hear("That's personal, I don't want to answer")
    assert beat.end_call is True
    assert beat.disposition == "AI-MEDICARE-REFUSED"


def test_two_unanswered_clarifies_then_ends(tmp_store, tmp_memory):
    """max_clarify=2 -> two re-asks, then the call ends (never loops forever)."""
    c = make()
    open_gate(c)
    first = c.hear("purple elephant sandwich")
    assert first.end_call is False and c.ctx.clarify_attempts == 1
    second = c.hear("gibberish response again")
    assert second.end_call is False and c.ctx.clarify_attempts == 2
    third = c.hear("still no meaningful answer")
    assert third.end_call is True
    assert third.disposition == "AI-UNCLEAR"


def test_unrelated_answer_is_repeated_once_not_skipped(tmp_store, tmp_memory):
    c = make()
    open_gate(c)
    beat = c.hear("the weather is lovely today")
    assert beat.end_call is False, "an off-topic reply must not skip the gate"
    assert c.ctx.q_index == 0, "still on the Medicare question"
    assert "medicare" in beat.ai_speech.lower()


def test_dnc_is_honored_at_any_point(tmp_store, tmp_memory):
    c = make()
    open_gate(c)
    beat = c.hear("Take me off your call list")
    assert beat.end_call is True
    assert beat.disposition == "AI-DNC"
    assert c.ctx.dnc is True


def test_busy_is_honored_at_any_point(tmp_store, tmp_memory):
    c = make()
    open_gate(c)
    beat = c.hear("I'm busy right now, call me back later")
    assert beat.end_call is True
    assert beat.disposition == "AI-BUSY"


# ---------------- age gate 45-90 ----------------

def test_age_in_range_transfers(tmp_store, tmp_memory):
    c = make()
    open_gate(c)
    c.hear("Yes I have Medicare")
    beat = c.hear("I'm 65")
    assert beat.end_call is True
    assert beat.disposition == "AI-TRANSFER"
    assert c.ctx.age == 65


@pytest.mark.parametrize("age", [18, 30, 44, 91, 95, 120])
def test_age_out_of_range_is_not_eligible(tmp_store, tmp_memory, age):
    c = make()
    open_gate(c)
    c.hear("Yes I have Medicare")
    beat = c.hear(f"I'm {age}")
    assert beat.end_call is True
    assert beat.disposition == "AI-NOT-ELIGIBLE"
    assert c.ctx.age == age


@pytest.mark.parametrize("age", [45, 90])
def test_age_boundaries_are_inclusive(tmp_store, tmp_memory, age):
    c = make()
    open_gate(c)
    c.hear("Yes I have Medicare")
    beat = c.hear(f"I'm {age}")
    assert beat.disposition == "AI-TRANSFER"


def test_unparseable_age_is_reasked_not_guessed(tmp_store, tmp_memory):
    c = make()
    open_gate(c)
    c.hear("Yes I have Medicare")
    beat = c.hear("I lost track of the years honestly")
    assert beat.end_call is False, "an unparseable age must never be guessed"
    assert c.ctx.age is None
    assert "old are you" in beat.ai_speech.lower()


def test_no_medicare_then_dnc_on_age_still_honored(tmp_store, tmp_memory):
    c = make()
    open_gate(c)
    c.hear("Yes I have Medicare")
    beat = c.hear("remove me from your list")
    assert beat.end_call is True
    assert beat.disposition == "AI-DNC"


def test_transfer_records_learned_memory(tmp_store, tmp_memory):
    c = make()
    open_gate(c)
    c.hear("Yes I have Medicare")
    c.hear("67 years old")
    assert c.ctx.disposition == "AI-TRANSFER"
    assert tmp_memory.insights()["stats"]["total_calls"] >= 1
