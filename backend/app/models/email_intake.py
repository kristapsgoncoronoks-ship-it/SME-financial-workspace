from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import GUID, Base, TimestampMixin, UUIDPrimaryKeyMixin


class EmailIntake(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Per-org inbound-email configuration: a unique address token invoices can
    be emailed to. One row per org; the address is ``<token>@<inbound-domain>``."""

    __tablename__ = "email_intakes"
    __table_args__ = (UniqueConstraint("org_id", name="uq_email_intake_org"),)

    org_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Globally unique so an inbound address maps to exactly one tenant.
    token: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)


class InboundInvoice(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An invoice received by email — one row per attachment. Parsed through the
    same extraction engine into a draft a user confirms into an Invoice."""

    __tablename__ = "inbound_invoices"
    __table_args__ = (Index("ix_inbound_invoices_org_message", "org_id", "message_id"),)

    org_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    from_addr: Mapped[str | None] = mapped_column(String(320), nullable=True)
    subject: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # BE-009 (audit 2026-09-05): the provider's Message-ID, so a webhook retry
    # of a delivery we already stored is recognised instead of duplicating every
    # attachment in the review inbox. Nullable: a provider that sends none gets
    # no dedupe (documented), never a guessed identity.
    message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    filename: Mapped[str] = mapped_column(String(300), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    # pending → confirmed | failed | discarded
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False, index=True)
    method: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )  # detected type / parse path
    draft_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # ParsedInvoiceDraft JSON
    reading_preparation_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # H-1: the CLASSIFIED cause when the row is `failed`/`rejected` — a stable
    # code from `capture_failures.KINDS`. `error` above keeps the raw message.
    failure_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # F-06: how many times this record has FAILED. Incremented on each transition
    # into a failed state, and what an acknowledgement is pinned to.
    #
    # It replaces a wall-clock timestamp as the failure IDENTITY. Coverage used to
    # be `ack.failure_seen_at >= failed_at`, so a re-failure recorded in the same
    # timestamp tick as the acknowledgement was wrongly treated as covered and
    # stayed hidden — a real failure the operator never saw again. That was
    # observed as a flaky test. Any timestamp column collides at its own
    # resolution; an integer does not.
    failure_seq: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)

    invoice_id: Mapped[str | None] = mapped_column(
        GUID(), ForeignKey("invoices.id", ondelete="SET NULL"), nullable=True
    )
