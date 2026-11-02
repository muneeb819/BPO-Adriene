"""Voice-library and TTS routes.

Everything behind /api/tts and /api/voices: listing engines and voices,
switching engine, uploading a reference, matching, designing with ElevenLabs or
the local designer, remixing, previewing, and the saved-voice CRUD.

Self-contained apart from CONFIG: the helpers below (the library handle, the
error mapping, the designer options, the draft config and the preview renderer)
are used only by these routes, so they travel with them rather than staying in
server/app.py.
"""
from __future__ import annotations


from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from server.context import CONFIG

router = APIRouter()


@router.get("/api/tts/voices")
def tts_voices():
    """List available voices for the configured TTS engine."""
    c = CONFIG
    from agent.tts import TTS
    tts = TTS(c)
    result = {"provider": tts.active, "primary": tts.primary, "standby": tts.standby}
    if tts.active != tts.primary:
        # Never make the dashboard guess why the configured engine is silent.
        # `status()` also consults the class-level record left by the request
        # that actually failed — a fresh TTS instance has no reason of its own.
        reason = tts.status()["failover_reason"]
        if reason:
            result["failover_reason"] = reason

    if tts.active == "kokoro":
        import requests
        try:
            r = requests.get(f"{c.kokoro_base_url.rstrip('/')}/v1/audio/voices", timeout=5)
            if r.status_code == 200:
                data = r.json()
                voices = data.get("voices", [])
                result["voices"] = [{"id": v, "name": v} for v in voices] if isinstance(voices, list) else voices
                result["reachable"] = True
                result["current_voice"] = c.kokoro_voice
                result["speed"] = c.kokoro_speed
            else:
                result["voices"] = []
                result["reachable"] = False
                result["error"] = f"Kokoro returned {r.status_code}"
        except Exception as e:
            result["voices"] = []
            result["reachable"] = False
            result["error"] = str(e)
        result["known_voices"] = [
            {"id": "af_bella", "name": "Bella (warm female)"},
            {"id": "af_heart", "name": "Heart (warm)"},
            {"id": "af_nicole", "name": "Nicole"},
            {"id": "af_sarah", "name": "Sarah"},
            {"id": "af_sky", "name": "Sky"},
            {"id": "am_adam", "name": "Adam"},
            {"id": "am_michael", "name": "Michael"},
        ]
    elif tts.active == "chatterbox":
        result["model_info"] = tts.chatterbox_model_info()
        result["voices"] = [{"id": "default", "name": "Default (cloned from reference)"}]
        result["current_voice"] = c.chatterbox_voice_path or "default"
        result["available_models"] = ["turbo", "nano", "multilingual", "original"]
    elif tts.active == "elevenlabs":
        result["voices"] = [{"id": c.elevenlabs_voice, "name": c.elevenlabs_voice}]
        result["model"] = c.elevenlabs_model
        result["current_voice"] = c.elevenlabs_voice
    elif tts.active == "piper":
        # Local neural voices: ask the offline server what it has installed.
        import requests

        result["model"] = c.piper_model
        result["current_voice"] = c.piper_voice or "(server default)"
        try:
            r = requests.get(f"{c.piper_base_url.rstrip('/')}/v1/audio/voices", timeout=3)
            payload = r.json() if r.status_code == 200 else {}
        except Exception:
            payload = {}
        details = payload.get("voice_details") or []
        result["voices"] = ([{"id": v["id"], "name": v.get("name") or v["id"],
                              "language": v.get("language"), "kind": v.get("kind"),
                              "neural": True} for v in details]
                            or [{"id": v, "name": v} for v in payload.get("voices", [])])
        result["reachable"] = bool(result["voices"])
        result["known_voices"] = result["voices"]
        result["engine"] = "piper"
        result["neural"] = True
    else:
        result["voices"] = []

    # A failed-over ChatterBox deployment still needs its full model metadata.
    # The dashboard's voice panel renders its model / language / tag controls
    # from `model_info` and `available_models`, so keying those on the *active*
    # engine left every dropdown empty (and the status line reading
    # "out NaNkHz") after a failover — precisely when an operator needs to
    # re-route the engine.
    if tts.primary == "chatterbox" and "model_info" not in result:
        from agent.tts import CHATTERBOX_MODELS

        result["model_info"] = tts.chatterbox_model_info()
        result["available_models"] = list(CHATTERBOX_MODELS)
    return result


