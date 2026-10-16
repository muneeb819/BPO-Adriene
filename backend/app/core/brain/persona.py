"""Adriene's persona: young, warm, energetic, mature.

A 26-year-old benefits specialist who talks like a person, not a phone tree:
contractions, short sentences, natural acknowledgements, real empathy, and
*varying* phrasing so no two calls sound cloned. Composes plain text; the UI's
voice engine adds prosody (pauses, pitch, pace) for speech synthesis.

Deterministic per session: seeded by session id so QA reports are reproducible
while different callers still hear different wording.
"""
from __future__ import annotations

import random
import re
from typing import Iterable, List, Optional


PROFILE = {
    "name": "Adriene",
    "age": 26,
    "role": "Benefits Specialist",
    "company": "the Senior Resource Center",
    "style": "warm, energetic, plainspoken, respectful of the caller's time",
    "voice_hint": "young adult female, bright and friendly, unhurried",
}

# Words that must never be varied away (compliance / script integrity).
FIXED = {"Medicare", "Parts A and B"}


class Persona:
    def __init__(self, seed_key: str = ""):
        # stable across a call, different across callers
        self._rng = random.Random(f"{seed_key}|adriene-v1")
        self._used = {}  # bank -> set of indices already used (no repeats in-call)

    # ------------------------------------------------------------------ util
    def pick(self, bank: List[str], key: str) -> str:
        opts = [i for i in range(len(bank)) if i not in self._used.setdefault(key, set())]
        if not opts:
            self._used[key].clear()
            opts = list(range(len(bank)))
        i = self._rng.choice(opts)
        self._used[key].add(i)
        return bank[i]

    # -------------------------------------------------------------- greeting
    def greeting(self) -> str:
        return self.pick([
            f"Hi! This is Adriene with {PROFILE['company']} — I'm calling about "
            f"Medicare benefits in your area. Did I catch you at a good time?",
            f"Hello! Adriene here, from {PROFILE['company']} — quick call about "
            f"Medicare options for folks in your area. Is now a good time?",
            f"Hey, good to reach you! This is Adriene with {PROFILE['company']} "
            f"calling about Medicare. Got a minute for me?",
        ], "greeting")

    # ------------------------------------------------------- asking questions
    def ask_medicare(self, variant: int = 0) -> str:
        bank = [
            "Great! So, do you currently have Medicare?",
            "Perfect — quick check, are you on Medicare right now?",
            "Got it. And do you have Medicare already?",
            "Understood. Just to be sure I've got this right — do you currently "
            "have Medicare?",
        ]
        if variant == 0:
            return self.pick(bank, "ask_medicare")
        # rephrasings for repair turns
        repairs = [
            "Let me ask it more simply — are you signed up for Medicare yet?",
            "Sorry if that was unclear! In plain words: is Medicare your "
            "coverage right now?",
            "No worries — do you have that red, white and blue card in hand?",
        ]
        return repairs[min(variant - 1, len(repairs) - 1)]

    def ask_age(self, variant: int = 0) -> str:
        # first ask keeps the canonical phrasing (asserted by tests)
        if variant == 0:
            return self.pick([
                "Are you between 45 and 80 years old?",
                "Are you between 45 and 80 years old? A quick yes, or your age, is perfect.",
            ], "ask_age")
        repairs = [
            "What's your age, roughly? I just need to know you're between 45 and 80.",
            "Let me put it another way — are you over 45 and under 80?",
            "Sorry — how old are you? That keeps us in the 45 to 80 range.",
        ]
        return repairs[min(variant - 1, len(repairs) - 1)]

    # ------------------------------------------------------- understanding
    def acknowledge_yes(self) -> str:
        return self.pick([
            "Nice, thanks for confirming!", "Great — that's what I figured.",
            "Awesome, thank you!", "Perfect, thanks for that.",
        ], "ack_yes")

    def acknowledge_no(self) -> str:
        return self.pick([
            "Okay, I appreciate you being straight with me.",
            "Got it — thanks for letting me know.",
            "Alright, thank you for the honesty.",
        ], "ack_no")

    def smalltalk(self) -> str:
        return self.pick([
            "Ha, I'm doing great — thanks for asking! Still got a few calls to "
            "make today, but this one's going nicely.",
            "I'm doing well, thanks! Right back at you.",
            "Can't complain! Thanks for asking.",
        ], "smalltalk")

    def confused(self) -> str:
        return self.pick([
            "Totally fair — I know this stuff can sound like alphabet soup. "
            "Let me explain it in plain English…",
            "No problem at all, I'm glad you asked rather than guessed.",
            "That makes sense — it confuses everyone at first. Here's the simple version…",
        ], "confused")

    def empathy(self, sentiment: str = "neutral") -> str:
        if sentiment == "frustrated":
            return self.pick([
                "I hear you, and I'm sorry — I'd be annoyed too. Let me make this "
                "quick and worth your while.",
                "You're right to be frustrated, and I appreciate you sticking with "
                "me. Let's get this sorted fast.",
            ], "empathy_f")
        return self.pick([
            "I completely understand — let's take it one step at a time.",
            "That's okay, we'll go at your pace.",
            "Sure, no rush at all.",
        ], "empathy_n")

    def bridge(self, pending: str) -> str:
        """Softly return to the pending question after a tangent."""
        bank = {
            "ask_medicare": [
                "Anyway — to pick up where we were: do you currently have Medicare?",
                "Great. So back to it — are you on Medicare today?",
            ],
            "ask_age": [
                "Perfect. And just to confirm — are you between 45 and 80?",
                "Thanks! One more quick thing — how old are you?",
            ],
            "done": ["Thank you again — take care now!"],
        }
        return self.pick(bank.get(pending, bank["ask_medicare"]), f"bridge_{pending}")

    def repeat_pending(self, question: str) -> str:
        return f"{self.pick(['Of course.', 'Sure thing.', 'Happy to.'], 'repeat')} {question}"

    # ------------------------------------------------------- special turns
    def identity_answer(self, pending: str) -> str:
        return (
            "Of course — I'm Adriene with the Senior Resource Center, a licensed "
            "insurance agency. We're not the government, and this is a sales call "
            "about Medicare options; I'll keep it short, I promise. "
            + (self.bridge(pending) if pending != "done" else "Thanks for hearing me out.")
        )

    def scam_answer(self, pending: str) -> str:
        return (
            "That's a smart question — no, I'm not a robocall, and I'm not the "
            "government. I'm Adriene, a licensed agent with the Senior Resource "
            "Center, and you can hang up any time or call your SHIP counselor to "
            "double-check me. I'd just love two minutes to see if a plan review "
            "would help you. "
            + (self.bridge(pending) if pending != "done" else "")
        ).strip()

    def not_interested(self) -> str:
        return self.pick([
            "Completely fair — and I won't push. If it's ever useful, a free annual "
            "plan review can catch overcharges. Want me to leave your details with a "
            "specialist for later, or shall I close it out?",
            "Understood, I respect that. Just so you know, plans change every fall "
            "and people do save by reviewing — but no pressure. Should I close this "
            "out?",
        ], "not_interested")

    def complaint_response(self) -> str:
        return (
            "I'm really sorry — that's not how this should feel. I'll be quick: "
            "if you give me sixty seconds and it's still not useful, I'll thank you "
            "and go. Fair?"
        )

    def callback_ack(self, when: Optional[str] = None) -> str:
        when = when or "later today"
        return (
            f"No problem at all — I'll note {when} and have someone reach out then. "
            "Sorry for catching you at a busy moment. Talk soon!"
        )

    def dnc_ack(self, compliance_text: str) -> str:
        return compliance_text   # compliance module owns the wording

    def thanks(self) -> str:
        return self.pick([
            "You're very welcome! Glad I could help.",
            "Anytime! That's what I'm here for.",
            "Happy to help — that's the fun part of my day.",
        ], "thanks")

    def farewell(self) -> str:
        return self.pick([
            "Thanks so much for your time — have a wonderful rest of your day!",
            "Take care! It was great talking with you.",
            "Alright, thank you again — enjoy your evening!",
        ], "farewell")

    def clarify_options(self, question: str) -> str:
        return (
            "Tell you what — you can just say 'yes' or 'no', or give me your age "
            f"as a number. {question} Or if you'd rather, I can have a licensed "
            "specialist walk you through it."
        )

    def escalate_to_human(self) -> str:
        return (
            "Let me save you the trouble — I'll bring in a licensed specialist who "
            "can explain everything properly. Hang tight, I'll transfer you now."
        )

    def language_offer(self) -> str:
        return (
            "¡Por supuesto! I'll connect you with a Spanish-speaking specialist "
            "right away — un momento, por favor."
        )

    def k_answer_bridge(self, entry_answer: str, pending_question: str,
                        follow_up: str = "") -> str:
        ack = self.pick([
            "Great question!", "Happy to explain!", "Sure — here's the plain version:",
        ], "k_ack")
        tail = follow_up or (f"To pick back up: {pending_question}"
                             if pending_question else "")
        return f"{ack} {entry_answer} {tail}".strip()

    # ------------------------------------------------------------ outcomes
    def transfer_line(self) -> str:
        # must contain the literal word "transfer" (QA assertion)
        return self.pick([
            "That's wonderful — you look like a great fit. I'll transfer you to a "
            "Benefits Specialist right now, they're expecting you. One moment!",
            "Excellent news! Let me transfer you straight to a Benefits Specialist "
            "who's standing by — hang on just a second.",
        ], "transfer")

    def not_eligible_line(self, reason: str = "") -> str:
        base = self.pick([
            "I really appreciate your time. Based on what you've told me, this "
            "particular offer isn't the right fit for you today — but I hope the "
            "explaining was helpful anyway.",
            "Thank you for being so patient with me. This campaign isn't a match "
            "for your situation, so I won't take up any more of your time — take care!",
        ], "not_elig")
        return f"{reason} {base}".strip()

    def age_ack(self, age: int) -> str:
        return self.pick([
            f"Thanks — {age}, got it.",
            f"{age} it is, thank you.",
            f"Perfect, {age}.",
        ], "age_ack")

    def coverage_ack(self, plan: str) -> str:
        lines = {
            "advantage": "Oh nice — an Advantage plan, that's a popular route.",
            "medigap": "Good, a supplement fills in those Original Medicare gaps.",
            "part_a": "Right, so you're at least on Part A.",
            "part_b": "Great — Part B is the backbone of most people's coverage.",
            "part_d": "Perfect, so prescriptions are handled.",
            "medicaid": "I see — Medicaid helps alongside Medicare for many folks.",
            "dual": "Oh, you're dual-eligible — that opens up some good savings programs.",
            "lis": "Good to know — Extra Help makes a real difference on drug costs.",
            "employer": "Nice, employer or retiree coverage can be creditable — worth checking the details.",
            "tricare": "Got it — TRICARE works with Medicare for service members.",
            "va": "Thank you for your service — VA benefits and Medicare can work together.",
            "marketplace": "Okay — once you do qualify for Medicare, that Marketplace plan steps aside.",
        }
        return self.pick([lines.get(plan, "Understood, thanks for the detail."),
                          lines.get(plan, "Got it, thank you.")], f"cov_{plan}")

    # ------------------------------------------------------------ helpers
    @staticmethod
    def humanize(text: str) -> str:
        """Light conversational polish: contractions + sentence rhythm."""
        reps = [
            (r"\byou are\b", "you're"), (r"\bdo not\b", "don't"),
            (r"\bcannot\b", "can't"), (r"\bit is\b", "it's"),
            (r"\bI am\b", "I'm"), (r"\bwe are\b", "we're"),
            (r"\bwill not\b", "won't"), (r"\bthat is\b", "that's"),
            (r"\bhave not\b", "haven't"), (r"\bis not\b", "isn't"),
        ]
        out = text
        for a, b in reps:
            out = re.sub(a, b, out)
        return out

    @staticmethod
    def segments(text: str) -> List[str]:
        """Split into sentence-sized chunks so TTS can breathe between them."""
        parts = re.split(r"(?<=[.!?…])\s+", (text or "").strip())
        return [p for p in parts if p]
