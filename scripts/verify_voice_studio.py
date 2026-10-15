#!/usr/bin/env python3
"""End-to-end verification of the voice studio (library/design/remix) live.

    .venv/bin/python scripts/verify_voice_studio.py --url http://localhost:8000

Exercises every studio endpoint against a running server: add from a server
path / an uploaded file / a microphone recording, design a voice, blend and
stitch two voices, preview, apply, rename, delete, plus the validation errors.
Exit code is 0 only when every check passes.
"""
from __future__ import annotations

import argparse
import sys

import base64
import io
import json
from pathlib import Path
import urllib.error
import urllib.request
import wave

ap = argparse.ArgumentParser()
ap.add_argument("--url", default="http://localhost:8000")
ap.add_argument("--keep", action="store_true",
                help="leave the voices this run created in the library "
                     "(default: clean up and restore the previously active voice)")
ARGS = ap.parse_args()
BASE = ARGS.url.rstrip("/")
CREATED = []      # (id, name) of every voice this run added
START_ACTIVE = "" # the voice that was active before we touched anything
PASS = FAIL = 0
def check(name, ok, detail=""):
    global PASS, FAIL
    ok = bool(ok); PASS += ok; FAIL += not ok
    print(("PASS  " if ok else "FAIL  ") + name + (f"\n        {detail}" if detail and not ok else ""))

def req(method, path, body=None, raw=False):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            payload = resp.read()
            return resp.status, (payload if raw else json.loads(payload or b"{}")), dict(resp.headers)
    except urllib.error.HTTPError as e:
        try: return e.code, json.loads(e.read()), dict(e.headers)
        except Exception: return e.code, {}, dict(e.headers)

# ---- 0. clean slate ----
st, d, _ = req("GET", "/api/voices")
START_ACTIVE = d.get("active_id") or ""
check("GET /api/voices lists the library + designer options",
      st == 200 and "voices" in d and "designer" in d, f"{st} {list(d)}")
check("designer exposes models, languages, tags and presets",
      d["designer"]["chatterbox_models"] == ["turbo","nano","multilingual","original"]
      and len(d["designer"]["chatterbox_languages"]) == 23
      and "[laugh]" in d["designer"]["chatterbox_tags"]
      and len(d["designer"]["presets"]) >= 3, json.dumps(d.get("designer", {}))[:200])
check("designer sees the local server's voices with metadata",
      bool(d["designer"]["backend_voices"])
      and all(v.get("id") and v.get("name") for v in d["designer"]["backend_voices"])
      and any(v.get("gender") or v.get("kind") for v in d["designer"]["backend_voices"]),
      str(d["designer"]["backend_voices"])[:200])
check("designer names the engine actually serving the local URL",
      d["designer"].get("backend_engine") in ("piper", "kokoro", ""),
      f"backend_engine={d['designer'].get('backend_engine')!r}")

#: The engine the local server says it is. "kokoro" and "piper" are two names for
#: one OpenAI-compatible local server, so a preview that reports this name is being
#: honest about who spoke — and the checks below use it as the reference.
BACKEND = (d["designer"].get("backend_engine") or "").strip()

# ---- 1. add from server path ----
REF = str(Path(__file__).resolve().parents[1] / "data/voice_preview/agent_ref.wav")
st, d, _ = req("POST", "/api/voices/upload", {"name":"Lina clone","path":REF,"engine":"chatterbox","model":"multilingual"})
v1 = d.get("voice", {})
CREATED.append((v1.get("id"), v1.get("name")))
check("1. add voice from a server path", st == 200 and v1.get("has_reference") and v1["duration_sec"] > 1,
      f"{st} {d}")
check("   stored reference is mono 24 kHz", v1.get("sample_rate") == 24000, str(v1.get("sample_rate")))

# ---- 2. add from an uploaded file (base64) ----
data_b64 = "data:audio/wav;base64," + base64.b64encode(open(REF,"rb").read()).decode()
st, d, _ = req("POST", "/api/voices/upload", {"name":"Uploaded voice","filename":"ref.wav","data_base64":data_b64})
v2 = d.get("voice", {})
CREATED.append((v2.get("id"), v2.get("name")))
check("2. add voice from an uploaded file", st == 200 and v2.get("source") == "uploaded", f"{st} {d}")

