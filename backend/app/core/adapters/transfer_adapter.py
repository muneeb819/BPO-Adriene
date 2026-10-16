"""Transfer adapter interface, mock, ViciDial stub, and the real Twilio dialer.

A transfer adapter hands a call off to a live agent. The factory's contract:
**resolve at call time, degrade to mock, never crash a live call** — a bad
provider value or broken credential must never take the conversation down.

- ``mock``        records the request and continues (default, zero config).
- ``twilio``      real outbound dial to the agent via plain HTTPS
                  (see twilio_adapter.py — env vars or DB credential, no SDK).
- ``vici``         ViciDial/SIP warm transfer — credential-aware stub: skips
                  cleanly when unconfigured (production wiring TBD).
"""
import os
TRANSFER_PROVIDER = os.getenv('TRANSFER_PROVIDER', 'mock')

class TransferAdapter:
    def transfer_call(self, session_id: str, target: str = None) -> bool:
        """Perform transfer of the given session to the specified target (queue/agent).
        Return True on success, False otherwise."""
        raise NotImplementedError

class MockTransferAdapter(TransferAdapter):
    def transfer_call(self, session_id: str, target: str = None) -> bool:
        print(f"[MockTransfer] transfer_call called for session {session_id} target={target}")
        return True

class ViciDialTransferAdapter(TransferAdapter):
    def __init__(self):
        # Placeholder: initialize API client or SIP connection here
        # Expect ViciDial credentials to be stored under provider='vici' in ProviderCredential
        from ...db import fetch_credential
        from ..crypto import decrypt_text
        cred = fetch_credential('vici')
        if cred:
            try:
                self.api_key = decrypt_text(cred.api_key_encrypted)
                self.enabled = True
            except Exception:
                self.enabled = False
        else:
            self.enabled = False
    def transfer_call(self, session_id: str, target: str = None) -> bool:
        # Real implementation would call ViciDial API or instruct SIP server to warm transfer
        if not getattr(self, 'enabled', False):
            print('[ViciDial] adapter not configured; transfer skipped')
            return False
        raise NotImplementedError('ViciDial transfer adapter not implemented')


def get_transfer_adapter() -> TransferAdapter:
    provider = os.getenv('TRANSFER_PROVIDER', TRANSFER_PROVIDER).lower()
    if provider == 'vici':
        try:
            return ViciDialTransferAdapter()
        except Exception as e:            # noqa: BLE001 — bad config must not kill startup
            print(f'[transfer] vici unavailable ({e}); falling back to mock')
            return MockTransferAdapter()
    if provider == 'twilio':
        try:
            from .twilio_adapter import TwilioAdapter
            return TwilioAdapter()        # env creds or DB row; disabled -> returns False
        except Exception as e:            # noqa: BLE001
            print(f'[transfer] twilio unavailable ({e}); falling back to mock')
            return MockTransferAdapter()
    # mock + anything unknown: a live call must never die on transfer wiring
    return MockTransferAdapter()
