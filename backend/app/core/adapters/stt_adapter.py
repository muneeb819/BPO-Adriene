"""STT adapter interfaces and mock implementation.

Providers are resolved at CALL time from the runtime config (so switching
in the config panel actually takes effect). Unimplemented cloud providers
degrade to the local mock instead of crashing a live call: in this app the
real speech-to-text runs in the browser (Web Speech API dictation) and
arrives here as text; MockSTT passes it through untouched.
"""
import os
from typing import Iterable, Iterator


class STTAdapter:
    def transcribe_text(self, text: str) -> str:
        """Transcribe text input (for local debug)."""
        raise NotImplementedError

class MockSTT(STTAdapter):
    def transcribe_text(self, text: str) -> str:
        # In local debug the frontend sends typed/dictated user utterances;
        # return them directly.
        return text

# Factory
def get_stt_adapter() -> STTAdapter:
    provider = os.getenv("STT_PROVIDER", "mock").lower()
    if provider == "mock":
        return MockSTT()
    # Deepgram/Google adapters are placeholders: until a key + implementation
    # exist, keep calls alive with the local passthrough (browser dictation
    # is the real STT path in this app).
    return MockSTT()
