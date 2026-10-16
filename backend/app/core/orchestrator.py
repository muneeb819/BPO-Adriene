"""Orchestrator for adapter selection and runtime config (local-first).

This module exposes helper functions to obtain adapters (STT, TTS, LLM, Media, Transfer)
based on an in-memory runtime config. The config defaults to environment variables but can
be updated via the REST endpoint (for local testing) so you can quickly swap providers
without restarting the server.
"""
import os
from typing import Dict
from .adapters.stt_adapter import get_stt_adapter
from .adapters.tts_adapter import get_tts_adapter
from .adapters.llm_adapter import get_llm_adapter
from .adapters.media_bridge import get_media_bridge
from .adapters.transfer_adapter import get_transfer_adapter

# Initialize runtime config from environment
_runtime_config: Dict[str, str] = {
    'STT_PROVIDER': os.getenv('STT_PROVIDER', 'mock'),
    'TTS_PROVIDER': os.getenv('TTS_PROVIDER', 'mock'),
    'LLM_PROVIDER': os.getenv('LLM_PROVIDER', 'mock'),
    'MEDIA_BRIDGE': os.getenv('MEDIA_BRIDGE', 'mock'),
    'TRANSFER_PROVIDER': os.getenv('TRANSFER_PROVIDER', 'mock'),
}


def get_runtime_config() -> Dict[str, str]:
    return dict(_runtime_config)


def set_runtime_config(new_cfg: Dict[str, str]):
    for k, v in new_cfg.items():
        if k in _runtime_config and v:
            _runtime_config[k] = v
            # Also set as env for provider factories that read env
            os.environ[k] = v


def get_stt():
    # Ensure factory reads current env (some factories look at env)
    os.environ['STT_PROVIDER'] = _runtime_config['STT_PROVIDER']
    return get_stt_adapter()


def get_tts():
    os.environ['TTS_PROVIDER'] = _runtime_config['TTS_PROVIDER']
    return get_tts_adapter()


def get_llm():
    os.environ['LLM_PROVIDER'] = _runtime_config['LLM_PROVIDER']
    return get_llm_adapter()


def get_media():
    os.environ['MEDIA_BRIDGE'] = _runtime_config['MEDIA_BRIDGE']
    return get_media_bridge()


def get_transfer():
    os.environ['TRANSFER_PROVIDER'] = _runtime_config.get('TRANSFER_PROVIDER', 'mock')
    return get_transfer_adapter()
