# LiveKit voice agent

Adriene can run as a **LiveKit agent**: the same dialogue brain, Medicare script,
compliance layer and local neural voice, but reachable from any WebRTC client —
the bundled browser console, a mobile app, or a SIP trunk.

No cloud key is required. With no keys at all the call runs entirely on the
machine: Silero VAD → local Whisper (`faster-whisper`) → the scripted brain →
Piper voice.

```
 browser / phone / SIP
        │  WebRTC
        ▼
  ┌──────────────┐        ┌──────────────────────────────────────────────┐
  │ LiveKit      │◄──────►│ livekit_agent.py (worker, one job per call)  │
  │ room         │  audio │                                              │
  └──────────────┘        │  STT ──► BrainLLM ──► TTS                    │
                          │          (CallSession)  (Piper / cloud)      │
                          │  VAD (silero)  ·  transcripts  ·  saved call │
                          └──────────────────────────────────────────────┘
```

---

## 1. Configure

`.env` (already created for this project; see `.env.example` for a template):

```bash
LIVEKIT_URL=wss://your-project.livekit.cloud
LIVEKIT_API_KEY=APIxxxxxxxxxxxx
LIVEKIT_API_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

LIVEKIT_AGENT_NAME=adriene     # blank = automatic dispatch
```

Get the keys in LiveKit Cloud → **Project → Settings → Keys**.
`LIVEKIT_AGENT_NAME` names this worker: the bundled debug console requests that
name in its join token, so the agent only joins rooms that ask for it. Leave it
blank to have the agent join **every** new room instead.

## 2. Install

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-livekit.txt
.venv/bin/python scripts/fetch_neural_voices.py --voice en_US-amy-low
```

`./start_livekit.sh` does all of the above on first run.

## 3. Run — debug mode

```bash
./start_livekit.sh dev              # macOS / Linux / Git Bash
.\start_livekit.bat                 # Windows (cmd) — same thing
.\start_livekit.ps1                 # Windows (PowerShell)
```

You should see:

```
  Adriene → LiveKit  (dev mode)
  url      wss://your-project.livekit.cloud
  agent    adriene
  stack    stt=faster-whisper  tts=local-neural  llm=brain  vad=True

