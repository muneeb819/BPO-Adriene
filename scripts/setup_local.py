#!/usr/bin/env python3
"""One-command setup for a fresh machine (Windows, Linux and macOS).

    python scripts/setup_local.py                 # everything, with voices
    python scripts/setup_local.py --no-voices     # skip the ~190 MB download
    python scripts/setup_local.py --no-install    # only .env + voices + report
    python scripts/setup_local.py --voice en_US-amy-low

What it does, in order:

1. checks the interpreter (>= 3.10) and creates `.venv` when it is missing
2. installs `requirements.txt` into that venv (`--no-install` skips this)
3. copies `.env.example` → `.env` when `.env` does not exist yet
4. downloads the local neural voices with `scripts/fetch_neural_voices.py`
   (skipped when they are already there, or with `--no-voices`)
5. runs `scripts/healthcheck.py --offline` and prints the exact next commands

It never touches an existing `.env`, never overwrites a downloaded voice, and is
safe to re-run after a `git pull` (it is how you upgrade).
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

IS_WINDOWS = os.name == "nt"
VENV = os.path.join(ROOT, ".venv")
VENV_PY = os.path.join(VENV, "Scripts" if IS_WINDOWS else "bin",
                       "python.exe" if IS_WINDOWS else "python")
MODELS = os.path.join(ROOT, "data", "models", "piper")

PASS, FAIL, STEP = "  ok  ", " fail ", " >>> "


def say(status: str, message: str) -> None:
    print(f"{status} {message}", flush=True)


def run(cmd: "list[str]", **kw) -> int:
    printable = " ".join(f'"{c}"' if " " in c else c for c in cmd)
    say(STEP, printable)
    try:
        return subprocess.call(cmd, cwd=ROOT, **kw)
    except FileNotFoundError:
        say(FAIL, f"could not run {cmd[0]!r} — is it on PATH?")
        return 127


def venv_python() -> str:
    """The interpreter to use for everything after bootstrapping."""
    if os.path.isfile(VENV_PY):
        return VENV_PY
    return sys.executable


def check_python() -> bool:
    v = sys.version_info
    if v < (3, 10):
        say(FAIL, f"Python {v.major}.{v.minor}.{v.micro} is too old — need 3.10+ "
                  "(3.12 recommended): https://www.python.org/downloads/")
        return False
    say(PASS, f"Python {v.major}.{v.minor}.{v.micro} at {sys.executable}")
    return True


def ensure_venv(*, install: bool) -> str:
    if os.path.isfile(VENV_PY):
        say(PASS, f"virtual environment already exists ({os.path.relpath(VENV, ROOT)})")
    else:
        if not install:
            say(FAIL, ".venv is missing and --no-install was given")
            return sys.executable
        if run([sys.executable, "-m", "venv", ".venv"]) != 0:
            say(FAIL, "could not create .venv — install Python 3.12 x64 and retry")
            return sys.executable
        say(PASS, "created .venv")

    py = venv_python()
    if install:
        if run([py, "-m", "pip", "install", "--upgrade", "pip", "--quiet"]) != 0:
            say(FAIL, "pip upgrade failed (continuing anyway)")
        if run([py, "-m", "pip", "install", "-r", "requirements.txt", "--quiet"]) != 0:
            say(FAIL, "dependency install failed — check the output above")
            return py
        say(PASS, "dependencies installed")

    # whatever is installed must be importable *by the venv*, not only by us
    probe = subprocess.run(
        [py, "-c", "import fastapi, uvicorn, piper, onnxruntime, soundfile; print('ok')"],
        cwd=ROOT, capture_output=True, text=True)
    if probe.returncode == 0:
        say(PASS, "core + voice packages import cleanly")
    else:
        say(FAIL, "import check failed: " + (probe.stderr.strip().splitlines() or [""])[-1])
        say(FAIL, "run: " + (".venv\\Scripts\\python.exe" if IS_WINDOWS
                             else ".venv/bin/python") + " -m pip install -r requirements.txt")
    return py


def ensure_env() -> None:
    env_path = os.path.join(ROOT, ".env")
    if os.path.isfile(env_path):
        say(PASS, ".env already exists (left untouched)")
        return
    example = os.path.join(ROOT, ".env.example")
    if not os.path.isfile(example):
        say(FAIL, ".env.example is missing — cannot create .env")
        return
    shutil.copyfile(example, env_path)
    say(PASS, "created .env from .env.example (edit it: keys, ARI, WEB_PASSWORD)")


def installed_voices() -> "list[str]":
    if not os.path.isdir(MODELS):
        return []
    return sorted(f[:-5] for f in os.listdir(MODELS) if f.endswith(".onnx"))


def ensure_voices(py: str, wanted: "list[str]", *, skip: bool) -> None:
    if skip:
        say(PASS, "--no-voices: skipping the voice download")
        return
    here = installed_voices()
    if here and not wanted:
        say(PASS, f"{len(here)} neural voice(s) already installed: {', '.join(here)}")
        return
    fetch = os.path.join(ROOT, "scripts", "fetch_neural_voices.py")
    if not os.path.isfile(fetch):
        say(FAIL, "scripts/fetch_neural_voices.py is missing")
        return
    cmd = [py, fetch]
    for voice in wanted:
        cmd += ["--voice", voice]
    if run(cmd) != 0:
        say(FAIL, "voice download failed — see DEPLOYMENT.md (Troubleshooting); "
                  "everything except audio still works without it")
    else:
        say(PASS, f"neural voices ready: {', '.join(installed_voices()) or 'none'}")


def next_steps(py: str) -> None:
    rel = os.path.relpath(py, ROOT)
    print()
    print("=" * 74)
    print("  Setup complete. Verify, then start:")
    print("=" * 74)
    print(f"  readiness report : {rel} scripts/healthcheck.py")
    print(f"  test suite       : {rel} -m pytest tests/ -q")
    print()
    if IS_WINDOWS:
        print("  start (debug)    : .\\start.ps1")
        print("  start (live PBX) : .\\start.ps1 -Serve")
    else:
        print("  start (debug)    : ./start.sh")
        print("  start (live PBX) : ./start.sh --serve")
    print()
    print("  The launcher starts the neural voice server on :8880 as well, so the")
    print("  dashboard's Test voice and the live call path both have audio.")
    print("  Full guide: DEPLOYMENT.md")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description="Set up this machine to run the voice fronter.")
    ap.add_argument("--voice", action="append", default=[],
                    help="fetch only this voice (repeatable); default: all of them")
    ap.add_argument("--no-voices", action="store_true", help="skip the voice download")
    ap.add_argument("--no-install", action="store_true",
                    help="do not create/upgrade the venv or install packages")
    ap.add_argument("--quiet-healthcheck", action="store_true",
                    help="skip the readiness report at the end")
    args = ap.parse_args()

    print()
    print("BPO Adriene — local setup")
    print("-" * 74)
    if not check_python():
        return 1
    py = ensure_venv(install=not args.no_install)
    ensure_env()
    ensure_voices(py, args.voice, skip=args.no_voices)
    if not args.quiet_healthcheck:
        print()
        run([py, os.path.join(ROOT, "scripts", "healthcheck.py"), "--offline"])
    next_steps(py)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
