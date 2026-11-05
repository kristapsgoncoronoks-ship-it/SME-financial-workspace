"""Multi-step expense-approval routing (mirrors the invoice approval engine).

`ExpenseApprovalPolicy` is the reusable rule: an ordered chain of approvers, chosen
for a report by amount threshold. `ExpenseApprovalStep` rows ARE the per-report
approval run — the report is the run, its ordered steps the instance; "the current
step" is the lowest-`seq` step still pending. No policy ⇒ one generic open step, so
the legacy single-approver behaviour is preserved unchanged.

The report parent link includes the denormalised `org_id`, alongside the ORM
guard and Postgres RLS, so each step belongs to its report's tenant.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import GUID, Base, TimestampMixin, UUIDPrimaryKeyMixin

# Step lifecycle.
STEP_PENDING = "pending"
STEP_APPROVED = "approved"
STEP_REJECTED = "rejected"
STEP_SKIPPED = "skipped"

# Step kinds.
KIND_APPROVER = "approver"
KIND_FINANCE_FINAL = "finance_final"


class ExpenseApprovalPolicy(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "expense_approval_policies"
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_expense_approval_policies_org_id"),
        Index("ix_expense_approval_policies_org_active", "org_id", "active", "priority"),
    )

    org_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Lower runs first; the first fully-matching active policy wins.
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    # Match criterion (null = wildcard): the report's EUR total must be >= this.
    min_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    # Ordered chain: a JSON array of user-ids, each an approval step in order.
    approver_ids: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Append a finance sign-off step at the end of the chain.
    finance_final: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    finance_approver_id: Mapped[str | None] = mapped_column(GUID(), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class ExpenseApprovalStep(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One step of a report's approval chain. `approver_id` NULL ⇒ any expense
    approver may act (an open step); non-null ⇒ a named approver."""

    __tablename__ = "expense_approval_steps"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "report_id"],
            ["expense_reports.org_id", "expense_reports.id"],
            name="fk_expense_approval_steps_report",
            ondelete="CASCADE",
        ),
        UniqueConstraint("org_id", "report_id", "seq", name="uq_expense_approval_steps_seq"),
        Index("ix_expense_approval_steps_report", "org_id", "report_id"),
        Index("ix_expense_approval_steps_inbox", "org_id", "approver_id", "status"),
    )

    org_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    report_id: Mapped[str] = mapped_column(GUID(), nullable=False, index=True)
    policy_id: Mapped[str | None] = mapped_column(GUID(), nullable=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)  # 0-based order
    kind: Mapped[str] = mapped_column(String(20), default=KIND_APPROVER, nullable=False)
    approver_id: Mapped[str | None] = mapped_column(GUID(), nullable=True)
    approver_email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=STEP_PENDING, nullable=False)
    decided_by: Mapped[str | None] = mapped_column(GUID(), nullable=True)
    decided_by_email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
