# Jessica FE Fronter — Final Expense Voice Fronter

An AI **voice calling agent** that acts as a *fronter* for final-expense (burial/whole-life) leads. It answers the call, qualifies the prospect using a beat-based dialogue, and either **transfers qualified leads to a live closer** (VICIdial queue/agent) or politely logs the disposition and hangs up.

Built in Python, integrates with **VICIdial / Asterisk** via ARI, powered by OpenAI, Anthropic, or local Ollama.

## What it does
- Greets the caller and qualifies them in a **beat-based conversation** (greeting → hook → age check → transfer/close).
- **Eligibility:** age 45–80. A bare "yes" also counts as confirmation.
- If eligible: congratulates them on a **lower monthly premium** and transfers to a licensed agent.
- If not eligible or not interested: graceful close, logs disposition.
- Live dashboard of call results at `http://localhost:8000`.

## Architecture
```
caller → Asterisk → Stasis(jessica-fe-fronter)
                      → ARI client (answer/record/play/redirect)
                      → Conversation engine (LLM + beat-based state machine)
                      → TTS (ElevenLabs v3 Jessica / free neural fallback)
                      → STT (Deepgram)
                      → eligible? redirect to closer queue : hangup + log
                      → VICIdial non-agent API (disposition)
```

Core modules:
- `agent/config.py` – env-driven config (voice settings, thresholds, compliance)
- `agent/llm.py` – OpenAI / Anthropic / Ollama abstraction
- `agent/prompt.py` – Jessica persona + beat instructions (professional, calm, sales-appropriate)
- `agent/qualifier.py` – intent classifier + age extractor (45–80 window)
- `agent/conversation.py` – beat-based state machine (greeting → hook → age → transfer)
- `agent/stt.py`, `agent/tts.py` – speech providers (ElevenLabs v3 primary, console for testing)
- `agent/ari.py` – Asterisk ARI client (answer/play/record/redirect)
- `agent/call_session.py` – channel abstraction (ConsoleChannel / AriChannel)
- `agent/vicidial.py` – dialer disposition logging
- `server/app.py` – FastAPI dashboard + call log
- `scripts/make_demo_recording.py` – generates demo call with Edge neural voices (free)

## Quick start (no PBX needed)
```bash
python -m venv .venv && source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env   # text mode works with zero keys; add voice keys for speech
python cli.py debug    # smoke test + dashboard (/) + interactive debug console (/debug)
```
Then open **`http://localhost:8000/debug`** to chat with (and hear 🎙️) Lina.
Full local + voice guide: [`docs/LOCAL_VOICE_SETUP.md`](docs/LOCAL_VOICE_SETUP.md).

Other commands:
```bash
python cli.py demo     # scripted smoke test using built-in mock LLM
python cli.py console  # interactive terminal role-play
python cli.py stream   # synthesize 3 Lina lines → saves data/stream_test/*.mp3 (needs ELEVENLABS_API_KEY)
python cli.py serve    # production-style server (dashboard + ARI listener)
```

## Verify everything works
Three independent levels, cheapest first:
```bash
python -m pytest tests/ -q          # 136 unit/integration tests (tmp stores, no server needed)
python scripts/healthcheck.py       # readiness report (deps, keys, config, live server)
python scripts/smoke_test.py        # full-feature end-to-end test against a running server
```
`smoke_test.py` drives **every** HTTP endpoint and **every** disposition path —
all four campaigns, the age boundaries, DNC/busy/unclear, campaign create →
edit → deploy → delete, insights, CSV/JSON exports, recordings (incl. a path
traversal guard) and the real-time voice WebSocket. It needs the server up:

```bash
python cli.py debug                 # terminal 1
python scripts/smoke_test.py        # terminal 2  → "ALL 127 CHECKS PASSED"
python scripts/smoke_test.py --url http://localhost:9000 --no-ws
```

## Live deployment (VICIdial + Asterisk)
1. Enable ARI in `ari.conf` (`enabled = yes`, create `ari` user with read/write).
2. Route your inbound final-expense DID to the Stasis app — see `configs/extensions.conf.example`.
3. Create a `closers` queue (or set `CLOSER_EXTENSION`) in VICIdial for handoff.
4. Set `.env`: `LLM_PROVIDER`, keys, `STT_PROVIDER=deepgram`, `TTS_PROVIDER=elevenlabs`,
   `ARI_*` and `VICIDIAL_*`.
5. Run: `python cli.py serve` (starts web dashboard + ARI listener).

## Voice studio (library, design, matching, remix)

The dashboard's **🎙 Voice studio** turns voice settings into saved, reusable
voices:

