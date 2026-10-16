# BPO-Adriene — AI Voice Bot Scaffold

This branch provides a local-debug scaffold for the AI Voice Agent Bot MVP. It uses a Python FastAPI backend and a simple React frontend. Everything runs locally (no ViciDial integration) for development and testing.

Goals:
- Run the call-flow state machine locally
- Simulate STT by typing user responses in the frontend
- Bot responses are spoken by the browser (SpeechSynthesis) and shown in the UI
- Trigger a "transfer" action when eligibility is confirmed

Run locally (requires Docker & Docker Compose) — or run backend and frontend separately.

With Docker (recommended):

1. docker-compose up --build
2. Open http://localhost:3000

Or run locally without Docker:
- Backend: cd backend && pip install -r requirements.txt && uvicorn app.main:app --reload --port 8000
- Frontend: cd frontend && npm install && npm start

Next steps (coming PRs / work):
- Integrate streaming STT & TTS providers (Deepgram, Google, ElevenLabs)
- Add SIP / Twilio or direct SIP handler for media bridging
- Implement LLM prompt templates & safety/compliance checks
- Implement recordings storage, analytics, and labeling UI

## QA Runner

Deterministic scenario runner for local QA (mock STT/LLM/TTS, no telephony):

```bash
# run every scenario under docs/scenarios/ and write a consolidated CSV
python backend/scripts/qa_runner.py --all

# or a single scenario
python backend/scripts/qa_runner.py --scenario docs/scenarios/cooperative_caller.json
```

Outputs land in `backend/qa_outputs/`:

- `report_{scenario_id}.json` — per-turn transcript, latency metrics and pass/fail checks
- `{scenario_id}_turn_{n}_agent.wav` — per-turn agent audio (requires `pyttsx3`, Windows)
- `results_summary.csv` — one row per scenario when running `--all`

With the backend and frontend running, browse them at http://localhost:3000/qa_reports.html
or call the API directly:

- `GET /api/qa/reports` — list reports and artifacts
- `GET /api/qa/report/{filename}` — a report JSON
- `GET /api/qa/download/{filename}` — stream audio / CSV artifacts

Scenario inputs live in `docs/scenarios/*.json` (see `docs/SCENARIO_TEMPLATES.md`).
The frontend dev server proxies `/api` and `/ws` to `http://localhost:8000`; override with
`PROXY_TARGET=http://localhost:<port> npm start` if the backend runs elsewhere.

License: MIT
