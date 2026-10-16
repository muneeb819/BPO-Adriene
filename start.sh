#!/usr/bin/env bash
# =============================================================================
#  BPO Adriene — AI Voice Bot launcher (Linux / macOS)
#
#    ./start.sh                # start on http://localhost:8055
#    ./start.sh --port 9000    # another port
#    ./start.sh --tests        # run the test suite, then exit
#
#  One process serves the API, the WebSocket call flow, the Kokoro neural
#  voice engine and the built console. First start downloads the voice model
#  (~330 MB, ~40 s); it is cached in ~/.cache/huggingface after that.
# =============================================================================
set -e
cd "$(dirname "$0")"

PORT=8055
if [ "$1" = "--port" ] && [ -n "$2" ]; then PORT="$2"; fi

if [ "$1" = "--tests" ]; then
  cd backend && python -m pytest tests -q
  exit $?
fi

export SECRET_KEY="${SECRET_KEY:-dev-secret}"
export DATABASE_URL="${DATABASE_URL:-sqlite:///./dev.db}"
export HF_HUB_DISABLE_XET=1   # xet chunk-downloader can fill the disk — keep it off

if [ ! -f frontend/build/index.html ]; then
  echo "Building the console (first run only)..."
  (cd frontend && [ -d node_modules ] || npm install && npm run build)
fi

echo "Adriene on http://localhost:$PORT  (Ctrl+C to stop)"
exec python -m uvicorn backend.app.main:app --port "$PORT"
