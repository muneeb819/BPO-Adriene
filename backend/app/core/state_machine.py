"""Dialogue policy for Adriene — a state machine that handles *any* utterance.

The old machine only accepted literal ``yes`` / ``no`` / digits. This one runs
every caller turn through the brain first (NLU + knowledge + persona +
compliance) and then applies policy, so at *any* state it can:

  * answer Medicare / policy / compliance questions without losing the script
  * handle objections (not interested, scam?, who is this), callbacks,
    complaints, do-not-call (compliance-owned wording), language requests
  * understand paraphrased yes/no, ages in words, plan statements, hedged
    answers ("I think I might have it")
  * repair confusion with escalating rephrasing -> explicit options -> warm
    human handoff (never a dead "I didn't understand" loop)
  * learn: heuristic interpretations are stored in memory; unknowns are queued;
    feedback corrections are applied on later calls

Public API is unchanged: ``CallSession(id)``, ``.state``, ``.data``,
``get_next_bot_prompt()``, ``process_user_utterance(text) -> (reply, transfer)``.
"""
from __future__ import annotations

import re
from typing import Dict, Optional, Tuple

from .brain.nlu import analyze, Utterance
from .brain.persona import Persona
from .brain.memory import get_memory, Memory
from .brain.knowledge import retrieve as kb_retrieve
from .brain import compliance


# plan entities that mean "yes, has Medicare"
_MEDICARE_PLANS = {"advantage", "medigap", "part_a", "part_b", "part_d", "dual"}
# plan entities that mean "covered, but not Medicare (yet)"
_OTHER_COVERAGE = {"medicaid", "employer", "tricare", "va", "marketplace", "lis"}

_Q_STATES = ("ask_medicare", "ask_age")


