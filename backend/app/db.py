from sqlmodel import SQLModel, create_engine, Session, select
from typing import Optional
from datetime import datetime
import os

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./dev.db")
engine = create_engine(DATABASE_URL, echo=False, connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {})

SECRET_KEY = os.getenv("SECRET_KEY", "changeme")

def init_db():
    SQLModel.metadata.create_all(engine)

def get_session():
    with Session(engine) as session:
        yield session

# Helper to fetch a provider credential
from .models import ProviderCredential

def fetch_credential(provider_name: str) -> Optional[ProviderCredential]:
    with Session(engine) as session:
        res = session.exec(select(ProviderCredential).where(ProviderCredential.provider == provider_name)).first()
        return res

def upsert_credential(provider_name: str, encrypted_key: str):
    with Session(engine) as session:
        existing = session.exec(select(ProviderCredential).where(ProviderCredential.provider == provider_name)).first()
        if existing:
            existing.api_key_encrypted = encrypted_key
            existing.updated_at = datetime.utcnow()
            session.add(existing)
            session.commit()
            session.refresh(existing)
            return existing
        else:
            pc = ProviderCredential(provider=provider_name, api_key_encrypted=encrypted_key)
            session.add(pc)
            session.commit()
            session.refresh(pc)
            return pc
