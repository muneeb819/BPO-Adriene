# QA Runner — demo script & verified results

A 2-minute walkthrough you can give anyone to show what this project does.

## Run it (3 commands)

```bash
# 1) generate QA outputs (reports, per-turn audio, CSV)
python backend/scripts/qa_runner.py --all

# 2) backend — serves API, WebSocket, the dashboard UI and the QA page on one origin
cd backend && uvicorn app.main:app --port 8000     # from repo root: uvicorn backend.app.main:app

# 3) optional: frontend dev server with hot reload (HTTP API proxied, use the backend URL for live calls)
cd frontend && npm install && npm start
```

Open **http://localhost:8000/** for the dashboard and
**http://localhost:8000/qa_reports.html** for QA reports.

> Note: `cli.py serve` / `start.*` launch `server/app.py` (the bigger app: campaigns,
> voice studio, live voice console). It serves the QA page too, at `/qa_reports.html`.

## Demo script

1. **Dashboard** (`/`) — runtime config panel (STT/TTS/LLM providers), recent calls table.
2. **Start Mock Call** — the bot greets you over a WebSocket. Type short answers:
   - `yes` → "Thanks. Are you between 45 and 80 years old?"
   - `67` → eligible → **"I have a Benefits Specialist who is waiting…"**
   - the UI logs `system: Transfer triggered`, the call row turns `transferring`,
     and the full transcript is saved and shown in *Call Detail*.
3. **QA Reports** (`/qa_reports.html`) — every scenario report, inline audio players for
   each agent turn, per-turn links, and **Download consolidated CSV**.
4. **Re-run the suite**: `python backend/scripts/qa_runner.py --all` (takes ~10 s,
   regenerates reports + WAVs + `results_summary.csv`).

## What the runner produces (`backend/qa_outputs/`)

| File | Contents |
|---|---|
| `report_{scenario_id}.json` | per-turn transcript, latency metrics, assertion results |
| `{scenario_id}_turn_{n}_agent.wav` | agent audio for each turn (pyttsx3) |
| `results_summary.csv` | one row per scenario: turns, checks passed, overall_pass |

Latency metrics captured per turn: `stt_latency`, `ttft`, `tts_latency`,
`end_to_first_audio`, `total_turn_time` (see `docs/TESTING_QA.md` §5).

Assertions evaluated: `must_contain`, `age_extraction`, `transfer_requested`,
`clarification_requested`, `barge_in_handled`.

## Latest verified run — 2026-10-16

| Scenario | Turns | Checks | Result |
|---|---|---|---|
| Cooperative Caller | 3 | 3/3 | PASS |
| Confused Caller | 3 | 2/2 | PASS |
| Interrupting Caller | 3 | 3/3 | PASS |

End-to-end checks (automated smoke, then live in the browser):

- `POST /api/call/start` → 200 · `WS /ws/call/{id}` → prompts and replies flow
- `yes` → `67` → `transfer_triggered`, call state `transferring`, 5 transcript rows persisted
- `/`, `/qa_reports.html`, `/api/qa/reports`, `/api/qa/download/*.csv|.wav` → all 200
- path-traversal attempts on `/api/qa/download/…` → 404
- browser console: **0 errors**

## API

- `GET /api/qa/reports` — list reports + artifacts
- `GET /api/qa/report/{filename}` — full report JSON
- `GET /api/qa/download/{filename}` — stream audio / CSV (traversal-guarded)

Scenario inputs live in `docs/scenarios/*.json` — see `docs/SCENARIO_TEMPLATES.md`
for the format and how to add your own.
