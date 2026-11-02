"""ChatterBox (Resemble AI) — in-process, open-source TTS.

Runs locally, no server: models are turbo (350M, English, paralinguistic tags),
nano (110M, English, CPU), multilingual (500M, 23+ languages) and original.
Output is 24 kHz and is resampled to the pipeline's 16 kHz on the way out.
"""
from __future__ import annotations

import asyncio
import os
import shutil

from ..resample import TARGET_RATE, resample_pcm16

from ._contract import EngineMixin

# ---------------- ChatterBox metadata (shared by engine + API) ----------------

# Selectable ChatterBox engines, in dashboard order.
CHATTERBOX_MODELS = ("turbo", "nano", "multilingual", "original")

# Languages Chatterbox-Multilingual accepts as `language_id` (23 + English).
CHATTERBOX_LANGUAGES = {
    "ar": "Arabic", "da": "Danish", "de": "German", "el": "Greek",
    "en": "English", "es": "Spanish", "fi": "Finnish", "fr": "French",
    "he": "Hebrew", "hi": "Hindi", "it": "Italian", "ja": "Japanese",
    "ko": "Korean", "ms": "Malay", "nl": "Dutch", "no": "Norwegian",
    "pl": "Polish", "pt": "Portuguese", "ru": "Russian", "sv": "Swedish",
    "sw": "Swahili", "tr": "Turkish", "zh": "Chinese",
}

_CHATTERBOX_INSTALL_HINT = (
    "ChatterBox is not installed. Install it with `pip install chatterbox-tts` "
    "(it pulls in PyTorch; the first synthesis downloads ~2-3 GB of model "
    "weights). Meanwhile, set TTS_PROVIDER=elevenlabs, or give the engine a "
    "standby with TTS_STANDBY_PROVIDER=kokoro so it fails over automatically."
)


def _chatterbox_unavailable(exc: ImportError) -> ImportError:
    """Turn \"No module named 'torch'\" into an actionable, failable error.

    Returned as a ModuleNotFoundError named after the engine so
    `is_provider_error()` treats it as an unavailable engine (failover) instead
    of an internal bug.
    """
    missing = (getattr(exc, "name", "") or "chatterbox").split(".")[0]
    return ModuleNotFoundError(
        f"{_CHATTERBOX_INSTALL_HINT} (missing dependency: {missing})",
        name="chatterbox",
    )



class ChatterboxEngine(EngineMixin):
    """ChatterBox synthesis, mixed into :class:`agent.tts.TTS`."""

