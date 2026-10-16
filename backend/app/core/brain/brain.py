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

        Plain stdlib HTTP (no SDK dependency) so the upgrade works in any
        deployment the moment a key exists in the environment:

            OPENAI_API_KEY / OPENAI_MODEL          (default gpt-4o-mini)
            ANTHROPIC_API_KEY / ANTHROPIC_MODEL    (default claude-sonnet-4-5)
            GEMINI_API_KEY (or GOOGLE_API_KEY) / GEMINI_MODEL
                                                   (default gemini-2.0-flash)

        Returns None (caller falls back to the local persona) on any failure —
        a live call must never die because a key expired or a host is down.
        """
        provider = self.llm_provider
        if not provider:
            return None
        try:
            import json as _json
            import urllib.request

            def _post(url: str, payload: dict, headers: dict, timeout: float = 8):
                req = urllib.request.Request(
                    url, data=_json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json", **headers})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return _json.loads(resp.read())

            if provider == "openai":
                data = _post(
                    "https://api.openai.com/v1/chat/completions",
                    {"model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
                     "messages": [{"role": "system", "content": system_prompt}] + messages,
                     "max_tokens": max_tokens, "temperature": temperature},
                    {"Authorization": f"Bearer {os.getenv('OPENAI_API_KEY', '')}"})
                text = (data["choices"][0]["message"].get("content") or "")
                return text.strip() or None

            if provider == "anthropic":
                data = _post(
                    "https://api.anthropic.com/v1/messages",
                    {"model": os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5"),
                     "max_tokens": max_tokens, "temperature": temperature,
                     "system": system_prompt, "messages": messages},
                    {"x-api-key": os.getenv("ANTHROPIC_API_KEY", ""),
                     "anthropic-version": "2023-06-01"})
                parts = data.get("content") or []
                text = parts[0].get("text", "") if parts else ""
                return text.strip() or None

            if provider == "gemini":
                model = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
                key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""
                contents = [
                    {"role": "user" if m.get("role") == "user" else "model",
                     "parts": [{"text": m.get("content", "")}]}
                    for m in messages
                ]
                data = _post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/"
                    f"{model}:generateContent?key={key}",
                    {"contents": contents,
                     "systemInstruction": {"parts": [{"text": system_prompt}]},
                     "generationConfig": {"maxOutputTokens": max_tokens,
                                          "temperature": temperature}},
                    {})
                cands = data.get("candidates") or [{}]
                parts = (cands[0].get("content") or {}).get("parts") or []
                text = parts[0].get("text", "") if parts else ""
                return text.strip() or None
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
