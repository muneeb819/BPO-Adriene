"""Runtime singletons shared by the server package.

CONFIG is created once per process and imported by every module that needs
settings. It lives here rather than in server/app.py so that route modules can
import it without importing the app (which would be a cycle, since the app
imports the routes).

Tests patch *attributes* of this object (appmod.CONFIG.tts_provider = ...),
which is why a single shared instance is the right shape: every module sees the
same patch.
"""
from __future__ import annotations

from agent.config import Config

CONFIG = Config.load()
