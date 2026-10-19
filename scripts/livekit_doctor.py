#!/usr/bin/env python3
"""Diagnose the LiveKit setup in one command.

    python scripts/livekit_doctor.py
    ./start_livekit.sh doctor

Checks, in the order that usually matters:
  1. .env is present and the three credentials are set / well-shaped
  2. the machine can reach the LiveKit endpoint (TCP + TLS)
  3. the token we sign is valid and carries the right grants
  4. which engines a call would use (STT / TTS / VAD / LLM)
  5. whether the local voice and speech models are installed

Exit code 0 = no blocking problem. Warnings never fail the run.
"""
from __future__ import annotations

import os
import socket
import ssl
import sys
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
except Exception:
    pass

GREEN, RED, YELLOW, DIM, END = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def ok(msg: str) -> None:
    print(f"  {GREEN}✓{END} {msg}")


def bad(msg: str) -> None:
    print(f"  {RED}✗{END} {msg}")


def warn(msg: str) -> None:
    print(f"  {YELLOW}!{END} {msg}")


def mask(value: str) -> str:
    if not value:
        return "(unset)"
    return value if len(value) <= 8 else f"{value[:4]}…{value[-3:]}"


def reachable(url: str, timeout: float = 4.0) -> tuple[bool, str]:
    parsed = urlparse(url if "://" in url else f"wss://{url}")
    host = parsed.hostname or ""
    port = parsed.port or 443
    if not host:
        return False, "no host in LIVEKIT_URL"
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with ctx.wrap_socket(raw, server_hostname=host):
                return True, f"TCP+TLS {host}:{port} OK"
    except Exception as exc:
        return False, f"{host}:{port} — {type(exc).__name__}: {exc}"


def main() -> int:
    print("\nBPO Adriene — LiveKit doctor")
    print(f"{DIM}repo {REPO_ROOT}{END}\n")

    problems = 0
    warnings = 0

    # --------------------------------------------------------- 1. credentials
    print("1. credentials")
    url = (os.getenv("LIVEKIT_URL") or "").strip()
    key = (os.getenv("LIVEKIT_API_KEY") or "").strip()
    secret = (os.getenv("LIVEKIT_API_SECRET") or "").strip()
    env_file = REPO_ROOT / ".env"
    if env_file.is_file():
        ok(f".env found ({env_file.name})")
    else:
        warn(".env not found — using the process environment")
        warnings += 1

    for label, value in (("LIVEKIT_URL", url), ("LIVEKIT_API_KEY", key),
                         ("LIVEKIT_API_SECRET", secret)):
        if value:
            ok(f"{label} = {mask(value)}")
        else:
            bad(f"{label} is not set")
            problems += 1

    if key and not key.startswith("API"):
        warn("API keys usually start with 'API' — double-check for a typo")
        warnings += 1
    if secret and len(secret) < 32:
        warn(f"API secret is only {len(secret)} chars (LiveKit issues 43+)")
        warnings += 1
    if url and not url.startswith(("ws://", "wss://")):
        warn("LIVEKIT_URL should start with wss:// (or ws:// for a local server)")
        warnings += 1

    # ------------------------------------------------------------ 2. network
    print("\n2. network")
    if url:
        reached, detail = reachable(url)
        (ok if reached else bad)(detail)
        if not reached:
            problems += 1
            print(f"     {DIM}The worker must reach LiveKit. If this box is firewalled,")
            print(f"     run the agent on a machine/container that is not.{END}")
    else:
        bad("skipped (no URL)")
        problems += 1

    # ------------------------------------------------------------- 3. tokens
    print("\n3. token signing")
    try:
        from livekit import api

        token = (
            api.AccessToken(key, secret)
            .with_identity("doctor")
            .with_grants(api.VideoGrants(room_join=True, room="adriene-doctor"))
            .to_jwt()
        )
        claims = api.TokenVerifier().verify(token, verify_signature=False)
        assert claims.video.room == "adriene-doctor"
        ok(f"JWT signed ({len(token)} chars), room grant verified")
    except Exception as exc:
        bad(f"could not sign a token: {type(exc).__name__}: {exc}")
        problems += 1

    # -------------------------------------------------------------- 4. stack
    print("\n4. agent stack")
    try:
        from backend.app.core.adapters.livekit_plugins.factory import (
            describe_stack,
            preload,
        )

        preload()
        stack = describe_stack()
        for leg in ("stt", "tts", "llm"):
            value = stack.get(leg, "none")
            (warn if value in (None, "", "none") else ok)(f"{leg.upper():4} {value}")
            if value in (None, "", "none") and leg != "llm":
                warnings += 1
        ok(f"VAD  {'silero' if stack.get('vad') else 'off'}")
        for line in stack.get("warnings", []):
            warn(line)
    except Exception as exc:
        bad(f"could not inspect the stack: {type(exc).__name__}: {exc}")
        problems += 1

    # -------------------------------------------------------------- 5. models
    print("\n5. local models")
    voices_dir = REPO_ROOT / "data" / "models" / "piper"
    voices = sorted(p.name for p in voices_dir.glob("*.onnx")) if voices_dir.is_dir() else []
    if voices:
        ok(f"{len(voices)} Piper voice(s): {', '.join(voices)}")
    else:
        warn("no Piper voice — run: python scripts/fetch_neural_voices.py")
        warnings += 1
    try:
        import faster_whisper  # noqa: F401

        ok("faster-whisper installed (base.en downloads on first use)")
    except Exception:
        warn("faster-whisper not installed (set DEEPGRAM_API_KEY or install it)")
        warnings += 1

    # -------------------------------------------------------------- summary
    print()
    if problems:
        print(f"{RED}{problems} problem(s){END}, {warnings} warning(s) — fix the ✗ items above.")
        return 1
    print(f"{GREEN}Ready.{END} {warnings} warning(s).")
    print("\nStart the worker:")
    print("  ./start_livekit.sh dev        # debug mode")
    print("  python livekit_agent.py console   # no server, local terminal")
    print("\nThen open /livekit.html to join a room from the browser.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
