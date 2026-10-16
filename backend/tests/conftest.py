"""Test isolation: every test gets a private brain memory (no cross-talk with
the live brain_memory.json, and no pollution between tests)."""
import pytest

from app.core.brain import memory as memory_mod
from app.core.brain import brain as brain_mod


@pytest.fixture(autouse=True)
def isolated_memory(tmp_path, monkeypatch):
    private = memory_mod.Memory(path=tmp_path / "brain_memory.json")
    monkeypatch.setattr(memory_mod, "_singleton", private)
    monkeypatch.setattr(brain_mod, "_brain", None)   # Brain caches a memory too
    yield private
    monkeypatch.setattr(memory_mod, "_singleton", None)
    monkeypatch.setattr(brain_mod, "_brain", None)
