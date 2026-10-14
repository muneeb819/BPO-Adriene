"""Kokoro TTS provider + primary/standby failover.

Kokoro is self-hosted and free, so it is exercised against a tiny in-process
HTTP server that speaks its OpenAI-compatible wire format
(POST /v1/audio/speech -> raw s16le PCM @24 kHz, GET /v1/audio/voices).
No model weights, GPU or network are needed.
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import pytest

from agent.config import Config
from agent.resample import PCM16Resampler, resample_pcm16, wav_to_pcm16
from agent.tts import TTS, is_provider_error, streaming_wav_header

SECONDS = 0.5
SRC_RATE = 24000
DST_RATE = 16000
EXPECTED_BYTES = int(DST_RATE * SECONDS) * 2  # 8000 samples * 2 bytes


def make_pcm_24k() -> bytes:
    """Deterministic 24 kHz tone — exact, so chunked vs one-shot can be compared."""
    n = int(SRC_RATE * SECONDS)
    return (np.sin(np.arange(n) * 2 * np.pi * 440 / SRC_RATE) * 8000).astype("<i2").tobytes()


def make_wav(pcm: bytes, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


# ---------------------------------------------------------------- mock server

class _KokoroHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep test output clean
        pass

    def do_GET(self):
        if self.path.startswith("/v1/audio/voices"):
            body = json.dumps({"voices": ["af_bella", "af_heart"]}).encode()
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
        pcm = self.server.pcm_24k
        body = pcm if fmt == "pcm" else make_wav(pcm, SRC_RATE)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        # Emit in deliberately small chunks so the resampler's carry logic is
        # exercised rather than getting one big buffer.
        for i in range(0, len(body), self.server.chunk):
            self.wfile.write(body[i:i + self.server.chunk])
            self.wfile.flush()


@pytest.fixture()
def kokoro_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _KokoroHandler)
    srv.daemon_threads = True
    srv.pcm_24k = make_pcm_24k()
    srv.chunk = 4096
    srv.last_payload = None
    import threading

    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


@pytest.fixture(autouse=True)
def _reset_tts_health():
    TTS.reset_health()
    yield
    TTS.reset_health()


def cfg(url: str, **kw) -> TTS:
    base = dict(
        tts_provider="kokoro",
        kokoro_base_url=url,
        kokoro_voice="af_bella",
        elevenlabs_api_key="",
    )
    base.update(kw)
    return TTS(Config(**base))


def url_of(srv) -> str:
    return f"http://127.0.0.1:{srv.server_address[1]}"


async def collect(agen):
    return b"".join([c async for c in agen])


# ---------------------------------------------------------------- resampler

def test_resample_length_is_two_thirds():
    pcm = resample_pcm16(make_pcm_24k(), SRC_RATE, DST_RATE)
    assert len(pcm) == EXPECTED_BYTES
    assert len(pcm) % 2 == 0, "must never emit a half sample"


def test_resample_odd_input_is_dropped_not_fatal():
    pcm = resample_pcm16(make_pcm_24k() + b"\x01", SRC_RATE, DST_RATE)
    assert len(pcm) % 2 == 0


def test_passthrough_when_rates_match():
    raw = make_pcm_24k()
    assert resample_pcm16(raw, 16000, 16000) == raw


def test_chunked_feed_matches_one_shot():
    """Feeding awkward chunk sizes must give byte-identical output to one big feed."""
    raw = make_pcm_24k()
    for size in (1, 7, 4096, 65537):
        rs = PCM16Resampler(SRC_RATE, DST_RATE)
        acc = bytearray()
        for i in range(0, len(raw), size):
            acc += rs.feed(raw[i:i + size])
        acc += rs.flush()
        assert bytes(acc) == resample_pcm16(raw, SRC_RATE, DST_RATE), f"chunk size {size} diverged"


def test_streaming_wav_header_is_parseable():
    hdr = streaming_wav_header(DST_RATE)
    assert hdr[:4] == b"RIFF" and hdr[8:12] == b"WAVE"
    assert hdr[12:16] == b"fmt " and hdr[36:40] == b"data"
    assert len(hdr) == 44


def test_wav_to_pcm16_roundtrip():
    pcm = make_pcm_24k()
    out, rate = wav_to_pcm16(make_wav(pcm, SRC_RATE))
    assert rate == SRC_RATE
    assert out == pcm


def test_wav_to_pcm16_rejects_non_wav():
    assert wav_to_pcm16(b"not a wav at all") == (b"", 0)


def test_wav_to_pcm16_downmixes_stereo():
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(np.zeros(200, dtype="<i2").tobytes())
    pcm, rate = wav_to_pcm16(buf.getvalue())
    assert rate == 16000
    assert len(pcm) == 100 * 2, "stereo -> mono halves the sample count"


# ---------------------------------------------------------------- classification

@pytest.mark.parametrize("exc,expected", [
    (ConnectionError("refused"), True),
    (TimeoutError("slow"), True),
    (OSError("broken"), True),
    (ValueError("our bug"), False),
    (KeyError("our bug"), False),
    (AttributeError("our bug"), False),
    (RuntimeError("our bug"), False),
])
def test_is_provider_error_classifies(exc, expected):
    assert is_provider_error(exc) is expected


# ---------------------------------------------------------------- kokoro provider

def test_synthesize_returns_wav_at_24k(kokoro_server):
    tts = cfg(url_of(kokoro_server))
    audio = tts.synthesize("Hello there")
    assert audio[:4] == b"RIFF"
    pcm, rate = wav_to_pcm16(audio)
    assert rate == SRC_RATE
    assert pcm == kokoro_server.pcm_24k


def test_kokoro_request_shape(kokoro_server):
    cfg(url_of(kokoro_server)).synthesize("Hi")
    payload = kokoro_server.last_payload
    assert payload["model"] == "kokoro"
    assert payload["input"] == "Hi"
    assert payload["voice"] == "af_bella"
    assert payload["response_format"] == "wav"
    assert payload["speed"] == pytest.approx(1.0)


def test_synthesize_pcm_is_16khz(kokoro_server):
    pcm = cfg(url_of(kokoro_server)).synthesize_pcm("Hello there")
    assert len(pcm) == EXPECTED_BYTES


def test_stream_pcm_chunks_sum_to_expected_audio(kokoro_server):
    async def run():
        tts = cfg(url_of(kokoro_server))
        chunks = [c async for c in tts.synthesize_stream_pcm("Hello there")]
        return chunks, b"".join(chunks)

    chunks, total = asyncio.run(run())
    assert len(chunks) > 1, "should stream several chunks, not one blob"
    assert len(total) == EXPECTED_BYTES
    assert len(total) % 2 == 0


def test_stream_pcm_resamples_to_16k_exact_length(kokoro_server):
    async def run():
        return await collect(cfg(url_of(kokoro_server)).synthesize_stream_pcm("x"))

    assert len(asyncio.run(run())) == EXPECTED_BYTES


# ---------------------------------------------------------------- failover

def test_primary_up_never_touches_standby(kokoro_server):
    tts = cfg(url_of(kokoro_server), tts_provider="kokoro", tts_standby_provider="openai")
    assert tts.active == "kokoro"
    assert asyncio.run(collect(tts.synthesize_stream_pcm("Hello there")))
    assert tts.active == "kokoro"
    assert tts.failover_reason == ""


def test_primary_down_fails_over_to_standby(kokoro_server):
    tts = cfg(url_of(kokoro_server), tts_provider="elevenlabs",
               tts_standby_provider="kokoro")
    assert tts.primary == "elevenlabs"
    audio = asyncio.run(collect(tts.synthesize_stream_pcm("Hello there")))
    assert len(audio) == EXPECTED_BYTES, "standby must have produced the voice"
    assert tts.active == "kokoro", "standby must be promoted"
    assert "elevenlabs" in tts.failover_reason.lower()
    # promoted for later calls too
    assert cfg(url_of(kokoro_server), tts_provider="elevenlabs",
               tts_standby_provider="kokoro").active == "kokoro"


def test_standby_failure_raises(kokoro_server):
    tts = cfg(url_of(kokoro_server), tts_provider="elevenlabs",
               tts_standby_provider="openai")
    with pytest.raises(Exception):
        asyncio.run(collect(tts.synthesize_stream_pcm("Hello there")))
    assert tts.active == "elevenlabs", "nothing left to promote to"


def test_midstream_failure_does_not_restart_the_line(kokoro_server, monkeypatch):
    """Once audio has been played we must not replay the line from the top."""

    async def _dies_after_one_chunk(self, text):
        yield b"\x00\x01"
        raise ConnectionError("upstream died mid-sentence")

    monkeypatch.setattr(TTS, "_pcm_elevenlabs", _dies_after_one_chunk)
    tts = cfg(url_of(kokoro_server), tts_provider="elevenlabs",
               tts_standby_provider="kokoro")

    async def run():
        got = []
        async for chunk in tts.synthesize_stream_pcm("Hello there"):
            got.append(chunk)
        return got

    with pytest.raises(ConnectionError):
        asyncio.run(run())
    assert tts.active == "elevenlabs", "no failover once the customer heard audio"


def test_unreachable_standby_does_not_hang(kokoro_server):
    """An unreachable standby must surface fast, not stall the call."""
    tts = cfg(url_of(kokoro_server), tts_provider="elevenlabs",
               tts_standby_provider="deepgram", deepgram_api_key="")
    with pytest.raises(Exception):
        asyncio.run(collect(tts.synthesize_stream_pcm("Hello there")))


# ---------------------------------------------------------------- status / probe

def test_status_reports_both_engines(kokoro_server):
    tts = cfg(url_of(kokoro_server), tts_provider="elevenlabs",
               tts_standby_provider="kokoro")
    status = tts.status()
    assert status["primary"] == "elevenlabs"
    assert status["standby"] == "kokoro"
    assert status["engines"]["kokoro"]["reachable"] is True
    assert status["engines"]["elevenlabs"]["configured"] is False


def test_probe_kokoro_false_when_down():
    assert cfg("http://127.0.0.1:1").probe_kokoro(timeout=0.5) is False


def test_reset_health_clears_promotion():
    tts = cfg("http://127.0.0.1:1", tts_provider="elevenlabs", tts_standby_provider="kokoro")
    TTS._mark("elevenlabs", False)
    tts._promote("kokoro")
    assert tts.active == "kokoro"
    TTS.reset_health()
    assert tts.active == "elevenlabs"