* **Library** — add a voice by uploading a file, pointing at a file on the
  server, or recording 8-20 s with the browser microphone; preview it, press
  **Use** and the bot speaks with it (the choice survives a restart).
* **Prompt to voice** — type *"warm older British man, speaks slowly"* and the
  studio picks the resident neural voice that fits, stores the tuning the words
  imply and shows you the reasoning word by word. Free and offline; the
  ElevenLabs Voice Design path generates a brand-new voice from the description
  when `ELEVENLABS_API_KEY` is set and says exactly how to enable it when it
  is not.
* **Matching** — feed it a recording and it measures pitch/brightness, names the
  resident neural voice that is closest (**Check match**) and renders the
  recording on that voice, so a clone or a blend can be judged by ear on a
  machine with no ChatterBox weights and no cloud key.
* **Remix** — pick two or more voices and combine them (weighted blend or
  stitched with a crossfade), optionally re-matching the result to a neural voice.

| | |
|---|---|
| API | `GET/POST /api/voices`, `PUT/DELETE /api/voices/{id}`, `POST /api/voices/{id}/apply`, `POST /api/voices/{id}/preview`, plus `/api/voices/upload`, `/design`, `/describe`, `/match`, `/design/providers`, `/design/elevenlabs`, `/remix`, `/current`, `/preview-draft` |
| Storage | `data/voices/library.json` + one normalised 24 kHz mono wav per reference |
| Docs | [VOICE_STUDIO.md](VOICE_STUDIO.md) — full reference, limits and verification |
| Offline | `scripts/fetch_neural_voices.py` + `scripts/neural_tts_server.py` — real neural voices, no keys, no torch, no multi-GB downloads |

```bash
.venv/bin/python -m pytest tests/ -q                                          # 353 tests
.venv/bin/python scripts/verify_voice_studio.py --url http://localhost:8000   # 49 live checks
```

## Voice configuration

### ChatterBox (local, clonable, 23 languages)

Set `TTS_PROVIDER=chatterbox` (the dashboard then grows a **ChatterBox voice
control** panel). Requires `pip install chatterbox-tts` and torch; the models
are ~2–3 GB and load on first use.

| Feature | How to use it |
|---------|---------------|
| Model switching | `POST /api/tts/switch-voice {"model":"turbo"\|"nano"\|"multilingual"\|"original"}` or the panel's **Model** dropdown. `turbo`/`nano` are fastest, `multilingual` speaks 23 languages, `original` is the reference model |
| Language | `{"language":"es"}` (multilingual only — the English-only engines return `400`). Codes with names come back in `available_languages` |
| Voice cloning | `{"voice":"C:/voices/agent_ref.wav"}` (10s+ of clean speech); `{"voice":""}` clears it back to the built-in voice. Missing/unreadable paths are rejected with the resolved absolute path |
| Fields combine | `{"model":"multilingual","language":"es","voice":"…wav"}` applies all three in one request and echoes them back under `applied` |
| Paralinguistic tags | `[laugh] [chuckle] [cough] [sigh] [gasp] [yawn] [um]` — clickable chips in the panel insert them into the test line (turbo/nano only) |
| Expressiveness | `CHATTERBOX_EXAGGERATION`, `CHATTERBOX_CFG_WEIGHT`, `CHATTERBOX_TEMPERATURE` in `.env` |
| Output format | ChatterBox synthesises at 24 kHz; the pipeline resamples to the 16 kHz mono the call path needs |

Robustness: every request is validated (`400` + the list of valid values, never
a `500`), an unavailable engine fails over to the standby instead of dropping
the call, and `/health` plus `GET /api/tts/voices` always report which engine is
speaking, the configured model/language, and why a failover happened.
Verified by `tests/test_chatterbox_tts.py`, `scripts/verify_chatterbox.py` and
the dashboard test panel (`POST /api/debug/speak`).

### Primary: ElevenLabs v3 (cloud, expressive)
| Setting | Value | Purpose |
|---------|-------|---------|
| `ELEVENLABS_VOICE` | `Jessica` | warm, young female voice (maps to ID `cgSgspJ2msm6clMCkdW9`) |
| `ELEVENLABS_MODEL` | `eleven_v3` | expressive v3 model |
| `ELEVENLABS_STABILITY` | `0.5` | balanced stability |
| `ELEVENLABS_SIMILARITY_BOOST` | `0.75` | consistent speaker |
| `ELEVENLABS_STYLE` | `0.3` | natural expressiveness |
| `ELEVENLABS_SPEAKER_BOOST` | `true` | enhanced clarity |

