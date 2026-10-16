from sqlmodel import SQLModel, Field, Relationship
from typing import Optional, List
from datetime import datetime

class CallRecord(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: str
    state: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

class Transcript(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    call_id: Optional[int] = Field(default=None, foreign_key="callrecord.id")
    role: str
    text: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
