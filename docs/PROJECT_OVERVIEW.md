# Project Overview — AI Voice Bot Management & Testing Playground

Project: BPO‑Adriene (branch: feature/ai-voice-bot-scaffold)

Elevator pitch

A local-first AI Voice Bot Management & Testing Playground that lets operations and product teams design, configure, test, and QA voice agents end‑to‑end — entirely in a controlled mock environment with no live telephony by default. It simulates STT → LLM → TTS pipelines, records sessions, runs deterministic QA scenarios, and produces reports and audio artifacts so teams can validate agents before any production telephony integration.

What it’s built on (tech stack)

- Backend: Python + FastAPI, WebSockets for real-time events
- Frontend: React (current scaffold); static preview pages for QA
- Persistence: SQLite for local dev (DATABASE_URL flexible for Postgres)
- Local TTS for QA: pyttsx3 (WAV output)
- Adapters: modular adapter layer for STT, LLM, TTS, transfer (mock + prototypes)
- QA tooling: Scenario runner (Python), per-scenario JSON reports, per-turn WAVs, consolidated CSV

What it does (core capabilities)

- Create and version AI agents and system prompts
- Run browser-based mock calls (mic → simulated STT → LLM → TTS) and hear agent replies locally
- Persist session transcripts, per-turn timestamps and events
- Run deterministic QA scenarios (batched) to produce JSON reports, per-turn audio, and a consolidated CSV
- View and play QA reports from a QA Reports page (download CSV/audio)
- Simulate handoffs/transfers in a safe mock mode (Twilio adapter prototype exists but disabled unless keys and opt-in)

Nature & flow (high level)

1. Agent configuration: ops/product create an agent, edit prompts, select voice/persona and providers (mock by default).
2. Start Mock Call: user clicks Start Mock Call in the Playground; browser captures microphone audio and/or the QA runner replays scripted scenarios.
3. Pipeline (mock): STT → LLM → TTS (rule-based or configured provider) → browser playback.
4. Orchestration: backend collects canonical timestamps at each step to compute latencies and persist the turn events to storage.
5. QA: Scenario runner replays scripts, writes per-scenario JSON reports and per-turn WAVs, consolidated CSV for analysis; reports viewable in QA Reports UI.

Key features & functions

- Agent Management: CRUD, prompt versioning, persistence
- Prompt Builder & Templates: edit and save system prompts and campaign templates
- Mock Call Playground: microphone controls, live transcript, browser TTS playback, barge-in behavior (client-side)
- Visualization: microphone & agent audio visualizers, speech activity indicators
- Per-turn Metrics: STT latency, TTFT, TTS first frame, end-to-first-audio, total turn time
- QA Engine: deterministic scenario runner (single/all), per-scenario JSON reports, per-turn WAVs, consolidated CSV
- QA UI: list reports, inline audio players, JSON viewer, CSV download
- Safe adapters: mock providers by default; prototype Twilio transfer adapter disabled unless enabled
- Secure credential storage: encrypted provider keys (SECRET_KEY)

Safety defaults

- Mock-first: No live PSTN/SIP dialing, SMS, or appointment booking unless explicitly configured and authorized.
- Audio retention: optional and disabled by default; QA runner writes artifacts only if TTS is available and enabled.
- Transfer/handoff actions are simulated until real provider integration is explicitly enabled.

Quick demo (how to run locally)

1) Generate QA outputs (create reports + audio):
   - cd backend
   - pip install pyttsx3
   - python backend/scripts/qa_runner.py --all

2) Start backend:
   - export SECRET_KEY="dev-secret"
   - export DATABASE_URL="sqlite:///./dev.db"
   - pip install -r requirements.txt
   - uvicorn backend.app.main:app --reload --port 8000

3) Start frontend:
   - cd frontend
   - npm install
   - npm start

4) Open QA Reports page:
   - http://localhost:3000/qa_reports.html

Where to look in the repo

- Backend main: backend/app/main.py
- QA runner: backend/scripts/qa_runner.py
- QA outputs: backend/qa_outputs/ (reports, WAVs, results_summary.csv)
- QA docs & scenarios: docs/TESTING_QA.md, docs/SCENARIO_TEMPLATES.md, docs/QA_CHECKLIST.md, docs/scenarios/*.json
- QA UI preview: frontend/public/qa_reports.html
- Twilio prototype adapter: backend/app/core/adapters/twilio_adapter.py
- Sample audio generator: backend/scripts/generate_sample_call.py

One-line summary

A safe, local-first AI Voice Bot Playground for creating, testing, and QA'ing voice agents end-to-end (STT → LLM → TTS), producing reproducible scenario-based reports and audio artifacts so teams can validate agents before any live telephony is enabled.
