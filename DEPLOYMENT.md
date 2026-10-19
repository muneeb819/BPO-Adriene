# Deploying BPO Adriene

Everything needed to run the bot on a machine you control. One process serves
**everything on one port**: REST API, WebSocket call flow, the Kokoro neural
voice engine, and the built React console.

| | |
|---|---|
| Console + API | `http://<host>:8055/` |
| Health probe | `GET /api/health` → `{"status":"ok"}` |
| Voice engine | in-process (Kokoro-82M, 20 US-English voices) |
| Storage | SQLite call log + self-learning memory (one data dir) |
| Cloud dependencies | **none** — everything runs local; an LLM key is optional |

---

## 1. Quick start — Docker (recommended)

```bash
git clone -b feature/ai-voice-bot-scaffold https://github.com/muneeb819/BPO-Adriene
cd BPO-Adriene
cp .env.example .env            # set SECRET_KEY; add an LLM key if you want one
docker compose up -d --build
open http://localhost:8055/
```

Two named volumes persist everything that matters:
- `bot-data` → SQLite DB + `brain_memory.json` (self-learning)
- `hf-cache` → the Kokoro model (~330 MB), so it is downloaded only once

## 2. Quick start — bare metal

Requirements: Python 3.11+, Node 18+ (build once), ~1.5 GB free disk.

```bash
# console (build once; the backend serves frontend/build)
cd frontend && npm install && npm run build && cd ..

# backend
cd backend
pip install -r requirements.txt
export SECRET_KEY=dev-secret DATABASE_URL=sqlite:///./dev.db   # setx on Windows
python -m uvicorn backend.app.main:app --port 8055
```

Windows one-click: `.\start.ps1` (or double-click `start.bat`).
Linux/macOS: `./start.sh`. Tests: `./start.sh --tests`.

> **CPU-only torch.** `kokoro` pulls torch — on GPU-less servers make sure the
> CPU build is what gets installed (`pip install torch --index-url
> https://download.pytorch.org/whl/cpu`). The Dockerfile does this for you.

## 3. Configuration

All configuration is environment variables — copy `.env.example` and edit.
Highlights:

| Variable | Default | Notes |
|---|---|---|
| `SECRET_KEY` | — | **set this** in any real deployment |
| `DATABASE_URL` | `sqlite:///./dev.db` | any SQLAlchemy URL (Postgres ready) |
| `BRAIN_MEMORY_PATH` | `backend/brain_memory.json` | self-learning memory file |
| `TTS_PROVIDER` | `kokoro` | `kokoro` (neural) · `mock` (browser voice) |
| `TTS_VOICE` / `TTS_SPEED` | `af_heart` / `1.0` | 20 voices; change live in the Voice Panel |
| `STT_PROVIDER` | `mock` | live STT is browser mic dictation (Web Speech API) |
| `LLM_PROVIDER` | `mock` | `openai` · `anthropic` · `gemini` |
| `TRANSFER_PROVIDER` | `mock` | `vici` · `twilio` (adapters need credentials) |

Runtime changes (voice, pace, providers) can be made live from the console's
**Runtime Config** panel — no restart needed.

## 4. Attaching an LLM

The bot is fully functional with **no key** — the local brain handles NLU,
Medicare knowledge, compliance, memory and wording. To upgrade wording to a
real model, add **one** env var and restart:

```bash
OPENAI_API_KEY=sk-...          # or ANTHROPIC_API_KEY / GEMINI_API_KEY
OPENAI_MODEL=gpt-4o-mini       # optional overrides: ANTHROPIC_MODEL / GEMINI_MODEL
```

- The key **is** the provider selection (no SDK needed — plain HTTPS).
- Decisions (eligibility, DNC, transfer) always stay in the policy layer; the
  model only re-words what the brain decided, so it can never go off-script.
- Any failure (expired key, network down, timeout at 8 s) falls back to the
  local persona instantly — a live call never dies.
- Verify with `GET /api/brain/status` → `"llm_ready": true`.

## 5. First start & model download

The first request that needs a voice downloads the Kokoro-82M model (~330 MB,
~40 s). Keep `HF_HUB_DISABLE_XET=1` set (the Dockerfile and launchers do) —
HF's experimental xet downloader is known to fill disks with partials. Cache
locations: `/root/.cache/huggingface` in Docker, `~/.cache/huggingface` bare
metal. Warm it ahead of traffic: `POST /api/tts/warm`.

## 6. Health, tests, QA

```bash
curl http://localhost:8055/api/health          # liveness (used by Docker HEALTHCHECK)
curl http://localhost:8055/api/brain/status    # persona, memory counts, llm_ready
cd backend && python -m pytest tests -q        # 71 tests
python backend/scripts/qa_runner.py --all      # scenario QA, writes backend/qa_outputs
```

Recordings of every scenario turn are browsable at `/recordings.html`, QA
reports at `/qa_reports.html`.

## 6b. LiveKit worker (optional second process)

The web console runs without LiveKit; the LiveKit agent is a separate worker that
connects *out* to LiveKit (no inbound ports). It can run on the same box or any
machine that can reach your LiveKit project.

```bash
pip install -r requirements-livekit.txt
python scripts/fetch_neural_voices.py          # local voice (~60 MB, once)
python livekit_agent.py start                  # production worker
```

`.env` keys it reads: `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`,
`LIVEKIT_AGENT_NAME`, `LIVEKIT_STT_PROVIDER`, `LIVEKIT_TTS_PROVIDER`,
`LIVEKIT_TRANSFER_NUMBER`. Tokens for the browser console are signed by the API
process (`POST /api/livekit/token`), so both processes need the same `.env`.

systemd unit (in addition to `deploy/bpo-adriene.service`):

```ini
[Service]
WorkingDirectory=/opt/bpo-adriene
EnvironmentFile=/opt/bpo-adriene/.env
ExecStart=/opt/bpo-adriene/.venv/bin/python livekit_agent.py start
Restart=always
```

Saved calls land in `data/livekit_calls/` (JSON: transcript, disposition,
qualification fields). See **[docs/LIVEKIT.md](docs/LIVEKIT.md)**.

## 7. Systemd (bare-metal Linux)

`deploy/bpo-adriene.service` is ready: copy to `/etc/systemd/system/`, adjust
`WorkingDirectory`/`User`, put env in `/opt/bpo-adriene/.env`, then
`systemctl enable --now bpo-adriene`.

## 8. Security & ops notes

- Serve behind TLS in production (the console uses mic + WebSocket — browsers
  require a secure context; `localhost` is exempt, public hosts are not).
- Back up the data dir: SQLite DB + `brain_memory.json` are the only state.
- The Kokoro cache volume is safe to delete (re-downloads on next use).
- Transfer adapters (ViciDial/Twilio) return `False` when unconfigured — the
  call continues; wire credentials before going live with handoffs.

## 9. Troubleshooting

| Symptom | Fix |
|---|---|
| Disk filling during first start | ensure `HF_HUB_DISABLE_XET=1`; delete `*.incomplete` under the HF cache |
| Very slow first synthesis | expected (~40 s cold); warm with `POST /api/tts/warm` |
| Mic button says unsupported | use Chrome/Edge (Web Speech API); needs HTTPS or localhost |
| No sound, only text | check the Voice Panel engine is `neural` and `POST /api/tts` returns audio |
| LLM not upgrading | `GET /api/brain/status` → `llm_ready`; check the key env var spelling |
