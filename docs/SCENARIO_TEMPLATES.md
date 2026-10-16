# Scenario Templates — AI Voice Bot Playground

This file contains deterministic scenario templates you can run manually or automate with a scenario runner.

1) Cooperative Caller (ID: scenario_coop_001)
- Description: Cooperative prospect who answers clearly and follows script.
- Variables:
  - {{customer_name}}: John Doe
  - {{campaign_name}}: Medicare Outreach
- Script (utterances with relative timing in seconds):
  - 0.0s: "Hello"
  - 0.8s: "Yes I have Medicare"
  - 1.5s: "I'm 67"
- Expected outcomes:
  - Agent asks about Medicare and age
  - Agent marks qualification as eligible
  - Agent initiates simulated transfer (mock)
- Pass assertions:
  - Transcript contains "Medicare" and age extraction = 67
  - TransferRecord.status == 'completed' (mock)

2) Confused Caller (ID: scenario_confused_001)
- Description: Caller unsure about whether they have Medicare.
- Script:
  - 0.0s: "Hi"
  - 1.0s: "I'm not sure what Medicare is"
  - 2.0s: "I think I might have it"
- Expected outcomes:
  - Agent asks clarifying questions, provides safe short clarification (from approved responses)
  - No unauthorized claims are made
- Pass assertions:
  - Agent used approved clarification wording
  - No unsupported claims detected

3) Interrupting Caller (ID: scenario_interrupt_001)
- Description: Caller frequently interrupts during agent speech.
- Script:
  - 0.0s: "Hello"
  - 0.7s: (while agent speaks) "Wait, I have a question"
  - 1.2s: "I'm 65"
- Expected outcomes:
  - Agent playback is cancelled on barge-in
  - Agent listens and processes the new utterance
  - Agent resumes with correct next question or flow
- Pass assertions:
  - barge_in_detected == true
  - No crash or hang; session completes or reaches an expected state

4) Background Noise (ID: scenario_noise_001)
- Description: Caller with background noise in the audio asset. Use test audio files for noise simulation.
- Script:
  - 0.0s: audio file containing speech+noise (pre-recorded)
- Expected behavior:
  - STT may produce lower confidence; system should request clarification when confidence below threshold
- Pass assertions:
  - Agent requests clarification when STT confidence < threshold

Notes
- Each scenario file should be a JSON document containing: metadata, variable values, an ordered list of utterances (text or path to audio), relative timings, and assertion definitions.
- Store scenarios under docs/scenarios/ as JSON files. Use deterministic timing to ensure reproducible runs.