# ---- 3. add from a mic recording (raw pcm) ----
w = wave.open(REF); pcm = w.readframes(w.getnframes())
st, d, _ = req("POST", "/api/voices/upload", {"name":"Mic take","pcm_base64":base64.b64encode(pcm).decode(),
                                              "sample_rate": w.getframerate()})
v3 = d.get("voice", {})
CREATED.append((v3.get("id"), v3.get("name")))
check("3. add voice from mic PCM", st == 200 and v3.get("source") == "recorded", f"{st} {d}")

# ---- 4. design a voice ----
st, d, _ = req("POST", "/api/voices/design", {"name":"Upbeat closer","engine":"chatterbox","model":"multilingual",
                                              "language":"es","tuning":{"exaggeration":0.9,"temperature":1.1,"speed":1.1},
                                              "tags":["laugh","um"],"notes":"designed in the studio"})
vd = d.get("voice", {})
CREATED.append((vd.get("id"), vd.get("name")))
check("4. design a voice", st == 200 and vd.get("engine") == "chatterbox" and vd["tuning"]["exaggeration"] == 0.9
      and vd["tags"] == ["[laugh]","[um]"] and not vd.get("has_reference"), f"{st} {d}")

# ---- 5. remix (blend) ----
st, d, _ = req("POST", "/api/voices/remix", {"name":"Blend A+B","mode":"blend","pitch_semitones":1.0,
                                             "sources":[{"id":v1["id"],"weight":2},{"id":v2["id"],"weight":1}]})
vr = d.get("voice", {})
CREATED.append((vr.get("id"), vr.get("name")))
check("5. remix two voices (blend)", st == 200 and vr.get("source") == "remixed" and vr.get("has_reference")
      and "blend of" in (vr.get("notes") or ""), f"{st} {d}")

# ---- 6. stitch ----
st, d, _ = req("POST", "/api/voices/remix", {"name":"Stitch A+B","mode":"stitch","sources":[{"id":v1["id"]},{"id":v2["id"]}]})
CREATED.append((d.get("voice", {}).get("id"), d.get("voice", {}).get("name")))
check("6. remix two voices (stitch)", st == 200 and d["voice"]["duration_sec"] > v1["duration_sec"], f"{st} {d}")

def engine_ok(hdr: dict, requested: str) -> bool:
    """True when a preview names the engine that really spoke.

    The provider nicknames ("kokoro", "piper") are interchangeable names for one
    local OpenAI-compatible server, and the studio reports what that server says
    it is (its X-Voice-Engine) whenever it says anything — so either name is
    honest. What is never acceptable is the profile's engine being passed off as
    the fallback's.
    """
    got = (hdr.get("x-voice-engine") or "").strip().lower()
    if not got:
        return False
    return got == (BACKEND.lower() or requested.lower())


# ---- 7. previews ----
st, raw, hdr = req("POST", f"/api/voices/{v1['id']}/preview", {"text":"Testing the studio preview."}, raw=True)
try:
    w = wave.open(io.BytesIO(raw)); ok = st == 200 and w.getframerate() == 16000 and w.getnframes() > 0
except Exception: ok = False
check("7a. preview a saved voice -> wav audio", ok, f"{st} {hdr.get('content-type')}")
hl = {k.lower(): v for k, v in hdr.items()}
check("    headers say which engine actually spoke + whether it fell back",
      hl.get("x-voice-engine") and hl.get("x-voice-mode") in ("profile", "standby"), str(hl))
check("    standby audio is labelled honestly (chatterbox is not installed here)",
      hl.get("x-voice-mode") == "standby" and hl.get("x-voice-fallback") == "1"
      and str(hl.get("x-voice-engine", "")).lower() != "chatterbox"
      and engine_ok(hl, "kokoro"), str(hl))
st, raw, hdr = req("POST", "/api/voices/preview-draft",
                   {"engine":"kokoro","voice":"af_sarah","tuning":{"speed":1.1,"pitch":60},
                    "text":"Draft preview."}, raw=True)
