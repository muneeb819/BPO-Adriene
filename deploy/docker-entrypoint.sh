#!/bin/sh
# =============================================================================
#  Container entrypoint: neural voice server + app in one container.
#
#  The voice server (Piper/ONNX) runs in the background on :8880 and the app
#  runs in the foreground on :8000 — the same layout as start.sh on a host.
#  If FETCH_VOICES=1 (default) the models are downloaded on first start, into
#  the volume-mounted /app/data/models directory, so a restart does not re-fetch.
# =============================================================================
set -e

cd /app
mkdir -p logs data/models/piper

if [ "${FETCH_VOICES:-1}" = "1" ] && [ -z "$(ls -A data/models/piper/*.onnx 2>/dev/null)" ]; then
  echo "[entrypoint] downloading the neural voices (one time, ~190 MB)..."
  python scripts/fetch_neural_voices.py || \
    echo "[entrypoint] voice download failed — the app still runs, audio needs the models"
fi

if [ "${START_VOICE:-1}" = "1" ]; then
  echo "[entrypoint] starting the neural voice server on :8880"
  python scripts/neural_tts_server.py --host 0.0.0.0 --port 8880 --warm \
    >> logs/voice-server.log 2>&1 &
  VOICE_PID=$!
  trap 'kill "$VOICE_PID" 2>/dev/null || true' INT TERM EXIT
  # wait for the first voice to load (the app can start sooner, but a ready
  # engine avoids a "no audio" first impression in the dashboard)
  i=0
  while [ $i -lt 60 ]; do
    if python - <<'PY' >/dev/null 2>&1
import json, urllib.request
urllib.request.urlopen("http://127.0.0.1:8880/health", timeout=1).read()
PY
    then break; fi
    i=$((i + 1)); sleep 1
  done
  echo "[entrypoint] voice server: $([ $i -lt 60 ] && echo ready || echo 'not answering (see logs/voice-server.log)')"
fi

echo "[entrypoint] starting the app on :8000"
exec python cli.py serve
