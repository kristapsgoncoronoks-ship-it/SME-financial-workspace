"""Expense reimbursement — payout batches (Phase 09).

Turns the one-way "reimbursed" status flip into a real payout: group APPROVED
expense reports into a batch, mark the batch paid together with a shared method +
reference (which flips each report to `reimbursed` and stamps its payment
metadata), and export the batch as a bank/payroll file. Tenant-scoped; pure DB +
logic, no HTTP (the route maps validation errors to status codes).
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import tenant
from app.core.csv_safety import sanitize_cell
from app.core.errors import AppError, ConflictError, ValidationError
from app.core.money import q2
from app.models.expense import (
    BATCH_CANCELLED,
    BATCH_OPEN,
    BATCH_PAID,
    ExpenseReport,
    ReimbursementBatch,
)
from app.services import audit


class ReimbursementError(Exception):
    """A reimbursement precondition failed (bad state / not eligible)."""


def _sod_conflict(
    batch: ReimbursementBatch, actor_id: str | None, actor_email: str | None
) -> str | None:
    """The segregation-of-duties check (§4.8, board R6): is the acting user the
    batch's creator? Unlike a payment run, a reimbursement batch has no separate
    approval stage (each underlying expense report was individually approved
    under its own SoD before batching — see `assert_exportable`), so the only
    conflicting role here is "creator". Compares the immutable user id first and
    falls back to the stored email so LEGACY batches (created before
    `created_by_id` existed, NULL) are still covered — fail-closed: a batch
    whose maker we can identify by either handle is never self-paid."""
    if (actor_id and batch.created_by_id and actor_id == batch.created_by_id) or (
        actor_email and batch.created_by and actor_email == batch.created_by
    ):
        return "creator"
    return None


async def _enforce_sod(
    db: AsyncSession,
    batch: ReimbursementBatch,
    *,
    actor_id: str | None,
    actor_email: str | None,
    is_platform_admin: bool,
    override_sod: bool,
) -> None:
    """Maker ≠ checker, enforced for EVERY role including the org Owner. The only
    exemption is the cross-tenant platform operator, and it is EXPLICIT (the
    request must carry `override_sod=true`) and AUDITED
    (`reimbursement.sod_override` naming the overridden control) — never silent.
    Mirrors `payment_run._enforce_sod`."""
    role = _sod_conflict(batch, actor_id, actor_email)
    if role is None:
        return
    if is_platform_admin and override_sod:
        event = await audit.record(
            db,
            audit.A.REIMBURSE_SOD_OVERRIDE,
            target_type="reimbursement_batch",
            target_id=batch.id,
            meta={
                "control": "maker_is_checker",
                "stage": "pay",
                "conflict_role": role,
                "actor": actor_email,
            },
        )
        if event is None:
            raise AppError(
                "The maker-checker override audit could not be recorded. No change was committed.",
                code="expense_audit_unavailable",
                status=503,
            )
        return
    raise AppError(
        f"You are this batch's {role}; a different user must pay it "
        "(maker–checker control on reimbursement payouts).",
        code="maker_is_checker",
        status=403,
    )


def assert_exportable(batch: ReimbursementBatch, *, confirm_reexport: bool) -> None:
    """WO-9 export gate for the employee payout rail. Unlike a payment run, a
    batch has NO separate approval stage — every report in it was individually
    approved under its own segregation of duties (a claimant can never approve
    their own report), so an OPEN batch may export; only a CANCELLED batch must
    never reach the bank. Export-once still applies: a second file needs the
    explicit `confirm_reexport` because a silent duplicate risks a double payout."""
    if batch.status == BATCH_CANCELLED:
        raise ConflictError(
            "Batch is cancelled; a cancelled batch cannot be exported as a bank file.",
            code="batch_not_exportable",
        )
    if batch.export_count and not confirm_reexport:
        first = batch.exported_at.isoformat() if batch.exported_at else "an earlier deploy"
        raise ConflictError(
            f"This batch was already exported (count {batch.export_count}, first at {first}). "
            "Re-exporting produces a NEW bank file with a new message id — pass "
            "confirm_reexport=true to proceed.",
            code="already_exported",
        )


def record_export(batch: ReimbursementBatch, *, msg_id: str | None) -> None:
    """Stamp a SUCCESSFUL export (mirror of `payment_run.record_export`):
    `exported_at` keeps the FIRST export's time, the counter feeds the next MsgId
    generation, and the last pain.001 MsgId stays queryable (each is audited)."""
    if batch.exported_at is None:
        batch.exported_at = datetime.now(UTC)
    batch.export_count += 1
    if msg_id:
        batch.last_msg_id = msg_id


def eur_of(r: ExpenseReport) -> Decimal:
    """The report's EUR amount for batch totals and the SEPA payout file.

    Fail-CLOSED (WO-8): an EUR-currency report with no stamped `total_eur` is
    the identity conversion (rate 1 by convention — not a guess); a FOREIGN
    report with no stamped `total_eur` is REFUSED. The old fallback silently
    treated the raw foreign total as EUR and the SEPA file paid that figure.
    A blocked batch is recoverable; a mis-currencied payout is not."""
    if r.total_eur is not None:
        return q2(Decimal(r.total_eur))
    if (r.currency or "EUR").upper() == "EUR":
        return q2(Decimal(r.total or 0))
    raise ReimbursementError(
        f"Report '{r.title}' is in {r.currency} with no recorded EUR conversion "
        f"(no rate was available for {r.currency}); refresh the ECB rates and "
        "re-submit the report before paying it."
    )


def eur_or_none(r: ExpenseReport) -> Decimal | None:
    """Best-effort EUR figure for REPORTING surfaces (the CSV export): None when
    the report has no reliable EUR value — the row still shows its original
    amount + currency, and the EUR column stays honestly blank."""
    try:
        return eur_of(r)
    except ReimbursementError:
        return None


async def batch_reports(db: AsyncSession, org_id: str, batch_id: str) -> list[ExpenseReport]:
    """Reports linked to a batch, ordered by employee for a readable payout file."""
    return list(
        await db.scalars(
            select(ExpenseReport)
            .where(ExpenseReport.org_id == org_id, ExpenseReport.payout_batch_id == batch_id)
            .order_by(ExpenseReport.employee_name.asc())
        )
    )


async def create_batch(
    db: AsyncSession,
    org_id: str,
    report_ids: list[str],
    *,
    method: str,
    note: str | None,
    created_by: str | None,
    created_by_id: str | None = None,
    report_versions: dict[str, int] | None = None,
) -> ReimbursementBatch:
    """Group approved, un-batched reports into an OPEN payout batch. Raises
    ReimbursementError if any report is missing, not approved, or already batched.
    Flushes; caller commits."""
    if not report_ids:
        raise ReimbursementError("Select at least one approved report.")
    reports = await _locked_reports(db, org_id, report_ids)
    found = {r.id for r in reports}
    missing = [rid for rid in report_ids if rid not in found]
    if report_versions and (
        missing
        or any(
            report_versions.get(r.id) != r.version
            or r.status not in ("approved", "marked_for_reimbursement")
            or r.payout_batch_id is not None
            for r in reports
        )
    ):
        raise ConflictError(
            "A selected report changed or entered a payout batch. Refresh the eligible "
            "reports and review your selection.",
            code="reimbursement_selection_conflict",
        )
    if missing:
        raise ReimbursementError("One or more selected reports were not found.")
    for r in reports:
        if r.status not in ("approved", "marked_for_reimbursement"):
            raise ReimbursementError(
                f"Report '{r.title}' is {r.status}; only approved reports can be paid."
            )
        if r.payout_batch_id is not None:
            raise ReimbursementError(f"Report '{r.title}' is already in a payout batch.")

    total_eur = q2(sum((eur_of(r) for r in reports), Decimal("0")))
    if total_eur > Decimal("999999999999.99"):
        raise ValidationError(
            "This batch exceeds the supported money range. Select fewer reports.",
            code="reimbursement_total_out_of_range",
        )
    batch = ReimbursementBatch(
        org_id=org_id,
        method=method,
        note=note,
        created_by=created_by,
        created_by_id=created_by_id,
        status=BATCH_OPEN,
        total_eur=total_eur,
    )
    db.add(batch)
    await db.flush()
    for r in reports:
        r.payout_batch_id = batch.id
        r.version += 1
    return batch


async def _locked_reports(
    db: AsyncSession, org_id: str, report_ids: list[str]
) -> list[ExpenseReport]:
    """Re-read the exact selection after deterministic PostgreSQL row locking.

    Two actors may hold different membership locks; their overlapping reports
    are the shared concurrency grain. Lock all selected rows before checking or
    allocating a batch, and refresh any ORM instances loaded before waiting.
    """
    return list(
        await db.scalars(
            select(ExpenseReport)
            .where(
                ExpenseReport.org_id == org_id,
                ExpenseReport.id.in_(report_ids),
            )
            .order_by(ExpenseReport.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )


async def mark_paid(
    db: AsyncSession,
    org_id: str,
    batch: ReimbursementBatch,
    *,
    reference: str | None,
    method: str | None = None,
    actor_id: str | None = None,
    actor_email: str | None = None,
    is_platform_admin: bool = False,
    override_sod: bool = False,
    expected_total_eur: Decimal | None = None,
    report_versions: dict[str, int] | None = None,
) -> list[ExpenseReport]:
    """Mark an open batch paid: stamp it, then flip every linked report to
    `reimbursed` with the payment metadata. Returns the affected reports.
    Segregation of duties (board R6): the batch's creator may never also be its
    payer — a single account must never carry a reimbursement payout from
    selection to settlement on its own, mirroring `payment_run.mark_paid`."""
    if batch.status != BATCH_OPEN:
        raise ReimbursementError(
            f"Batch is {batch.status}; only an open batch can be recorded paid."
        )
    # Acquire financial rows before the override audit's tenant advisory lock.
    # Expense decisions use report -> audit, so reversing this order here can
    # create a report/audit cycle. Refresh rows after any actual lock wait.
    reports = await _locked_batch_reports(db, org_id, batch.id)
    if not reports:
        raise ConflictError(
            "Batch has no reports to record paid.", code="reimbursement_selection_conflict"
        )
    if (report_versions and set(report_versions) != {row.id for row in reports}) or any(
        row.status not in ("approved", "marked_for_reimbursement")
        or row.reimbursed_at is not None
        or row.payment_reference is not None
        or row.payment_method is not None
        or (report_versions is not None and report_versions.get(row.id) != row.version)
        for row in reports
    ):
        raise ConflictError(
            "A linked report changed or already has payment evidence. Refresh the batch "
            "and review its reports before recording payment.",
            code="reimbursement_selection_conflict",
        )
    try:
        locked_total = q2(sum((eur_of(row) for row in reports), Decimal("0")))
    except ReimbursementError as exc:
        raise ConflictError(str(exc), code="reimbursement_total_conflict") from exc
    if (
        not locked_total.is_finite()
        or locked_total < 0
        or locked_total > Decimal("999999999999.99")
        or locked_total != batch.total_eur
        or (expected_total_eur is not None and locked_total != expected_total_eur)
    ):
        raise ConflictError(
            "The linked reports do not match the recorded or reviewed batch total. "
            "Review the batch evidence; no payment was recorded.",
            code="reimbursement_total_conflict",
        )
    await _enforce_sod(
        db,
        batch,
        actor_id=actor_id,
        actor_email=actor_email,
        is_platform_admin=is_platform_admin,
        override_sod=override_sod,
    )
    now = datetime.now(UTC)
    if method:
        batch.method = method
    batch.reference = reference
    batch.status = BATCH_PAID
    batch.paid_at = now
    batch.version += 1
    for r in reports:
        r.status = "reimbursed"
        r.reimbursed_at = now
        r.payment_method = batch.method
        r.payment_reference = reference
        r.version += 1
    return reports


async def _locked_batch_reports(
    db: AsyncSession, org_id: str, batch_id: str
) -> list[ExpenseReport]:
    """Production report-lock seam for an already locked payout batch."""
    return list(
        await db.scalars(
            select(ExpenseReport)
            .where(ExpenseReport.org_id == org_id, ExpenseReport.payout_batch_id == batch_id)
            .order_by(ExpenseReport.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )


async def cancel_batch(
    db: AsyncSession,
    org_id: str,
    batch: ReimbursementBatch,
    *,
    report_versions: dict[str, int] | None = None,
) -> list[ExpenseReport]:
    """Cancel an open batch with no recorded export, releasing its reports.

    The caller holds the batch lock shared with export/pay. An exported bank
    instruction may already be in use externally, so its reports must not be
    released for another batch. Retain partial legacy export markers as well.
    """
    if batch.status != BATCH_OPEN:
        raise ReimbursementError(f"Batch is {batch.status}; only an open batch can be cancelled.")
    if batch.export_count != 0 or batch.exported_at is not None or batch.last_msg_id is not None:
        raise ConflictError(
            "Batch export records prevent cancellation. Check the bank file's status "
            "with your bank or accountant before arranging another payment.",
            code="batch_exported_cannot_cancel",
        )
    if batch.paid_at is not None:
        raise ConflictError(
            "Batch has contradictory payment evidence. Review it with your accountant "
            "before releasing its reports.",
            code="reimbursement_cancellation_conflict",
        )
    # Batch -> ordered reports -> audit matches pay and expense decisions. Read
    # current row values after actual lock waits, before unlinking any report.
    with tenant.include_deleted():
        reports = await _locked_batch_reports(db, org_id, batch.id)
    if (report_versions is not None and set(report_versions) != {r.id for r in reports}) or any(
        r.deleted_at is not None
        or r.status not in ("approved", "marked_for_reimbursement")
        or r.reimbursed_at is not None
        or r.payment_reference is not None
        or r.payment_method is not None
        or (report_versions is not None and report_versions.get(r.id) != r.version)
        for r in reports
    ):
        raise ConflictError(
            "A linked report changed or has payment evidence. Refresh the batch and "
            "review its reports before cancelling.",
            code="reimbursement_selection_conflict",
        )
    for r in reports:
        r.payout_batch_id = None
        r.version += 1
    batch.status = BATCH_CANCELLED
    batch.version += 1
    return reports


async def batch_sepa(
    db: AsyncSession,
    org_id: str,
    batch: ReimbursementBatch,
    *,
    msg_id: str,
    acknowledge_skipped: bool = False,
) -> tuple[str, list[str]]:
    """Render a reimbursement batch as an ISO 20022 pain.001 SEPA credit-transfer
    file — debtor = our issuer profile, one creditor per employee (those with an
    IBAN on file). Returns (xml, skipped) where `skipped` NAMES the employees
    excluded for a missing IBAN; if any exist the export is refused (409
    `skipped_payees`) unless `acknowledge_skipped` is set — an employee silently
    dropped from the payout file is an unpaid person nobody was told about (WO-9).
    Reuses the AP `sepa.build_pain001` primitive so both payout rails share one
    renderer, under a caller-supplied per-generation `msg_id`."""
    from datetime import UTC, datetime

    from app.models.issuer import IssuerProfile
    from app.models.user import User
    from app.services import sepa

    reports = await batch_reports(db, org_id, batch.id)
    if not reports:
        raise ReimbursementError("Batch has no reports to pay.")

    # Load each payee's bank details once (employee_id → User). B1.5: payees are
    # resolved through MEMBERSHIPS — an employee currently switched into another
    # org must still be paid by this org, so filtering by `users.org_id` (the
    # active-org pointer) would silently drop their IBAN from the payout file.
    from sqlalchemy import and_ as _and

    from app.models.membership import Membership

    emp_ids = {r.employee_id for r in reports}
    users = {
        u.id: u
        for u in await db.scalars(
            select(User)
            .join(Membership, _and(Membership.user_id == User.id, Membership.org_id == org_id))
            .where(User.id.in_(emp_ids))
        )
    }

    transfers: list[sepa.CreditTransfer] = []
    skipped: list[str] = []
    for r in reports:
        u = users.get(r.employee_id)
        iban = (u.iban if u else None) or None
        if not iban:
            skipped.append(r.employee_name)
            continue
        transfers.append(
            sepa.CreditTransfer(
                end_to_end=r.id[:35],
                amount=eur_of(r),
                creditor_name=r.employee_name,
                creditor_iban=iban,
                creditor_bic=u.bic if u else None,
                remittance=f"Expense reimbursement {r.title}"[:140],
            )
        )

    # Total absence of payable employees stays the historical 422 (build_pain001
    # below); a PARTIAL drop is the silent-skip hazard needing acknowledgement.
    if transfers:
        sepa.assert_skipped_acknowledged(skipped, acknowledge_skipped)
    issuer = await db.scalar(select(IssuerProfile).where(IssuerProfile.org_id == org_id))
    debtor_name = (issuer.legal_name if issuer and issuer.legal_name else None) or "Our company"
    xml = sepa.build_pain001(
        msg_id=msg_id,
        created_at=batch.created_at or datetime.now(UTC),
        exec_date=(
            batch.paid_at.date()
            if batch.paid_at
            else (batch.created_at or datetime.now(UTC)).date()
        ),
        debtor_name=debtor_name,
        debtor_iban=issuer.iban if issuer else None,
        debtor_bic=issuer.bic if issuer else None,
        transfers=transfers,
    )
    return xml, skipped


def export_csv(batch: ReimbursementBatch, reports: list[ExpenseReport]) -> str:
    """A bank/payroll-friendly CSV of the batch's payouts (one row per report).

    `r.employee_name` and `r.title` are free text (an employee's account
    name, a self-typed report title) reaching a payroll-adjacent CSV a human
    opens straight in Excel — sanitized against CSV/Excel formula injection
    (board R1, CWE-1236). The amount/currency/method columns are never
    sanitized: they are formatted numbers/codes, not free text, and a
    legitimate negative amount must round-trip untouched.
    """
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        ["batch_reference", "employee", "report", "amount", "currency", "amount_eur", "method"]
    )
    for r in reports:
        eur = eur_or_none(r)
        w.writerow(
            [
                sanitize_cell(batch.reference or batch.id),
                sanitize_cell(r.employee_name),
                sanitize_cell(r.title),
                f"{q2(Decimal(r.total or 0))}",
                r.currency,
                f"{eur}" if eur is not None else "",
                batch.method,
            ]
        )
    return buf.getvalue()
