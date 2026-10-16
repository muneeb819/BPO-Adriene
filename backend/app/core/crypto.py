"""Simple encrypt/decrypt helpers using Fernet with a key derived from SECRET_KEY env.

Note: For production use, supply a strong SECRET_KEY and manage it via secrets manager.
"""
import os
import base64
import hashlib
from cryptography.fernet import Fernet

SECRET_KEY = os.getenv('SECRET_KEY', 'changeme')

def _derive_fernet_key(secret: str) -> bytes:
    # Derive a 32-byte key from the SECRET_KEY using SHA256 and urlsafe base64 encode it for Fernet
    h = hashlib.sha256(secret.encode('utf-8')).digest()
    return base64.urlsafe_b64encode(h)

def encrypt_text(plain: str) -> str:
    key = _derive_fernet_key(SECRET_KEY)
    f = Fernet(key)
    token = f.encrypt(plain.encode('utf-8'))
    return token.decode('utf-8')

def decrypt_text(token: str) -> str:
    key = _derive_fernet_key(SECRET_KEY)
    f = Fernet(key)
    plain = f.decrypt(token.encode('utf-8'))
    return plain.decode('utf-8')
