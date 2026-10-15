"""Voice library: named, reusable voices for the bot (library/design/remix).

One "voice" is a profile — engine + model + language + optional clone reference
+ tuning — that can be previewed, applied to the live bot, renamed, blended with
another voice or deleted, all from the dashboard.

Storage layout (kept out of git, like the other runtime data):

    data/voices/library.json      metadata for every profile + the active id
    data/voices/<voice-id>.wav    the mono 24 kHz clone reference (when it has one)

Everything is validated on the way in: the reference is decoded, trimmed of
silence, capped to `MAX_REFERENCE_SECONDS`, levelled and resampled, so a stored
voice is always usable by the engine.
"""
from __future__ import annotations

import base64
import binascii
import copy
import json
import os
import re
import secrets
import time
from dataclasses import dataclass, field, replace

from . import audio_dsp as dsp
from .audio_dsp import AudioError

LIBRARY_VERSION = 1

#: engines a profile can target; "kokoro" covers the OpenAI-compatible local
#: server (Kokoro, or the bundled neural Piper voices).
#: Engines a profile can target. "kokoro" and "piper" are both served by a
#: self-hosted OpenAI-compatible HTTP server; piper is the neural ONNX one that
#: needs no torch and no cloud key (see agent/neural_tts.py).
#: "elevenlabs" profiles point at a voice id on ElevenLabs — that is where a
#: Voice Design result lives, and the reference sample we download with it makes
#: the voice usable by the local cloner/matcher too.
ENGINES = ("chatterbox", "kokoro", "piper", "elevenlabs")
REMIX_MODES = ("blend", "stitch")
TUNING_KEYS = ("exaggeration", "cfg_weight", "temperature", "speed", "pitch",
               "volume")
MAX_UPLOAD_BYTES = 25 * 1024 * 1024


def validate_engine_language(engine: str, model: str, language: str) -> None:
    """Mirror the /api/tts/switch-voice rules so a saved voice cannot be invalid."""
    if (engine or "").lower() != "chatterbox" or not language or language == "en":
        return
    from .tts import CHATTERBOX_LANGUAGES, CHATTERBOX_MODELS

    if model != "multilingual":
        raise AudioError(
            f"the '{model or 'turbo'}' ChatterBox model is English-only — use "
            f"'multilingual' to speak {language}")
    if language not in CHATTERBOX_LANGUAGES:
        raise AudioError(f"unknown language '{language}' — use one of: "
                         f"{', '.join(CHATTERBOX_LANGUAGES)}")


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return slug[:40] or "voice"