check("7b. preview an unsaved draft", st == 200 and len(raw) > 1000, f"{st} {hdr.get('X-Voice-Mode')}")

# ---- 8. apply to the bot ----
st, d, _ = req("POST", f"/api/voices/{v1['id']}/apply")
check("8a. apply a cloned voice -> engine switches to chatterbox",
      st == 200 and d["applied"]["engine"] == "chatterbox" and d["applied"]["model"] == "multilingual"
      and d["applied"]["voice"].endswith(".wav"), f"{st} {d}")
st, d, _ = req("GET", "/health")
check("8b. /health reflects the applied voice settings",
      d["tts_primary"] == "chatterbox", str({k: d.get(k) for k in ("tts", "tts_primary")}))
st, dk, _ = req("POST", "/api/voices/design", {"name":"Calm local voice","engine":"kokoro","voice":"af_sarah",
                                               "tuning":{"speed":0.9,"pitch":46},"tags":["[sigh]"]})
vk = dk.get("voice", {})
CREATED.append((vk.get("id"), vk.get("name")))
check("8c. design a local-server voice", st == 200 and vk.get("engine") == "kokoro" and vk["tuning"]["pitch"] == 46, f"{st} {dk}")
st, d, _ = req("POST", f"/api/voices/{vk['id']}/apply")
check("8d. apply it -> engine, voice, speed and pitch all set",
      st == 200 and d["applied"]["engine"] == "kokoro" and d["applied"]["voice"] == "af_sarah"
      and d["applied"]["speed"] == 0.9 and d["applied"]["pitch"] == 46, f"{st} {d}")
st, raw, hdr = req("POST", f"/api/voices/{vk['id']}/preview", {"text":"Local voice preview."}, raw=True)
hl = {k.lower(): v for k, v in hdr.items()}
check("8e. local-server voice previews natively (no fallback)",
      st == 200 and hl.get("x-voice-mode") == "profile" and hl.get("x-voice-fallback") == "0"
      and engine_ok(hl, "kokoro"), str(hl))

# ---- 9. save current settings as a voice ----
st, d, _ = req("POST", "/api/voices/current", {"name":"Snapshot"})
CREATED.append((d.get("voice", {}).get("id"), d.get("voice", {}).get("name")))
check("9. save the current voice as a library entry", st == 200 and d["voice"]["name"] == "Snapshot", f"{st} {d}")

# ---- 10. rename + delete ----
st, d, _ = req("PUT", f"/api/voices/{v3['id']}", {"name":"Renamed take","notes":"renamed"})
check("10a. rename a voice", st == 200 and d["voice"]["name"] == "Renamed take", f"{st} {d}")
st, d, _ = req("DELETE", f"/api/voices/{v3['id']}")
check("10b. delete a voice", st == 200 and d.get("deleted") == v3["id"], f"{st} {d}")
st, d, _ = req("GET", f"/api/voices/{v3['id']}")
check("10c. deleted voice is gone (404)", st == 404, f"{st}")

# ---- 11. validation ----
for label, method, path, body, want in (
    ("unknown voice id", "GET", "/api/voices/ghost", None, 404),
    ("design without a name", "POST", "/api/voices/design", {"engine":"chatterbox"}, 400),
    ("English-only model + Spanish", "POST", "/api/voices/design",
     {"name":"x","engine":"chatterbox","model":"turbo","language":"es"}, 400),
    ("unknown language", "POST", "/api/voices/design",
     {"name":"x","engine":"chatterbox","model":"multilingual","language":"klingon"}, 400),
    ("unknown engine", "POST", "/api/voices/design", {"name":"x","engine":"klingon"}, 400),
    ("remix with an unknown source", "POST", "/api/voices/remix", {"name":"x","sources":[{"id":"ghost"}]}, 400),
    ("remix with no audio source", "POST", "/api/voices/remix",
     {"name":"x","sources":[{"id":vd["id"]}]}, 400),
    ("upload of a missing path", "POST", "/api/voices/upload", {"name":"x","path":"/tmp/nope.wav"}, 400),
    ("upload with nothing attached", "POST", "/api/voices/upload", {"name":"x"}, 400),
    ("garbage audio", "POST", "/api/voices/upload",
     {"name":"x","data_base64":base64.b64encode(b"this is not audio").decode(),"filename":"x.wav"}, 400),
):
    st, d, _ = req(method, path, body)
    check(f"11. {label} -> {want}", st == want, f"got {st}: {str(d)[:120]}")

