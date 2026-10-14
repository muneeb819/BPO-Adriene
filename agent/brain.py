"""AI understanding engine: uses a real LLM to understand the customer in context.

Replaces keyword matching. The LLM reads the conversation so far plus the latest
utterance and returns a structured understanding (intent + entities). Falls back
to an improved semantic heuristic when no LLM provider is reachable, so the bot
still behaves sensibly offline.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .config import Config

# ---------------- cheap offline sentiment (no LLM call) ----------------
# Used for real-time tone matching without adding latency. The richer LLM
# sentiment lives in Understanding; this is the fast path for the voice loop.
_POS = {
    "thank", "thanks", "great", "good", "yes", "sure", "happy", "appreciate",
    "fine", "okay", "ok", "love", "glad", "please", "yeah", "yep", "absolutely",
    "definitely", "wonderful", "nice", "pleasure",
}
_NEG = {
    "no", "not", "never", "hate", "annoyed", "annoying", "waste", "stupid",
    "terrible", "awful", "frustrated", "frustrating", "upset", "unhappy",
    "disappointed", "ridiculous", "sorry", "cant", "can't", "dont", "don't",
}
_ANGRY = {
    "angry", "hate", "scam", "lawsuit", "ridiculous", "stupid", "idiot",
    "take me off", "stop calling", "do not call", "never call", "hang up",
    "fed up", "sick of", "backup", "attorney", "attorney general",
}


def quick_sentiment(text: str) -> str:
    """Fast, local sentiment for tone-matching. Returns positive/neutral/negative/angry."""
    low = (text or "").lower()
    if not low.strip():
        return "neutral"
    if any(p in low for p in _ANGRY):
        return "angry"
    pos = sum(1 for w in _POS if w in low)
    neg = sum(1 for w in _NEG if w in low)
    if pos > neg:
        return "positive"
    if neg > pos:
        return "negative"
    return "neutral"


UNDERSTAND_PROMPT = """You are the understanding engine for an insurance verification phone call.

You read the conversation so far and the customer's latest reply, then return a structured
understanding. You do NOT generate dialogue — you only analyze what the customer said.

CONVERSATION SO FAR:
{transcript}

CUSTOMER JUST SAID:
"{utterance}"

Return ONLY a JSON object (no markdown fences):
{{
  "medicare": "YES" | "NO" | "UNSURE" | "QUESTION" | "PRIVATE_ONLY" | "REFUSE" | "NOT_ASKED",
  "age": <integer 0-120 if stated, else null>,
  "intent": "POSITIVE" | "NEGATIVE" | "UNCLEAR" | "DNC" | "BUSY" | "QUESTION" | "SMALL_TALK",
  "question": "<the customer's question if they asked one, else empty>",
  "sentiment": "positive" | "neutral" | "negative" | "angry",
  "confidence": <0.0-1.0 how clear the medicare answer is>
}}

RULES:
- medicare: only YES/NO if explicit and unambiguous. "maybe"/"i think so"/"not sure" = UNSURE.
  "i have blue cross" (private only, no medicare mention) = PRIVATE_ONLY. "what does that mean" = QUESTION.
  "none of your business" = REFUSE. Do NOT guess medicare from age or small talk.
- age: extract any stated age (e.g. "67", "sixty seven" -> 67, "i am 75 years old" -> 75).
- intent: DNC if they ask to be removed/stop calling. BUSY if they can't talk now.
  QUESTION if they ask what this is about/who you are. SMALL_TALK if just chatting.
