"""Document registry (Slice 5d of the data model).

Binary originals live in object storage, content-addressed by sha256 (ADR-0008);
their sha/size are stored inline on each owner (an expense item's receipt, an
issuer logo, an email attachment). Until now there was no central metadata — no
way to list "what documents this workspace holds". This registry is one row per
distinct stored object `(org, content, kind)`, written automatically at the
single storage choke point (`documents.store`), so every current and future
upload path registers without extra wiring.

Tenant-scoped (org_id + RLS + ORM guard). Content-addressed dedup: re-storing the
same bytes under the same kind touches the existing row rather than duplicating.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import GUID, Base, TimestampMixin, UUIDPrimaryKeyMixin


class Document(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "documents"
    __table_args__ = (
        Index("uq_documents_org_id", "org_id", "id", unique=True),
        # One row per distinct (content, kind) within a tenant — content-addressed.
        UniqueConstraint("org_id", "sha256", "kind", name="uq_documents_org_sha_kind"),
        Index("ix_documents_org_kind", "org_id", "kind"),
        Index("ix_documents_org_created", "org_id", "created_at"),
    )

    org_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    mime: Mapped[str | None] = mapped_column(String(80), nullable=True)
    kind: Mapped[str] = mapped_column(
        String(24), nullable=False
    )  # receipts|logos|email-attachments
    filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    uploaded_by: Mapped[str | None] = mapped_column(String(200), nullable=True)  # actor email
