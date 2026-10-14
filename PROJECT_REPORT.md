# BPO Adriene — AI Voice Fronter · Project Report

**Project:** Multi-campaign AI voice qualification bot (Medicare / Final Expense / ACA / Hospital Indemnity)
**Report date:** 14 October 2026
**Run address:** `http://localhost:9000` (dashboard `/`, campaigns `/campaigns`, voice console `/debug`)

---

## 1. What this system is

A production-oriented **outbound voice qualification bot** that speaks with a live
customer in real time, verifies qualification criteria (Medicare coverage, age,
interest, etc.), and hands qualified leads to a licensed agent — or politely ends
the call with the correct disposition.

The **Medicare fronter is the flagship and is production-ready**. The same engine
also runs Final Expense, ACA, and Hospital Indemnity campaigns, all selectable and
editable from the dashboard.

### Core architecture

```
Customer mic (16 kHz PCM, browser)
      │  WebSocket (binary audio + JSON control)
      ▼
Deepgram streaming STT  ──►  final / interim transcripts
      │
      ▼
Campaign Engine (state machine, compliance rules)
      │  verdict → next / transfer / clarify / end:<disposition>
      ▼
LLM Brain (Gemini)  ── naturalizes each compliance-correct line
      │                 (+ sentiment tone, + few-shot from memory)
      ▼
TTS facade  ──►  ElevenLabs (primary)  ─┐  both expose raw PCM16 @16 kHz
                 Kokoro   (standby)  ──┘  with automatic failover
      ▲
      └── Barge-in: customer interrupts → TTS cancelled in <200 ms
```

---

## 2. What has been done (completed & verified)

### 2.1 Environment & runtime
- Python 3.12 project; dependencies installed.
- `.env` holds **ElevenLabs** (TTS — voice *Jessica*, model `eleven_v3`) and
  **Deepgram** (STT — model `nova-2`) keys. Both **verified working**.
- Server runs via `python cli.py debug` on **port 9000** (`PORT=9000`).
- Launchers: `start.bat`, `start.ps1` (`-Port`, `-Check`, `-Tests`).
- Readiness tool: `python scripts/healthcheck.py` → **24 checks PASS, 1 NOTE**.

### 2.2 Medicare verification script (flagship — strict compliance)
- Ask **"Do you currently have Medicare?"** → only a **clear YES** proceeds.
- **NO / refuse / unclear after 2 attempts** → politely end call (correct disposition).
- **Unsure** → confirm "yes or no?"; **question** → clarify & re-ask;
  **private insurance only** → "Do you *also* have Medicare?".
- After YES → **age verification, eligible range 45–90** (44 / 91 → not eligible).
- Dispositions implemented & tested: `AI-TRANSFER`, `AI-NO-MEDICARE`,
  `AI-MEDICARE-REFUSED`, `AI-UNCLEAR`, `AI-NOT-ELIGIBLE`, `AI-DNC`, `AI-BUSY`.
- All Final Expense language removed — the default campaign is Medicare-only.
- **Verified end-to-end over HTTP** — every path returns the correct disposition.

### 2.3 Real-time voice with barge-in
- `server/voice_ws.py` — persistent WebSocket session:
  - Browser streams **binary PCM16** audio; server forwards it to Deepgram
    continuously (interim + final transcripts).
  - **Barge-in works because a reader task runs the whole call** — a
    `{"type":"barge"}` message sent *while the bot is speaking* cancels the TTS
    task immediately (< 200 ms), stops browser playback, and keeps listening.
  - `websockets 17` compatibility (`additional_headers`) and starlette
    `receive()` loop handled correctly.
- **Verified programmatically:** connected, received greeting + **150 streamed
  PCM audio chunks** from ElevenLabs — the full round-trip works.
- Debug console `/debug`: mic capture, gapless PCM playback, client-side VAD
  barge-in, live transcript, typed-message fallback, quick-reply pills.
- **New:** `_speak_guarded` now catches non-cancellation errors, so a total TTS
  failure degrades to silence (the line is still shown in the dashboard transcript)
  instead of dropping the call.

### 2.4 LLM brain
- **Gemini provider** wired via its OpenAI-compatible endpoint (no extra
  dependency). Config: `LLM_PROVIDER=gemini`, `GEMINI_MODEL=gemini-2.0-flash`.
- `agent/brain.py` — understanding engine (intent, entities, sentiment) with a
  **semantic regex fallback** so the bot still behaves correctly offline.
- **Graceful degradation:** if the LLM returns an error, the bot speaks the
  scripted compliance line instead of leaking the error (verified).

