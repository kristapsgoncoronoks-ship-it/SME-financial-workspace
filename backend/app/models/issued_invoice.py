from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
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
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import GUID, Base, TimestampMixin, UUIDPrimaryKeyMixin

Money = Numeric(14, 2)

# The STORED lifecycle states (INV-3). The payment status (open/partial/paid/
# overdue/credited) and delivery (sent/viewed) are DERIVED on top of `issued`
# and are never written here — `services.issued_lifecycle` owns the transition
# table and imports this tuple; the CHECK below enforces it in the database.
LIFECYCLES = ("draft", "approved", "issued", "disputed", "written_off", "cancelled")


class IssuedInvoice(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An invoice this org ISSUES to a customer (outbound / accounts-receivable).

    Distinct from the received invoices used for spend analytics. A snapshot of
    the seller is taken at issue time so a later profile edit never rewrites a
    finalized invoice. Numbering is sequential per issuer.
    """

    __tablename__ = "issued_invoices"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "partner_id"],
            ["partners.org_id", "partners.id"],
            name="fk_issued_invoices_partner",
        ),
        ForeignKeyConstraint(
            ["org_id", "customer_id"],
            ["customers.org_id", "customers.id"],
            name="fk_issued_invoices_customer_tenant",
        ),
        ForeignKeyConstraint(
            ["org_id", "corrected_invoice_id"],
            ["issued_invoices.org_id", "issued_invoices.id"],
            name="fk_issued_invoices_corrected_tenant",
        ),
        # Listing/ordering issued invoices by date within a tenant.
        Index("ix_issued_org_issue", "org_id", "issue_date"),
        UniqueConstraint("org_id", "create_command_key", name="uq_issued_create_command"),
        UniqueConstraint("org_id", "issue_command_key", name="uq_issued_issue_command"),
        # Target for the composite FK from the payment ledger (tenant-safe refs).
        UniqueConstraint("org_id", "id", name="uq_issued_invoices_org_id"),
        # Revenue-side project link (docs/design/project-profitability.md phase 1)
        # — the same composite tenant FK shape `invoices` and `expense_items`
        # already use for their COST-side links. SET NULL: deleting a project
        # must never take issued (legal) documents with it.
        ForeignKeyConstraint(
            ["org_id", "project_id"],
            ["projects.org_id", "projects.id"],
            name="fk_issued_invoices_project",
            ondelete="SET NULL",
        ),
        Index("ix_issued_org_project", "org_id", "project_id"),
        # Numbering backstop: even if two issue transactions somehow raced past the
        # profile row lock, the DB refuses a duplicate document number per tenant.
        UniqueConstraint("org_id", "number", name="uq_issued_invoices_org_number"),
        # Recurring cross-worker dedup: at most one invoice per (schedule, run date),
        # so two overlapping recurring workers can't double-generate an occurrence.
        UniqueConstraint(
            "org_id",
            "recurring_id",
            "recurring_period",
            name="uq_issued_invoices_recurring_period",
        ),
        # Dogfood subscription billing (H1.6 / WO-48): at most one invoice per
        # (subscriber tenant, billing period), so a re-run of the daily job can't
        # double-invoice a tenant for the same month.
        UniqueConstraint(
            "org_id",
            "subscription_org_id",
            "subscription_period",
            name="uq_issued_invoices_subscription_period",
        ),
        # DB-017: the issuer link is tenant-safe — `(org_id, issuer_id)` must
        # name an issuer of THIS org (issuer_profiles carries the composite
        # target `uq_issuer_profiles_org_id_id` for exactly this). SET NULL on
        # the issuer column only: Postgres carries `ON DELETE SET NULL
        # (issuer_id)` via the migration (a column list, PG ≥ 15), because a
        # plain composite SET NULL would null `org_id` too (DB-018). The route
        # refuses to delete an issuer that has invoices, so the action never
        # fires in practice; the constraint is the backstop.
        ForeignKeyConstraint(
            ["org_id", "issuer_id"],
            ["issuer_profiles.org_id", "issuer_profiles.id"],
            name="fk_issued_invoices_issuer",
            ondelete="SET NULL",
        ),
        # DB-011: the stored lifecycle is a closed set (LIFECYCLES).
        CheckConstraint(
            "lifecycle IN (" + ", ".join(f"'{s}'" for s in LIFECYCLES) + ")",
            name="ck_issued_invoices_lifecycle",
        ),
    )

    # Optional for legacy callers; keyed UI commands persist identity with the legal document.
    create_command_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    create_command_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    issue_command_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    issue_command_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    org_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # The project (won contract/job) this revenue belongs to. Nullable — not
    # every invoice is project work — and the P&L is only computable when set.
    project_id: Mapped[str | None] = mapped_column(GUID(), nullable=True)
    # Optional link to a Partner (counterparty). When set, the partner's
    # pre-invoicing workflow gates issuance. `kind` distinguishes a normal
    # invoice from a penalty (late-interest) invoice.
    partner_id: Mapped[str | None] = mapped_column(
        GUID(), ForeignKey("partners.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Optional link to the sales Customer master this was billed to (the buyer
    # block is still snapshotted onto the invoice at issue time).
    customer_id: Mapped[str | None] = mapped_column(
        GUID(), ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # The issuer legal entity this invoice was numbered from and snapshots as the
    # seller (multi-issuer: each entity owns its own gap-free series). NULL for
    # legacy rows created before the registry — they used the org's default issuer.
    # The FK is the composite `fk_issued_invoices_issuer` in __table_args__ (DB-017).
    issuer_id: Mapped[str | None] = mapped_column(GUID(), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(
        String(12), default="standard", nullable=False
    )  # standard | penalty

    # Document type: a normal receivable invoice, or a CREDIT NOTE that corrects
    # (reduces) one. A credit note carries its own gap-free number series, links
    # to the invoice it corrects, and REDUCES that invoice's outstanding balance
    # (applied via `credited_total` on the corrected invoice) and the tenant's
    # reported turnover. Both document types are immutable once created.
    doc_type: Mapped[str] = mapped_column(
        String(12), default="invoice", nullable=False
    )  # invoice | credit_note
    corrected_invoice_id: Mapped[str | None] = mapped_column(
        GUID(), ForeignKey("issued_invoices.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Art. 219 (WO-K): the corrected invoice's NUMBER, snapshotted at credit-note
    # creation so the "unambiguous reference to the initial invoice" survives on
    # the document itself (PDF + CII BT-25) even if the row link is ever severed
    # (the FK above is SET NULL on delete). Set only on doc_type='credit_note'.
    corrected_invoice_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Recurring provenance (Phase INV-1): when generated from a schedule, the
    # schedule id + the occurrence's run date. The unique (org_id, recurring_id,
    # recurring_period) constraint makes generation cross-worker idempotent.
    recurring_id: Mapped[str | None] = mapped_column(GUID(), nullable=True, index=True)
    recurring_period: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Dogfood subscription billing (H1.6 / WO-48): set ONLY on an invoice issued
    # from the platform's own org (`settings.platform_org_id`) billing another
    # tenant for its InvoiceIQ subscription. `subscription_org_id` identifies
    # WHICH tenant (a plain reference, like `customer_id`/`partner_id` — not a
    # second tenant-scoping dimension: RLS still keys strictly on `org_id`, the
    # platform org's own id); `subscription_period` is the first-of-month the
    # invoice covers. NULL on every ordinary invoice.
    subscription_org_id: Mapped[str | None] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    subscription_period: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Sum of credit notes applied AGAINST this invoice (only meaningful on an
    # invoice). Effective amount owed = total − credited_total − amount_paid.
    credited_total: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)

    # Explicit workflow lifecycle (INV-3). The AR/payment status (open/partial/
    # paid/overdue/credited) and delivery (sent/viewed) are DERIVED on top of the
    # `issued` lifecycle; draft/approved/disputed/written_off/cancelled are stored.
    # Default `issued` = the historical "born final" behaviour (unchanged).
    lifecycle: Mapped[str] = mapped_column(
        String(16), default="issued", server_default="issued", nullable=False, index=True
    )
    # NULL until issued — a draft carries no gap-free number.
    number: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    issue_date: Mapped[date] = mapped_column(Date, nullable=False)
    supply_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="EUR", nullable=False)

    # Buyer (Art. 226)
    buyer_name: Mapped[str] = mapped_column(String(200), nullable=False)
    buyer_email: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )  # for delivery/reminders
    buyer_vat_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    buyer_address_line1: Mapped[str | None] = mapped_column(String(200), nullable=True)
    buyer_city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    buyer_postal_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    buyer_country: Mapped[str | None] = mapped_column(String(2), nullable=True)

    # Seller snapshot (frozen at issue)
    seller_json: Mapped[str] = mapped_column(Text, nullable=False)

    # VAT treatment: 'standard' | 'reverse_charge' | 'intra_eu' | 'exempt'
    vat_scheme: Mapped[str] = mapped_column(String(20), default="standard", nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Buyer's purchase-order reference (EN-16931 BT-13). Free text a buyer quotes
    # so they can match the invoice to their PO.
    po_reference: Mapped[str | None] = mapped_column(String(60), nullable=True)
    # VAT-exemption reason text (EN-16931 BT-120): the legal ground when no VAT is
    # charged (reverse-charge / intra-EU / exempt). Defaults from the scheme note.
    tax_exemption_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)

    subtotal: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    tax_total: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    total: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)

    # Accounts-receivable settlement. Payment status is DERIVED (never stored):
    # paid when amount_paid >= total, partial when 0 < amount_paid < total,
    # overdue when still owed past due_date, otherwise open. See issued_reports.
    amount_paid: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    paid_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    # Lifecycle (Phase 10): delivery + cancellation. `sent_at` records the first
    # time the invoice PDF was emailed to the buyer; `voided_at`/`void_reason`
    # cancel an unpaid invoice (a voided invoice reads as status VOID and refuses
    # payment / credit-note / send).
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    viewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    void_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # Dispute / bad-debt write-off (both from the `issued` lifecycle).
    disputed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dispute_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    written_off_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    writeoff_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Late-payment interest. Only invoices that CARRY a rate accrue a penalty; the
    # accrued figure is advisory (computed, never added to `total`). See issued_status.
    penalty_rate: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 2), nullable=True
    )  # % per annum
    # The date through which interest on this invoice has already been BILLED on a
    # penalty invoice. NULL means none has been. Accrual for billing purposes runs
    # from max(due_date, this) — without it every penalty invoice re-bills from the
    # due date and a debtor billed monthly pays month 1 again in month 2.
    interest_billed_through: Mapped[date | None] = mapped_column(Date, nullable=True)

    # Dunning: how many reminders sent and when the last went out.
    reminder_count: Mapped[int] = mapped_column(default=0, nullable=False)
    last_reminder_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Highest dunning-ladder level already fired for this invoice (Phase 16), so
    # each escalation level sends at most once. 0 = no reminder sent yet.
    dunning_level: Mapped[int] = mapped_column(default=0, server_default="0", nullable=False)

    lines: Mapped[list[IssuedInvoiceLine]] = relationship(
        back_populates="invoice",
        cascade="all, delete-orphan",
        order_by="IssuedInvoiceLine.position",
    )


class IssuedInvoiceLine(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "issued_invoice_lines"

    invoice_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("issued_invoices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(default=1, nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=Decimal("1"), nullable=False)
    unit: Mapped[str] = mapped_column(String(8), default="C62", nullable=False)  # UN/ECE unit code
    unit_price: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    # Per-line discount as a percentage of qty×unit_price (EN-16931 line allowance).
    # net_amount is stored POST-discount and is what the totals/VAT are built on.
    discount_percent: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), default=Decimal("0"), server_default="0", nullable=False
    )
    vat_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("0"), nullable=False)
    net_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    # Slice 4b: SNAPSHOT of the tax-code catalogue entry chosen at issue time (the
    # code drives `vat_rate` on creation). A label, not a live FK — an issued
    # invoice is immutable, so a later catalogue edit must not change it.
    tax_code: Mapped[str | None] = mapped_column(String(24), nullable=True)

    invoice: Mapped[IssuedInvoice] = relationship(back_populates="lines")


class IssuedInvoiceAttachment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A supporting document attached to an ISSUED invoice (a signed PO, a delivery
    note, a contract). The bytes live in object storage; this row holds the
    metadata + sha256 pointer. Tenant-scoped (own org_id + RLS)."""

    __tablename__ = "issued_invoice_attachments"

    # WO-M: the recycle bin (owner decision 2026-08-15). Stamped instead of
    # destroyed; hidden from every read by the ORM guard (SOFT_DELETE_MODELS).
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_by: Mapped[str | None] = mapped_column(String(320), nullable=True)
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "invoice_id"],
            ["issued_invoices.org_id", "issued_invoices.id"],
            name="fk_issued_attachments_invoice",
            ondelete="CASCADE",
        ),
        UniqueConstraint("org_id", "id", name="uq_issued_attachments_org_id"),
        UniqueConstraint("org_id", "command_key", name="uq_issued_attachment_command"),
        Index("ix_issued_attachments_invoice", "org_id", "invoice_id"),
    )

    org_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    invoice_id: Mapped[str] = mapped_column(GUID(), nullable=False)
    command_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    command_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime: Mapped[str | None] = mapped_column(String(120), nullable=True)
    size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    uploaded_by: Mapped[str | None] = mapped_column(GUID(), nullable=True)
    uploaded_by_email: Mapped[str | None] = mapped_column(String(200), nullable=True)