Free fallback: `python cli.py stream` uses Edge neural voices (`AriaNeural`) if no ElevenLabs key.

### Standby: Kokoro (self-hosted, free, no key)

Kokoro is a local TTS server you run on the same machine. It speaks at 24 kHz,
so the bot resamples to the 16 kHz the voice pipeline needs and streams it
through the same barge-in path as ElevenLabs.

| Setting | Default | Purpose |
|---------|---------|---------|
| `TTS_STANDBY_PROVIDER` | `kokoro` | enables automatic failover |
| `KOKORO_BASE_URL` | `http://localhost:8880` | where the Kokoro server listens |
| `KOKORO_VOICE` | `af_bella` | warm female voice |
| `KOKORO_SPEED` | `1.0` | speech rate |
| `KOKORO_TIMEOUT` | `30` | seconds to wait for synthesis |

**Failover behaviour** — keep one engine active and the other on standby:

- ElevenLabs is primary; Kokoro is standby. Flip `TTS_PROVIDER`/`TTS_STANDBY_PROVIDER` to swap them.
- If the active engine fails *before* any audio has played, the bot switches to the
  standby for that utterance, promotes it, and keeps using it while the primary
  stays down — the call is never dropped.
- If it fails *after* the customer has already heard part of the line, the bot
  stops rather than replaying the sentence from the top.
- Health is remembered for 60 seconds, so a discovered outage is not re-hit on
  every call; once the record goes stale the primary is retried automatically.
- `/health` reports `tts_primary`, `tts_standby`, `tts_active`, the failover reason
  and each engine's reachability.

Run it (installs ~1.5-2 GB of CPU torch + weights on first run):

```powershell
.\start-kokoro.ps1            # install (first run) + start on :8880
.\start-kokoro.ps1 -Stop      # stop it
```

Then open http://localhost:8880 in a browser for Kokoro's own web player.


### Hearing the bot with no keys (offline neural voices)

If the machine cannot download the multi-GB Kokoro/ChatterBox models (or has no
keys), run the bundled **Piper/ONNX neural server**. It speaks the same
OpenAI-compatible wire format, so the dashboard's *Test voice*, the debug
console's live call and the call recordings all work unchanged — and the audio is
a real neural voice, so a clone or a blend can be judged by ear rather than
guessed at.

```bash
.venv/bin/pip install piper-tts onnxruntime                        # once
.venv/bin/python scripts/fetch_neural_voices.py                    # 3 voices, ~60 MB each
.venv/bin/python scripts/neural_tts_server.py --port 8880 --warm   # then PIPER_BASE_URL=http://localhost:8880
curl -s localhost:8880/health | python -m json.tool                # what is loaded
```

`TTS_PROVIDER=piper` uses it as the live voice; `TTS_STANDBY_PROVIDER` can point
at another engine for automatic failover. Renders take ~0.3 s per line on CPU.

**Can't hear anything?** Two browser-side causes are handled for you now:
*Test voice* in the dashboard plays the clip immediately (no second click), and
the debug console resumes the page's `AudioContext` — Chrome starts it
*suspended* when it is created outside a click handler, which plays the call
silently. Click anywhere on the page to unlock sound. Every call is also saved
under **Call recordings**: press play there to hear the exact bytes the bot sent.

## Configuration highlights (env)
| Key | Purpose |
|-----|---------|
| `MIN_AGE` / `MAX_AGE` | fallback eligibility window (default 50–85); **each campaign's own per-step ages govern the live call** |
| `POSITIVE_CONFIDENCE_THRESHOLD` | transfer threshold (default 0.85) |
| `CLOSER_QUEUE` / `CLOSER_EXTENSION` | where qualified leads go |
| `VICIDIAL_URL` / `VICIDIAL_USER` / `VICIDIAL_PASS` | disposition logging |
| `CALLING_HOUR_START` / `CALLING_HOUR_END` | compliance window (default 8–21) |

## Docs
- [Local + Voice Setup](docs/LOCAL_VOICE_SETUP.md) — download, install, free voice keys, talk to Lina in 5 minutes.
- [Budget Cost Structure + scientific diagrams](docs/BUDGET_COST_STRUCTURE.md) — per-call economics, monthly budgets (Pilot/Growth/Scale), architecture & call-flow diagrams.

## Notes
- The live media path is **turn-based** (bot speaks, records caller until DTMF/timeout, then transcribes). For full-duplex low-latency audio, replace `agent/ari.py` with an ARI `externalMedia`/RTP bridge (see `agent/ari_streaming.py`).
- Map VICIdial status codes (`Q`, `NI`, `INC`, `QC`) to your status categories in Admin → Status Categories.