@router.post("/api/tts/switch-voice")
async def tts_switch_voice(request: Request):
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    from agent.tts import TTS
    tts = TTS(CONFIG)
    provider = tts.active
    if tts.primary == "chatterbox":
        # This endpoint configures the *selected* engine, not whichever engine
        # happens to be speaking at this instant. After a failover `active`
        # becomes the standby, which used to send every model/language/voice
        # request down the kokoro branch (or 400 "empty voice") — exactly when
        # an operator is most likely to be re-routing the ChatterBox engine.
        provider = "chatterbox"
    voice = data.get("voice", "").strip()
    if not voice and provider != "chatterbox":
        return JSONResponse({"error": "empty voice"}, status_code=400)
    if provider == "kokoro":
        CONFIG.kokoro_voice = voice
        return {"ok": True, "provider": provider, "voice": voice}
    elif provider == "piper":
        # Changing the voice keeps the current tuning; pitch/speed arrive through
        # the voice library (a library voice carries its own measured tuning).
        CONFIG.piper_voice = voice
        tune = data.get("tuning") or {}
        if "pitch" in tune:
            CONFIG.piper_pitch = int(float(tune["pitch"]))
        if "speed" in tune:
            CONFIG.piper_speed = float(tune["speed"])
        if "volume" in tune:
            CONFIG.piper_volume = float(tune["volume"])
        return {"ok": True, "provider": provider, "voice": voice,
                "pitch": CONFIG.piper_pitch, "speed": CONFIG.piper_speed,
                "volume": CONFIG.piper_volume}
    elif provider == "elevenlabs":
        CONFIG.elevenlabs_voice = voice
        return {"ok": True, "provider": provider, "voice": voice}
    elif provider == "chatterbox":
        # One request may carry model + language + voice together (that is what
        # the documented curl does), and every field must actually stick.
        # Previously the chain returned after the first branch, so "language"
        # and "voice" were silently dropped and an unknown model raised
        # ValueError → HTTP 500 instead of a friendly 400.
        from agent.tts import CHATTERBOX_LANGUAGES, CHATTERBOX_MODELS

        try:
            resolved = tts.chatterbox_settings(data)
        except ValueError as e:
            return JSONResponse(
                {"error": str(e), "available_models": list(CHATTERBOX_MODELS),
                 "available_languages": list(CHATTERBOX_LANGUAGES)},
                status_code=400,
            )

        if "model" in data and resolved["model"] != CONFIG.chatterbox_model:
            try:
                tts.switch_chatterbox_model(resolved["model"])
            except ValueError as e:
                return JSONResponse(
                    {"error": str(e), "available_models": list(CHATTERBOX_MODELS)},
                    status_code=400,
                )
        elif resolved["model"] in CHATTERBOX_MODELS:
            # Keep the config in step even when the engine is unchanged, so
            # model_info never reports a different engine than the one in use.
            CONFIG.chatterbox_model = resolved["model"]

        if resolved["language"]:
            CONFIG.chatterbox_language = resolved["language"]

        voice_error = None
        if resolved["voice"] is not None:
            if resolved["voice"]:
                voice_error = tts.validate_voice_reference(resolved["voice"])
                if voice_error:
                    return JSONResponse({"error": voice_error}, status_code=400)
            CONFIG.chatterbox_voice_path = resolved["voice"] or ""
        elif CONFIG.chatterbox_voice_path:
            # A different engine was selected; a reference cloned for the old
            # one may no longer be valid, so re-check it rather than pretend.
            voice_error = tts.validate_voice_reference(CONFIG.chatterbox_voice_path)

        return {
            "ok": True,
            "provider": provider,
            "result": resolved["message"],
            "model_info": tts.chatterbox_model_info(),
            "available_models": list(CHATTERBOX_MODELS),
            "available_languages": dict(CHATTERBOX_LANGUAGES),
            "applied": {
                "model": CONFIG.chatterbox_model,
                "language": CONFIG.chatterbox_language,
                "voice": CONFIG.chatterbox_voice_path,
            },
            **({"warning": voice_error} if voice_error else {}),
        }
    return JSONResponse({"error": f"unsupported provider {provider}"}, status_code=400)


