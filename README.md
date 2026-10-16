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
cp .env.example .env && docker compose up -d --build
open http://localhost:8055/

# — or bare metal —
cd frontend && npm install && npm run build && cd ..
cd backend && pip install -r requirements.txt
set SECRET_KEY=dev-secret && set DATABASE_URL=sqlite:///./dev.db
python -m uvicorn backend.app.main:app --port 8055
```

Windows: `.\start.ps1` · Linux/macOS: `./start.sh` · Tests: `./start.sh --tests`

## Layout

```
backend/app/main.py        API + WebSocket call flow (serves the console too)
backend/app/core/brain/    NLU · Medicare KB · persona · compliance · memory
backend/app/core/          state_machine · orchestrator · adapters (kokoro_tts,
                           llm, stt, tts, transfer, media, twilio)
backend/scripts/           qa_runner (scenario QA + per-turn audio)
backend/tests/             pytest suite (71 tests)
frontend/src/App.js        console: call UI, voice panel, mic, hands-free
Dockerfile · docker-compose.yml · .env.example · start.* launchers
```

See **[DEPLOYMENT.md](DEPLOYMENT.md)** for production deployment (Docker,
systemd, env table, LLM attach, persistence, troubleshooting).
