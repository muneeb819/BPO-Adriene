"""The contract an engine mixin may rely on.

Each module in this package is a mixin that is combined into
:class:`agent.tts.TTS`. A mixin calls back into the facade — ``self.config`` for
settings, ``self._health`` for the failover cache — and occasionally into another
engine's helpers (Kokoro's frame reader, for instance).

Declaring that surface here does two things:

* it documents, in one place, what a new engine is allowed to assume about the
  object it is mixed into;
* it lets mypy check the mixins. Without it every ``self.config`` in a mixin is
  an ``attr-defined`` error, which is why the engine modules were invisible to
  type checking while they lived inside the (unchecked) facade.

Members inherited from a sibling mixin are declared as ``Any`` on purpose: they
are provided by a class this one does not inherit from, so pinning their
signatures here would be a second source of truth that can drift.
"""
from __future__ import annotations

from typing import Any, Dict, Tuple

from ..config import Config


class EngineMixin:
    """What every engine mixin may assume about its host (``agent.tts.TTS``)."""

    #: The live configuration (voice, model, speed, keys).
    config: Config

    #: Failover health cache, shared by all instances: {key: (timestamp, ok)}.
    #: Held on the class because promotion must survive the per-utterance TTS
    #: instances the API creates.
    _health: Dict[str, Tuple[float, bool]]

    # --- provided by sibling mixins -----------------------------------------
    # Kokoro/frame helpers, used by the shared local-HTTP transport.
    _kokoro_payload: Any
    _kokoro: Any
    _kokoro_pcm: Any
    _kokoro_stream_sync: Any
    _pcm_kokoro: Any

    # Provided by the shared local-HTTP mixin, used by engine modules.
    _local_url: Any
    _local_pcm: Any
    _local_audio: Any
    _note_engine: Any
