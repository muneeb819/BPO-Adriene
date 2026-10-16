"""BPO-Adriene backend package.

Loads ``.env`` at import time — before ANY submodule reads configuration —
so ``cp .env.example .env`` actually works on bare metal (Docker compose
interpolates ``./.env`` and systemd reads ``EnvironmentFile`` itself; a plain
``uvicorn``/``start.*`` run previously ignored the file entirely).

Precedence: real process environment > root ``.env`` > ``backend/.env``.
Parsing is deliberately forgiving (comments, ``export`` prefix, quoting) and
wrapped so a malformed file can never break startup.
"""
import os
from pathlib import Path

__all__ = ["_load_env_file"]


def _load_env_file(path: Path) -> None:
    """Parse a dotenv-style file into os.environ without overriding real env."""
    try:
        if not path.is_file():
            return
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            # strip inline comments ("value   # why") — quoted values keep '#'
            if value and not value[0] in "\"'":
                value = value.split(" #", 1)[0].rstrip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key:
                os.environ.setdefault(key, value)
    except Exception:                     # noqa: BLE001
        pass                               # env parsing must never fail startup


_HERE = Path(__file__).resolve().parents    # [0]=app, [1]=backend, [2]=repo root
_load_env_file(_HERE[2] / ".env")           # root .env (canonical, Docker-visible)
_load_env_file(_HERE[1] / ".env")           # backend/.env fills any gaps
