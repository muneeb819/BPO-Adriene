"""ElevenLabs **Voice Design** — a text description becomes a brand-new voice.

This is the cloud half of prompt-to-voice. The local half lives in
:mod:`agent.neural_tts` (``design_from_text``), which matches a description onto
one of the resident neural voices; this module *generates* a voice that did not
exist before, which needs an ElevenLabs key.

    description ──create_previews──► 3 candidate voices (audio, base64)
                                        │
                        user picks one ▼
                              ──create──► a permanent voice_id  ──► library entry

Every call here is optional: when no key is configured, :func:`available` is
``False``, :func:`hint` explains exactly how to add one, and the endpoint returns
a friendly 400 rather than a 500. Nothing else in the studio depends on it.
"""
from __future__ import annotations

import base64
import os

#: Shown in the dashboard and in the 400 body when no key is configured.
HINT = (
    "Voice Design from a description needs an ElevenLabs key. Add "
    "ELEVENLABS_API_KEY=sk-... to .env (elevenlabs.io → your profile → API keys) "
    "and restart the server. Everything else — the local neural voices, the "
    "library, matching a recording, remixing — works without a key."
)

#: Voice Design bills per generation, so the candidates are capped.
MAX_PREVIEWS = 3
#: The longest sample we keep in memory/base64 for the browser.
MAX_PREVIEW_CHARS = 4_000_000
#: ElevenLabs caps design prompts at 600 characters.
MAX_DESCRIPTION_CHARS = 600


class DesignUnavailable(RuntimeError):
    """Raised when Voice Design is asked for without a configured key."""


class DesignError(RuntimeError):
    """The API was reachable but refused the request."""


def api_key(config=None) -> str:
    if config is not None:
        return (getattr(config, "elevenlabs_api_key", "") or "").strip()
    return (os.environ.get("ELEVENLABS_API_KEY") or "").strip()


def available(config=None) -> bool:
    """True when a key is configured (the SDK is a hard dependency of the app)."""
    return bool(api_key(config))


def hint() -> str:
    return HINT


def _client(config=None):
    key = api_key(config)
    if not key:
        raise DesignUnavailable(HINT)
    from elevenlabs.client import ElevenLabs

    return ElevenLabs(api_key=key)


def providers(config=None) -> "list[dict]":
    """What the dashboard offers in the "create a voice from a description" panel."""
    from . import neural_tts

    neural = neural_tts.available()
    return [
        {
            "id": "local",
            "name": "Local neural voices (Piper)",
            "available": neural,
            "needs_key": False,
            "kind": "describe",
            "detail": ("Pick the resident neural voice that fits your description — "
                       "free, offline, instant"
                       + ("" if neural else " — no voices installed yet, run "
                          "python scripts/fetch_neural_voices.py")),
        },
        {
            "id": "elevenlabs",
            "name": "ElevenLabs Voice Design",
            "available": available(config),
            "needs_key": True,
            "kind": "generate",
            "detail": ("Generate a brand-new voice from the description "
                       + ("(key configured)" if available(config)
                          else "— needs ELEVENLABS_API_KEY in .env")),
            "hint": HINT,
        },
    ]


def design(description: str, text: str = "", *, config=None, loudness: float = 0.5,
           guidance: float = 0.5, seed: "int | None" = None,
           enhance: bool = True) -> dict:
    """Generate candidate voices from a description. Returns the previews.

    Each preview is ``{generated_voice_id, media_type, duration_secs, language,
    audio_base64}`` — base64 so the dashboard can play candidates without storing
    anything on disk before the user picks one.
    """
    description = (description or "").strip()
    if not description:
        raise DesignError("describe the voice first — e.g. 'a warm, confident "
                          "middle-aged woman with a slight British accent'")
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise DesignError(f"keep the description under {MAX_DESCRIPTION_CHARS} characters "
                          f"(it is {len(description)})")
    client = _client(config)
    payload = {"voice_description": description}
    if text:
        payload["text"] = text
    else:
        payload["auto_generate_text"] = True
    payload["loudness"] = max(0.0, min(1.0, float(loudness)))
    payload["guidance_scale"] = max(0.0, min(1.0, float(guidance)))
    payload["should_enhance"] = bool(enhance)
    if seed is not None:
        payload["seed"] = int(seed)
    try:
        response = client.text_to_voice.create_previews(**payload)
    except Exception as exc:                      # noqa: BLE001 - surfaced as a 400/502
        raise DesignError(f"Voice Design failed: {type(exc).__name__}: {exc}") from None
    previews = []
    for item in list(getattr(response, "previews", None) or [])[:MAX_PREVIEWS]:
        audio = getattr(item, "audio_base_64", None) or ""
        previews.append({
            "generated_voice_id": getattr(item, "generated_voice_id", ""),
            "media_type": getattr(item, "media_type", "audio/mpeg"),
            "duration_secs": getattr(item, "duration_secs", None),
            "language": getattr(item, "language", None),
            "audio_base64": audio[:MAX_PREVIEW_CHARS],
        })
    if not previews:
        raise DesignError("the API returned no voice previews for that description")
    return {"description": description, "text": getattr(response, "text", "") or text,
            "previews": previews}


