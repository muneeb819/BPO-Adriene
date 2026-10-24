"""LiveKit endpoints: debug console status, room tokens, saved calls.

The heavy lifting happens in ``livekit_agent.py`` (the worker). These endpoints
only *configure and observe* it:

    GET  /api/livekit/status     is LiveKit configured? what will the call run?
    POST /api/livekit/token      join token for the debug console (signed locally)
    GET  /api/livekit/rooms      rooms that currently exist (needs network)
    GET  /api/livekit/calls      transcripts the worker saved under data/livekit_calls
    GET  /api/livekit/doctor     one-shot diagnosis of config + reachability

Tokens are signed with the API secret on this machine, so ``/token`` keeps
working even when the server itself has no outbound network (the browser talks
to LiveKit directly).
"""
from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/livekit", tags=["livekit"])

# Warm the LiveKit plugin registry on the main thread (see factory.preload).
try:
    from .core.adapters.livekit_plugins.factory import preload as _preload

    _preload()
except Exception:
    pass

REPO_ROOT = Path(__file__).resolve().parents[2]
CALL_LOG_DIR = Path(os.getenv("LIVEKIT_CALL_LOG_DIR", REPO_ROOT / "data" / "livekit_calls"))


# --------------------------------------------------------------------------- #
#                                  helpers
# --------------------------------------------------------------------------- #
def credentials() -> dict:
    return {
        "url": (os.getenv("LIVEKIT_URL") or "").strip(),
        "api_key": (os.getenv("LIVEKIT_API_KEY") or "").strip(),
        "api_secret": (os.getenv("LIVEKIT_API_SECRET") or "").strip(),
    }


def _mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "set"
    return f"{value[:4]}…{value[-3:]}"


def _host_port(url: str) -> tuple[str, int]:
    parsed = urlparse(url if "://" in url else f"wss://{url}")
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme in ("wss", "https") else 7880)
    return host, port


def _reachable(url: str, timeout: float = 3.0) -> bool:
    """TCP + TLS handshake. A plain TCP check lies behind blocking proxies."""
    host, port = _host_port(url)
    if not host:
        return False
    import ssl

    try:
        context = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with context.wrap_socket(raw, server_hostname=host):
                return True
    except (OSError, ssl.SSLError):
        return False


def _sdk():
    """Return ``livekit.api`` or None when livekit-agents isn't installed."""
    try:
        from livekit import api  # noqa: PLC0415
        return api
    except Exception:
        return None


def _stack() -> dict:
    try:
        from .core.adapters.livekit_plugins import describe_stack  # noqa: PLC0415
        return describe_stack()
    except Exception as exc:
        return {"available": False, "reason": str(exc), "stt": "none", "tts": "none",
                "llm": "brain", "vad": False, "voices": [], "warnings": []}


# --------------------------------------------------------------------------- #
#                                   status
# --------------------------------------------------------------------------- #
@router.get("/status")
def status() -> dict:
    creds = credentials()
    configured = all(creds.values())
    api = _sdk()
    reachable = _reachable(creds["url"]) if creds["url"] else False
    return {
        "configured": configured,
        "url": creds["url"],
        "api_key": _mask(creds["api_key"]),
        "api_secret_set": bool(creds["api_secret"]),
        "agent_name": (os.getenv("LIVEKIT_AGENT_NAME") or "").strip(),
        "dispatch": (os.getenv("LIVEKIT_AGENT_NAME") or "").strip() or "automatic",
        "sdk_installed": api is not None,
        "reachable": reachable,
        "stack": _stack(),
        "call_log_dir": str(CALL_LOG_DIR),
        "transfer_number": (os.getenv("LIVEKIT_TRANSFER_NUMBER") or "").strip(),
    }


@router.get("/doctor")
def doctor() -> dict:
    """Everything a support call needs, in one payload."""
    creds = credentials()
    checks = []

    checks.append({
        "check": "credentials",
        "ok": all(creds.values()),
        "detail": ("LIVEKIT_URL / API_KEY / API_SECRET all set" if all(creds.values())
                   else "set LIVEKIT_URL, LIVEKIT_API_KEY and LIVEKIT_API_SECRET in .env"),
    })
    checks.append({
        "check": "key shape",
        "ok": bool(creds["api_key"].startswith("API") and len(creds["api_secret"]) >= 32),
        "detail": f"key={_mask(creds['api_key'])} ({len(creds['api_key'])} chars), "
                  f"secret={len(creds['api_secret'])} chars",
    })
    api = _sdk()
    checks.append({
        "check": "livekit-agents installed",
        "ok": api is not None,
        "detail": "pip install -r requirements-livekit.txt" if api is None else "ok",
    })
    if creds["url"]:
        host, port = _host_port(creds["url"])
        reachable = _reachable(creds["url"])
        checks.append({
            "check": "network to LiveKit",
            "ok": reachable,
            "detail": f"TCP {host}:{port} {'open' if reachable else 'blocked/unreachable'}",
        })
    stack = _stack()
    checks.append({
        "check": "voice (TTS)",
        "ok": stack.get("tts") not in (None, "", "none"),
        "detail": stack.get("tts"),
    })
    checks.append({
        "check": "speech recognition (STT)",
        "ok": stack.get("stt") not in (None, "", "none"),
        "detail": stack.get("stt") if stack.get("stt") not in (None, "", "none")
        else "typed input still works; install faster-whisper or set DEEPGRAM_API_KEY",
    })
    return {
        "ok": all(c["ok"] for c in checks if c["check"] != "speech recognition (STT)"),
        "checks": checks,
        "stack": stack,
        "warnings": stack.get("warnings", []),
    }


