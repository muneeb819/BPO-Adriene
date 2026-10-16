# =============================================================================
#  BPO Adriene — AI Voice Bot
#
#  One image runs everything: FastAPI (REST + WebSocket call flow) with the
#  Kokoro-82M neural voice engine in-process, serving the built React console
#  on a single port.
#
#    docker build -t bpo-adriene .
#    cp .env.example .env          # set SECRET_KEY (and any LLM key)
#    docker compose up -d
#    open http://localhost:8055/
#
#  First start downloads the Kokoro model (~330 MB) into the hf-cache volume
#  (~40 s); every start after that is instant.
# =============================================================================

# --- stage 1: build the React console ---------------------------------------
FROM node:20-alpine AS webbuild
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/public ./public
COPY frontend/src ./src
RUN npm run build

# --- stage 2: backend + voice engine ----------------------------------------
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HUB_DISABLE_XET=1 \
    SECRET_KEY=change-me \
    DATABASE_URL=sqlite:////app/data/dev.db \
    BRAIN_MEMORY_PATH=/app/data/brain_memory.json \
    TTS_PROVIDER=kokoro \
    TTS_VOICE=af_heart \
    TTS_SPEED=1.0 \
    STT_PROVIDER=mock \
    LLM_PROVIDER=mock \
    TRANSFER_PROVIDER=mock \
    MEDIA_BRIDGE=mock

WORKDIR /app

# CPU-only torch (~200 MB) — deliberately NOT the CUDA build. Installed first
# so kokoro's torch dependency is already satisfied and never pulls CUDA.
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend/ ./backend/
COPY --from=webbuild /web/build ./frontend/build
COPY frontend/public ./frontend/public

# /app/data holds the SQLite call log + self-learning memory (mount a volume
# there to keep them). The Kokoro model lives in /root/.cache/huggingface.
RUN mkdir -p /app/data

EXPOSE 8055

HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8055/api/health', timeout=4).status==200 else 1)"

CMD ["python", "-m", "uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8055"]
