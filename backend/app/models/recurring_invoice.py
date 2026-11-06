from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import GUID, Base, TimestampMixin, UUIDPrimaryKeyMixin


class RecurringInvoice(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A schedule that generates issued invoices on a cadence (subscriptions,
    retainers, rent). It is a TEMPLATE plus a schedule — it never itself owes
    money. Each run materialises a real `IssuedInvoice` from the frozen template
    and advances `next_run_date`.

    Generation is idempotent: the invoice is created and `next_run_date` advanced
    in the same transaction, so a re-run (or an overlapping worker) never emits a
    duplicate for a date it has already passed.
    """

    __tablename__ = "recurring_invoices"

    # WO-M: the recycle bin (owner decision 2026-08-15 — "bin extends to all
    # entities"). Stamped instead of destroyed; hidden from every read by the
    # ORM guard (SOFT_DELETE_MODELS), restorable for BIN_RETENTION_DAYS.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_by: Mapped[str | None] = mapped_column(String(320), nullable=True)
    # Worker sweep: find active schedules that are due.
    __table_args__ = (
        Index("ix_recurring_org_due", "org_id", "active", "next_run_date"),
        ForeignKeyConstraint(
            ["org_id", "partner_id"],
            ["partners.org_id", "partners.id"],
            name="fk_recurring_invoices_partner",
        ),
    )

    org_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    partner_id: Mapped[str | None] = mapped_column(
        GUID(), ForeignKey("partners.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # A friendly label for the schedule list (e.g. "Globex — monthly retainer").
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # The frozen invoice template: an IssuedInvoiceCreate-shaped payload (buyer,
    # lines, vat_scheme, note, penalty_rate, currency) WITHOUT the schedule/dates.
    template_json: Mapped[str] = mapped_column(Text, nullable=False)

    # Cadence: every `interval` × `frequency`. Payment terms optional override.
    frequency: Mapped[str] = mapped_column(
        String(10), default="monthly", nullable=False
    )  # weekly|monthly|quarterly|yearly
    interval: Mapped[int] = mapped_column(Integer, default=1, nullable=False)  # every N periods
    payment_terms_days: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )  # None → issuer default

    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    next_run_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date | None] = mapped_column(
        Date, nullable=True
    )  # inclusive; None → open-ended
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    last_generated_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    generated_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
