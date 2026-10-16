# Testing & Quality Assurance — AI Voice Bot Playground

This document is an additional, non-invasive Testing & QA section for the project. It does not change runtime code — it describes test plans, scenario templates, metrics, and how to validate the system using the existing local-first scaffold. Additions are documentation-only and safe to review without modifying the live application.

Purpose
- Provide a structured QA program for agent configuration, mock-call behavior, metrics collection, and acceptance testing.
- Keep testing isolated in the playground (no live telephony by default). All transfer/handoff calls are simulated unless explicitly enabled in a separate deployment.

Contents
- Test types and scope
- Test environment and setup (local-first)
- Scenario templates and sample scripts
- Automated scenario runner (how to simulate)
- Metrics and latency diagnostics to collect
- Evaluation & scorecard definitions
- Acceptance mapping (criterion -> test)
- Troubleshooting checklist and runbook

1) Test types
- Unit tests: backend model logic, state machine, adapter factories (already included under backend/tests).
- Integration tests: end-to-end mock-call flows using the WebSocket mock media bridge and the mock STT/TTS/LLM adapters.
- Scenario tests: deterministic scripted conversations executed by the Scenario Runner.
- Load / stability tests: simulated parallel mock sessions (optional, later stage).
- Security & safety tests: ensure credential handling, no outbound side effects in mock mode.

2) Test environment (local-first)
- The testing docs and scripts assume you run the existing feature/ai-voice-bot-scaffold branch locally.
- Required env variables for local QA:
  - SECRET_KEY (any dev secret) — required for encrypted keys handling
  - DATABASE_URL (default sqlite://./dev.db) — can be swapped to SQL Server/Postgres for integration testing
- Start services as described in README. All tests should run without adding real provider keys.

3) Scenario templates
- See docs/SCENARIO_TEMPLATES.md for a set of deterministic scenarios (Cooperative Caller, Confused Caller, Interrupting Caller, Background Noise, Repeats/Change Answers). Each scenario contains:
  - Name, ID, description
  - Initial variables (dynamic placeholders)
  - Scripted user utterances with timings
  - Expected agent utterances or decision points
  - Pass/fail assertions

4) Running scenarios (manual & automated)
- Manual: Use the web UI Playground (Start Mock Call) and follow the scripted utterances from the scenario template.
- Automated (scripted runner): A simple Python runner can connect to the websocket endpoint, replay user utterances with timing, capture transcripts and events, and generate a result JSON for the scenario.

Example: pseudo-command to run scenario runner (script located in backend/scripts/qa_runner.py — implement if desired):

  python backend/scripts/qa_runner.py --scenario docs/scenarios/cooperative_caller.json --out results/report.json

The runner should:
- Create or reuse an agent instance and prompt version
- Start a mock session via /api/call/start
- Open the websocket at /ws/call/{session_id}
- Send user_utterance messages with scenario timing
- Capture all websocket messages and timestamps
- Evaluate pass/fail assertions
- Store a structured session record and scorecard

5) Metrics & diagnostics to collect per-turn
- timestamps:
  - user_speech_start (client VAD timestamp)
  - user_speech_end (end of utterance)
  - stt_first_token (when STT returns first token / partial)
  - stt_complete (STT completes transcript)
  - llm_first_token (TTFT)
  - llm_complete
  - tts_first_audio_frame
  - tts_playback_start
  - tts_playback_end
- derived metrics:
  - stt_latency = stt_first_token - user_speech_end
  - ttft = llm_first_token - user_speech_end
  - tts_latency = tts_first_audio_frame - llm_complete
  - end_to_first_audio = tts_playback_start - user_speech_end
  - total_turn_time = tts_playback_end - user_speech_start
- Capture status flags: barge_in_detected, playback_cancelled, tool_call_count, errors

6) Evaluation & scorecard
- Define weighted criteria: goal_completion (40%), required_questions (25%), correct_extractions (15%), latency_penalty (10%), safety_violations (10%). We will provide a sample scorer script that reads session events and applies these weights to produce a scorecard.
- Critical violations (e.g., prohibited phrase used, unauthorized transfer) mark the scenario as FAILED regardless of numeric score.

7) Acceptance mapping (from your acceptance criteria)
- Map each acceptance item to a test and test evidence location. See docs/QA_CHECKLIST.md for the full mapping and checkboxes to mark Passed / Failed / Blocked.

8) Data retention & privacy
- By default tests store only synthetic data and mock transcripts. Configure raw audio retention via env variable TEST_AUDIO_RETENTION (default: false) to avoid storing audio during initial testing.
- The testing docs show how to redact or delete session audio and how to export anonymized transcripts.

9) Troubleshooting runbook
- Common failure causes and console logs to inspect
- Steps to repro, collect logs, capture full websocket transcript and event JSON, and escalate

10) Next steps (optional automation)
- Implement backend/scripts/qa_runner.py to execute scenarios automatically and persist results
- Add GitHub Actions workflow to run a small set of scenario smoke tests on PRs (mocks only) to validate regressions
- Add UI to list test reports and compare runs between prompt versions

Files added (this doc set):
- docs/TESTING_QA.md  (this file)
- docs/SCENARIO_TEMPLATES.md
- docs/QA_CHECKLIST.md

If you approve, I will not change any runtime code. This is documentation-only and can be extended with runner scripts upon your confirmation.
