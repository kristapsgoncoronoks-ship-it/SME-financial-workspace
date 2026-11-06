"""Bank-statement import + reconciliation (Phase 12 of the data model).

A `bank_statements` row is one imported statement (a CSV/PDF a treasurer uploads);
`bank_lines` are its individual transactions. Reconciliation MATCHES each bank line
to a cash movement already on our books — an incoming CREDIT to a `receipt` (money
received against issued invoices), an outgoing DEBIT to a paid `reimbursement_batch`
(an expense payout) — so the statement and the ledger agree, line by line.

The match is a POLYMORPHIC soft reference (`matched_kind` + `matched_id`, org-scoped)
rather than a composite FK, because a line points at one of several target tables.
`amount` is SIGNED: a credit is positive (money in), a debit negative (money out) —
mirroring how a bank prints it. Tenant-scoped (org_id + RLS + ORM guard).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import GUID, Base, TimestampMixin, UUIDPrimaryKeyMixin

Money = Numeric(14, 2)


class BankStatement(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "bank_statements"
    __table_args__ = (
        # Composite-FK target for bank_lines.statement_id (tenant-safe).
        UniqueConstraint("org_id", "id", name="uq_bank_statements_org_id"),
        # DB-004 (audit 2026-09-05): the duplicate-import guard was a SELECT
        # then INSERT in `reconciliation.import_statement`; two uploads of the
        # same file landing together both passed the SELECT, and the org held
        # two statements and two full sets of bank lines for one set of bank
        # data — each duplicate credit independently matchable, so one customer
        # payment could settle two invoices. The database holds the invariant.
        UniqueConstraint("org_id", "sha256", name="uq_bank_statements_org_sha"),
    )

    org_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    filename: Mapped[str] = mapped_column(String(300), nullable=False)
    source_format: Mapped[str] = mapped_column(String(12), default="csv", nullable=False)
    # The parser method actually used (csv | text-layer | ocr).
    method: Mapped[str] = mapped_column(String(16), default="csv", nullable=False)
    # SHA-256 of the uploaded bytes — duplicate-import guard.
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    line_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(320), nullable=True)


class BankLine(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "bank_lines"
    __table_args__ = (
        CheckConstraint(
            "version = CAST(version AS INTEGER) AND version >= 1 AND version <= 2147483647",
            name="ck_bank_lines_version",
        ),
        Index("uq_bank_lines_org_id", "org_id", "id", unique=True),
        ForeignKeyConstraint(
            ["org_id", "statement_id"],
            ["bank_statements.org_id", "bank_statements.id"],
            name="fk_bank_lines_statement",
            ondelete="CASCADE",
        ),
        Index("ix_bank_lines_org_statement", "org_id", "statement_id"),
        Index("ix_bank_lines_org_status", "org_id", "status"),
    )

    org_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    statement_id: Mapped[str] = mapped_column(GUID(), nullable=False)
    # A migration baseline, not a reconstruction of historical decisions.
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    line_date: Mapped[date] = mapped_column(Date, nullable=False)
    description: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    # Signed: credit (money in) positive, debit (money out) negative.
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    direction: Mapped[str] = mapped_column(String(8), default="credit", nullable=False)
    # The currency the BANK stated for this line (camt `Amt/@Ccy`). NULL means the
    # source stated none — a CSV/PDF statement usually does not — which is NOT the
    # same as "the org's currency". A stated currency that is not the base currency
    # blocks matching: every reconcilable target is denominated in the base.
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    balance: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    # unmatched | matched | ignored
    status: Mapped[str] = mapped_column(String(12), default="unmatched", nullable=False)
    # Soft polymorphic reference to the reconciled cash movement.
    matched_kind: Mapped[str | None] = mapped_column(
        String(16), nullable=True
    )  # receipt | reimbursement
    matched_id: Mapped[str | None] = mapped_column(GUID(), nullable=True)
