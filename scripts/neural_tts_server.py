#!/usr/bin/env python3
"""Offline **neural** TTS server — OpenAI/Kokoro-compatible wire format.

Same wire format as the app's other local engines, but the audio comes from real
Piper ONNX voices rather than a formant synthesiser, so a clone or a remix can
actually be judged by ear.

    python scripts/neural_tts_server.py --port 8880
    curl -s localhost:8880/health | python -m json.tool

Endpoints
    GET  /health                 engine, installed voices, what is loaded
    GET  /v1/audio/voices        {"voices": [...], "voice_details": [...]}
    POST /v1/audio/speech        {"input","voice","response_format","speed","pitch","volume"}
    POST /v1/audio/describe      {"description": "warm older British man, slow"}
                                 -> the local voice + tuning that description asks for
    POST /v1/audio/match         raw wav in the body (or ?path=) -> closest voice

``pitch`` is the studio's 0-99 scale (50 = the voice's own pitch, 0 = -6
semitones, 99 = +6) and the pace the pitch shift implies is cancelled inside the
model, so the speaker does not speed up when you raise the pitch.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import audio_dsp as dsp          # noqa: E402
from agent import neural_tts as nt          # noqa: E402

DEFAULT_PORT = 8880
_LOCK = threading.Lock()
_STATS = {"requests": 0, "errors": 0, "audio_seconds": 0.0}


class Handler(BaseHTTPRequestHandler):
    server_version = "BPOAdriene-NeuralTTS/1.0"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------------ utils
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, code: int, payload) -> None:
        self._send(code, json.dumps(payload, indent=2).encode("utf-8"), "application/json")

    def _error(self, code: int, message: str, hint: str = "") -> None:
        with _LOCK:
            _STATS["errors"] += 1
        self._json(code, {"error": {"message": message, "type": "invalid_request_error",
                                    "hint": hint}})

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw.decode("utf-8"))
            return parsed if isinstance(parsed, dict) else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}

    def log_message(self, fmt, *args):          # keep the console usable
        if os.environ.get("NEURAL_TTS_QUIET") != "1":
            sys.stderr.write("  [neural-tts] " + (fmt % args) + "\n")

    # ------------------------------------------------------------------- HTTP
    def do_OPTIONS(self) -> None:                # noqa: N802
        self._send(204, b"", "text/plain")

    def do_GET(self) -> None:                    # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path in ("/health", "/healthz", "/"):
            payload = nt.health()
            payload["stats"] = dict(_STATS)
            payload["status"] = "ok" if payload["available"] else "no-voices"
            self._json(200, payload)
            return
        if path in ("/v1/audio/voices", "/voices"):
            models = nt.list_models()
            self._json(200, {
                "voices": [m["id"] for m in models],
                "voice_details": [
                    {"id": m["id"], "name": m["name"], "language": m["language"],
                     "kind": m["kind"], "gender": m["kind"],   # alias the studio reads
                     "sample_rate": m["sample_rate"],
                     "num_speakers": m["num_speakers"],
                     "descriptors": {"engine": "piper", "neural": True,
                                     "size_mb": round(m["bytes"] / 1048576, 1)}}
                    for m in models
                ],
                "model": "piper", "engine": "piper",
            })
            return
        if path in ("/v1/models", "/models"):
            self._json(200, {"object": "list", "data": [
                {"id": m["id"], "object": "model", "owned_by": "local-piper"}
                for m in nt.list_models()]})
            return
        self._error(404, f"unknown path {self.path!r}",
                    "try /health, /v1/audio/voices or POST /v1/audio/speech")

    def do_POST(self) -> None:                   # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/")
        if path == "/v1/audio/speech":
            self._speech()
            return
        if path == "/v1/audio/describe":
            self._describe()
            return
        if path == "/v1/audio/match":
            self._match()
            return
        self._error(404, f"unknown path {self.path!r}",
                    "try POST /v1/audio/speech")

    # --------------------------------------------------------------- handlers
    def _speech(self) -> None:
        payload = self._body()
        text = str(payload.get("input") or payload.get("text") or "").strip()
        if not text:
            self._error(400, "no 'input' text was sent")
            return
        models = nt.list_models()
        if not models:
            self._error(503, "no neural voices are installed",
                        "run: python scripts/fetch_neural_voices.py")
            return
        requested = str(payload.get("voice") or payload.get("model") or "").strip()
        known = {m["id"]: m for m in models}
        aliases = {m["name"].lower(): m["id"] for m in models}
        voice = known.get(requested) or None
        if voice is None and requested:
            voice = known.get(aliases.get(requested.lower(), ""), None)
        if voice is None:                       # default: the middle register
            ordered = nt.describe_models()
            voice = ordered[len(ordered) // 2] if len(ordered) > 1 else ordered[0]
        model_id = voice["id"] if isinstance(voice, dict) else voice
        try:
            pitch = float(payload.get("pitch", nt.NEUTRAL_PITCH) or nt.NEUTRAL_PITCH)
            speed = float(payload.get("speed", 1.0) or 1.0)
            volume = float(payload.get("volume", 1.0) or 1.0)
            fmt = str(payload.get("response_format") or "wav").lower()
            rate = int(payload.get("sample_rate") or nt.PIPELINE_RATE)
            if fmt not in ("wav", "pcm"):
                self._error(400, f"response_format '{fmt}' is not supported",
                            "use 'wav' or 'pcm'")
                return
            audio = nt.synthesize(text, model_id, pitch_level=pitch, speed=speed,
                                  volume=volume, rate=rate, response_format=fmt)
        except nt.NeuralTTSError as exc:
            self._error(400, str(exc))
            return
        except Exception as exc:                 # pragma: no cover - defensive
            self._error(500, f"synthesis failed: {type(exc).__name__}: {exc}")
            return
        try:
            info = dsp.describe(*dsp.load_audio(audio))
        except Exception:
            info = {"duration_sec": 0.0}
        with _LOCK:
            _STATS["requests"] += 1
            _STATS["audio_seconds"] = round(_STATS["audio_seconds"] + info["duration_sec"], 2)
        self.send_response(200)
        self.send_header("Content-Type",
                         "audio/wav" if fmt == "wav" else "audio/pcm")
        self.send_header("Content-Length", str(len(audio)))
        self.send_header("X-Voice-Engine", "piper")
        self.send_header("X-Voice-Model", model_id)
        self.send_header("X-Audio-Seconds", f"{info['duration_sec']:.2f}")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(audio)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _describe(self) -> None:
        payload = self._body()
        description = str(payload.get("description") or payload.get("prompt") or "").strip()
        if not description:
            self._error(400, "no 'description' was sent",
                        'example: {"description": "warm older British man, slow"}')
            return
        try:
            plan = nt.design_from_text(
                description,
                language=payload.get("language"),
                pitch_level=payload.get("pitch"),
                speed=payload.get("speed"),
            )
        except nt.NeuralTTSError as exc:
            self._error(503, str(exc), "run: python scripts/fetch_neural_voices.py")
            return
        plan["description"] = description
        self._json(200, plan)

    def _match(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw:
            self._error(400, "send the reference wav in the request body")
            return
        try:
            samples, rate = dsp.load_audio(raw)
            samples, _ = dsp.prepare_reference(samples, rate)
            match = nt.match_reference(samples, dsp.TARGET_RATE)
        except Exception as exc:
            self._error(400, f"could not analyse that audio: {exc}")
            return
        self._json(200, match)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Offline neural TTS server (Piper)")
    parser.add_argument("--host", default=os.environ.get("NEURAL_TTS_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("NEURAL_TTS_PORT", DEFAULT_PORT)))
    parser.add_argument("--warm", action="store_true",
                        help="synthesise one probe sentence per voice before serving")
    args = parser.parse_args(argv)

    models = nt.list_models()
    print(f"neural TTS server (piper) on http://{args.host}:{args.port}")
    print(f"  models dir : {os.path.abspath(nt.models_dir())}")
    print(f"  python     : {sys.executable}")
    if not nt.piper_importable():
        print("  WARNING: piper is not installed -> every request will fail.")
        print("           pip install piper-tts onnxruntime")
    if not models:
        print("  WARNING: no voices installed -> every request will fail.")
        print("           python scripts/fetch_neural_voices.py")
    else:
        print(f"  voices     : {len(models)}")
        for model in models:
            print(f"    - {model['id']:26} {model['language']:8} {model['kind']:10} "
                  f"{model['bytes'] / 1048576:5.1f} MB")
    if args.warm:
        for model in models:
            try:
                stats = nt.characterize(model["id"])
                print(f"  measured   : {model['id']:26} {stats.get('f0_hz', 0):6.1f} Hz  "
                      f"{stats.get('brightness_hz', 0):6.0f} Hz centroid")
            except Exception as exc:
                print(f"  measured   : {model['id']:26} FAILED: {exc}")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    print("  ready — Ctrl-C to stop", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
