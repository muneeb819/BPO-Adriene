"""Map friendly voice names to ElevenLabs voice IDs.

The ElevenLabs API requires a voice_id, not a display name. These are the
built-in library voices available on the account.
"""
from __future__ import annotations

VOICE_IDS = {
    "jessica": "cgSgspJ2msm6clMCkdW9",       # Playful, Bright, Warm, Cute, young
    "sarah": "EXAVITQu4vr4xnSDxMaL",         # young female (classic)
    "bella": "EXAVITQu4vr4xnSDxMaL",         # same id as Sarah
    "charlotte": "6fZce9LFNG3iEITDfqZZ",     # young, British
    "emma": "56bWURjYFHyYyVf490Dp",          # Australian female
    "laura": "FGY2WhTYpPnrIDTdsKH5",         # young, sassy
    "rachel": "21m00Tcm4TlvDq8ikWAM",
    "elli": "MF3mGYEyCl7XYWbV9V6O",
    "josh": "TxGEqnHWrfWFTfGW9XjX",
    "river": "SAz9YHcvj6GT2YYXdXww",         # calm, neutral
}


def resolve_voice_id(name: str) -> str:
    """Return the voice_id for a friendly voice name, or the raw value if it
    is already an id format (32-char alphanumeric)."""
    key = (name or "").strip().lower()
    if key in VOICE_IDS:
        return VOICE_IDS[key]
    if len(key) == 32 and key.isalnum():
        return name
    return VOICE_IDS.get("jessica", "cgSgspJ2msm6clMCkdW9")