# ---------------- provider: chatterbox ----------------
    # ChatterBox: Resemble AI open-source TTS. Runs in-process (no server needed).
    # Models: turbo (350M, English, paralinguistic tags), nano (110M, English, CPU),
    #         multilingual (500M, 23+ languages). Output @24 kHz → resampled to 16 kHz.
    _chatterbox_model_cache: dict = {}

    # Rough HF download sizes per engine — used only for a fast, friendly
    # pre-flight disk check so a doomed download fails in seconds.
    _chatterbox_model_gb = {"turbo": 2.5, "nano": 2.0, "multilingual": 3.0, "original": 3.0}

    def _chatterbox_disk_ok(self, need_gb: float) -> None:

        cache = os.environ.get("HF_HOME") or os.path.join(
            os.path.expanduser("~"), ".cache", "huggingface"
        )
        # The cache directory may not exist yet on a first run (or HF_HOME may
        # point somewhere not created), and disk_usage() raises
        # FileNotFoundError for a missing path — which would turn this friendly
        # pre-flight check into a crash. Measure the nearest existing parent,
        # i.e. the drive the weights would actually land on.
        probe = os.path.abspath(cache)
        while probe and not os.path.isdir(probe):
            parent = os.path.dirname(probe)
            if parent == probe:
                break
            probe = parent
        free = shutil.disk_usage(probe or os.path.abspath(os.sep)).free / (1024 ** 3)
        if free < need_gb:
            raise RuntimeError(
                f"ChatterBox needs ~{need_gb:.1f} GB free to download its model "
                f"weights, but only {free:.1f} GB is available on this drive. "
                f"Free up space (for example delete unused downloaded models) "
                f"and try again — or set HF_HOME to a drive with more room."
            )

    def _get_chatterbox_model(self):
        """Load and cache the ChatterBox model in-process."""
        key = (self.config.chatterbox_model, self.config.chatterbox_device,
               self.config.chatterbox_language)
        cached = self._chatterbox_model_cache.get(key)
        if cached is not None:
            return cached

        self._chatterbox_disk_ok(
            self._chatterbox_model_gb.get(self.config.chatterbox_model, 2.5)
        )

        try:
            if self.config.chatterbox_model == "multilingual":
                from chatterbox.mtl_tts import ChatterboxMultilingualTTS
                model = ChatterboxMultilingualTTS.from_pretrained(
                    device=self.config.chatterbox_device,
                )
            elif self.config.chatterbox_model in ("turbo", "nano"):
                # The published chatterbox-tts wheel ships a single Turbo engine;
                # the smaller "nano" variant only exists in the development
                # source tree. Map "nano" onto the same engine so the
                # switch-voice API keeps working on any installation.
                from chatterbox.tts_turbo import ChatterboxTurboTTS
                model = ChatterboxTurboTTS.from_pretrained(
                    device=self.config.chatterbox_device,
                )
            else:
                from chatterbox.tts import ChatterboxTTS
                model = ChatterboxTTS.from_pretrained(
                    device=self.config.chatterbox_device)
        except ImportError as exc:
            raise _chatterbox_unavailable(exc) from exc
        self._chatterbox_model_cache[key] = model
        return model

    def _chatterbox_sync(self, text: str) -> bytes:
        """Generate full audio from ChatterBox, return PCM16 @16 kHz."""
        try:
            import numpy as np
            import torch
        except ImportError as exc:
            raise _chatterbox_unavailable(exc) from exc

        model = self._get_chatterbox_model()
        voice_path = self.config.chatterbox_voice_path or None
        audio_prompt = voice_path if voice_path else None

        if self.config.chatterbox_model == "multilingual":
            wav = model.generate(
                text,
                language_id=self.config.chatterbox_language,
                audio_prompt_path=audio_prompt,
                exaggeration=self.config.chatterbox_exaggeration,
                cfg_weight=self.config.chatterbox_cfg_weight,
                temperature=self.config.chatterbox_temperature,
            )
        elif self.config.chatterbox_model in ("turbo", "nano"):
            wav = model.generate(
                text,
                audio_prompt_path=audio_prompt,
                temperature=self.config.chatterbox_temperature,
            )
        else:
            wav = model.generate(
                text,
                audio_prompt_path=audio_prompt,
                exaggeration=self.config.chatterbox_exaggeration,
                cfg_weight=self.config.chatterbox_cfg_weight,
                temperature=self.config.chatterbox_temperature,
            )

        if isinstance(wav, torch.Tensor):
            wav = wav.squeeze(0).cpu().numpy()

        # Normalize to int16
        peak = abs(wav).max() if len(wav) > 0 else 1
        if peak > 0:
            wav = (wav / peak * 0.95 * 32767).astype(np.int16)
        else:
            wav = (wav * 32767).astype(np.int16)
        pcm = wav.tobytes()

        # ChatterBox outputs at 24 kHz -> resample to 16 kHz
        return resample_pcm16(pcm, 24000, TARGET_RATE)

    async def _pcm_chatterbox(self, text: str):
        """Async: generate audio in a worker thread, resample to 16 kHz."""
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, lambda: self._chatterbox_sync(text)
        )
        if result:
            yield result

    def switch_chatterbox_model(self, model: str) -> str:
        """Switch to a different ChatterBox model at runtime."""
        if model not in CHATTERBOX_MODELS:
            raise ValueError(
                f"Invalid model '{model}'. Use one of: {', '.join(CHATTERBOX_MODELS)}")
        self.config.chatterbox_model = model
        self._chatterbox_model_cache.clear()
        return f"Switched to Chatterbox-{model}"

    @staticmethod
    def chatterbox_settings(body: dict) -> dict:
        """Validate + resolve a ChatterBox switch request into config changes.

        A single POST may carry several fields at once (that is what the
        documented curl does), so the precedence is explicit rather than
        accidental — and identical for the API and for callers that edit the
        config directly:

            model      → selects the engine
            voice      → reference wav for cloning (empty string clears it)
            language   → only meaningful for the multilingual engine, and only
                         the engine switch counts as "switched"

        Returns a dict with the resolved model / voice / language plus a
        `changed` flag and a human-readable `message`. Raises ValueError with a
        friendly message when a value is rejected.
        """
        model_given = "model" in body
        model = str(body.get("model") or "").strip()
        if model_given and not model:
            raise ValueError("empty model")
        if model and model not in CHATTERBOX_MODELS:
            raise ValueError(
                f"invalid model '{model}'. Use one of: {', '.join(CHATTERBOX_MODELS)}")

        voice_given = "voice" in body
        voice = str(body.get("voice") or "").strip()

        lang_given = "language" in body
        language = str(body.get("language") or "").strip().lower()
        if lang_given and not language:
            raise ValueError(
                "empty language. Use one of: " + ", ".join(CHATTERBOX_LANGUAGES))
        if language and language not in CHATTERBOX_LANGUAGES:
            raise ValueError(
                f"unsupported language '{language}'. Chatterbox-Multilingual "
                f"speaks: {', '.join(CHATTERBOX_LANGUAGES)}")

        # Precedence: the model asked for in this request, else the model class
        # implied by the language, else whatever is already configured.
        effective_model = model or ("multilingual" if language else "")
        if not effective_model:
            effective_model = "multilingual"

        # Changing/replacing the engine, cloning the voice, or re-targeting a
        # language all change how the line sounds — remember that something moved.
        changed = bool(model) or voice_given or bool(language)

        if language and model and model != "multilingual":
            # Accepting this silently would be the very bug this resolver exists
            # to prevent: the operator would believe Spanish was set while the
            # English-only engine ignores it.
            raise ValueError(
                f"the '{model}' engine is English-only, so it cannot speak "
                f"'{CHATTERBOX_LANGUAGES[language]}'. Use "
                f"model='multilingual' for {', '.join(CHATTERBOX_LANGUAGES)}."
            )

        message = f"Switched to Chatterbox-{effective_model}"
        return {
            "model": effective_model,
            "voice": voice if voice_given else None,
            "language": language if language else None,
            "changed": changed,
            "message": message,
        }

    @staticmethod
    def validate_voice_reference(path: str) -> str:
        """Check a voice-clone reference file is actually usable.

        Returns an empty string when it is fine, else a human-readable problem.
        ChatterBox loads this file from the *server's* filesystem, so a path
        that does not resolve there (a typo, or another machine's drive letter)
        must be reported now rather than blowing up mid-call.
        """

        if not path:
            return ""
        ext = os.path.splitext(path)[1].lower()
        audio_ext = (".wav", ".mp3", ".flac", ".m4a", ".ogg", ".opus", ".aiff", ".aif")
        if ext not in audio_ext:
            return (f"voice reference must be an audio file "
                    f"({', '.join(audio_ext)}), got '{path}'")
        if not os.path.isfile(path):
            return (
                f"voice reference not found on the server: {os.path.abspath(path)}. "
                f"ChatterBox reads this file from the machine running the bot — "
                f"upload/copy it there (10-15 s of clean speech works best), or "
                f"clear the field to use the built-in voice."
            )
        try:
            if os.path.getsize(path) == 0:
                return f"voice reference is empty: {path}"
        except OSError as exc:
            return f"voice reference is not readable ({exc}): {path}"
        return ""

    def chatterbox_model_info(self) -> dict:
        """Return info about the active ChatterBox model.

        `voice_cloned` only claims a clone when the reference file is really
        usable — reporting True for a typo'd path hid a broken setup behind a
        green tick. `voice_reference_problem` carries the reason when it is not.
        """
        model = self.config.chatterbox_model
        ref = (self.config.chatterbox_voice_path or "").strip()
        problem = self.validate_voice_reference(ref) if ref else ""
        info = {
            "model": model,
            "device": self.config.chatterbox_device,
            "voice_path": ref,
            "voice_cloned": bool(ref) and not problem,
            "voice_reference_problem": problem,
            "language": self.config.chatterbox_language,
            "sample_rate": 24000,
            "output_rate": TARGET_RATE,
            "paralinguistic_tags": model in ("turbo", "nano"),
            "multilingual": model == "multilingual",
            "available_models": list(CHATTERBOX_MODELS),
            "available_languages": dict(CHATTERBOX_LANGUAGES),
            "exaggeration": self.config.chatterbox_exaggeration,
            "cfg_weight": self.config.chatterbox_cfg_weight,
            "temperature": self.config.chatterbox_temperature,
        }
        if model == "turbo":
            info["description"] = "Chatterbox-Turbo (350M) — paralinguistic tags, low latency"
            info["languages"] = ["en"]
        elif model == "nano":
            info["description"] = "Chatterbox-Nano (110M) — CPU 3x realtime, smallest"
            info["languages"] = ["en"]
        elif model == "multilingual":
            info["description"] = "Chatterbox-Multilingual V3 (500M) — 23+ languages"
            info["languages"] = list(CHATTERBOX_LANGUAGES)
        else:
            info["description"] = "Chatterbox Original (500M) — English"
            info["languages"] = ["en"]
        return info

