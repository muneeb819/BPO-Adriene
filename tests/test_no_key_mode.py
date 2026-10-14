"""No-key mode — the configuration the project ships in.

With the LLM API section blank (as delivered), generation must fall back to
the compliance-correct script rather than the offline mock's guesses. A wrong
guess here would be spoken to a live customer.
"""
from __future__ import annotations

import pytest

from agent.campaign import get_store
from agent.config import Config
from agent.conversation import Conversation
from agent.mock_llm import MockLLM


def make(cid: str = "medicare") -> Conversation:
    return Conversation(Config.load(), MockLLM(Config.load()),
                        campaign=get_store().get(cid))


def test_mock_llm_is_treated_as_no_model():
    assert MockLLM(Config.load()).models_available() is False
    assert make()._llm_ready is False


def test_no_key_greeting_is_the_real_greeting(tmp_store, tmp_memory):
    line = make().start()
    assert "How are you today?" in line, "must greet, not emit a generic line"
    assert "Lina" in line and "EverTrust" in line
    assert "recorded" in line, "disclosure must still be spoken"


def test_no_key_walks_the_whole_gate(tmp_store, tmp_memory):
    c = make()
    c.start()
    asked = c.hear("Doing well, thanks")
    assert "medicare" in asked.ai_speech.lower()

    age = c.hear("Yes I have Medicare")
    assert "how old are you" in age.ai_speech.lower(), "must advance to age"

    done = c.hear("I'm 67")
    assert done.end_call and done.disposition == "AI-TRANSFER"


def test_no_key_never_repeats_the_same_question(tmp_store, tmp_memory):
    """Regression: the mock used to answer every turn with the Medicare line."""
    c = make()
    c.start()
    lines = [c.hear("Sure, hello there").ai_speech]
    lines.append(c.hear("Yes I have it").ai_speech)
    lines.append(c.hear("I'm 72").ai_speech)
    assert len(set(lines)) == len(lines), f"repeated lines: {lines}"


def test_no_key_disqualification_is_polite(tmp_store, tmp_memory):
    c = make()
    c.start()
    c.hear("hello there")
    beat = c.hear("No I don't have Medicare")
    assert beat.end_call and beat.disposition == "AI-NO-MEDICARE"
    assert beat.ai_speech and "medicare?" not in beat.ai_speech.lower()


def test_no_key_has_no_error_text_in_any_line(tmp_store, tmp_memory):
    c = make()
    assert "[LLM error" not in c.start()
    for text in ("hello", "yes I do", "sixty five"):
        line = c.hear(text).ai_speech or ""
        assert "[LLM error" not in line
        assert "{name}" not in line


@pytest.mark.parametrize("cid", ["medicare", "final_expense", "aca", "hi"])
def test_no_key_works_for_every_campaign(tmp_store, tmp_memory, cid):
    c = make(cid)
    greeting = c.start()
    assert greeting
    first = c.hear("Sure, go ahead")
    assert first.ai_speech, f"{cid} must ask its first question with no key"
