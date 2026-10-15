# Voice Studio — Library, Design (prompt-to-voice), Remixing & Matching

Everything on this page is controlled from the dashboard (`/` → **🎙 Voice
studio**) and backed by the same JSON API, so it can be automated. Verified end
to end:

| Suite | What it proves | Result |
|---|---|---|
| `scripts/verify_voice_studio.py` | every studio endpoint against a live server | **49 checks** |
| `tests/test_voice_library.py` | profiles, validation, HTTP contract, fallbacks | 97 tests |
| `tests/test_neural_tts.py` | the local neural engine: describe, match, render | 22 tests |
| `tests/test_piper_provider.py` | the engine inside the live call pipeline | 14 tests |
| `tests/test_elevenlabs_design.py` | cloud Voice Design (mocked SDK) | 14 tests |
| `scripts/smoke_test.py` | the call flow end to end | 126 checks |

```
┌──────────────── Voice studio (dashboard) ─────────────────┐
│  Library   Add voice   Design   Remix   Test line         │
│                                                           │
│  Library : cards with ▶ Preview · Use · Rename · Delete    │
│  Add     : upload file · server path · record mic          │
│            ☑ Render on a neural voice + "Check match"      │
│  Design  : describe a voice in words → plan → save         │
│            or engine/model/language + sliders + tags       │
│  Remix   : pick 2+ voices → blend/stitch → ☑ re-match      │
└───────────────────────────────────────────────────────────┘
```

## 1. Voice library

A "voice" is a saved profile: **engine + model + language + clone reference +
tuning + tags + notes**. The bot speaks with whichever voice you press **Use**
on; the library remembers it and re-applies it on the next start
(`cli.py` prints `[voice] restored the saved voice '…'`).

| Action | Dashboard | API |
|---|---|---|
| List voices + designer options | Library tab | `GET /api/voices` |
| Add from a file you upload | *Add voice* → file picker | `POST /api/voices/upload` with `data_base64` |
| Add from a file on the server | *Add voice* → path box | `POST /api/voices/upload` with `path` |
| Record 8–20 s with the microphone | *Add voice* → **⏺ Record & add** | `POST /api/voices/upload` with `pcm_base64` + `sample_rate` |
| Render the recording on a neural voice | ☑ *Render on a neural voice* | same call plus `"match": true` |
| Save whatever the bot uses now | *Library* → "Save current voice as" | `POST /api/voices/current` |
| Preview | card **▶ Preview** | `POST /api/voices/{id}/preview` |
| Make it the live voice | card **Use** | `POST /api/voices/{id}/apply` |
| Rename / notes / tags / tuning | card **Rename** | `PUT /api/voices/{id}` |
| Delete (also removes its audio) | card **Delete** | `DELETE /api/voices/{id}` |

**Every reference is normalised on the way in** — decoded (wav/flac/ogg/mp3/aiff
via `soundfile`; plain wav always works), mixed to mono, silence trimmed, capped
to the loudest 30 s, levelled up (max +26 dB) and resampled to 24 kHz. Clips
under 1 s, silent recordings, empty files, unknown formats and files over 25 MB
are rejected with a readable `400`. Stored voices live in `data/voices/`
(git-ignored): `library.json` + one `<id>.wav` per reference.

## 2. Matching a recording (❤ judge a clone/blend by ear, offline)

A clone reference is only as useful as the audio you can hear from it. On a
machine without ChatterBox weights or an ElevenLabs key, the studio measures your
recording and renders it on the **resident neural voice** (Piper/ONNX) that fits
it best — a real voice, offline, in ~0.3 s:

```bash
curl -s -X POST localhost:8000/api/voices/match -H 'Content-Type: application/json' \
     -d '{"path":"data/voices/my_recording.wav"}' | python -m json.tool
```

```json
{ "match": { "model": "en_US-amy-low", "pitch_level": 61, "speed": 1.0,
             "volume": 1.144, "confidence": 0.807,
             "reference": {"f0_hz": 205.1, "brightness_hz": 1847.0, "seconds": 2.9},
             "voice_stats": {"f0_hz": 190.5, "brightness_hz": 1075.8},
             "why": "reference pitch ~205 Hz, brightness ~1847 Hz; closest resident
                     voice is en_US-amy-low (~190 Hz, ~1076 Hz) — pitch moved
                     +1.3 semitones to close the gap" } }
```

