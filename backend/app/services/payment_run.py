"""Supplier payment runs (Phase 14).

The AP counterpart of `services.reimbursement`: group scheduled-for-payment
supplier invoices into a payout run, mark the run paid together (which writes an AP
payment-ledger entry settling each invoice in full and advances it to `paid`), and
export it as a bank file. A paid run is a reconciliation debit target. Tenant-
scoped; pure DB + logic, no HTTP (the route maps errors to status codes).
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.csv_safety import sanitize_cell
from app.core.errors import AppError, ConflictError
from app.core.money import q2
from app.models.invoice import Invoice, InvoiceStatus, WorkflowState
from app.models.payment_run import RUN_APPROVED, RUN_CANCELLED, RUN_OPEN, RUN_PAID, PaymentRun
from app.models.vendor import VENDOR_PROVISIONAL, Vendor
from app.services import ap_payments, audit, invoice_workflow
from app.services import vendors as vendor_service


class PaymentRunError(Exception):
    """A payment-run precondition failed (bad state / not eligible)."""


# A bank file may only leave the building for a run a SECOND person has reviewed
# (approved) or that is already settled (paid). Exporting an open run would let
# the maker send their own unreviewed file to the bank — the exact gap WO-9 closes.
EXPORTABLE_STATUSES = (RUN_APPROVED, RUN_PAID)


def _sod_conflict(
    run: PaymentRun, actor_id: str | None, actor_email: str | None, *, include_approver: bool
) -> str | None:
    """The segregation-of-duties check (§4.8): which recorded role, if any, the
    acting user already holds on this run. Compares the immutable user id first
    and falls back to the stored email so LEGACY runs (created before the id
    columns existed, `created_by_id` NULL) are still covered — fail-closed: a
    run whose maker we can identify by either handle is never self-checked."""
    if (actor_id and run.created_by_id and actor_id == run.created_by_id) or (
        actor_email and run.created_by and actor_email == run.created_by
    ):
        return "creator"
    if include_approver and (
        (actor_id and run.approved_by_id and actor_id == run.approved_by_id)
        or (actor_email and run.approved_by and actor_email == run.approved_by)
    ):
        return "approver"
    return None


async def _enforce_sod(
    db: AsyncSession,
    run: PaymentRun,
    *,
    stage: str,
    actor_id: str | None,
    actor_email: str | None,
    is_platform_admin: bool,
    override_sod: bool,
    include_approver: bool,
) -> None:
    """Maker ≠ checker, enforced for EVERY role including the org Owner. The only
    exemption is the cross-tenant platform operator, and it is EXPLICIT (the
    request must carry `override_sod=true`) and AUDITED (`payment_run.sod_override`
    naming the overridden control) — never silent."""
    role = _sod_conflict(run, actor_id, actor_email, include_approver=include_approver)
    if role is None:
        return
    if is_platform_admin and override_sod:
        await audit.record(
            db,
            audit.A.AP_RUN_SOD_OVERRIDE,
            target_type="payment_run",
            target_id=run.id,
            meta={
                "control": "maker_is_checker",
                "stage": stage,
                "conflict_role": role,
                "actor": actor_email,
            },
        )
        return
    raise AppError(
        f"You are this run's {role}; a different user must {stage} it "
        "(maker–checker control on payment runs).",
        code="maker_is_checker",
        status=403,
    )


async def _assert_vendors_payable(
    db: AsyncSession,
    org_id: str,
    invoices: list[Invoice],
    *,
    allow_provisional: bool,
) -> None:
    """The WO-2 fraud gate on the payout path. Refuses (naming the vendor):

    - a vendor with a PENDING protected-field change request — paying while a
      bank-detail change is in flight would either pay an account someone is
      actively trying to replace, or race the approval;
    - a PROVISIONAL vendor (bank/tax identity captured at creation, never
      independently verified) unless the caller explicitly confirmed it.

    Fail-closed: this runs at run CREATION and again at PAY time, because a
    change request can be filed between the two."""
    vendor_ids = sorted({inv.vendor_id for inv in invoices if inv.vendor_id})
    if not vendor_ids:
        return
    vendors = {
        v.id: v
        for v in await db.scalars(
            select(Vendor).where(Vendor.org_id == org_id, Vendor.id.in_(vendor_ids))
        )
    }
    pending = await vendor_service.pending_requests_for(db, org_id, vendor_ids)
    for vid in vendor_ids:
        vendor = vendors.get(vid)
        if vendor is None:
            continue
        if pending.get(vid):
            fields = ", ".join(sorted({r.field for r in pending[vid]}))
            raise PaymentRunError(
                f"Vendor '{vendor.name}' has a pending bank-detail change request "
                f"({fields}); approve or reject it before paying this vendor."
            )
        if vendor.status == VENDOR_PROVISIONAL and not allow_provisional:
            raise PaymentRunError(
                f"Vendor '{vendor.name}' is provisional (its bank/tax identity was "
                "captured but never verified); confirm it explicitly to include it "
                "in a payment run."
            )


def eur_of(inv: Invoice) -> Decimal:
    """The invoice's EUR amount for run totals and the SEPA bank file.

    Fail-CLOSED (WO-8): an EUR-currency invoice with no stamped `total_eur` is
    the identity conversion (rate 1 by convention — not a guess); a FOREIGN
    invoice with no stamped `total_eur` is REFUSED. The old fallback silently
    used the raw foreign total as if it were EUR, and the SEPA file then
    instructed the bank to pay e.g. EUR 1,000 for a 1,000-PLN invoice. A
    blocked run is recoverable; a mis-currencied payment is not."""
    if inv.total_eur is not None:
        return q2(Decimal(inv.total_eur))
    if (inv.currency or "EUR").upper() == "EUR":
        return q2(Decimal(inv.total or 0))
    raise PaymentRunError(
        f"Invoice '{inv.invoice_number}' is in {inv.currency} with no recorded EUR "
        f"conversion (no rate was available for {inv.currency}); refresh the ECB "
        "rates and re-register the invoice before paying it."
    )


def eur_or_none(inv: Invoice) -> Decimal | None:
    """Best-effort EUR figure for REPORTING surfaces (the CSV export): None when
    the invoice has no reliable EUR value — the row still shows its original
    amount + currency, and the EUR column stays honestly blank."""
    try:
        return eur_of(inv)
    except PaymentRunError:
        return None


async def payable_invoices(db: AsyncSession, org_id: str) -> list[Invoice]:
    """Scheduled-for-payment invoices not yet in a run — the run's candidate pool."""
    return list(
        await db.scalars(
            select(Invoice)
            .where(
                Invoice.org_id == org_id,
                Invoice.workflow_state == WorkflowState.scheduled_for_payment,
                Invoice.payment_run_id.is_(None),
            )
            .options(selectinload(Invoice.vendor))
            .order_by(Invoice.due_date.asc())
        )
    )


