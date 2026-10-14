# Run & Test on Your Local Machine — with Voice Features 🎙️

Everything you need to download this bot, run it on your own computer, and talk to
Lina with a **real voice** (she speaks, you can speak back via microphone).

> No phone system needed. Text chat works with **zero keys**; voice needs two free API keys.

---

## Step 1 — Download the code

**Option A: git clone (recommended)**

```bash
git clone -b arena/9a6af3fe-final-expense-fronter https://github.com/muneeb819/final-expense-fronter.git
cd final-expense-fronter
```

**Option B: ZIP download (no git needed)**

Download and unzip:
`https://github.com/muneeb819/final-expense-fronter/archive/refs/heads/arena/9a6af3fe-final-expense-fronter.zip`

---

## Step 2 — Install Python dependencies

Requires **Python 3.10+** ([download here](https://www.python.org/downloads/)).

```bash
# Windows (PowerShell) / macOS / Linux:
python -m venv .venv

# Activate it:
#   Windows:  .venv\Scripts\activate
#   macOS/Linux:  source .venv/bin/activate

pip install --upgrade pip
pip install -r requirements.txt
```

---

## Step 3 — Create your `.env` file

```bash
# Windows:  copy .env.example .env
# macOS/Linux:  cp .env.example .env
```

The defaults already run in **debug mode** (text chat, no keys). For voice, add keys (Step 4).

---

## Step 4 — Get FREE voice API keys (2 minutes each)

| Feature | Provider | Free tier | Get key |
|---|---|---|---|
| 🔊 **Lina speaks** (voice output) | ElevenLabs | 10,000 chars/mo free | [elevenlabs.io/app](https://elevenlabs.io/app) → profile → API keys |
| 🎤 **Mic input** (you speak) | Deepgram | $200 free credit, no card | [console.deepgram.com](https://console.deepgram.com/) → API keys |

Paste into your `.env`:

```env
TTS_PROVIDER=elevenlabs
ELEVENLABS_API_KEY=sk_your_key_here
STT_PROVIDER=deepgram
DEEPGRAM_API_KEY=your_key_here
```

### Optional: add Kokoro as a free local standby voice

If you want a voice that costs nothing and works with no API key, run Kokoro
locally. The bot uses it as an automatic standby: if ElevenLabs ever fails
mid-call, the bot switches to Kokoro instead of dropping the call.

```powershell
.\start-kokoro.ps1        # first run installs ~1.5-2 GB of CPU torch + weights
```

```env
TTS_STANDBY_PROVIDER=kokoro
KOKORO_BASE_URL=http://localhost:8880
KOKORO_VOICE=af_bella
```

Check it is up: `http://localhost:8880/health` and `GET /v1/audio/voices`.

Optional (smarter brain — or skip it, the built-in mock brain works offline):

```env
LLM_PROVIDER=openai
OPENAI_API_KEY=sk_your_key_here
```

---

## Step 5 — Launch the bot 🚀

```bash
python cli.py debug
```

You'll see a smoke test pass, then:

```
Starting web on 0.0.0.0:8000 [tts=elevenlabs]
  Dashboard:     http://0.0.0.0:8000/
  Debug console: http://0.0.0.0:8000/debug
```

Open **`http://localhost:8000/debug`** in Chrome or Edge.

---

## Step 6 — Talk to Lina 🗣️

1. The bot greets you — with **Voice: on**, you'll **hear Lina speak** 🔊
2. **Type** a reply (e.g. `I'm 67 years old`) — or click **🎤 Mic**, speak, click Stop
3. Watch the **beat/state inspector** (state transitions, age detection, disposition)
4. Try: `I am 35` (ineligible), `Take me off your list` (DNC), `Who is this?`
5. Click **🔄 New call** to restart; visit `/` for the dashboard call log

> Mic needs `localhost` + microphone permission in the browser. If sound doesn't
> autoplay, click anywhere on the page once (browser policy), then continue.

---

## Other commands

| Command | What it does |
|---|---|
| `python cli.py debug` | Smoke test + web server (dashboard + debug console) ⭐ |
| `python cli.py demo` | Scripted 2-turn test, no server |
| `python cli.py console` | Terminal role-play (you type as caller) |
| `python cli.py stream` | Synthesize 3 Lina lines → saves `data/stream_test/*.mp3` (hear her voice instantly) |
| `python cli.py serve` | Production-style server (dashboard + ARI listener) |

**Fastest voice check (no browser):** set the ElevenLabs key, run `python cli.py stream`,
then play the `.mp3` files in `data/stream_test/`.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `voice: text only` badge | Keys missing — check `.env` values, **restart** `cli.py debug` |
| Mic button says keys needed | Set `STT_PROVIDER=deepgram` + `DEEPGRAM_API_KEY`, restart |
| No sound on first reply | Click anywhere on the page once (autoplay policy) |
| `pip install` fails on Windows | Install Python from python.org (check “Add to PATH”), use `py -m venv .venv` |
| Port 8000 busy | Change `PORT=8001` in `.env` |
| `[ARI] not connected` message | Normal without a phone system — text/voice debug still works fully |

## Going live later (phone calls)

Set `ARI_*` (Asterisk), `VICIDIAL_*`, `STT_PROVIDER=deepgram`, `TTS_PROVIDER=elevenlabs`,
route your DID to the Stasis app (see `configs/extensions.conf.example`), and run
`python cli.py serve`. See `README.md` + `docs/BUDGET_COST_STRUCTURE.md` for costs.
