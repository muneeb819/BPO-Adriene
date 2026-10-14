"""Voice Calibration Studio — generate real ElevenLabs v3 audio for candidate
voices so you can compare pitch, tone, accent, and pacing before choosing.
"""
from __future__ import annotations

import os
import sys

from elevenlabs.client import ElevenLabs
from elevenlabs import save

API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")

# Candidates that best match "sweet, soft, loving, caring young woman"
CANDIDATES = {
    "jessica": "cgSgspJ2msm6clMCkdW9",     # Playful, Bright, Warm, Cute, young
    "sarah": "EXAVITQu4vr4xnSDxMaL",       # young female
    "charlotte": "6fZce9LFNG3iEITDfqZZ",   # young, cute, British
    "emma": "56bWURjYFHyYyVf490Dp",        # Australian female
    "laura": "FGY2WhTYpPnrIDTdsKH5",       # young, sassy (energetic)
    "matilda": "XrExE9yKIg1WjnnlVkGX",     # upbeat professional
}

SCRIPT = (
    "Hello, this is Lina calling from Senior Resource Center. How are you doing today? "
    "I understand. We offer fixed-income coverage for final expenses. How young are you? "
    "That's great, you're eligible for a lower monthly premium. "
    "One moment, let me connect you with a licensed agent who can assist you further."
)

OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "voice_preview")


def main():
    if not API_KEY:
        print("ELEVENLABS_API_KEY not set")
        sys.exit(1)
    os.makedirs(OUT_DIR, exist_ok=True)
    client = ElevenLabs(api_key=API_KEY)
    names = sys.argv[1:] or list(CANDIDATES.keys())
    for name in names:
        vid = CANDIDATES.get(name)
        if not vid:
            print(f"unknown voice: {name}")
            continue
        print(f"[{name}] generating ...")
        audio = client.text_to_speech.convert(
            voice_id=vid,
            text=SCRIPT,
            model_id="eleven_v3",
            voice_settings={
                "stability": 0.5,
                "similarity_boost": 0.9,
                "style": 0.4,
                "use_speaker_boost": True,
            },
            output_format="mp3_44100_128",
        )
        if hasattr(audio, "__iter__") and not isinstance(audio, (bytes, bytearray)):
            audio = b"".join(bytes(a) for a in audio)
        path = os.path.join(OUT_DIR, f"{name}.mp3")
        with open(path, "wb") as f:
            f.write(audio)
        print(f"  saved {path} ({len(audio)} bytes)")


if __name__ == "__main__":
    main()