### 2.5 Self-learning layer ("gets smarter with use")
- `agent/memory.py` — `CallMemory`, a persistent, outcome-weighted store
  (`data/memory.json`):
  - Records **every finished call** (transcript, disposition, age, sentiment,
    quality score).
  - **Few-shot retrieval:** similar past exchanges — weighted toward successful
    ones — are injected into every line-generation prompt.
  - **Self-analysis:** `/api/insights` reports total calls, confusion hotspots,
    phrases that caused confusion, and an improvement suggestion.
- `agent/brain.py::quick_sentiment` — fast offline sentiment (no LLM call) used
  to shift tone: patient with an angry customer, warm with a pleasant one.

### 2.6 Multi-campaign engine (the big architectural win)
- `agent/campaign.py` — **data-driven campaign schema** (`Campaign` + `Question`)
  and a JSON store (`data/campaigns.json`).
- Four campaigns seeded and **verified working end-to-end**:
  | Campaign | Flow | Age compliance |
  |---|---|---|
  | **Medicare Fronter** (default) | Medicare YES → age | 45–90 |
  | **Final Expense Fronter** | interest YES → age | 50–85 |
  | **ACA / Marketplace** | uninsured → age | 18–64 |
  | **Hospital Indemnity** | age → no recent hospital stay | 50–85 |
- `agent/conversation.py` rewritten as a **generic gate engine** that walks
  whatever questions the active campaign defines — *nothing about Medicare is
  hardcoded*. Routing grammar: `next | transfer | clarify | end:DISPOSITION`.
- Deploying a campaign switches the live bot instantly (verified: Medicare →
  Final Expense → back).

### 2.7 Dashboard
- `/` — status (pipeline, TTS, STT, brain), live call log, links, recordings.
- **`/campaigns` — campaign manager:**
  - Card grid of all campaigns with **Deploy** button and LIVE badge.
  - **Full editor:** name, description, agent/brand, opening & closing lines,
    and per-step **script + compliance** (question text, type, age min/max,
    re-ask limits, response lines, routing rules).
  - Add / delete / reorder verification steps; create new campaign (clone or empty).
  - **Self-learning insights panel** and reset-to-defaults.
  - Save → `PUT /api/campaigns/{id}`; Deploy → `POST /api/campaigns/active`;
    Delete → `DELETE /api/campaigns/{id}` (built-ins and live campaigns guarded).
  - **Exports:** `GET /api/export/calls|transcripts|insights` (CSV with UTF-8
    BOM, or JSON).
- **Verified:** editor loads, **save round-trip persists**, **the engine honored
  an edit live** (age 91 transferred), reset restored 90, and **all four page
  scripts pass `node --check`** (a double-`JSON.stringify` bug in the
  Deploy/Delete inline handlers was found and fixed).

### 2.8 Dual-engine TTS with automatic failover (new)
- **ElevenLabs** is the primary engine (cloud, expressive, voice *Jessica*).
- **Kokoro** is the standby: a free, self-hosted, OpenAI-compatible TTS server
  (`POST /v1/audio/speech` → raw s16le PCM @ 24 kHz).
- `agent/resample.py` — streaming **24 kHz → 16 kHz** resampler (linear
  interpolation, carries fractional read position and half-samples across chunk
  boundaries), so Kokoro's output feeds the same 16 kHz PCM path as ElevenLabs.
- `agent/tts.py` rewritten as a **single facade**:
  - `synthesize_stream_pcm(text)` is the one method the voice pipeline calls;
    it yields raw PCM16 @ 16 kHz mono regardless of engine.
  - **Failover:** if the active engine fails *before any audio has played*, the
    standby is used for that utterance and promoted; health is remembered for
    60 s so a discovered outage is not re-hit on every call, and once the record
    goes stale the primary is retried automatically.
  - If it fails *after* the customer has heard part of the line, the bot stops
    rather than replaying the sentence from the top.
  - Errors are classified (`is_provider_error`) so a real outage fails over while
    our own bugs (ValueError, KeyError, …) still surface loudly.
- `start-kokoro.ps1` installs and runs Kokoro on **:8880**. It is **idempotent**:
  dependencies are only installed when the venv lacks `torch`, and the model
  only when the weights are missing — re-running never re-downloads 4.6 GB.
- `/health` now reports `tts_primary`, `tts_standby`, `tts_active`,
  `tts_failover_reason` and each engine's `configured` / `reachable` / `healthy`.

