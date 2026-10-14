"""Wires the ARI StasisStart event to a full fronter call: runs the beat-based
conversation, handles transfer/hangup, and logs the result to VICIdial + call log.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from .ari import ARI
from .call_session import AriChannel, run_session
from .config import Config
from .stt import STT
from .tts import TTS
from .vicidial import ViciDial


def build_stasis_handler(config: Config, call_log: list):
    tts = TTS(config)
    stt = STT(config)
    ari = ARI(config)
    vicidial = ViciDial(config)

    def handler(channel_id: str, args: dict, variables: dict):
        lead_id = (variables or {}).get("lead_id") or (args or {}).get("lead_id")
        channel = AriChannel(config, ari, tts, stt, channel_id)
        ari.answer(channel_id)
        bridge_id = ari.bridge_create()
        ari.bridge_add(bridge_id, channel_id)

        result = run_session(channel, config)

        record = {
            "id": str(uuid.uuid4()),
            "ts": datetime.now(timezone.utc).isoformat(),
            "lead_id": lead_id,
            "action": result.get("action"),
            "disposition": result.get("disposition"),
            "facts": result.get("facts"),
        }
        call_log.append(record)
        if vicidial.enabled() and lead_id:
            vicidial.log_qualification(lead_id, result.get("disposition", ""), result.get("facts", {}))

    return handler