def save(preview_id: str, name: str, description: str, *, config=None) -> dict:
    """Turn a chosen preview into a permanent voice. Returns its details."""
    preview_id = (preview_id or "").strip()
    if not preview_id:
        raise DesignError("pick a preview to keep (generated_voice_id is required)")
    name = (name or "").strip() or "Designed voice"
    client = _client(config)
    try:
        voice = client.text_to_voice.create(
            voice_name=name[:100],
            voice_description=(description or "")[:MAX_DESCRIPTION_CHARS] or None,
            generated_voice_id=preview_id,
        )
    except Exception as exc:                      # noqa: BLE001
        raise DesignError(f"saving the voice failed: {type(exc).__name__}: {exc}") from None
    return {"voice_id": getattr(voice, "voice_id", ""),
            "name": getattr(voice, "name", name),
            "description": getattr(voice, "description", "") or description,
            "category": getattr(voice, "category", "") or ""}


def sample(voice_id: str, text: str, *, config=None, model_id: str = "") -> bytes:
    """Render a short sample of a designed voice (mp3 bytes)."""
    client = _client(config)
    voice_id = (voice_id or "").strip()
    if not voice_id:
        raise DesignError("a voice id is required to render a sample")
    try:
        audio = client.text_to_speech.convert(
            voice_id=voice_id,
            model_id=model_id or "eleven_multilingual_v2",
            text=text or "This is how the designed voice sounds.",
        )
    except Exception as exc:                      # noqa: BLE001
        raise DesignError(f"rendering a sample failed: {type(exc).__name__}: {exc}") from None
    if isinstance(audio, (bytes, bytearray)):
        return bytes(audio)
    return b"".join(bytes(chunk) for chunk in audio if isinstance(chunk, (bytes, bytearray)))


def save_locally(voice_id: str, name: str, description: str, text: str, *,
                 config=None, samples=None, audio: bytes = b"") -> "tuple[object, dict]":
    """Save a designed ElevenLabs voice into the library.

    The sample audio is stored as the profile's reference, which means the voice
    is usable in two ways at once: ``apply()`` points the live bot at the
    ElevenLabs voice itself, and the reference can be handed to the local
    ChatterBox cloner or matched onto a resident neural voice offline.
    Returns ``(profile, info)``.
    """
    from .audio_dsp import AudioError
    from .voice_library import get_library

    lib = samples if samples is not None else get_library()
    audio = audio or sample(voice_id, text, config=config)
    meta = {"engine": "elevenlabs", "voice": voice_id, "language": "multi",
            "notes": (f"ElevenLabs Voice Design ({voice_id}) — {description}".strip()
                      if description else f"ElevenLabs Voice Design ({voice_id})")}
    try:
        profile = lib.create_from_bytes(name, audio, filename=f"{voice_id}.mp3", **meta)
    except AudioError:
        # some browsers/keys return an unsupported container — keep the settings
        profile = lib.design(name, **meta)          # type: ignore[arg-type]
    return profile, {"voice_id": voice_id, "name": name, "description": description,
                     "reference": bool(profile.ref)}


def status(config=None) -> dict:
    return {"available": available(config), "hint": HINT,
            "description_limit": MAX_DESCRIPTION_CHARS, "previews_per_call": MAX_PREVIEWS}
