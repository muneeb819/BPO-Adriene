"""Self-learning memory: the bot gets smarter the more calls it handles.

Every finished call is stored with its outcome and a quality score. On new calls
the brain retrieves similar past exchanges -- weighted toward the ones that led
to a clean, successful outcome -- and injects them into the LLM prompt as
few-shot examples. Over time the bot preferentially imitates phrasings and
answers that actually worked, and mines its own history for failure patterns.

This is a *retrieval + outcome-weighting* learning loop. It is honest,
local, and needs no training infrastructure -- the bot genuinely improves with use.
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

_WORD = re.compile(r"[a-z']+")
_STOP = {
    "a", "an", "the", "i", "im", "you", "your", "to", "of", "and", "or", "is",
    "are", "am", "it", "that", "this", "in", "on", "for", "do", "does", "have",
    "has", "was", "were", "be", "my", "me", "we", "they", "at", "so", "if", "not",
}


def _tokens(text: str) -> list[str]:
    return [w for w in _WORD.findall((text or "").lower()) if w not in _STOP]


# How good a given outcome was, for weighting examples the bot learns from.
# A clean transfer is the ideal; a graceful rule-following close is still correct
# handling; AI-UNCLEAR means the bot got confused (least useful to imitate).
_QUALITY = {
    "AI-TRANSFER": 1.0,
    "AI-NO-MEDICARE": 0.7,
    "AI-MEDICARE-REFUSED": 0.7,
    "AI-BUSY": 0.7,
    "AI-DNC": 0.7,
    "AI-NOT-ELIGIBLE": 0.6,
    "AI-NOT-DECISION-MAKER": 0.6,
    "AI-UNCLEAR": 0.25,
}


@dataclass
class CallRecord:
    ts: str
    outcome: str
    quality: float
    medicare: str
    age: int | None
    sentiment: str
    ended_at_beat: str
    duration_turns: int
    turns: list[dict] = field(default_factory=list)


class CallMemory:
    """Persistent, outcome-weighted conversational memory (shared across calls)."""

    def __init__(self, path: str = "data/memory.json"):
        self.path = path
        self._lock = threading.Lock()
        self.calls: list[dict] = []
        self._idf: dict[str, float] = {}
        self._load()

    # ---------------- persistence ----------------

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self.calls = json.load(f).get("calls", [])
        except (OSError, ValueError):
            self.calls = []
        self._rebuild_idf()

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"calls": self.calls}, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def _rebuild_idf(self):
        df: Counter[str] = Counter()
        docs = 0
        for call in self.calls:
            for t in call.get("turns", []):
                if t.get("role") != "customer":
                    continue
                docs += 1
                for w in set(_tokens(t.get("text", ""))):
                    df[w] += 1
        n = max(docs, 1)
        self._idf = {w: math.log((n + 1) / (c + 1)) + 1.0 for w, c in df.items()}

    # ---------------- learning (record) ----------------

    def record(
        self,
        *,
        outcome: str,
        medicare: str,
        age: int | None,
        sentiment: str,
        ended_at_beat: str,
        turns: list[dict],
    ) -> CallRecord:
        quality = _QUALITY.get(outcome, 0.4)
        rec = CallRecord(
            ts=datetime.now(timezone.utc).isoformat(),
            outcome=outcome,
            quality=quality,
            medicare=medicare or "",
            age=age,
            sentiment=sentiment or "neutral",
            ended_at_beat=ended_at_beat,
            duration_turns=len(turns),
            turns=turns,
        )
        with self._lock:
            self.calls.append(asdict(rec))
            # Rebuild IDF incrementally-ish (cheap for our scale) and persist.
            self._rebuild_idf()
            self._save()
        return rec

    # ---------------- retrieval (few-shot) ----------------

    def _sim(self, q_toks: list[str], doc_toks: list[str]) -> float:
        if not q_toks or not doc_toks:
            return 0.0
        qv = Counter(q_toks)
        dv = Counter(doc_toks)
        dot = sum(qv[w] * self._idf.get(w, 1.0) ** 2 for w in qv if w in dv)
        qn = math.sqrt(sum((c * self._idf.get(w, 1.0)) ** 2 for w, c in qv.items()))
        dn = math.sqrt(sum((c * self._idf.get(w, 1.0)) ** 2 for w, c in dv.items()))
        if qn == 0 or dn == 0:
            return 0.0
        return dot / (qn * dn)

    def few_shot(self, beat: str, utterance: str, k: int = 3) -> list[dict]:
        """Most similar past (customer line -> bot reply) pairs, ranked by
        similarity * outcome quality. These get injected into the LLM prompt."""
        q_toks = _tokens(utterance)
        scored: list[tuple[float, dict]] = []
        with self._lock:
            calls = list(self.calls)
        for call in calls:
            q = call.get("quality", 0.4)
            turns = call.get("turns", [])
            for i in range(len(turns) - 1):
                cust, bot = turns[i], turns[i + 1]
                if cust.get("role") != "customer" or bot.get("role") != "ai":
                    continue
                sim = self._sim(q_toks, _tokens(cust.get("text", "")))
                if sim <= 0.01:
                    continue
                score = sim * (0.5 + q)  # quality boosts good examples
                scored.append((score, {
                    "customer": cust.get("text", ""),
                    "bot": bot.get("text", ""),
                    "beat": beat,
                    "quality": q,
                }))
        scored.sort(key=lambda x: x[0], reverse=True)
        # de-dup by customer line, keep best
        seen, out = set(), []
        for _, ex in scored:
            key = ex["customer"].lower()[:40]
            if key in seen:
                continue
            seen.add(key)
            out.append(ex)
            if len(out) >= k:
                break
        return out

    # ---------------- insight (self-analysis) ----------------

    def stats(self) -> dict:
        with self._lock:
            calls = list(self.calls)
        if not calls:
            return {"total_calls": 0, "learned_from": 0}
        by_outcome = Counter(c["outcome"] for c in calls)
        by_medicare = Counter(c.get("medicare") or "unset" for c in calls)
        ages = [c["age"] for c in calls if isinstance(c.get("age"), int)]
        qualities = [c.get("quality", 0) for c in calls]
        return {
            "total_calls": len(calls),
            "avg_quality": round(sum(qualities) / len(qualities), 3),
            "by_outcome": dict(by_outcome.most_common()),
            "by_medicare": dict(by_medicare.most_common()),
            "avg_age": round(sum(ages) / len(ages), 1) if ages else None,
            "transfers": by_outcome.get("AI-TRANSFER", 0),
            "confusion_rate": round(
                by_outcome.get("AI-UNCLEAR", 0) / len(calls), 3
            ),
        }

    def insights(self) -> dict:
        """Actionable self-analysis: where calls die and what to improve."""
        with self._lock:
            calls = list(self.calls)
        unclear = [c for c in calls if c["outcome"] == "AI-UNCLEAR"]
        # Which beat generates the most confusion?
        beat_confusion = Counter(c.get("ended_at_beat", "?") for c in unclear)
        # Customer lines that immediately preceded a confused/ended call.
        dead_phrases: Counter[str] = Counter()
        for c in calls:
            if c["outcome"] == "AI-UNCLEAR":
                cust = [t["text"] for t in c.get("turns", []) if t.get("role") == "customer"]
                if cust:
                    dead_phrases[cust[-1].lower()[:60]] += 1
        return {
            "stats": self.stats(),
            "confusion_hotspots": dict(beat_confusion.most_common(5)),
            "phrases_that_caused_confusion": dict(dead_phrases.most_common(8)),
            "suggestion": self._suggestion(beat_confusion),
        }

    @staticmethod
    def _suggestion(beat_confusion: Counter) -> str:
        if not beat_confusion:
            return "No confusing calls yet. Keep testing edge cases."
        top = beat_confusion.most_common(1)[0][0]
        tips = {
            "MEDICARE_CHECK": "Customers get stuck on the Medicare question — try clarifying what 'Medicare' means before asking.",
            "AGE_DETECTION": "Age isn't landing — rephrase 'how old are you' more naturally.",
        }
        return tips.get(top, f"Most confusion happens at {top}. Review those lines.")


# Shared singleton so every call (debug + voice) learns from the same history.
_MEMORY: CallMemory | None = None
_MEM_LOCK = threading.Lock()


def get_memory() -> CallMemory:
    global _MEMORY
    with _MEM_LOCK:
        if _MEMORY is None:
            _MEMORY = CallMemory()
        return _MEMORY
