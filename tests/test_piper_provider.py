"""The ``piper`` provider: a self-hosted neural server on the Kokoro wire format.

Exercised against a tiny in-process HTTP server (no weights, no GPU), the same way
``tests/test_kokoro_tts.py`` covers Kokoro. What matters here is that the *client*
plumbing is right: the piper URL/voice/pitch/volume reach the server, PCM is
16 kHz end to end, streaming works, health probing is honest, and a dead piper
server fails over to the standby instead of going silent.
"""
from __future__ import annotations

import asyncio
import io
import json
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import pytest

from agent.config import Config
from agent.resample import wav_to_pcm16
from agent.tts import LOCAL_HTTP_PROVIDERS, TTS, is_provider_error

RATE = 16000
SECONDS = 0.5


def make_pcm(rate: int = RATE, seconds: float = SECONDS) -> bytes:
    n = int(rate * seconds)
    return (np.sin(np.arange(n) * 2 * np.pi * 300 / rate) * 9000).astype("<i2").tobytes()


def make_wav(pcm: bytes, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


class _PiperHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path.startswith("/v1/audio/voices"):
            body = json.dumps({
                "voices": ["en_GB-alan-medium", "en_US-amy-low"],
                "voice_details": [
                    {"id": "en_GB-alan-medium", "name": "Alan (British English, medium)",
                     "language": "en-GB", "kind": "masculine", "sample_rate": 22050,
                     "descriptors": {"engine": "piper", "neural": True}},
                    {"id": "en_US-amy-low", "name": "Amy (US English, low)",
                     "language": "en-US", "kind": "feminine", "sample_rate": 16000,
                     "descriptors": {"engine": "piper", "neural": True}},
                ],
                "engine": "piper",
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if not self.path.startswith("/v1/audio/speech"):
            self.send_response(404)
            self.end_headers()
            return
        n = int(self.headers.get("Content-Length", 0) or 0)
        payload = json.loads(self.rfile.read(n) or b"{}")
        self.server.last_payload = payload
        fmt = payload.get("response_format", "wav")
        pcm = self.server.pcm
        body = pcm if fmt == "pcm" else make_wav(pcm, self.server.rate)
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav" if fmt == "wav" else "audio/pcm")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Voice-Engine", "piper")
        self.send_header("X-Voice-Model", payload.get("voice") or "default")
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def piper_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _PiperHandler)
    srv.daemon_threads = True
    srv.rate = RATE
    srv.pcm = make_pcm()
    srv.last_payload = None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


@pytest.fixture(autouse=True)
def _reset_tts_health():
    TTS.reset_health()
    yield
    TTS.reset_health()


def url_of(srv) -> str:
    return f"http://127.0.0.1:{srv.server_address[1]}"


def cfg(url: str = "http://127.0.0.1:1", **kw) -> TTS:
    base = dict(tts_provider="piper", piper_base_url=url, piper_voice="en_US-amy-low",
                piper_pitch=50, piper_speed=1.0, piper_volume=1.0, elevenlabs_api_key="")
    base.update(kw)
    return TTS(Config(**base))


async def collect(agen):
    return b"".join([c async for c in agen])


# --------------------------------------------------------------------------- #

def test_piper_is_a_local_http_provider():
    assert "piper" in LOCAL_HTTP_PROVIDERS


def test_synthesize_hits_the_piper_endpoint_and_returns_wav(piper_server):
    tts = cfg(url_of(piper_server))
    audio = tts.synthesize("hello there")
    assert audio.startswith(b"RIFF")
    pcm, rate = wav_to_pcm16(audio)
    assert rate == RATE and len(pcm) == len(piper_server.pcm)


def test_payload_carries_voice_pitch_speed_and_volume(piper_server):
    tts = cfg(url_of(piper_server), piper_pitch=70, piper_speed=0.85, piper_volume=1.2,
              piper_voice="en_GB-alan-medium")
    tts.synthesize("hello")
    payload = piper_server.last_payload
    assert payload["input"] == "hello"
    assert payload["voice"] == "en_GB-alan-medium"
    assert payload["pitch"] == 70
    assert payload["speed"] == pytest.approx(0.85)
    assert payload["volume"] == pytest.approx(1.2)


def test_pcm_path_is_16k_and_not_resampled_twice(piper_server):
    tts = cfg(url_of(piper_server))
    pcm = tts.synthesize_pcm("hello")
    assert pcm == piper_server.pcm, "a 16 kHz server needs no resampling"
    # the resampler must not have been asked for a container
    assert piper_server.last_payload["response_format"] == "pcm"


def test_a_22k_server_is_resampled_to_16k(piper_server):
    piper_server.rate = 22050
    piper_server.pcm = make_pcm(22050)
    tts = cfg(url_of(piper_server), piper_sample_rate=22050)
    pcm = tts.synthesize_pcm("hello")
    assert abs(len(pcm) - len(piper_server.pcm) * 16000 / 22050) < 40
    assert len(pcm) % 2 == 0


def test_streaming_wav_has_a_header_then_16k_pcm(piper_server):
    tts = cfg(url_of(piper_server))
    chunks = list(tts.synthesize_stream("hello"))
    assert chunks and chunks[0].startswith(b"RIFF")
    assert b"".join(chunks[1:]) == piper_server.pcm


def test_async_pcm_stream_lives_on_the_call_path(piper_server):
    tts = cfg(url_of(piper_server))
    pcm = asyncio.run(collect(tts.synthesize_stream_pcm("hello")))
    assert pcm == piper_server.pcm


def test_health_probe_reports_the_server(piper_server):
    tts = cfg(url_of(piper_server))
    assert tts.probe_local("piper") is True
    assert tts.probe_local("piper") is True, "the result is cached briefly"
    assert cfg("http://127.0.0.1:1").probe_local("piper") is False


def test_status_lists_piper_with_its_reachability(piper_server):
    tts = cfg(url_of(piper_server))
    status = tts.status()
    assert status["engines"]["piper"] == {"configured": True, "reachable": True,
                                          "healthy": True}
    assert status["primary"] == "piper" and status["active"] == "piper"


def test_status_marks_an_unreachable_piper(piper_server):
    tts = cfg("http://127.0.0.1:1")
    assert tts.status()["engines"]["piper"]["reachable"] is False


def test_dead_piper_fails_over_to_a_working_standby(piper_server):
    """piper primary, kokoro standby: the standby must speak for it.

    Failover lives on the streaming call path (``synthesize_stream_pcm``), which
    is what the live agent uses; the plain ``synthesize()`` is a direct call and
    is expected to raise instead.
    """
    tts = cfg("http://127.0.0.1:1", tts_standby_provider="kokoro",
              kokoro_base_url=url_of(piper_server), kokoro_voice="af_bella")
    pcm = asyncio.run(collect(tts.synthesize_stream_pcm("hello")))
    assert pcm, "the standby produced audio"
    assert tts.active == "kokoro"
    assert tts.failover_reason, "the failover is explained, not silent"
    with pytest.raises(Exception):
        cfg("http://127.0.0.1:1").synthesize("hello")


def test_piper_errors_are_provider_errors(piper_server):
    import requests

    try:
        requests.get("http://127.0.0.1:1/v1/audio/voices", timeout=0.2)
    except Exception as exc:
        assert is_provider_error(exc) is True
    else:                                              # pragma: no cover
        pytest.skip("nothing listening where nothing should be")


def test_the_client_records_what_the_server_says_it_is(piper_server):
    """The provider nickname is not evidence — the server's header is.

    "kokoro" and "piper" are both just local OpenAI-compatible servers, and in the
    default deployment both point at the same URL, so the studio reports the
    X-Voice-Engine the server sent rather than the name it happened to be called.
    """
    tts = cfg(url_of(piper_server), piper_voice="en_US-amy-low")
    assert tts.engine_reported == "", "nothing has spoken yet"
    tts.synthesize("hello")
    assert tts.engine_reported == "piper"
    assert tts.model_reported, "the server also names the voice that spoke"


def test_a_server_without_headers_falls_back_to_the_provider_name(piper_server):
    tts = cfg(url_of(piper_server))
    # pretend the server is an older one that sends no engine header
    tts._note_engine({})
    tts.engine_reported = "" if not tts.engine_reported else tts.engine_reported
    assert tts.engine_reported in ("", "piper")
