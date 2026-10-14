"""Streaming PCM16 resampling.

The voice pipeline runs on raw signed 16-bit little-endian mono audio at 16 kHz
(that is what Deepgram expects). Local Kokoro synthesises at 24 kHz, so its
output must be resampled before it reaches the browser or the recording.

`PCM16Resampler` is stateful: feed() it any number of arbitrarily-sized byte
chunks and it emits correctly resampled audio while carrying the fractional
read position and any half-sample across chunk boundaries. That is what lets us
resample a *streaming* response instead of buffering the whole utterance.
"""
from __future__ import annotations

import numpy as np

# The pipeline's canonical format (must match DEEPGRAM_SAMPLE_RATE).
TARGET_RATE = 16000


class PCM16Resampler:
    """Linear-interpolation resampler for s16le mono PCM."""

    def __init__(self, src_rate: int, dst_rate: int = TARGET_RATE):
        src_rate, dst_rate = int(src_rate), int(dst_rate)
        if src_rate <= 0 or dst_rate <= 0:
            raise ValueError(f"invalid sample rate {src_rate}->{dst_rate}")
        self.src_rate = src_rate
        self.dst_rate = dst_rate
        self.step = src_rate / dst_rate      # input samples consumed per output sample
        self.pos = 0.0                       # fractional read offset into the buffer
        self.carry = b""                     # unconsumed input bytes
        self._passthrough = src_rate == dst_rate

    def feed(self, data: bytes) -> bytes:
        """Consume raw s16le bytes, return resampled s16le bytes."""
        if self._passthrough:
            return data or b""
        buf = self.carry + (data or b"")
        usable = len(buf) - (len(buf) % 2)
        if usable == 0:
            self.carry = buf                 # a single stray byte; wait for its pair
            return b""
        odd = buf[usable:]                   # trailing odd byte, joined next call
        samples = np.frombuffer(buf[:usable], dtype="<i2")
        n = samples.shape[0]
        out: list[float] = []

        if n >= 2:
            x = samples.astype(np.float64)
            while self.pos <= n - 2:
                i = int(self.pos)
                frac = self.pos - i
                out.append(x[i] * (1.0 - frac) + x[i + 1] * frac)
                self.pos += self.step
            keep = int(self.pos)             # resume from here next time
            self.pos -= keep
            tail = samples[keep:].tobytes()
        else:
            keep = 0
            tail = samples.tobytes()

        self.carry = tail + odd
        if not out:
            return b""
        return np.clip(np.asarray(out), -32768, 32767).astype("<i2").tobytes()

    def flush(self) -> bytes:
        """Emit the final held sample and reset (call once at end of stream)."""
        if self._passthrough:
            return b""
        pcm, rate = _split_s16le(self.carry)
        if pcm.size == 0:
            self._reset()
            return b""
        i = min(int(self.pos), pcm.size - 1)
        out = np.asarray([pcm[i]], dtype="<i2").tobytes()
        self._reset()
        return out

    def _reset(self) -> None:
        self.pos = 0.0
        self.carry = b""


def _split_s16le(data: bytes):
    usable = len(data) - (len(data) % 2)
    return np.frombuffer(data[:usable], dtype="<i2"), usable // 2


def resample_pcm16(data: bytes, src_rate: int, dst_rate: int = TARGET_RATE) -> bytes:
    """One-shot resample of a complete s16le buffer."""
    r = PCM16Resampler(src_rate, dst_rate)
    out = r.feed(data) + r.flush()
    if len(out) % 2:                          # never emit a half sample
        out = out[:-1]
    return out


def wav_to_pcm16(data: bytes) -> tuple[bytes, int]:
    """Strip a RIFF/WAVE header, returning (raw_s16le_samples, sample_rate).

    Several TTS providers only offer containerised output; the pipeline needs
    bare PCM, so the header has to come off before anything else happens.
    Returns (b"", 0) if the buffer is not a recognisable WAV.
    """
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return b"", 0
    pos, rate, bits, channels = 12, 0, 16, 1
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        size = int.from_bytes(data[pos + 4:pos + 8], "little")
        body = data[pos + 8:pos + 8 + size]
        if cid == b"fmt " and len(body) >= 8:
            channels = int.from_bytes(body[2:4], "little") or 1
            rate = int.from_bytes(body[4:8], "little")
            bits = int.from_bytes(body[14:16], "little") or 16
        elif cid == b"data":
            if bits != 16:
                return b"", 0
            pcm = body[:size]
            if channels > 1:                  # downmix to mono (pipeline is mono)
                arr = np.frombuffer(pcm[:len(pcm) - (len(pcm) % (2 * channels))],
                                    dtype="<i2").reshape(-1, channels)
                pcm = arr.mean(axis=1).astype("<i2").tobytes()
            return pcm, rate
        pos += 8 + size + (size % 2)
    return b"", 0
