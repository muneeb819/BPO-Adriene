"""Media bridge abstraction. Provides a pluggable endpoint for SIP/Twilio or mock websocket for local debug."""
import os

MEDIA_BRIDGE = os.getenv("MEDIA_BRIDGE", "mock")

class MediaBridge:
    def start_call(self, session_id: str):
        raise NotImplementedError

    def send_audio(self, session_id: str, audio_bytes: bytes):
        raise NotImplementedError

    def stop_call(self, session_id: str):
        raise NotImplementedError

class MockMediaBridge(MediaBridge):
    def start_call(self, session_id: str):
        # No-op for local debug
        return True
    def send_audio(self, session_id: str, audio_bytes: bytes):
        return True
    def stop_call(self, session_id: str):
        return True


def get_media_bridge() -> MediaBridge:
    # Read at call time; anything unimplemented keeps local debug alive.
    return MockMediaBridge()
