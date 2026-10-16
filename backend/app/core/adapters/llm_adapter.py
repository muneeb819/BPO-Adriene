"""LLM adapter interface and a mock implementation.

Mock returns short scripted responses for testing. Real providers (OpenAI) can be added later.
"""
import os
from typing import List

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "mock")

class LLMAdapter:
    def generate(self, system_prompt: str, messages: List[dict]) -> str:
        raise NotImplementedError

class MockLLM(LLMAdapter):
    def generate(self, system_prompt: str, messages: List[dict]) -> str:
        # Very simple rule-based fallback for local testing
        last_user = None
        for m in reversed(messages):
            if m.get('role') == 'user':
                last_user = m.get('content')
                break
        if not last_user:
            return "Hello, I'm Adriene. May I ask if you have Medicare?"
        u = last_user.lower()
        if 'medicare' in u:
            return "Thanks for confirming. Are you between 45 and 80 years old?"
        if any(tok in u for tok in ['yes', 'y', 'yeah']):
            return "Thank you. One moment while I transfer you."
        if any(tok in u for tok in ['no', 'not']):
            return "Understood. Thank you for your time."
        # default
        return "I didn't catch that. Could you please repeat?"


def get_llm_adapter() -> LLMAdapter:
    if LLM_PROVIDER == 'mock':
        return MockLLM()
    raise NotImplementedError(f"LLM provider {LLM_PROVIDER} not implemented")
