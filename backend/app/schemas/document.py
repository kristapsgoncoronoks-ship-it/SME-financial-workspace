from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    sha256: str
    size: int
    mime: str | None = None
    kind: str
    filename: str | None = None
    uploaded_by: str | None = None
    created_at: datetime


class DocumentPageOut(BaseModel):
    items: list[DocumentOut]
    total: int
    kinds: list[str]
