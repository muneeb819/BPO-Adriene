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

License: MIT