class CallSession:
    def __init__(self, session_id: str, memory: Optional[Memory] = None):
        self.session_id = session_id
        self.state = "greeting"
        self.pending = "ask_medicare"
        self.data: Dict = {
            "has_medicare": None,
            "age": None,
            "age_verified": None,
            "plan": None,
            "name": None,
            "dnc": False,
        }
        self.repair = 0              # consecutive misunderstandings (ladder)
        self.knowledge_turns = 0
        self._ni_count = 0           # repeated "not interested" turns
        self.persona = Persona(seed_key=session_id)
        self.memory = memory or get_memory()

    # ------------------------------------------------------------------ api
    def get_next_bot_prompt(self) -> str:
        if self.state == "greeting":
            self.state = "ask_medicare"
            self.pending = "ask_medicare"
            self.memory.count("calls")
            return self.persona.humanize(self.persona.greeting())
        return self._pending_question()

    def process_user_utterance(self, text: str) -> Tuple[str, bool]:
        """Understand one turn and reply. Returns (reply, should_transfer)."""
        ut = analyze(text, self.memory)
        self.memory.remember("caller", ut.raw)
        self.memory.count(ut.intent)

        # ---- closed call: stay warm but don't resurrect the script ---------
        if self.state == "ended":
            return (self.persona.farewell(), False)
        if self.state in ("not_eligible", "eligible", "transferring"):
            return ("Thank you again — take care now!", False)

        # ---- do-not-call: compliance owns the wording (never varied) -------
        if ut.intent == "dnc" or compliance.is_dnc_request(ut.norm):
            self.state = "ended"
            self.data["dnc"] = True
            self.memory.count("dnc")
            self.memory.learn(ut.norm, "dnc", None, "dialogue")
            return (compliance.dnc_confirmation(), False)

        # ---- conversation-level intents (valid in every state) -------------
        if ut.intent == "farewell":
            self.state = "ended"
            return (self.persona.farewell(), False)

        if ut.intent == "complaint":
            self.memory.count("complaints")
            return (self._with_bridge(self.persona.complaint_response()), False)

        if ut.intent == "human_request":
            self.memory.count("transfers")
            self.memory.learn(ut.norm, "human_request", None, "dialogue")
            return (
                "Absolutely — I'll transfer you to a licensed specialist right now. "
                + compliance.transfer_disclosure(),
                True,
            )

        if ut.intent == "language":
            self.memory.learn(ut.norm, "language", None, "dialogue")
            return (self.persona.language_offer() + " " + compliance.transfer_disclosure(), True)

        if ut.intent == "identity_query":
            if any(w in ut.norm for w in ("scam", "spam", "robocall", "robot",
                                          "real", "legitimate", "verify")):
                reply = self.persona.scam_answer(self._pending_or_empty())
            else:
                reply = self.persona.identity_answer(self._pending_or_empty())
            return (reply, False)

        if ut.intent == "callback":
            self.state = "ended"
            self.memory.count("callbacks")
            self.memory.learn(ut.norm, "callback", None, "dialogue")
            return (self.persona.callback_ack(ut.entities.get("time_ref")), False)

        if ut.intent == "not_interested":
            self._ni_count += 1
            if self._ni_count >= 2:
                self.state = "ended"
                return ("No pressure at all — I'll let you go. Take care!", False)
            return (self.persona.not_interested(), False)

        if ut.intent == "thanks":
            return (self._with_bridge(self.persona.thanks()), False)

        if ut.intent in ("smalltalk", "greeting"):
            # a greeting glued to a real answer ("Hey! yeah I've had it for
            # years") is an ANSWER — skip smalltalk and fall to the script
            has_answer = (ut.polarity is not None or bool(ut.entities.get("age"))
                          or bool(ut.entities.get("plan")))
            if not has_answer:
                # the smalltalk lines already carry a transition ("…so, about
                # Medicare…") — follow with the plain pending question, no bridge
                if self.pending in _Q_STATES:
                    return f"{self.persona.smalltalk()} {self._pending_question()}".strip(), False
                return self.persona.smalltalk(), False

        if ut.intent == "question_invite":
            return ("Of course — I'm all ears. What would you like to know?", False)

        if ut.intent == "consent_continue":
            return (self._pending_question(), False)

        if ut.intent == "confused":
            return self._repair(ut)

        # ---- knowledge: answer from the KB, absorb embedded answers --------
        # (if the KB has no entry — e.g. "yep, I've had Medicare since 2019" —
        #  fall through to the script below so the answer still counts)
        k_entry = kb_retrieve(ut.norm)[0] if ut.intent == "knowledge_question" else None
        if k_entry is not None:
            entry = k_entry
            self.knowledge_turns += 1
            self.memory.count("knowledge_answers")
            # a question may still carry the script answer ("Yes, and what's Part B?")
            outcome = self._absorb(ut)
            if outcome == "transfer":
                return (self._transfer_reply(), True)
            if outcome == "not_eligible":
                return (self._not_eligible_reply(), False)
            correction = self.memory.correction_for(entry.id)
            answer = correction or entry.answer
            extra = ""
            if self.knowledge_turns >= 3 and self.pending in _Q_STATES:
                extra = " We're nearly done with two quick checks, I promise."
            # tail: keep ONE question on the table — the script question when
            # it's still open, else the KB's own follow-up (never two at once)
            if self.pending in _Q_STATES:
                tail = self._pending_question()
            else:
                tail = entry.follow_up or ""
            reply = self.persona.k_answer_bridge(answer, "", follow_up=tail or None)
            return ((reply + extra).strip(), False)

        # ---- the script itself: absorb the answer --------------------------
        outcome = self._absorb(ut)
        if outcome == "transfer":
            return (self._transfer_reply(), True)
        if outcome == "not_eligible":
            return (self._not_eligible_reply(), False)
        if outcome == "aged":
            # age given before the pending question was answered —
            # acknowledge it (understanding beats a re-ask) and continue
            self.repair = 0
            ack = self.persona.age_ack(int(self.data.get("age") or 0))
            return (f"{ack} {self._pending_question()}".strip(), False)
        if outcome == "advanced":
            self.repair = 0
            ack = self._ack_for(ut)
            return (f"{ack} {self._pending_question()}".strip(), False)

        # ---- nothing understood: escalating repair ladder ------------------
        return self._repair(ut)

    # ------------------------------------------------------------- internals
    def _pending_question(self) -> str:
        if self.pending == "ask_age":
            return self.persona.ask_age(0)
        return self.persona.ask_medicare(0)

    def _rephrased_question(self) -> str:
        v = min(self.repair, 3)
        if self.pending == "ask_age":
            return self.persona.ask_age(v)
        return self.persona.ask_medicare(v)

    def _pending_or_empty(self) -> str:
        return self._pending_question() if self.pending in _Q_STATES else ""

    def _with_bridge(self, lead: str) -> str:
        if self.pending in _Q_STATES:
            return f"{lead} {self.persona.bridge(self.pending)}".strip()
        return lead

    def _ack_for(self, ut: Utterance) -> str:
        if ut.entities.get("plan"):
            return self.persona.coverage_ack(ut.entities["plan"])
        if ut.polarity is True:
            return self.persona.acknowledge_yes()
        if ut.polarity is False:
            return self.persona.acknowledge_no()
        if ut.entities.get("age"):
            return self.persona.age_ack(ut.entities["age"]["age"])
        return "Great,"

    def _absorb(self, ut: Utterance) -> str:
        """Apply this utterance to the pending script question.

        Returns one of: 'advanced' (move to next question), 'transfer',
        'not_eligible', 'none' (couldn't interpret).
        """
        d = self.data
        plan = ut.entities.get("plan")
        age_ent = ut.entities.get("age")
        pol = ut.polarity

        # state-aware interpretation: mention of Medicare with no negation
        if (self.pending == "ask_medicare" and pol is None and plan is None
                and "medicare" in ut.norm and not re.search(r"\b(not|never|no|hardly)\b", ut.norm)):
            pol, ut.polarity_source = True, "medicare-mention"

        if self.pending == "ask_medicare":
            changed = False
            if plan and "question" not in ut.flags:
                # a *question* about a plan ("what is Part D?") isn't a claim
                # that the caller has it — only statements absorb as coverage
                d["plan"] = plan
                if plan in _MEDICARE_PLANS:
                    d["has_medicare"] = True
                elif plan in _OTHER_COVERAGE:
                    d["has_medicare"] = False
                changed = True
            elif pol is not None:
                d["has_medicare"] = pol
                changed = True
            if age_ent:
                d["age"] = age_ent["age"]
            if not changed:
                if age_ent:
                    return "aged"   # heard + stored the age; still need the answer
                return "none"
            self._auto_learn(ut)
            if d.get("age") is not None or d.get("age_verified") is not None:
                return self._decide()
            self.pending = "ask_age"
            self.state = "ask_age"
            return "advanced"

        if self.pending == "ask_age":
            changed = False
            if age_ent:
                d["age"] = age_ent["age"]
                changed = True
            if plan:
                # plan talk answers the MEDICARE question, not the age one —
                # "I've had Part B for years" must not count as "yes, I'm 45-80"
                d["has_medicare"] = plan not in _OTHER_COVERAGE
                changed = True
            if pol is not None and age_ent is None and not plan:
                d["age_verified"] = pol
                changed = True
            if not changed:
                return "none"
            self._auto_learn(ut)
            return self._decide()

        return "none"

    def _decide(self) -> str:
        d = self.data
        has = d.get("has_medicare")
        age = d.get("age")
        age_ver = d.get("age_verified")
        if age is not None:
            age_good: Optional[bool] = 45 <= age <= 80
        else:
            age_good = age_ver
        if has is None or age_good is None:
            # not enough yet — make sure we're on the next open question
            if self.pending == "ask_medicare":
                self.pending = "ask_age"
                self.state = "ask_age"
            return "advanced"
        if has and age_good:
            return "transfer"
        return "not_eligible"

    def _auto_learn(self, ut: Utterance) -> None:
        """Store heuristic interpretations so the next call starts smarter."""
        if ut.polarity is None:
            return                              # never learn a guess as a fact
        if ut.polarity_source in ("medicare-mention", "deny-scoped", "plan-context") \
                and len(ut.norm.split()) >= 2:
            self.memory.learn(ut.norm, "affirm" if ut.polarity else "deny",
                              ut.polarity, "dialogue")

    def _transfer_reply(self) -> str:
        self.state = "eligible"
        self.pending = "done"
        self.memory.count("transfers")
        line = self.persona.transfer_line()              # contains "transfer"
        safe, _ = compliance.ensure_compliant(line)
        return f"{safe} {compliance.transfer_disclosure()}"

    def _not_eligible_reply(self) -> str:
        self.state = "not_eligible"
        self.pending = "done"
        d = self.data
        if d.get("has_medicare") is False and d.get("plan") == "medicaid":
            reason = ("Medicaid is a separate program from Medicare, so this "
                      "particular Medicare benefit wouldn't apply to you —")
        elif d.get("has_medicare") is False:
            reason = ("Since you're not on Medicare right now, this particular "
                      "benefit wouldn't apply to you —")
        else:
            reason = ("This offer is for folks between 45 and 80, so I won't "
                      "take up any more of your time —")
        line = self.persona.not_eligible_line(reason)
        safe, _ = compliance.ensure_compliant(line)
        return safe

    def _repair(self, ut: Utterance) -> Tuple[str, bool]:
        """Escalating recovery: rephrase -> give options -> warm handoff."""
        self.repair += 1
        if ut.confidence < 0.5:
            self.memory.remember_unknown(ut.norm, self.state)
        lead = ""
        if ut.sentiment in ("frustrated", "negative"):
            lead = self.persona.empathy(ut.sentiment) + " "
        if self.repair <= 1:
            reply = f"{lead}{self.persona.confused()} {self._rephrased_question()}"
            return reply.strip(), False
        if self.repair == 2:
            reply = f"{lead}{self.persona.clarify_options(self._rephrased_question())}"
            return reply.strip(), False
        # third strike: stop wasting the caller's time — human handoff
        self.memory.count("transfers")
        reply = (f"{lead}{self.persona.escalate_to_human()} "
                 f"{compliance.transfer_disclosure()}")
        return reply.strip(), True
