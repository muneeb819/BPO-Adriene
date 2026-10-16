Title: feat: OpenAI adapter (LLM) — runtime-wired via dashboard credentials

Summary:
- Adds an OpenAI LLM adapter (OpenAIAdapter) that reads its API key from the encrypted ProviderCredential stored in the DB and uses the openai.ChatCompletion API.
- Adapter is optional and defaults to erroring if no credential is available; system still falls back to mock LLM when LLM_PROVIDER=mock.
- No API keys are committed — add the OpenAI key via the dashboard (Config → API Keys) or via the /api/credentials endpoint.

What’s included:
- OpenAI adapter implementing the LLMAdapter interface (backend/app/core/adapters/openai_adapter.py)
- llm_adapter factory updated to lazily load OpenAI adapter when LLM_PROVIDER=openai (backend/app/core/adapters/llm_adapter.py)
- README notes for OpenAI usage and how to add key via dashboard
- (Tests can be added in a follow-up commit if desired)

How to test locally:
1. Ensure SECRET_KEY is exported: export SECRET_KEY="your_secret"
2. Start the backend:
   - cd backend
   - pip install -r requirements.txt
   - uvicorn backend.app.main:app --reload --port 8000
3. Start the frontend:
   - cd frontend
   - npm install
   - npm start
4. In the frontend, open Config → API Keys → choose OpenAI → paste your OpenAI API key and Save Key.
5. In Config, set LLM Provider to openai and Save Config. The runtime will pick up the key and use OpenAI for generation.
6. Start a mock call and exercise the flow. If the adapter fails to initialize you’ll get a RuntimeError in logs; check the backend logs.

Security notes:
- SECRET_KEY must match the value used to encrypt provider keys.
- Reveal endpoint will return the full key (you asked for unrestricted reveal in dashboard).

Checklist for PR review:
- [ ] Backend starts
- [ ] Add OpenAI key via dashboard and verify LLM responses change from mock to OpenAI
- [ ] No secrets in repo
- [ ] README updated with OpenAI setup steps

Notes:
- Default OpenAI model is gpt-3.5-turbo. This can be overridden via the OPENAI_MODEL environment variable.
- The adapter reads the encrypted OpenAI key from the ProviderCredential table in the database and decrypts it using SECRET_KEY at runtime; if decryption fails the adapter disables itself and the system remains in mock mode.
