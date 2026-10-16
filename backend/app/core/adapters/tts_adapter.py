"""TTS adapter interface and mock implementation."""
import os
TTS_PROVIDER = os.getenv("TTS_PROVIDER", "mock")

class TTSAdapter:
    def synthesize_to_bytes(self, text: str) -> bytes:
        """Synthesize text to raw audio bytes."""
        raise NotImplementedError

class MockTTS(TTSAdapter):
    def synthesize_to_bytes(self, text: str) -> bytes:
        # Local debug uses browser SpeechSynthesis; server-side mock returns empty bytes
        return b""

def get_tts_adapter() -> TTSAdapter:
    if TTS_PROVIDER == "mock":
        return MockTTS()
    raise NotImplementedError(f"TTS provider {TTS_PROVIDER} not implemented")