# --------------------------------------------------------------------------- #
#                                   tokens
# --------------------------------------------------------------------------- #
@router.post("/token")
def create_token(payload: Optional[dict] = None) -> dict:
    payload = payload or {}
    creds = credentials()
    if not all(creds.values()):
        raise HTTPException(
            status_code=400,
            detail="LiveKit is not configured: set LIVEKIT_URL, LIVEKIT_API_KEY "
                   "and LIVEKIT_API_SECRET (see .env.example).",
        )
    api = _sdk()
    if api is None:
        raise HTTPException(
            status_code=503,
            detail="livekit-agents is not installed on the server: "
                   "pip install -r requirements-livekit.txt",
        )

    room = str(payload.get("room") or "adriene-debug").strip()[:64] or "adriene-debug"
    identity = str(payload.get("identity") or "").strip() or f"caller-{os.urandom(3).hex()}"
    agent_name = (os.getenv("LIVEKIT_AGENT_NAME") or "").strip()

    token = (
        api.AccessToken(creds["api_key"], creds["api_secret"])
        .with_identity(identity)
        .with_name(identity)
        .with_grants(api.VideoGrants(room_join=True, room=room))
    )

    dispatched = False
    if agent_name:
        # Explicit dispatch: only the named agent joins this room.
        try:
            token = token.with_room_config(
                api.RoomConfiguration(
                    agents=[api.RoomAgentDispatch(agent_name=agent_name)]
                )
            )
            dispatched = True
        except Exception:
            dispatched = False

    return {
        "token": token.to_jwt(),
        "url": creds["url"],
        "room": room,
        "identity": identity,
        "agent_name": agent_name,
        "explicit_dispatch": dispatched,
        "expires_in": 6 * 3600,
    }


@router.get("/rooms")
def list_rooms() -> dict:
    creds = credentials()
    if not all(creds.values()):
        return {"ok": False, "error": "LiveKit is not configured", "rooms": []}
    api = _sdk()
    if api is None:
        return {"ok": False, "error": "livekit-agents is not installed", "rooms": []}
    if not _reachable(creds["url"]):
        return {
            "ok": False,
            "error": f"cannot reach {creds['url']} from this server",
            "rooms": [],
        }
    try:
        import asyncio

        async def _list():
            async with api.LiveKitAPI(
                url=creds["url"],
                api_key=creds["api_key"],
                api_secret=creds["api_secret"],
            ) as lk:
                return await lk.room.list_rooms(api.ListRoomsRequest())

        started = time.time()
        response = asyncio.run(_list())
        return {
            "ok": True,
            "rooms": [
                {
                    "name": r.name,
                    "sid": r.sid,
                    "participants": r.num_participants,
                    "created_at": r.creation_time,
                }
                for r in response.rooms
            ],
            "took_ms": round((time.time() - started) * 1000),
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "rooms": []}


# --------------------------------------------------------------------------- #
#                              saved transcripts
# --------------------------------------------------------------------------- #
@router.get("/calls")
def saved_calls(limit: int = 20) -> dict:
    if not CALL_LOG_DIR.is_dir():
        return {"calls": []}
    files = sorted(CALL_LOG_DIR.glob("*.json"), reverse=True)[: max(1, min(limit, 200))]
    calls = []
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        calls.append({
            "file": path.name,
            "room": data.get("room"),
            "saved_at": data.get("saved_at"),
            "disposition": data.get("disposition"),
            "turns": len(data.get("transcript") or []),
            "data": data.get("data") or {},
        })
    return {"calls": calls, "dir": str(CALL_LOG_DIR)}


@router.get("/calls/{name}")
def saved_call(name: str) -> dict:
    if Path(name).name != name or not name.endswith(".json"):
        raise HTTPException(status_code=404, detail="not found")
    path = CALL_LOG_DIR / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return json.loads(path.read_text(encoding="utf-8"))