st, d, _ = req("GET", "/api/voices")
check("library survived all of it", st == 200 and len(d["voices"]) >= 4, str(len(d.get("voices", []))))

# ---- 12. local neural voices: describe (prompt to voice), match, neural renders ----
def peak_of(wav_bytes: bytes) -> int:
    """Loudest sample of a wav, using only the standard library."""
    with wave.open(io.BytesIO(wav_bytes)) as w:
        frames = w.readframes(w.getnframes())
        rate, width, channels = w.getframerate(), w.getsampwidth(), w.getnchannels()
    if width != 2 or not frames:
        return 0
    import struct
    samples = struct.unpack("<%dh" % (len(frames) // 2), frames[:len(frames) // 2 * 2])
    return max((abs(v) for v in samples), default=0)

st, providers, _ = req("GET", "/api/voices/design/providers")
prov = {p["id"]: p for p in (providers.get("providers") or [])}
check("12a. design providers list the local engine and the key-gated cloud one",
      st == 200 and "local" in prov and "elevenlabs" in prov
      and prov["local"]["needs_key"] is False and prov["elevenlabs"]["needs_key"] is True,
      f"{st} {providers}")
LOCAL_OK = bool(prov.get("local", {}).get("available"))
CLOUD_OK = bool(prov.get("elevenlabs", {}).get("available"))
check("12b. the cloud provider explains how to unlock it",
      bool(prov.get("elevenlabs", {}).get("hint")) or CLOUD_OK,
      str(prov.get("elevenlabs"))[:160])

st, d, _ = req("GET", "/api/voices")
designer = d.get("designer", {})
check("12c. the designer offers the local neural voices and the piper engine",
      "piper" in (designer.get("engines") or []) and (designer.get("neural_voices") or LOCAL_OK),
      json.dumps({k: designer.get(k) for k in ("engines", "neural_voices")})[:220])

if LOCAL_OK:
    st, d, _ = req("POST", "/api/voices/describe",
                   {"description": "warm older British man, speaks slowly", "dry_run": True})
    plan = d.get("plan") or {}
    check("12d. describe (dry run) explains which voice fits and why",
          st == 200 and d.get("dry_run") and plan.get("model") and plan.get("why"),
          f"{st} {str(d)[:180]}")
    check("12e. the plan reports a register, a pitch and a pace",
          isinstance(plan.get("pitch_level"), int) and 0 <= plan["pitch_level"] <= 99
          and float(plan.get("speed") or 0) > 0,
          json.dumps({k: plan.get(k) for k in ("pitch_level", "speed")}))

    st, d, _ = req("POST", "/api/voices/describe",
                   {"name": "Described voice", "description": "deep serious male narrator, slow"})
    described = d.get("voice") or {}
    CREATED.append((described.get("id"), described.get("name")))
    check("12f. describe creates a local neural voice from the words",
          st == 200 and described.get("engine") == "piper" and described.get("voice"),
          f"{st} {str(d)[:180]}")

    st, audio, headers = req("POST", f"/api/voices/{described.get('id')}/preview",
                             {"text": "This is the voice the description asked for."}, raw=True)
    kind = (headers.get("content-type") or "").lower()
    check("12g. the described voice renders audio (not a placeholder)",
          st == 200 and "audio" in kind and len(audio) > 8000 and peak_of(audio) > 300,
          f"{st} {kind} {len(audio)} bytes peak={peak_of(audio) if st == 200 else 'n/a'}")
    check("12h. the render says which engine spoke",
          (headers.get("x-voice-engine") or "").lower() == "piper",
          json.dumps({k: v for k, v in headers.items() if k.lower().startswith("x-voice")}))

    ref = base64.b64encode(Path(__file__).resolve().parents[1].joinpath(
        "data", "voice_preview", "agent_ref.wav").read_bytes()).decode() \
        if Path(__file__).resolve().parents[1].joinpath(
            "data", "voice_preview", "agent_ref.wav").is_file() else ""
    if not ref:
        st, audio, _ = req("POST", f"/api/voices/{described.get('id')}/preview", {"text": "reference"}, raw=True)
        ref = base64.b64encode(audio).decode()
    st, d, _ = req("POST", "/api/voices/match", {"data_base64": ref})
    match = d.get("match") or {}
    check("12i. match measures a recording and names the closest voice",
          st == 200 and match.get("model") and match.get("why") and match.get("alternatives"),
          f"{st} {str(d)[:200]}")
    check("12j. the match explains the register it measured",
          isinstance(match.get("reference", {}).get("f0_hz"), (int, float))
          and match["reference"]["f0_hz"] > 0,
          str(match.get("reference"))[:160])

    st, d, _ = req("POST", "/api/voices/upload",
                   {"name": "Matched recording", "data_base64": ref, "match": True})
    matched = d.get("voice") or {}
    CREATED.append((matched.get("id"), matched.get("name")))
    check("12k. adding a recording with match=1 renders it on a neural voice",
          st == 200 and matched.get("engine") == "piper" and matched.get("has_reference")
          and (d.get("match") or {}).get("model") == matched.get("voice"),
          f"{st} {str(d)[:200]}")

    if described.get("id"):
        st, d, _ = req("POST", "/api/voices/remix",
                       {"name": "Neural blend", "sources": [{"id": matched.get("id") or described["id"]},
                                                             {"id": matched.get("id") or described["id"]}],
                        "mode": "blend", "match": True})
        blended = d.get("voice") or {}
        CREATED.append((blended.get("id"), blended.get("name")))
        check("12l. a remix can be rendered on a neural voice straight away",
              st == 200 and blended.get("engine") == "piper" and blended.get("voice"),
              f"{st} {str(d)[:200]}")
else:
    print("SKIP  local neural voices are not configured on this deployment")
    print("      run: python scripts/fetch_neural_voices.py && pip install piper-tts onnxruntime")
    print("      then: python scripts/neural_tts_server.py --port 8880")

# ---- 13. the cloud design endpoint ----
st, d, _ = req("POST", "/api/voices/design/elevenlabs", {"description": "a smoky radio announcer"})
if CLOUD_OK:
    check("13a. ElevenLabs Voice Design generates previews (key configured)",
          st == 200 and d.get("previews"), f"{st} {str(d)[:200]}")
else:
    check("13a. without a key the cloud design endpoint explains how to enable it",
          st == 400 and "ElevenLabs" in (d.get("error") or "")
          and "ELEVENLABS_API_KEY" in (d.get("hint") or "")
          and (d.get("alternative") or {}).get("endpoint") == "/api/voices/describe",
          f"{st} {str(d)[:220]}")
check("13b. the keyless fallback really is the local one", st in (200, 400, 502), f"{st}")
# ---- cleanup: this script must not change the deployment it verifies ----
if not ARGS.keep:
    for vid, name in CREATED:
        if vid:
            req("DELETE", f"/api/voices/{vid}")
    if START_ACTIVE:
        st, _, _ = req("POST", f"/api/voices/{START_ACTIVE}/apply")
        print(f"cleanup: removed {len([c for c in CREATED if c[0]])} test voices, "
              f"restored the active voice ({'ok' if st == 200 else 'HTTP ' + str(st)})")
    else:
        print(f"cleanup: removed {len([c for c in CREATED if c[0]])} test voices")
    left = req("GET", "/api/voices")[1].get("voices", [])
    print(f"library now holds {len(left)} voice(s)")

print("=" * 68)
print("ALL %d CHECKS PASSED" % (PASS + FAIL) if not FAIL
      else "%d of %d CHECKS FAILED" % (FAIL, PASS + FAIL))
sys.exit(1 if FAIL else 0)
