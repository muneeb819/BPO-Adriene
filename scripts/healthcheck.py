#!/usr/bin/env python3
"""Readiness report for the BPO Adriene voice fronter.

Run it any time — before a deployment, after a change, or when something is
quiet — to see whether this machine can actually take calls:

    python scripts/healthcheck.py                 # everything (live checks if up)
    python scripts/healthcheck.py --offline       # skip the live-server checks
    python scripts/healthcheck.py --url http://192.168.1.10:8000
    python scripts/healthcheck.py --json          # machine-readable summary

It checks the things that actually break a deployment: the interpreter and
packages, `.env`, the **voice engine** (models on disk, server answering, and a
real, non-silent render), the campaign/gate engine, the data volume, and — when
the app is running — its pages and JSON APIs.

Exit code 0 = ready, 1 = something must be fixed.
"""
from __future__ import annotations

import argparse
import json
import os
import struct
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

PASS, FAIL, NOTE = "PASS", "FAIL", "NOTE"
_results: list[tuple[str, str, str]] = []


def check(status: str, label: str, detail: str = "") -> bool:
    _results.append((status, label, detail))
    return status != FAIL


def _get(url: str, timeout: float = 3.0):
    """GET a JSON endpoint (tolerates a UTF-8 BOM, which uvicorn sometimes adds)."""
    with urllib.request.urlopen(url, timeout=timeout) as r:
        raw = r.read().decode("utf-8-sig")
        kind = r.headers.get("Content-Type", "")
        return json.loads(raw) if "json" in kind else raw


def _post_json(url: str, payload: dict, timeout: float = 30.0) -> bytes:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _audio_stats(blob: bytes) -> tuple[float, int, float]:
    """(seconds, peak, rms) of a wav — or of raw 16-bit PCM if it has no header."""
    import io
    import wave

    try:
        with wave.open(io.BytesIO(blob)) as w:
            frames = w.readframes(w.getnframes())
            rate, width = w.getframerate(), w.getsampwidth()
        seconds = len(frames) / max(1, width * max(1, rate))
    except Exception:
        frames, rate, width = blob, 16000, 2
        seconds = len(frames) / (2 * rate)
    if width != 2 or len(frames) < 4:
        return seconds, 0, 0.0
    n = len(frames) // 2
    samples = struct.unpack("<%dh" % n, frames[: n * 2])
    peak = max((abs(v) for v in samples), default=0)
    rms = (sum(v * v for v in samples) / max(1, n)) ** 0.5
    return seconds, peak, rms


# ---------------------------------------------------------------------------
# 1. Runtime
# ---------------------------------------------------------------------------

def check_runtime() -> None:
    v = sys.version_info
    check(PASS if v >= (3, 10) else FAIL,
          "Python version", f"{v.major}.{v.minor}.{v.micro} (need >= 3.10)")

    for mod in ("fastapi", "uvicorn", "websockets", "dotenv", "requests"):
        try:
            __import__(mod)
            check(PASS, f"dependency: {mod}")
        except Exception as e:                                   # noqa: BLE001
            check(FAIL, f"dependency: {mod}",
                  f"{e} — run: python scripts/setup_local.py")

    for mod, why in (("elevenlabs", "cloud voices / Voice Design"),
                     ("soundfile", "mp3/flac/ogg uploads in the studio"),
                     ("pytest", "the test suite")):
        try:
            __import__(mod)
            check(PASS, f"optional: {mod}")
        except Exception:                                        # noqa: BLE001
            check(NOTE, f"optional: {mod}", f"not installed ({why} unavailable)")


# ---------------------------------------------------------------------------
# 2. Configuration
# ---------------------------------------------------------------------------

def _env_file_present() -> bool:
    return os.path.isfile(os.path.join(ROOT, ".env"))


