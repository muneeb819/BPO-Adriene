# Deploying BPO Adriene on your own server

Everything needed to run this on a machine you control: what to install, how to
configure it, how to start it, how to keep it running, and how to prove it works.

Read the **Quick start** section for your OS and you are done in ~10 minutes. The
rest is reference (autostart, Asterisk, Docker, troubleshooting, backup).

---

## 1. What runs, and on which ports

| Process | Port | Started by | Purpose |
|---|---|---|---|
| **App** (`cli.py serve`) | **8000** (tcp) | `start.ps1 -Serve` / `start.sh --serve` / systemd | dashboard, campaigns, voice studio, ARI listener for Asterisk |
| **Neural voice server** (`scripts/neural_tts_server.py`) | **8880** (local) | the same launcher / systemd | turns text into speech with local Piper/ONNX voices |
| Asterisk ARI (already on your PBX) | 8088 | your PBX | places/handles the actual phone calls |

* The app is the only thing that needs to be reachable from the network.
* **8880 is private** — the app talks to it on `127.0.0.1`. Do not expose it.
* No cloud key is required for audio: the voices are neural models running on CPU
  (~0.3 s per sentence). Keys only add options (see §5).

```
caller ──► Asterisk ──► ARI websocket ──► app (:8000)
                                            │  STT: Deepgram (or typed in the console)
                                            │  text → speech
                                            ▼
                                  voice server (:8880)  → Piper/ONNX neural voices
                                            │
                                  16 kHz PCM ──► Asterisk playback ──► caller
```

---

## 2. Requirements

**Windows** (Windows 10/11 or Windows Server 2019+)

* Python **3.12 x64** — https://www.python.org/downloads/windows/ (tick *Add
  python.exe to PATH*)
* ~1 GB free disk (190 MB of voice models + recordings) and 2 GB RAM
* Optional: an Asterisk/FreePBX box for real phone calls

**Linux** (Ubuntu 22.04+/Debian 12+)

```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv git libsndfile1
```

**Either OS:** outbound HTTPS is *not* needed at runtime. It is needed once, to
download the voice models (~190 MB from GitHub). A machine with no internet can
be set up by copying `data/models/piper/` from a machine that has it.

---

## 3. Quick start

### Windows

```powershell
git clone https://github.com/muneeb819/BPO-Adriene.git
cd BPO-Adriene
.\start.ps1                 # first run: creates .venv, installs deps, starts everything
```

Then open **http://localhost:8000/** — the dashboard. Press **Test voice** in the
*Voice studio* panel to hear Lina.

For the live server (Asterisk/ARI, no demo call, DEBUG off):

```powershell
.\start.ps1 -Serve
```

### Linux / macOS

```bash
git clone https://github.com/muneeb819/BPO-Adriene.git
cd BPO-Adriene
python3 scripts/setup_local.py     # venv + dependencies + .env + voices + readiness report
./start.sh                         # debug: dashboard + demo call + local voices
./start.sh --serve                 # live server: ARI + web
```

`start.sh` starts the voice server on 8880 (logging to `logs/voice-server.log`)
and the app in the foreground; Ctrl-C stops both. Common flags:

```bash
./start.sh --check            # readiness report, then exit
./start.sh --tests            # run the test suite, then exit
./start.sh --no-voice         # app only (when the voice engine runs elsewhere)
./start.sh --port 8080 --voice-port 8881
```

### Both launchers do the same first-run bootstrap

1. create `.venv` and `pip install -r requirements.txt`
2. copy `.env.example` → `.env` **once** (your edits are never overwritten)
3. download the neural voices if `data/models/piper/` is empty
4. start the voice server, wait until it answers, then start the app

Nothing else is needed for a working debug deployment.

---

## 4. Manual install (no launcher)

```bash
python -m venv .venv
# Windows:  .venv\Scripts\activate
# Linux:    source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

cp .env.example .env          # Windows: copy .env.example .env
python scripts/fetch_neural_voices.py          # ~190 MB, one time

# terminal 1 — voice engine
python scripts/neural_tts_server.py --host 0.0.0.0 --port 8880 --warm
# terminal 2 — the app
python cli.py serve            # production (ARI + web)
# or
python cli.py debug            # demo call + web, no PBX needed
```

