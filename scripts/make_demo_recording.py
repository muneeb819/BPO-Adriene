"""Generate a full demo call recording using free Microsoft Edge neural voices.

Agent (Lina) speaks with a warm professional female voice; the prospect is a
male caller. Lines are stitched together with natural gaps into one WAV.

Uses edge-tts (free, no API key) + pydub/ffmpeg for merging.
"""
from __future__ import annotations

import asyncio
import os
import subprocess

import imageio_ffmpeg

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


AGENT_VOICE = "en-US-AriaNeural"     # warm, professional female
CALLER_VOICE = "en-US-GuyNeural"     # warm, natural male
AGENT_RATE = "+2%"
CALLER_RATE = "+0%"
GAP_MS = 700                          # pause between turns


async def synth(voice: str, text: str, out: str, rate: str) -> None:
    import edge_tts

    tts = edge_tts.Communicate(text, voice, rate=rate)
    await tts.save(out)


def main() -> None:
    base = os.path.join("data", "demo_recording")
    os.makedirs(base, exist_ok=True)

    # Dialog: ("agent"|"caller", line)
    dialog = [
        ("agent", "Hello, this is Lina calling from Senior Resource Center. How are you doing today?"),
        ("caller", "Hi, pretty good, thanks. Who is this?"),
        ("agent", "We offer fixed income coverage for final expenses. How young are you?"),
        ("caller", "I'm sixty seven years old."),
        ("agent", "That's great! You're eligible for a lower monthly premium. One moment, let me connect you with a licensed agent."),
    ]

    asyncio.run(_build(dialog, base))


async def _build(dialog, base):
    import edge_tts

    files = []
    for i, (who, line) in enumerate(dialog):
        voice = AGENT_VOICE if who == "agent" else CALLER_VOICE
        rate = AGENT_RATE if who == "agent" else CALLER_RATE
        tag = "lina" if who == "agent" else "caller"
        path = os.path.join(base, f"turn_{i+1}_{tag}.mp3")
        tts = edge_tts.Communicate(line, voice, rate=rate)
        await tts.save(path)
        files.append(path)
        print(f"  [{who}]  {line}")

    # Build an ffmpeg concat filter: each file + silence gap.
    # Add a short silence mp3 as a virtual gap by extending each input with
    # pad/adelay via a filter_complex, then concatenate into one wav.
    inputs = []
    for p in files:
        inputs += ["-i", p]
    n = len(files)
    gap_s = GAP_MS / 1000.0
    filt = []
    for idx in range(n):
        filt.append(f"[{idx}:a]apad=pad_dur={gap_s}[a{idx}]")
    concat_in = "".join(f"[a{k}]" for k in range(n))
    filt.append(f"{concat_in}concat=n={n}:v=0:a=1[out]")
    cmd = [FFMPEG, "-y"]
    cmd += inputs
    cmd += ["-filter_complex", ";".join(filt), "-map", "[out]",
            "-ar", "44100", "-ac", "1", os.path.join(base, "final_demo.wav")]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print("FFMPEG ERROR:\n", proc.stderr[-2000:])
        return
    outfile = os.path.join(base, "final_demo.wav")
    print(f"\nSaved: {outfile} ({os.path.getsize(outfile)} bytes)")


if __name__ == "__main__":
    main()
