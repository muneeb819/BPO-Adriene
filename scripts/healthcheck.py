"""Readiness report for the BPO Adriene voice fronter.

Run it any time to see whether the project is ready to take calls:

    python scripts/healthcheck.py            # everything (offline + live if up)
    python scripts/healthcheck.py --offline  # skip the live server checks
    python scripts/healthcheck.py --url http://localhost:9000

Exit code 0 = ready, 1 = something must be fixed.
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

PASS, FAIL, NOTE = "PASS", "FAIL", "NOTE"
_results: list[tuple[str, str, str]] = []


def check(status: str, label: str, detail: str = "") -> bool:
    _results.append((status, label, detail))
    return status != FAIL


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
        except Exception as e:
            check(FAIL, f"dependency: {mod}", str(e))

    for mod in ("elevenlabs", "pytest"):
        try:
            __import__(mod)
            check(PASS, f"optional: {mod}")
        except Exception:
            check(NOTE, f"optional: {mod}", "not installed (voice/tests unavailable)")


# ---------------------------------------------------------------------------
# 2. Configuration
# ---------------------------------------------------------------------------

def check_config() -> None:
    env_path = os.path.join(ROOT, ".env")
    if not os.path.isfile(env_path):
        check(NOTE, ".env", "missing — defaults will be used (copy .env.example)")
        return
    check(PASS, ".env", "found")

    from agent.config import Config

    cfg = Config.load()

    # Voice keys — required for real streaming speech.
    check(PASS if cfg.elevenlabs_api_key and not cfg.elevenlabs_api_key.startswith("PASTE")
          else FAIL,
          "ElevenLabs TTS key", "set" if cfg.elevenlabs_api_key else "MISSING — no voice output")
    check(PASS if cfg.deepgram_api_key and not cfg.deepgram_api_key.startswith("PASTE")
          else FAIL,
          "Deepgram STT key", "set" if cfg.deepgram_api_key else "MISSING — no speech input")

    # LLM key — intentionally optional: the bot runs on compliance script lines.
    llm_keys = {
        "gemini": cfg.gemini_api_key,
        "openai": cfg.openai_api_key,
        "anthropic": cfg.anthropic_api_key,
    }
    active = llm_keys.get(cfg.llm_provider, "")
    placeholder = (not active) or active.startswith("PASTE")
    if placeholder:
        check(NOTE, "LLM API key",
              f"intentionally BLANK (provider={cfg.llm_provider}) — bot speaks the "
              "compliance script; paste a key to enable natural, varied wording")
    else:
        check(PASS, "LLM API key", f"set (provider={cfg.llm_provider})")

    check(PASS, "port", str(cfg.port))
    check(PASS, "agent / brand", f"{cfg.agent_name} @ {cfg.business_name}")


# ---------------------------------------------------------------------------
# 3. Campaigns + engine
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
              (disclosure[:70] + "...") if disclosure and len(disclosure) > 70 else (disclosure or "none configured"))

        ages = active.compliance()
        check(PASS if ages.get("min_age") and ages.get("max_age") else NOTE,
              "age compliance range",
              f"{ages.get('min_age')}-{ages.get('max_age')}" if ages.get("min_age") else "not set")
    except Exception as e:
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
              f"first question asked: {asked.ai_speech!r}" if ok else "did not ask the first question")
    except Exception as e:
        check(FAIL, "gate engine", f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# 4. Data files
# ---------------------------------------------------------------------------

def check_data() -> None:
    from agent.recorder import list_recordings

    rec_dir = os.path.join(ROOT, "data", "recordings")
    recs = list_recordings(rec_dir)
    check(PASS, "call recordings", f"{len(recs)} saved (data/recordings)")

    for name in ("campaigns.json", "memory.json", "call_log.json"):
        path = os.path.join(ROOT, "data", name)
        if os.path.isfile(path):
            check(PASS, f"data/{name}", f"{os.path.getsize(path):,} bytes")
        else:
            check(NOTE, f"data/{name}", "will be created on first run")

    tests = os.path.join(ROOT, "tests")
    n = len([f for f in os.listdir(tests) if f.startswith("test_")]) if os.path.isdir(tests) else 0
    check(PASS if n else NOTE, "test suite", f"{n} test module(s) — run: python -m pytest tests/ -q")


# ---------------------------------------------------------------------------
# 5. Live server
# ---------------------------------------------------------------------------

def check_live(url: str) -> None:
    import json as _json
    import urllib.error
    import urllib.request

    def get(path: str, timeout: float = 3.0):
        with urllib.request.urlopen(url.rstrip("/") + path, timeout=timeout) as r:
            raw = r.read().decode("utf-8-sig")
            return _json.loads(raw) if "json" in r.headers.get("Content-Type", "") else raw

    try:
        health = get("/health")
        check(PASS, "server /health", f"{health.get('status')} mode={health.get('mode')} "
                                      f"calls={health.get('calls')}")
    except Exception as e:
        check(NOTE, "server /health", f"not running ({e.__class__.__name__}) — start it with start.ps1")
        return

    for path, label in (
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
            body = get(path)
            size = len(body) if isinstance(body, str) else len(str(body))
            check(PASS if size > 0 else FAIL, label, f"HTTP 200, {size:,} chars")
        except urllib.error.HTTPError as e:
            check(FAIL, label, f"HTTP {e.code}")
        except Exception as e:
            check(FAIL, label, str(e))


# ---------------------------------------------------------------------------

def _output_setup() -> tuple[dict[str, str], bool]:
    """Pick glyphs the console can actually encode, and enable colour safely.

    Windows consoles default to cp1252, which cannot encode checkmarks.
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    enc = getattr(sys.stdout, "encoding", None) or "ascii"
    glyphs = {"pass": "\u2713", "fail": "\u2717", "note": "\u25cb"}
    for glyph in glyphs.values():
        try:
            glyph.encode(enc)
        except Exception:
            glyphs = {"pass": "[OK]", "fail": "[XX]", "note": "[--]"}
            break
    ansi = bool(os.environ.get("WT_SESSION") or os.environ.get("TERM")
                or os.environ.get("ANSICON") or os.environ.get("FORCE_COLOR"))
    return glyphs, ansi and sys.stdout.isatty()


def main() -> int:
    ap = argparse.ArgumentParser(description="Readiness report for the voice fronter.")
    ap.add_argument("--url", default="http://localhost:9000", help="server base URL")
    ap.add_argument("--offline", action="store_true", help="skip live server checks")
    args = ap.parse_args()

    glyphs, use_color = _output_setup()

    print()
    print("=" * 74)
    print("  BPO Adriene - AI Voice Fronter :: readiness report")
    print("=" * 74)

    check_runtime()
    check_config()
    check_campaigns()
    check_data()
    if not args.offline:
        check_live(args.url)

    print()
    colors = {PASS: "\033[92m", FAIL: "\033[91m", NOTE: "\033[93m"}
    keys = {PASS: "pass", FAIL: "fail", NOTE: "note"}
    for status, label, detail in _results:
        head = f"{colors[status]}{glyphs[keys[status]]} {status:<4}\033[0m" if use_color \
            else f"{glyphs[keys[status]]} {status:<4}"
        line = f"{head}  {label}"
        if detail:
            line += f"  \u2014  {detail}"
        print(line)

    fails = [r for r in _results if r[0] == FAIL]
    notes = [r for r in _results if r[0] == NOTE]
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
