from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    event,
    select,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import GUID, Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.fx import FX_SOURCE_CHECK

Money = Numeric(14, 2)

# Lifecycle (see app.services.expense_state for the transition graph):
#   draft ⇄ returned → submitted → approved → marked_for_reimbursement → reimbursed
#                                 ↘ rejected      (submitted → returned = send back)
# withdraw takes submitted → draft before a decision is made.
EXPENSE_STATUSES = (
    "draft",
    "submitted",
    "partially_approved",
    "approved",
    "rejected",
    "returned",
    "marked_for_reimbursement",
    "reimbursed",
)
# Line-item kinds. A standard receipted spend, a mileage claim (distance × rate),
# or a per-diem allowance (days × rate).
EXPENSE_TYPES = ("standard", "mileage", "per_diem")
EXPENSE_CATEGORIES = (
    "travel",
    "meals",
    "accommodation",
    "transport",
    "supplies",
    "software",
    "other",
)


class ExpenseReport(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An employee's expense claim — a set of receipted items submitted for
    approval and reimbursement. Tenant-scoped (org_id) and owned by an employee."""

    __tablename__ = "expense_reports"
    __table_args__ = (
        # DB-010: the item parent key includes the tenant. A unique index is a
        # valid FK target without rebuilding existing report rows on SQLite.
        Index("uq_expense_reports_org_id", "org_id", "id", unique=True),
        # DB-011: the workflow state is a closed set; a value outside it is a
        # bug reaching storage, never data. Kept in step with EXPENSE_STATUSES.
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in EXPENSE_STATUSES) + ")",
            name="ck_expense_reports_status",
        ),
    )

    org_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # RESTRICT, not CASCADE (DB-009): a claim is a financial document — it may
    # be approved, reimbursed and booked. Removing the employee's user row must
    # not erase it; erasure (privacy.erase) pseudonymises the person instead.
    employee_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    employee_name: Mapped[str] = mapped_column(String(200), nullable=False)
    # WO-M: the recycle bin (owner decision 2026-08-15 — "bin extends to all
    # entities"). Stamped instead of destroyed; hidden from every read by the
    # ORM guard (SOFT_DELETE_MODELS), restorable for BIN_RETENTION_DAYS.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_by: Mapped[str | None] = mapped_column(String(320), nullable=True)

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    # Width fits the longest workflow status ("marked_for_reimbursement" = 24).
    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False, index=True)
    currency: Mapped[str] = mapped_column(String(3), default="EUR", nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    total: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    vat_total: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    total_eur: Mapped[Decimal | None] = mapped_column(Money, nullable=True)

    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Reviewed submission identity (R4): decisions, withdrawal and a changed
    # pending approver invalidate the previous review. The first submit retains
    # the draft counter. All editable mutations and lifecycle writes share the
    # report row lock; a withdrawn/edited resubmission cannot accept an earlier
    # approver version, and no late draft edit can overwrite submitted contents.
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)

    # Reimbursement (Phase 09): payment metadata recorded when the report is paid,
    # and an optional link to the payout batch it was paid in. `payout_batch_id`
    # is a soft (org-scoped) reference — tenant isolation is enforced by the ORM
    # guard + RLS, so no composite FK is needed on this existing table.
    reimbursed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payment_method: Mapped[str | None] = mapped_column(String(40), nullable=True)
    payment_reference: Mapped[str | None] = mapped_column(String(200), nullable=True)
    payout_batch_id: Mapped[str | None] = mapped_column(GUID(), nullable=True, index=True)

    items: Mapped[list[ExpenseItem]] = relationship(
        back_populates="report", cascade="all, delete-orphan", order_by="ExpenseItem.spend_date"
    )


class ExpenseItem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "expense_items"
    __table_args__ = (
        Index("uq_expense_items_org_id", "org_id", "id", unique=True),
        ForeignKeyConstraint(
            ["org_id", "report_id"],
            ["expense_reports.org_id", "expense_reports.id"],
            name="fk_expense_items_report",
            ondelete="CASCADE",
        ),
        # FX provenance is a closed enum (WO-8): eur/stated/ecb/unknown or NULL.
        CheckConstraint(FX_SOURCE_CHECK, name="ck_expense_items_fx_source"),
        # Slice 2b: normalised links to the cost-allocation master tables, same
        # composite-FK tenant guard invoices use. Requires org_id on this table
        # (denormalised from the parent report — see the before_insert hook).
        ForeignKeyConstraint(
            ["org_id", "cost_center_id"],
            ["cost_centers.org_id", "cost_centers.id"],
            name="fk_expense_items_cost_center",
            ondelete="SET NULL",
        ),
        ForeignKeyConstraint(
            ["org_id", "department_id"],
            ["departments.org_id", "departments.id"],
            name="fk_expense_items_department",
            ondelete="SET NULL",
        ),
        ForeignKeyConstraint(
            ["org_id", "project_id"],
            ["projects.org_id", "projects.id"],
            name="fk_expense_items_project",
            ondelete="SET NULL",
        ),
        Index("ix_expense_items_org_cost_center", "org_id", "cost_center_id"),
        Index("ix_expense_items_org_department", "org_id", "department_id"),
        Index("ix_expense_items_org_project", "org_id", "project_id"),
    )

    # Denormalised tenant key (mirrors the parent report's org_id). Makes the row
    # first-class tenant-scoped: the ORM guard + Postgres RLS can restrict it
    # directly instead of trusting the join, and the composite cost-allocation FKs
    # above can be tenant-safe. Populated automatically by the before_insert hook.
    org_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    report_id: Mapped[str] = mapped_column(GUID(), nullable=False, index=True)
    spend_date: Mapped[date] = mapped_column(Date, nullable=False)
    category: Mapped[str] = mapped_column(String(40), default="other", nullable=False)
    description: Mapped[str] = mapped_column(String(300), nullable=False)
    merchant: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # `amount` is the GROSS spend in the report's reporting currency (the converted
    # figure). When the receipt was in another currency, `original_amount`/`currency`
    # carry the source figure and `fx_rate`/`fx_source` its provenance (non-negotiable
    # #3: original + reporting currency with provenance).
    amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)  # gross
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)  # original currency
    original_amount: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    # `fx_rate` follows the ONE convention (WO-8): original-currency units per 1
    # unit of the report currency — converting divides. `fx_source` is the closed
    # provenance enum (models.fx.FxSource: eur/stated/ecb/unknown).
    fx_rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    fx_source: Mapped[str | None] = mapped_column(String(40), nullable=True)
    vat_amount: Mapped[Decimal] = mapped_column(
        Money, default=Decimal("0"), nullable=False
    )  # tax amount
    # Whether the tax on this item is reclaimable (some categories/countries aren't).
    reclaimable_tax: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    payment_method: Mapped[str] = mapped_column(String(20), default="personal", nullable=False)
    # Rebillable to a client — feeds project/customer billing.
    customer_billable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    billable_customer: Mapped[str | None] = mapped_column(String(200), nullable=True)
    comment: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # business purpose (Concur-style)

    # Entry kind + the mileage / per-diem computation inputs (null on a standard item).
    expense_type: Mapped[str] = mapped_column(String(16), default="standard", nullable=False)
    mileage_distance: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    mileage_rate: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    mileage_unit: Mapped[str | None] = mapped_column(String(4), nullable=True)  # "km" | "mi"
    per_diem_days: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    per_diem_rate: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    # A declared reason for a missing receipt (in lieu of the document itself), so a
    # receipt-required item can still be submitted with an explicit affidavit.
    missing_receipt_declaration: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Cost-allocation dimensions (see app.core.dimensions) — e.g. tag fuel to a
    # vehicle, a site visit to a property, billable time to a project.
    cost_center: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    department: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    project: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    vehicle: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    property_ref: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)

    # Slice 2b: normalised links to the cost-allocation master tables (nullable
    # during the dual-read transition; the free-text tags above are the fallback).
    cost_center_id: Mapped[str | None] = mapped_column(GUID(), nullable=True)
    department_id: Mapped[str | None] = mapped_column(GUID(), nullable=True)
    project_id: Mapped[str | None] = mapped_column(GUID(), nullable=True)
    # Bank-statement verification: a human-readable reference to the matched bank/
    # card transaction. Non-null ⇒ this entry is reconciled against the statement.
    bank_reference: Mapped[str | None] = mapped_column(String(300), nullable=True)

    receipt_mime: Mapped[str | None] = mapped_column(String(60), nullable=True)
    # Object-storage reference (ADR-0008): receipt bytes live in object storage,
    # keyed by sha256. The legacy in-DB blob was dropped once migrated.
    receipt_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    receipt_size: Mapped[int | None] = mapped_column(Integer, nullable=True)

    report: Mapped[ExpenseReport] = relationship(back_populates="items")


@event.listens_for(ExpenseItem, "before_insert")
def _fill_expense_item_org(mapper, connection, target: ExpenseItem) -> None:
    """Denormalise `org_id` from the parent report on insert, so every code path
    that creates an item — via the `report`/`items` relationship OR by setting
    `report_id` directly — gets it without having to remember.

    By the time this fires the FK (`report_id`) is always populated. We prefer the
    already-loaded parent (no query); otherwise we look the org up on the flush
    connection. `__dict__.get` reads the loaded relationship WITHOUT triggering a
    lazy load (which would fail on the async engine)."""
    if target.org_id is not None:
        return
    parent = target.__dict__.get("report")
    if parent is not None:
        target.org_id = parent.org_id
    elif target.report_id is not None:
        target.org_id = connection.scalar(
            select(ExpenseReport.org_id).where(ExpenseReport.id == target.report_id)
        )


class ExpenseTransaction(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An 'available expense' — a card/bank-feed transaction in the employee's
    inbox (SAP Concur style). It sits unassigned until the employee adds it to a
    report, at which point it becomes an expense entry."""

    __tablename__ = "expense_transactions"

    # WO-M: the recycle bin (owner decision 2026-08-15 — "bin extends to all
    # entities"). Stamped instead of destroyed; hidden from every read by the
    # ORM guard (SOFT_DELETE_MODELS), restorable for BIN_RETENTION_DAYS.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_by: Mapped[str | None] = mapped_column(String(320), nullable=True)
    # The 'available expenses' inbox filters by (employee, status) together.
    __table_args__ = (
        Index("ix_exp_txn_employee_status", "employee_id", "status"),
        # The single item FK detaches the optional ID on SQLite. PostgreSQL
        # projects this immediate guard to SET NULL(item_id), preserving org.
        ForeignKeyConstraint(
            ["org_id", "item_id"],
            ["expense_items.org_id", "expense_items.id"],
            name="fk_expense_transactions_item",
        ),
        ForeignKeyConstraint(
            ["org_id", "import_batch_id"],
            ["expense_import_batches.org_id", "expense_import_batches.id"],
            name="fk_expense_transaction_batch",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "org_id", "import_batch_id", "source_row", name="uq_expense_transaction_source_row"
        ),
    )

    org_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # RESTRICT (DB-009), as on expense_reports: an imported card/bank line that
    # was assigned to a claim is part of that claim's evidence.
    employee_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    txn_date: Mapped[date] = mapped_column(Date, nullable=False)
    description: Mapped[str] = mapped_column(String(300), nullable=False)
    merchant: Mapped[str | None] = mapped_column(String(200), nullable=True)
    amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="EUR", nullable=False)
    direction: Mapped[str] = mapped_column(String(8), default="debit", nullable=False)
    source: Mapped[str] = mapped_column(String(20), default="bank_statement", nullable=False)
    status: Mapped[str] = mapped_column(
        String(12), default="available", nullable=False, index=True
    )  # available|assigned
    item_id: Mapped[str | None] = mapped_column(
        GUID(), ForeignKey("expense_items.id", ondelete="SET NULL"), nullable=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    import_batch_id: Mapped[str | None] = mapped_column(GUID(), nullable=True)
    source_row: Mapped[int | None] = mapped_column(Integer, nullable=True)
    currency_source: Mapped[str | None] = mapped_column(String(16), nullable=True)


class ExpenseImportBatch(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Exact source identity, independent of candidate bin/restore/purge."""

    __tablename__ = "expense_import_batches"
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_expense_import_batch_org_id"),
        UniqueConstraint(
            "org_id",
            "employee_id",
            "account_reference",
            "source_sha256",
            name="uq_expense_import_source",
        ),
    )
    org_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    employee_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    account_reference: Mapped[str] = mapped_column(String(120), nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(40), nullable=False)
    method: Mapped[str] = mapped_column(String(40), nullable=False)
    statement_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    credit_count: Mapped[int] = mapped_column(Integer, nullable=False)
    warnings_json: Mapped[str] = mapped_column(Text, nullable=False)


class ExpenseCommand(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Committed expense outcome; not a job or automation executor."""

    __tablename__ = "expense_commands"
    __table_args__ = (
        UniqueConstraint(
            "org_id", "employee_id", "command_key", name="uq_expense_command_identity"
        ),
        Index("ix_expense_commands_org_operation_target", "org_id", "operation", "target_id"),
    )
    org_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    employee_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    command_key: Mapped[str] = mapped_column(String(120), nullable=False)
    operation: Mapped[str] = mapped_column(String(24), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str] = mapped_column(GUID(), nullable=False)
    result_json: Mapped[str] = mapped_column(Text, nullable=False)


class ExpenseComment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A comment on an expense report — the employee↔approver thread."""

    __tablename__ = "expense_comments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "report_id"],
            ["expense_reports.org_id", "expense_reports.id"],
            name="fk_expense_comments_report",
            ondelete="CASCADE",
        ),
    )

    org_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    report_id: Mapped[str] = mapped_column(GUID(), nullable=False, index=True)
    author_id: Mapped[str] = mapped_column(GUID(), nullable=False)
    author_name: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)


# Reimbursement statuses.
BATCH_OPEN = "open"
BATCH_PAID = "paid"
BATCH_CANCELLED = "cancelled"
BATCH_STATUSES = (BATCH_OPEN, BATCH_PAID, BATCH_CANCELLED)


class ReimbursementBatch(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A payout run (Phase 09): a set of APPROVED expense reports grouped for
    payment, marked paid together with a shared method + reference, and exportable
    as a bank/payroll file. Marking the batch paid flips each linked report to
    `reimbursed` and stamps its payment metadata. Tenant-scoped."""

    __tablename__ = "reimbursement_batches"
    __table_args__ = (
        # Target for a future composite FK from expense_reports; also the tenant
        # uniqueness guard used across the codebase.
        UniqueConstraint("org_id", "id", name="uq_reimbursement_batches_org_id"),
        Index("ix_reimbursement_batches_org_status", "org_id", "status"),
        # DB-011: closed set, see BATCH_STATUSES.
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in BATCH_STATUSES) + ")",
            name="ck_reimbursement_batches_status",
        ),
    )

    org_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    reference: Mapped[str | None] = mapped_column(String(200), nullable=True)
    method: Mapped[str] = mapped_column(String(40), default="bank_transfer", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=BATCH_OPEN, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Sum of the linked reports' EUR totals (reports may be multi-currency).
    total_eur: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # R6 (§4.8 SoD): the immutable user id of the maker, compared against the
    # payer at `pay` time. NULL for pre-migration batches — the maker≠checker
    # check falls back to the `created_by` email for those (see
    # `reimbursement._sod_conflict`), mirroring `PaymentRun.created_by_id`.
    created_by_id: Mapped[str | None] = mapped_column(GUID(), nullable=True)
    # WO-9 export-once (same treatment as payment_runs): first bank-file export,
    # total exports produced, and the last pain.001 MsgId sent to the bank.
    exported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    export_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_msg_id: Mapped[str | None] = mapped_column(String(35), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class ExpensePolicy(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """The org's expense-spending rules (Phase 09) — one active policy per tenant.
    Caps are ADVISORY: they flag out-of-policy items for the approver rather than
    hard-blocking submission (the business-purpose + receipt compliance gate stays
    the hard submit gate). Any cap left null is not enforced."""

    __tablename__ = "expense_policies"
    __table_args__ = (UniqueConstraint("org_id", name="uq_expense_policies_org"),)

    org_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Hard-ish per-item ceiling (any single item above this is flagged).
    max_item_amount: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    # A receipt is expected on any item above this amount (flag if missing).
    receipt_required_over: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    # Per-category ceilings as a JSON object {category: max_amount}.
    category_caps: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- Configurable review rules (all advisory by default; see `blocking_rules`) --
    # JSON array of allowed category keys; an item outside it is flagged.
    allowed_categories: Mapped[str | None] = mapped_column(Text, nullable=True)
    # JSON array of ISO currency codes the org accepts; anything else is flagged.
    allowed_currencies: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Flag spend dated on a Saturday/Sunday.
    warn_weekend: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Flag likely duplicates (same receipt hash, or same amount+date+merchant).
    duplicate_detection: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Sanctioned mileage rate + the ± tolerance a claimed rate may deviate by.
    mileage_rate: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    mileage_rate_tolerance: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    # Require a business purpose on any item above this amount (flag if missing).
    require_purpose_over: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    # Flag an item whose spend date is older than this many days at submission.
    late_submission_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # JSON array of rule codes that HARD-BLOCK submission (everything else warns).
    blocking_rules: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
