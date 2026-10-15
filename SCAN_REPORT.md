# Deep Scan & Debug-Mode Test Report

**Project:** BPO Adriene — AI Voice Fronter (multi-campaign outbound voice qualification bot)
**Branch:** `arena/181ae36f-bpo-adriene` (from `main` @ `ae095b6`)
**Date:** 14 October 2026
**Run mode:** `python cli.py debug` on `0.0.0.0:8000` (no PBX, no API keys)

---

## 1. Result at a glance

| Verification | Command | Result |
|---|---|---|
| Unit / integration suite | `python -m pytest tests/ -q` | **136 passed** |
| End-to-end feature test (new) | `python scripts/smoke_test.py` | **ALL 127 CHECKS PASSED** |
| Readiness report | `python scripts/healthcheck.py` | 26 PASS, 2 expected FAIL (no voice keys) |

**6 defects found and fixed**, including one security issue (a broken `.gitignore`
that left `.env` — and every API key in it — unprotected) and two that could take
a live call down (`/ws/voice` unusable with no keys, and an empty campaign
returning HTTP 500 / killing the call).

---

## 2. What the project actually is (verified, not assumed)

A FastAPI app that runs a **beat-based voice qualification state machine** over
either a terminal, a WebSocket (debug console), or Asterisk ARI (production).

```
browser mic ─┬─────────────────────────────► /ws/voice  (VoiceSession)
             │                                   │
             │                        ┌──────────┴──────────┐
             │                   Deepgram STT          Conversation engine
             │                  (optional now)                │
             │                                            Campaign (JSON)
             │                                                  │
             │                                           TTS facade ──► PCM16 @16 kHz
             └◄──── base64 audio + JSON control ◄──────────────┘   (failover primary/standby)
```

**Module map (all read):**

| Layer | Files | Role |
|---|---|---|
| Config | `agent/config.py` | `.env`-driven dataclass; ~70 settings, 20+ providers |
| Conversation | `agent/conversation.py`, `agent/qualifier.py`, `agent/prompt.py` | Campaign gate engine + strict yes/no, age, keyword, DNC/busy classifiers |
| Campaigns | `agent/campaign.py` (24 KB) | `Campaign`/`Question` schema, JSON store, 4 built-ins, CRUD + migration |
| Intelligence | `agent/brain.py`, `agent/llm.py`, `agent/memory.py`, `agent/mock_llm.py` | Intent/sentiment, 4 LLM providers (gemini/openai/anthropic/ollama), self-learning few-shot store |
| Voice | `agent/tts.py` (28 KB), `agent/resample.py`, `agent/stt.py`, `agent/voice_ids.py` | TTS facade w/ failover, 24→16 kHz streaming resampler |
| Channels | `agent/call_session.py`, `agent/ari.py`, `agent/ari_streaming.py`, `agent/voice_agent.py` | Console / ARI / streaming transports |
| Server | `server/app.py` (67 KB), `server/voice_ws.py` | 30+ endpoints, 3 HTML pages, barge-in WebSocket |
| Tooling | `scripts/healthcheck.py`, `scripts/smoke_test.py`, `cli.py` | Readiness, E2E test, CLI |

**Four built-in campaigns, all verified end-to-end** — Medicare (45–90),
Final Expense (50–85), ACA (18–64), Hospital Indemnity (50–85) — routed by a
data-driven grammar (`next | transfer | clarify | end:DISPOSITION`), so nothing
about any campaign is hardcoded in the engine.

**Dispositions verified live:** `AI-TRANSFER`, `AI-NO-MEDICARE`,
`AI-MEDICARE-REFUSED`, `AI-NOT-ELIGIBLE`, `AI-UNCLEAR`, `AI-DNC`, `AI-BUSY`,
`AI-NOT-INTERESTED`, `AI-REFUSED`, `AI-HAS-INSURANCE`, `AI-RECENT-HOSPITAL`.

---

## 3. Defects found and fixed

### 🔴 1. `.gitignore` was UTF-16LE — no ignore rules were in effect (security)
`.gitignore` was encoded UTF-16LE (the Windows PowerShell `>` / `Out-File`
default). Git cannot parse UTF-16, so **every rule silently did nothing**:

