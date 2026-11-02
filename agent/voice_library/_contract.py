"""What a voice-library mixin may assume about its host.

The store and creation mixins are combined into :class:`VoiceLibrary` in
library.py. Each one calls attributes that the facade's ``__init__`` sets and
methods that the other mixin provides, neither of which the mixin itself
declares — so without this, mypy reports ``has no attribute`` for every one of
them and the package cannot be type checked.

Keeping the declared surface in one place also documents the seam: the facade
owns the instance state (root, index path, cached index); the mixins own the
behaviour.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


class LibraryMixin:
    """State and methods shared by every mixin in this package."""

    # --- set by VoiceLibrary.__init__ ---------------------------------------
    root: str
    index_path: str
    _data: Optional[Dict[str, Any]]

    # --- provided by StoreMixin, used by CreationMixin ----------------------
    _load: Any
    _save: Any
    _new_id: Any
    _store_reference: Any
    _add: Any
    _meta: Any
