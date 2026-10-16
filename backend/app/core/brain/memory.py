"""Self-learning memory: the part that makes Adriene get smarter over time.

Three loops, all offline and dependency-free (JSON on disk):

1. **Learn**  — phrasings confirmed by the 👍/👎 feedback endpoint (or by the
   dialogue with high confidence) are stored as utterance -> intent/polarity
   mappings. The NLU checks this store *first*, so a caller's exact wording —
   including slang the lexicon never saw — is recognised on later calls.
2. **Notice** — utterances the brain couldn't classify are queued as
   "unknowns" with their dialogue state, so gaps are visible (GET
   /api/brain/memory) instead of silently repeating mistakes.
3. **Recall** — everything else: intent frequencies, transfer/DNC outcomes and
   a rolling corpus of recent exchanges, used for reporting and prompts.

Thread-safe (single lock), atomic writes, path:
``backend/brain_memory.json`` (git-ignored — live data, not source).
"""
from __future__ import annotations

import difflib
import json
import os
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional


DEFAULT_PATH = Path(os.getenv("BRAIN_MEMORY_PATH") or
                    Path(__file__).resolve().parents[3] / "brain_memory.json")

_MAX_ITEMS = 500          # cap learned/unknown lists
_RECENT = 60              # exchanges kept for context

_EMPTY = {
    "version": 1,
    "learned": {},          # normalized utterance -> {intent, polarity, source, hits, last_seen}
    "unknowns": {},         # normalized utterance -> {state, hits, last_seen}
    "feedback": [],         # raw feedback records (newest last)
    "stats": {              # counters the dialogue increments
        "intents": {},
        "transfers": 0,
        "dnc": 0,
        "callbacks": 0,
        "complaints": 0,
        "knowledge_answers": 0,
        "unknown_total": 0,
        "learned_total": 0,
        "calls": 0,
    },
    "recent": [],           # [{who, text, ts}] rolling context corpus
}