```
$ git check-ignore -v .env      # (no output, exit 1 = NOT ignored)
$ git status --porcelain --ignored | grep -c '^!!'   → 1   (should be dozens)
```

`.env` — holding `ELEVENLABS_API_KEY`, `DEEPGRAM_API_KEY`, `ARI_PASSWORD`,
`VICIDIAL_PASS` — was **untracked but not ignored**, so a routine `git add .`
would have committed live credentials to the repository. `.venv/` (hundreds of
MB), `__pycache__/`, and call recordings were equally exposed.

**Fix:** re-encoded `.gitignore` to UTF-8 (content unchanged, 1148 → 572 bytes).
Now verified: `.env`, `.venv/`, `__pycache__/`, `data/recordings/` all report
`IGNORED ✓`.

### 🟠 2. The voice console was dead in keyless debug mode
`VoiceSession.run()` called `_connect_deepgram()` unconditionally and `return`ed
on failure. With no `DEEPGRAM_API_KEY` (i.e. plain debug mode) the very first
WebSocket frame was an error and the socket closed — despite the README
advertising typed input as the zero-key fallback:

```
RECV: {"type":"error","message":"Deepgram failed: "}
ConnectionClosedError: no close frame received or sent
```

**Fix (`server/voice_ws.py`):** live transcription is now optional. Deepgram is
only dialled when `STT_PROVIDER=deepgram` *and* a key exists; otherwise the
session sends a friendly `notice` frame and runs in typed-only mode.
`_read_deepgram()` no-ops when there is no socket. The console UI renders
`notice` as an ℹ️ line (`server/app.py`).

**Verified:** the whole call now completes over the WebSocket with no keys —
`notice → agent_say → speech_start/end → … → {"type":"result","action":"transfer",
"disposition":"AI-TRANSFER"}`.

### 🟠 3. A campaign with no verification steps crashed the engine
Creating a campaign and deleting all its steps (the state the shipped
`mynewcampaign` record is in) produced an unhandled exception on the first
caller turn:

```
File "agent/conversation.py", line 240, in _ask
    q = self.campaign.questions[idx]
IndexError: list index out of range   →  HTTP 500 / dropped live call
```

**Fix (`agent/conversation.py`):** `_ask()` bounds-checks its index and routes
to a new `_no_steps()` that logs a loud warning, speaks the campaign's closing
line, and ends the call as `AI-UNCLEAR`. An unscreened lead is deliberately
**not** transferred to a closer.

Note: the API intentionally still *allows* deploying an empty campaign
(`tests/test_api.py::test_deploy_makes_campaign_live` relies on it), so the fix
belongs in the engine — which is where it was made.

### 🟡 4. Blank campaign lines produced total silence
`dict.get(key, default)` returns `""` when the key exists but is empty — and the
dashboard editor makes it easy to save empty fields. A call on such a campaign
(and the busy/DNC/close paths of any campaign) would have the bot say *nothing*.

**Fix (`agent/conversation.py`):** new `_line()` helper treats blank/whitespace
as absent; applied to `greet`, `busy_close`, `transfer_handoff`, and
`not_interested_close`.

### 🟡 5. `healthcheck.py` probed the wrong port
`--url` defaulted to a hardcoded `http://localhost:9000`, so on the project's own
default (`PORT=8000`) the report claimed **"server /health — not running"** even
with the server up. **Fix:** the default now follows `Config.load().port`.

### 🟢 6. `python cli.py console` crashed on closed stdin
Piped/Ctrl-D input raised `EOFError` out of `ConsoleChannel.listen()` and dumped
a traceback. **Fix:** EOF is treated as the caller hanging up (clean exit).

---

## 4. Live test evidence

**Conversation engine — age boundaries (Medicare, 45–90):**
```
45 → AI-TRANSFER      90 → AI-TRANSFER
44 → AI-NOT-ELIGIBLE  91 → AI-NOT-ELIGIBLE
```
**Compliance branches:** DNC honoured at any turn → `AI-DNC`; busy → `AI-BUSY`;
refusal → `AI-MEDICARE-REFUSED`; 3 unanswered clarifications → `AI-UNCLEAR`
(never guesses); "I have Blue Cross" → clarification, not a false YES.

