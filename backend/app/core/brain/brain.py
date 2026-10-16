"""Brain facade — one object that owns memory + persona + optional real LLM.

``Brain.answer(text)`` is the *stateless* path (used by the LLM adapter for
scripted/one-shot generations): it applies the same NLU, knowledge and
compliance layers as the dialogue manager, just without script state.

``Brain.llm_available`` / ``Brain.generate`` implement the upgrade path: when
``OPENAI_API_KEY`` / ``ANTHROPIC_API_KEY`` / ``GEMINI_API_KEY`` appears in the
environment the wording can be delegated to a real model — decisions stay in
the policy layer, only phrasing moves to the model.
"""
from __future__ import annotations

import os
from typing import Dict, List, Optional

from .nlu import analyze
from .knowledge import retrieve as kb_retrieve
from .persona import Persona, PROFILE
from .memory import Memory, get_memory
from . import compliance


class Brain:
    def __init__(self, session_id: str = "stateless", memory: Optional[Memory] = None):
        self.session_id = session_id
        self.memory = memory or get_memory()
        self.persona = Persona(seed_key=session_id)

    # ------------------------------------------------------------- upgrade
    @property
    def llm_provider(self) -> Optional[str]:
        """First configured cloud LLM key in the environment, if any."""
        if os.getenv("OPENAI_API_KEY"):
            return "openai"
        if os.getenv("ANTHROPIC_API_KEY"):
            return "anthropic"
        if os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"):
            return "gemini"
        return None

    @property
    def llm_available(self) -> bool:
        return self.llm_provider is not None

    def generate(self, system_prompt: str, messages: List[dict],
                 max_tokens: int = 400, temperature: float = 0.6) -> Optional[str]:
        """Optional cloud re-wording of a decision made by the policy layer.

        Returns None (caller falls back to the local persona) on any failure —
        a live call must never die because a key expired.
        """
        provider = self.llm_provider
        if not provider:
            return None
        try:
            if provider == "openai":
                import openai
                client = openai.OpenAI()
                r = client.chat.completions.create(
                    model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
                    messages=[{"role": "system", "content": system_prompt}] + messages,
                    max_tokens=max_tokens, temperature=temperature,
                    timeout=8,
                )
                return (r.choices[0].message.content or "").strip() or None
            if provider == "anthropic":
                import anthropic
                client = anthropic.Anthropic()
                r = client.messages.create(
                    model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5"),
                    max_tokens=max_tokens, temperature=temperature,
                    system=system_prompt, messages=messages, timeout=8,
                )
                return (r.content[0].text if r.content else "").strip() or None
        except Exception as e:                       # noqa: BLE001
            print(f"[brain] LLM re-word failed ({provider}): {e}")
        return None

    # ---------------------------------------------------------- stateless
    def answer(self, text: str) -> str:
        """One-shot understanding + grounded reply (no script state)."""
        ut = analyze(text, self.memory)
        if ut.intent == "dnc" or compliance.is_dnc_request(ut.norm):
            self.memory.count("dnc")
            return compliance.dnc_confirmation()
        if ut.intent == "farewell":
            return self.persona.farewell()
        if ut.intent == "identity_query":
            return self.persona.identity_answer("")
        entry, score = kb_retrieve(ut.norm)
        if entry is not None and score >= 0.66:
            self.memory.count("knowledge_answers")
            safe, _ = compliance.ensure_compliant(entry.answer)
            return safe
        if ut.intent in ("greeting", "smalltalk") or not text.strip():
            return self.persona.greeting()
        if ut.intent == "thanks":
            return self.persona.thanks()
        if ut.intent in ("human_request", "language"):
            return self.persona.escalate_to_human() + " " + compliance.transfer_disclosure()
        # generic conversational fallback — always ends with a real question
        return (
            f"{self.persona.empathy(ut.sentiment)} Just so I get it right — "
            "I'm Adriene, calling about Medicare options. Do you currently "
            "have Medicare?"
        )


_brain: Optional[Brain] = None


def get_brain() -> Brain:
    global _brain
    if _brain is None:
        _brain = Brain()
    return _brain