# ---------------- VOICE STUDIO (library / design / remix) ----------------
DEFAULT_PREVIEW_TEXT = (
    "Hi, this is Lina from EverTrust. I'm calling about your Medicare benefits, "
    "and I wanted to see how you're doing today."
)


def _library():
    from agent.voice_library import get_library

    return get_library(CONFIG.voice_library_dir)


def _voice_error(e) -> JSONResponse:
    from agent.audio_dsp import AudioError

    if isinstance(e, AudioError):
        return JSONResponse({"error": str(e)}, status_code=400)
    if isinstance(e, KeyError):
        return JSONResponse({"error": f"unknown voice '{e.args[0]}'"}, status_code=404)
    raise e


def _designer_options() -> dict:
    """Everything the dashboard panels need to build their controls."""
    from agent.tts import CHATTERBOX_LANGUAGES, CHATTERBOX_MODELS

    backend_voices = []
    backend_engine = ""
    try:
        import requests

        r = requests.get(f"{CONFIG.kokoro_base_url.rstrip('/')}/v1/audio/voices", timeout=3)
        if r.status_code == 200:
            data = r.json()
            # The same wire format serves both local engines, so report which one
            # is really behind the URL (the label "kokoro" is historical).
            backend_engine = str(data.get("engine") or data.get("model") or "")
            details = data.get("voice_details")
            if isinstance(details, list) and details:
                backend_voices = details
            else:
                backend_voices = [{"id": v, "name": v} for v in data.get("voices", [])
                                  if isinstance(v, str)]
    except Exception:
        backend_voices = []
        backend_engine = ""
    from agent import elevenlabs_design as el
    from agent import neural_tts
    from agent.voice_library import ENGINES

    try:
        neural_voices = neural_tts.describe_models()
    except Exception as exc:                                  # noqa: BLE001
        neural_voices = []
        neural_error = str(exc)
    else:
        neural_error = ""
    return {
        "engines": list(ENGINES),
        "neural_voices": neural_voices,
        "neural_error": neural_error,
        "design_providers": el.providers(CONFIG),
        "chatterbox_models": list(CHATTERBOX_MODELS),
        "chatterbox_languages": dict(CHATTERBOX_LANGUAGES),
        "chatterbox_tags": ["[laugh]", "[chuckle]", "[cough]", "[sigh]",
                            "[gasp]", "[yawn]", "[um]"],
        "backend_voices": backend_voices,
        "backend_engine": backend_engine,
        "presets": [
            {"id": "warm", "name": "Warm & calm",
             "tuning": {"exaggeration": 0.35, "cfg_weight": 0.5, "temperature": 0.7, "speed": 0.95, "pitch": 48}},
            {"id": "bright", "name": "Bright & upbeat",
             "tuning": {"exaggeration": 0.75, "cfg_weight": 0.4, "temperature": 0.9, "speed": 1.08, "pitch": 60}},
            {"id": "serious", "name": "Serious & steady",
             "tuning": {"exaggeration": 0.2, "cfg_weight": 0.7, "temperature": 0.6, "speed": 0.98, "pitch": 38}},
            {"id": "energetic", "name": "Energetic",
             "tuning": {"exaggeration": 0.9, "cfg_weight": 0.35, "temperature": 1.0, "speed": 1.15, "pitch": 56}},
        ],
        "max_reference_seconds": 30,
        "max_upload_mb": 25,
    }


@router.get("/api/voices")
def voices_list():
    """The saved voice library (distinct from /api/tts/voices = engine voices)."""
    lib = _library()
    return {
        "ok": True,
        "voices": lib.list(),
        "active_id": lib.active_id(),
        "designer": _designer_options(),
        "remix_modes": ["blend", "stitch"],
    }