**Campaign manager:** create (clone/scratch) → edit → **save round-trip
persisted** → **deploy changed live behaviour** (max age edited to 70 made an
age-75 caller `AI-NOT-ELIGIBLE`; reset restored 45–90) → delete guards enforced
(built-in 409, live 409) → delete works when not live.

**Also verified:** CSV exports carry a UTF-8 BOM and `Content-Disposition`
attachment; recordings serve valid `RIFF` WAV; `/api/recordings/..%2F..%2Fetc%2Fpasswd`
is contained (404); self-learning insights report 50+ recorded calls with
outcome weighting (`avg_quality 0.94`).

---

## 5. What I could *not* test here (and what you must do)

| Limitation | Why | What to do |
|---|---|---|
| **No voice audio** | No `ELEVENLABS_API_KEY`; no `espeak`/`ffmpeg`/`torch` in the sandbox for a local engine | Add an ElevenLabs or Deepgram key, or run Kokoro locally (`start-kokoro.ps1`) |
| **No mic STT** | Needs `DEEPGRAM_API_KEY` | Add the key; typed input works meanwhile |
| **No LLM wording variation** | No Gemini/OpenAI/Anthropic key → falls back to the compliance script (by design) | Paste `GEMINI_API_KEY` |
| **No ARI / VICIdial transfer** | No Asterisk PBX to dial | Point `ARI_*` at a real PBX; `agent/ari.py` is untested against a live switch |
| **Not a real browser mic test** | Headless sandbox | Open `/debug` and click **Start call** — barge-in, VAD threshold and latency need a human |

---

## 6. Findings left as-is (product decisions, not defects)

1. **Calling-hours compliance is configured but never enforced.**
   `CALLING_HOUR_START`/`CALLING_HOUR_END` (default 8–21) are parsed into
   `Config` and read by nothing. For a TCPA-facing dialer this is the most
   important open gap — an outbound call would fire at 3 a.m.
   (I deliberately did not add a hard gate: it is currently ~22:30 UTC, and
   enabling it would have blocked the debug session you asked for.)
2. **Some paths hang up without a goodbye.** `AI-DNC` and
   clarification-exhaustion return `ai_speech=None` and end the call, so the
   customer hears nothing before disconnect. Silence before hanging up is a
   defensible DNC choice, but for `AI-UNCLEAR` a short closing line would be
   warmer and safer — worth a product decision.
3. **`data/memory.json` grows without bound** (now ~71 KB / 50+ calls). Needs a
   retention cap before production.
4. **`data/campaigns.json` ships a leftover empty `mynewcampaign`** from an
   earlier test session.
5. **`README` config table was stale** — documented `MIN_ELIGIBLE_AGE` /
   `MAX_ELIGIBLE_AGE`, which do not exist. Corrected to `MIN_AGE` / `MAX_AGE`
   and noted that each campaign's per-step ages actually govern the call.

---

## 7. Files changed in this session

| File | Change |
|---|---|
| `.gitignore` | **re-encoded UTF-16LE → UTF-8** so the ignore rules actually work |
| `server/voice_ws.py` | STT is optional; typed-only sessions + `notice` frames |
| `agent/conversation.py` | `_ask` bounds guard + `_no_steps()`; `_line()` blank-aware helper |
| `scripts/healthcheck.py` | probes the configured `PORT` instead of hardcoded 9000 |
| `agent/call_session.py` | `EOFError` on closed stdin handled cleanly |
| `server/app.py` | debug console renders `notice` frames |
| `scripts/smoke_test.py` | **new** — 127-check full-feature end-to-end test |
| `README.md` | documented the 3-level verification path; fixed stale env-var table |
| `data/*.json` | runtime state written by the test run (call log, learned memory) |

Re-run everything with:
```bash
python cli.py debug                # terminal 1  → dashboard on :8000
python scripts/smoke_test.py       # terminal 2  → ALL 127 CHECKS PASSED
python -m pytest tests/ -q         # 136 passed
```