* `confidence` is `1 − distance` over pitch + brightness, so it says *how* close
  the match is instead of pretending every recording matches perfectly.
* `why` is the same sentence the dashboard shows under **Check match**.
* `alternatives` lists the other resident voices with their error, so you can
  pick a different one on purpose.
* Body may be `path`, `data_base64` (file) or `pcm_base64` (mic recording).

That measurement is applied automatically when you add a voice with
*Render on a neural voice* ticked (`"match": true`): the profile keeps the
recording **and** stores engine `piper`, the matched model and the tuning, so
**▶ Preview**, **Use** and remixes all speak with a real voice immediately.

## 3. Prompt to voice — describe a voice in words

Type what you want and the studio builds it. There is no reference audio
involved at all.

| Provider | What it does | Needs a key? |
|---|---|---|
| **Local neural voices** (`POST /api/voices/describe`) | reads the description, picks the resident neural voice that fits, and stores the pitch/pace/volume the words imply | **no** — free, offline, instant |
| **ElevenLabs Voice Design** (`POST /api/voices/design/elevenlabs`) | generates a brand-new voice from the description | yes — `ELEVENLABS_API_KEY` |

```bash
# what would it do? (nothing is created, the plan is shown)
curl -s -X POST localhost:8000/api/voices/describe -H 'Content-Type: application/json' \
     -d '{"description":"warm older British man, speaks slowly","dry_run":true}'

# create it, then hear it
curl -s -X POST localhost:8000/api/voices/describe -H 'Content-Type: application/json' \
     -d '{"name":"Warm British man","description":"warm older British man, speaks slowly"}'
curl -s -X POST localhost:8000/api/voices/<id>/preview -H 'Content-Type: application/json' \
     -d '{"text":"Hi, this is Lina calling about your Medicare plan."}' --output sample.wav
```

The plan explains itself, word by word:

```json
{ "plan": { "model": "en_GB-alan-medium", "model_name": "Alan (British English, medium)",
            "pitch_level": 78, "speed": 0.83, "volume": 1.15, "mood": "warm",
            "scores": {"gender": -1.0, "pitch": 0.0, "age": 1.0, "pace": -1.0,
                       "accent": 1.0, "mood": 1.0},
            "tokens": ["warm","older","british","man","speaks","slowly"],
            "matched_words": [{"word":"warm","axis":"mood","value":1.0}, …],
            "why": "…" } }
```