Verify at any time:

```bash
python scripts/healthcheck.py            # readiness report (exit 0 = ready)
python scripts/healthcheck.py --json     # machine-readable, for automation
```

---

## 5. Configuration (`.env`)

Everything is in `.env` (created from `.env.example`, which documents **every**
setting). The values that matter for a live deployment:

| Key | Set it to | Why |
|---|---|---|
| `DEBUG` | `false` | production: no DEBUG badge, no demo call |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | where the dashboard listens |
| `WEB_PASSWORD` | a password | **required if the dashboard is reachable beyond localhost** — every page, API and recording is then behind HTTP Basic auth (`/health` stays open for monitoring, and the voice socket reuses the login cookie) |
| `TTS_PROVIDER` | `piper` | the local neural engine (default, no key) |
| `PIPER_BASE_URL` | `http://127.0.0.1:8880` | where the voice server runs |
| `PIPER_VOICE` | e.g. `en_US-amy-low` | which voice speaks by default (or pick one in the Voice studio) |
| `STT_PROVIDER` | `deepgram` | **real phone calls need this** — `console` only works for typed input in the dashboard |
| `DEEPGRAM_API_KEY` | your key | transcribes the caller |
| `ARI_URL`, `ARI_USERNAME`, `ARI_PASSWORD` | your PBX | connects to Asterisk |
| `CLOSER_QUEUE` / `CLOSER_EXTENSION` | your queue or extension | where a qualified call is transferred |
| `LLM_PROVIDER` + its key | optional | without a key the bot speaks the campaign's compliance lines |

Change a value → restart the app (`start.ps1 -Serve` / `./start.sh --serve`). The
Voice studio's **Use** button changes the live voice immediately and survives a
restart; writing the choice into `PIPER_VOICE` makes it the default for everyone.

### Voice engines you can choose

| `TTS_PROVIDER` | What you get | Needs |
|---|---|---|
| `piper` **(default)** | real neural voices, offline, ~0.3 s/sentence, CPU only | `pip install piper-tts onnxruntime` + the fetched models |
| `elevenlabs` | most expressive cloud voice, and the only engine that can *generate* a brand-new voice from a description | `ELEVENLABS_API_KEY` |
| `chatterbox` | local cloning engine (clone any reference audio) | `pip install chatterbox-tts` + ~2–3 GB of weights |
| `kokoro` | a real Kokoro server (same wire format as the Piper one) | a running Kokoro server |
| `console` | no audio (text only) | — |

`TTS_STANDBY_PROVIDER` is the automatic fallback if the primary fails mid-call —
only name an engine that is actually installed on this server. Leave it empty
rather than pointing it at something that is not there.

### Voice studio (no keys required)

* **Library** — add voices from a file, a server path, or the browser mic.
* **Describe a voice** — type *"warm older British man, speaks slowly"* and the
  studio picks the resident neural voice that fits, shows its reasoning word by
  word, and stores it. Cloud *Voice Design* (a brand-new voice from a description)
  appears when `ELEVENLABS_API_KEY` is set; without a key the panel says exactly
  how to add one.
* **Match** — feed it a recording and it names the closest resident voice and
  renders the recording on it, so you can judge a clone by ear without any
  cloning weights.
* **Remix** — blend or stitch saved voices, optionally re-matched to a neural voice.

---

## 6. Autostart (keep it running)

### Windows — NSSM (recommended)

```powershell
# https://nssm.cc/download  → put nssm.exe in C:\Tools\
$root = "C:\bpo-adriene"

C:\Tools\nssm.exe install BPO-Voice "$root\.venv\Scripts\python.exe" `
  "scripts\neural_tts_server.py --host 0.0.0.0 --port 8880 --warm"
C:\Tools\nssm.exe set BPO-Voice AppDirectory $root
C:\Tools\nssm.exe set BPO-Voice AppStdout "$root\logs\voice-server.log"
C:\Tools\nssm.exe set BPO-Voice AppStderr "$root\logs\voice-server.log"
C:\Tools\nssm.exe set BPO-Voice Start SERVICE_AUTO_START

