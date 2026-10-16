"""LLM adapter interface — Mock now delegates to the real brain.

The local "mock" is no longer canned text: ``MockLLM`` routes through
``core.brain`` (NLU + Medicare knowledge + persona + compliance + memory), so
every consumer of the LLM adapter gets the same intelligence as the live
WebSocket call flow. Real providers (OpenAI/Anthropic/Gemini) are picked up
automatically by ``Brain.generate`` when a key exists in the environment.
"""
import os
from typing import List

from ..brain import get_brain


class LLMAdapter:
    def generate(self, system_prompt: str, messages: List[dict]) -> str:
        raise NotImplementedError


class MockLLM(LLMAdapter):
    """Local brain — understands any reply, answers Medicare questions,
    stays compliant, and learns (see core/brain/)."""

    def generate(self, system_prompt: str, messages: List[dict]) -> str:
        brain = get_brain()
        last_user = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                last_user = m.get("content") or ""
                break
        # optional upgrade: real model re-words the decision when a key exists
        if brain.llm_available:
            out = brain.generate(system_prompt, messages)
            if out:
                return out
        if not last_user:
            return brain.persona.greeting()
        return brain.answer(last_user)


def get_llm_adapter() -> LLMAdapter:
    # Provider is read at call time so runtime switching works. Every value
    # (mock/openai/anthropic/gemini/unknown) maps to the local brain first —
    # Brain.generate upgrades wording automatically when a key is present.
    return MockLLM()
