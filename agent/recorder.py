"""Call recorder: saves a mixed audio file + transcript for every live call.

Both sides of the call are 16 kHz mono PCM16, so we mix them onto a single
wall-clock timeline (customer voice + Lina's TTS) into one WAV — exactly like a
phone-call recording — and write a companion JSON with the transcript, outcome,
and facts. Files land in data/recordings/ and are served by the dashboard.

Works even when the mic is dead: Lina's side is still captured and the customer
side is simply silent (the transcript still records typed replies).
"""
from __future__ import annotations

import json
import os
import time
import uuid
import wave
from array import array
from datetime import datetime, timezone

RATE = 16000


class CallRecorder:
    def __init__(self, out_dir: str = "data/recordings", call_id: str | None = None):
        self.call_id = call_id or (
            datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        )
        self.out_dir = out_dir
        self.start = time.monotonic()
        self.buf = array("h")        # mixed PCM16 little-endian timeline
        self.cust_pos = 0            # next write index for customer audio
        self.agent_pos = 0           # next write index for Lina's audio
        self.speaking = False
        self._saved = False

    # ---------------- mixing ----------------

    def _mix(self, offset: int, samples: array):
        if offset < 0:
            offset = 0
        n = len(samples)
        end = offset + n
        if end > len(self.buf):
            self.buf.extend([0] * (end - len(self.buf)))
        buf = self.buf
        for i in range(n):
            v = buf[offset + i] + samples[i]
            if v > 32767:
                v = 32767
            elif v < -32768:
                v = -32768
            buf[offset + i] = v

    @staticmethod
    def _to_samples(pcm: bytes) -> array:
        a = array("h")
        if len(pcm) >= 2:
            a.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
        return a

    def add_customer(self, pcm: bytes):
        """Customer mic audio (arrives contiguously in real time)."""
        samples = self._to_samples(pcm)
        if not samples:
            return
        self._mix(self.cust_pos, samples)
        self.cust_pos += len(samples)

    def speech_start(self):
        """Lina begins speaking — anchor her audio to the wall-clock timeline."""
        elapsed = int((time.monotonic() - self.start) * RATE)
        if self.agent_pos < elapsed:
            self.agent_pos = elapsed
        self.speaking = True

    def add_agent(self, pcm: bytes):
        """Lina's TTS audio (tiles contiguously from speech start, like playback)."""
        samples = self._to_samples(pcm)
        if not samples:
            return
        self._mix(self.agent_pos, samples)
        self.agent_pos += len(samples)

    def speech_end(self):
        self.speaking = False

    # ---------------- persistence ----------------

    @property
    def duration_sec(self) -> float:
        return round(len(self.buf) / RATE, 2)

    def save(self, meta: dict | None = None) -> dict | None:
        """Write recordings/{id}.wav + {id}.json. Idempotent (safe to call twice)."""
        if self._saved:
            return None
        self._saved = True
        try:
            os.makedirs(self.out_dir, exist_ok=True)
            wav_path = os.path.join(self.out_dir, f"{self.call_id}.wav")
            with wave.open(wav_path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(RATE)
                w.writeframes(self.buf.tobytes())

            payload = {
                "id": self.call_id,
                "started": datetime.fromtimestamp(
                    time.time() - (time.monotonic() - self.start), tz=timezone.utc
                ).isoformat(),
                "duration_sec": self.duration_sec,
                "had_audio": self.cust_pos > 0,
                **(meta or {}),
            }
            json_path = os.path.join(self.out_dir, f"{self.call_id}.json")
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            return payload
        except OSError:
            return None


def list_recordings(out_dir: str = "data/recordings") -> list[dict]:
    """Newest-first list of saved recordings (metadata + transcript)."""
    if not os.path.isdir(out_dir):
        return []
    out = []
    for name in os.listdir(out_dir):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(out_dir, name), encoding="utf-8") as f:
                data = json.load(f)
            data["has_wav"] = os.path.exists(
                os.path.join(out_dir, data.get("id", name[:-5]) + ".wav")
            )
            out.append(data)
        except (OSError, ValueError):
            continue
    out.sort(key=lambda d: d.get("started", ""), reverse=True)
    return out