* `scores` are the six axes the words move: gender, pitch, age, pace, accent,
  mood. `pitch_level` is the studio's 0-99 scale (50 = the voice's own pitch);
  `speed` is a multiplier (1.0 = the voice's own pace).
* A description with **no** register words leaves the pitch alone instead of
  inventing one.
* `dry_run` returns the plan without touching the library; the dashboard uses it
  to show the reasoning *before* you save.
* `GET /api/voices/design/providers` lists both providers with `available`,
  `needs_key`, a `hint`, and `local_models` (each with measured F0, brightness
  and words-per-second, so the choice is inspectable).

### ElevenLabs Voice Design (the only part that needs a cloud key)

```bash
# 1. does it work here?
curl -s localhost:8000/api/voices/design/providers | python -m json.tool | grep -A3 elevenlabs
# 2. try to generate — without a key you get a 400 that says exactly what to do
curl -s -X POST localhost:8000/api/voices/design/elevenlabs \
     -H 'Content-Type: application/json' -d '{"description":"deep calm narrator"}'
```

```json
{ "error": "Voice Design from a description needs an ElevenLabs key",
  "hint": "Voice Design from a description needs an ElevenLabs key. Add
           ELEVENLABS_API_KEY=sk-... to .env (elevenlabs.io → your profile →
           API keys) and restart the server. Everything else — the local neural
           voices, the library, matching a recording, remixing — works without a
           key.",
  "alternative": { "endpoint": "/api/voices/describe",
                   "detail": "design a voice from the resident neural voices without any key" } }
```

With a key in `.env` the same call generates up to three previews
(`generated_voice_id` + audio), `POST /api/voices/design/elevenlabs/save` creates
the voice on your ElevenLabs account, and the studio renders a sample and stores
it as the new library entry's reference so the local matcher can use it too.
Preview generations are billed by ElevenLabs (3 per request).

## 4. Voice remixing

Pick two or more saved voices (at least one with a reference) and combine them:

* **Blend** — weighted mix of the references (weighted average of timbres).
  Weights cannot clip by construction.
* **Stitch** — join them end to end with a 60 ms equal-power crossfade.
* **Pitch** — resample-based shift, ±12 semitones. It moves pitch *and* pace
  together (the label says so).
* **☑ Render on a neural voice** (`"match": true`) — re-measures the *blend* and
  renders it on the resident neural voice, so a remix is immediately judgeable
  by ear instead of only being a stored wav.

The result is levelled, stored as a new voice and noted as
`blend of A + B, pitch +1.5` so you can see where it came from.

## 5. Hearing a preview when the engine is not installed

`POST /api/voices/{id}/preview` speaks with the profile's own engine and falls
back to the standby engine when that engine cannot run here (no weights, no
key), and then to any other local server that is actually answering. The
response headers tell you the truth:

| Header | Meaning |
|---|---|
| `X-Voice-Engine` | the engine/model that actually produced the audio (the local server's own `X-Voice-Engine` when it reports one) |
| `X-Voice-Model` | the voice name that spoke, when the server reports it |
| `X-Voice-Mode` | `profile` (its own engine) or `standby` (fell back) |
| `X-Voice-Requested` | what the profile asked for |
| `X-Voice-Fallback` | `1` when the audio came from the standby |
| `X-Voice-Note` | the failover reason, e.g. `chatterbox unavailable: …` |

Only when *every* engine fails do you get `502` with the combined reasons and a
hint.

## 6. Offline neural voices (Piper) — the free local engine

The pipeline talks to a self-hosted OpenAI-compatible server
(`PIPER_BASE_URL` / `KOKORO_BASE_URL`, default `http://127.0.0.1:8880`). The
bundled implementation speaks with real **Piper/ONNX** voices — neural models
that run on CPU with no torch and no key:

```bash
.venv/bin/pip install piper-tts onnxruntime                       # once
.venv/bin/python scripts/fetch_neural_voices.py --list            # what is available
.venv/bin/python scripts/fetch_neural_voices.py                   # download (~60 MB each)
.venv/bin/python scripts/neural_tts_server.py --port 8880 --warm  # start it
curl -s localhost:8880/health | python -m json.tool               # 3 voices, loaded
```

* `--warm` loads every voice at start-up, so the first preview is as fast as the
  rest (~0.3 s per line).
* `GET /v1/audio/voices` returns `voice_details` (id, name, gender, accent) that
  the designer shows as *Backend voice*, and the server reports itself through
  `X-Voice-Engine` / `X-Voice-Model` so previews can be honest about who spoke.
* `POST /v1/audio/describe` and `POST /v1/audio/match` expose the same
  prompt-to-voice and matching logic over the wire.
* Voices are **not** committed to git and not vendored in any wheel; the fetch
  script streams them from public GitHub repositories and checks their SHA-256.
  Add more voices by dropping `<name>.onnx` + `<name>.onnx.json` into
  `data/models/piper/`; `/health` and the designer pick them up immediately.
* The old espeak-ng test server is gone: it was a formant synthesiser, so it
  could not tell you what a clone or a blend sounds like. Use this server.

## 7. Re-running the verification

```bash
.venv/bin/python -m pytest tests/ -q                                        # 353 tests
.venv/bin/python scripts/verify_voice_studio.py --url http://localhost:8000 # 49 live checks
.venv/bin/python scripts/smoke_test.py --url http://localhost:8000          # 126 checks
```

The studio verifier is self-cleaning: it creates voices, exercises every
endpoint (upload / path / mic / describe / match / blend / stitch / preview /
apply / rename / delete plus the validation errors) and restores whatever voice
was active before it started.

## 8. Limits worth knowing

* A **designed** voice has no reference audio, so it cannot be remixed — the API
  says exactly that instead of producing silence.
* Local prompt-to-voice picks from the resident neural voices (3 by default) and
  tunes them; it does not invent a new timbre. Arbitrary voice generation is the
  ElevenLabs Voice Design path above (key-gated, and the key is a free account
  away).
* Remixing changes *reference audio*; how faithfully ChatterBox clones a blend
  depends on the model weights, which cannot be downloaded in this sandbox.
* `Use` changes the running configuration (in-memory) and is remembered in
  `library.json`; make a choice permanent by putting it in `.env`
  (`TTS_PROVIDER`, `PIPER_VOICE`, `CHATTERBOX_MODEL`, …).
* ElevenLabs voices are selected by id, not by reference audio, so they are not
  importable into the library (the API says so).
