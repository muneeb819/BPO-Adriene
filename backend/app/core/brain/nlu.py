"""Natural-language understanding for Adriene.

Understands *any* caller reply — paraphrases, slang, typos, hedged answers,
multi-clause answers — instead of exact-match keywords. Pure stdlib, offline,
deterministic (safe for QA), and it consults the self-learning memory so
phrasings learned from feedback are recognised on later calls.

Pipeline:  normalize -> learned-memory lookup -> polarity -> entities -> intent
           -> sentiment -> confidence
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set


# --------------------------------------------------------------------------- #
# normalization
# --------------------------------------------------------------------------- #

_CONTRACTIONS = {
    "i'm": "i am", "i've": "i have", "i'll": "i will", "i'd": "i would",
    "you're": "you are", "you've": "you have", "you'll": "you will", "you'd": "you would",
    "he's": "he is", "she's": "she is", "it's": "it is", "that's": "that is",
    "there's": "there is", "here's": "here is", "what's": "what is",
    "who's": "who is", "where's": "where is", "how's": "how is",
    "we're": "we are", "we've": "we have", "they're": "they are", "they've": "they have",
    "isn't": "is not", "aren't": "are not", "wasn't": "was not", "weren't": "were not",
    "don't": "do not", "doesn't": "does not", "didn't": "did not",
    "can't": "can not", "cannot": "can not", "won't": "will not", "wouldn't": "would not",
    "couldn't": "could not", "shouldn't": "should not", "mustn't": "must not",
    "haven't": "have not", "hasn't": "has not", "hadn't": "had not",
    "ain't": "is not", "let's": "let us",
    "ma'am": "madam", "'re": " are", "'ve": " have", "'ll": " will", "'d": " would",
    "'em": "them", "o'clock": "oclock",
}

_FILLER_WORDS = ("um", "uh", "erm", "hmm", "mm", "uhh", "ah", "eh")

# common speech-to-text / typing slips -> canonical spelling
_TYPO_MAP = {
    "wat": "what", "wot": "what", "whats": "what is", "u": "you", "ur": "your",
    "r": "are", "ya": "you", "yahh": "yeah", "plz": "please", "pls": "please",
    "thx": "thanks", "medicre": "medicare", "medicair": "medicare",
    "medicarere": "medicare", "insurence": "insurance", "suppliment": "supplement",
    "advantge": "advantage", "penalaty": "penalty",
}


def normalize(text: str) -> str:
    """Lowercase, expand contractions, de-hyphenate, fix common typos."""
    t = (text or "").strip().lower()
    t = t.replace("\u2019", "'").replace("\u2018", "'")   # smart quotes
    t = t.replace("\u2014", " - ").replace("\u2013", " - ")  # dashes -> word sep
    for a, b in _CONTRACTIONS.items():
        t = t.replace(a, b)
    # de-spaced contractions like "do not 't" edge cases are already handled above
    t = re.sub(r"[^a-z0-9'?\s.,!%$-]", " ", t)             # drop other symbols
    t = re.sub(r"([a-z])'([a-z])", r"\1\2", t)             # leftover ' (its, im)
    t = re.sub(r"(?<=[a-z])-(?=[a-z])", " ", t)            # uh-huh, sixty-seven
    t = re.sub(r"\s+", " ", t).strip()
    # typo corrections (word-level) — note: filler words ("um", "uh") are NOT
    # stripped here, they'd eat backchannels like "uh-huh" / "mm-hmm"
    words = [_TYPO_MAP.get(w, w) for w in t.split()]
    return " ".join(words)


# --------------------------------------------------------------------------- #
# polarity: yes / no / unsure — phrase-first so context beats single words
# --------------------------------------------------------------------------- #

# order matters: longer / more specific phrases first
_UNCERTAIN_PHRASES = [
    "not sure", "don not know", "do not know", "dont know", "unsure", "uncertain",
    "not certain", "not certain about", "i think so", "i guess", "i suppose",
    "maybe", "might be", "might have", "probably", "possibly", "could be",
    "hard to say", "not really sure", "no idea", "i have no idea", "who knows",
    "sort of", "kind of", "kinda", "sorta", "not really clear", "off and on",
    "i believe so", "as far as i know", "i suppose so", "what do you mean",
]
_DENY_PHRASES = [
    "definitely not", "absolutely not", "certainly not", "of course not",
    "no way", "not at all", "not really", "nope", "nah", "nahh", "no thank you",
    "no thanks", "i do not", "i do not have", "i donot have", "do not have",
    "do not", "did not", "never had", "never been", "not anymore", "i refuse",
    "not interested in", "negative", "not me", "i am not", "not on medicare",
    "no i do not", "nope i do not", "i ain't got", "ain't got", "ain't have",
]
_AFFIRM_PHRASES = [
    "absolutely yes", "yes absolutely", "of course", "certainly", "definitely",
    "you bet", "you know it", "sure thing", "sure do", "sure does", "yes i do",
    "yes i have", "yeah i do", "yeah i have", "i do", "i have it", "i have got it",
    "i got it", "i have medicare", "i am on medicare", "on medicare since",
    "yep", "yup", "yepp", "yah", "yeah", "aye", "uh huh", "mm hmm", "mhm",
    "okay", "ok", "alright", "right", "correct", "true", "affirmative",
    "yes", "sure", "indeed", "yep i do", "glad to", "happy to", "absolutely",
    # long-form affirmations ("I've had it for years")
    "have had it", "have had", "for years", "years now", "i have had medicare",
]
_AFFIRM_FIRST = ["yes"]  # bare "yes" checked after longer phrases

_AFFIRM_PHRASES = _AFFIRM_PHRASES + _AFFIRM_FIRST


def polarity(norm: str) -> tuple[Optional[bool], str]:
    """Return (True=affirm, False=deny, None=unknown, source)."""
    t = f" {norm} "
    for p in _UNCERTAIN_PHRASES:
        if p in t:
            return None, "uncertain"
    # negation-scoped: "not 65 yet" / "not on medicare" etc.
    for p in _DENY_PHRASES:
        if p in t:
            return False, "deny"
    # bare "no" as its own word ("nope"/"not" don't match \b no \b)
    if re.search(r"\bno\b", t):
        return False, "deny-bare"
    # scoped negation — but never when "not" belongs to "can not" (afford...)
    if re.search(r"(?<!can )\bnot\b", t) and re.search(
        r"\b(have|on|yet|no|am|really|currently)\b", t
    ) and not re.search(r"\bnot sure|not now|can not afford\b", t):
        # e.g. "not on it yet", "not 65 yet", "I am not"
        return False, "deny-scoped"
    for p in _AFFIRM_PHRASES:
        if p in t:
            return True, "affirm"
    if re.search(r"\byes\b", t):
        return True, "affirm"
    return None, "none"


# --------------------------------------------------------------------------- #
# entities
# --------------------------------------------------------------------------- #

_NUM_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
    "eighty": 80, "ninety": 90,
}
_DECADE_WORDS = {
    "fifties": 55, "sixties": 65, "seventies": 75, "eighties": 85,
    "mid sixties": 65, "early sixties": 62, "late sixties": 68,
    "mid seventies": 75, "early seventies": 72, "late seventies": 78,
}


def extract_age(norm: str) -> Optional[Dict]:
    """Age entity: digits, word numbers, decade phrases, thresholds."""
    # decade phrase first ("in my sixties")
    for phrase, approx in _DECADE_WORDS.items():
        if phrase in norm:
            return {"age": approx, "approx": True, "raw": phrase}
    # "65 and a half"
    m = re.search(r"\b(\d{1,3})\s+and\s+a\s+half\b", norm)
    if m and 18 <= int(m.group(1)) <= 120:
        return {"age": int(m.group(1)), "approx": False, "raw": m.group(0)}
    # thresholds before bare digits: "over 65" means 66-ish, not 65
    m = re.search(r"\b(over|above|older than|at least|past)\s*(\d{1,3})\b", norm)
    if m and 18 <= int(m.group(2)) <= 120:
        return {"age": int(m.group(2)) + 1, "approx": True, "raw": m.group(0)}
    m = re.search(r"\b(under|below|younger than|less than)\s*(\d{1,3})\b", norm)
    if m and 18 <= int(m.group(2)) <= 120:
        return {"age": int(m.group(2)) - 1, "approx": True, "raw": m.group(0)}
    # plain digits near an age context (avoid phone numbers: 10-11 digits)
    digits = re.findall(r"\b(\d{1,3})\b", norm)
    ctx = re.search(
        r"(\d{1,3})\s*(?:years old|year old|yrs|yr|years|yo\b|'|s\b)|"
        r"(?:age|ages|age of|aged|i am|i'm|im|being)\D{0,12}(\d{1,3})|"
        r"\b(\d{1,3})\b(?=\s*(?:,|\.|$))",
        norm,
    )
    if ctx:
        val = next(g for g in ctx.groups() if g)
        age = int(val)
        if 18 <= age <= 120:
            return {"age": age, "approx": False, "raw": val}
    # word-number age: "sixty seven" (normalize turns sixty-seven into spaces)
    m = re.search(
        r"(twenty|thirty|forty|fifty|sixty|seventy|eighty)\s*(one|two|three|four|"
        r"five|six|seven|eight|nine)?", norm,
    )
    if m:
        base = _NUM_WORDS[m.group(1)]
        unit = _NUM_WORDS.get(m.group(2) or "", 0)
        age = base + unit
        if 18 <= age <= 120:
            return {"age": age, "approx": False, "raw": m.group(0)}
    if digits and re.search(r"\bold\b|\bage\b|\byoung\b", norm):
        age = int(digits[0])
        if 18 <= age <= 120:
            return {"age": age, "approx": True, "raw": digits[0]}
    return None


_PLAN_TERMS = {
    "medicare advantage": "advantage", "part c": "advantage", "ma plan": "advantage",
    "advantage plan": "advantage", "hmo": "advantage", "ppo plan": "advantage",
    "medigap": "medigap", "medicare supplement": "medigap", "supplement": "medigap",
    "part a": "part_a", "part b": "part_b", "part d": "part_d",
    "drug plan": "part_d", "prescription plan": "part_d", "rx plan": "part_d",
    "medicaid": "medicaid", "dual eligible": "dual", "qualifying low income": "lis",
    "extra help": "lis", "low income subsidy": "lis",
    "tricare": "tricare", "va benefits": "va", "veteran": "va",
    "employer plan": "employer", "group plan": "employer", "through work": "employer",
    "retiree plan": "employer", "marketplace": "marketplace", "obamacare": "marketplace",
    "commercial insurance": "marketplace", "private insurance": "marketplace",
}


def extract_plan(norm: str) -> Optional[str]:
    for phrase, key in _PLAN_TERMS.items():
        if phrase in norm:
            return key
    return None


_NAME_RE = re.compile(
    r"(?:my name is|this is|i am|i'm|its|it's)\s+([a-z][a-z' -]{1,30}?)(?:\s+(?:speaking|here|calling|"
    r"\d|,|\.|$))"
)


def extract_name(norm: str) -> Optional[str]:
    m = _NAME_RE.search(norm)
    if not m:
        return None
    name = m.group(1).strip()
    stop = {"speaking", "here", "calling", "the", "a", "not", "fine", "okay", "ok", "good"}
    if name.split()[0] in stop or len(name) < 2:
        return None
    return name.title()


def extract_time_ref(norm: str) -> Optional[str]:
    for pat in (
        r"\btomorrow\b", r"\btonight\b", r"\bthis evening\b", r"\bthis afternoon\b",
        r"\bnext week\b", r"\bnext month\b", r"\bafter \d{1,2}(:\d{2})?\s*(am|pm)?\b",
        r"\b(?:in|at) \d{1,2}(?::\d{2})?\s*(am|pm)\b", r"\bmonday\b|\btuesday\b|"
        r"\bwednesday\b|\bthursday\b|\bfriday\b|\bsaturday\b|\bsunday\b",
    ):
        m = re.search(pat, norm)
        if m:
            return m.group(0)
    return None


# --------------------------------------------------------------------------- #
# intents
# --------------------------------------------------------------------------- #

# (intent, patterns) — evaluated in order; first match with a hit wins.
_INTENT_RULES: List[tuple] = [
    ("dnc", [
        "take me off", "remove my number", "stop calling", "do not call",
        "don not call", "delete my number", "opt me out", "opt out", "no more calls",
        "never call me", "call list", "remove me", "quit calling", "stop calling me",
    ]),
    ("callback", [
        "call me later", "call me back", "call back", "call me tomorrow",
        "not a good time", "bad time", "busy right now", "in a meeting",
        "another time", "some other time", "try later", "later today",
        "can you call", "ring me", "call again", "half an hour", "in an hour",
        "not right now", "now is not", "in the middle of", "at work right now",
        "get back to me", "give me a call",
    ]),
    ("human_request", [
        "real person", "human being", "actual person", "live person",
        "speak to someone", "speak with someone", "talk to somebody",
        "representative", "agent please", "another person", "real human",
        "transfer me", "put me through", "connect me", "operator",
    ]),
    ("not_interested", [
        "not interested", "no interest", "i am good", "im good", "not right now",
        "already have an agent", "have an agent", "happy with my current",
        "not looking for", "i will pass", "i pass", "leave me alone",
        "do not need", "do not want", "not for me", "no need", "not buying",
    ]),
    ("complaint", [
        "ridiculous", "waste of time", "annoying", "angry", "unacceptable",
        "terrible", "worst", "frustrat", "upset", "sick of", "tired of",
        "harassing", "harassment", "lawsuit", "illegal", "creepy",
    ]),
    ("identity_query", [
        "who is this", "who is that", "who's this", "who am i talking to",
        "who is calling", "who are you", "what company", "what is this company",
        "are you a robot", "are you human", "is this a robot", "am i talking to a bot",
        "is this a scam", "scam", "spam", "robocall", "telemarketer",
        "how did you get my number", "where did you get", "how do you know me",
        "is this real", "are you real", "what is this about", "what is regarding",
        "what's this regarding", "why are you calling", "why did you call",
        "what do you want", "what are you selling", "what is the purpose",
        "what is this",
    ]),
    ("language", [
        "spanish", "espanol", "español", "french", "tagalog", "vietnamese",
        "chinese", "korean", "speak in", "in spanish", "interpret",
    ]),
    ("thanks", ["thank you", "thanks", "appreciate", "much obliged", "god bless"]),
    ("farewell", ["goodbye", "good bye", "bye", "gotta go", "have to go", "hang up",
                  "end the call", "see you", "take care now", "i am done", "enough"]),
    ("question_invite", [
        "i have a question", "can i ask", "quick question", "i want to ask",
        "let me ask", "can you explain", "what does that mean", "why do you ask",
        "i need to ask", "before we go on", "hold on i have",
    ]),
    ("confused", [
        "i am confused", "do not understand", "dont understand", "what do you mean",
        "come again", "say that again", "pardon", "did not catch", "huh",
        "what was that", "i do not follow", "lost me", "unclear", "explain",
    ]),
    ("greeting", ["hello", "hi there", "hey", "hi", "good morning", "good afternoon",
                  "good evening", "howdy", "yo", "morning", "afternoon", "evening"]),
    ("smalltalk", [
        "how are you", "how is it going", "how you doing", "i am fine", "i am good",
        "not much", "just here", "nothing much", "pretty good", "and you",
        "doing well", "same here", "long day",
    ]),
    ("consent_continue", [
        "go ahead", "i am listening", "listening", "tell me more", "go on",
        "what is this regarding", "sure what", "what about", "what is it",
        "i am curious", "let us hear",
    ]),
]

_KNOWLEDGE_HINTS = [
    "medicare", "part a", "part b", "part c", "part d", "medigap", "supplement",
    "advantage", "enroll", "enrollment", "premium", "deductible", "copay",
    "penalty", "medicaid", "extra help", "low income", "open enrollment",
    "irmaa", "coverage", "prescription", "drug", "network", "hospital",
    "doctor", "what is", "how does", "when can", "eligib", "disability",
    "turning 65", "at 65", "before 65", "donut hole", "coverage gap",
]


@dataclass
class Utterance:
    raw: str
    norm: str
    intent: str = "unknown"
    polarity: Optional[bool] = None
    polarity_source: str = "none"
    confidence: float = 0.0
    entities: Dict = field(default_factory=dict)
    sentiment: str = "neutral"          # positive | neutral | negative | frustrated
    flags: Set[str] = field(default_factory=set)   # question, learned, barge_hint
    knowledge_hits: List[str] = field(default_factory=list)
    learned: Optional[Dict] = None


def _score_intent(norm: str) -> tuple[str, int]:
    best, best_hits = "unknown", 0
    for intent, patterns in _INTENT_RULES:
        hits = sum(1 for p in patterns if p in norm)
        if hits > best_hits:
            best, best_hits = intent, hits
        if best_hits >= 2:
            break
    return best, best_hits


def _sentiment(norm: str) -> str:
    if any(w in norm for w in (
        "ridiculous", "annoy", "angry", "waste of time", "sick of", "harass",
        "stupid", "idiot", "shut up", "damn", "hell", "unacceptable", "furious",
    )):
        return "frustrated"
    if any(w in norm for w in ("upset", "sad", "worried", "nervous", "scared",
                               "confused", "concern", "unfortunately", "bad day")):
        return "negative"
    if any(w in norm for w in ("great", "wonderful", "thanks", "thank you", "good",
                               "happy", "glad", "appreciate", "love", "perfect")):
        return "positive"
    return "neutral"


def analyze(text: str, memory=None) -> Utterance:
    """Full understanding pass over one caller utterance."""
    raw = text or ""
    norm = normalize(raw)
    ut = Utterance(raw=raw, norm=norm)

    # ---- self-learning memory first: known phrasings win -------------------
    if memory is not None:
        learned = memory.match(norm)
        if learned:
            ut.learned = learned
            ut.flags.add("learned")
            ut.intent = learned.get("intent", "unknown")
            if learned.get("polarity") is not None:
                ut.polarity = bool(learned["polarity"])
                ut.polarity_source = "learned"
            ut.confidence = 0.95

    # ---- polarity ---------------------------------------------------------
    if ut.polarity_source == "none":
        ut.polarity, ut.polarity_source = polarity(norm)
        if ut.polarity is not None:
            ut.confidence = max(ut.confidence, 0.9)

    # ---- entities ---------------------------------------------------------
    age = extract_age(norm)
    if age:
        ut.entities["age"] = age
        ut.confidence = max(ut.confidence, 0.85)
    plan = extract_plan(norm)
    if plan:
        ut.entities["plan"] = plan
        ut.confidence = max(ut.confidence, 0.85)
    nm = extract_name(norm)
    if nm:
        ut.entities["name"] = nm
    when = extract_time_ref(norm)
    if when:
        ut.entities["time_ref"] = when

    # ---- intent -----------------------------------------------------------
    intent, hits = _score_intent(norm)
    if ut.intent == "unknown":          # memory didn't decide it
        ut.intent = intent
    if hits:
        ut.confidence = max(ut.confidence, 0.75 + 0.1 * min(hits, 2))

    # ---- knowledge questions (intent = knowledge_question + topic hints) ---
    khits = [k for k in _KNOWLEDGE_HINTS if k in norm]
    if not khits:                     # typo-tolerant: "medicre" ~ "medicare"
        for tok in norm.split():
            if len(tok) < 5:
                continue
            close = difflib.get_close_matches(tok, _KNOWLEDGE_HINTS, n=1, cutoff=0.84)
            if close:
                khits.append(close[0])
    ut.knowledge_hits = khits
    if khits and ut.intent in ("unknown", "consent_continue", "greeting",
                               "smalltalk", "question_invite"):
        ut.intent = "knowledge_question"
        ut.confidence = max(ut.confidence, 0.7 + 0.05 * min(len(khits), 3))
    elif khits and ut.intent == "knowledge_question":
        ut.confidence = max(ut.confidence, 0.8)

    # coverage answers without explicit yes/no: "I have a plan through work"
    if ut.intent == "unknown" and ut.entities.get("plan") and ut.polarity is None:
        ut.intent = "coverage_answer"
        ut.polarity = ut.entities["plan"] in ("advantage", "medigap", "part_a",
                                              "part_b", "part_d")
        ut.polarity_source = "plan-context"
        ut.confidence = 0.8

    # ---- flags / sentiment ------------------------------------------------
    if "?" in raw or norm.startswith(("what", "who", "why", "when", "where",
                                      "how", "is this", "does", "do i", "can i",
                                      "are there", "will i")):
        ut.flags.add("question")
    if re.search(r"\bwait\b|\bhold on\b|\bactually\b|\bbut\b", norm):
        ut.flags.add("interjection")
    ut.sentiment = _sentiment(norm)

    # ---- final confidence -------------------------------------------------
    if ut.intent == "unknown" and ut.polarity is None and not ut.entities:
        ut.confidence = 0.25
    elif ut.confidence == 0.0:
        ut.confidence = 0.5
    return ut


def fuzzy_equals(a: str, b: str, threshold: float = 0.88) -> bool:
    """Used by the memory to recognise re-typed phrasings."""
    return difflib.SequenceMatcher(None, a, b).ratio() >= threshold