async def runs_awaiting_check(db: AsyncSession, org_id: str, *, checker_id: str) -> int:
    """Open runs awaiting a second pair of eyes, EXCLUDING runs the checker made
    (maker≠checker, §4.8 — `_sod_conflict` would refuse their approval, so the
    dashboard must not count them toward "waiting on me"). A legacy run with no
    recorded `created_by_id` counts: the id-based exclusion cannot prove the
    caller made it, and the email-based SoD check still guards the action itself
    (fail toward showing work, never toward permitting it). Canonical read for
    the composed home dashboard (WO-16)."""
    return int(
        await db.scalar(
            select(func.count()).where(
                PaymentRun.org_id == org_id,
                PaymentRun.status == RUN_OPEN,
                or_(PaymentRun.created_by_id.is_(None), PaymentRun.created_by_id != checker_id),
            )
        )
        or 0
    )


async def run_invoices(db: AsyncSession, org_id: str, run_id: str) -> list[Invoice]:
    """Invoices linked to a run, ordered for a readable payout file."""
    return list(
        await db.scalars(
            select(Invoice)
            .where(Invoice.org_id == org_id, Invoice.payment_run_id == run_id)
            .options(selectinload(Invoice.vendor))
            .order_by(Invoice.invoice_number.asc())
        )
    )


