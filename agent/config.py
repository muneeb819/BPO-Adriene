"""Runtime configuration for the Final Expense Voice Fronter."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class Config:
    llm_provider: str = "anthropic"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_base_url: str = ""
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-3-5-sonnet-latest"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"

    agent_name: str = "Lina"
    business_name: str = "Final Expense Solutions"
    brand_voice: str = "natural, warm, conversational, expressive, empathetic"
    licensed_states: list[str] = field(default_factory=lambda: [
        "AZ", "CA", "TX", "FL", "OH", "GA", "NC", "TN", "IL", "PA", "MI", "VA", "IN", "MO"
    ])
    min_age: int = 50
    max_age: int = 85

    stt_provider: str = "console"
    deepgram_api_key: str = ""
    deepgram_model: str = "nova-2"

    tts_provider: str = "elevenlabs"
    # Standby engine: used automatically when the primary TTS fails mid-call,
    # and can be promoted to primary by changing TTS_PROVIDER. Empty = off.
    tts_standby_provider: str = ""
    tts_voice: str = "aura-luna-en"
    openai_tts_api_key: str = ""
    openai_tts_model: str = "tts-1"
    openai_tts_voice: str = "shimmer"
    azure_speech_key: str = ""
    azure_speech_region: str = ""
    azure_voice: str = "en-US-JennyNeural"
    elevenlabs_api_key: str = ""
    elevenlabs_voice: str = "Jessica"
    elevenlabs_model: str = "eleven_v3"
    elevenlabs_stability: float = 0.5
    elevenlabs_similarity_boost: float = 0.75
    elevenlabs_style: float = 0.3
    elevenlabs_speaker_boost: bool = True
    # Kokoro: self-hosted, free, OpenAI-compatible (POST /v1/audio/speech).
    kokoro_base_url: str = "http://localhost:8880"
    kokoro_api_key: str = "not-needed"
    kokoro_model: str = "kokoro"
    kokoro_voice: str = "af_bella"
    kokoro_speed: float = 1.0
    kokoro_pitch: int = 0                     # 0 = the server's own default
    kokoro_timeout: float = 30.0
    # Local neural voices (Piper/ONNX). Same wire format as Kokoro, served by
    # scripts/neural_tts_server.py; voices live in data/models/piper.
    piper_base_url: str = "http://127.0.0.1:8880"
    piper_api_key: str = "not-needed"
    piper_model: str = "piper"
    piper_voice: str = ""                     # "" = the server picks the middle voice
    piper_speed: float = 1.0
    piper_pitch: int = 50                     # 0-99 studio scale, 50 = no shift
    piper_volume: float = 1.0
    piper_sample_rate: int = 16000
    piper_timeout: float = 30.0

    # ChatterBox: Resemble AI open-source TTS (voice cloning, multilingual, paralinguistic tags)
    # Models: turbo (350M, English, paralinguistic tags), nano (110M, English, CPU 3x realtime),
    # multilingual (500M, 23+ languages)
    chatterbox_model: str = "turbo"           # turbo | nano | multilingual
    chatterbox_device: str = "cuda"           # cuda | cpu | mps
    chatterbox_voice_path: str = ""           # path to reference audio for voice cloning (10s+)
    chatterbox_language: str = "en"           # language code for multilingual (en, es, fr, de, etc.)
    chatterbox_exaggeration: float = 0.5      # expressiveness (0.0-1.0)
    chatterbox_cfg_weight: float = 0.5        # classifier-free guidance (0.0-1.0)
    chatterbox_temperature: float = 0.8       # sampling temperature

    positive_threshold: float = 0.85
    negative_threshold: float = 0.85
    dnc_threshold: float = 0.75
    max_clarify_attempts: int = 1

    calling_hour_start: int = 8
    calling_hour_end: int = 21

    ari_url: str = "http://localhost:8088"
    ari_app: str = "final-expense-fronter"
    ari_username: str = "ari"
    ari_password: str = ""
    ari_outbound_context: str = "from-internal"
    closer_queue: str = "closers"
    closer_extension: str = ""
    asterisk_sounds_dir: str = "/var/lib/asterisk/sounds/en"
    ari_recordings_dir: str = "/var/spool/asterisk/monitor"

    vicidial_url: str = ""
    vicidial_user: str = ""
    vicidial_pass: str = ""
    vicidial_source: str = "AIFRONTER"

    host: str = "0.0.0.0"
    port: int = 8000
    web_password: str = ""
    data_dir: str = "data"
    voice_library_dir: str = "data/voices"    # saved voices (library/design/remix)

    @classmethod
    def load(cls) -> "Config":
        try:
            from dotenv import load_dotenv

            load_dotenv()
        except Exception:
            pass

        def env(name: str, default):
            val = os.environ.get(name)
            if val is None or val == "":
                return default
            return val

        def env_list(name: str, default):
            val = os.environ.get(name)
            if not val:
                return default
            return [s.strip().upper() for s in val.split(",") if s.strip()]

        return cls(
            llm_provider=env("LLM_PROVIDER", "anthropic"),
            openai_api_key=env("OPENAI_API_KEY", ""),
            openai_model=env("OPENAI_MODEL", "gpt-4o-mini"),
            openai_base_url=env("OPENAI_BASE_URL", ""),
            anthropic_api_key=env("ANTHROPIC_API_KEY", ""),
            anthropic_model=env("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest"),
            ollama_base_url=env("OLLAMA_BASE_URL", "http://localhost:11434"),
            ollama_model=env("OLLAMA_MODEL", "llama3.1"),
            gemini_api_key=env("GEMINI_API_KEY", ""),
            gemini_model=env("GEMINI_MODEL", "gemini-2.0-flash"),
            agent_name=env("AGENT_NAME", "Lina"),
            business_name=env("BUSINESS_NAME", "Final Expense Solutions"),
            brand_voice=env("BRAND_VOICE", "natural, warm, conversational, expressive, empathetic"),
            licensed_states=env_list("LICENSED_STATES", cls().licensed_states),
            min_age=int(env("MIN_AGE", "50")),
            max_age=int(env("MAX_AGE", "85")),
            stt_provider=env("STT_PROVIDER", "console"),
            deepgram_api_key=env("DEEPGRAM_API_KEY", ""),
            deepgram_model=env("DEEPGRAM_MODEL", "nova-2"),
            tts_provider=env("TTS_PROVIDER", "elevenlabs"),
            tts_standby_provider=env("TTS_STANDBY_PROVIDER", ""),
            tts_voice=env("TTS_VOICE", "aura-asteria-en"),
            openai_tts_api_key=env("OPENAI_TTS_API_KEY", ""),
            openai_tts_model=env("OPENAI_TTS_MODEL", "tts-1"),
            openai_tts_voice=env("OPENAI_TTS_VOICE", "alloy"),
            azure_speech_key=env("AZURE_SPEECH_KEY", ""),
            azure_speech_region=env("AZURE_SPEECH_REGION", ""),
            azure_voice=env("AZURE_VOICE", "en-US-AriaNeural"),
            elevenlabs_api_key=env("ELEVENLABS_API_KEY", ""),
            elevenlabs_voice=env("ELEVENLABS_VOICE", "Rachel"),
            elevenlabs_model=env("ELEVENLABS_MODEL", "eleven_v3"),
            elevenlabs_stability=float(env("ELEVENLABS_STABILITY", "0.65")),
            elevenlabs_similarity_boost=float(env("ELEVENLABS_SIMILARITY_BOOST", "0.75")),
            elevenlabs_style=float(env("ELEVENLABS_STYLE", "0.3")),
            elevenlabs_speaker_boost=env("ELEVENLABS_SPEAKER_BOOST", "true").lower() == "true",
            kokoro_base_url=env("KOKORO_BASE_URL", "http://localhost:8880"),
            kokoro_api_key=env("KOKORO_API_KEY", "not-needed"),
            kokoro_model=env("KOKORO_MODEL", "kokoro"),
            kokoro_voice=env("KOKORO_VOICE", "af_bella"),
            kokoro_speed=float(env("KOKORO_SPEED", "1.0")),
            kokoro_pitch=int(env("KOKORO_PITCH", "0")),
            kokoro_timeout=float(env("KOKORO_TIMEOUT", "30")),
            piper_base_url=env("PIPER_BASE_URL", "http://127.0.0.1:8880"),
            piper_api_key=env("PIPER_API_KEY", "not-needed"),
            piper_model=env("PIPER_MODEL", "piper"),
            piper_voice=env("PIPER_VOICE", ""),
            piper_speed=float(env("PIPER_SPEED", "1.0")),
            piper_pitch=int(env("PIPER_PITCH", "50")),
            piper_volume=float(env("PIPER_VOLUME", "1.0")),
            piper_sample_rate=int(env("PIPER_SAMPLE_RATE", "16000")),
            piper_timeout=float(env("PIPER_TIMEOUT", "30")),
            chatterbox_model=env("CHATTERBOX_MODEL", "turbo"),
            chatterbox_device=env("CHATTERBOX_DEVICE", "cuda"),
            chatterbox_voice_path=env("CHATTERBOX_VOICE_PATH", ""),
            chatterbox_language=env("CHATTERBOX_LANGUAGE", "en"),
            chatterbox_exaggeration=float(env("CHATTERBOX_EXAGGERATION", "0.5")),
            chatterbox_cfg_weight=float(env("CHATTERBOX_CFG_WEIGHT", "0.5")),
            chatterbox_temperature=float(env("CHATTERBOX_TEMPERATURE", "0.8")),
            positive_threshold=float(env("POSITIVE_CONFIDENCE_THRESHOLD", "0.85")),
            negative_threshold=float(env("NEGATIVE_CONFIDENCE_THRESHOLD", "0.85")),
            dnc_threshold=float(env("DNC_CONFIDENCE_THRESHOLD", "0.75")),
            max_clarify_attempts=int(env("MAX_CLARIFY_ATTEMPTS", "1")),
            calling_hour_start=int(env("CALLING_HOUR_START", "8")),
            calling_hour_end=int(env("CALLING_HOUR_END", "21")),
            ari_url=env("ARI_URL", "http://localhost:8088"),
            ari_app=env("ARI_APP", "final-expense-fronter"),
            ari_username=env("ARI_USERNAME", "ari"),
            ari_password=env("ARI_PASSWORD", ""),
            ari_outbound_context=env("ARI_OUTBOUND_CONTEXT", "from-internal"),
            closer_queue=env("CLOSER_QUEUE", "closers"),
            closer_extension=env("CLOSER_EXTENSION", ""),
            asterisk_sounds_dir=env("ASTERISK_SOUNDS_DIR", "/var/lib/asterisk/sounds/en"),
            ari_recordings_dir=env("ARI_RECORDINGS_DIR", "/var/spool/asterisk/monitor"),
            vicidial_url=env("VICIDIAL_URL", ""),
            vicidial_user=env("VICIDIAL_USER", ""),
            vicidial_pass=env("VICIDIAL_PASS", ""),
            vicidial_source=env("VICIDIAL_SOURCE", "AIFRONTER"),
            host=env("HOST", "0.0.0.0"),
            port=int(env("PORT", "8000")),
            web_password=env("WEB_PASSWORD", ""),
            data_dir=env("DATA_DIR", "data"),
            voice_library_dir=env("VOICE_LIBRARY_DIR", "data/voices"),
        )
