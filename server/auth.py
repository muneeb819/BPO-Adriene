"""Access control for the dashboard, its API and the live voice WebSocket.

Set WEB_PASSWORD in .env whenever the dashboard is reachable by anyone but the
machine itself. Every page, API and recording is then behind HTTP Basic auth.

Two deliberate details:

* ``/health`` stays open, so monitoring (and ``scripts/healthcheck.py``) still
  works;
* a successful login also sets a cookie, because a browser cannot put an
  ``Authorization`` header on a WebSocket handshake — the live voice socket
  would otherwise be impossible to use from the dashboard.

An empty WEB_PASSWORD (the default) leaves the server open.

The password itself is supplied by the caller through :func:`configure`, so this
module has no dependency on the server app or on how configuration is loaded.
(Callable provider rather than a plain value so that setting the password takes
effect immediately, including from the studio's "Use" button.)
"""
from __future__ import annotations

import base64
import hashlib
import secrets
from typing import Callable, Optional, Tuple

from fastapi import Request, WebSocket

AUTH_COOKIE = "bpo_auth"
AUTH_OPEN_PATHS = ("/health",)
AUTH_REALM = 'Basic realm="BPO Adriene"'

def _no_password() -> str:
    """Default provider: an open server, until the app configures a real one."""
    return ""


_password_provider: Callable[[], str] = _no_password


def configure(provider: Callable[[], str]) -> None:
    """Point the module at the live source of the dashboard password."""
    global _password_provider
    _password_provider = provider


def web_password() -> str:
    """The configured dashboard password, or "" when the server is open."""
    try:
        return (_password_provider() or "").strip()
    except Exception:                                   # a broken provider must not 500
        return ""


def auth_token(password: str) -> str:
    """Cookie value proving a successful login for this password."""
    return hashlib.sha256(f"bpo-adriene:{password}".encode("utf-8")).hexdigest()[:32]


def basic_credentials(header: str) -> Optional[Tuple[str, str]]:
    """Parse an ``Authorization: Basic`` header into (user, password)."""
    if not header.lower().startswith("basic "):
        return None
    try:
        raw = base64.b64decode(header.split(None, 1)[1].strip()).decode("utf-8")
    except Exception:                                            # noqa: BLE001
        return None
    user, _, password = raw.partition(":")
    return user, password


def _matches(supplied: str, password: str) -> bool:
    return bool(supplied) and secrets.compare_digest(supplied, password)


def authorised(request: Request) -> bool:
    """HTTP request check: Basic credentials or a valid session cookie."""
    password = web_password()
    creds = basic_credentials(request.headers.get("authorization", ""))
    if creds and secrets.compare_digest(creds[1], password):
        return True
    cookie = request.cookies.get(AUTH_COOKIE, "")
    return _matches(cookie, auth_token(password))


def ws_authorised(websocket: WebSocket) -> bool:
    """WebSocket handshake check: cookie, Basic credentials, or ?password=."""
    password = web_password()
    if not password:
        return True
    cookie = websocket.cookies.get(AUTH_COOKIE, "")
    if _matches(cookie, auth_token(password)):
        return True
    creds = basic_credentials(websocket.headers.get("authorization", ""))
    if creds and secrets.compare_digest(creds[1], password):
        return True
    return _matches(websocket.query_params.get("password", ""), password)
