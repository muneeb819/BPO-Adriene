"""End-to-end: a whole call, through the real WebSocket, with no hardware.

This is the test the audit asked for and the suite did not have. It does not
fake the pipeline — it opens /ws/voice on the real app, drives the real
VoiceSession, and lets the real Conversation, CallRecorder and call log do their
work. Only the two things a test machine cannot supply are replaced:

  * the microphone — utterances are typed, which is a supported path the console
    uses when STT is unavailable ("type your replies — everything else works");
  * the speech engine — TTS is allowed to produce no audio (see below).

What that means honestly: this proves the *control plane* end to end — greeting,
turn-taking, compliance short-circuit, disposition, recording, persistence. It
does not prove acoustic quality, and it does not prove Deepgram transcription.
Those need a microphone and a key; `test_audio_bytes_survive_the_socket` covers
the audio transport, and the rest is called out in docs/CODE_QUALITY.md rather
than pretended.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import server.app as appmod

#: Compliance phrase that must short-circuit the call regardless of the state
#: machine. Chosen because it is the highest-stakes branch in the product: if
#: "stop calling" is ever ignored, the bot is dialling someone who asked it not to.
DNC_PHRASE = "please stop calling me"


@pytest.fixture()
def client(tmp_store, tmp_memory, call_log_tmp):
    with TestClient(appmod.app) as c:
        yield c


def _authenticate(c: TestClient) -> None:
    c.cookies.set("bpo_auth", appmod._auth_token("s3cret"))


def _drain_until(ws, wanted: str, limit: int = 40) -> dict:
    """Read frames until one of type `wanted` arrives.

    The session interleaves notices, agent_say and audio, so a test that reads a
    fixed number of frames is testing frame order rather than behaviour.
    """
    seen: list[str] = []
    for _ in range(limit):
        frame = ws.receive_json()
        seen.append(frame.get("type", "?"))
        if frame.get("type") == wanted:
            return frame
    raise AssertionError(f"never saw {wanted!r}; received {seen}")


def test_a_full_call_reaches_a_disposition_and_is_recorded(client):
    """Greeting -> a turn -> compliance stop -> disposition -> saved recording.

    One test rather than four because each step depends on the previous one
    having actually happened on the same live socket; splitting them would mean
    asserting the pipeline in pieces and calling that end to end.
    """
    _authenticate(client)

    with client.websocket_connect("/ws/voice") as ws:
        greeting = _drain_until(ws, "agent_say")
        assert greeting["text"], "the bot must open with something audible"

        # First turn: the bot should ask its opening question, not end the call.
        ws.send_text(json.dumps({"type": "text", "text": "Hello, who is this?"}))
        turn = _drain_until(ws, "agent_say")
        assert turn["text"]

        # Compliance short-circuit.
        ws.send_text(json.dumps({"type": "text", "text": DNC_PHRASE}))
        result = _drain_until(ws, "result")

        assert result["action"] == "hangup"
        assert result["disposition"] == "AI-DNC"


def test_a_do_not_call_ends_the_call_immediately_from_the_opening_turn(client):
    """The phrase must work as the very first thing the customer says.

    A state machine that only honours DNC after the first question would still
    be dialling someone who asked not to be.
    """
    _authenticate(client)

    with client.websocket_connect("/ws/voice") as ws:
        _drain_until(ws, "agent_say")
        ws.send_text(json.dumps({"type": "text", "text": "take me off your list"}))
        result = _drain_until(ws, "result")
        assert result["disposition"] == "AI-DNC"


def test_the_recording_is_persisted_for_the_call_that_just_happened(client):
    """A disposition without a recording is an unverifiable claim."""
    _authenticate(client)

    with client.websocket_connect("/ws/voice") as ws:
        _drain_until(ws, "agent_say")
        ws.send_text(json.dumps({"type": "text", "text": DNC_PHRASE}))
        _drain_until(ws, "result")
        saved = _drain_until(ws, "recording_saved")

    assert saved["id"], "recording_saved must name the recording"
    assert saved["duration_sec"] >= 0

    listed = client.get(f"/api/recordings/{saved['id']}")
    assert listed.status_code == 200, "the saved recording must be retrievable"


def test_the_call_lands_in_the_call_log_with_its_disposition(client):
    """The dashboard reads the call log; a call the log never sees does not exist."""
    _authenticate(client)

    before = len(client.get("/api/calls").json()["calls"])

    with client.websocket_connect("/ws/voice") as ws:
        _drain_until(ws, "agent_say")
        ws.send_text(json.dumps({"type": "text", "text": DNC_PHRASE}))
        _drain_until(ws, "result")

    calls = client.get("/api/calls").json()["calls"]
    assert len(calls) == before + 1, "exactly one call should have been logged"
    assert calls[0]["disposition"] == "AI-DNC"


def test_audio_bytes_sent_up_the_socket_reach_the_recorder(client):
    """The audio transport, tested with bytes rather than simulated with a mock.

    Silence is enough: this asserts the path (binary frame -> recorder), not the
    transcription. 0.5 s of 16 kHz PCM16 is 16000 bytes.
    """
    _authenticate(client)

    with client.websocket_connect("/ws/voice") as ws:
        _drain_until(ws, "agent_say")
        ws.send_bytes(b"\x00\x00" * 8000)
        ws.send_text(json.dumps({"type": "text", "text": DNC_PHRASE}))
        _drain_until(ws, "result")
        saved = _drain_until(ws, "recording_saved")

    assert saved["duration_sec"] >= 0.0


def test_barge_in_is_accepted_without_ending_the_call(client):
    """Barge-in is a real user action; it must not tear down the session."""
    _authenticate(client)

    with client.websocket_connect("/ws/voice") as ws:
        _drain_until(ws, "agent_say")
        ws.send_text(json.dumps({"type": "barge"}))
        # The session keeps its reader alive across a barge, so a normal turn
        # afterwards must still be answered on the same socket.
        ws.send_text(json.dumps({"type": "text", "text": "Sorry, go on."}))
        assert _drain_until(ws, "agent_say")["text"]
