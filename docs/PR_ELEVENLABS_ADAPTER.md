Title: feat: ElevenLabs adapter (TTS) — runtime-configurable speech synthesis via dashboard credentials

Summary:
- Adds an ElevenLabs TTS adapter that reads its API key from the encrypted ProviderCredential stored in the database and uses the ElevenLabs text-to-speech API.
- The adapter is optional and local-first: if no ElevenLabs key is configured, the system remains in mock TTS mode so local debugging remains fast and safe.
- Dashboard-configured provider keys become active at runtime without restarting the app.

What’s included:
- ElevenLabs adapter implementing the TTSAdapter interface (backend/app/core/adapters/elevenlabs_adapter.py)
- TTS factory updated to lazy-load the ElevenLabs adapter when TTS_PROVIDER=elevenlabs (backend/app/core/adapters/tts_adapter.py)
- README notes for ElevenLabs setup and provider configuration
- Example flow for switching the app from browser SpeechSynthesis to server-side ElevenLabs playback during testing

How to test locally:
1. Export SECRET_KEY and set DATABASE_URL if needed.
2. Start the backend and frontend.
3. In the frontend, open Config → API Keys → choose ElevenLabs → enter your API key and Save Key.
4. Set TTS Provider to elevenlabs and Save Config.
5. Start a mock call and observe the bot response. In local debug it will usually still speak using browser SpeechSynthesis unless the backend is explicitly configured to emit TTS audio to the client.
6. If no key is present, mock TTS remains active and the app continues to work without changes.

Security notes:
- SECRET_KEY must match the value used to encrypt provider keys.
- The Reveal button returns decrypted values; this is currently unrestricted as requested.

Checklist for PR review:
- [ ] Backend starts cleanly
- [ ] Add ElevenLabs key via dashboard and verify TTS provider switch works
- [ ] No secrets in repo
- [ ] README updated with ElevenLabs setup steps

Notes:
- The adapter reads the configured provider credential from the database at runtime and decrypts it using SECRET_KEY.
- The default local-debug path remains mock TTS to keep localhost iteration cost-effective and low-risk while production providers are introduced.