def check_config():
    """Configuration sanity. Returns the loaded Config (or None)."""
    if not _env_file_present():
        check(NOTE, ".env",
              "missing — built-in defaults are used (copy .env.example to .env)")
    else:
        check(PASS, ".env", "found")

    try:
        from agent.config import Config

        cfg = Config.load()
    except Exception as e:                                       # noqa: BLE001
        check(FAIL, "configuration", f"{type(e).__name__}: {e}")
        return None

    check(PASS, "agent / brand", f"{cfg.agent_name} @ {cfg.business_name}")
    chain = cfg.tts_provider or "console"
    if cfg.tts_standby_provider:
        chain += f" → {cfg.tts_standby_provider}"
    check(PASS, "voice chain", chain)
    check(PASS, "listening on", f"{cfg.host}:{cfg.port}")
    if cfg.host not in ("127.0.0.1", "localhost") and not cfg.web_password:
        check(NOTE, "dashboard password",
              "HOST is reachable from the network and WEB_PASSWORD is empty — "
              "anyone who can reach the port can control the bot")
    else:
        check(PASS, "dashboard password",
              "set" if cfg.web_password else "not needed (bound to localhost)")
    if (os.environ.get("DEBUG") or "").lower() in ("1", "true", "yes"):
        check(NOTE, "DEBUG mode", "on — the dashboard shows a DEBUG badge and the "
                                  "console runs a demo call; set DEBUG=false on a live server")
    else:
        check(PASS, "DEBUG mode", "off (production)")

    # Keys are only required by the providers you actually selected.
    wants_cloud_voice = cfg.tts_provider == "elevenlabs"
    has_key = bool(cfg.elevenlabs_api_key and not cfg.elevenlabs_api_key.startswith("PASTE"))
    if wants_cloud_voice:
        check(PASS if has_key else FAIL, "ElevenLabs TTS key",
              "set" if has_key else
              "MISSING — TTS_PROVIDER=elevenlabs needs ELEVENLABS_API_KEY "
              "(or switch to the local engine: TTS_PROVIDER=piper)")
    else:
        check(PASS if has_key else NOTE, "ElevenLabs key (optional)",
              "set — cloud voices and Voice Design are available" if has_key
              else "not set — local neural voices are used (no key needed)")

    stt = (cfg.stt_provider or "console").lower()
    has_stt_key = bool(cfg.deepgram_api_key and not cfg.deepgram_api_key.startswith("PASTE"))
    if stt == "deepgram":
        check(PASS if has_stt_key else FAIL, "Deepgram STT key",
              "set" if has_stt_key else
              "MISSING — live calls cannot be transcribed without it")
    else:
        check(NOTE, "speech-to-text",
              f"STT_PROVIDER={stt} — typed input only. Real phone calls need "
              "STT_PROVIDER=deepgram plus DEEPGRAM_API_KEY")

    llm_keys = {"gemini": cfg.gemini_api_key, "openai": cfg.openai_api_key,
                "anthropic": cfg.anthropic_api_key}
    active = llm_keys.get(cfg.llm_provider, "")
    if (not active) or active.startswith("PASTE"):
        check(NOTE, "LLM API key",
              f"blank (provider={cfg.llm_provider}) — the bot speaks the campaign's "
              "compliance lines; add a key for natural wording")
    else:
        check(PASS, "LLM API key", f"set (provider={cfg.llm_provider})")
    return cfg


# ---------------------------------------------------------------------------
# 3. Voice engine — models on disk, server up, and a real render
# ---------------------------------------------------------------------------

def _models_dir() -> str:
    return os.path.join(ROOT, "data", "models", "piper")


