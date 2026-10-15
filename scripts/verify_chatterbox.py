#!/usr/bin/env python3
"""End-to-end verification of the ChatterBox voice features (live server).

Checks every documented feature against a running server: model switching,
language selection, voice cloning, the paralinguistic tags, the `.env` voice
settings, the 24 kHz -> 16 kHz pipeline, error handling and failover.

    .venv/bin/python scripts/verify_chatterbox.py --url http://localhost:9000

Exit code is 0 only when every check passes.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import urllib.error
import urllib.request
import wave

PASSED = 0
FAILED = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    PASSED += bool(ok)
    FAILED += not ok
    line = f"{'PASS' if ok else 'FAIL'}  {name}"
    if detail and not ok:
        line += f"\n        {detail}"
    print(line)


def req(base: str, method: str, path: str, body=None, raw: bool = False):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(base + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            payload = resp.read()
            return resp.status, (payload if raw else json.loads(payload)), dict(resp.headers)
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            return e.code, json.loads(payload), dict(e.headers)
        except Exception:
            return e.code, {"raw": payload[:200]}, dict(e.headers)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:9000")
    ap.add_argument("--reference", default="data/voice_preview/agent_ref.wav",
                    help="existing .wav used for the voice-clone checks")
    args = ap.parse_args()
    base = args.url.rstrip("/")

    # ---------------- 0. is this deployment configured for ChatterBox? ----------------
    st, d, _ = req(base, "GET", "/api/tts/voices")
    if st == 200 and not d.get("available_models"):
        st2, health, _ = req(base, "GET", "/health")
        print(f"SKIP  this deployment speaks with '{d.get('provider')}' "
              f"(primary: {d.get('primary')}), not ChatterBox.\n"
              f"      Re-run it with TTS_PROVIDER=chatterbox (see CHATTERBOX_FEATURES.md), "
              f"or apply a ChatterBox voice first:\n"
              f"        curl -s -X POST {base}/api/voices -H 'Content-Type: application/json' \\\n"
              f"             -d '{{\"name\":\"cb\",\"path\":\"<a .wav on the server>\",\"model\":\"multilingual\"}}'\n"
              f"        curl -s -X POST {base}/api/voices/<id>/apply")
        return 0

    # ---------------- 1. model switching ----------------
    check("GET /api/tts/voices lists all four models",
          st == 200 and d.get("available_models") == ["turbo", "nano", "multilingual", "original"],
          f"{st} {sorted(d)}")
    for model in ("turbo", "nano", "multilingual", "original"):
        st, d, _ = req(base, "POST", "/api/tts/switch-voice", {"model": model})
        check(f"switch model -> {model}",
              st == 200 and d.get("ok") is True
              and d.get("result") == f"Switched to Chatterbox-{model}"
              and d["model_info"]["model"] == model, f"{st} {d}")

    st, d, _ = req(base, "POST", "/api/tts/switch-voice", {"model": "multilingual"})
    info = d.get("model_info", {})
    check("multilingual advertises 23 languages", len(info.get("languages", [])) == 23,
          str(len(info.get("languages", []))))
    check("language names are supplied (es=Spanish, de=German)",
          info.get("available_languages", {}).get("es") == "Spanish"
          and info.get("available_languages", {}).get("de") == "German")

    # ---------------- 2. language selection ----------------
    st, d, _ = req(base, "POST", "/api/tts/switch-voice", {"language": "es"})
    check("multilingual accepts 'es'", st == 200 and d["applied"]["language"] == "es"
          and d["model_info"]["language"] == "es", f"{st} {d}")
    st, d, _ = req(base, "POST", "/api/tts/switch-voice", {"language": "de"})
    check("multilingual accepts 'de'", st == 200 and d["model_info"]["language"] == "de", f"{st} {d}")
    st, d, _ = req(base, "POST", "/api/tts/switch-voice", {"language": "klingon"})
    check("unknown language -> 400 listing the valid codes",
          st == 400 and "klingon" in d.get("error", "")
          and len(d.get("available_languages", [])) == 23, f"{st} {d}")
    st, d, _ = req(base, "POST", "/api/tts/switch-voice", {"model": "turbo", "language": "es"})
    check("language on an English-only engine -> 400",
          st == 400 and "English-only" in d.get("error", ""), f"{st} {d}")

    # ---------------- 3. voice cloning ----------------
    ref = args.reference
    st, d, _ = req(base, "POST", "/api/tts/switch-voice", {"model": "multilingual", "voice": ref})
    check("a real .wav clones the voice",
          st == 200 and d["model_info"]["voice_cloned"] is True
          and d["applied"]["voice"] == ref
          and not d["model_info"]["voice_reference_problem"], f"{st} {d}")
    st, d, _ = req(base, "POST", "/api/tts/switch-voice", {"voice": "/no/such/ref.wav"})
    check("missing reference -> 400 'not found on the server'",
          st == 400 and "not found on the server" in d.get("error", ""), f"{st} {d}")
    st, d, _ = req(base, "POST", "/api/tts/switch-voice", {"voice": ref.rsplit(".", 1)[0] + ".txt"})
    check("non-wav reference -> 400", st == 400, f"{st} {d}")
    st, d, _ = req(base, "POST", "/api/tts/switch-voice", {"voice": ""})
    check("empty voice clears the clone", st == 200 and d["applied"]["voice"] == ""
          and d["model_info"]["voice_cloned"] is False, f"{st} {d}")

    # ---------------- 4. paralinguistic tags ----------------
    st, d, _ = req(base, "POST", "/api/tts/switch-voice", {"model": "turbo"})
    check("turbo advertises paralinguistic tags",
          d["model_info"]["paralinguistic_tags"] is True, str(d.get("model_info", {}).get("paralinguistic_tags")))
    st, d, _ = req(base, "POST", "/api/tts/switch-voice", {"model": "original"})
    check("original does not advertise tags",
          d["model_info"]["paralinguistic_tags"] is False)
    try:
        html = urllib.request.urlopen(base + "/", timeout=10).read().decode()
    except Exception as e:                                     # pragma: no cover
        html = ""
        check("dashboard reachable", False, str(e))
    check("dashboard ships the clickable tag chips",
          all(t in html for t in ("[laugh]", "[chuckle]", "[cough]")), "chips missing from /")

    # ---------------- 5. settings from .env ----------------
    st, d, _ = req(base, "GET", "/api/tts/voices")
    info = d.get("model_info", {})
    check("exaggeration / cfg_weight / temperature exposed",
          all(isinstance(info.get(k), float) for k in ("exaggeration", "cfg_weight", "temperature")),
          str({k: info.get(k) for k in ("exaggeration", "cfg_weight", "temperature")}))

    # ---------------- 6. 24 kHz -> 16 kHz resampling ----------------
    check("engine generates at 24 kHz, pipeline outputs 16 kHz",
          info.get("sample_rate") == 24000 and info.get("output_rate") == 16000,
          f"{info.get('sample_rate')} -> {info.get('output_rate')}")
    spoken = "I am Lina from EverTrust and this is a spoken sentence, not a beep."
    st, raw, headers = req(base, "POST", "/api/debug/speak", {"text": spoken}, raw=True)
    peak, secs = 0, 0.0
    try:
        w = wave.open(io.BytesIO(raw))
        secs = w.getnframes() / max(1, w.getframerate())
        pcm = w.readframes(w.getnframes())
        peak = max(abs(int.from_bytes(pcm[i:i + 2], "little", signed=True))
                   for i in range(0, len(pcm) - 1, 2)) if pcm else 0
        ok_wav = w.getframerate() == 16000 and w.getnchannels() == 1 and w.getnframes() > 0
    except Exception as e:
        ok_wav = False
        w = None
    check("live audio is a 16 kHz mono WAV",
          st == 200 and ok_wav and headers.get("content-type") == "audio/wav",
          f"HTTP {st} {headers.get('content-type')} "
          f"{'bad wav: ' + str(e) if w is None else f'{w.getframerate()}Hz {w.getnchannels()}ch {w.getnframes()}f'}")
    # Silence and "a fixed-length beep" are the two failure modes that make the
    # dashboard look like it has no audio at all: real speech is loud enough and
    # its length scales with the sentence.
    check("the audio is audible (not silence)",
          peak > 500, f"peak sample = {peak}")
    check("the audio is speech, not a fixed-length tone",
          secs >= 0.9 + 0.02 * len(spoken), f"{secs:.2f}s for {len(spoken)} characters")

    # ---------------- 7. error handling + failover ----------------
    st, d, _ = req(base, "POST", "/api/tts/switch-voice", {"model": "nonexistent"})
    check("unknown model -> 400 (never 500) with available_models",
          st == 400 and d.get("available_models") == ["turbo", "nano", "multilingual", "original"],
          f"{st} {d}")
    st, d, _ = req(base, "POST", "/api/tts/switch-voice", {"model": ""})
    check("empty model -> 400", st == 400, f"{st} {d}")

    # A synth request drives the real failover path when ChatterBox cannot run
    # here, so the standby speaks and the reason is recorded.
    req(base, "POST", "/api/debug/speak", {"text": "trigger failover"}, raw=True)
    st, d, _ = req(base, "GET", "/health")
    check("failover is recorded with a reason",
          st == 200 and d.get("tts") == "kokoro" and d.get("tts_primary") == "chatterbox"
          and bool(d.get("tts_failover_reason")),
          str({k: d.get(k) for k in ("tts", "tts_primary", "tts_standby", "tts_failover_reason")}))

    # ---------------- 8. the documented curl, while failed over ----------------
    st, d, _ = req(base, "POST", "/api/tts/switch-voice", {"model": "multilingual"})
    check("documented switch-voice request still succeeds during a failover",
          st == 200 and d.get("ok") is True and d.get("provider") == "chatterbox"
          and d.get("result") == "Switched to Chatterbox-multilingual"
          and d["model_info"]["multilingual"] is True
          and len(d["model_info"]["languages"]) == 23, f"{st} {d}")
    st, d, _ = req(base, "GET", "/api/tts/voices")
    check("dashboard payload keeps models + info + failover reason",
          d.get("available_models") == ["turbo", "nano", "multilingual", "original"]
          and d.get("model_info", {}).get("output_rate") == 16000
          and bool(d.get("failover_reason")), f"keys={sorted(d)}")

    total = PASSED + FAILED
    print("=" * 68)
    print(f"{'ALL ' + str(total) + ' CHECKS PASSED' if not FAILED else str(FAILED) + ' of ' + str(total) + ' CHECKS FAILED'}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
