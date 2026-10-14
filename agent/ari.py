"""Asterisk REST Interface (ARI) client: answer channels, play TTS, record speech,
and redirect qualified leads to a closer queue / extension.

Media model: turn-based. The bot plays a generated WAV, then records the caller
until they press '#' (DTMF) or a max duration elapses, then transcribes the WAV.
For low-latency full-duplex audio, swap this for an ARI externalMedia/RTP bridge.
"""
from __future__ import annotations

import os
import time
from datetime import datetime

import requests


class ARI:
    def __init__(self, config):
        self.config = config
        self.base = config.ari_url.rstrip("/") + "/ari"
        self.auth = (config.ari_username, config.ari_password)

    def _url(self, path: str) -> str:
        return f"{self.base}/{path.lstrip('/')}"

    def request(self, method: str, path: str, **kwargs):
        kwargs.setdefault("auth", self.auth)
        r = requests.request(method, self._url(path), timeout=30, **kwargs)
        r.raise_for_status()
        if r.content and r.headers.get("Content-Type", "").startswith("application/json"):
            return r.json()
        return None

    def answer(self, channel_id: str) -> None:
        self.request("POST", f"channels/{channel_id}/answer")

    def bridge_create(self, name: str = "fronter") -> str:
        data = self.request("POST", "bridges", json={"type": "mixing", "name": name})
        return data["id"]

    def bridge_add(self, bridge_id: str, channel_id: str) -> None:
        self.request("POST", f"bridges/{bridge_id}/addChannel", json={"channel": channel_id})

    def play(self, channel_id: str, media_uri: str) -> None:
        playback = self.request(
            "POST", f"channels/{channel_id}/play", json={"media": media_uri}
        )
        self._wait_playback(playback["id"])

    def _wait_playback(self, playback_id: str, timeout: int = 30) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data = self.request("GET", f"playbacks/{playback_id}")
                if data is None or data.get("state") in ("done", "canceled"):
                    return
            except Exception:
                return
            time.sleep(0.3)

    def record(self, channel_id: str, name: str, max_seconds: int = 30) -> None:
        self.request(
            "POST",
            f"channels/{channel_id}/record",
            json={
                "name": name,
                "format": "wav",
                "maxDurationSeconds": max_seconds,
                "terminateOn": "any",
                "beep": False,
            },
        )

    def stop_recording(self, name: str) -> None:
        try:
            self.request("DELETE", f"recordings/{name}")
        except Exception:
            pass

    def _recording_path(self, name: str) -> str:
        return os.path.join(self.config.ari_recordings_dir, f"{name}.wav")

    def wait_for_recording(self, name: str, timeout: int = 40) -> str | None:
        path = self._recording_path(name)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.exists(path) and os.path.getsize(path) > 44:
                self.stop_recording(name)
                return path
            time.sleep(0.5)
        self.stop_recording(name)
        return path if os.path.exists(path) else None

    def redirect(self, channel_id: str, context: str, extension: str) -> None:
        self.request(
            "POST",
            f"channels/{channel_id}/redirect",
            json={"endpoint": f"Local/{extension}@{context}"},
        )

    def hangup(self, channel_id: str) -> None:
        try:
            self.request("DELETE", f"channels/{channel_id}")
        except Exception:
            pass

    def transfer_to_closer(self, channel_id: str) -> None:
        ext = self.config.closer_extension or self.config.closer_queue
        self.redirect(channel_id, self.config.ari_outbound_context, ext)

    def run_forever(self, app: str, on_stasis_start) -> None:
        import websocket

        base = self.config.ari_url.rstrip("/")
        # websocket-client needs a ws(s):// URL, not http(s)://
        if base.startswith("https://"):
            base = "wss://" + base[len("https://"):]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://"):]
        ws_url = f"{base}/ari/events?api_key={self.config.ari_username}:{self.config.ari_password}&app={app}"
        ws = websocket.create_connection(ws_url, timeout=10)
        while True:
            msg = ws.recv()
            import json

            event = json.loads(msg)
            if event.get("type") == "StasisStart":
                channel = event["channel"]
                args = event.get("args", [])
                try:
                    on_stasis_start(channel["id"], dict(args), channel.get("variables", {}))
                except Exception as e:
                    print(f"[ARI] handler error: {e}")
                    self.hangup(channel["id"])