C:\Tools\nssm.exe install BPO-App "$root\.venv\Scripts\python.exe" "cli.py serve"
C:\Tools\nssm.exe set BPO-App AppDirectory $root
C:\Tools\nssm.exe set BPO-App AppStdout "$root\logs\app.log"
C:\Tools\nssm.exe set BPO-App AppStderr "$root\logs\app.log"
C:\Tools\nssm.exe set BPO-App DependOnService BPO-Voice
C:\Tools\nssm.exe set BPO-App Start SERVICE_AUTO_START

Start-Service BPO-Voice, BPO-App
```

Alternative without extra software: **Task Scheduler** → *Create Task* → trigger
*At startup* → action *Start a program* → `…\.venv\Scripts\python.exe` with
arguments `cli.py serve` and *Start in* = the project folder. Do the same for the
voice server. On Windows Server also open the firewall for the dashboard:

```powershell
New-NetFirewallRule -DisplayName "BPO Adriene dashboard" -Direction Inbound `
  -Protocol TCP -LocalPort 8000 -Action Allow
```

### Linux — systemd

```bash
sudo useradd -r -s /usr/sbin/nologin bpo
sudo mkdir -p /opt/bpo-adriene && sudo chown bpo:bpo /opt/bpo-adriene
sudo -u bpo git clone https://github.com/muneeb819/BPO-Adriene.git /opt/bpo-adriene
cd /opt/bpo-adriene
sudo -u bpo python3 scripts/setup_local.py        # venv, deps, .env, voices

sudo cp deploy/bpo-voice.service deploy/bpo-adriene.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bpo-voice bpo-adriene
systemctl status bpo-voice bpo-adriene
journalctl -u bpo-adriene -f
```

The units in `deploy/` use `/opt/bpo-adriene` and user `bpo` — edit them if your
paths differ. They restart automatically, log to `logs/`, and start the voice
server before the app.

---

## 7. Docker

```bash
cp .env.example .env          # edit keys / ARI / WEB_PASSWORD
docker compose up -d          # builds, downloads the voices into ./data, starts both processes
docker compose logs -f
docker compose exec app python scripts/healthcheck.py
```

`./data` (call log, recordings, campaigns, voice library, voice models) lives on
the host, so `docker compose down` never loses a recording. Port 8000 is
published; 8880 stays inside the container.

---

## 8. Asterisk (real phone calls)

1. `/etc/asterisk/ari.conf`

   ```ini
   [general]
   enabled = yes
   [ari]
   type = user
   read_only = no
   password = <ARI_PASSWORD in .env>
   ```

2. `/etc/asterisk/http.conf`: `enabled = yes` (and `bindaddr = 0.0.0.0` if the app
   is on another machine).
3. Route the inbound DID to the Stasis app — see `configs/extensions.conf.example`
   (`Stasis(final-expense-fronter)`).
4. Create the transfer target: a `closers` queue (or set `CLOSER_EXTENSION`).
5. Set `STT_PROVIDER=deepgram` + `DEEPGRAM_API_KEY`, then restart and call the DID.

A qualified call is transferred to the closer queue; the disposition is written to
the call log and shown under *Insights* in the dashboard.

---

## 9. Verify the deployment

Run these on the server; each one exits non-zero if something is wrong, so they
can be wired into a scheduled task or CI.

```bash
python scripts/healthcheck.py                             # ~50 checks incl. a real voice render
python -m pytest tests/ -q                                # 384 unit tests
python scripts/smoke_test.py --url http://localhost:8000  # 126 end-to-end checks
python scripts/verify_voice_studio.py --url http://localhost:8000   # 49 studio checks
```

What "ready" looks like:

```
✓ PASS  voice chain — piper → chatterbox
✓ PASS  local neural voices — 3 installed (190 MB): en_GB-alan-medium, en_US-amy-low, en_US-lessac-medium
✓ PASS  voice server /health — http://127.0.0.1:8880 — engine=piper, voices=3, loaded=3
✓ PASS  voice render — 2.75s, peak 32767, rms 4671 — voice='en_US-amy-low'
✓ PASS  server /health — ok mode=turn-based
  READY — 50 checks passed, 6 note(s).
```

