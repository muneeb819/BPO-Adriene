"""Twilio outbound transfer adapter — real REST, zero SDK dependency.

The ``twilio`` pip package is deliberately NOT required: the handoff is a
single HTTPS call, so this works in any deployment the moment credentials
exist (same rule as the LLM upgrade path).

Credentials resolve in this order:

1. Environment (deploy-friendly — see .env.example):
       TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN,
       TWILIO_FROM_NUMBER,  TWILIO_AGENT_NUMBER
2. The encrypted ``ProviderCredential`` DB row (provider='twilio', value
   "ACCOUNT_SID:AUTH_TOKEN:FROM_NUMBER:AGENT_NUMBER", encrypted with SECRET_KEY).

With neither present the adapter stays disabled: ``transfer_call`` returns
False and the conversation continues — a live call never dies on transfer
wiring. ``__init__`` never raises, so startup can't be broken by bad config.

Prototype-grade handoff: it DIALS the agent with a short <Say> message. A
production warm SIP transfer must bridge the live call SID into a conference
or <Redirect> — that needs deeper media integration (see media_bridge).
"""
import base64
import json as _json
import os
import urllib.parse
import urllib.request
from typing import Optional


class TwilioAdapter:
    def __init__(self):
        self.enabled = False
        self.account_sid: Optional[str] = None
        self.auth_token: Optional[str] = None
        self.from_number: Optional[str] = None
        self.agent_number: Optional[str] = None
        self.last_result: dict = {}
        try:
            sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
            token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
            if sid and token:
                self.account_sid, self.auth_token = sid, token
                self.from_number = os.getenv("TWILIO_FROM_NUMBER", "").strip() or None
                self.agent_number = os.getenv("TWILIO_AGENT_NUMBER", "").strip() or None
                self.enabled = True
                return
            # fall back to the encrypted DB credential (relative import so this
            # resolves whether the app was started as `app.*` or `backend.app.*`)
            from ...db import fetch_credential
            from ..crypto import decrypt_text
            cred = fetch_credential("twilio")
            if cred:
                parts = decrypt_text(cred.api_key_encrypted).split(":")
                self.account_sid, self.auth_token = parts[0], parts[1]
                self.from_number = parts[2] if len(parts) > 2 else None
                self.agent_number = parts[3] if len(parts) > 3 else None
                self.enabled = True
        except Exception as e:            # noqa: BLE001
            print(f"[twilio] not configured ({e}); transfers degrade to mock")
            self.enabled = False

    def transfer_call(self, session_id: str, target: Optional[str] = None) -> bool:
        """Dial the agent with a short handoff message.

        True only when Twilio accepts the call. Any failure — missing creds,
        bad key, network down — returns False so the live call continues.
        """
        if not self.enabled:
            print("[twilio] adapter not configured; transfer skipped")
            return False
        to_number = target or self.agent_number
        if not to_number or not self.from_number:
            print("[twilio] missing TWILIO_AGENT_NUMBER / TWILIO_FROM_NUMBER; transfer skipped")
            return False
        try:
            url = (f"https://api.twilio.com/2010-04-01/Accounts/"
                   f"{self.account_sid}/Calls.json")
            twiml = ('<Response><Say voice="alloy">You are being connected to a '
                     'Medicare benefits caller. Please answer to accept transfer.'
                     '</Say></Response>')
            payload = urllib.parse.urlencode(
                {"To": to_number, "From": self.from_number, "Twiml": twiml}).encode()
            auth = base64.b64encode(
                f"{self.account_sid}:{self.auth_token}".encode()).decode()
            req = urllib.request.Request(url, data=payload, headers={
                "Authorization": f"Basic {auth}",
                "Content-Type": "application/x-www-form-urlencoded",
            })
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = _json.loads(resp.read())
            self.last_result = {"status": data.get("status"), "call_sid": data.get("sid")}
            print(f"[twilio] outbound handoff initiated session={session_id} "
                  f"sid={data.get('sid')}")
            return True
        except Exception as e:            # noqa: BLE001
            self.last_result = {"status": "failed", "error": str(e)}
            print(f"[twilio] transfer failed ({e}); call continues")
            return False
