"""Voice library: named, reusable voices for the bot (library / design / remix).

The package is split by responsibility:

    library.py   the VoiceLibrary facade: profiles, active voice, lookup
    models.py    the VoiceProfile record, the engine/mode constants and the
                 pure validation helpers shared by everything else
    store.py     persistence: reading and writing library.json + references
    creation.py  making voices: upload, design, describe, remix, match, apply

Everything public is re-exported here, so `from agent.voice_library import ...`
keeps working unchanged for the API, the tests and the CLI.

Storage layout (kept out of git, like the other runtime data):

    data/voices/library.json      metadata for every profile + the active id
    data/voices/<voice-id>.wav    the mono 24 kHz clone reference (when it has one)

Everything is validated on the way in: the reference is decoded, trimmed of
silence, capped to `MAX_REFERENCE_SECONDS`, levelled and resampled, so a stored
voice is always usable by the engine.
"""
from __future__ import annotations

# AudioError is re-exported because it was importable from the module before
# the split, and callers (the API, the tests) rely on that.
from ..audio_dsp import AudioError
from .library import VoiceLibrary, get_library
from .models import (
    ENGINES,
    LIBRARY_VERSION,
    MAX_UPLOAD_BYTES,
    REMIX_MODES,
    TUNING_KEYS,
    VoiceProfile,
    clean_tags,
    clean_tuning,
    decode_base64,
    validate_engine_language,
)

__all__ = [
    "AudioError",
    "ENGINES",
    "LIBRARY_VERSION",
    "MAX_UPLOAD_BYTES",
    "REMIX_MODES",
    "TUNING_KEYS",
    "VoiceLibrary",
    "VoiceProfile",
    "clean_tags",
    "clean_tuning",
    "decode_base64",
    "get_library",
    "validate_engine_language",
]
