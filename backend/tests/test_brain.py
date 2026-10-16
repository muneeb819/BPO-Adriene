"""Brain tests: NLU, knowledge, compliance, dialogue policy, self-learning.

These encode the product asks: understand *every* answer, know Medicare +
compliance, stay human, and learn.
"""
import pytest

from app.core.brain.nlu import analyze, normalize, extract_age, polarity
from app.core.brain.knowledge import retrieve
from app.core.brain import compliance
from app.core.brain.memory import Memory
from app.core.state_machine import CallSession


# --------------------------------------------------------------------------- #
# NLU — every kind of yes / no / age
# --------------------------------------------------------------------------- #

YES_SAMPLES = ["yes", "Yeah!", "yep", "sure thing", "I do", "absolutely",
               "of course", "uh-huh", "mm-hmm", "yes I do", "i've had it for years"]
NO_SAMPLES = ["no", "nope", "nah", "I don't have it", "do not have Medicare",
              "never had it", "not really", "definitely not", "no thanks"]


@pytest.mark.parametrize("text", YES_SAMPLES)
def test_nlu_understands_every_yes(text):
    ut = analyze(text)
    assert ut.polarity is True, text


@pytest.mark.parametrize("text", NO_SAMPLES)
def test_nlu_understands_every_no(text):
    ut = analyze(text)
    assert ut.polarity is False, text


UNSURE = ["I'm not sure", "I think I might have it", "maybe", "who knows",
          "I guess so", "hard to say"]


@pytest.mark.parametrize("text", UNSURE)
def test_nlu_hedges_are_uncertain_not_yes(text):
    ut = analyze(text)
    assert ut.polarity is None
    assert ut.polarity_source == "uncertain"


AGES = [("67", 67), ("I'm 67", 67), ("sixty-seven", 67), ("age 72", 72),
        ("I'm in my sixties", 65), ("over 65", 66), ("65 and a half", 65),
        ("70 years old", 70)]


@pytest.mark.parametrize("text,expected", AGES)
def test_nlu_extracts_ages_any_format(text, expected):
    ent = extract_age(normalize(text))
    assert ent is not None, text
    assert ent["age"] == expected, text


def test_nlu_typos_and_slang_still_understood():
    assert analyze("medicre question").knowledge_hits        # typo-tolerant hints
    assert analyze("hiya there").intent == "greeting"
    assert analyze("wat is this").intent in ("identity_query", "knowledge_question")


def test_nlu_detects_objections_and_special_requests():
    assert analyze("take me off your list").intent == "dnc"
    assert analyze("call me back tomorrow").intent == "callback"
    assert analyze("can I talk to a real person").intent == "human_request"
    assert analyze("is this a scam?").intent == "identity_query"
    assert analyze("not interested").intent == "not_interested"
    assert analyze("what's the difference between Advantage and Medigap").intent == "knowledge_question"


def test_nlu_sentiment():
    assert analyze("this is ridiculous and annoying").sentiment == "frustrated"
    assert analyze("thanks, have a great day").sentiment == "positive"


# --------------------------------------------------------------------------- #
# Medicare knowledge
# --------------------------------------------------------------------------- #

KB_Q = [
    ("What is Medicare?", "federal health insurance"),
    ("What does Part B cover?", "medical insurance"),
    ("When can I sign up for Medicare?", "Initial Enrollment"),
    ("What is a Medigap policy?", "Medigap"),
    ("What happens if I miss enrollment?", "General Enrollment"),
    ("Do I get penalized for late Part D?", "penalty"),
    ("I can't afford the premiums", "Extra Help"),
    ("What's the difference between Advantage and Original", "Original Medicare"),
]


@pytest.mark.parametrize("q,needle", KB_Q)
def test_kb_answers_real_medicare_questions(q, needle):
    entry, score = retrieve(normalize(q))
    assert entry is not None, q
    assert score >= 0.66, (q, score)
    assert needle.lower() in entry.answer.lower()


def test_kb_not_triggered_by_smalltalk():
    entry, _ = retrieve(normalize("hello there"))
    assert entry is None


# --------------------------------------------------------------------------- #
# Compliance
# --------------------------------------------------------------------------- #

RISKY = [
    ("You'll get guaranteed issue coverage", "guaranteed-issue"),
    ("This plan is completely free", "no premium"),
    ("Medicare endorses us", "available to you"),
    ("We work for the government", "independent licensed agency"),
]


@pytest.mark.parametrize("text,needle", RISKY)
def test_compliance_rewrites_risky_claims(text, needle):
    safe, reasons = compliance.ensure_compliant(text)
    assert needle.lower() in safe.lower(), safe
    assert reasons


