Title: feat: Deepgram adapter (STT) — runtime-configurable transcription via dashboard credentials

Summary:
- Adds a Deepgram STT adapter that reads its API key from the encrypted ProviderCredential stored in the database and uses the Deepgram SDK for transcription.
- The adapter is optional and local-first: if no Deepgram key is configured, the system remains in mock STT mode so local debugging is unaffected.
- Dashboard-configured provider keys become active at runtime without restarting the app.

What’s included:
- Deepgram adapter implementing the STTAdapter interface (backend/app/core/adapters/deepgram_adapter.py)
- STT factory updated to lazy-load the Deepgram adapter when STT_PROVIDER=deepgram (backend/app/core/adapters/stt_adapter.py)
- README notes for Deepgram setup and provider configuration
- Example flow for using typed local input and switching to Deepgram in the dashboard

How to test locally:
1. Export SECRET_KEY and set DATABASE_URL if needed.
2. Start the backend and frontend.
3. In the frontend, open Config → API Keys → choose Deepgram → enter your Deepgram API key and Save Key.
4. Set STT Provider to deepgram and Save Config.
5. Start a mock call and send a user utterance. The backend should pick up the Deepgram key and use the Deepgram adapter for transcription when configured.
6. If no key is present, local mock STT remains active and the app continues to work without changes.

Security notes:
- SECRET_KEY must match the value used to encrypt provider keys.
- The Reveal button returns decrypted values; this is currently unrestricted as requested.

Checklist for PR review:
- [ ] Backend starts cleanly
- [ ] Add Deepgram key via dashboard and verify STT provider switch works
- [ ] No secrets in repo
- [ ] README updated with Deepgram setup steps

Notes:
- The adapter uses the Deepgram SDK and reads the configured provider credential from the database at runtime.
- The default local-debug path remains mock STT; this keeps localhost iteration fast and low-risk while production adapters are introduced.