def _default_piper_voice() -> str:
    """A middle-register resident voice, or the first one installed."""
    try:
        from . import neural_tts

        models = neural_tts.describe_models()
        if not models:
            return ""
        return models[len(models) // 2]["id"] if len(models) > 1 else models[0]["id"]
    except Exception:
        return ""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


@dataclass
class VoiceProfile:
    """A saved voice. `ref` is relative to the library directory."""

    id: str
    name: str
    engine: str = "chatterbox"
    model: str = "turbo"
    language: str = "en"
    voice: str = ""                      # engine voice id (kokoro/piper backends)
    ref: str = ""                        # clone reference file name, "" = built-in
    tuning: dict = field(default_factory=dict)
    tags: list = field(default_factory=list)
    notes: str = ""
    source: str = "designed"             # uploaded | recorded | imported | designed | remixed | captured
    created: str = ""
    last_used: str = ""
    duration_sec: float = 0.0
    sample_rate: int = 0
    preview_text: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "engine": self.engine,
            "model": self.model, "language": self.language, "voice": self.voice,
            "ref": self.ref, "has_reference": bool(self.ref),
            "tuning": dict(self.tuning), "tags": list(self.tags),
            "notes": self.notes, "source": self.source, "created": self.created,
            "last_used": self.last_used, "duration_sec": self.duration_sec,
            "sample_rate": self.sample_rate, "preview_text": self.preview_text,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "VoiceProfile":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


class VoiceLibrary:
    """File-backed store of voice profiles."""

    def __init__(self, root: str = "data/voices"):
        self.root = root
        self.index_path = os.path.join(root, "library.json")
        self._data: dict | None = None

    # ------------------------------------------------------------------ io
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

    def create_from_bytes(self, name: str, data: bytes, filename: str = "",
                          **meta) -> VoiceProfile:
        """Create a voice from an uploaded audio file (wav/flac/ogg/mp3/aiff)."""
        if len(data) > MAX_UPLOAD_BYTES:
            raise AudioError(
                f"that file is {len(data) / 1048576:.1f} MB — the limit is "
                f"{MAX_UPLOAD_BYTES // 1048576} MB (send 10-15 s of speech)")
        samples, rate = dsp.load_audio(data)
        return self._create_from_samples(name, samples, rate,
                                         source="uploaded", filename=filename, **meta)

    def create_from_pcm(self, name: str, pcm: bytes, sample_rate: int, **meta) -> VoiceProfile:
        """Create a voice from raw browser-recorded microphone PCM (mono s16le)."""
        if len(pcm) > MAX_UPLOAD_BYTES:
            raise AudioError("that recording is too long — keep it under 30 seconds")
        samples, info = dsp.prepare_reference(_samples(pcm), int(sample_rate or 0) or 16000)
        return self._create_from_samples(name, samples, dsp.TARGET_RATE,
                                         source="recorded", known_info=info, **meta)

    def create_from_path(self, name: str, path: str, **meta) -> VoiceProfile:
        """Create a voice from a file that already lives on the server."""
        from .tts import TTS

        problem = TTS.validate_voice_reference(path)
        if problem:
            raise AudioError(problem)
        samples, rate = dsp.load_audio(path)
        return self._create_from_samples(name, samples, rate,
                                         source="imported",
                                         filename=os.path.basename(path), **meta)

    def _create_from_samples(self, name: str, samples, rate: int, *, source: str,
                             filename: str = "", known_info: dict | None = None,
                             **meta) -> VoiceProfile:
        name = (name or "").strip()
        if not name:
            raise AudioError("give the voice a name first")
        vid = self._new_id(name)
        if known_info:
            info = known_info
        else:
            samples, info = dsp.prepare_reference(samples, rate)
        # write the normalised reference under the new id
        os.makedirs(self.root, exist_ok=True)
        with open(os.path.join(self.root, f"{vid}.wav"), "wb") as fh:
            fh.write(dsp.wav_bytes(samples, dsp.TARGET_RATE))
        fields = self._meta(**meta)
        profile = VoiceProfile(
            id=vid, name=name, ref=f"{vid}.wav", source=source, created=_now(),
            duration_sec=info["duration_sec"], sample_rate=info["sample_rate"],
            **fields)
        if filename and not profile.notes:
            profile.notes = f"from {filename}"
        return self._add(profile)

    def design(self, name: str, **meta) -> VoiceProfile:
        """Create a tuned voice without a reference (engine-level design)."""
        name = (name or "").strip()
        if not name:
            raise AudioError("give the voice a name first")
        fields = self._meta(**meta)
        if fields["engine"] not in ENGINES:
            raise AudioError(f"unknown engine '{fields['engine']}' — use one of: {', '.join(ENGINES)}")
        if fields["engine"] == "kokoro" and not fields["voice"]:
            fields["voice"] = "af_bella"      # a sane default for the local server
        if fields["engine"] == "piper" and not fields["voice"]:
            fields["voice"] = _default_piper_voice()
        if fields["engine"] == "elevenlabs" and not fields["voice"]:
            raise AudioError(
                "an ElevenLabs voice needs a voice id — create one with "
                "POST /api/voices/design/elevenlabs, or set ELEVENLABS_VOICE "
                "and save the current voice")
        validate_engine_language(fields["engine"], fields["model"], fields["language"])
        profile = VoiceProfile(id=self._new_id(name), name=name, source="designed",
                               created=_now(), **fields)
        return self._add(profile)

    def describe(self, name: str, description: str, **meta):
        """Create a voice **from a text description** on the local neural engine.

        "warm older British man, speaks slowly" becomes a concrete resident voice
        plus pitch/speed/volume. Returns ``(profile, plan)`` where the plan carries
        the measurements and the reasoning, so the studio can show its work.

        Explicit tuning passed in ``meta`` still wins, so the sliders can refine
        whatever the words asked for.
        """
        from . import neural_tts

        description = (description or "").strip()
        if not description:
            raise AudioError("describe the voice you want, e.g. "
                             "'warm older British man, speaks slowly'")
        tuning = dict(meta.pop("tuning", None) or {})
        plan = neural_tts.design_from_text(
            description,
            language=meta.get("language"),
            pitch_level=tuning.pop("pitch", None),
            speed=tuning.pop("speed", None),
        )
        meta.setdefault("engine", "piper")
        meta.setdefault("voice", plan["model"])
        merged = {"pitch": plan["pitch_level"], "speed": plan["speed"],
                  "volume": plan["volume"]}
        merged.update(tuning)
        meta["tuning"] = merged
        meta.setdefault("language", (meta.get("language")
                                     or plan.get("model", "").split("-")[0].lower()))
        meta.setdefault("notes", f"designed from: {description}")
        profile = self.design(name, **meta)
        return profile, plan

    def _apply_match(self, profile: VoiceProfile, language: "str | None" = None) -> dict:
        """Point a profile at the local neural voice closest to its reference."""
        from . import neural_tts

        ref = self.ref_path(profile.id)
        if not profile.ref or not os.path.isfile(ref):
            raise AudioError(f"'{profile.name}' has no reference audio to match")
        samples, rate = dsp.load_audio(ref)
        match = neural_tts.match_reference(samples, rate, language=language)
        profile.engine = "piper"
        profile.voice = match["model"]
        profile.tuning = clean_tuning({
            "pitch": match["pitch_level"], "speed": match["speed"],
            "volume": match["volume"]})
        if not profile.language:
            profile.language = match["model"].split("-")[0].lower()
        note = ("matched to " + match["model"] +
                f" (pitch {match['pitch_level']}/99, speed {match['speed']:.2f}x, "
                f"confidence {match['confidence']:.2f})")
        if note not in (profile.notes or ""):
            profile.notes = f"{profile.notes} — {note}" if profile.notes else note
        self._save()
        return match

    def create_matched(self, name: str, *, data: bytes = b"", path: str = "",
                       pcm: bytes = b"", sample_rate: int = 16000, **meta):
        """Create a voice from a recording **and render it on a neural voice**.

        The recording is stored as the profile's reference (so the true cloning
        engine still has it) and then measured: the closest resident Piper voice
        is chosen and its pitch is nudged to the recording's register. This is
        *voice matching* — the timbre is the resident neural voice, not an
        embedding of the speaker — which is exactly what makes a clone, or a
        remix, judgeable on a machine without the ChatterBox weights.
        Returns ``(profile, match)``.
        """
        sources = [bool(data), bool(path), bool(pcm)]
        if sum(sources) != 1:
            raise AudioError("provide exactly one of data, path or pcm")
        meta.setdefault("engine", "piper")
        if path:
            profile = self.create_from_path(name, path, **meta)
        elif data:
            profile = self.create_from_bytes(name, data, **meta)
        else:
            profile = self.create_from_pcm(name, pcm, sample_rate, **meta)
        match = self._apply_match(profile, language=meta.get("language"))
        return profile, match

    def remix(self, name: str, sources: "list[dict]", mode: str = "blend",
              pitch_semitones: float = 0.0, match: bool = False,
              **meta) -> VoiceProfile:
        """Create a new voice by blending/stitching existing voices' references.

        With ``match=True`` the blended reference is immediately measured and
        rendered on the closest resident neural voice, so the remix can be heard
        as neural speech instead of as a statistical average of two recordings.
        """
        name = (name or "").strip()
        if not name:
            raise AudioError("give the new voice a name first")
        mode = (mode or "blend").lower()
        if mode not in REMIX_MODES:
            raise AudioError(f"unknown remix mode '{mode}' — use one of: {', '.join(REMIX_MODES)}")
        layers, weights, used = [], [], []
        for item in sources or []:
            vid = str(item.get("id", ""))
            weight = float(item.get("weight", 1.0) or 1.0)
            profile = self.find(vid)
            if profile is None:
                raise AudioError(f"unknown voice id '{vid}'")
            if not profile.ref:
                continue                      # designed voices have no audio to mix
            path = self.ref_path(vid)
            if not os.path.isfile(path):
                raise AudioError(f"the reference file for '{profile.name}' is missing")
            samples, rate = dsp.load_audio(path)
            layers.append(samples if rate == dsp.TARGET_RATE
                          else dsp.resample(samples, rate, dsp.TARGET_RATE))
            weights.append(weight)
            used.append(profile.name)
        if len(layers) < 1:
            raise AudioError("pick at least one voice that has a reference to remix")
        if mode == "blend":
            mixed = dsp.blend(list(zip(layers, weights)))
        else:
            mixed = dsp.stitch(layers, dsp.TARGET_RATE)
        mixed = dsp.pitch_shift(mixed, dsp.TARGET_RATE, float(pitch_semitones or 0.0))
        mixed = dsp.normalize(mixed)
        info = dsp.describe(mixed, dsp.TARGET_RATE)

        vid = self._new_id(name)
        os.makedirs(self.root, exist_ok=True)
        with open(os.path.join(self.root, f"{vid}.wav"), "wb") as fh:
            fh.write(dsp.wav_bytes(mixed, dsp.TARGET_RATE))
        fields = self._meta(**meta)
        # A remix inherits the first source's engine unless told otherwise.
        first = self.find(str((sources or [{}])[0].get("id", "")))
        if first is not None and not meta.get("engine"):
            fields["engine"], fields["model"] = first.engine, first.model
            fields["language"] = first.language
            # a remix of local-server voices needs a concrete voice id and the
            # tuning it was made with, otherwise it renders with the defaults
            if not fields["voice"]:
                fields["voice"] = first.voice
            if not fields["tuning"]:
                fields["tuning"] = dict(first.tuning)
        profile = VoiceProfile(
            id=vid, name=name, ref=f"{vid}.wav", source="remixed", created=_now(),
            duration_sec=info["duration_sec"], sample_rate=info["sample_rate"],
            notes=fields.pop("notes", "") or (
                f"{mode} of " + " + ".join(used)
                + (f", pitch {pitch_semitones:+.1f}" if pitch_semitones else "")),
            **fields)
        profile = self._add(profile)
        if match:
            # measure the blend and render it on the nearest neural voice, so the
            # remix can actually be listened to
            self._apply_match(profile, language=profile.language)
        return profile

    def save_from_current(self, name: str, config, **meta) -> VoiceProfile:
        """Snapshot the voice the bot is using right now as a library entry."""
        engine = (config.tts_provider or "").strip().lower()
        # Record the engine we are saving *from* before any branch runs, otherwise
        # design() falls back to its chatterbox default and the snapshot points at
        # the wrong engine.
        meta.setdefault("engine", engine)
        if engine == "elevenlabs":
            voice_id = (getattr(config, "elevenlabs_voice", "") or "").strip()
            if not voice_id:
                raise AudioError(
                    "no ElevenLabs voice id is configured — set ELEVENLABS_VOICE in "
                    ".env, or design one with POST /api/voices/design/elevenlabs")
            meta.setdefault("voice", voice_id)
            return self.design(name, **meta)
        if engine not in ENGINES:
            raise AudioError(f"the active engine '{engine}' has no savable voice settings")
        if engine == "chatterbox":
            meta.setdefault("model", config.chatterbox_model)
            meta.setdefault("language", config.chatterbox_language)
            meta.setdefault("tuning", {
                "exaggeration": config.chatterbox_exaggeration,
                "cfg_weight": config.chatterbox_cfg_weight,
                "temperature": config.chatterbox_temperature,
            })
            ref = (config.chatterbox_voice_path or "").strip()
            if ref and os.path.isfile(ref):
                samples, rate = dsp.load_audio(ref)
                return self._create_from_samples(name, samples, rate, source="captured", **meta)
            profile = self.design(name, **meta)
            profile.notes = profile.notes or "built-in ChatterBox voice (no reference)"
            self._save()
            return profile
        if engine == "piper":
            meta.setdefault("voice", getattr(config, "piper_voice", "")
                            or _default_piper_voice())
            meta.setdefault("tuning", {
                "speed": getattr(config, "piper_speed", 1.0),
                "pitch": getattr(config, "piper_pitch", 50),
                "volume": getattr(config, "piper_volume", 1.0)})
            return self.design(name, **meta)
        meta.setdefault("voice", config.kokoro_voice)
        meta.setdefault("tuning", {"speed": config.kokoro_speed,
                                   "pitch": getattr(config, "kokoro_pitch", 0)})
        return self.design(name, **meta)

    def update(self, vid: str, **fields) -> VoiceProfile:
        profile = self.get(vid)
        for key in ("name", "notes", "model", "language", "voice", "engine",
                    "preview_text", "duration_sec"):
            if key in fields and fields[key] is not None:
                value = fields[key]
                if isinstance(value, str):
                    value = value.strip()
                    if key == "language":
                        value = value.lower()
                if key == "name" and not value:
                    raise AudioError("the voice name cannot be empty")
                setattr(profile, key, value)
        if "tuning" in fields and fields["tuning"] is not None:
            merged = dict(profile.tuning)
            merged.update(clean_tuning(fields["tuning"]))
            profile.tuning = merged
        if "tags" in fields and fields["tags"] is not None:
            profile.tags = clean_tags(fields["tags"])
        if profile.engine not in ENGINES:
            raise AudioError(f"unknown engine '{profile.engine}'")
        validate_engine_language(profile.engine, profile.model, profile.language)
        self._save()
        return profile

    def delete(self, vid: str) -> bool:
        data = self._load()
        profile = data["voices"].pop(vid, None)
        if profile is None:
            return False
        if profile.ref:
            path = os.path.join(self.root, profile.ref)
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
        if data["active_id"] == vid:
            data["active_id"] = ""
        self._save()
        return True

    # ------------------------------------------------------------- actions
    def apply(self, vid: str, config, *, mark_active: bool = True) -> dict:
        """Point the running bot at this voice.

        Only the fields that engine actually uses are written, so switching a
        ChatterBox voice never silently rewrites the Kokoro settings and vice
        versa.
        """
        profile = self.get(vid)
        applied: dict = {"engine": profile.engine, "voice_id": profile.id,
                         "name": profile.name}
        # Keep the two-engine safety net. Switching to the engine that is
        # currently the standby would otherwise leave the bot with no failover at
        # all (the previous primary is promoted into the standby slot instead).
        current_primary = (getattr(config, "tts_provider", "") or "").strip().lower()
        current_standby = (getattr(config, "tts_standby_provider", "") or "").strip().lower()
        if profile.engine == current_standby and current_primary not in ("", profile.engine):
            config.tts_standby_provider = current_primary
            applied["standby"] = current_primary
        if profile.engine == "chatterbox":
            config.tts_provider = "chatterbox"
            if profile.model:
                from .tts import CHATTERBOX_MODELS

                if profile.model not in CHATTERBOX_MODELS:
                    raise AudioError(
                        f"unknown ChatterBox model '{profile.model}' — use one of: "
                        f"{', '.join(CHATTERBOX_MODELS)}")
                config.chatterbox_model = profile.model
            if profile.language:
                config.chatterbox_language = profile.language
            ref = self.ref_path(vid) if profile.ref else ""
            if ref and not os.path.isfile(ref):
                raise AudioError(f"the reference file for '{profile.name}' is missing")
            config.chatterbox_voice_path = ref
            for key in ("exaggeration", "cfg_weight", "temperature"):
                if key in profile.tuning:
                    setattr(config, f"chatterbox_{key}", float(profile.tuning[key]))
            applied.update({"model": config.chatterbox_model,
                            "language": config.chatterbox_language,
                            "voice": config.chatterbox_voice_path})
        elif profile.engine == "elevenlabs":
            config.tts_provider = "elevenlabs"
            config.elevenlabs_voice = profile.voice
            applied.update({"voice": profile.voice})
        elif profile.engine == "piper":
            config.tts_provider = "piper"
            if profile.voice:
                config.piper_voice = profile.voice
            if "speed" in profile.tuning:
                config.piper_speed = float(profile.tuning["speed"])
            if "pitch" in profile.tuning and hasattr(config, "piper_pitch"):
                config.piper_pitch = int(profile.tuning["pitch"])
            if "volume" in profile.tuning and hasattr(config, "piper_volume"):
                config.piper_volume = float(profile.tuning["volume"])
            applied.update({"voice": config.piper_voice,
                            "speed": config.piper_speed,
                            "pitch": getattr(config, "piper_pitch", None),
                            "volume": getattr(config, "piper_volume", None)})
        else:
            config.tts_provider = "kokoro"
            if profile.voice:
                config.kokoro_voice = profile.voice
            if "speed" in profile.tuning:
                config.kokoro_speed = float(profile.tuning["speed"])
            if "pitch" in profile.tuning and hasattr(config, "kokoro_pitch"):
                config.kokoro_pitch = int(profile.tuning["pitch"])
            applied.update({"voice": config.kokoro_voice,
                            "speed": config.kokoro_speed,
                            "pitch": getattr(config, "kokoro_pitch", None)})
        if mark_active:
            data = self._load()
            data["active_id"] = profile.id
            profile.last_used = _now()
            self._save()
        return applied

    def preview_config(self, vid: str, config):
        """A copy of the config that speaks the way this profile sounds."""
        profile = self.get(vid)
        overrides: dict = {"tts_provider": profile.engine}
        if profile.engine == "chatterbox":
            if profile.model:
                overrides["chatterbox_model"] = profile.model
            if profile.language:
                overrides["chatterbox_language"] = profile.language
            overrides["chatterbox_voice_path"] = self.ref_path(vid) if profile.ref else ""
            for key in ("exaggeration", "cfg_weight", "temperature"):
                if key in profile.tuning:
                    overrides[f"chatterbox_{key}"] = float(profile.tuning[key])
        elif profile.engine == "elevenlabs":
            overrides["elevenlabs_voice"] = profile.voice
            overrides.pop("tts_provider", None)      # engine name is the provider
            overrides["tts_provider"] = "elevenlabs"
        elif profile.engine == "piper":
            if profile.voice:
                overrides["piper_voice"] = profile.voice
            if "speed" in profile.tuning:
                overrides["piper_speed"] = float(profile.tuning["speed"])
            if "pitch" in profile.tuning:
                overrides["piper_pitch"] = int(profile.tuning["pitch"])
            if "volume" in profile.tuning:
                overrides["piper_volume"] = float(profile.tuning["volume"])
        else:
            if profile.voice:
                overrides["kokoro_voice"] = profile.voice
            if "speed" in profile.tuning:
                overrides["kokoro_speed"] = float(profile.tuning["speed"])
            if "pitch" in profile.tuning:
                overrides["kokoro_pitch"] = int(profile.tuning["pitch"])
        try:
            return replace(config, **overrides)
        except TypeError:                     # older config objects
            clone = copy.copy(config)
            for key, value in overrides.items():
                setattr(clone, key, value)
            return clone


# --------------------------------------------------------------------------- #
# validation helpers
# --------------------------------------------------------------------------- #
def clean_tuning(tuning: "dict | None") -> dict:
    """Keep only known numeric settings, clamped to sane ranges."""
    limits = {
        "exaggeration": (0.0, 1.0), "cfg_weight": (0.0, 1.0),
        "temperature": (0.05, 2.0), "speed": (0.5, 2.0), "pitch": (0, 99),
        "volume": (0.2, 2.0),
    }
    out: dict = {}
    for key, value in (tuning or {}).items():
        if key not in TUNING_KEYS or value in (None, ""):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise AudioError(f"{key} must be a number, got {value!r}") from None
        low, high = limits[key]
        out[key] = float(max(low, min(high, number)))
        if key == "pitch":
            out[key] = int(out[key])
    return out


def clean_tags(tags) -> list:
    if not tags:
        return []
    if isinstance(tags, str):
        tags = [t for t in re.split(r"[,\s]+", tags) if t]
    cleaned = []
    for tag in tags:
        tag = str(tag).strip()
        if not tag:
            continue
        if not tag.startswith("["):
            tag = f"[{tag.strip('[]')}]"
        if tag not in cleaned:
            cleaned.append(tag)
    return cleaned


def _samples(pcm: bytes) -> "array":
    """Raw little-endian s16le bytes -> int16 sample array."""
    from array import array

    if not pcm:
        raise AudioError("no audio data received")
    body = pcm[: len(pcm) - (len(pcm) % 2)]
    samples = array("h")
    samples.frombytes(body)
    return samples


def decode_base64(payload: str, *, what: str = "audio") -> bytes:
    """Decode a data-url or bare base64 string from the browser."""
    if not payload:
        raise AudioError(f"no {what} data received")
    text = payload.split(",", 1)[1] if payload.startswith("data:") else payload
    try:
        return base64.b64decode(text, validate=False)
    except (binascii.Error, ValueError) as e:
        raise AudioError(f"could not decode the uploaded {what}: {e}") from e


def get_library(root: str | None = None) -> VoiceLibrary:
    """The process-wide library (data/voices unless configured elsewhere)."""
    global _LIBRARY
    if root is None:
        try:
            from .config import Config

            root = Config.load().voice_library_dir
        except Exception:
            root = "data/voices"
    if _LIBRARY is None or root != _LIBRARY.root:
        _LIBRARY = VoiceLibrary(root)
    return _LIBRARY


_LIBRARY: "VoiceLibrary | None" = None
