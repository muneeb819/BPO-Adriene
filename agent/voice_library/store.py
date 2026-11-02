"""Persistence for the voice library.

Reading and writing library.json and the per-voice reference WAVs, plus the
lookups over the loaded index. Split from the facade so the file format has one
home: anything that touches the directory layout is here, and nothing else in
the package writes to disk.
"""
from __future__ import annotations

import json
import os
import secrets

from .. import audio_dsp as dsp
from ._contract import LibraryMixin
from .models import (
    LIBRARY_VERSION,
    VoiceProfile,
    _samples,
    _slug,
    clean_tags,
    clean_tuning,
)


class StoreMixin(LibraryMixin):
    """Index + reference storage, mixed into :class:`VoiceLibrary`."""

    def _load(self) -> dict:
        if self._data is not None:
            return self._data
        try:
            with open(self.index_path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            raw = {}
        except json.JSONDecodeError:
            raw = {}                     # a corrupt index must not kill the bot
        voices = {}
        for entry in raw.get("voices", []):
            try:
                profile = VoiceProfile.from_dict(entry)
            except TypeError:
                continue
            voices[profile.id] = profile
        self._data = {"version": LIBRARY_VERSION, "active_id": raw.get("active_id", ""),
                      "voices": voices}
        return self._data

    def _save(self) -> None:
        data = self._load()
        os.makedirs(self.root, exist_ok=True)
        payload = {
            "version": LIBRARY_VERSION,
            "active_id": data["active_id"],
            "voices": [v.to_dict() for v in data["voices"].values()],
        }
        tmp = self.index_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
        os.replace(tmp, self.index_path)

    def reload(self) -> None:
        self._data = None

    # --------------------------------------------------------------- reads
    def list(self) -> "list[dict]":
        data = self._load()
        out = []
        for profile in sorted(data["voices"].values(), key=lambda p: p.created, reverse=True):
            item = profile.to_dict()
            item["active"] = profile.id == data["active_id"]
            item["ref_path"] = self.ref_path(profile.id) if profile.ref else ""
            out.append(item)
        return out

    def active_id(self) -> str:
        return self._load()["active_id"]

    def find(self, vid: str) -> "VoiceProfile | None":
        return self._load()["voices"].get(vid)

    def get(self, vid: str) -> VoiceProfile:
        profile = self.find(vid)
        if profile is None:
            raise KeyError(vid)
        return profile

    def ref_path(self, vid: str) -> str:
        profile = self.find(vid)
        if profile is None or not profile.ref:
            return ""
        return os.path.join(self.root, profile.ref)

    def _new_id(self, name: str) -> str:
        data = self._load()
        for _ in range(20):
            vid = f"{_slug(name)}-{secrets.token_hex(3)}"
            if vid not in data["voices"]:
                return vid
        return f"voice-{secrets.token_hex(6)}"

    # -------------------------------------------------------------- writes
    def _store_reference(self, vid: str, pcm: bytes, rate: int) -> dict:
        """Validate, normalise and persist a clone reference; returns its info."""
        samples, info = dsp.prepare_reference(_samples(pcm), rate)
        os.makedirs(self.root, exist_ok=True)
        path = os.path.join(self.root, f"{vid}.wav")
        with open(path, "wb") as fh:
            fh.write(dsp.wav_bytes(samples, dsp.TARGET_RATE))
        return info

    def _add(self, profile: VoiceProfile) -> VoiceProfile:
        data = self._load()
        data["voices"][profile.id] = profile
        self._save()
        return profile

    def _meta(self, engine: str = "chatterbox", model: str = "turbo", language: str = "en",
              voice: str = "", tuning: dict | None = None, tags=None, notes: str = "",
              preview_text: str = "") -> dict:
        return {
            "engine": (engine or "chatterbox").lower(),
            "model": (model or "").strip(),
            "language": (language or "en").strip().lower(),
            "voice": (voice or "").strip(),
            "tuning": clean_tuning(tuning),
            "tags": clean_tags(tags),
            "notes": (notes or "").strip(),
            "preview_text": (preview_text or "").strip(),
        }

