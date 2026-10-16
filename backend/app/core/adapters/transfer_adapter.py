"""Transfer adapter interface and mock implementation.

A transfer adapter performs the handoff of a call to a live agent. In local mock mode the
adapter just records the request. Production adapters (e.g., ViciDial SIP warm-transfer) can
be added by implementing TransferAdapter and wiring credentials.
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
        from app.db import fetch_credential
        from app.core.crypto import decrypt_text
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
        # For now raise NotImplementedError to indicate adapter needs configuration
        if not getattr(self, 'enabled', False):
            raise RuntimeError('ViciDial adapter not configured')
        raise NotImplementedError('ViciDial transfer adapter not implemented')

class TwilioSipTransferAdapter(TransferAdapter):
    def __init__(self):
        from app.db import fetch_credential
        from app.core.crypto import decrypt_text
        cred = fetch_credential('twilio')
        if cred:
            try:
                self.api_key = decrypt_text(cred.api_key_encrypted)
                self.enabled = True
            except Exception:
                self.enabled = False
        else:
            self.enabled = False
    def transfer_call(self, session_id: str, target: str = None) -> bool:
        # Real implementation would instruct Twilio or SIP to perform warm transfer
        if not getattr(self, 'enabled', False):
            raise RuntimeError('Twilio adapter not configured')
        raise NotImplementedError('Twilio SIP transfer adapter not implemented')


def get_transfer_adapter() -> TransferAdapter:
    provider = os.getenv('TRANSFER_PROVIDER', TRANSFER_PROVIDER)
    if provider == 'mock':
        return MockTransferAdapter()
    if provider == 'vici':
        return ViciDialTransferAdapter()
    if provider == 'twilio':
        return TwilioSipTransferAdapter()
    raise NotImplementedError(f"Transfer provider {provider} not implemented")
