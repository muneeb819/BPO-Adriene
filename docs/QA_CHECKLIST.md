# QA Checklist and Acceptance Mapping

This checklist maps the project acceptance criteria to specific tests or evidence locations.

Instructions: For each criterion, run the linked test(s) and record the result as Passed / Failed / Blocked. Attach evidence: transcript excerpts, session IDs, timestamps, screenshots, or log extracts.

1. I can create an AI agent, configure its prompt, and save a persistent version.
- Test: Agent CRUD test (UI + API)
- Evidence: Agent ID, prompt version ID, GET /api/agents/{id} response

2. I can upload a supported campaign document and retrieve relevant information during a test conversation.
- Test: KB upload + RAG retrieval smoke test
- Evidence: Uploaded file ID, retrieval excerpt shown in session

3. I can define dynamic variables and see them correctly injected into a mock session.
- Test: Dynamic variable injection during mock call
- Evidence: Transcript shows resolved variables, session context payload

4. I can start a browser-based voice session, speak through my microphone, and hear an actual AI-generated spoken response.
- Test: Manual mock call in Playground (Start Mock Call + microphone)
- Evidence: session_id, screenshots, audio playback observed

5. The transcript displays recognized user speech and agent responses with timestamps.
- Test: Transcript validation per-turn
- Evidence: GET /api/calls/{id} transcript JSON

6. Audio visualizers reflect actual microphone and agent output activity.
- Test: Visualizer inspection during live session
- Evidence: Screen recording or screenshots showing waveforms tied to speech

7. Interrupting the agent works, and the relevant barge-in events are recorded.
- Test: Run Interrupting Caller scenario
- Evidence: session event logs showing barge_in_detected, cancelled playback

8. Latency measurements are captured from real timestamps wherever supported.
- Test: Measure per-turn timestamps for STT/LLM/TTS
- Evidence: per-turn metric JSON and a short summary report

9. Tool calls execute against working test implementations or clearly identified simulations.
- Test: Tool call simulation (lookup_customer, calendar_book_slot)
- Evidence: Tool execution logs and simulated response payloads

10. Automated scenarios run and produce evidence-based evaluation reports.
- Test: Run 3 scenarios via QA runner
- Evidence: results/report.json and exported CSV of scorecards

11. I can inspect session traces, replay available audio, identify failures, and export results.
- Test: Session replay for scenario run
- Evidence: replay link or saved audio, transcript alignment, exported JSON

12. I can compare prompt versions and repeat tests against the same scenarios.
- Test: Run same scenario against two prompt versions and compare scorecards
- Evidence: diff of scorecards and prompt version IDs

13. Configuration, session history, and evaluation results persist after refreshing or restarting the application.
- Test: Restart backend and verify session presence
- Evidence: Sessions list contains previous runs

14. Automated tests cover the critical backend, audio-session, tool, and evaluation workflows.
- Test: Run pytest and scenario smoke tests in CI (mocks only)
- Evidence: CI run logs, pytest output

15. No mock session can accidentally place a live telephone call or execute an unauthorized external action.
- Test: Verify default providers are mock and transfer provider is mock by default
- Evidence: config defaults and test run shows no external calls

16. The project starts using documented setup instructions and can be deployed to the selected hosting environment.
- Test: Follow README deployment steps (local or staging)
- Evidence: Deployed instance URL or successful local run logs


-- End of checklist --