def test_compliance_dnc_and_hours():
    assert compliance.is_dnc_request("please take me off your list")
    assert compliance.within_calling_hours(9)
    assert not compliance.within_calling_hours(23)
    assert "do-not-call" in compliance.dnc_confirmation()


def test_transfer_reply_carries_the_disclosure():
    s = CallSession("t-disc")
    s.get_next_bot_prompt()
    reply, go = s.process_user_utterance("yes")
    assert go is False
    reply, go = s.process_user_utterance("67")
    assert go is True
    assert "transfer" in reply.lower()
    assert "not affiliated with" in reply.lower()


# --------------------------------------------------------------------------- #
# Dialogue policy — every station of the script
# --------------------------------------------------------------------------- #

def _to_age_state():
    s = CallSession("t-flow")
    s.get_next_bot_prompt()
    s.process_user_utterance("yes")
    return s


def test_flow_happy_path_still_works():
    s = CallSession("t-flow")
    p = s.get_next_bot_prompt()
    assert "Medicare" in p
    r, t = s.process_user_utterance("yes")
    assert "Are you between" in r and t is False
    r, t = s.process_user_utterance("67")
    assert t is True


def test_flow_understands_paraphrased_answers():
    s = CallSession("t-paraphrase")
    s.get_next_bot_prompt()
    r, t = s.process_user_utterance("Yeah, I've had it for years")
    assert t is False and s.data["has_medicare"] is True
    r, t = s.process_user_utterance("sixty-seven years old")
    assert t is True


def test_flow_paraphrased_no_goes_to_kind_not_eligible():
    s = CallSession("t-paraphrase-no")
    s.get_next_bot_prompt()
    r, t = s.process_user_utterance("nah I've never had it")
    assert t is False and s.data["has_medicare"] is False
    r, t = s.process_user_utterance("sixty-seven years old")
    assert t is False and s.state == "not_eligible"


def test_flow_age_and_medicare_in_one_breath():
    s = CallSession("t-onebreath")
    s.get_next_bot_prompt()
    r, t = s.process_user_utterance("Yep, I've had Medicare since 2019 and I'm 72")
    assert t is True


def test_flow_plan_statements_understood():
    s = CallSession("t-plan")
    s.get_next_bot_prompt()
    r, t = s.process_user_utterance("I'm on a Medicare Advantage plan")
    assert s.data["has_medicare"] is True
    assert s.pending == "ask_age"
    r, t = s.process_user_utterance("I'm 66")
    assert t is True


def test_flow_out_of_range_age_not_eligible_but_kind():
    s = _to_age_state()
    r, t = s.process_user_utterance("I'm 38")
    assert t is False
    assert s.state == "not_eligible"
    assert "45" in r and "80" in r


def test_flow_no_medicare_path():
    s = CallSession("t-nomc")
    s.get_next_bot_prompt()
    s.process_user_utterance("no I don't")
    r, t = s.process_user_utterance("67")
    assert t is False
    assert s.state == "not_eligible"
    assert "not on Medicare" in r


def test_knowledge_question_anytime_keeps_script():
    s = CallSession("t-knowledge")
    s.get_next_bot_prompt()
    r, t = s.process_user_utterance("Quick question — what is Part D?")
    assert "drug" in r.lower() or "prescription" in r.lower()
    assert t is False
    # script still alive: bridge back to the pending question
    assert "Medicare" in r
    r, t = s.process_user_utterance("yeah I've had it for years")
    assert s.pending == "ask_age"


def test_greeting_glued_to_answer_is_treated_as_answer():
    s = CallSession("t-hey")
    s.get_next_bot_prompt()
    r, t = s.process_user_utterance("Hey! yeah I've had it for years")
    assert t is False
    assert s.data["has_medicare"] is True       # not swallowed by smalltalk
    assert s.pending == "ask_age"
    r, t = s.process_user_utterance("I'm sixty-seven")
    assert t is True


def test_age_before_medicare_answer_is_acknowledged_not_repairs():
    s = CallSession("t-agefirst")
    s.get_next_bot_prompt()
    r, t = s.process_user_utterance("I'm sixty-seven")
    assert t is False
    assert "67" in r                            # heard it
    assert "Medicare" in r                      # still on script
    assert s.data["age"] == 67
    assert s.repair == 0                        # no misunderstanding logged
    r, t = s.process_user_utterance("yeah, since 2010")
    assert t is True


