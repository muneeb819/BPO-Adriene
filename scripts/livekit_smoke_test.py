#!/usr/bin/env python3
"""Offline end-to-end check of the LiveKit agent's audio + dialogue path.

It runs the *real* pieces the worker uses — ``BrainLLM`` (the scripted brain as a
LiveKit ``llm.LLM``) and ``LocalNeuralTTS`` (the Piper voice as a LiveKit
``tts.TTS``) — without a LiveKit server, a microphone or a sound card. That makes
it the fastest way to answer "is the bot actually working?" on a headless box or
in CI, where ``livekit_agent.py console`` would need PortAudio.

    python scripts/livekit_smoke_test.py
    python scripts/livekit_smoke_test.py --out /tmp/call.wav

Outputs (default):
    backend/qa_outputs/livekit_demo.wav              the spoken conversation
    backend/qa_outputs/livekit_demo_transcript.json  turns + disposition

Exit code 0 means: the brain answered every turn, the voice rendered audio, and
the qualification/transfer path fired as designed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import wave
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from livekit.agents import llm  # noqa: E402

from backend.app.core.adapters.livekit_plugins import (  # noqa: E402
    BrainLLM,
    LocalNeuralTTS,
    describe_stack,
)

DEFAULT_OUT = REPO_ROOT / "backend" / "qa_outputs" / "livekit_demo.wav"

SCRIPT = [
    None,                                   # the greeting
    "Yes, I have Medicare.",
    "I'm sixty-seven.",
]


async def _turn(brain: BrainLLM, text: str | None) -> str:
    ctx = llm.ChatContext.empty()
    if text:
        ctx.add_message(role="user", content=text)
    parts = []
    async for chunk in brain.chat(chat_ctx=ctx):
        if chunk.delta and chunk.delta.content:
            parts.append(chunk.delta.content)
    return "".join(parts).strip()


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="wav output path")
    parser.add_argument("--no-audio", action="store_true", help="skip TTS rendering")
    args = parser.parse_args(argv)

    print("stack:", json.dumps(describe_stack(), indent=2))

    brain = BrainLLM(session_id="smoke-test")
    tts = None if args.no_audio else LocalNeuralTTS(voice="")
    if tts is not None:
        print(f"voice: {tts.model} @ {tts.sample_rate} Hz\n")
        tts.prewarm()

    pcm = bytearray()
    turns: list[dict] = []

    for text in SCRIPT:
        reply = await _turn(brain, text)
        if text:
            print(f"CALLER : {text}")
        print(f"ADRIENE: {reply}\n")
        turns.append({"role": "user" if text else "greeting", "text": text or ""})
        turns.append({"role": "assistant", "text": reply})

        if tts is None or not reply:
            continue
        stream = tts.synthesize(reply)
        async for audio in stream:
            if audio.frame is not None:
                pcm.extend(bytes(audio.frame.data))

    if tts is not None and pcm:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(out), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(tts.sample_rate)
            w.writeframes(bytes(pcm))
        seconds = len(pcm) / 2 / tts.sample_rate
        print(f"wrote {out}  ({seconds:.1f}s of speech)")

        transcript_path = out.with_name(out.stem + "_transcript.json")
        transcript_path.write_text(
            json.dumps({"turns": turns, **brain.result()}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"wrote {transcript_path}")

    result = brain.result()
    print("\ndisposition :", result["disposition"])
    print("data        :", result["data"])

    ok = (
        len([t for t in turns if t["role"] == "assistant"]) == len(SCRIPT)
        and (args.no_audio or len(pcm) > 0)
        and result["disposition"] in ("AI-TRANSFER", "QUALIFIED")
    )
    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
