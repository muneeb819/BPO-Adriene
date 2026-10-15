# Run & Test on Your Local Machine — with Voice Features 🎙️

Everything you need to download this bot, run it on your own computer, and talk to
Lina with a **real voice** (she speaks, you can speak back via microphone).

 > No phone system needed, and **no API keys at all**: the voice is a neural model
 > (Piper/ONNX) that runs locally on CPU. Keys only add options — see Step 4.
 >
 > Deploying this on a real server instead of your laptop? Use
 > [DEPLOYMENT.md](../DEPLOYMENT.md), which covers autostart, Asterisk, Docker,
 > verification and troubleshooting.

---

## Step 1 — Download the code

**Option A: git clone (recommended)**

```bash
git clone https://github.com/muneeb819/BPO-Adriene.git
cd BPO-Adriene
```

**Option B: ZIP download (no git needed)**

Download and unzip `https://github.com/muneeb819/BPO-Adriene/archive/refs/heads/main.zip`

---

## Step 2 — Install (one command)

Requires **Python 3.10+** (3.12 recommended — [download here](https://www.python.org/downloads/)).

```powershell
.\start.ps1                  # Windows: venv + dependencies + voices + .env, then starts
```
```bash
python3 scripts/setup_local.py    # Linux/macOS: the same setup, without starting
./start.sh                        #   then this
```

The launcher does everything below for you. Manual equivalent:

```bash
python -m venv .venv
#   Windows:  .venv\Scripts\activate
#   macOS/Linux:  source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
python scripts/fetch_neural_voices.py            # 3 neural voices, ~190 MB, one time
```

---

## Step 3 — Create your `.env` file

```bash
# Windows:  copy .env.example .env
# macOS/Linux:  cp .env.example .env
```

The defaults already run in **debug mode** (text chat, no keys). For voice, add keys (Step 4).

---

## Step 4 — Voices and keys (all optional)

**Lina already speaks with no keys.** `.env` ships with
`TTS_PROVIDER=piper` — real neural voices rendered locally on CPU by
`scripts/neural_tts_server.py` (started by the launcher). Pick a voice in the
dashboard's **🎙 Voice studio** (▶ Preview plays it, **Use** makes it live), or
type a description in *Describe a voice* ("warm older British man, speaks slowly").

Optional upgrades:

| Want | Provider | Free tier | Get key |
|---|---|---|---|
| More expressive cloud voice, or generating a brand-new voice from a description | ElevenLabs | 10,000 chars/mo free | [elevenlabs.io/app](https://elevenlabs.io/app) → profile → API keys |
| **Real phone calls** (the bot must hear the caller) | Deepgram | $200 credit, no card | [console.deepgram.com](https://console.deepgram.com/) → API keys |
| Natural, varied wording instead of the scripted lines | OpenAI / Anthropic / Gemini / Ollama | varies | any provider |

```env
# cloud voice (optional — the local neural voices need nothing)
ELEVENLABS_API_KEY=sk_your_key_here
# TTS_PROVIDER=elevenlabs        # switch to it if you prefer
# real call audio (required for live phone calls, not for this local test)
STT_PROVIDER=deepgram
DEEPGRAM_API_KEY=your_key_here
# smarter brain (optional)
LLM_PROVIDER=openai
OPENAI_API_KEY=sk_your_key_here
```

If you install `chatterbox-tts` (2-3 GB of weights) you can also name it as the
engine and clone any reference recording — the Voice studio's **☑ Render on a
neural voice** already lets you judge a recording on the local voices without it.

---

## Step 5 — Launch the bot 🚀

```powershell
.\start.ps1            # Windows       (starts the voice server too)
```
```bash
./start.sh             # Linux/macOS
```

You'll see a smoke test pass, then:

```
[voice] starting the neural voice server on :8880 — ready
Starting web on 0.0.0.0:8000 [tts=piper]
  Dashboard:     http://0.0.0.0:8000/
  Debug console: http://0.0.0.0:8000/debug
  Health:        http://0.0.0.0:8000/health
```

Not sure everything is wired up? `python scripts/healthcheck.py` prints a
readiness report (and speaks a sentence through the engine to prove audio works).

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

Set `ARI_*` (Asterisk), `VICIDIAL_*` and `STT_PROVIDER=deepgram` (+ its key) so the
bot can hear the caller, route your DID to the Stasis app (see
`configs/extensions.conf.example`), and run `python cli.py serve` — or
`./start.sh --serve` / `.\start.ps1 -Serve`, which also keeps the local neural
voice engine running. Keep `TTS_PROVIDER=piper` for free local speech, or switch to
`elevenlabs`/`chatterbox`.

**[DEPLOYMENT.md](../DEPLOYMENT.md)** is the full server guide (autostart, systemd /
NSSM, Docker, verification, troubleshooting, backups). Costs:
`docs/BUDGET_COST_STRUCTURE.md`.
