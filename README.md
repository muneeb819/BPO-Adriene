# BPO Adriene — AI Voice Bot

A local-first Medicare outreach voice agent. A dialogue brain that understands
any reply, a natural neural voice, hands-free microphone conversation, and a
self-learning memory — all running on your machine with zero cloud keys.

## What it does

- **Dialogue brain** — script flow (greeting → Medicare → age → eligibility →
  warm transfer) with real understanding: Medicare knowledge base answers,
  objections, DNC (compliance-owned wording), complaints, callbacks, Spanish
  handoff, repair ladder for confused callers, and a self-learning memory that
  queues unknown phrasings you can teach later.
- **Neural voice** — Kokoro-82M with 20 US-English voices (11F/9M, quality
  graded in the Voice Panel), pace control, per-sentence chunking + prefetch,
  and barge-in (talk over her — she stops and listens).
- **Live microphone** — 🎤 push-to-talk with live transcript, or 🔄 hands-free
  mode: the mic auto-arms after she finishes and mutes while she speaks
  (half-duplex, so she never hears herself).
- **Attach an LLM by adding one env key** (OpenAI / Anthropic / Gemini) — the
  model re-words the brain's decisions; any failure falls back instantly.
- **QA built in** — scenario runner with per-turn audio + reports, and a
  `/recordings.html` player of every recorded call turn.

## Quick start

```bash
# Docker (recommended)
cp .env.example .env          # set SECRET_KEY first — see below
docker compose up -d --build
open http://localhost:8055/

# — or bare metal, one command (creates .venv, installs, builds, starts) —
./start.sh                    # Windows: .\start.ps1  (or double-click start.bat)
```

`SECRET_KEY` is **required** and has no default. The launchers stop and tell you
so rather than starting with a known secret:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
# then put SECRET_KEY=<value> in .env
```

## Install, build, test

Everything below works in a fresh clone with no prior setup. `make` targets and
the raw commands are equivalent — use whichever you prefer.

```bash
make install     # python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
make build       # cd frontend && npm install && npm run build
make test        # .venv/bin/python -m pytest tests/ backend/tests/ -v
make lint        # .venv/bin/python -m ruff check .
make type        # .venv/bin/python -m mypy
make check       # readiness report: keys, voices, audio (no server)
make run         # start on http://localhost:8055
```

| task | command |
|---|---|
| install | `make install` |
| build console | `make build` |
| **test** | `make test` — or `pytest tests/ backend/tests/ -v` |
| lint | `make lint` — the same gate CI runs |
| type check | `make type` — mypy over `agent/`, `server/`, `backend/app/` |
| readiness | `make check` |
| run | `make run` — or `./start.sh` |

`pytest` with no arguments runs both suites (`pytest.ini` lives in
`pyproject.toml`; `tests/` is the bot, `backend/tests/` the FastAPI app).
Tests that need the neural voice models, the LiveKit worker or a cloud key skip
cleanly instead of failing — install those extras to run them.

### Dependencies are pinned

`requirements.txt` states the supported ranges; **`requirements-lock.txt` pins
the exact set** (60 packages) that CI and `make install` use, so a fresh clone
reproduces the same environment. To change a dependency, edit
`requirements.txt` and re-freeze:

```bash
make lock        # regenerates requirements-lock.txt in a scratch venv
```

Dependabot opens weekly PRs for pip, the backend, npm and GitHub Actions.

### CI

`.github/workflows/ci.yml` runs on every push to `main` and every pull request:

| job | what it does |
|---|---|
| `ruff` | lint over `agent/ server/ backend/ tests/ scripts/` |
| `mypy` | type check over `agent/ server/ backend/app/` |
| `pytest` | the full suite, from the lockfile |
| `fresh-clone` | nothing but `make install && make test && make lint && make type` |

A red build blocks the merge. Current state on `main`: 450 passed, 40 skipped,
ruff clean, mypy clean in 64 files.

Contributions follow **[CONTRIBUTING.md](CONTRIBUTING.md)** — one change per
commit with the test that pins it. **[docs/CODE_QUALITY.md](docs/CODE_QUALITY.md)**
lists what is enforced, how to verify each claim yourself, and what is still
outstanding.

Windows: `.\start.ps1` · Linux/macOS: `./start.sh` · Tests: `./start.sh --tests`

## Run on LiveKit (real rooms, WebRTC, SIP)

The same brain and voice also run as a **LiveKit agent** — a browser client, a
mobile app, or a phone call can talk to it.

```bash
cp .env.example .env            # add LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET
./start_livekit.sh              # macOS / Linux / Git Bash
.\start_livekit.bat             # Windows (cmd or double-click)
.\start_livekit.ps1             # Windows (PowerShell)
# then open http://localhost:8055/livekit.html and join the room
```

| command | what it does |
|---|---|
| `./start_livekit.sh dev` (`start_livekit.bat`) | worker in debug mode (DEBUG logs) |
| `python livekit_agent.py start` | production worker |
| `python livekit_agent.py console` | local terminal session, no server |
| `./start_livekit.sh doctor` (`start_livekit.bat doctor`) | credentials + network + token + stack check |
| `python scripts/livekit_smoke_test.py` | offline brain → voice check (writes a WAV) |

With no cloud keys the call runs fully locally: Silero VAD → local Whisper →
the scripted brain → Piper voice. `DEEPGRAM_API_KEY`, `ELEVENLABS_API_KEY` or
`OPENAI_API_KEY` upgrade the matching leg automatically. Full guide:
**[docs/LIVEKIT.md](docs/LIVEKIT.md)**.

## Layout

```
backend/app/main.py        API + WebSocket call flow (serves the console too)
backend/app/core/brain/    NLU · Medicare KB · persona · compliance · memory
backend/app/core/          state_machine · orchestrator · adapters (kokoro_tts,
                           llm, stt, tts, transfer, media, twilio)
livekit_agent.py           LiveKit worker (AgentServer; one job per call)
backend/app/core/adapters/livekit_plugins/   brain-as-LLM · Piper TTS · whisper STT
backend/app/livekit_endpoints.py             /api/livekit/* (tokens, status, calls)
frontend/public/livekit.html                 browser debug console
backend/scripts/           qa_runner (scenario QA + per-turn audio)
backend/tests/             pytest suite (71 tests)
frontend/src/App.js        console: call UI, voice panel, mic, hands-free
Dockerfile · docker-compose.yml · .env.example · start.* launchers
```

See **[DEPLOYMENT.md](DEPLOYMENT.md)** for production deployment (Docker,
systemd, env table, LLM attach, persistence, troubleshooting).
