"""Campaign engine: one bot, many campaigns.

A Campaign is a data-driven definition of a qualification script: the persona,
the opening line, an ordered list of verification questions (each with its own
routing rules, compliance range, and response lines), and the dispositions.

The Medicare fronter is just the *default* campaign. Final Expense, ACA, HI,
and any future campaign are editable definitions stored in data/campaigns.json
and selected + deployed from the dashboard. The Conversation engine walks
whatever questions the active campaign defines -- nothing is hardcoded.

Verdict grammar for routes:
    next      -> proceed to the next question
    transfer  -> qualified, hand off to a licensed agent
    clarify   -> re-ask (up to the question's max_clarify), then fall through
    end:DIspo -> end the call with that disposition
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

STORE_PATH = "data/campaigns.json"


@dataclass
class Question:
    id: str
    kind: str                      # "yes_no" | "age" | "keyword"
    ask_line: str                  # fallback question line
    routes: dict[str, str]         # verdict -> "next"|"transfer"|"clarify"|"end:AI-X"
    lines: dict[str, str] = field(default_factory=dict)  # verdict/label -> fallback line
    max_clarify: int = 2
    age_min: int | None = None     # compliance range (age kind)
    age_max: int | None = None
    pass_keywords: list[str] = field(default_factory=list)  # keyword kind

    def default_end(self) -> str:
        return "end:AI-UNCLEAR"


@dataclass
class Campaign:
    id: str
    name: str
    description: str = ""
    agent_name: str = "Lina"
    business_name: str = "EverTrust"
    persona_extra: str = ""        # extra persona guidance appended to the base
    lines: dict = field(default_factory=dict)   # greet, transfer_handoff, busy_close, ...
    questions: list[Question] = field(default_factory=list)
    # Compliance: spoken immediately after the greeting ("this call is recorded...").
    # Empty = no disclosure spoken (some jurisdictions / campaigns don't require one).
    disclosure: str = ""
    updated: str = ""
    # --- Dograh-style general settings ---
    use_case_id: str = ""
    turn_detection_mode: str = "transcription_based"  # transcription_based | semantic_vad | none
    ambient_noise_enabled: bool = False
    interruption_min_words: int = 2
    call_max_duration: int = 300        # seconds
    call_idle_timeout: int = 10         # seconds
    dictionary_words: list[str] = field(default_factory=list)
    voicemail_settings: dict = field(default_factory=dict)
    widget_config: dict = field(default_factory=dict)
    agent_uuid: str = ""

    def summary(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "questions": len(self.questions),
            "updated": self.updated,
        }

    def compliance(self) -> dict:
        """Derive the headline age range from any age questions (for the UI)."""
        mins = [q.age_min for q in self.questions if q.kind == "age" and q.age_min]
        maxs = [q.age_max for q in self.questions if q.kind == "age" and q.age_max]
        return {
            "min_age": min(mins) if mins else None,
            "max_age": max(maxs) if maxs else None,
            "verification_steps": [q.id for q in self.questions],
            "recording_disclosure": self.disclosure,
        }


# ---------------------------------------------------------------------------
# Default campaigns
# ---------------------------------------------------------------------------

def _medicare() -> Campaign:
    return Campaign(
        id="medicare",
        name="Medicare Fronter",
        description="Verify current Medicare coverage, then confirm age 45-90.",
        agent_name="Lina",
        business_name="EverTrust",
        use_case_id="UC-LINA-001",
        disclosure="Just so you know, this call may be recorded for quality and training purposes.",
        turn_detection_mode="transcription_based",
        ambient_noise_enabled=False,
        interruption_min_words=2,
        call_max_duration=300,
        call_idle_timeout=10,
        lines={
            "greet": "Hi, this is {name} from {campaign}. How are you today?",
            "transfer_handoff": "Perfect, one moment and I'll get you connected with our licensed agent.",
            "busy_close": "No problem. I appreciate your time. Have a great day.",
            "not_interested_close": "I understand. Thank you for your time. Have a great day.",
            "hello_checkin": "Hello? Just checking you're still there.",
        },
        questions=[
            Question(
                id="medicare",
                kind="yes_no",
                ask_line="Do you currently have Medicare?",
                max_clarify=2,
                routes={
                    "YES": "next",
                    "NO": "end:AI-NO-MEDICARE",
                    "REFUSE": "end:AI-MEDICARE-REFUSED",
                    "QUESTION": "clarify",
                    "PRIVATE_ONLY": "clarify",
                    "UNSURE": "clarify",
                    "UNCLEAR": "clarify",
                    "DNC": "end:AI-DNC",
                    "BUSY": "end:AI-BUSY",
                },
                lines={
                    "no": "I understand. Thank you for your time. Have a great day.",
                    "refuse": "I understand. Thank you for your time. Have a great day.",
                    "unsure": "Just to confirm, do you currently have Medicare — yes or no?",
                    "question": "I'm just confirming whether you currently have Medicare coverage. Do you have it — yes or no?",
                    "private": "Do you also currently have Medicare?",
                    "repeat": "I'm sorry, I just need to confirm — do you currently have Medicare, yes or no?",
                },
            ),
            Question(
                id="age",
                kind="age",
                ask_line="How old are you, if you don't mind me asking?",
                age_min=45,
                age_max=90,
                max_clarify=1,
                routes={
                    "PASS": "transfer",
                    "FAIL": "end:AI-NOT-ELIGIBLE",
                    "UNCLEAR": "clarify",
                    "DNC": "end:AI-DNC",
                    "BUSY": "end:AI-BUSY",
                },
                lines={
                    "pass": "Thank you. One moment, let me get you connected with our licensed agent.",
                    "fail": "I appreciate your time, but this option is for ages {min_age} to {max_age}. Thank you. Have a great day.",
                    "clarify": "I'm sorry, I didn't catch that. How old are you?",
                },
            ),
        ],
    )


def _final_expense() -> Campaign:
    return Campaign(
        id="final_expense",
        name="Final Expense Fronter",
        description="Confirm interest in burial/final-expense coverage, then verify age 50-85.",
        agent_name="Lina",
        business_name="EverTrust",
        use_case_id="UC-LINA-002",
        disclosure="Just so you know, this call may be recorded for quality and training purposes.",
        turn_detection_mode="transcription_based",
        ambient_noise_enabled=False,
        interruption_min_words=2,
        call_max_duration=300,
        call_idle_timeout=10,
        lines={
            "greet": "Hi, this is {name} from {campaign}. How are you today?",
            "transfer_handoff": "Great, one moment and I'll connect you with a licensed agent who can walk you through the options.",
            "busy_close": "No problem at all. I appreciate your time. Have a great day.",
            "not_interested_close": "I understand completely. Thank you for your time. Have a great day.",
            "hello_checkin": "Hello? Just checking you're still there.",
        },
        questions=[
            Question(
                id="interest",
                kind="yes_no",
                ask_line="Are you looking to help cover final expenses like a funeral or burial costs?",
                max_clarify=2,
                routes={
                    "YES": "next",
                    "NO": "end:AI-NOT-INTERESTED",
                    "REFUSE": "end:AI-REFUSED",
                    "UNSURE": "clarify",
                    "UNCLEAR": "clarify",
                    "DNC": "end:AI-DNC",
                    "BUSY": "end:AI-BUSY",
                },
                lines={
                    "no": "I understand. Thank you for your time. Have a great day.",
                    "refuse": "I understand. Thank you for your time. Have a great day.",
                    "unsure": "Just to confirm — are you interested in help covering final expenses, yes or no?",
                    "repeat": "I'm sorry, I just need to confirm — are you interested in final expense coverage, yes or no?",
                },
            ),
            Question(
                id="age",
                kind="age",
                ask_line="How old are you, if you don't mind me asking?",
                age_min=50,
                age_max=85,
                max_clarify=1,
                routes={
                    "PASS": "transfer",
                    "FAIL": "end:AI-NOT-ELIGIBLE",
                    "UNCLEAR": "clarify",
                    "DNC": "end:AI-DNC",
                    "BUSY": "end:AI-BUSY",
                },
                lines={
                    "pass": "Thank you. One moment, let me connect you with a licensed agent.",
                    "fail": "I appreciate your time, but this option is for ages 50 to 85. Thank you. Have a great day.",
                    "clarify": "I'm sorry, I didn't catch that. How old are you?",
                },
            ),
        ],
    )


def _aca() -> Campaign:
    return Campaign(
        id="aca",
        name="ACA / Marketplace Fronter",
        description="Find uninsured consumers, then verify age 18-64 for marketplace subsidies.",
        agent_name="Lina",
        business_name="EverTrust",
        use_case_id="UC-LINA-003",
        disclosure="Just so you know, this call may be recorded for quality and training purposes.",
        turn_detection_mode="transcription_based",
        ambient_noise_enabled=False,
        interruption_min_words=2,
        call_max_duration=300,
        call_idle_timeout=10,
        lines={
            "greet": "Hi, this is {name} from {campaign}. How are you today?",
            "transfer_handoff": "Perfect, one moment and I'll connect you with a licensed agent about marketplace options.",
            "busy_close": "No problem. I appreciate your time. Have a great day.",
            "not_interested_close": "I understand. Thank you for your time. Have a great day.",
            "hello_checkin": "Hello? Just checking you're still there.",
        },
        questions=[
            Question(
                id="insured",
                kind="yes_no",
                ask_line="Do you currently have health insurance or a marketplace plan?",
                max_clarify=2,
                routes={
                    "NO": "next",
                    "YES": "end:AI-HAS-INSURANCE",
                    "REFUSE": "end:AI-REFUSED",
                    "UNSURE": "clarify",
                    "UNCLEAR": "clarify",
                    "DNC": "end:AI-DNC",
                    "BUSY": "end:AI-BUSY",
                },
                lines={
                    "yes": "Understood — thank you. Have a great day.",
                    "no": "Okay, thank you.",
                    "refuse": "I understand. Thank you for your time. Have a great day.",
                    "unsure": "Just to confirm — do you currently have health insurance, yes or no?",
                    "repeat": "I'm sorry, I just need to confirm — do you have health insurance right now, yes or no?",
                },
            ),
            Question(
                id="age",
                kind="age",
                ask_line="How old are you, if you don't mind me asking?",
                age_min=18,
                age_max=64,
                max_clarify=1,
                routes={
                    "PASS": "transfer",
                    "FAIL": "end:AI-NOT-ELIGIBLE",
                    "UNCLEAR": "clarify",
                    "DNC": "end:AI-DNC",
                    "BUSY": "end:AI-BUSY",
                },
                lines={
                    "pass": "Thank you. One moment, let me connect you with a licensed agent.",
                    "fail": "I appreciate your time, but this option is for ages 18 to 64. Thank you. Have a great day.",
                    "clarify": "I'm sorry, I didn't catch that. How old are you?",
                },
            ),
        ],
    )


def _hi() -> Campaign:
    return Campaign(
        id="hi",
        name="Hospital Indemnity Fronter",
        description="Verify age 50-85, then screen for recent hospital stays.",
        agent_name="Lina",
        business_name="EverTrust",
        use_case_id="UC-LINA-004",
        disclosure="Just so you know, this call may be recorded for quality and training purposes.",
        turn_detection_mode="transcription_based",
        ambient_noise_enabled=False,
        interruption_min_words=2,
        call_max_duration=300,
        call_idle_timeout=10,
        lines={
            "greet": "Hi, this is {name} from {campaign}. How are you today?",
            "transfer_handoff": "Great, one moment and I'll connect you with a licensed agent about hospital cash benefits.",
            "busy_close": "No problem. I appreciate your time. Have a great day.",
            "not_interested_close": "I understand. Thank you for your time. Have a great day.",
            "hello_checkin": "Hello? Just checking you're still there.",
        },
        questions=[
            Question(
                id="age",
                kind="age",
                ask_line="How old are you, if you don't mind me asking?",
                age_min=50,
                age_max=85,
                max_clarify=1,
                routes={
                    "PASS": "next",
                    "FAIL": "end:AI-NOT-ELIGIBLE",
                    "UNCLEAR": "clarify",
                    "DNC": "end:AI-DNC",
                    "BUSY": "end:AI-BUSY",
                },
                lines={
                    "pass": "Thank you.",
                    "fail": "I appreciate your time, but this option is for ages 50 to 85. Thank you. Have a great day.",
                    "clarify": "I'm sorry, I didn't catch that. How old are you?",
                },
            ),
            Question(
                id="hospital_stay",
                kind="yes_no",
                ask_line="Have you had any hospital stays in the last 24 months?",
                max_clarify=2,
                routes={
                    "NO": "transfer",
                    "YES": "end:AI-RECENT-HOSPITAL",
                    "REFUSE": "end:AI-REFUSED",
                    "UNSURE": "clarify",
                    "UNCLEAR": "clarify",
                    "DNC": "end:AI-DNC",
                    "BUSY": "end:AI-BUSY",
                },
                lines={
                    "no": "Thank you. One moment, let me connect you with a licensed agent.",
                    "yes": "I understand — thank you for your time. Have a great day.",
                    "refuse": "I understand. Thank you for your time. Have a great day.",
                    "unsure": "Just to confirm — any hospital stays in the last two years, yes or no?",
                    "repeat": "I'm sorry, I just need to confirm — any hospital stays in the last two years?",
                },
            ),
        ],
    )


DEFAULTS = {
    "medicare": _medicare,
    "final_expense": _final_expense,
    "aca": _aca,
    "hi": _hi,
}


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class CampaignStore:
    def __init__(self, path: str = STORE_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._data: dict = {"campaigns": {}, "active": "medicare"}
        self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
        except (OSError, ValueError):
            self._data = {"campaigns": {}, "active": "medicare"}
        if not self._data.get("campaigns"):
            self._seed_defaults()
            return
        self._migrate()

    def _migrate(self) -> bool:
        """Backfill fields added in later releases into an existing store.

        Without this, a campaigns.json written by an older build would silently
        miss the recorded-call disclosure (and any future compliance field)
        until someone hit "Reset all" — which would also wipe their edits.
        """
        changed = False
        for cid, factory in DEFAULTS.items():
            raw = self._data.get("campaigns", {}).get(cid)
            if raw is None:
                continue
            fresh = factory()
            for attr in ("disclosure", "use_case_id", "turn_detection_mode",
                         "ambient_noise_enabled", "interruption_min_words",
                         "call_max_duration", "call_idle_timeout",
                         "dictionary_words", "voicemail_settings",
                         "widget_config", "agent_uuid"):
                # Presence test, not truthiness: an operator who deliberately
                # cleared the line must keep it cleared.
                if attr not in raw:
                    raw[attr] = getattr(fresh, attr)
                    changed = True
            if "lines" not in raw:
                raw["lines"] = asdict(fresh)["lines"]
                changed = True
        if changed:
            self._save()
        return changed

    def _seed_defaults(self):
        self._data["campaigns"] = {
            cid: asdict(factory()) for cid, factory in DEFAULTS.items()
        }
        self._data.setdefault("active", "medicare")
        self._save()

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            pass

    # -- read --

    def list(self) -> list[dict]:
        with self._lock:
            active = self._data.get("active")
            out = []
            for cid, d in self._data.get("campaigns", {}).items():
                s = {
                    "id": cid,
                    "name": d.get("name", cid),
                    "description": d.get("description", ""),
                    "questions": len(d.get("questions", [])),
                    "updated": d.get("updated", ""),
                    "active": cid == active,
                    "builtin": cid in DEFAULTS,
                }
                out.append(s)
            return out

    def get(self, cid: str) -> Campaign | None:
        with self._lock:
            d = self._data.get("campaigns", {}).get(cid)
        return _from_dict(d) if d else None

    def active_id(self) -> str:
        with self._lock:
            return self._data.get("active", "medicare")

    def active(self) -> Campaign:
        cid = self.active_id()
        return self.get(cid) or _medicare()

    # -- write --

    def save(self, campaign: Campaign):
        campaign.updated = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._data.setdefault("campaigns", {})[campaign.id] = asdict(campaign)
            self._save()

    def set_active(self, cid: str) -> bool:
        with self._lock:
            if cid not in self._data.get("campaigns", {}):
                return False
            self._data["active"] = cid
            self._save()
            return True

    def create(self, *, name: str, description: str = "", base: str | None = None) -> Campaign:
        """Create a brand-new campaign.

        If `base` is given, clone that campaign's script (a fast way to start a
        variant); otherwise seed from an empty template with one yes/no step so
        the editor opens on something valid.
        """
        name = (name or "").strip() or "New Campaign"
        with self._lock:
            slug = "".join(ch for ch in name.lower() if ch.isalnum() or ch == "-").strip("-") or "campaign"
            cid, n = slug, 2
            while cid in self._data.get("campaigns", {}):
                cid, n = f"{slug}-{n}", n + 1

            src = None
            if base:
                raw = self._data.get("campaigns", {}).get(base)
                if raw:
                    src = json.loads(json.dumps(raw))  # deep copy

            if src:
                d = src
                d["id"] = cid
                d["name"] = name
                d["description"] = description or f"Variant of {base}"
            else:
                d = {
                    "id": cid,
                    "name": name,
                    "description": description or "New campaign — edit the script and compliance, then deploy.",
                    "agent_name": "Lina",
                    "business_name": "EverTrust",
                    "persona_extra": "",
                    "disclosure": "Just so you know, this call may be recorded for quality and training purposes.",
                    "lines": {
                        "greet": "Hi, this is {name} from {campaign}. How are you today?",
                        "transfer_handoff": "Thank you. One moment, let me get you connected with our licensed agent.",
                    },
                    "questions": [{
                        "id": "qualify",
                        "kind": "yes_no",
                        "ask_line": "Are you interested in hearing about your options?",
                        "routes": {"YES": "transfer", "NO": "end:AI-NOT-INTERESTED"},
                        "lines": {"NO": "No problem at all — thank you for your time."},
                        "max_clarify": 2,
                    }],
                    "updated": "",
                }
            self._data.setdefault("campaigns", {})[cid] = d
            self._save()
        return _from_dict(self._data["campaigns"][cid])

    def delete(self, cid: str) -> bool:
        """Delete a campaign. Refuses to delete the live one or a default."""
        with self._lock:
            if cid == self._data.get("active"):
                return False
            if cid in DEFAULTS:
                return False
            if cid not in self._data.get("campaigns", {}):
                return False
            del self._data["campaigns"][cid]
            self._save()
            return True

    def reset_defaults(self):
        with self._lock:
            self._seed_defaults()


def _from_dict(d: dict) -> Campaign:
    qs = [Question(**q) for q in d.get("questions", [])]
    return Campaign(
        id=d.get("id", "campaign"),
        name=d.get("name", "Campaign"),
        description=d.get("description", ""),
        agent_name=d.get("agent_name", "Lina"),
        business_name=d.get("business_name", "EverTrust"),
        persona_extra=d.get("persona_extra", ""),
        lines=d.get("lines", {}),
        questions=qs,
        disclosure=d.get("disclosure", ""),
        updated=d.get("updated", ""),
        use_case_id=d.get("use_case_id", ""),
        turn_detection_mode=d.get("turn_detection_mode", "transcription_based"),
        ambient_noise_enabled=d.get("ambient_noise_enabled", False),
        interruption_min_words=d.get("interruption_min_words", 2),
        call_max_duration=d.get("call_max_duration", 300),
        call_idle_timeout=d.get("call_idle_timeout", 10),
        dictionary_words=d.get("dictionary_words", []),
        voicemail_settings=d.get("voicemail_settings", {}),
        widget_config=d.get("widget_config", {}),
        agent_uuid=d.get("agent_uuid", ""),
    )


# Shared singleton (all sessions learn from / deploy the same store).
_STORE: CampaignStore | None = None
_STORE_LOCK = threading.Lock()


def get_store() -> CampaignStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = CampaignStore()
        return _STORE