@router.post("/api/voices/upload")
async def voices_upload(request: Request):
    """Add a voice from an uploaded file, a server path, or a mic recording."""
    from agent.voice_library import decode_base64

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    name = (data.get("name") or "").strip()
    meta = {k: data.get(k) for k in ("engine", "model", "language", "voice",
                                     "tuning", "tags", "notes", "preview_text")}
    # match=1 stores the recording *and* points the voice at the resident neural
    # voice closest to it, so the clone can be heard immediately (see
    # VoiceLibrary.create_matched). Without it, the recording is just stored.
    want_match = bool(data.get("match"))
    match: dict = {}
    try:
        lib = _library()
        if want_match:
            meta.pop("engine", None)             # the matcher decides the engine
            if data.get("pcm_base64"):
                profile, match = lib.create_matched(
                    name, pcm=decode_base64(data["pcm_base64"], what="recording"),
                    sample_rate=int(data.get("sample_rate") or 16000), **meta)
            elif data.get("data_base64"):
                profile, match = lib.create_matched(
                    name, data=decode_base64(data["data_base64"], what="file"), **meta)
            elif data.get("path"):
                profile, match = lib.create_matched(name, path=data["path"].strip(), **meta)
            else:
                return JSONResponse(
                    {"error": "send one of: data_base64, pcm_base64 or path"},
                    status_code=400)
            return {"ok": True, "voice": profile.to_dict(), "match": match}
        if data.get("pcm_base64"):
            profile = lib.create_from_pcm(
                name, decode_base64(data["pcm_base64"], what="recording"),
                int(data.get("sample_rate") or 16000), **meta)
        elif data.get("data_base64"):
            profile = lib.create_from_bytes(
                name, decode_base64(data["data_base64"], what="file"),
                data.get("filename") or "", **meta)
        elif data.get("path"):
            profile = lib.create_from_path(name, data["path"].strip(), **meta)
        else:
            return JSONResponse(
                {"error": "send one of: data_base64 (a file), pcm_base64 "
                          "(a mic recording) or path (a file on the server)"},
                status_code=400)
    except Exception as e:                                    # noqa: BLE001
        return _voice_error(e)
    return {"ok": True, "voice": profile.to_dict()}


@router.post("/api/voices/match")
async def voices_match(request: Request):
    """Measure a recording and report which resident neural voice it matches.

    Read-only: nothing is created, so the dashboard can show "your recording is
    closest to Amy (pitch +1.3 semitones)". Send the audio as ``data_base64``
    (file), ``pcm_base64`` (mic) or ``path`` (already on the server).
    """
    from agent import audio_dsp as dsp
    from agent import neural_tts
    from agent.voice_library import decode_base64

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    try:
        if data.get("pcm_base64"):
            samples, rate = dsp.load_audio(decode_base64(data["pcm_base64"], what="recording"))
            if not rate:
                rate = int(data.get("sample_rate") or 16000)
        elif data.get("data_base64"):
            samples, rate = dsp.load_audio(decode_base64(data["data_base64"], what="file"))
        elif data.get("path"):
            samples, rate = dsp.load_audio(data["path"].strip())
            if len(samples) == 0:
                raise dsp.AudioError("that file has no audio in it")
        else:
            return JSONResponse({"error": "send data_base64, pcm_base64 or path"},
                                status_code=400)
        prepared, _ = dsp.prepare_reference(samples, rate or 16000)
        match = neural_tts.match_reference(prepared, dsp.TARGET_RATE,
                                          language=data.get("language"))
    except neural_tts.NeuralTTSError as e:
        return JSONResponse({"error": str(e), "hint": "python scripts/fetch_neural_voices.py"},
                            status_code=503)
    except Exception as e:                                    # noqa: BLE001
        return _voice_error(e)
    return {"ok": True, "match": match}


@router.post("/api/voices/describe")
async def voices_describe(request: Request):
    """**Prompt to voice**: turn a description into a voice.

    Local neural engine (default): ``{"description": "warm older British man,
    speaks slowly"}`` picks the resident voice that fits and stores the measured
    tuning. ``dry_run`` returns the plan without creating anything.
    """
    from agent import neural_tts

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    description = (data.get("description") or data.get("prompt") or "").strip()
    if not description:
        return JSONResponse(
            {"error": "describe the voice first, e.g. 'warm older British man, "
                      "speaks slowly'",
             "examples": ["warm older British man, speaks slowly",
                          "young energetic American woman",
                          "deep serious male narrator",
                          "bright cheerful female, fast"]},
            status_code=400)
    try:
        if data.get("dry_run"):
            plan = neural_tts.design_from_text(
                description, language=data.get("language"),
                pitch_level=(data.get("tuning") or {}).get("pitch"),
                speed=(data.get("tuning") or {}).get("speed"))
            return {"ok": True, "dry_run": True, "plan": plan}
        lib = _library()
        profile, plan = lib.describe(
            data.get("name") or description[:40], description,
            language=data.get("language"), tuning=data.get("tuning"),
            tags=data.get("tags"), notes=data.get("notes"),
            preview_text=data.get("preview_text"))
    except Exception as e:                                    # noqa: BLE001
        return _voice_error(e)
    return {"ok": True, "voice": profile.to_dict(), "plan": plan}