def check_voices(cfg, offline: bool) -> None:
    """The part that decides whether callers hear anything at all."""
    from agent.tts import LOCAL_HTTP_PROVIDERS

    primary = (getattr(cfg, "tts_provider", "") or "console").strip().lower()
    standby = (getattr(cfg, "tts_standby_provider", "") or "").strip().lower()
    local_engine = primary in LOCAL_HTTP_PROVIDERS or standby in LOCAL_HTTP_PROVIDERS

    # --- models on disk ---
    voices = []
    mdir = _models_dir()
    if os.path.isdir(mdir):
        for name in sorted(os.listdir(mdir)):
            if name.endswith(".onnx"):
                model = os.path.join(mdir, name)
                conf = model + ".json"
                voices.append((name[:-5], os.path.getsize(model), os.path.isfile(conf)))
    total_mb = sum(v[1] for v in voices) / 1e6
    if voices:
        missing_conf = [v[0] for v in voices if not v[2]]
        check(PASS if not missing_conf else NOTE, "local neural voices",
              f"{len(voices)} installed ({total_mb:.0f} MB): "
              + ", ".join(v[0] for v in voices)
              + (f" — no .json config for {', '.join(missing_conf)}" if missing_conf else ""))
    elif local_engine:
        check(FAIL, "local neural voices",
              "TTS_PROVIDER uses a local engine but data/models/piper is empty — "
              "run: python scripts/fetch_neural_voices.py")
    else:
        check(NOTE, "local neural voices",
              "not installed — run: python scripts/fetch_neural_voices.py")

    # --- engine packages ---
    if local_engine:
        for mod, why in (("piper", "rendering the voices"),
                         ("onnxruntime", "running the ONNX models")):
            try:
                __import__(mod)
                check(PASS, f"voice engine: {mod}")
            except Exception as e:                               # noqa: BLE001
                check(FAIL, f"voice engine: {mod}",
                      f"{e} — run: python scripts/setup_local.py")

    # --- the local server, and a real render through it ---
    if local_engine and not offline:
        base = (getattr(cfg, "piper_base_url", "")
                or getattr(cfg, "kokoro_base_url", "") or "http://127.0.0.1:8880")
        base = base.rstrip("/")
        try:
            health = _get(base + "/health", timeout=3.0)
            check(PASS, "voice server /health",
                  f"{base} — engine={health.get('engine') or health.get('provider')}, "
                  f"voices={health.get('voices', len(health.get('models') or []))}, "
                  f"loaded={len(health.get('loaded') or [])}")
            _render_check(base, cfg, health)
        except Exception as e:                                   # noqa: BLE001
            check(FAIL, "voice server",
                  f"{base} not answering ({e.__class__.__name__}) — start it with: "
                  "python scripts/neural_tts_server.py --port 8880 --warm "
                  "(start.ps1 / start.sh do this for you)")

    # --- is the configured standby actually usable? ---
    if standby:
        if standby == "elevenlabs":
            ok = bool(getattr(cfg, "elevenlabs_api_key", "").strip())
            check(PASS if ok else NOTE, "standby engine",
                  "elevenlabs: key set" if ok else
                  "standby is elevenlabs but no ELEVENLABS_API_KEY is set — "
                  "failover would have nothing to fall back to")
        elif standby in LOCAL_HTTP_PROVIDERS:
            base = ((getattr(cfg, "piper_base_url", "") or getattr(cfg, "kokoro_base_url", ""))
                    or "http://127.0.0.1:8880").rstrip("/")
            up = False
            if not offline:
                try:
                    _get(base + "/health", timeout=3.0)
                    up = True
                except Exception:                                # noqa: BLE001
                    up = False
            check(PASS if (up or offline) else NOTE, "standby engine",
                  f"{standby}: local server at {base}" if up else
                  f"standby is {standby} but nothing answers on {base}")
        elif standby == "chatterbox":
            try:
                __import__("chatterbox")
                check(PASS, "standby engine", "chatterbox: installed")
            except Exception:                                    # noqa: BLE001
                check(NOTE, "standby engine",
                      "standby is chatterbox, which is not installed on this machine — "
                      "a piper outage would have nothing to fall back to "
                      "(set TTS_STANDBY_PROVIDER= or install chatterbox-tts)")
        else:
            check(NOTE, "standby engine",
                  f"{standby}: not a local engine — assuming it is configured elsewhere")

    # --- the studio library ---
    try:
        from agent.voice_library import get_library

        lib = get_library(getattr(cfg, "voice_library_dir", "data/voices"))
        active = lib.active_id()
        profile = lib.find(active) if active else None
        if profile is None:
            check(NOTE, "voice library",
                  "no voice selected yet — pick one in the dashboard's Voice studio")
        else:
            check(PASS, "voice library",
                  f"active: {profile.name} ({profile.engine}"
                  + (f"/{profile.voice}" if profile.voice else "") + ")")
    except Exception as e:                                       # noqa: BLE001
        check(NOTE, "voice library", f"not readable yet ({e.__class__.__name__})")


def _render_check(base: str, cfg, health: dict) -> None:
    """Speak a real line and measure it — the only proof audio reaches a caller."""
    voices = (health.get("loaded") or health.get("models")
              or [v.get("id") for v in (health.get("voice_details") or [])
                  if isinstance(v, dict)])
    voice = (getattr(cfg, "piper_voice", "") or "").strip() or (voices[0] if voices else "")
    payload = {"input": "Health check. Testing one two three.",
               "voice": voice, "response_format": "wav"}
    for key, attr in (("speed", "piper_speed"), ("pitch", "piper_pitch"),
                      ("volume", "piper_volume")):
        value = getattr(cfg, attr, None)
        if value not in (None, "", 0) and not (key == "pitch" and value == 50):
            payload[key] = value
        elif key == "pitch" and value == 50:
            payload[key] = 50
    try:
        blob = _post_json(base + "/v1/audio/speech", payload, timeout=60.0)
    except Exception as e:                                       # noqa: BLE001
        check(FAIL, "voice render", f"{type(e).__name__}: {e}")
        return
    seconds, peak, rms = _audio_stats(blob)
    if seconds < 0.3 or peak < 500:
        check(FAIL, "voice render",
              f"silent or empty audio ({len(blob)} bytes, {seconds:.2f}s, peak {peak}) "
              f"— voice={voice!r}")
    else:
        check(PASS, "voice render",
              f"{seconds:.2f}s, peak {peak}, rms {rms:.0f} — voice={voice!r} "
              f"({len(blob) / 1024:.0f} KB)")