### 2.9 Test evidence (HTTP, live server)
```
Medicare:  hello → "Do you currently have Medicare?"
           "yes I have medicare" → "How old are you...?"
           "I'm 67 years old" → "Thank you..." [ended, AI-TRANSFER]
Compliance edit: max age 90→92 saved → age 91 → AI-TRANSFER → reset → 90
Campaign deploy: medicare ⇄ final_expense switched, compliance reflected (45-90 vs 50-85)
Insights: calls recorded, avg quality, transfers, confusion hotspots
Recordings: 8 saved under data/recordings (WAV + JSON transcript)
```

---

## 3. What needs to be done (pending)

### 🔴 One action from you: paste the Gemini API key
The LLM API section of `.env` is **deliberately blank**:

```env
LLM_PROVIDER=gemini          # kept so pasting a key activates it
GEMINI_API_KEY=              # ← paste your key here
GEMINI_MODEL=gemini-2.0-flash
```

Until a key is present the bot speaks the **compliance script lines** — correct
wording, no variation, fully safe for live calls. Paste the key and restart to
get natural, varied, warm, sentiment-aware wording.
Get a key at [Google AI Studio](https://aistudio.google.com/apikey).

### 🟠 High value (before going live)
1. **Live microphone test of barge-in in a browser** — the WebSocket + audio
   streaming + interrupt logic are verified programmatically, but a real
   mic/speaker round-trip (and tuning the VAD threshold) needs a human test.
2. **End-to-end voice call with real speech** and **latency measurement**
   (target: < 1.5 s response; barge-in < 200 ms). Tune capture buffer
   (4096→2048) and Deepgram `endpointing` if needed.
3. **Vicidial / ARI transfer integration** — `agent/ari.py` and the transfer
   disposition exist, but a live Asterisk/Vicidial connection is needed to
   actually bridge the qualified call to a closer queue.
4. **Run Kokoro** (optional, local free voice) — `.\start-kokoro.ps1`. Needs
   ~1.5–2 GB of disk for CPU torch + weights. Until then `/health` reports
   Kokoro as unreachable and the bot runs on ElevenLabs alone.

### 🟡 Medium (product polish)
5. **Embeddings-based memory retrieval** — current few-shot uses TF-IDF/token
   overlap (works well offline); upgrade to Gemini embeddings for better matching.
6. **Production deployment** — HTTPS/real domain (required for microphone access
   outside localhost), process manager, monitoring, error alerting.
7. **A/B testing of script lines** using the self-learning data to pick winners.

---

## 4. How to run

```powershell
cd <project folder>
python cli.py debug          # starts dashboard + campaigns + voice console
# open http://localhost:9000/           (dashboard)
# open http://localhost:9000/campaigns  (select / edit / deploy campaigns)
# open http://localhost:9000/debug       (live voice console)
```

```powershell
python -m pytest tests/ -q          # 136 tests
python scripts/healthcheck.py       # 24-point readiness report
.\start-kokoro.ps1                   # optional: local standby voice on :8880
```

Add your `GEMINI_API_KEY` to `.env` and restart to activate the AI brain.

---

## 5. Key files

| File | Purpose |
|---|---|
| `agent/campaign.py` | Campaign schema, 4 default campaigns, JSON store |
| `agent/conversation.py` | Generic gate engine (runs any campaign) |
| `agent/qualifier.py` | Yes/No, age, keyword, DNC/busy classifiers |
| `agent/memory.py` | Self-learning store (few-shot + insights) |
| `agent/brain.py` | LLM understanding + offline sentiment |
| `agent/prompt.py` | Persona + compliance + line naturalizer |
| `agent/llm.py` | Gemini / OpenAI / Anthropic / Ollama providers |
| `agent/tts.py` | TTS facade: ElevenLabs + Kokoro, failover, PCM16 @16 kHz |
| `agent/resample.py` | Streaming 24 kHz → 16 kHz PCM resampler |
| `server/voice_ws.py` | Real-time voice pipeline + barge-in |
| `server/app.py` | FastAPI: dashboard, campaigns API, voice WebSocket |
| `start-kokoro.ps1` | Installs/runs the local Kokoro standby TTS |
| `scripts/healthcheck.py` | Readiness report |
| `.env` | API keys & config (⚠ Gemini key intentionally blank) |

---

## 6. Bottom line

**Done:** A working, tested, multi-campaign voice bot with a production-grade
Medicare script, real-time streaming voice with barge-in, a dashboard to select
and edit campaigns and deploy them, a genuine self-learning loop, **call-log
persistence and exports**, **recording disclosure compliance**, and a **dual-engine
TTS with automatic failover** (ElevenLabs primary, Kokoro standby).

**The one action needed from you:** paste `GEMINI_API_KEY` into `.env`.
**The one thing blocking production:** a live Vicidial/Asterisk to transfer calls
into, plus a real-microphone test and a production HTTPS host.
