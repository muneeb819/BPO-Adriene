# =============================================================================
#  BPO Adriene — AI Voice Fronter
#
#  One container runs both processes (neural voice server on :8880 and the app
#  on :8000) via deploy/docker-entrypoint.sh:
#
#     docker build -t bpo-adriene .
#     docker run -d --name bpo-adriene -p 8000:8000 \
#       -v "$PWD/data:/app/data" --env-file .env bpo-adriene
#
#  Voices (~190 MB) download into /app/data/models on first start, so mounting
#  ./data keeps them across restarts. `--build-arg FETCH_VOICES=1` bakes them
#  into the image instead.
# =============================================================================
FROM python:3.12-slim

ARG FETCH_VOICES=0

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIPER_BASE_URL=http://127.0.0.1:8880 \
    KOKORO_BASE_URL=http://127.0.0.1:8880 \
    HOST=0.0.0.0 \
    PORT=8000

WORKDIR /app

# libsndfile is what `soundfile` needs to decode mp3/flac/ogg uploads in the
# voice studio; the neural engine itself needs nothing beyond pip packages.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libsndfile1 \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

# Optionally bake the voice models into the image (adds ~190 MB)
RUN if [ "$FETCH_VOICES" = "1" ]; then python scripts/fetch_neural_voices.py; fi

RUN mkdir -p data/models/piper logs && chmod +x deploy/docker-entrypoint.sh

EXPOSE 8000 8880

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD python - <<'PY' || exit 1
import json, urllib.request
app = json.loads(urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=3).read())
assert app.get("status") == "ok", app
try:
    voice = json.loads(urllib.request.urlopen("http://127.0.0.1:8880/health", timeout=3).read())
    assert voice.get("voices"), voice
except Exception:
    pass          # the app is healthy even when the voice engine is not running
PY

CMD ["deploy/docker-entrypoint.sh"]
