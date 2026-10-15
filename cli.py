"""CLI: run an interactive console session, a scripted smoke-test, or serve ARI + web."""
from __future__ import annotations

import json
import re
import sys

from agent.config import Config


def cmd_console():
    from agent.call_session import ConsoleChannel, run_session

    config = Config.load()
    channel = ConsoleChannel()
    run_session(channel, config)


def cmd_demo():
    from agent.call_session import ConsoleChannel, run_session

    config = Config.load()
    print("=== BEAT-BASED SMOKE TEST (scripted, no keys required for console) ===")
    channel = _ScriptedChannel([
        "Yeah, pretty good, thanks",
        "Yes",  # Medicare: YES
        "I'm 67 years old",
    ])
    result = run_session(channel, config, max_turns=8)
    print("\n=== RESULT ===")
    print(result)


def _restore_saved_voice(config) -> None:
    """Re-apply the voice the operator last chose in the studio.

    The library remembers which voice is active; without this the bot would come
    back speaking whatever `.env` says while the dashboard showed a different
    voice as "ACTIVE". Failures are reported, never fatal.
    """
    try:
        from agent.voice_library import get_library

        library = get_library(config.voice_library_dir)
        vid = library.active_id()
        if not vid or library.find(vid) is None:
            return
        applied = library.apply(vid, config)
        print(f"[voice] restored the saved voice '{applied['name']}' ({applied['engine']})")
    except Exception as e:                                     # noqa: BLE001
        print(f"[voice] could not restore the saved voice: {e}")


def cmd_serve():
    import uvicorn

    from server.app import CONFIG, CALL_LOG
    from agent.service import build_stasis_handler
    from agent.ari import ARI

    _restore_saved_voice(CONFIG)

    print(f"Starting web on {CONFIG.host}:{CONFIG.port} [tts={CONFIG.tts_provider}]")
    print(f"  Dashboard:     http://{CONFIG.host}:{CONFIG.port}/")
    print(f"  Debug console: http://{CONFIG.host}:{CONFIG.port}/debug")
    print(f"  Health:        http://{CONFIG.host}:{CONFIG.port}/health")
    import threading

    def ari_thread():
        handler = build_stasis_handler(CONFIG, CALL_LOG)
        try:
            ARI(CONFIG).run_forever(CONFIG.ari_app, handler)
        except Exception as e:
            print(f"[ARI] not connected (expected in debug mode without Asterisk): {e}")

    threading.Thread(target=ari_thread, daemon=True).start()
    uvicorn.run("server.app:app", host=CONFIG.host, port=CONFIG.port)


def cmd_debug():
    """Debug mode: run smoke test, then serve dashboard + debug console.

    Respects .env providers (so ElevenLabs/Deepgram voice keys work in debug);
    falls back to console providers + MockLLM when no keys are set.
    """
    import os

    os.environ["DEBUG"] = "true"
    print("=== DEBUG MODE (no PBX required) ===")
    cmd_demo()
    print("\n=== Starting debug web server ===")
    cmd_serve()


def cmd_stream():
    """Test ElevenLabs TTS streaming with Lina's voice (saves playable .mp3 files)."""
    import os

    from agent.config import Config as C
    from agent.tts import TTS

    config = C.load()
    if config.elevenlabs_api_key:
        config.tts_provider = "elevenlabs"
    print(f"=== STREAMING TTS TEST [{config.tts_provider}] voice={config.elevenlabs_voice} ===")
    if config.tts_provider != "elevenlabs" or not config.elevenlabs_api_key:
        print("NOTE: no ElevenLabs key — set TTS_PROVIDER=elevenlabs and ELEVENLABS_API_KEY in .env")
        print("      to hear Lina's real voice. See docs/LOCAL_VOICE_SETUP.md")
    outdir = os.path.join("data", "stream_test")
    os.makedirs(outdir, exist_ok=True)
    tts = TTS(config)
    lines = [
        "Hello, this is Lina calling from Senior Resource Center. How are you doing today?",
        "I understand. We offer fixed-income coverage for final expenses. How young are you?",
        "That's great — you're eligible for a lower monthly premium. One moment, let me get you connected.",
    ]
    for i, line in enumerate(lines, 1):
        print(f"AGENT: {line}")
        try:
            audio = tts.synthesize(line)
        except Exception as e:
            print(f"  [TTS ERROR: {e}]")
            print("  Check ELEVENLABS_API_KEY and your internet connection, then retry.")
            break
        print(f"  [{len(audio)} bytes audio generated]")
        if audio:
            path = os.path.join(outdir, f"line{i}.mp3")
            with open(path, "wb") as f:
                f.write(audio)
            print(f"  saved: {path}  (play it to hear Lina)")
    print("=== DONE ===")


class _ScriptedChannel:
    def __init__(self, lines):
        self.lines = list(lines)
        self.i = 0

    def say(self, text: str):
        print(f"AGENT: {text}")

    def listen(self) -> str:
        if self.i >= len(self.lines):
            return ""
        line = self.lines[self.i]
        self.i += 1
        print(f"CALLER: {line}")
        return line

    def transfer(self):
        print("[TRANSFER] -> closer queue")

    def hangup(self):
        print("[HANGUP]")


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else "console"
    if cmd == "console":
        cmd_console()
    elif cmd == "demo":
        cmd_demo()
    elif cmd == "serve":
        cmd_serve()
    elif cmd == "debug":
        cmd_debug()
    elif cmd == "stream":
        cmd_stream()
    else:
        print("Usage: python cli.py [console|demo|serve|debug|stream]")
        sys.exit(1)


if __name__ == "__main__":
    main()