- sentiment: angry if hostile/frustrated. negative if unhappy. positive if warm.
- confidence: how sure you are about the medicare value (1.0 for "yes"/"no", 0.4 for "maybe").
"""


@dataclass
class Understanding:
    medicare: str = "NOT_ASKED"   # YES/NO/UNSURE/QUESTION/PRIVATE_ONLY/REFUSE/NOT_ASKED
    age: int | None = None
    intent: str = "UNCLEAR"
    question: str = ""
    sentiment: str = "neutral"
    confidence: float = 0.0
    raw: dict = field(default_factory=dict)


class Brain:
    """Understands the customer using a real LLM, with a semantic fallback."""

    def __init__(self, config: Config, llm):
        self.config = config
        self.llm = llm

    def understand(self, transcript: list[dict], utterance: str) -> Understanding:
        # Prefer the real LLM when it is actually reachable.
        if self._llm_available():
            try:
                return self._understand_llm(transcript, utterance)
            except Exception:
                pass
        return self._understand_fallback(utterance)

    def _llm_available(self) -> bool:
        try:
            return self.llm.models_available()
        except Exception:
            return False

    def _understand_llm(self, transcript: list[dict], utterance: str) -> Understanding:
        turns = "\n".join(
            f"{'Agent' if t['role'] == 'ai' else 'Customer'}: {t['text']}"
            for t in transcript[-10:]
        ) or "(none yet)"
        prompt = UNDERSTAND_PROMPT.format(transcript=turns, utterance=utterance)
        raw = self.llm.chat_json(
            [{"role": "user", "content": prompt}], temperature=0.1
        )
        if not raw:
            return self._understand_fallback(utterance)
        return Understanding(
            medicare=str(raw.get("medicare", "NOT_ASKED")).upper(),
            age=raw.get("age"),
            intent=str(raw.get("intent", "UNCLEAR")).upper(),
            question=raw.get("question", "") or "",
            sentiment=str(raw.get("sentiment", "neutral")).lower(),
            confidence=float(raw.get("confidence", 0) or 0),
            raw=raw,
        )

    # ---------- semantic fallback (no LLM reachable) ----------

    def _understand_fallback(self, utterance: str) -> Understanding:
        low = (utterance or "").lower().strip()
        u = Understanding()

        if not low:
            return u

        # Age first (a number is a number regardless of topic).
        u.age = self._extract_age(low)

        # DNC — highest priority, compliance.
        if re.search(
            r"take me (?:off|out)|remove (?:me|my (?:name|number))|stop calling|don'?t call|do not call|never call|click here|unsubscribe",
            low,
        ):
            u.intent = "DNC"
            u.sentiment = "negative"
            u.confidence = 0.95
            return u

        # Busy.
        if re.search(
            r"\b(?:i'?m|i am|we'?re|we are) (?:busy|at work|in a (?:meeting|call)|driving)|"
            r"can'?t (?:talk|speak|chat) (?:right )?now|call (?:me )?back (?:later|another)|not a good time",
            low,
        ):
            u.intent = "BUSY"
            u.confidence = 0.9
            return u

        # Refuse.
        if re.search(
            r"none of (?:your|the) business|that'?s personal|don'?t want to (?:answer|discuss)|why (?:do you|are you) (?:want to )?(?:know|ask)",
            low,
        ):
            u.medicare = "REFUSE"
            u.sentiment = "angry"
            u.confidence = 0.9
            return u

        # Question about Medicare meaning.
        if re.search(
            r"what (?:do you mean|is (?:it|that|medicare|this)|does (?:that|it|this) mean|are you talking about)|"
            r"what'?s (?:it|that|medicare)|i don'?t (?:know|understand) what",
            low,
        ):
            u.medicare = "QUESTION"
            u.intent = "QUESTION"
            u.confidence = 0.9
            return u

        # Question about who/what (identity).
        if re.search(
            r"who (?:is|are|'?s) (?:this|you|calling)|what'?s? (?:this|it) (?:about|regarding)|"
            r"how did you get my (?:number|name)|is this a (?:scam|robot|real person)",
            low,
        ):
            u.intent = "QUESTION"
            u.question = utterance
            u.confidence = 0.9
            return u

        # Positive Medicare signals (checked before "no" to catch "yes i have it").
        has_medicare_word = "medicare" in low
        yes_signal = bool(re.search(
            r"^(?:yes|yeah|yep|yup|sure|absolutely|definitely|certainly|of course|i do|y|right|correct|ok|okay|mh?m|uh huh)\b|"
            r"\bi (?:do|have|am covered|have (?:it|medicare))\b|"
            r"\b(?:yes|i do)[,;!.]?\s*(?:i|we)?\s*(?:have|am covered)",
            low,
        ))
        no_signal = bool(re.search(
            r"^(?:no|nope|nah|naw|n)\b|"
            r"\b(?:i|we) (?:don['']t|do not|dont|am not|ain['']t) (?:have|covered)\b|"
            r"\bnot (?:really|covered|at all|i know of|that i know)\b|"
            r"\b(?:don['']t|do not|dont) have (?:it|medicare|any)\b",
            low,
        ))
        unsure_signal = bool(re.search(
            r"maybe|i think so|i'?m (?:not sure|unsure|uncertain)|i don'?t (?:know|recall|remember)|"
            r"possibly|probably|i guess|not sure|hard to say|can'?t remember|no idea|kind ?sort of",
            low,
        ))
        private_signal = bool(re.search(
            r"private (?:insurance|plan|coverage|policy)|i have (?:blue cross|aetna|united ?health|cigna|humana|kaiser|a supplement)|"
            r"employer(?:'s)? (?:plan|coverage|insurance)|marketplace|obamacare|supplement(?:al)? (?:plan|insurance)",
            low,
        ))

        # If they only mention private insurance and never say medicare → PRIVATE_ONLY.
        if private_signal and not has_medicare_word:
            u.medicare = "PRIVATE_ONLY"
            u.confidence = 0.85
            return u

        # NO beats YES (so "no i don't have it" is never misread as YES).
        if no_signal:
            u.medicare = "NO"
            u.intent = "NEGATIVE"
            u.confidence = 0.95
            return u
        if yes_signal:
            u.medicare = "YES"
            u.intent = "POSITIVE"
            u.confidence = 0.95
            return u
        if unsure_signal:
            u.medicare = "UNSURE"
            u.confidence = 0.4
            return u

        # Small talk (greeting/thanks/how are you).
        if re.search(
            r"^(?:hi|hello|hey|good morning|good afternoon|fine|good|great|okay|not bad|pretty good|thanks|thank you|i'?m ok)\b",
            low,
        ):
            u.intent = "SMALL_TALK"
            u.medicare = "UNSURE"
            u.confidence = 0.2
            return u

        # A bare number (age-only reply).
        if re.fullmatch(r"\d{1,3}", low) and u.age:
            u.medicare = "NOT_ASKED"
            u.intent = "POSITIVE"
            u.confidence = 0.5
            return u

        u.intent = "UNCLEAR"
        u.medicare = "UNSURE"
        u.confidence = 0.2
        return u

    @staticmethod
    def _extract_age(low: str) -> int | None:
        for pat in (
            r"(\d{2,3})\s*(?:years?\s*)?(?:old|of\s*age)",
            r"\b(?:i['']?m|i\s+am|age(?:d)?(?:\s+is)?)\s+(\d{2,3})",
            r"\b(\d{2,3})\b",
        ):
            m = re.search(pat, low)
            if m:
                try:
                    a = int(m.group(1))
                except ValueError:
                    continue
                if 18 <= a <= 120:
                    return a
        words = {
            "forty five": 45, "forty six": 46, "forty seven": 47, "forty eight": 48,
            "forty nine": 49, "fifty": 50, "fifty one": 51, "fifty two": 52,
            "fifty three": 53, "fifty four": 54, "fifty five": 55, "fifty six": 56,
            "fifty seven": 57, "fifty eight": 58, "fifty nine": 59, "sixty": 60,
            "sixty one": 61, "sixty two": 62, "sixty three": 63, "sixty four": 64,
            "sixty five": 65, "sixty six": 66, "sixty seven": 67, "sixty eight": 68,
            "sixty nine": 69, "seventy": 70, "seventy one": 71, "seventy two": 72,
            "seventy three": 73, "seventy four": 74, "seventy five": 75,
            "seventy six": 76, "seventy seven": 77, "seventy eight": 78,
            "seventy nine": 79, "eighty": 80, "eighty one": 81, "eighty two": 82,
            "eighty three": 83, "eighty four": 84, "eighty five": 85,
            "eighty six": 86, "eighty seven": 87, "eighty eight": 88,
            "eighty nine": 89, "ninety": 90,
        }
        for w in sorted(words, key=len, reverse=True):
            if re.search(rf"\b{w}\b", low):
                return words[w]
        return None
