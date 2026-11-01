"""Persistence for the call log.

The log itself lives in memory (``server.app.CALL_LOG``) because routes slice it
and tests replace it wholesale; this module owns only the disk I/O — load, and a
crash-safe atomic save — so neither concern has to know about the other.

Kept as plain functions over a path and a list rather than a class holding its
own state: the server app is the single owner of the in-memory list, and a
second copy of it here would silently drift.
"""
from __future__ import annotations

import json
import os
from typing import List

MAX_ENTRIES = 5000      # keep the newest N on disk


def load(path: str) -> List[dict]:
    """Read the persisted log. Missing or corrupt file -> empty list."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def save(entries: List[dict], path: str, max_entries: int = MAX_ENTRIES) -> bool:
    """Atomically persist the newest ``max_entries``. Returns False on OSError.

    Write-then-rename so a crash mid-write cannot truncate the existing log.
    """
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(entries[-max_entries:], f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        return True
    except OSError:
        return False
