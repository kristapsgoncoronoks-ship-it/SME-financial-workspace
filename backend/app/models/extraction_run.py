"""Extraction lineage for received invoices (Slice 5b of the data model).

One row per capture attempt at the `parse_invoice_file` choke point: HOW an
uploaded document was read (method), whether it parsed, the source (filename +
content sha256), and how many line items / warnings it produced. Created at
UPLOAD time (server-authoritative — the client can't forge it) with no invoice
yet; LINKED to the invoice when the reviewed draft is saved. A run whose
`invoice_id` stays NULL is a parse that was never saved (an abandoned upload or a
failed parse) — itself useful provenance.

Tenant-scoped (org_id + RLS + ORM guard). The link is a composite FK
`(org_id, invoice_id) → invoices(org_id, id)` — cross-tenant-safe — with ON
DELETE CASCADE, so an invoice's lineage is removed with it (a NULL invoice_id row
is simply not FK-checked).
"""

from __future__ import annotations

from sqlalchemy import (
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import GUID, Base, TimestampMixin, UUIDPrimaryKeyMixin


class ExtractionRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "extraction_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "invoice_id"],
            ["invoices.org_id", "invoices.id"],
            name="fk_extraction_runs_invoice",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["org_id", "admission_command_id"],
            ["capture_commands.org_id", "capture_commands.id"],
            name="fk_extraction_runs_command",
            ondelete="CASCADE",
        ),
        UniqueConstraint("org_id", "admission_command_id", name="uq_extraction_runs_command"),
        # Composite-FK target for extraction_fields (Slice 5f).
        UniqueConstraint("org_id", "id", name="uq_extraction_runs_org_id"),
        Index("ix_extraction_runs_org_invoice", "org_id", "invoice_id"),
        Index("ix_extraction_runs_org_created", "org_id", "created_at"),
    )

    org_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    admission_command_id: Mapped[str | None] = mapped_column(GUID(), nullable=True)
    invoice_id: Mapped[str | None] = mapped_column(GUID(), nullable=True)
    source_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # e-invoice-xml | text-layer | ocr | csv | json | unknown | failed
    method: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(
        String(12), default="parsed", nullable=False
    )  # parsed|saved|failed
    field_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )  # line items parsed
    warning_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)  # first warning / error message
    # H-1: the CLASSIFIED cause when `status == "failed"` — a stable code from
    # `capture_failures.KINDS`, never prose. `note` above keeps the raw library
    # message for an engineer; this is what the operator's worklist reasons over.
    # NULL on a successful run, and on a failure recorded before this contract
    # existed (read as `unknown_failure`, which does not claim a cause).
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
    # Durable direct-upload generation. A job without this field in its payload
    # names legacy generation zero; it can never adopt a later retry.
    attempt_seq: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    # Async direct-upload capture (Stage B): the serialized ParsedInvoiceDraft the
    # worker produced, so the client can fetch it after the parse runs OFF the API
    # tier. NULL for a synchronous/email run or one still queued.
    draft_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Actual preparation for this published attempt, including failed readings.
    # Historical rows remain unknown; never infer it from current settings.
    reading_preparation_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Current bounded summary only; full facts remain in source-owned history.
    reading_observation_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    preview_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # WO-X — live progress, so a long capture can be told apart from a stuck one.
    # `stage` is a code from `capture_progress.STAGES`; NULL means the run predates
    # the contract and is reported as unknown rather than back-filled to a guess.
    stage: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Counted ONLY on the OCR phase, which is the only one that is both slow and
    # divisible. 0/NULL everywhere else — a page count invented for a CSV would be
    # a number nothing measured.
    pages_done: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    pages_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