The **voice render** line is the important one: it speaks a real sentence through
the engine and measures the audio, so "the caller will hear her" is proven, not
assumed. `NOTE` lines are advice (missing optional keys, DEBUG on, …); `FAIL`
lines stop the deployment being called ready.

---

## 10. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Dashboard opens, **Test voice** is silent | voice server not running | `python scripts/neural_tts_server.py --port 8880 --warm`; check `logs/voice-server.log` |
| `voice server /health … not answering` | port 8880 busy or crashed | `netstat -ano \| findstr 8880` (Windows) / `ss -ltnp \| grep 8880`; stop the other process or use another `--voice-port` **and** update `PIPER_BASE_URL` |
| `local neural voices — 0 installed` | models missing | `python scripts/fetch_neural_voices.py` (needs internet once), or copy `data/models/piper/` from another machine |
| Preview returns **502** with a reason | the profile's engine is not installed here | the header `X-Voice-Mode: standby` says which engine actually spoke; install the engine or pick a local voice in the studio |
| Calls connect but nobody hears Lina / she hears nothing | STT/TTS provider mismatch | `healthcheck.py` → `speech-to-text` note: set `STT_PROVIDER=deepgram` + `DEEPGRAM_API_KEY` for real calls |
| `ARI not connected … Connection refused` | Asterisk ARI off or wrong URL/password | check `ARI_URL`, `ari.conf`, `http.conf`; the dashboard still works without ARI |
| `port 8000 already in use` | an old instance is still running | stop it, or start with `-Port 8080` / `--port 8080` |
| Browser blocks the microphone in the studio | mic access needs a secure origin | use `http://localhost:8000` on the server itself, or upload a file instead |
| Windows console shows boxes instead of ✓ | legacy console encoding | cosmetic only — the report falls back to `[OK]`; use Windows Terminal for colour |
| PIP install fails on a fresh Windows box | Python not on PATH / wrong arch | reinstall Python 3.12 **x64** with *Add to PATH* |

More detail on any of these: `logs/voice-server.log`, `logs/app.log`,
`journalctl -u bpo-adriene`, and `python scripts/healthcheck.py --json`.

---

## 11. Backup, upgrade, restore

**Back up** (everything that is not code):

```bash
tar -czf bpo-backup-$(date +%F).tar.gz .env data/
```

That covers the call log, recordings, campaigns, memory, and the voice library
(including your cloned/designed voices). `data/models/` is re-downloadable, so it
can be skipped to keep the archive small.

**Upgrade** in place:

```bash
git pull
python scripts/setup_local.py          # re-installs deps, keeps .env and voices
./start.sh --check                     # readiness report
# systemd: sudo systemctl restart bpo-voice bpo-adriene
```

**Restore**: put `.env` and `data/` back and start.

---

## 12. Security notes

* Set `WEB_PASSWORD` whenever the dashboard is reachable from anywhere but the
  server itself: it puts HTTP Basic auth in front of the dashboard, the campaign
  editor, the studio, every API and every recording. The username is ignored (any
  name works) and `/health` stays open so monitoring keeps working; the live voice
  WebSocket authenticates with the cookie the login sets, because a browser cannot
  send an Authorization header on a WebSocket handshake. The readiness report warns
  when `HOST` is networked and no password is set.
* Leave `HOST=127.0.0.1` if only the server itself needs the dashboard.
* Never expose port **8880** — it has no authentication; it is meant for localhost.
* Keep `.env` out of git (already in `.gitignore`) and out of chat; it holds the
  PBX password and any API keys.
* Recordings contain personal data (names, ages, Medicare answers): restrict
  access to `data/recordings/` and to the backup archives.
* `CALLING_HOUR_START` / `CALLING_HOUR_END` and the campaign gates (age, DNC,
  licensed states) are enforced by the engine — keep them in sync with your
  compliance rules.
