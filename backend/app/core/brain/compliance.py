"""Compliance guardrails for Medicare outbound calling.

Applied to *every* agent reply before it is spoken. Two jobs:

1. Strip / rewrite claims that violate Medicare marketing norms — no
   "guaranteed issue", no "free", no "the government/Medicare endorses us",
   no savings promises, no misrepresenting who we are.
2. Enforce the required disclosures — identity, independent-agency status,
   calling-hours (8am-9pm local) and do-not-call honoring.

Rules are deliberately conservative; this is the safety net under the persona,
not legal advice. (docs/COMPLIANCE.md summarises the references.)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Tuple


@dataclass(frozen=True)
class Rule:
    pattern: str          # regex on the outgoing text (case-insensitive)
    replacement: str      # safer wording
    reason: str


# Prohibited / risky claims -> safe rewrites.
BANNED_CLAIMS: List[Rule] = [
    Rule(r"\bguaranteed (issue|acceptance|approval|coverage)\b",
         "guaranteed-issue rights during your enrollment window",
         "'guaranteed' outside a genuine GI window misrepresents Medigap rules"),
    Rule(r"\bguaranteed\b(?!\s*-?\s*issue)",
         "reliable",
         "absolute guarantees about coverage are prohibited"),
    Rule(r"\bfree (plan|coverage|insurance|medicare|everything|money|gift)\b",
         "no-premium option",
         "'free' overstates plan costs"),
    Rule(r"\bcompletely free\b", "at no premium to you", "misleading cost claim"),
    Rule(r"\b(medicare|the government|government) (approv|endors|recommend|certif)",
         "available to you under Medicare's rules",
         "implying government endorsement of a private agent"),
    Rule(r"\bwe (work |are )?(for|with|under) (medicare|the government|social security)\b",
         "we're an independent licensed agency",
         "must not claim government affiliation"),
    Rule(r"\bofficial(ly)? (medicare|government)\b",
         "licensed",
         "must not imply official status"),
    Rule(r"\byou (will|can) (definitely )?save \$?\d+.*\b",
         "many people in your area save on costs",
         "specific savings promises are prohibited"),
    Rule(r"\byou'?ll save (money|a lot|thousands)\b",
         "you could find savings",
         "savings promises are prohibited"),
    Rule(r"\b100% (covered|paid|free)\b",
         "fully covered in most cases",
         "absolute coverage claims are prohibited"),
    Rule(r"\bno cost ever\b", "no premium for Part A for most people",
         "absolute cost claims are prohibited"),
    Rule(r"\bbest (plan|insurance|coverage) (in|for) (the )?world\b",
         "a plan that fits your needs",
         "'best' comparative claims"),
    Rule(r"\bpre-?existing (condition|conditions) (are )?(always )?(cover|covered)\b",
         "Medicare Advantage can't deny you for health conditions",
         "pre-existing condition claims require nuance"),
    Rule(r"\byou (don'?t|do not) have to (ever )?(pay|answer|talk)\b",
         "you're in control of what happens next",
         "absolute statements about caller obligations"),
]


def ensure_compliant(text: str) -> Tuple[str, List[str]]:
    """Rewrite risky claims in `text`. Returns (safe_text, list of applied reasons)."""
    out = text or ""
    applied: List[str] = []
    for rule in BANNED_CLAIMS:
        new = re.sub(rule.pattern, rule.replacement, out, flags=re.IGNORECASE)
        if new != out:
            out = new
            applied.append(rule.reason)
    return out, applied


# --------------------------------------------------------------------------- #
# required disclosures
# --------------------------------------------------------------------------- #

DISCLAIMER_IDENTITY = (
    "Full disclosure — I'm Adriene, calling from an independent licensed "
    "insurance agency. We're not affiliated with Medicare or the government."
)

DISCLAIMER_TRANSFER = (
    "Before I bring them in: I'm an independent licensed agent — we're not "
    "affiliated with or endorsed by Medicare or the government."
)


def identity_disclosure() -> str:
    return DISCLAIMER_IDENTITY


def transfer_disclosure() -> str:
    return DISCLAIMER_TRANSFER


# --------------------------------------------------------------------------- #
# calling-hours & do-not-call helpers
# --------------------------------------------------------------------------- #

CALL_WINDOW_LOCAL = (8, 21)   # TCPA: 8:00 a.m. – 9:00 p.m. recipient local time


def within_calling_hours(hour_local: int) -> bool:
    return CALL_WINDOW_LOCAL[0] <= hour_local < CALL_WINDOW_LOCAL[1]


_DNC_PATTERNS = [
    "take me off", "remove my number", "stop calling", "do not call",
    "don't call", "delete my number", "opt me out", "opt out", "no more calls",
    "never call", "remove me", "quit calling",
]


def is_dnc_request(normalized_text: str) -> bool:
    t = f" {normalized_text} "
    return any(p in t for p in _DNC_PATTERNS)


def dnc_confirmation() -> str:
    return (
        "Of course — I'm adding your number to our internal do-not-call list right "
        "now, and you won't hear from us again. I'm sorry for the interruption, and "
        "thank you for telling me. Have a great day."
    )


REQUIRED_PHRASES_ON_TRANSFER = [DISCLAIMER_TRANSFER]