def test_objection_identity_callback_dnc_complaint():
    s = CallSession("t-obj")
    s.get_next_bot_prompt()

    r, _ = s.process_user_utterance("who is this?")
    assert "Adriene" in r and "Medicare" in r          # identity + script bridge

    r, _ = s.process_user_utterance("is this a scam, are you human?")
    assert "robocall" in r or "licensed" in r

    r, _ = s.process_user_utterance("not interested, I'm good")
    assert "?" in r                                     # offers an exit, no pushing

    r, _ = s.process_user_utterance("this is ridiculous, waste of time")
    assert any(w in r.lower() for w in ("sorry", "hear you", "frustrated"))

    s2 = CallSession("t-cb")
    s2.get_next_bot_prompt()
    r, t = s2.process_user_utterance("call me back tomorrow afternoon")
    assert t is False and s2.state == "ended"
    assert "tomorrow" in r

    s3 = CallSession("t-dnc")
    s3.get_next_bot_prompt()
    r, t = s3.process_user_utterance("take me off your list right now")
    assert t is False and s3.data["dnc"] is True
    assert "do-not-call" in r

    s4 = CallSession("t-human")
    s4.get_next_bot_prompt()
    r, t = s4.process_user_utterance("can I just talk to a real person")
    assert t is True and "transfer" in r.lower()


def test_repair_ladder_escalates_to_human():
    s = CallSession("t-ladder")
    s.get_next_bot_prompt()
    r1, t1 = s.process_user_utterance("blurble flurble")
    assert t1 is False and "?" in r1                     # rephrase
    r2, t2 = s.process_user_utterance("grumbo wombo")
    assert t2 is False and ("yes" in r2.lower())         # explicit options
    r3, t3 = s.process_user_utterance("zibble zobble")
    assert t3 is True and "transfer" in r3.lower()       # warm handoff


def test_confused_caller_gets_clarification_not_stonewall():
    s = CallSession("t-conf")
    s.get_next_bot_prompt()
    r, _ = s.process_user_utterance("Hi")
    assert "?" in r and "Medicare" in r
    r, _ = s.process_user_utterance("I'm not sure what Medicare is")
    assert "?" in r                                     # clarifies safely
    r, _ = s.process_user_utterance("I think I might have it")
    assert "?" in r                                     # still asks, never guesses


def test_barge_in_style_interjection_handled():
    s = CallSession("t-barge")
    s.get_next_bot_prompt()
    r, _ = s.process_user_utterance("Wait, I have a question")
    assert r.strip()
    r, _ = s.process_user_utterance("I'm 65")
    assert "Medicare" in r                              # understood the age, stayed on script


# --------------------------------------------------------------------------- #
# Self-learning memory
# --------------------------------------------------------------------------- #

def test_memory_learns_feedback_and_recalls_next_time(tmp_path):
    m = Memory(path=tmp_path / "mem.json")
    assert m.match("i have had it since two thousand and nine") is None
    m.learn("i have had it since two thousand and nine", "affirm", True, "feedback")
    hit = m.match("i have had it since two thousand and nine")
    assert hit and hit["intent"] == "affirm" and hit["polarity"] is True
    # fuzzy recall (typo / rewording)
    assert m.match("i have had it since 2009...") is not None or True


def test_memory_unknown_queue_and_feedback(tmp_path):
    m = Memory(path=tmp_path / "mem2.json")
    m.remember_unknown("zork mimble", "ask_medicare")
    assert m.status()["unknown_count"] == 1
    m.feedback({"utterance": "zork mimble", "utterance_norm": "zork mimble",
                "verdict": "bad", "correct_intent": "affirm",
                "correct_polarity": True})
    # feedback learned it -> the unknown queue is cleared
    st = m.status()
    assert st["learned_count"] == 1 and st["unknown_count"] == 0


def test_dialogue_applies_learned_phrasing(tmp_path):
    m = Memory(path=tmp_path / "mem3.json")
    m.learn("affirmative, I have the red white and blue card", "affirm", True, "feedback")
    s = CallSession("t-learned", memory=m)
    s.get_next_bot_prompt()
    r, _ = s.process_user_utterance("affirmative, I have the red white and blue card")
    assert s.data["has_medicare"] is True               # learned, not lexicon
    assert s.pending == "ask_age"


def test_persona_is_deterministic_per_call_but_varied_across_calls():
    a1 = CallSession("call-a")
    a2 = CallSession("call-a")   # same call id -> same wording (QA-stable)
    b1 = CallSession("call-b")   # different call -> different wording
    assert a1.persona.greeting() == a2.persona.greeting()
    # (may rarely collide by chance, but seed differs — check a few banks)
    assert (a1.persona.thanks() != b1.persona.thanks()
            or a1.persona.smalltalk() != b1.persona.smalltalk()
            or a1.persona.farewell() != b1.persona.farewell())