INFO livekit.agents - starting worker
INFO livekit.agents - registered worker
```

Other entry points:

| command | what it does |
|---|---|
| `./start_livekit.sh` | same as `dev` |
| `python livekit_agent.py start` | production worker |
| `python livekit_agent.py console` | local terminal session, no server, no keys |
| `./start_livekit.sh doctor` (`start_livekit.bat doctor`) | config + network + token + stack diagnosis |
| `python scripts/livekit_smoke_test.py` | offline brain→voice check, writes a WAV |

`console` needs PortAudio (`libportaudio2` / `brew install portaudio`, on Windows
it ships with the driver). On a headless box use the smoke test instead.

> **Windows note:** use `.\start_livekit.bat` from `cmd` (or double-click it).
> From Git Bash, `./start_livekit.sh` works too. If either says *No such file or
> directory*, you are not in the project folder — `cd` into the directory that
> contains `.git` and `livekit_agent.py` first.

## 4. Talk to it — the browser console

With the app running (`./start.sh`, port 8055) open:

```
http://localhost:8055/livekit.html
```

It asks `POST /api/livekit/token` for a join token (signed locally with your
secret), connects the browser straight to LiveKit, publishes the microphone and
renders live transcripts. A text box lets you **type** caller turns — those are
published on the room data channel and fed to the brain exactly like speech, which
is the fastest way to test the script without a mic.

The **Agent stack** card on that page shows what the worker will run with, and
flags missing pieces (no voice, no STT, credentials, server↔LiveKit route).

## 5. How the wiring works

| piece | file | notes |
|---|---|---|
| worker / job entrypoint | `livekit_agent.py` | `AgentServer`, greeting, data-channel text, transfer/hangup, saves every call |
| dialogue brain as LLM | `backend/app/core/adapters/livekit_plugins/brain_llm.py` | one `CallSession` per job; policy decides, never the model |
| local voice as TTS | `.../livekit_plugins/neural_tts.py` | Piper ONNX → 16 kHz PCM → `AudioEmitter` |
| local STT | `.../livekit_plugins/whisper_stt.py` | `faster-whisper`, batch per utterance |
| provider selection | `.../livekit_plugins/factory.py` | env-driven, never crashes on a missing key |
| tokens + status | `backend/app/livekit_endpoints.py` | `/api/livekit/*` |
| browser console | `frontend/public/livekit.html` | `livekit-client` UMD, vendored in `frontend/public/vendor/` |

**The script is the brain.** `BrainLLM` emits exactly what
`CallSession.process_user_utterance()` decides: greeting, Medicare question,
age check, knowledge answers, objections, DNC wording (compliance-owned), repair
ladder, transfer. Language understanding, memory and persona are the same code the
local console uses — a LiveKit call and a browser call behave identically.

**Transfer / hangup.** When the brain sets `pending_action`, the worker waits for
the current sentence to finish playing and then either performs a SIP transfer
(`LIVEKIT_TRANSFER_NUMBER`) or leaves the room. Every call is written to
`data/livekit_calls/<timestamp>-<room>.json` with disposition and qualification
fields.

**Interruptions.** `allow_interruptions=True`: talking over Adriene stops her,
which is the behaviour live callers expect (the same barge-in the browser console
implements manually).

## 6. Upgrading individual legs

Nothing below is required. Set one variable and the matching leg switches:

| env | value | effect |
|---|---|---|
| `LIVEKIT_STT_PROVIDER` | `auto` (default) | cloud key if present, else local whisper |
| | `deepgram` | `DEEPGRAM_API_KEY`, streaming, best latency |
| | `openai` | `OPENAI_API_KEY`, whisper-1 |
| | `faster_whisper` | force local |
| `LIVEKIT_TTS_PROVIDER` | `auto` (default) | ElevenLabs → OpenAI → local Piper |
| | `local` | always the local voice |
| | `elevenlabs` / `openai` | cloud voices |
| `LIVEKIT_LLM_PROVIDER` | `brain` (default) | scripted, compliant, deterministic |
| | `openai` / `anthropic` / `gemini` | free-form model with the script in the prompt (experimental) |

Install the matching plugin, e.g. `pip install livekit-plugins-deepgram`.

Because `auto` only picks a cloud provider when its key **and** plugin exist, a
half-configured box still makes a call.

## 7. Phone calls (SIP)

Inbound: create a SIP trunk + dispatch rule in LiveKit Cloud (or the CLI) that
dispatches the agent into the call's room. Outbound: `CreateSIPParticipant` with
the room name the agent was dispatched to.

Warm transfer to a human: set

```bash
LIVEKIT_TRANSFER_NUMBER=+15551234567
```

When the brain reaches the `AI-TRANSFER` disposition the worker calls
`TransferSIPParticipant` for the SIP participant and leaves. With no number it
ends the call after the transfer line (correct for browser demos).

## 8. Troubleshooting

| symptom | cause / fix |
|---|---|
| `failed to connect to livekit … ssl:default [None]` | the worker's network cannot reach LiveKit. Open outbound TCP 443 to `<project>.livekit.cloud`. Sandboxes/CI often block it — run the worker where LiveKit is reachable. |
| `registered worker` never appears | wrong `LIVEKIT_URL`/key/secret. `./start_livekit.sh doctor` shows which check fails. |
| Browser joins, no agent appears | worker not running, or `LIVEKIT_AGENT_NAME` mismatch. With a name set, the room token must explicitly dispatch it (the bundled console does); blank = automatic dispatch. |
| Agent joins, is silent | no voice installed: `python scripts/fetch_neural_voices.py`. Check the **Agent stack** card. |
| Caller is heard but nothing happens | no STT: install `faster-whisper` or set `DEEPGRAM_API_KEY`. Typed input still works. First whisper run downloads the model (needs internet). |
| `Plugins must be registered on the main thread` | plugin imported from a worker thread. The app preloads plugins at startup (`factory.preload()`); keep imports inside that path. |
| `PortAudio library not found` | `console` mode needs audio hardware: `apt install libportaudio2` / `brew install portaudio`. Use the smoke test on servers. |
| Words are cut off / robotic | lower `LIVEKIT_TTS_SPEED`, or try another voice (`LIVEKIT_TTS_VOICE`). Local low-quality voices (e.g. `en_US-amy-low`) are 16 kHz; fetch a `medium` voice for better audio. |

## 9. Testing

```bash
.venv/bin/python scripts/livekit_smoke_test.py     # brain → voice → WAV (offline)
.venv/bin/python -m pytest backend/tests/test_livekit_agent.py -q
cd backend && ../.venv/bin/python -m pytest tests -q   # full existing suite
```
