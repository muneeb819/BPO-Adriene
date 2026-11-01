"""Logging setup: structured output, levels, and idempotent configuration.

Before agent/logging_config.py the project configured no logging at all, so the
modules that did log fell through to Python's last-resort handler (no timestamp,
no logger name, no level) and several failures were swallowed into bare
``except`` blocks. These tests pin the behaviour that replaced it.
"""
from __future__ import annotations

import json
import logging

import pytest

from agent import logging_config as lc


def test_json_formatter_emits_one_object_per_line():
    formatter = lc.JsonFormatter()
    record = logging.LogRecord(
        name="adriene.test", level=logging.WARNING, pathname=__file__, lineno=1,
        msg="voice engine failed: %s", args=("piper",), exc_info=None)

    payload = json.loads(formatter.format(record))

    assert payload["level"] == "WARNING"
    assert payload["logger"] == "adriene.test"
    assert payload["msg"] == "voice engine failed: piper"   # args are interpolated
    assert "ts" in payload


def test_json_formatter_keeps_structured_fields():
    """extra=... is how a call id or engine name travels with the message."""
    formatter = lc.JsonFormatter()
    record = logging.LogRecord("adriene.server", logging.INFO, __file__, 1,
                               "server ready", None, None)
    record.call_id = "abc123"
    record.tts_provider = "kokoro"

    payload = json.loads(formatter.format(record))

    assert payload["call_id"] == "abc123"
    assert payload["tts_provider"] == "kokoro"


def test_json_formatter_includes_a_traceback():
    formatter = lc.JsonFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        import sys
        record = logging.LogRecord("adriene.server", logging.ERROR, __file__, 1,
                                   "failed", None, sys.exc_info())
    payload = json.loads(formatter.format(record))
    assert "ValueError: boom" in payload["exc"]


def test_configure_is_idempotent(monkeypatch):
    """Importing a module that also configures logging must not rewire handlers."""
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    lc.configure(force=True)
    logger = lc.get_logger("tests")
    handlers_after_first = list(logger.handlers)

    lc.configure(level="ERROR")           # second call, no force

    assert logger.handlers == handlers_after_first
    assert lc.get_logger("tests").name == "adriene.tests"


def test_configure_respects_log_level(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    lc.configure(force=True)
    assert lc.get_logger().level == logging.WARNING

    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    lc.configure(force=True)
    assert lc.get_logger().level == logging.DEBUG


def test_configure_falls_back_to_info_on_a_bad_level(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "LOUD")
    lc.configure(force=True)
    assert lc.get_logger().level == logging.INFO


def test_get_logger_is_namespaced():
    assert lc.get_logger().name == lc.LOGGER_NAME
    assert lc.get_logger("server").name == f"{lc.LOGGER_NAME}.server"


@pytest.mark.parametrize("model", ["agent", "server", "backend"])
def test_no_shipped_default_secret(model):
    """A default key in source is a known key — see the launchers' SECRET_KEY check."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for path in (root / model).rglob("*.py"):
        text = path.read_text(errors="ignore")
        assert '"changeme"' not in text and "'changeme'" not in text, \
            f"{path.relative_to(root)} ships a default secret"
        assert "dev-secret" not in text, f"{path.relative_to(root)} ships a default secret"
