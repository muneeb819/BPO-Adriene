"""Shared fixtures for the test suite.

Every fixture redirects a persistent resource (campaign store, learned memory,
call log) to a throwaway tmp_path so running the tests never mutates real
project data under data/.
"""
from __future__ import annotations

import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
# Stores use project-relative paths (data/...); pin cwd to the project root.
os.chdir(ROOT)


class ScriptedLLM:
    """Deterministic LLM double.

    Returns an empty string, which Conversation._llm_line() treats as "no LLM
    line available" -> the engine falls back to the compliance-correct script.
    That makes gate assertions exact instead of dependent on model wording.
    """

    def chat(self, messages, temperature=0.7, max_tokens=1500) -> str:
        return ""

    def chat_json(self, messages, temperature=0.3) -> dict:
        return {"intent": "UNCLEAR", "confidence": 0.5}

    def models_available(self) -> bool:
        return False


@pytest.fixture()
def llm() -> ScriptedLLM:
    return ScriptedLLM()


@pytest.fixture()
def tmp_store(tmp_path, monkeypatch):
    """Fresh campaign store that seeds the 4 built-in campaigns."""
    import agent.campaign as camp

    store = camp.CampaignStore(path=str(tmp_path / "campaigns.json"))
    monkeypatch.setattr(camp, "_STORE", store)
    return store


@pytest.fixture()
def tmp_memory(tmp_path, monkeypatch):
    """Fresh self-learning memory so insights/record calls stay in tmp."""
    import agent.memory as mem

    memory = mem.CallMemory(path=str(tmp_path / "memory.json"))
    monkeypatch.setattr(mem, "_MEMORY", memory)
    return memory


@pytest.fixture()
def call_log_tmp(tmp_path, monkeypatch):
    import server.app as appmod

    monkeypatch.setattr(appmod, "CALL_LOG_PATH", str(tmp_path / "call_log.json"))
    monkeypatch.setattr(appmod, "CALL_LOG", [])
    return appmod
