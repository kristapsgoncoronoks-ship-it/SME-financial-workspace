"""Vendor protected-field change requests (WO-2) — the payment-redirection
fraud-safety invariant.

A vendor's `iban` and `tax_id` are PROTECTED: on an existing vendor they are
never written directly. A request to change one lands here as a `pending` row
recording old→new, and only a SECOND user holding SETTINGS_MANAGE may approve
it (maker ≠ checker, invariant §4.8). Approval applies the value and audits;
rejection audits the reason. The row itself stores the FULL values (they are
needed to apply the change and to verify it against the source document) — the
AUDIT trail, by contrast, only ever carries a masked IBAN (last 4 + length).

Tenant-scoped: composite FK (org_id, vendor_id) → vendors(org_id, id), listed
in TENANT_MODELS and covered by an RLS policy in the same migration.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import GUID, Base, TimestampMixin, UUIDPrimaryKeyMixin, utcnow

CR_PENDING = "pending"
CR_APPROVED = "approved"
CR_REJECTED = "rejected"
CR_STATUSES = (CR_PENDING, CR_APPROVED, CR_REJECTED)


class VendorChangeRequest(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "vendor_change_requests"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "source_document_id"],
            ["documents.org_id", "documents.id"],
            name="fk_vendor_change_requests_source_document",
        ),
        # Cross-tenant-safe link: the vendor MUST belong to the same org.
        ForeignKeyConstraint(
            ["org_id", "vendor_id"],
            ["vendors.org_id", "vendors.id"],
            name="fk_vendor_change_requests_vendor",
            ondelete="CASCADE",
        ),
        Index("ix_vendor_change_requests_org_vendor_status", "org_id", "vendor_id", "status"),
        # At most ONE open request per (vendor, field): two competing pending
        # values for the same account number would make "what gets approved"
        # ambiguous. Partial unique index (SQLite + Postgres); the service also
        # pre-checks to return a friendly 409 instead of an IntegrityError.
        Index(
            "uq_vendor_change_requests_open_field",
            "org_id",
            "vendor_id",
            "field",
            unique=True,
            sqlite_where=text("status = 'pending'"),
            postgresql_where=text("status = 'pending'"),
        ),
    )

    org_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    vendor_id: Mapped[str] = mapped_column(GUID(), nullable=False)
    # Which protected field ("iban" | "tax_id") — see services.vendors.PROTECTED_FIELDS.
    field: Mapped[str] = mapped_column(String(32), nullable=False)
    old_value: Mapped[str | None] = mapped_column(String(128), nullable=True)
    new_value: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=CR_PENDING, nullable=False)
    requested_by: Mapped[str] = mapped_column(GUID(), nullable=False)
    # Email snapshot for display/audit continuity (same pattern as
    # AuditEvent.actor_email / ApprovalStep.decided_by_email).
    requested_by_email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    decided_by: Mapped[str | None] = mapped_column(GUID(), nullable=True)
    decided_by_email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The vaulted document the new value was read from (nullable; SET NULL if
    # the document row is ever removed — the request must outlive it).
    source_document_id: Mapped[str | None] = mapped_column(
        GUID(), ForeignKey("documents.id", ondelete="SET NULL"), nullable=True
    )
