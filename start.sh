#!/usr/bin/env bash
# =============================================================================
#  BPO Adriene — AI Voice Fronter launcher (Linux / macOS)
#
#    ./start.sh                 # debug: dashboard + demo call + local neural voices
#    ./start.sh --serve         # live server: ARI listener + web (needs Asterisk)
#    ./start.sh --check         # readiness report, then exit
#    ./start.sh --tests         # test suite, then exit
#    ./start.sh --no-voice      # do not start the neural voice server
#    ./start.sh --port 8080 --voice-port 8881
#
#  Starts the local neural voice server (Piper/ONNX, :8880, no key, no torch) and
#  the app (:8000) together, and stops the voice server again on exit.
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PORT=8000
VOICE_PORT=8880
MODE="debug"
DO_CHECK=0
DO_TESTS=0
START_VOICE=1

while [ $# -gt 0 ]; do
  case "$1" in
    --serve)      MODE="serve" ;;
    --debug)      MODE="debug" ;;
    --check)      DO_CHECK=1 ;;
    --tests)      DO_TESTS=1 ;;
    --no-voice)   START_VOICE=0 ;;
    --port)       PORT="$2"; shift ;;
    --voice-port) VOICE_PORT="$2"; shift ;;
    -h|--help)    sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "[error] unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac
  shift
done

info()  { printf '\033[36m%s\033[0m\n' "$*"; }
ok()    { printf '\033[32m%s\033[0m\n' "$*"; }
warn()  { printf '\033[33m%s\033[0m\n' "$*"; }
fail()  { printf '\033[31m%s\033[0m\n' "$*"; }

port_open() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && exec 3<&- && exec 3>&-; }

# --- interpreter -------------------------------------------------------------
PY=""
for candidate in "$ROOT/.venv/bin/python" python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c "import sys" 2>/dev/null; then
    PY="$candidate"; break
  fi
done
if [ -z "$PY" ]; then
  fail "[error] No Python 3 found. Install Python 3.12 (apt install python3.12 python3.12-venv)."
  exit 1
fi

if ! "$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  fail "[error] $("$PY" -V) is too old — this project needs Python 3.10+ (3.12 recommended)."
  exit 1
fi

# --- bootstrap ---------------------------------------------------------------
if [ ! -x "$ROOT/.venv/bin/python" ]; then
  warn "[setup] First run: creating .venv and installing dependencies..."
  "$PY" -m venv .venv || { fail "[error] python3-venv missing? try: apt install python3.12-venv"; exit 1; }
  "$ROOT/.venv/bin/python" -m pip install --upgrade pip
  "$ROOT/.venv/bin/python" -m pip install -r requirements.txt
fi
PY="$ROOT/.venv/bin/python"

if [ ! -f "$ROOT/.env" ] && [ -f "$ROOT/.env.example" ]; then
  cp "$ROOT/.env.example" "$ROOT/.env"
  warn "[setup] Created .env from .env.example — edit it (keys, ARI, WEB_PASSWORD)."
fi

if [ "$DO_TESTS" = 1 ]; then
  info "[tests] running the suite..."
  exec "$PY" -m pytest tests/ -q
fi
if [ "$DO_CHECK" = 1 ]; then
  info "[check] running the readiness report..."
  exec "$PY" scripts/healthcheck.py --url "http://localhost:$PORT"
fi

export PORT PIPER_BASE_URL="http://127.0.0.1:$VOICE_PORT"
mkdir -p "$ROOT/logs"

# --- 1. local neural voice server -------------------------------------------
VOICE_PID=""
cleanup() {
  if [ -n "$VOICE_PID" ] && kill -0 "$VOICE_PID" 2>/dev/null; then
    info "[voice] stopping the neural voice server (pid $VOICE_PID)"
    kill "$VOICE_PID" 2>/dev/null || true
    wait "$VOICE_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

if [ "$START_VOICE" = 1 ]; then
  if port_open "$VOICE_PORT"; then
    ok "[voice] already running on :$VOICE_PORT (left as it is)"
  else
    info "[voice] starting the neural voice server on :$VOICE_PORT (log: logs/voice-server.log)"
    "$PY" scripts/neural_tts_server.py --host 0.0.0.0 --port "$VOICE_PORT" --warm \
      >"$ROOT/logs/voice-server.log" 2>&1 &
    VOICE_PID=$!
    for _ in $(seq 1 40); do
      sleep 0.5
      if port_open "$VOICE_PORT"; then break; fi
    done
    if port_open "$VOICE_PORT"; then
      ok "[voice] ready on :$VOICE_PORT"
    else
      warn "[voice] not answering yet — check logs/voice-server.log"
      warn "        (the dashboard still works; audio needs the voice server)"
    fi
  fi
else
  warn "[voice] skipped (--no-voice)"
fi

# --- 2. the app --------------------------------------------------------------
if [ "$MODE" = "serve" ]; then MODE_TEXT="serve (live: ARI + web)"; else MODE_TEXT="debug (demo call + web)"; fi
cat <<EOF

============================================================
  BPO Adriene - AI Voice Fronter          [$MODE_TEXT]
  Dashboard:      http://localhost:$PORT/
  Campaigns:      http://localhost:$PORT/campaigns
  Voice console:  http://localhost:$PORT/debug
  Voice studio:   http://localhost:$PORT/  (Voice studio panel)
  Health:         http://localhost:$PORT/health
  Voice server:   http://localhost:$VOICE_PORT/health
  Press CTRL+C to stop.
============================================================

EOF

if [ "$MODE" = "serve" ]; then
  "$PY" cli.py serve
else
  "$PY" cli.py debug
fi
