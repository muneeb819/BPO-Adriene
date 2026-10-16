"""Twilio transfer adapter implementation (prototype).

This adapter performs a simple outbound call to an agent phone number using Twilio REST API.
It requires the following provider credential to be stored in ProviderCredential with provider='twilio':
- account_sid: as the stored API key value (format: SID)
- auth_token: stored as a separate ProviderCredential with provider='twilio_auth' OR combined in a single string separated by ':'

For local-first testing we accept a simple API key stored as 'twilio' where the value is "ACCOUNT_SID:AUTH_TOKEN:FROM_NUMBER:AGENT_NUMBER"
(You can save more structured storage later). The adapter will parse these values and attempt to create an outbound call to the agent.

IMPORTANT: This is a prototype to let you test transfers locally. A production warm SIP transfer requires passing the live call SID and bridging legs into a conference or issuing a Twilio <Redirect> to the live call; that requires deeper media integration.
"""
from typing import Optional
from twilio.rest import Client
from app.db import fetch_credential
from app.core.crypto import decrypt_text

class TwilioAdapter:
    def __init__(self):
        # Expect a single ProviderCredential with provider='twilio' containing a colon-separated value:
        # ACCOUNT_SID:AUTH_TOKEN:FROM_NUMBER:AGENT_NUMBER
        cred = fetch_credential('twilio')
        if cred:
            try:
                plain = decrypt_text(cred.api_key_encrypted)
                parts = plain.split(':')
                self.account_sid = parts[0]
                self.auth_token = parts[1]
                self.from_number = parts[2] if len(parts) > 2 else None
                self.agent_number = parts[3] if len(parts) > 3 else None
                self.client = Client(self.account_sid, self.auth_token)
                self.enabled = True
            except Exception:
                self.enabled = False
                self.client = None
        else:
            self.enabled = False
            self.client = None

    def transfer_call(self, session_id: str, target: Optional[str] = None) -> dict:
        """Attempt to create an outbound call to the configured agent number and return a dict with status and details.
        This is a best-effort prototype for local testing.
        """
        if not getattr(self, 'enabled', False):
            raise RuntimeError('Twilio adapter not configured')
        # Use agent_number or provided target
        to_number = target or self.agent_number
        if not to_number:
            raise RuntimeError('No agent number configured')
        # Create a simple TwiML that speaks a short message and hangs up — in production you'd bridge or conference.
        twiml = f"<Response><Say voice=\"alloy\">You are being connected to a Medicare benefits caller. Please answer to accept transfer.</Say></Response>"
        call = self.client.calls.create(
            to=to_number,
            from_=self.from_number,
            twiml=twiml
        )
        return { 'status': 'initiated', 'call_sid': call.sid }
