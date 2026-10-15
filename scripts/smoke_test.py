"""Full-feature smoke test for a running server (debug mode).

Exercises every HTTP endpoint, every campaign, and every disposition path the
conversation engine can reach, plus the real-time voice WebSocket — so one
command tells you whether the whole product still works end to end.

Usage
-----
    python cli.py debug                       # in one terminal
    python scripts/smoke_test.py              # in another (defaults to :8000)
    python scripts/smoke_test.py --url http://localhost:9000
    python scripts/smoke_test.py --no-ws      # skip the WebSocket part

Exit code is 0 only when every check passes.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "http://localhost:8000"

# --------------------------------------------------------------------------
# tiny test harness
# --------------------------------------------------------------------------


class Report:
    def __init__(self) -> None:
        self.passed = 0
        self.failed: list[tuple[str, str]] = []
        self.section = ""

    def head(self, title: str) -> None:
        self.section = title
        print(f"\n\033[1m{title}\033[0m")

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        if ok:
            self.passed += 1
            print(f"  \033[32mPASS\033[0m  {name}")
        else:
            self.failed.append((f"{self.section} :: {name}", detail))
            print(f"  \033[31mFAIL\033[0m  {name}")
            if detail:
                print(f"        {detail}")
        return ok

    def eq(self, name: str, got, want) -> bool:
        return self.check(name, got == want, f"got {got!r}, expected {want!r}")

    def summary(self) -> int:
        total = self.passed + len(self.failed)
        print("\n" + "=" * 68)
        if not self.failed:
            print(f"\033[32mALL {total} CHECKS PASSED\033[0m")
            return 0
        print(f"\033[31m{len(self.failed)} of {total} CHECKS FAILED\033[0m")
        for name, detail in self.failed:
            print(f"  - {name}")
            if detail:
                print(f"      {detail}")
        return 1


def http(method: str, url: str, body=None, timeout: int = 60):
    """Return (status, body_bytes). Never raises for HTTP error codes."""
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return 0, str(e).encode()


def http_headers(method: str, url: str, timeout: int = 60):
    """Like http(), but also returns the response headers (lower-cased keys)."""
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read()
    except Exception as e:
        return 0, {}, str(e).encode()


def jget(url: str, timeout: int = 60):
    code, raw = http("GET", url, timeout=timeout)
    try:
        return code, json.loads(raw.decode())
    except Exception:
        return code, {}


def jpost(url: str, body=None, timeout: int = 60):
    code, raw = http("POST", url, body, timeout=timeout)
    try:
        return code, json.loads(raw.decode())
    except Exception:
        return code, {}


# --------------------------------------------------------------------------
# conversation driver
# --------------------------------------------------------------------------


def run_call(base: str, utterances: list[str]) -> dict:
    """Start a debug session and feed it `utterances`; return the final state."""
    code, started = jpost(f"{base}/api/debug/start")
    if code != 200 or "session_id" not in started:
        return {"error": f"start failed: {code} {started}"}
    sid = started["session_id"]
    last = {"agent": started.get("agent"), "ended": False, "disposition": ""}
    for text in utterances:
        code, resp = jpost(f"{base}/api/debug/chat",
                           {"session_id": sid, "text": text})
        if code != 200:
            return {"error": f"chat {code}: {resp}", "session_id": sid}
        last = resp
        if resp.get("ended"):
            break
    last["session_id"] = sid
    last["greeting"] = started.get("agent")
    return last


def deploy(base: str, cid: str) -> None:
    jpost(f"{base}/api/campaigns/active", {"id": cid})


# --------------------------------------------------------------------------
# test sections
# --------------------------------------------------------------------------


def test_pages(base: str, r: Report) -> None:
    r.head("1. Web pages (dashboard / console / campaign manager)")
    for path, marker in (
        ("/", "Recordings"),
        ("/debug", "WebSocket"),
        ("/campaigns", "Campaign"),
    ):
        code, raw = http("GET", f"{base}{path}")
        body = raw.decode("utf-8", "replace")
        r.check(f"GET {path} → 200", code == 200, f"status {code}")
        r.check(f"GET {path} contains page markup",
                len(body) > 2000 and marker.lower() in body.lower(),
                f"{len(body)} bytes; marker {marker!r} "
                f"{'found' if marker.lower() in body.lower() else 'MISSING'}")


def test_health(base: str, r: Report) -> None:
    r.head("2. Health & runtime configuration")
    code, body = jget(f"{base}/health")
    r.check("GET /health → 200", code == 200, f"status {code}")
    r.eq("health.status == ok", body.get("status"), "ok")
    r.check("health reports a TTS engine", bool(body.get("tts")), f"tts={body.get('tts')!r}")
    r.check("health reports an LLM provider", bool(body.get("llm")), f"llm={body.get('llm')!r}")
    r.check("health exposes tts_engines map",
            isinstance(body.get("tts_engines"), dict) and len(body["tts_engines"]) >= 4)
    r.check("health debug flag is true", body.get("debug") is True)
    r.check("health reports call count", isinstance(body.get("calls"), int))

    code, cfg = jget(f"{base}/api/config")
    r.check("GET /api/config → 200", code == 200, f"status {code}")
    for key in ("mode", "llm_provider", "stt_provider", "tts_provider", "agent_name"):
        r.check(f"config has {key}", key in cfg)
    r.check("config exposes campaign engine settings",
            "closer_queue" in cfg and "vicidial_enabled" in cfg)

    code, voice = jget(f"{base}/api/debug/voice")
    r.check("GET /api/debug/voice → 200", code == 200, f"status {code}")
    for key in ("speak", "mic", "tts_provider", "stt_provider", "llm", "realtime"):
        r.check(f"voice status has {key}", key in voice)


def test_call_log(base: str, r: Report) -> None:
    r.head("3. Call log (write + read + persistence)")
    code, resp = jpost(f"{base}/api/calls", {
        "lead_id": "smoke-test", "action": "hangup",
        "disposition": "SMOKE-TEST", "facts": {"note": "from smoke_test.py"},
    })
    r.check("POST /api/calls → 200", code == 200, f"status {code}")
    r.check("POST /api/calls returns an id", bool(resp.get("id")), f"{resp}")

    code, listing = jget(f"{base}/api/calls?limit=50")
    r.check("GET /api/calls → 200", code == 200, f"status {code}")
    calls = listing.get("calls", [])
    r.check("call log contains the entry we just wrote",
            any(c.get("disposition") == "SMOKE-TEST" for c in calls),
            f"{len(calls)} calls; dispositions="
            f"{sorted({c.get('disposition') for c in calls})}")

    code, limited = jget(f"{base}/api/calls?limit=1")
    r.eq("limit=1 returns exactly one call", len(limited.get("calls", [])), 1)


def test_campaigns(base: str, r: Report) -> None:
    r.head("4. Campaign manager (list / get / create / edit / deploy / delete)")
    code, body = jget(f"{base}/api/campaigns")
    r.check("GET /api/campaigns → 200", code == 200, f"status {code}")
    ids = [c["id"] for c in body.get("campaigns", [])]
    for cid in ("medicare", "final_expense", "aca", "hi"):
        r.check(f"built-in campaign present: {cid}", cid in ids, f"ids={ids}")

    code, active = jget(f"{base}/api/campaigns/active")
    r.check("GET /api/campaigns/active → 200", code == 200, f"status {code}")
    r.check("active campaign reports compliance block",
            "compliance" in active and "verification_steps" in active.get("compliance", {}))

    code, one = jget(f"{base}/api/campaigns/medicare")
    r.check("GET /api/campaigns/medicare → 200", code == 200, f"status {code}")
    r.check("campaign payload includes questions + lines",
            bool(one.get("questions")) and isinstance(one.get("lines"), dict))
    r.eq("unknown campaign → 404", jget(f"{base}/api/campaigns/ghost")[0], 404)

    # create (clone)
    code, made = jpost(f"{base}/api/campaigns", {"name": "Smoke Clone", "base": "medicare"})
    r.check("POST /api/campaigns creates a clone", code == 200 and made.get("ok"),
            f"{code} {made}")
    clone_id = made.get("id", "")
    if clone_id:
        code, clone = jget(f"{base}/api/campaigns/{clone_id}")
        r.eq("clone copied the verification steps",
             len(clone.get("questions", [])), 2)

        # edit + persist round trip
        clone["name"] = "Smoke Clone Renamed"
        if clone.get("questions"):
            clone["questions"][0]["ask_line"] = "Smoke test question?"
        code, saved = http("PUT", f"{base}/api/campaigns/{clone_id}", clone)
        r.check("PUT /api/campaigns/{id} saves", code == 200, f"status {code}")
        code, again = jget(f"{base}/api/campaigns/{clone_id}")
        r.eq("edit persisted (name)", again.get("name"), "Smoke Clone Renamed")
        if again.get("questions"):
            r.eq("edit persisted (question text)",
                 again["questions"][0].get("ask_line"), "Smoke test question?")

        # a comment-3 answer 91 must not transfer once max age is edited down
        edited = jget(f"{base}/api/campaigns/{clone_id}")[1]
        for q in edited.get("questions", []):
            if q.get("kind") == "age":
                q["age_max"] = 70
        http("PUT", f"{base}/api/campaigns/{clone_id}", edited)
        deploy(base, clone_id)
        res = run_call(base, ["yeah pretty good thanks", "yes I have medicare",
                              "I am 75 years old"])
        r.eq("live edit is honored by the engine (75 > max 70 → not eligible)",
             res.get("disposition"), "AI-NOT-ELIGIBLE")

        # delete guards
        r.eq("cannot delete the live campaign (409)",
             http("DELETE", f"{base}/api/campaigns/{clone_id}")[0], 409)

        # restore + delete
        deploy(base, "medicare")
        r.eq("can delete a non-live custom campaign",
             http("DELETE", f"{base}/api/campaigns/{clone_id}")[0], 200)
        r.eq("deleted campaign is gone", jget(f"{base}/api/campaigns/{clone_id}")[0], 404)

    r.eq("built-in campaign cannot be deleted (409)",
         http("DELETE", f"{base}/api/campaigns/medicare")[0], 409)
    r.eq("deploying an unknown campaign → 404",
         jpost(f"{base}/api/campaigns/active", {"id": "ghost"})[0], 404)
    r.eq("reset requires a built-in id → 404",
         jpost(f"{base}/api/campaigns/ghost/reset")[0], 404)
    code, reset = jpost(f"{base}/api/campaigns/medicare/reset")
    r.check("POST /api/campaigns/medicare/reset works", code == 200, f"status {code}")
    r.eq("medicare range restored to 45-90",
         jget(f"{base}/api/campaigns/medicare")[1].get("questions", [{}])[1].get("age_min"), 45)


def test_conversation_paths(base: str, r: Report) -> None:
    r.head("5. Conversation engine — Medicare (every disposition path)")
    deploy(base, "medicare")

    res = run_call(base, ["yeah pretty good thanks", "yes I have medicare",
                          "I am 67 years old"])
    r.eq("qualified lead → AI-TRANSFER", res.get("disposition"), "AI-TRANSFER")
    r.eq("transfer action reported", res.get("action"), "transfer")
    r.check("engine captured the age", res.get("debug", {}).get("ctx_age") == 67,
            f"ctx_age={res.get('debug', {}).get('ctx_age')}")

    res = run_call(base, ["hello", "no I don't have medicare"])
    r.eq("no Medicare → AI-NO-MEDICARE", res.get("disposition"), "AI-NO-MEDICARE")

    res = run_call(base, ["hello", "that's personal, I don't want to answer"])
    r.eq("refusal → AI-MEDICARE-REFUSED", res.get("disposition"), "AI-MEDICARE-REFUSED")

    res = run_call(base, ["hello", "maybe I think so"])
    r.check("'unsure' asks a clarifying question instead of guessing",
            res.get("ended") is False and bool(res.get("agent")),
            f"ended={res.get('ended')} agent={res.get('agent')!r}")

    res = run_call(base, ["hello", "I have blue cross"])
    r.check("private insurance triggers a clarification, not a guess",
            res.get("ended") is False and bool(res.get("agent")),
            f"ended={res.get('ended')} agent={res.get('agent')!r}")

    res = run_call(base, ["hello", "what do you mean?"])
    r.check("a question gets clarified and re-asked",
            res.get("ended") is False and bool(res.get("agent")),
            f"ended={res.get('ended')} agent={res.get('agent')!r}")

    res = run_call(base, ["hello", "hmm", "hmm", "hmm"])
    r.eq("unclear 3x (beyond max_clarify) → AI-UNCLEAR",
         res.get("disposition"), "AI-UNCLEAR")

    res = run_call(base, ["hello", "stop calling me"])
    r.eq("do-not-call request → AI-DNC", res.get("disposition"), "AI-DNC")

    res = run_call(base, ["hello", "I'm busy right now"])
    r.eq("busy → AI-BUSY", res.get("disposition"), "AI-BUSY")

    res = run_call(base, ["hello", "yes I have medicare", "I am 30 years old"])
    r.eq("age 30 (below 45) → AI-NOT-ELIGIBLE", res.get("disposition"), "AI-NOT-ELIGIBLE")

    res = run_call(base, ["hello", "yes I have medicare", "I am 95 years old"])
    r.eq("age 95 (above 90) → AI-NOT-ELIGIBLE", res.get("disposition"), "AI-NOT-ELIGIBLE")

    res = run_call(base, ["hello", "yes I have medicare", "I am 45 years old"])
    r.eq("boundary age 45 (min) → AI-TRANSFER", res.get("disposition"), "AI-TRANSFER")

    res = run_call(base, ["hello", "yes I have medicare", "I am 90 years old"])
    r.eq("boundary age 90 (max) → AI-TRANSFER", res.get("disposition"), "AI-TRANSFER")

    res = run_call(base, ["hello", "yes I have medicare", "I am 91 years old"])
    r.eq("just past the boundary (91) → AI-NOT-ELIGIBLE",
         res.get("disposition"), "AI-NOT-ELIGIBLE")

    r.head("6. Conversation engine — the other three campaigns")
    deploy(base, "final_expense")
    res = run_call(base, ["hello", "yes", "I am 60 years old"])
    r.eq("Final Expense: interest + age 60 (50-85) → AI-TRANSFER",
         res.get("disposition"), "AI-TRANSFER")
    res = run_call(base, ["hello", "no thanks"])
    r.eq("Final Expense: not interested → AI-NOT-INTERESTED",
         res.get("disposition"), "AI-NOT-INTERESTED")
    res = run_call(base, ["hello", "yes", "I am 40 years old"])
    r.eq("Final Expense: age 40 (below 50) → AI-NOT-ELIGIBLE",
         res.get("disposition"), "AI-NOT-ELIGIBLE")

    deploy(base, "aca")
    res = run_call(base, ["hello", "no I am not insured", "I am 30 years old"])
    r.eq("ACA: uninsured + age 30 (18-64) → AI-TRANSFER",
         res.get("disposition"), "AI-TRANSFER")
    res = run_call(base, ["hello", "yes I have insurance"])
    r.eq("ACA: already insured → AI-HAS-INSURANCE",
         res.get("disposition"), "AI-HAS-INSURANCE")
    res = run_call(base, ["hello", "no", "I am 70 years old"])
    r.eq("ACA: age 70 (above 64) → AI-NOT-ELIGIBLE",
         res.get("disposition"), "AI-NOT-ELIGIBLE")

    deploy(base, "hi")
    res = run_call(base, ["hello", "I am 60 years old", "no I have not"])
    r.eq("Hospital Indemnity: 60 + no recent stay → AI-TRANSFER",
         res.get("disposition"), "AI-TRANSFER")
    res = run_call(base, ["hello", "I am 60 years old", "yes I was in hospital"])
    r.eq("Hospital Indemnity: recent hospital stay → AI-RECENT-HOSPITAL",
         res.get("disposition"), "AI-RECENT-HOSPITAL")

    r.head("7. Misconfiguration safety (empty campaign must not crash)")
    code, made = jpost(f"{base}/api/campaigns", {"name": "Smoke Empty"})
    empty_id = made.get("id", "")
    if empty_id:
        # POST seeds one starter question; strip it so the campaign has no
        # verification steps at all (the state that used to 500 the engine).
        body = jget(f"{base}/api/campaigns/{empty_id}")[1]
        body["questions"] = []
        http("PUT", f"{base}/api/campaigns/{empty_id}", body)
        r.eq("campaign saved with zero verification steps",
             len(jget(f"{base}/api/campaigns/{empty_id}")[1].get("questions", [])), 0)
        deploy(base, empty_id)
        code, started = jpost(f"{base}/api/debug/start")
        sid = started.get("session_id", "")
        code, resp = jpost(f"{base}/api/debug/chat",
                           {"session_id": sid, "text": "hello there"})
        r.check("questionless campaign answers 200, not 500", code == 200, f"status {code}")
        r.eq("questionless campaign ends safely as AI-UNCLEAR",
             resp.get("disposition"), "AI-UNCLEAR")
        r.check("questionless campaign still speaks a closing line",
                bool(resp.get("agent")), f"agent={resp.get('agent')!r}")
        deploy(base, "medicare")
        http("DELETE", f"{base}/api/campaigns/{empty_id}")

    r.eq("session id is required (bad sid → error, no crash)",
         jpost(f"{base}/api/debug/chat", {"session_id": "nope", "text": "hi"})[1].get("error")
         is not None, True)
    r.eq("empty text is rejected",
         jpost(f"{base}/api/debug/chat",
               {"session_id": "x", "text": ""})[1].get("error") is not None, True)


def test_transcript_and_voice(base: str, r: Report) -> None:
    r.head("8. Transcript inspect & voice/TTS control APIs")
    deploy(base, "medicare")
    res = run_call(base, ["hello", "yes I have medicare", "I am 67 years old"])
    sid = res.get("session_id", "")
    code, tr = jget(f"{base}/api/debug/transcript?session_id={sid}")
    r.check("GET /api/debug/transcript → 200", code == 200, f"status {code}")
    turns = tr.get("transcript", [])
    r.check("transcript recorded both speakers",
            any(t["role"] == "ai" for t in turns) and any(t["role"] == "customer" for t in turns),
            f"{len(turns)} turns")
    r.eq("transcript reports the disposition", tr.get("disposition"), "AI-TRANSFER")
    r.eq("unknown session → 404-ish error",
         "error" in jget(f"{base}/api/debug/transcript?session_id=zzz")[1], True)

    code, voices = jget(f"{base}/api/tts/voices")
    r.check("GET /api/tts/voices → 200", code == 200, f"status {code}")
    r.check("voice list reports the active provider", "provider" in voices)

    code, switched = jpost(f"{base}/api/tts/switch-voice", {"voice": "af_heart"})
    r.check("POST /api/tts/switch-voice responds without crashing",
            code in (200, 400), f"status {code} {switched}")

    code, spoke = jpost(f"{base}/api/debug/speak", {"text": "hello"})
    r.check("POST /api/debug/speak degrades cleanly with no voice key",
            code in (200, 400, 502), f"status {code} {spoke}")
    if code != 200:
        r.check("speak error message explains how to enable voice",
                "TTS_PROVIDER" in json.dumps(spoke), f"{spoke}")

    code, empty = jpost(f"{base}/api/debug/speak", {"text": ""})
    r.eq("speak rejects empty text with 400", code, 400)


def test_insights_exports(base: str, r: Report) -> None:
    r.head("9. Self-learning insights & exports")
    code, ins = jget(f"{base}/api/insights")
    r.check("GET /api/insights → 200", code == 200, f"status {code}")
    r.check("insights include stats", isinstance(ins.get("stats"), dict))
    r.check("insights include an improvement suggestion",
            isinstance(ins.get("suggestion"), str) and len(ins["suggestion"]) > 0)
    r.check("insights track outcomes",
            isinstance(ins.get("stats", {}).get("by_outcome"), dict))
    r.check("insights record calls (self-learning is live)",
            ins.get("stats", {}).get("total_calls", 0) > 0,
            f"total_calls={ins.get('stats', {}).get('total_calls')}")

    code, raw = http("GET", f"{base}/api/export/calls?fmt=csv")
    r.check("GET /api/export/calls (csv) → 200", code == 200, f"status {code}")
    r.check("CSV carries a UTF-8 BOM (Excel-safe)", raw.startswith(b"\xef\xbb\xbf"))
    r.check("CSV has a header row", b"disposition" in raw[:200])
    code, headers, _ = http_headers("GET", f"{base}/api/export/calls?fmt=csv")
    r.check("CSV is served as a download attachment",
            "attachment" in headers.get("content-disposition", ""),
            f"content-disposition={headers.get('content-disposition')!r}")
    r.check("CSV content-type is text/csv",
            "text/csv" in headers.get("content-type", ""),
            f"content-type={headers.get('content-type')!r}")

    code, raw = http("GET", f"{base}/api/export/calls?fmt=json")
    r.check("GET /api/export/calls (json) → 200", code == 200, f"status {code}")
    try:
        parsed = json.loads(raw.decode())
        r.check("call-log JSON export parses as a list", isinstance(parsed, list))
    except Exception as e:
        r.check("call-log JSON export parses", False, str(e))

    code, raw = http("GET", f"{base}/api/export/transcripts?fmt=csv")
    r.check("GET /api/export/transcripts (csv) → 200", code == 200, f"status {code}")
    r.check("transcript CSV has the expected columns",
            b"role" in raw[:400] and b"text" in raw[:400])

    code, raw = http("GET", f"{base}/api/export/insights")
    r.check("GET /api/export/insights → 200", code == 200, f"status {code}")
    try:
        r.check("insights export parses as JSON", isinstance(json.loads(raw.decode()), dict))
    except Exception as e:
        r.check("insights export parses as JSON", False, str(e))


def test_recordings(base: str, r: Report) -> None:
    r.head("10. Call recordings (list / meta / audio / delete / traversal guard)")
    code, body = jget(f"{base}/api/recordings")
    r.check("GET /api/recordings → 200", code == 200, f"status {code}")
    recs = body.get("recordings", [])
    r.check("recordings list returned", isinstance(recs, list), f"{type(recs)}")

    if not recs:
        r.check("(no recordings yet — run a voice call first)", True,
                "skipped detail checks")
    else:
        rid = recs[0].get("id")
        code, meta = jget(f"{base}/api/recordings/{rid}")
        r.check("GET /api/recordings/{id} → 200", code == 200, f"status {code}")
        r.check("recording meta has a transcript",
                "transcript" in meta or "turns" in meta, f"keys={list(meta)[:8]}")

        code, raw = http("GET", f"{base}/recordings/{rid}")
        r.check("GET /recordings/{id} serves audio/wav",
                code == 200 and raw[:4] == b"RIFF",
                f"status {code}, magic={raw[:4]!r}")

    r.eq("unknown recording → 404", jget(f"{base}/api/recordings/does-not-exist")[0], 404)
    r.eq("path traversal on meta is contained (404)",
         jget(f"{base}/api/recordings/..%2F..%2Fetc%2Fpasswd")[0], 404)
    r.eq("path traversal on audio is contained (404)",
         http("GET", f"{base}/recordings/..%2F..%2Fetc%2Fpasswd")[0], 404)


def test_websocket(base: str, r: Report) -> None:
    r.head("11. Real-time voice WebSocket (barge-in loop, typed input)")
    try:
        import asyncio

        import websockets
    except Exception as e:
        r.check("websockets library available", False, str(e))
        return

    ws_url = base.replace("http://", "ws://").replace("https://", "wss://") + "/ws/voice"
    seen_types: list[str] = []
    result: dict = {}

    async def drive():
        async with websockets.connect(ws_url, max_size=None) as ws:
            async def collect(timeout=8):
                """Read one message; audio frames are counted, not returned."""
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=timeout)
                except asyncio.TimeoutError:
                    return None
                data = json.loads(msg) if isinstance(msg, str) else {"type": "audio"}
                seen_types.append(data.get("type", ""))
                return data

            # Greeting (plus a notice when live STT is not configured). With a
            # working TTS this streams many `audio` frames, so wait for the
            # greeting to finish rather than for a fixed number of messages.
            for _ in range(400):
                d = await collect()
                if d is None or d.get("type") == "speech_end":
                    break

            # Drive the call one turn at a time. Each utterance is paced to the
            # end of the previous turn's speech (`speech_end` always arrives,
            # even when both TTS engines fail), and the reader keeps running so
            # the final `result` is still seen: the server sends it immediately
            # AFTER the last turn's `speech_end`, so stopping at `speech_end`
            # would drop the disposition on the floor.
            utterances = ("yeah pretty good thanks",
                          "yes I have medicare",
                          "I am 67 years old")
            sent = 1
            await ws.send(json.dumps({"type": "text", "text": utterances[0]}))
            for _ in range(1500):
                d = await collect(timeout=8)
                if d is None:
                    # No speech_end (e.g. a hung TTS engine) — don't stall the
                    # call; move on to the next line, or give up at the end.
                    if sent < len(utterances):
                        await ws.send(json.dumps({"type": "text",
                                                  "text": utterances[sent]}))
                        sent += 1
                        continue
                    return
                kind = d.get("type")
                if kind == "result":
                    result.update(d)
                    return
                if kind == "speech_end" and sent < len(utterances):
                    await ws.send(json.dumps({"type": "text",
                                              "text": utterances[sent]}))
                    sent += 1

    try:
        asyncio.run(drive())
    except Exception as e:
        r.check("WebSocket session completed", False, str(e))
        return

    r.check("WebSocket accepted and spoke a greeting",
            "agent_say" in seen_types, f"messages={sorted(set(seen_types))}")
    r.check("WebSocket drove the call to a disposition",
            bool(result.get("disposition")),
            f"result={result} types={sorted(set(seen_types))}")
    if result.get("disposition"):
        r.eq("voice call qualified → AI-TRANSFER", result.get("disposition"), "AI-TRANSFER")
        r.eq("voice call action is transfer", result.get("action"), "transfer")
    r.check("no unhandled session error mid-call",
            not any(t == "error" for t in seen_types),
            f"types={sorted(set(seen_types))}")


def test_state_restore(base: str, r: Report) -> None:
    r.head("12. Final state restore")
    code, resp = jpost(f"{base}/api/campaigns/active", {"id": "medicare"})
    r.check("medicare redeployed as the live campaign", code == 200, f"{code} {resp}")
    r.eq("live campaign is medicare",
         jget(f"{base}/api/campaigns")[1].get("active"), "medicare")


# --------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description="Full-feature smoke test")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--no-ws", action="store_true", help="skip the WebSocket test")
    args = ap.parse_args()
    base = args.url.rstrip("/")

    code, health = jget(f"{base}/health", timeout=15)
    if code != 200:
        print(f"\033[31mServer not reachable at {base} — start it with "
              f"`python cli.py debug` first.\033[0m")
        return 2

    print(f"Target: {base}   (mode={health.get('mode')}, "
          f"tts={health.get('tts')}, llm={health.get('llm')}, debug={health.get('debug')})")
    r = Report()
    test_pages(base, r)
    test_health(base, r)
    test_call_log(base, r)
    test_campaigns(base, r)
    test_conversation_paths(base, r)
    test_transcript_and_voice(base, r)
    test_insights_exports(base, r)
    test_recordings(base, r)
    if not args.no_ws:
        test_websocket(base, r)
    test_state_restore(base, r)
    return r.summary()


if __name__ == "__main__":
    sys.exit(main())
