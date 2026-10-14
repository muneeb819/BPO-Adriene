"""Speech-to-text providers. Console provider is for local testing without a PBX/keys."""
from __future__ import annotations

import os
from dataclasses import dataclass

from .config import Config


class STT:
    def __init__(self, config: Config):
        self.config = config
        self.provider = config.stt_provider.lower()

    def transcribe_file(self, wav_path: str) -> str:
        if self.provider == "console":
            return input("CALLER: ").strip()
        if self.provider == "deepgram":
            return self._deepgram_file(wav_path)
        return ""

    def _deepgram_file(self, wav_path: str) -> str:
        import requests

        url = f"https://api.deepgram.com/v1/listen?model={self.config.deepgram_model}&punctuate=true&utterances=true"
        with open(wav_path, "rb") as f:
            r = requests.post(
                url,
                headers={"Authorization": f"Token {self.config.deepgram_api_key}"},
                data=f,
                timeout=60,
            )
        r.raise_for_status()
        return r.json()["results"]["channels"][0]["alternatives"][0]["transcript"]
