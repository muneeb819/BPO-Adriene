"""TTS adapter interface and local-first implementations.

MockTTS (the default) synthesizes with the local Kokoro-82M model when it is
installed, and returns empty bytes otherwise (the browser voice engine takes
over client-side in that case).
"""
import os
TTS_PROVIDER = os.getenv("TTS_PROVIDER", "mock")

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
    if TTS_PROVIDER in ("mock", "kokoro"):
        return MockTTS()
    raise NotImplementedError(f"TTS provider {TTS_PROVIDER} not implemented")
