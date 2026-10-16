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
        pass
    def transfer_call(self, session_id: str, target: str = None) -> bool:
        # Real implementation would call ViciDial API or instruct SIP server to warm transfer
        raise NotImplementedError("ViciDial transfer adapter not implemented yet")


def get_transfer_adapter() -> TransferAdapter:
    if TRANSFER_PROVIDER == 'mock':
        return MockTransferAdapter()
    if TRANSFER_PROVIDER == 'vici':
        return ViciDialTransferAdapter()
    raise NotImplementedError(f"Transfer provider {TRANSFER_PROVIDER} not implemented")
