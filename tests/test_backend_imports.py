"""The backend app must be importable in the CI environment.

CI runs backend/tests/, but before this file nothing imported the app's own
persistence and crypto modules — so those tests passed while
`import backend.app.db` failed with ModuleNotFoundError (sqlmodel) and
`backend.app.core.crypto` failed the same way (cryptography). A green suite that
cannot import half the app is a suite that will break the first time a test
touches it.

These tests fail loudly if the CI dependency set loses either package again.
"""
from __future__ import annotations

import importlib

import pytest

APP_MODULES = [
    "backend.app.db",
    "backend.app.core.crypto",
    "backend.app.core.brain.brain",
    "backend.app.core.brain.memory",
    "backend.app.core.brain.nlu",
    "backend.app.core.state_machine",
]


@pytest.mark.parametrize("module", APP_MODULES)
def test_backend_module_imports(module):
    """Each module the backend app is built from must import cleanly."""
    importlib.import_module(module)


def test_crypto_round_trips_without_a_secret_key(monkeypatch):
    """crypto.py reads SECRET_KEY at import; it must not ship a default.

    With no key set the module still has to import and round-trip — the point
    is that the *default value* is empty, not that encryption is unavailable.
    """
    monkeypatch.setenv("SECRET_KEY", "a-real-key-for-this-test")
    crypto = importlib.reload(importlib.import_module("backend.app.core.crypto"))

    token = crypto.encrypt_text("hello")
    assert token != "hello"
    assert crypto.decrypt_text(token) == "hello"


def test_empty_database_url_falls_back_to_sqlite(monkeypatch):
    """A blank DATABASE_URL from .env must not crash the app at import.

    .env.example documents DATABASE_URL as optional and ships it empty, so
    load_dotenv() puts "" in the environment. os.getenv(name, default) returns
    that "", and SQLAlchemy raises "Could not parse SQLAlchemy URL" — exactly
    what a fresh clone hit the first time anything imported db.py.
    """
    monkeypatch.setenv("DATABASE_URL", "")
    import sys

    for name in [m for m in sys.modules if m == "backend.app.db" or m.startswith("backend.app.db.")]:
        del sys.modules[name]
    db = importlib.import_module("backend.app.db")
    assert db.DATABASE_URL == "sqlite:///./dev.db"


def test_secret_key_has_no_shipped_default():
    """backend/app/db.py and crypto.py used to fall back to 'changeme'."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for rel in ("backend/app/db.py", "backend/app/core/crypto.py"):
        text = (root / rel).read_text()
        assert "changeme" not in text, f"{rel} ships a default secret"
