"""Voice library: named, reusable voices for the bot (library/design/remix).

One "voice" is a profile — engine + model + language + optional clone reference
+ tuning — that can be previewed, applied to the live bot, renamed, blended with
another voice or deleted, all from the dashboard.

Storage layout (kept out of git, like the other runtime data):

    data/voices/library.json      metadata for every profile + the active id
    data/voices/<voice-id>.wav    the mono 24 kHz clone reference (when it has one)

Everything is validated on the way in: the reference is decoded, trimmed of
silence, capped to `MAX_REFERENCE_SECONDS`, levelled and resampled, so a stored
voice is always usable by the engine.
"""
from __future__ import annotations



import os

from .creation import CreationMixin
from .store import StoreMixin

class VoiceLibrary(CreationMixin, StoreMixin):
    """File-backed store of voice profiles."""

    def __init__(self, root: str = "data/voices"):
        self.root = root
        self.index_path = os.path.join(root, "library.json")
        self._data: dict | None = None

    # ------------------------------------------------------------------ io
def get_library(root: str | None = None) -> VoiceLibrary:
    """The process-wide library (data/voices unless configured elsewhere)."""
    global _LIBRARY
    if root is None:
        try:
            from ..config import Config

            root = Config.load().voice_library_dir
        except Exception:
            root = "data/voices"
    if _LIBRARY is None or root != _LIBRARY.root:
        _LIBRARY = VoiceLibrary(root)
    return _LIBRARY


_LIBRARY: "VoiceLibrary | None" = None
