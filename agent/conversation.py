"""Campaign-driven conversation engine.

The bot walks the active Campaign's ordered verification questions. Each
question classifies the customer's answer into a verdict and routes it
(next / transfer / clarify / end:DISPOSITION). Nothing about Medicare is
hardcoded -- Medicare is simply the default campaign; Final Expense, ACA, HI
and anything else are data.

Layered on top (the "intelligence"):
  - Self-learning: each finished call is recorded to CallMemory, and similar
    past successful exchanges are injected as few-shot examples, so the bot's
    phrasing improves with use.
  - Sentiment-aware tone: fast local sentiment shifts delivery (patient with an
    annoyed customer, warm with a pleasant one) without extra LLM calls.
  - The LLM naturalizes each compliance-correct line so it sounds human and
    varies per call, while the required message is always preserved.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .campaign import Campaign, Question, get_store
from .config import Config
from .llm import LLM
from .memory import get_memory
from .prompt import build_persona, naturalize_prompt
from .qualifier import (
    classify_yes_no,
    classify_keyword,
    detect_busy,
    detect_dnc,
    extract_age,
)


@dataclass
class BeatResult:
    ai_speech: str | None
    end_call: bool
    disposition: str = ""
    classification: dict = field(default_factory=dict)


@dataclass
class ConversationState:
    state: str = "GREETING"
    q_index: int = -1
    clarify_attempts: int = 0
    sentiment: str = "neutral"
    last_ai_line: str = ""
    transcript: list[dict] = field(default_factory=list)
    disposition: str = ""
    dnc: bool = False
    age: int | None = None
    medicare_status: str = ""          # kept for debug API/back-compat
    answers: dict = field(default_factory=dict)   # question_id -> verdict


class _SafePlaceholders(dict):
    """Leaves unknown {tokens} literal instead of raising KeyError.

    Script lines are written by operators in the dashboard editor, so a token
    we don't recognise must never crash the call — it just stays visible.
    """

    def __missing__(self, key):
        return "{" + key + "}"


class Conversation:
    def __init__(self, config: Config, llm: LLM, campaign: Campaign | None = None):
        self.config = config
        self.llm = llm
        self.campaign = campaign or get_store().active()
        self.persona = build_persona(config, self.campaign)
        self.memory = get_memory()
        self.ctx = ConversationState()
        self.ended = False
        # With no API key configured we run on the compliance script. The mock
        # LLM only heuristically guesses at phrasing, and a wrong guess here
        # would be *spoken to a live customer* — so we never let it generate.
        try:
            self._llm_ready = bool(llm.models_available())
        except Exception:
            self._llm_ready = False

    # ---------------- helpers ----------------

    def _fmt(self, text: str) -> str:
        c = self.campaign
        lo = hi = ""
        for q in c.questions:
            if q.kind == "age" and q.age_min and q.age_max:
                lo, hi = q.age_min, q.age_max
                break
        values = _SafePlaceholders(
            name=c.agent_name,
            agent=c.agent_name,
            agent_name=c.agent_name,
            campaign=c.business_name,
            business=c.business_name,
            business_name=c.business_name,
            min_age=lo,
            max_age=hi,
        )
        try:
            out = (text or "").format_map(values).strip()
        except Exception:
            # A malformed spec (e.g. "{min_age!r:>5}") must not kill the call.
            out = (text or "").strip()
        # Never let raw template syntax reach the customer's ear.
        out = re.sub(r"\{[^{}]*\}", "", out)
        return re.sub(r"\s{2,}", " ", out).strip()

    def _recent_turns(self) -> str:
        turns = self.ctx.transcript[-6:]
        return "\n".join(
            f"{'You' if t['role'] == 'ai' else 'Customer'}: {t['text']}"
            for t in turns
        )

    def _llm_line(self, prompt: str) -> str | None:
        """Return a clean LLM line, or None to use the compliance fallback.

        The LLM wrapper returns error text (e.g. "[LLM error: ...]") instead of
        raising, so we must detect that and fall back — a customer should never
        hear an error, only the correct scripted line.

        When no API key is configured (`models_available()` is False) we skip
        generation entirely: the scripted compliance line is guaranteed
        correct, whereas an offline mock's guess is not.
        """
        if not self._llm_ready:
            return None
        try:
            line = self.llm.chat(
                [
                    {"role": "system", "content": self.persona},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.7,
                max_tokens=120,
            ).strip().strip('"')
            if not line:
                return None
            if line.startswith("[LLM error") or line.startswith("[Unknown provider"):
                return None
            return line
        except Exception:
            return None

    def _few_shot(self, qid: str, utterance: str) -> list[dict]:
        try:
            return self.memory.few_shot(qid, utterance, k=3)
        except Exception:
            return []

    def _speak(self, fallback: str, customer_text: str | None, *, qid: str,
               ask_line: str, verdict: str, kind: str) -> str:
        """Naturalize a compliance-correct line via the LLM; fallback on error."""
        prompt = naturalize_prompt(
            self.config,
            ask_line=ask_line,
            verdict=verdict,
            fallback=fallback,
            recent_turns=self._recent_turns(),
            customer_utterance=customer_text,
            few_shot=self._few_shot(qid, customer_text or ""),
            sentiment=self.ctx.sentiment,
            kind=kind,
        )
        line = self._llm_line(prompt)
        return line if line else self._fmt(fallback)

    # ---------------- flow ----------------

    def start(self) -> str:
        fallback = self.campaign.lines.get("greet",
            "Hi, this is {name} from {campaign}. How are you today?")
        prompt = (
            f"You are starting the call. Warmly introduce yourself and ask how they are today.\n"
            f"Suggested: \"{self._fmt(fallback)}\"\n\n"
            "Reply with ONLY the line you would speak."
        )
        line = self._llm_line(prompt) or self._fmt(fallback)

        # Compliance: speak the campaign's recorded-call disclosure right after
        # the greeting (never silently dropped, never invented beyond the line).
        disclosure = (self.campaign.disclosure or "").strip()
        if disclosure:
            line = f"{line} {self._fmt(disclosure)}"

        self.ctx.transcript.append({"role": "ai", "text": line})
        self.ctx.last_ai_line = line
        self.ctx.state = "WAIT_FOR_RESPONSE"
        return line

    def hear(self, customer_text: str) -> BeatResult:
        if self.ended:
            return BeatResult(ai_speech=None, end_call=True)

        self.ctx.transcript.append({"role": "customer", "text": customer_text})
        self.ctx.sentiment = self._sentiment(customer_text)

        # Compliance: DNC and busy are honored at any point.
        if detect_dnc(customer_text):
            self.ctx.dnc = True
            self._finalize("AI-DNC")
            return BeatResult(ai_speech=None, end_call=True, disposition="AI-DNC")
        if detect_busy(customer_text):
            self._finalize("AI-BUSY")
            line = self._speak(
                self.campaign.lines.get("busy_close", "No problem. Have a great day."),
                customer_text, qid="busy", ask_line="", verdict="busy", kind="yes_no",
            )
            return BeatResult(ai_speech=line, end_call=True, disposition="AI-BUSY")

        # Right after the greeting: ask the first question.
        if self.ctx.state == "WAIT_FOR_RESPONSE":
            return self._ask(0, customer_text)

        if not (0 <= self.ctx.q_index < len(self.campaign.questions)):
            self._finalize("AI-UNCLEAR")
            return BeatResult(ai_speech=None, end_call=True, disposition="AI-UNCLEAR")

        q = self.campaign.questions[self.ctx.q_index]
        verdict = self._verdict(q, customer_text)
        self.ctx.answers[q.id] = verdict
        if q.id == "medicare":
            self.ctx.medicare_status = verdict
        return self._route(q, verdict, customer_text)

    # ---------------- gates ----------------

    def _ask(self, idx: int, customer_text: str | None) -> BeatResult:
        self.ctx.q_index = idx
        self.ctx.state = "IN_QUESTION"
        self.ctx.clarify_attempts = 0
        q = self.campaign.questions[idx]
        line = self._speak(q.ask_line, customer_text, qid=q.id,
                           ask_line=q.ask_line, verdict="ask", kind=q.kind)
        self.ctx.transcript.append({"role": "ai", "text": line})
        self.ctx.last_ai_line = line
        return BeatResult(ai_speech=line, end_call=False)

    def _verdict(self, q: Question, text: str) -> str:
        if q.kind == "age":
            age_res = extract_age(text)
            if age_res.age is not None:  # require a real number to verify age
                self.ctx.age = age_res.age
                if q.age_min is not None and age_res.age < q.age_min:
                    return "FAIL"
                if q.age_max is not None and age_res.age > q.age_max:
                    return "FAIL"
                return "PASS"
            return "UNCLEAR"
        if q.kind == "yes_no":
            return classify_yes_no(text)
        if q.kind == "keyword":
            return classify_keyword(text, q.pass_keywords)
        return "UNCLEAR"

    def _route(self, q: Question, verdict: str, text: str) -> BeatResult:
        route = q.routes.get(verdict, q.routes.get("UNCLEAR", "end:AI-UNCLEAR"))

        if route == "clarify":
            return self._clarify(q, verdict, text)

        if route == "next":
            nxt = self.ctx.q_index + 1
            if nxt < len(self.campaign.questions):
                return self._ask(nxt, text)
            return self._transfer(text)  # malformed campaign: default to transfer

        if route == "transfer":
            return self._transfer(text)

        if route.startswith("end:"):
            disp = route[4:]
            line = self._response_line(q, verdict, text)
            self._finalize(disp)
            return BeatResult(ai_speech=line, end_call=True, disposition=disp)

        self._finalize("AI-UNCLEAR")
        return BeatResult(ai_speech=None, end_call=True, disposition="AI-UNCLEAR")

    def _clarify(self, q: Question, verdict: str, text: str) -> BeatResult:
        self.ctx.clarify_attempts += 1
        if self.ctx.clarify_attempts > q.max_clarify:
            self._finalize("AI-UNCLEAR")
            return BeatResult(ai_speech=None, end_call=True, disposition="AI-UNCLEAR")
        line = self._response_line(q, verdict, text)
        self.ctx.transcript.append({"role": "ai", "text": line})
        self.ctx.last_ai_line = line
        return BeatResult(ai_speech=line, end_call=False)

    def _transfer(self, text: str | None) -> BeatResult:
        handoff = self.campaign.lines.get(
            "transfer_handoff",
            "Perfect, one moment and I'll connect you with our licensed agent.")
        # Prefer the passing question's own "pass" line if it has one.
        q = self.campaign.questions[self.ctx.q_index] if self.ctx.q_index >= 0 else None
        fallback = (q.lines.get("pass") if q and q.lines.get("pass") else handoff)
        line = self._speak(fallback, text, qid=(q.id if q else "transfer"),
                           ask_line=(q.ask_line if q else ""), verdict="PASS",
                           kind=(q.kind if q else "yes_no"))
        self._finalize("AI-TRANSFER")
        return BeatResult(
            ai_speech=line, end_call=True, disposition="AI-TRANSFER",
            classification={"age": self.ctx.age, "answers": dict(self.ctx.answers)},
        )

    def _response_line(self, q: Question, verdict: str, text: str) -> str:
        """Pick the fallback line for a verdict, then naturalize it."""
        v = verdict.lower()
        key_map = {
            "no": "no", "refuse": "refuse", "unsure": "unsure", "question": "question",
            "private_only": "private", "unclear": "repeat", "fail": "fail",
            "pass": "pass", "yes": "yes",
        }
        key = key_map.get(v, "repeat")
        fallback = q.lines.get(key) or q.lines.get("repeat") or q.ask_line
        return self._speak(fallback, text, qid=q.id, ask_line=q.ask_line,
                           verdict=verdict, kind=q.kind)

    # ---------------- memory / sentiment ----------------

    def _sentiment(self, text: str) -> str:
        try:
            from .brain import quick_sentiment
            return quick_sentiment(text)
        except Exception:
            return "neutral"

    def _finalize(self, disposition: str):
        self.ctx.disposition = disposition
        self.ended = True
        ended_beat = (self.campaign.questions[self.ctx.q_index].id
                      if 0 <= self.ctx.q_index < len(self.campaign.questions) else "greet")
        try:
            self.memory.record(
                outcome=disposition,
                medicare=self.ctx.medicare_status or self.ctx.answers.get("medicare", ""),
                age=self.ctx.age,
                sentiment=self.ctx.sentiment,
                ended_at_beat=ended_beat,
                turns=self.ctx.transcript,
            )
        except Exception:
            pass
