@@
 @app.post("/api/config")
 async def update_config(payload: dict):
@@
     return JSONResponse({"status": "ok", "config": get_runtime_config()})
 
 @app.get('/api/config')
 async def get_config():
     return get_runtime_config()
+
+
+# Credential management endpoints (store encrypted API keys)
+@app.post('/api/credentials')
+async def save_credential(payload: dict, db: Session = Depends(get_session)):
+    """Payload: {provider: 'openai', api_key: 'xxx'}"""
+    provider = payload.get('provider')
+    api_key = payload.get('api_key')
+    if not provider or not api_key:
+        return JSONResponse({'error': 'provider and api_key required'}, status_code=400)
+    from .core.crypto import encrypt_text
+    enc = encrypt_text(api_key)
+    from .db import upsert_credential
+    pc = upsert_credential(provider, enc)
+    return {'status': 'ok', 'provider': provider}
+
+
+@app.get('/api/credentials')
+async def list_credentials(db: Session = Depends(get_session)):
+    from .db import fetch_credential
+    from .core.crypto import decrypt_text
+    from sqlmodel import select
+    creds = []
+    with Session(__import__('sqlmodel').create_engine("sqlite:///./dev.db", connect_args={"check_same_thread": False})) as s:
+        rows = s.exec(select(__import__('app').models.ProviderCredential)).all()
+        for r in rows:
+            try:
+                plain = decrypt_text(r.api_key_encrypted)
+                masked = plain[:4] + '...' + plain[-4:]
+            except Exception:
+                masked = '****'
+            creds.append({'provider': r.provider, 'api_key_masked': masked, 'created_at': r.created_at.isoformat()})
+    return creds
