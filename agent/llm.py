"""LLM provider abstraction: OpenAI, Anthropic, or local Ollama."""
from __future__ import annotations

import json
import re

from .config import Config


class LLM:
    def __init__(self, config: Config):
        self.config = config
        self.provider = config.llm_provider.lower()
        self._anthropic = None

    @staticmethod
    def _strip(text: str) -> str:
        return re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL).strip()

    def chat(self, messages: list[dict], temperature: float = 0.7, max_tokens: int = 1500) -> str:
        try:
            if self.provider == "openai":
                return self._strip(self._chat_openai(messages, temperature, max_tokens))
            if self.provider == "gemini":
                return self._strip(self._chat_gemini(messages, temperature, max_tokens))
            if self.provider == "anthropic":
                return self._strip(self._chat_anthropic(messages, temperature, max_tokens))
            if self.provider == "ollama":
                return self._strip(self._chat_ollama(messages, temperature, max_tokens))
        except Exception as e:
            return f"[LLM error: {e}]"
        return f"[Unknown provider: {self.provider}]"

    def chat_json(self, messages: list[dict], temperature: float = 0.3) -> dict:
        raw = self.chat(messages, temperature=temperature, max_tokens=1500)
        return self._extract_json(raw)

    @staticmethod
    def _extract_json(raw: str) -> dict:
        try:
            return json.loads(raw)
        except Exception:
            match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(0))
                except Exception:
                    pass
        return {}

    def _chat_openai(self, messages, temperature, max_tokens) -> str:
        from openai import OpenAI

        kwargs = {"api_key": self.config.openai_api_key}
        if self.config.openai_base_url:
            kwargs["base_url"] = self.config.openai_base_url
        client = OpenAI(**kwargs)
        resp = client.chat.completions.create(
            model=self.config.openai_model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return resp.choices[0].message.content.strip()

    def _chat_gemini(self, messages, temperature, max_tokens) -> str:
        """Gemini via its OpenAI-compatible endpoint (no extra dependency)."""
        from openai import OpenAI

        client = OpenAI(
            api_key=self.config.gemini_api_key,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        )
        resp = client.chat.completions.create(
            model=self.config.gemini_model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return resp.choices[0].message.content.strip()

    def _chat_anthropic(self, messages, temperature, max_tokens) -> str:
        if self._anthropic is None:
            import anthropic

            self._anthropic = anthropic.Anthropic(api_key=self.config.anthropic_api_key)
        system = ""
        convo = []
        for m in messages:
            if m["role"] == "system":
                system = m["content"]
            else:
                convo.append({"role": m["role"], "content": m["content"]})
        resp = self._anthropic.messages.create(
            model=self.config.anthropic_model,
            system=system,
            messages=convo,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return "".join(block.text for block in resp.content).strip()

    def _chat_ollama(self, messages, temperature, max_tokens) -> str:
        import requests

        url = f"{self.config.ollama_base_url.rstrip('/')}/api/chat"
        payload = {
            "model": self.config.ollama_model,
            "messages": messages,
            "options": {"temperature": temperature},
            "stream": False,
        }
        r = requests.post(url, json=payload, timeout=180)
        r.raise_for_status()
        return r.json()["message"]["content"].strip()

    def models_available(self) -> bool:
        if self.provider == "ollama":
            try:
                import requests

                r = requests.get(f"{self.config.ollama_base_url.rstrip('/')}/api/tags", timeout=5)
                return r.status_code == 200
            except Exception:
                return False
        return bool(
            (self.provider == "openai" and self.config.openai_api_key)
            or (self.provider == "anthropic" and self.config.anthropic_api_key)
            or (self.provider == "gemini" and self.config.gemini_api_key)
        )