async def create_run(
    db: AsyncSession,
    org_id: str,
    invoice_ids: list[str],
    *,
    method: str,
    note: str | None,
    created_by: str | None,
    created_by_id: str | None = None,
    confirm_provisional: bool = False,
) -> PaymentRun:
    """Group scheduled-for-payment, un-run invoices into an OPEN run. Raises
    PaymentRunError if any invoice is missing, not scheduled for payment, or already
    in a run — or (WO-2) if a linked vendor has a pending bank-detail change, or is
    provisional and `confirm_provisional` was not set. Flushes; caller commits."""
    if not invoice_ids:
        raise PaymentRunError("Select at least one scheduled invoice.")
    invoices = list(
        await db.scalars(
            select(Invoice).where(Invoice.org_id == org_id, Invoice.id.in_(invoice_ids))
        )
    )
    found = {i.id for i in invoices}
    missing = [iid for iid in invoice_ids if iid not in found]
    if missing:
        raise PaymentRunError(f"Invoice(s) not found: {', '.join(missing)}")
    for inv in invoices:
        if inv.workflow_state != WorkflowState.scheduled_for_payment:
            raise PaymentRunError(
                f"Invoice '{inv.invoice_number}' is {inv.workflow_state.value}; "
                "only a scheduled-for-payment invoice can be added to a run."
            )
        if inv.payment_run_id:
            raise PaymentRunError(f"Invoice '{inv.invoice_number}' is already in a run.")
    await _assert_vendors_payable(db, org_id, invoices, allow_provisional=confirm_provisional)

    run = PaymentRun(
        org_id=org_id,
        method=method,
        note=note,
        created_by=created_by,
        created_by_id=created_by_id,
        status=RUN_OPEN,
        total_eur=q2(sum((eur_of(i) for i in invoices), Decimal("0"))),
    )
    db.add(run)
    await db.flush()
    for inv in invoices:
        inv.payment_run_id = run.id
    return run


async def approve_run(
    db: AsyncSession,
    org_id: str,
    run: PaymentRun,
    *,
    actor_id: str | None,
    actor_email: str | None,
    is_platform_admin: bool = False,
    override_sod: bool = False,
) -> None:
    """The checker's step (WO-9): a SECOND person reviews an open run and marks it
    approved — only then can it be paid or exported as a bank file. The creator can
    never approve their own run (maker ≠ checker, §4.8); the explicit, audited
    platform-admin override is the sole exemption. Flushes nothing new; caller
    commits."""
    if run.status != RUN_OPEN:
        raise PaymentRunError(f"Run is {run.status}; only an open run can be approved.")
    await _enforce_sod(
        db,
        run,
        stage="approve",
        actor_id=actor_id,
        actor_email=actor_email,
        is_platform_admin=is_platform_admin,
        override_sod=override_sod,
        include_approver=False,
    )
    run.approved_by = actor_email
    run.approved_by_id = actor_id
    run.approved_at = datetime.now(UTC)
    run.status = RUN_APPROVED
    run.version += 1


async def mark_paid(
    db: AsyncSession,
    org_id: str,
    run: PaymentRun,
    *,
    reference: str | None,
    method: str | None = None,
    actor_id: str | None = None,
    actor_email: str | None = None,
    is_platform_admin: bool = False,
    override_sod: bool = False,
) -> list[Invoice]:
    """Mark an approved run paid: stamp it, then settle every linked invoice IN FULL
    via the AP payment ledger (stamped with the run id) and advance it to `paid`.
    Returns the affected invoices. Segregation of duties (WO-9): neither the run's
    creator nor its approver may be the payer — a single account must never carry a
    payment from selection to settlement on its own."""
    if run.status != RUN_APPROVED:
        raise PaymentRunError(
            f"Run is {run.status}; only an approved run can be paid"
            + (" (a second user must approve it first)." if run.status == RUN_OPEN else ".")
        )
    await _enforce_sod(
        db,
        run,
        stage="pay",
        actor_id=actor_id,
        actor_email=actor_email,
        is_platform_admin=is_platform_admin,
        override_sod=override_sod,
        include_approver=True,
    )
    invoices = await run_invoices(db, org_id, run.id)
    if not invoices:
        raise PaymentRunError("Run has no invoices to pay.")
    # Re-check at pay time: a bank-detail change request filed AFTER the run was
    # created must still block the payout (provisional vendors were confirmed —
    # or refused — at creation, so only the pending-change gate re-runs here).
    await _assert_vendors_payable(db, org_id, invoices, allow_provisional=True)
    now = datetime.now(UTC)
    if method:
        run.method = method
    run.reference = reference
    run.status = RUN_PAID
    run.paid_at = now
    run.total_eur = q2(sum((eur_of(i) for i in invoices), Decimal("0")))
    run.version += 1
    for inv in invoices:
        await ap_payments.set_cumulative(
            db,
            org_id,
            inv,
            new_total=q2(Decimal(inv.total)),
            paid_date=now.date(),
            method=run.method,
            reference=reference,
            run_id=run.id,
        )
        # scheduled_for_payment → paid is a legal transition; sync the aging status.
        invoice_workflow.assert_transition(inv.workflow_state, WorkflowState.paid)
        inv.workflow_state = WorkflowState.paid
        inv.status = InvoiceStatus.paid
        inv.version = (inv.version or 0) + 1
    return invoices