@router.get("/api/voices/design/providers")
async def voices_design_providers():
    """Which prompt-to-voice engines are usable right now (and how to unlock them)."""
    from agent import elevenlabs_design as el
    from agent import neural_tts

    return {"ok": True, "providers": el.providers(CONFIG),
            "local_models": neural_tts.describe_models()}


@router.post("/api/voices/design/elevenlabs")
async def voices_design_elevenlabs(request: Request):
    """ElevenLabs Voice Design: generate voices from a description, keep one.

    ``{"description": "..."}`` returns playable previews. Add
    ``{"keep": true, "generated_voice_id": "..."}`` to save the chosen one as a
    real voice; it is then stored in the library (engine ``elevenlabs``) with a
    rendered sample as its reference, so the local cloner/matcher can use it too.
    """
    from agent import elevenlabs_design as el

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    if not el.available(CONFIG):
        return JSONResponse(
            {"error": "Voice Design from a description needs an ElevenLabs key",
             "hint": el.hint(),
             "alternative": {"endpoint": "/api/voices/describe",
                             "detail": "design a voice from the resident neural voices "
                                       "without any key"}},
            status_code=400)
    try:
        description = (data.get("description") or "").strip()
        if not data.get("generated_voice_id"):
            result = el.design(description, (data.get("text") or "").strip(),
                               config=CONFIG,
                               loudness=float(data.get("loudness") or 0.5),
                               guidance=float(data.get("guidance") or 0.5),
                               seed=data.get("seed"), enhance=bool(data.get("enhance", True)))
            return {"ok": True, **result}
        # keeping one: persist it on ElevenLabs and in the local library
        saved = el.save(data["generated_voice_id"], data.get("name") or description[:40],
                        description, config=CONFIG)
        profile, info = el.save_locally(
            saved["voice_id"], data.get("name") or saved["name"], description,
            (data.get("text") or "").strip(), config=CONFIG, samples=_library())
        voice: dict = profile.to_dict()  # type: ignore[attr-defined]
        return {"ok": True, "saved": saved, "voice": voice, "info": info}
    except el.DesignUnavailable as e:
        return JSONResponse({"error": str(e), "hint": el.hint()}, status_code=400)
    except el.DesignError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    except Exception as e:                                    # noqa: BLE001
        return _voice_error(e)


@router.post("/api/voices/design")
async def voices_design(request: Request):
    """Create a voice by tuning an engine (no reference audio needed)."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    try:
        lib = _library()
        profile = lib.design(
            data.get("name"), engine=data.get("engine") or "chatterbox",
            model=data.get("model"), language=data.get("language"),
            voice=data.get("voice"), tuning=data.get("tuning"),
            tags=data.get("tags"), notes=data.get("notes"),
            preview_text=data.get("preview_text"))
    except Exception as e:                                    # noqa: BLE001
        return _voice_error(e)
    return {"ok": True, "voice": profile.to_dict()}


@router.post("/api/voices/remix")
async def voices_remix(request: Request):
    """Blend or stitch existing voices' references into a new voice."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    try:
        lib = _library()
        profile = lib.remix(
            data.get("name"), data.get("sources"), mode=data.get("mode") or "blend",
            pitch_semitones=data.get("pitch_semitones") or 0.0,
            match=bool(data.get("match")),
            engine=data.get("engine"), model=data.get("model"),
            language=data.get("language"), tuning=data.get("tuning"),
            tags=data.get("tags"), notes=data.get("notes"))
    except Exception as e:                                    # noqa: BLE001
        return _voice_error(e)
    return {"ok": True, "voice": profile.to_dict()}


