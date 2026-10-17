#!/usr/bin/env bash
# =============================================================================
#  BPO Adriene — AI Voice Bot launcher (Linux / macOS)
#
#    ./start.sh                # start on https://localhost:8055
#    ./start.sh --port 9000    # another port
#    ./start.sh --tests        # run the test suite, then exit
#
#  HTTPS is on by default (self-signed dev cert for localhost). The frontend
#  switches to wss:// automatically. Accept the browser's "not private" warning
#  once. Real certs: drop cert.pem + key.pem into backend/certs/ (or terminate
#  TLS at a reverse proxy in production).
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

if [ ! -f backend/certs/cert.pem ]; then
  python backend/scripts/gen_cert.py
fi

echo "Adriene on https://localhost:$PORT  (Ctrl+C to stop)"
exec python -m uvicorn backend.app.main:app --port "$PORT" --ssl-keyfile backend/certs/key.pem --ssl-certfile backend/certs/cert.pem