async def cancel_run(db: AsyncSession, org_id: str, run: PaymentRun) -> list[Invoice]:
    """Cancel an open or approved run: unlink its invoices (they return to the
    scheduled pool). Paid runs and runs with recorded bank exports cannot be
    cancelled: a file may already be with the bank."""
    if run.status not in (RUN_OPEN, RUN_APPROVED):
        raise PaymentRunError(
            f"Run is {run.status}; only an open or approved run can be cancelled."
        )
    if run.export_count != 0 or run.exported_at is not None or run.last_msg_id is not None:
        raise ConflictError(
            "Run export records prevent cancellation. Check the bank file’s status "
            "with your bank or accountant before arranging another payment.",
            code="run_exported_cannot_cancel",
        )
    invoices = await run_invoices(db, org_id, run.id)
    for inv in invoices:
        inv.payment_run_id = None
    run.status = RUN_CANCELLED
    run.version += 1
    return invoices


def assert_exportable(run: PaymentRun, *, confirm_reexport: bool) -> None:
    """The WO-9 export gate. Fail-closed on both checks:

    - STATE: only an approved/paid run may produce a bank file — an open run has
      not been reviewed by a second person, and a cancelled run must never reach
      the bank at all.
    - EXPORT-ONCE: a run that already produced a file needs an EXPLICIT
      `confirm_reexport` — a silent second file risks a double payment at the
      bank, and the refusal tells the caller when the first one was produced."""
    if run.status not in EXPORTABLE_STATUSES:
        raise ConflictError(
            f"Run is {run.status}; only an approved or paid run can be exported as a bank file.",
            code="run_not_exportable",
        )
    if run.export_count and not confirm_reexport:
        first = run.exported_at.isoformat() if run.exported_at else "an earlier deploy"
        raise ConflictError(
            f"This run was already exported (count {run.export_count}, first at {first}). "
            "Re-exporting produces a NEW bank file with a new message id — pass "
            "confirm_reexport=true to proceed.",
            code="already_exported",
        )


def record_export(run: PaymentRun, *, msg_id: str | None) -> None:
    """Stamp a SUCCESSFUL export: `exported_at` keeps the FIRST export's time (the
    409 names it), the counter feeds the next MsgId generation, and the last
    pain.001 MsgId stays queryable (each one is also audited)."""
    if run.exported_at is None:
        run.exported_at = datetime.now(UTC)
    run.export_count += 1
    if msg_id:
        run.last_msg_id = msg_id


def export_csv(run: PaymentRun, invoices: list[Invoice]) -> str:
    """A bank-friendly CSV of the run's payments (one row per invoice).

    `run.reference` (operator-typed) and `inv.invoice_number` (can originate
    from a vendor-supplied PDF a human transcribes) are free text a human
    reads straight into Excel — sanitized against CSV/Excel formula
    injection (board R1, CWE-1236). The amount/currency/method columns are
    never sanitized: they are formatted numbers/codes, not free text, and a
    legitimate negative amount must round-trip untouched.
    """
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["run_reference", "invoice_number", "amount", "currency", "amount_eur", "method"])
    for inv in invoices:
        eur = eur_or_none(inv)
        w.writerow(
            [
                sanitize_cell(run.reference or run.id),
                sanitize_cell(inv.invoice_number),
                f"{q2(Decimal(inv.total or 0))}",
                inv.currency,
                f"{eur}" if eur is not None else "",
                run.method,
            ]
        )
    return buf.getvalue()