class Memory:
    """JSON-backed memory with an in-process lock and fuzzy recall."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else DEFAULT_PATH
        self._lock = threading.Lock()
        self._data = self._load()

    # ----------------------------------------------------------------- io
    def _load(self) -> dict:
        try:
            if self.path.exists():
                with self.path.open("r", encoding="utf-8") as f:
                    raw = json.load(f)
                data = json.loads(json.dumps(_EMPTY))   # deep copy of defaults
                for k, v in raw.items():
                    if isinstance(v, dict) and isinstance(data.get(k), dict):
                        data[k].update(v)
                    else:
                        data[k] = v
                return data
        except Exception:
            pass
        return json.loads(json.dumps(_EMPTY))

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)
        except Exception as e:      # never let memory I/O break a live call
            print(f"[memory] save failed: {e}")

    # ------------------------------------------------------------- learn
    def match(self, norm: str) -> Optional[Dict]:
        """Recall a learned phrasing (exact, then fuzzy). Applies a hit."""
        if not norm:
            return None
        with self._lock:
            hit = self._data["learned"].get(norm)
            if hit is None:
                # fuzzy recall over learned keys (typos, word order)
                keys = list(self._data["learned"].keys())
                if keys:
                    close = difflib.get_close_matches(norm, keys, n=1, cutoff=0.88)
                    if close:
                        hit = self._data["learned"][close[0]]
            if hit is None:
                return None
            hit["hits"] = int(hit.get("hits", 0)) + 1
            hit["last_seen"] = time.time()
            self._save()
            return dict(hit)

    def learn(self, norm: str, intent: str, polarity: Optional[bool] = None,
              source: str = "feedback") -> bool:
        """Store an utterance->intent mapping (the actual 'learning' step)."""
        if not norm or not intent:
            return False
        with self._lock:
            table = self._data["learned"]
            if norm in table:
                table[norm]["hits"] = int(table[norm].get("hits", 0)) + 1
                table[norm]["last_seen"] = time.time()
            else:
                if len(table) >= _MAX_ITEMS:      # keep the freshest
                    oldest = min(table.items(), key=lambda kv: kv[1].get("last_seen", 0))
                    table.pop(oldest[0], None)
                table[norm] = {
                    "intent": intent,
                    "polarity": polarity,
                    "source": source,
                    "hits": 1,
                    "last_seen": time.time(),
                }
                self._data["stats"]["learned_total"] = \
                    int(self._data["stats"].get("learned_total", 0)) + 1
            # learning resolves the unknown, drop it
            self._data["unknowns"].pop(norm, None)
            self._save()
        return True

    def forget(self, norm: str) -> bool:
        with self._lock:
            removed = self._data["learned"].pop(norm, None) is not None
            if removed:
                self._save()
            return removed

    # ----------------------------------------------------------- unknowns
    def remember_unknown(self, norm: str, state: str = "") -> None:
        if not norm or len(norm) > 300:
            return
        with self._lock:
            table = self._data["unknowns"]
            item = table.setdefault(norm, {"state": state, "hits": 0,
                                           "last_seen": time.time()})
            item["hits"] += 1
            item["last_seen"] = time.time()
            item["state"] = state or item.get("state", "")
            if len(table) > _MAX_ITEMS:
                oldest = min(table.items(), key=lambda kv: kv[1].get("last_seen", 0))
                table.pop(oldest[0], None)
            self._data["stats"]["unknown_total"] = \
                int(self._data["stats"].get("unknown_total", 0)) + 1
            self._save()

    # ----------------------------------------------------------- feedback
    def feedback(self, record: Dict) -> Dict:
        """Record a 👍/👎 from the dashboard; 'bad' + corrected intent learns."""
        rec = dict(record)
        rec["ts"] = time.time()
        with self._lock:
            self._data["feedback"].append(rec)
            self._data["feedback"] = self._data["feedback"][-200:]
            verdict = (rec.get("verdict") or "").lower()
            utterance = (rec.get("utterance_norm") or "").strip()
            if verdict in ("bad", "wrong") and utterance:
                corrected = rec.get("correct_intent") or rec.get("corrected_reply")
                if rec.get("correct_intent"):
                    self._learn_unlocked(utterance, rec["correct_intent"],
                                         rec.get("correct_polarity"), "feedback")
                # a corrected reply is learned verbatim as a knowledge answer
                if rec.get("corrected_reply") and rec.get("topic"):
                    self._data.setdefault("corrections", {})[rec["topic"]] = {
                        "utterance": rec.get("utterance"),
                        "reply": rec["corrected_reply"],
                        "ts": rec["ts"],
                    }
                # learning resolves the unknown — pull it out of the queue
                if utterance:
                    self._data["unknowns"].pop(utterance, None)
                _ = corrected
            self._save()
            return rec

    def _learn_unlocked(self, norm: str, intent: str, polarity, source: str):
        table = self._data["learned"]
        table[norm] = {"intent": intent, "polarity": polarity, "source": source,
                       "hits": table.get(norm, {}).get("hits", 0),
                       "last_seen": time.time()}

    # -------------------------------------------------------------- stats
    def count(self, kind: str, n: int = 1) -> None:
        with self._lock:
            stats = self._data["stats"]
            if kind in ("transfers", "dnc", "callbacks", "complaints",
                        "knowledge_answers", "calls"):
                stats[kind] = int(stats.get(kind, 0)) + n
            else:                                    # intent tally
                intents = stats.setdefault("intents", {})
                intents[kind] = int(intents.get(kind, 0)) + n
            self._save()

    def remember(self, who: str, text: str) -> None:
        with self._lock:
            recent = self._data.setdefault("recent", [])
            recent.append({"who": who, "text": text, "ts": time.time()})
            del recent[:-_RECENT]
            self._save()

    def correction_for(self, topic: str) -> Optional[str]:
        with self._lock:
            c = self._data.get("corrections", {}).get(topic)
            return c.get("reply") if c else None

    def dump(self) -> Dict:
        with self._lock:
            return json.loads(json.dumps(self._data))

    def stats(self) -> Dict:
        with self._lock:
            return json.loads(json.dumps(self._data["stats"]))

    def status(self) -> Dict:
        d = self.dump()
        return {
            "path": str(self.path),
            "learned_count": len(d.get("learned", {})),
            "unknown_count": len(d.get("unknowns", {})),
            "feedback_count": len(d.get("feedback", {})),
            "stats": d.get("stats", {}),
            "top_learned": sorted(
                ({**v, "utterance": k} for k, v in d.get("learned", {}).items()),
                key=lambda x: -x.get("hits", 0),
            )[:10],
            "top_unknowns": sorted(
                ({**v, "utterance": k} for k, v in d.get("unknowns", {}).items()),
                key=lambda x: -x.get("hits", 0),
            )[:10],
        }


# --------------------------------------------------------------------------- #
# process-wide singleton (uvicorn workers share one)
# --------------------------------------------------------------------------- #
_singleton: Optional[Memory] = None
_singleton_lock = threading.Lock()


def get_memory() -> Memory:
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = Memory()
        return _singleton
