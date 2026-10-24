"""LiveKit adapters for Adriene.

These plug the project's own engines into a LiveKit ``AgentSession`` so the bot
can run as a real LiveKit agent (WebRTC, SIP, rooms) instead of only the local
browser console:

    brain_llm.py    the dialogue brain (``CallSession``) as an ``llm.LLM``
    neural_tts.py   the local Piper/ONNX voice as a ``tts.TTS``
    whisper_stt.py  optional local ``faster-whisper`` speech recognition
    factory.py      env-driven provider selection + a status report

Everything is local-first: with no cloud keys the brain, the neural voice and
the VAD all run on the machine. Adding ``DEEPGRAM_API_KEY``, ``OPENAI_API_KEY``
or ``ELEVENLABS_API_KEY`` upgrades the matching leg without touching code.
"""
from .brain_llm import BrainLLM, BrainLLMStream
from .neural_tts import LocalNeuralTTS, LocalNeuralTTSStream
from .factory import build_llm, build_stt, build_tts, build_vad, describe_stack

__all__ = [
    "BrainLLM", "BrainLLMStream",
    "LocalNeuralTTS", "LocalNeuralTTSStream",
    "build_llm", "build_stt", "build_tts", "build_vad", "describe_stack",
]