# ---------------------------------------------------------------------------
# 4. Campaigns + gate engine
# ---------------------------------------------------------------------------

def check_campaigns() -> None:
    try:
        from agent.campaign import DEFAULTS, get_store

        store = get_store()
        campaigns = store.list()
        check(PASS if len(campaigns) >= len(DEFAULTS) else FAIL,
              "campaign store", f"{len(campaigns)} campaign(s): "
              + ", ".join(c["id"] for c in campaigns))

        active = store.active()
        check(PASS, "active campaign", f"{active.id} — {active.name}")

        disclosure = active.compliance().get("recording_disclosure")
        check(PASS if disclosure else NOTE, "recording disclosure",
              (disclosure[:70] + "...") if disclosure and len(disclosure) > 70
              else (disclosure or "none configured"))

        ages = active.compliance()
        check(PASS if ages.get("min_age") and ages.get("max_age") else NOTE,
              "age compliance range",
              f"{ages.get('min_age')}-{ages.get('max_age')}" if ages.get("min_age")
              else "not set")
    except Exception as e:                                       # noqa: BLE001
        check(FAIL, "campaign store", str(e))
        return

    # Drive the real gate end-to-end (no network, no LLM needed).
    try:
        from agent.config import Config
        from agent.conversation import Conversation
        from agent.mock_llm import MockLLM

        c = Conversation(Config.load(), MockLLM(Config.load()), campaign=active)
        c.start()
        asked = c.hear("Sure, hello there")
        ok = asked.ai_speech and c.ctx.q_index == 0
        check(PASS if ok else FAIL, "gate engine",
              f"first question asked: {asked.ai_speech!r}" if ok
              else "did not ask the first question")
    except Exception as e:                                       # noqa: BLE001
        check(FAIL, "gate engine", f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# 5. Data volume
# ---------------------------------------------------------------------------

def check_data(cfg=None) -> None:
    from agent.recorder import list_recordings

    data_dir = os.path.join(ROOT, (getattr(cfg, "data_dir", "") or "data"))
    rec_dir = os.path.join(data_dir, "recordings")
    recs = list_recordings(rec_dir)
    check(PASS, "call recordings", f"{len(recs)} saved ({os.path.relpath(rec_dir, ROOT)})")

    for name in ("campaigns.json", "memory.json", "call_log.json"):
        path = os.path.join(data_dir, name)
        if os.path.isfile(path):
            check(PASS, f"data/{name}", f"{os.path.getsize(path):,} bytes")
        else:
            check(NOTE, f"data/{name}", "will be created on first run")

    # writable? (a read-only checkout breaks recordings and the voice library)
    probe = os.path.join(data_dir, ".healthcheck.tmp")
    try:
        os.makedirs(data_dir, exist_ok=True)
        with open(probe, "w") as fh:
            fh.write("ok")
        os.remove(probe)
        check(PASS, "data directory", f"{data_dir} is writable")
    except Exception as e:                                       # noqa: BLE001
        check(FAIL, "data directory", f"{data_dir} is not writable ({e})")

    # disk space (calls are written here; 1 GB is a comfortable floor)
    try:
        free_gb = __import__("shutil").disk_usage(data_dir).free / 1e9
        check(PASS if free_gb >= 1 else NOTE, "free disk space",
              f"{free_gb:.1f} GB available for recordings")
    except Exception:                                            # noqa: BLE001
        pass

    tests = os.path.join(ROOT, "tests")
    n = len([f for f in os.listdir(tests) if f.startswith("test_")]) if os.path.isdir(tests) else 0
    check(PASS if n else NOTE, "test suite",
          f"{n} test module(s) — run: python -m pytest tests/ -q")


# ---------------------------------------------------------------------------
# 6. Live server
# ---------------------------------------------------------------------------

def check_live(url: str) -> None:
    base = url.rstrip("/")
    try:
        health = _get(base + "/health")
        check(PASS, "server /health", f"{health.get('status')} mode={health.get('mode')} "
                                      f"calls={health.get('calls')}")
    except Exception as e:                                       # noqa: BLE001
        check(NOTE, "server /health",
              f"not running ({e.__class__.__name__}) — start it with start.ps1 / start.sh")
        return

    engines = health.get("tts_engines") or {}
    if engines:
        up = [name for name, info in engines.items()
              if isinstance(info, dict) and info.get("reachable")]
        check(PASS if up else NOTE, "server voice engines",
              ", ".join(f"{name}={'up' if name in up else 'down'}" for name in engines))

    for path, label in (
        ("/api/tts/voices", "server voice state"),
        ("/api/voices", "server voice studio"),
        ("/api/campaigns", "server campaigns API"),
        ("/api/recordings", "server recordings API"),
        ("/api/insights", "server insights API"),
        ("/api/export/calls", "server call-log export"),
        ("/api/export/transcripts", "server transcript export"),
        ("/api/export/insights", "server insights export"),
        ("/", "dashboard page"),
        ("/campaigns", "campaign editor page"),
        ("/debug", "voice console page"),
    ):
        try:
            body = _get(base + path)
            size = len(body) if isinstance(body, str) else len(str(body))
            check(PASS if size > 0 else FAIL, label, f"HTTP 200, {size:,} chars")
        except urllib.error.HTTPError as e:
            check(FAIL, label, f"HTTP {e.code}")
        except Exception as e:                                   # noqa: BLE001
            check(FAIL, label, str(e))


# ---------------------------------------------------------------------------

def _output_setup() -> tuple[dict[str, str], bool]:
    """Pick glyphs the console can actually encode, and enable colour safely.

    Windows consoles default to cp1252, which cannot encode checkmarks.
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                            # noqa: BLE001
        pass
    enc = getattr(sys.stdout, "encoding", None) or "ascii"
    glyphs = {"pass": "\u2713", "fail": "\u2717", "note": "\u25cb"}
    for glyph in glyphs.values():
        try:
            glyph.encode(enc)
        except Exception:                                        # noqa: BLE001
            glyphs = {"pass": "[OK]", "fail": "[XX]", "note": "[--]"}
            break
    ansi = bool(os.environ.get("WT_SESSION") or os.environ.get("TERM")
                or os.environ.get("ANSICON") or os.environ.get("FORCE_COLOR"))
    return glyphs, ansi and sys.stdout.isatty()


def _default_url() -> str:
    """Probe the port the project is actually configured to serve on.

    Hard-coding 9000 made the report claim "server not running" for anyone on
    the default PORT=8000. Follow .env / the environment instead.
    """
    try:
        from agent.config import Config

        return f"http://localhost:{Config.load().port}"
    except Exception:                                            # noqa: BLE001
        return "http://localhost:8000"


def main() -> int:
    ap = argparse.ArgumentParser(description="Readiness report for the voice fronter.")
    ap.add_argument("--url", default=_default_url(),
                    help="server base URL (default: the configured PORT)")
    ap.add_argument("--offline", action="store_true", help="skip live server checks")
    ap.add_argument("--json", action="store_true",
                    help="print a machine-readable summary instead of the report")
    args = ap.parse_args()

    check_runtime()
    cfg = check_config()
    if cfg is None:
        from agent.config import Config

        cfg = Config()
    check_voices(cfg, offline=args.offline)
    check_campaigns()
    check_data(cfg)
    if not args.offline:
        check_live(args.url)

    fails = [r for r in _results if r[0] == FAIL]
    notes = [r for r in _results if r[0] == NOTE]

    if args.json:
        print(json.dumps({
            "ready": not fails,
            "checks": len(_results),
            "failures": [{"label": label, "detail": detail} for _, label, detail in fails],
            "notes": [{"label": label, "detail": detail} for _, label, detail in notes],
            "results": [{"status": s, "label": label, "detail": detail}
                        for s, label, detail in _results],
        }, indent=2))
        return 1 if fails else 0

    glyphs, use_color = _output_setup()

    print()
    print("=" * 74)
    print("  BPO Adriene - AI Voice Fronter :: readiness report")
    print("=" * 74)

    colors = {PASS: "\033[92m", FAIL: "\033[91m", NOTE: "\033[93m"}
    keys = {PASS: "pass", FAIL: "fail", NOTE: "note"}
    for status, label, detail in _results:
        head = f"{colors[status]}{glyphs[keys[status]]} {status:<4}\033[0m" if use_color \
            else f"{glyphs[keys[status]]} {status:<4}"
        line = f"{head}  {label}"
        if detail:
            line += f"  \u2014  {detail}"
        print(line)

    print()
    print("-" * 74)
    if fails:
        print(f"  NOT READY: {len(fails)} issue(s) must be fixed.")
        for _, label, detail in fails:
            print(f"    - {label}: {detail}")
        code = 1
    else:
        print(f"  READY \u2014 {len(_results)} checks passed, {len(notes)} note(s).")
        code = 0
    print("-" * 74)
    print()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
