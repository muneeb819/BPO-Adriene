#!/usr/bin/env bash
# =============================================================================
#  BPO Adriene — AI Voice Bot launcher (Linux / macOS)
#
#    ./start.sh                # bootstrap if needed, then start on :8055
#    ./start.sh --port 9000    # another port
#    ./start.sh --check        # readiness report, then exit (no server)
#    ./start.sh --tests        # run the full test suite, then exit
#    ./start.sh --serve        # start without re-running the bootstrap
#    ./start.sh --no-voice     # start with the browser voice (TTS_PROVIDER=mock)
#
#  One process serves the API, the WebSocket call flow, the Kokoro neural
#  voice engine and the built console. First start downloads the voice model
#  (~330 MB, ~40 s); it is cached in ~/.cache/huggingface after that.
#
#  SECRET_KEY is REQUIRED. The launcher will not invent one: a shipped default
#  secret is a shipped vulnerability. Put it in .env (see .env.example).
# =============================================================================
set -e
cd "$(dirname "$0")"

PORT=8055
SERVE_ONLY=0
NO_VOICE=0
MODE="start"

while [ $# -gt 0 ]; do
  case "$1" in
    --port)     PORT="${2:?--port needs a number}"; shift 2 ;;
    --tests)    MODE="tests"; shift ;;
    --check)    MODE="check"; shift ;;
    --serve)    SERVE_ONLY=1; shift ;;
    --no-voice) NO_VOICE=1; shift ;;
    -h|--help)  sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac
done

VENV=".venv"
PYTHON="${PYTHON:-python3}"

# ---------------------------------------------------------------------------
# 1. .env — create from the example on first run, never overwrite
# ---------------------------------------------------------------------------
if [ ! -f .env ]; then
  if [ -f .env.example ]; then
    cp .env.example .env
    echo "Created .env from .env.example"
  fi
fi

# Load .env so SECRET_KEY and friends are visible to this script.
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

# ---------------------------------------------------------------------------
# 2. SECRET_KEY — fail fast, never fall back to a default
# ---------------------------------------------------------------------------
if [ "$MODE" != "tests" ] && [ -z "${SECRET_KEY:-}" ]; then
  cat >&2 <<'MSG'
ERROR: SECRET_KEY is not set.

Refusing to start with a default secret — anyone who knows the default can
forge dashboard sessions. Generate one and put it in .env:

    python3 -c "import secrets; print(secrets.token_urlsafe(32))"

then add the line  SECRET_KEY=<that value>  to .env (or export it).
MSG
  exit 1
fi

export DATABASE_URL="${DATABASE_URL:-sqlite:///./dev.db}"
export HF_HUB_DISABLE_XET=1   # xet chunk-downloader can fill the disk — keep it off
if [ "$NO_VOICE" = "1" ]; then export TTS_PROVIDER=mock; fi

# ---------------------------------------------------------------------------
# 3. Test mode — run BOTH suites (tests/ and backend/tests/), then exit
# ---------------------------------------------------------------------------
if [ "$MODE" = "tests" ]; then
  if [ -x "$VENV/bin/python" ]; then PY="$VENV/bin/python"; else PY="$PYTHON"; fi
  exec "$PY" -m pytest tests/ backend/tests/ "$@"
fi

# ---------------------------------------------------------------------------
# 4. Bootstrap: virtualenv + dependencies (skipped with --serve)
# ---------------------------------------------------------------------------
if [ "$SERVE_ONLY" != "1" ]; then
  if [ ! -x "$VENV/bin/python" ]; then
    echo "Creating virtualenv in $VENV (first run only)..."
    "$PYTHON" -m venv "$VENV"
  fi
  PY="$VENV/bin/python"
  if ! "$PY" -c "import fastapi" >/dev/null 2>&1; then
    echo "Installing dependencies (first run only)..."
    "$PY" -m pip install --upgrade pip >/dev/null
    "$PY" -m pip install -r requirements.txt
  fi
else
  PY="${PYTHON}"
fi

# ---------------------------------------------------------------------------
# 5. Readiness check
# ---------------------------------------------------------------------------
if [ "$MODE" = "check" ]; then
  exec "$PY" scripts/healthcheck.py
fi

# ---------------------------------------------------------------------------
# 6. Build the console once
# ---------------------------------------------------------------------------
if [ ! -f frontend/build/index.html ]; then
  echo "Building the console (first run only)..."
  (cd frontend && { [ -d node_modules ] || npm install; } && npm run build)
fi

echo "Adriene on http://localhost:$PORT  (Ctrl+C to stop)"
exec "$PY" -m uvicorn backend.app.main:app --port "$PORT"
