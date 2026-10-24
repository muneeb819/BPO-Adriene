#!/usr/bin/env bash
# =============================================================================
#  BPO Adriene — LiveKit launcher
#
#    ./start_livekit.sh              # debug/dev mode (recommended)
#    ./start_livekit.sh dev          # same: hot-reload logs, DEBUG level
#    ./start_livekit.sh start        # production worker
#    ./start_livekit.sh console      # local terminal, no LiveKit server
#    ./start_livekit.sh doctor       # check config + reachability, then exit
#
#  Reads credentials from ./.env (LIVEKIT_URL / LIVEKIT_API_KEY /
#  LIVEKIT_API_SECRET). On first run it creates .venv, installs
#  requirements-livekit.txt and fetches a local neural voice.
# =============================================================================
set -e
cd "$(dirname "$0")"

MODE="${1:-dev}"
PY=".venv/bin/python"

if [ ! -x "$PY" ]; then
  echo "Creating .venv (first run)…"
  python3 -m venv .venv
  .venv/bin/pip install -q -U pip setuptools wheel
fi

if ! "$PY" -c "import livekit.agents" >/dev/null 2>&1; then
  echo "Installing the LiveKit runtime (first run)…"
  .venv/bin/pip install -q -r requirements-livekit.txt
fi

if ! "$PY" -c "import fastapi, uvicorn" >/dev/null 2>&1; then
  .venv/bin/pip install -q fastapi "uvicorn[standard]" sqlmodel python-dotenv
fi

# A local voice is what makes the bot audible with zero cloud keys.
if ! ls data/models/piper/*.onnx >/dev/null 2>&1; then
  echo "Fetching a local neural voice (~60 MB, once)…"
  "$PY" scripts/fetch_neural_voices.py --voice en_US-amy-low || true
fi

if [ "$MODE" = "doctor" ]; then
  exec "$PY" scripts/livekit_doctor.py
fi

export LOG_LEVEL="${LOG_LEVEL:-DEBUG}"
echo "Adriene → LiveKit ($MODE mode). Ctrl+C to stop."
exec "$PY" livekit_agent.py "$MODE" --log-level debug
