"""STT adapter interfaces and mock implementation."""
from typing import Iterable, Iterator
import os

STT_PROVIDER = os.getenv("STT_PROVIDER", "mock")

class STTAdapter:
    def transcribe_text(self, text: str) -> str:
        """Transcribe text input (for local debug)."""
        raise NotImplementedError

class MockSTT(STTAdapter):
    def transcribe_text(self, text: str) -> str:
        # In local debug the frontend sends typed user utterances; return them directly.
        return text

# Factory
def get_stt_adapter() -> STTAdapter:
    if STT_PROVIDER == "mock":
        return MockSTT()
    # Placeholder for Deepgram/Google adapters
    raise NotImplementedError(f"STT provider {STT_PROVIDER} not implemented")
