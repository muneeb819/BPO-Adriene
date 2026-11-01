"""Logging setup: one place that decides the format and level for the process.

Before this module the project had no logging configuration at all — the
modules that do log (the LiveKit adapters, TTS engines) fell back to Python's
"last resort" handler, which prints a bare message with no timestamp, no
logger name and no level, while the server swallowed real failures inside bare
``except`` blocks.

Call :func:`configure` once, at process start (``server/app.py``,
``livekit_agent.py``, ``cli.py``). It is idempotent, so importing a module that
also calls it is harmless.

Two formats:

* ``json`` — one JSON object per line, for anything collecting logs (the
  default outside a TTY, and whenever ``LOG_FORMAT=json``);
* ``console`` — aligned human-readable output for a terminal.

``LOG_LEVEL`` (default ``INFO``) sets the level. ``LOG_FORMAT`` overrides the
format. Neither requires a dependency: this is ``logging.config.dictConfig``
plus a small formatter.
"""
from __future__ import annotations

import json
import logging
import logging.config
import os
import sys

LOGGER_NAME = "adriene"

# Attributes every LogRecord already has — anything else was passed via
# `extra=` and belongs in the JSON payload.
_RESERVED = frozenset(
    """name msg args levelname levelno pathname filename module exc_info exc_text
    stack_info lineno funcName created msecs relativeCreated thread threadName
    processName process taskName""".split()
)


class JsonFormatter(logging.Formatter):
    """Render a record as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Caller-supplied structured fields (logger.info("...", extra={"call_id": x}))
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def _wants_json(fmt: str) -> bool:
    if fmt:
        return fmt.lower() == "json"
    return not sys.stderr.isatty()


def configure(level: str = "", fmt: str = "", force: bool = False) -> None:
    """Configure logging for the process. Safe to call more than once."""
    if getattr(configure, "_done", False) and not force:
        return

    level = (level or os.environ.get("LOG_LEVEL", "INFO")).upper()
    fmt = fmt or os.environ.get("LOG_FORMAT", "")
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        level = "INFO"

    if _wants_json(fmt):
        formatter = {"()": "agent.logging_config.JsonFormatter"}
    else:
        formatter = {
            "format": "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            "datefmt": "%H:%M:%S",
        }

    logging.config.dictConfig({
        "version": 1,
        # Respect handlers a host application (uvicorn, pytest) already set up.
        "disable_existing_loggers": False,
        "formatters": {"default": formatter},
        "handlers": {
            "stderr": {
                "class": "logging.StreamHandler",
                "formatter": "default",
                "stream": "ext://sys.stderr",
            },
        },
        "loggers": {
            LOGGER_NAME: {"handlers": ["stderr"], "level": level, "propagate": False},
            # Third-party loggers are noisy at INFO; a misconfigured bot should
            # not be buried under httpx request lines.
            "httpx": {"level": "WARNING"},
            "httpcore": {"level": "WARNING"},
            "urllib3": {"level": "WARNING"},
        },
        "root": {"handlers": ["stderr"], "level": level},
    })
    configure._done = True


def get_logger(name: str = "") -> logging.Logger:
    """Return a namespaced logger: ``adriene.<name>``."""
    return logging.getLogger(f"{LOGGER_NAME}.{name}" if name else LOGGER_NAME)
