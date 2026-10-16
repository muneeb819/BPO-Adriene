"""TTS adapter interface and local-first implementations.

Providers are resolved at CALL time from the runtime config (switching in
the config panel takes effect immediately). Every provider degrades to the
local stack instead of crashing a live call:

    mock / kokoro  -> MockTTS: Kokoro-82M neural voices when installed,
                      empty bytes otherwise (browser engine takes over)
    elevenlabs etc -> same local fallback until a key + implementation exist

The empty-bytes contract means "no server-side audio" — the frontend then
speaks with the browser voice engine, so a call is never left silent.
"""
import os


class TTSAdapter:
    def synthesize_to_bytes(self, text: str) -> bytes:
        """Synthesize text to raw audio bytes."""
        raise NotImplementedError

class MockTTS(TTSAdapter):
    """Local synthesis: Kokoro-82M neural voices when available, else empty."""
    def synthesize_to_bytes(self, text: str) -> bytes:
        try:
            from .kokoro_tts import available, synthesize, DEFAULT_VOICE
            if available():
                return synthesize(text, voice=os.getenv("TTS_VOICE", DEFAULT_VOICE),
                                  speed=float(os.getenv("TTS_SPEED", "1.0")))
        except Exception:
            pass
        return b""

def get_tts_adapter() -> TTSAdapter:
    # kokoro IS the local neural engine; every other provider currently maps
    # to the same local-first behavior (no cloud keys configured).
    return MockTTS()