@router.post("/api/voices/current")
async def voices_save_current(request: Request):
    """Save whatever voice the bot is configured with right now."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    try:
        lib = _library()
        profile = lib.save_from_current(data.get("name"), CONFIG,
                                        tags=data.get("tags"), notes=data.get("notes"))
    except Exception as e:                                    # noqa: BLE001
        return _voice_error(e)
    return {"ok": True, "voice": profile.to_dict()}


@router.get("/api/voices/{vid}")
def voices_detail(vid: str):
    from agent.voice_library import get_library  # noqa: F401  (keeps import local)

    lib = _library()
    profile = lib.find(vid)
    if profile is None:
        return JSONResponse({"error": f"unknown voice '{vid}'"}, status_code=404)
    item = profile.to_dict()
    item["active"] = lib.active_id() == vid
    item["ref_path"] = lib.ref_path(vid)
    return {"ok": True, "voice": item}


@router.put("/api/voices/{vid}")
async def voices_update(vid: str, request: Request):
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    try:
        lib = _library()
        profile = lib.update(vid, **data)
    except Exception as e:                                    # noqa: BLE001
        return _voice_error(e)
    return {"ok": True, "voice": profile.to_dict()}


@router.delete("/api/voices/{vid}")
def voices_delete(vid: str):
    lib = _library()
    if not lib.delete(vid):
        return JSONResponse({"error": f"unknown voice '{vid}'"}, status_code=404)
    return {"ok": True, "deleted": vid}


@router.post("/api/voices/{vid}/apply")
async def voices_apply(vid: str):
    """Make this voice the one the bot speaks with."""
    from agent.tts import TTS

    try:
        lib = _library()
        applied = lib.apply(vid, CONFIG)
    except Exception as e:                                    # noqa: BLE001
        return _voice_error(e)
    # An explicit operator choice deserves a fresh attempt at the engine.
    TTS.reset_health()
    return {"ok": True, "applied": applied, "active_id": vid,
            "voice": lib.get(vid).to_dict()}


@router.post("/api/voices/{vid}/preview")
async def voices_preview(vid: str, request: Request):
    """Speak a phrase with this voice and return it as wav audio.

    Falls back to the standby engine when the profile's own engine is not
    runnable here (no weights, no key), so the operator always hears something —
    the fallback is reported in the response headers.
    """
    body = {}
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            body = await request.json()
        except Exception:
            body = {}
    try:
        lib = _library()
        profile = lib.get(vid)
    except KeyError as e:
        return JSONResponse({"error": f"unknown voice '{e.args[0]}'"}, status_code=404)

    text = (body.get("text") or "").strip() or profile.preview_text or DEFAULT_PREVIEW_TEXT
    if body.get("include_tags") and profile.tags:
        text = f"{text} {' '.join(profile.tags)}"

    cfg = lib.preview_config(vid, CONFIG)
    return await _render_preview(cfg, text)


def _draft_config(data: dict):
    """Build a config for an unsaved voice definition (designer preview)."""
    from dataclasses import replace

    from agent.audio_dsp import AudioError
    from agent.voice_library import clean_tags, clean_tuning, validate_engine_language

    engine = (data.get("engine") or "chatterbox").strip().lower()
    if engine not in ("chatterbox", "kokoro", "piper"):
        raise AudioError("unknown engine '%s' — use one of: chatterbox, kokoro, piper"
                         % engine)
    overrides: dict = {"tts_provider": engine}
    tuning = clean_tuning(data.get("tuning") or {})
    if engine == "piper":
        # The offline neural engine: pick a resident voice and pass the studio
        # sliders through to it (pitch/speed are applied by the voice server).
        if data.get("voice"):
            overrides["piper_voice"] = str(data["voice"]).strip()
        if "speed" in tuning:
            overrides["piper_speed"] = float(tuning["speed"])
        if "pitch" in tuning:
            overrides["piper_pitch"] = int(tuning["pitch"])
        if "volume" in tuning:
            overrides["piper_volume"] = float(tuning["volume"])
    elif engine == "chatterbox":
        model = (data.get("model") or CONFIG.chatterbox_model or "turbo").strip()
        language = (data.get("language") or "en").strip().lower()
        validate_engine_language(engine, model, language)
        overrides.update({"chatterbox_model": model, "chatterbox_language": language})
        for key in ("exaggeration", "cfg_weight", "temperature"):
            if key in tuning:
                overrides[f"chatterbox_{key}"] = float(tuning[key])
    else:
        if data.get("voice"):
            overrides["kokoro_voice"] = str(data["voice"]).strip()
        if "speed" in tuning:
            overrides["kokoro_speed"] = float(tuning["speed"])
        if "pitch" in tuning:
            overrides["kokoro_pitch"] = int(tuning["pitch"])
    return replace(CONFIG, **overrides), tuning, clean_tags(data.get("tags"))


async def _render_preview(cfg, text: str):
    """Speak `text` with `cfg`, falling back to the standby engine if it fails.

    The operator must always hear something: if the profile's own engine cannot
    run on this machine (no weights, no key) the standby speaks instead and the
    response headers say which engine and whether it was a fallback.
    """
    from dataclasses import replace

    from agent.tts import LOCAL_HTTP_PROVIDERS, TTS, wav_from_pcm16

    #: what the *profile* asked for, kept separate from the engine a given attempt
    #: uses — the operator cares "I asked ChatterBox, piper spoke", not the internals
    requested_engine = str(cfg.tts_provider)
    attempts = [("profile", cfg)]
    # Order of the fallbacks: the configured standby first, then any other local
    # server that is actually answering. That last part matters because the
    # standby *can* be the same engine the profile asks for (apply() swaps the two
    # slots around), and a machine without ChatterBox weights or an API key should
    # still let the operator hear the voice — labelled as a fallback, never passed
    # off as the profile's own engine.
    order: "list[str]" = []
    standby = (CONFIG.tts_standby_provider or "").strip().lower()
    if standby:
        order.append(standby)
    order.extend(name for name in LOCAL_HTTP_PROVIDERS if name not in order)
    seen = {cfg.tts_provider}
    for name in order:
        if not name or name in seen:
            continue
        seen.add(name)
        if name in LOCAL_HTTP_PROVIDERS:
            try:
                if not TTS(replace(cfg, tts_provider=name)).probe_local(name):
                    continue             # not running: do not make the user wait on it
            except Exception:            # noqa: BLE001 - a probe must never block a preview
                pass
        attempts.append(("standby", replace(cfg, tts_provider=name)))
    problems = []
    for label, attempt_cfg in attempts:
        try:
            tts = TTS(attempt_cfg)
            audio = b"".join([c async for c in tts.synthesize_stream_pcm(text)])
            if not audio:
                raise RuntimeError("the engine returned no audio")
        except Exception as e:                                  # noqa: BLE001
            problems.append(f"{attempt_cfg.tts_provider}: {e}")
            continue
        # The engine has its own failover chain, so the engine that *actually*
        # spoke can differ from the one we asked for. Report the truth rather
        # than labelling standby audio as the profile's own voice.
        used = tts.active
        # Prefer what the server itself reported (X-Voice-Engine): the provider
        # nicknames "kokoro" and "piper" can point at the same URL, and only the
        # server knows which model actually produced the audio.
        reported = (getattr(tts, "engine_reported", "") or "").strip() or str(used)
        reason = (tts.status().get("failover_reason") or "")[:180]
        mode = label if used == attempt_cfg.tts_provider else "standby"
        headers = {"X-Voice-Engine": reported, "X-Voice-Mode": mode,
                   "X-Voice-Requested": requested_engine,
                   "X-Voice-Fallback": "0" if mode == "profile" else "1"}
        model = (getattr(tts, "model_reported", "") or "").strip()
        if model:
            headers["X-Voice-Model"] = model
        if reason:
            headers["X-Voice-Note"] = "".join(c if 32 <= ord(c) < 127 else " "
                                              for c in reason)
        return Response(content=wav_from_pcm16(audio), media_type="audio/wav",
                        headers=headers)
    return JSONResponse(
        {"error": "could not render this voice: " + " | ".join(problems),
         "hint": "install the engine (pip install chatterbox-tts) or set "
                 "TTS_STANDBY_PROVIDER to a running local voice server"},
        status_code=502)


@router.post("/api/voices/preview-draft")
async def voices_preview_draft(request: Request):
    """Preview a voice that has not been saved yet (the designer's preview button)."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    text = (data.get("text") or "").strip() or DEFAULT_PREVIEW_TEXT
    try:
        cfg, _tuning, tags = _draft_config(data)
    except Exception as e:                                        # noqa: BLE001
        return _voice_error(e)
    if data.get("include_tags") and tags:
        text = f"{text} {' '.join(tags)}"
    return await _render_preview(cfg, text)


