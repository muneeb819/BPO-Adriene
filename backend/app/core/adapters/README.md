# Adapters README

This folder contains adapter interfaces and simple mock implementations for STT, TTS, LLM, and the media bridge.

Goal: keep everything local-first. Each adapter exposes a uniform API so production providers (Deepgram, ElevenLabs, OpenAI, Twilio, etc.) can be added later by implementing the same interface and selecting via environment variables.

How to use in local debug:
- By default adapters use the "mock" provider which works with typed input from the React frontend.
- To enable a real provider, set the relevant environment variables in backend/.env and implement provider-specific code.

Files:
- stt_adapter.py
- tts_adapter.py
- llm_adapter.py
- media_bridge.py
