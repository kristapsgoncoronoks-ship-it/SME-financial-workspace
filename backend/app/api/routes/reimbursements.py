"""Expense reimbursement payout routes (Phase 09).

A designated approver (EXPENSE_APPROVE) groups APPROVED expense reports into a
payout batch, marks it paid (which flips each report to `reimbursed` with the
payment method + reference), and exports it as a bank/payroll CSV. Gated behind
the expenses module; changes are audited. Cancellation has no external event.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, require_perm
from app.api.routes.expenses import _guard
from app.core import authz
from app.core.errors import ConflictError, ValidationError
from app.core.security_headers import content_disposition
from app.models.expense import ReimbursementBatch
from app.schemas.expense import ExpenseCommandOut
from app.schemas.reimbursement import (
    BatchCancel,
    BatchCancelOut,
    BatchCreate,
    BatchDetailOut,
    BatchOut,
    BatchPage,
    BatchPay,
    EligibleReportPage,
)
from app.services import audit, reimbursement, reimbursement_commands, sepa

# Structural authorization (ADR-0024): every reimbursement-payout route is an
# approver/finance action — router-level EXPENSE_APPROVE.
router = APIRouter(
    prefix="/reimbursements",
    tags=["reimbursements"],
    dependencies=[Depends(require_perm(authz.Permission.EXPENSE_APPROVE))],
)


async def _load(
    db: DbSession, org_id: str, batch_id: str, *, lock: bool = False
) -> ReimbursementBatch:
    stmt = select(ReimbursementBatch).where(
        ReimbursementBatch.id == batch_id, ReimbursementBatch.org_id == org_id
    )
    if lock:
        # Serialize state-changing operations (pay/cancel) so the version check +
        # settlement are atomic and can't double-write the ledger. SQLite ignores
        # FOR UPDATE (writes serialize); Postgres takes the row lock.
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    b = await db.scalar(stmt)
    if b is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Reimbursement batch not found")
    return b


async def _detail(db: DbSession, org_id: str, b: ReimbursementBatch) -> BatchDetailOut:
    return await reimbursement_commands.batch_detail(db, org_id, b)


@router.get("", response_model=list[BatchOut])
async def list_batches(
    current: CurrentUser,
    db: DbSession,
    response: Response,
    page: int | None = Query(default=None, ge=1),
    page_size: int = Query(default=100, ge=1, le=100),
):
    """Compatibility list shape; callers can page or use the explicit /page envelope."""
    await _guard(db, current.org_id)
    result = await reimbursement_commands.batch_page(
        db, current.org_id, page=page or 1, page_size=page_size
    )
    if page is None and result.total > page_size:
        raise ValidationError(
            "There are more reimbursement batches than this response can include. "
            "Request an explicit page or use /reimbursements/page and follow its total.",
            code="reimbursement_pagination_required",
        )
    response.headers["X-Total-Count"] = str(result.total)
    return result.items


@router.get("/eligible", response_model=EligibleReportPage)
async def eligible_reports(
    current: CurrentUser,
    db: DbSession,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=100),
):
    await _guard(db, current.org_id)
    return await reimbursement_commands.eligible_page(
        db, current.org_id, page=page, page_size=page_size
    )


@router.get("/page", response_model=BatchPage)
async def paged_batches(
    current: CurrentUser,
    db: DbSession,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=100),
):
    await _guard(db, current.org_id)
    return await reimbursement_commands.batch_page(
        db, current.org_id, page=page, page_size=page_size
    )


@router.get("/commands/{command_key}", response_model=ExpenseCommandOut)
async def create_command_status(command_key: str, current: CurrentUser, db: DbSession):
    await _guard(db, current.org_id)
    return await reimbursement_commands.command_status(db, current, command_key)


@router.post("", response_model=BatchDetailOut, status_code=status.HTTP_201_CREATED)
async def create_batch(
    body: BatchCreate,
    current: CurrentUser,
    db: DbSession,
    command_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=120),
):
    await _guard(db, current.org_id)
    try:
        detail = await reimbursement_commands.create(db, current, body, command_key)
    except reimbursement.ReimbursementError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e))
    await db.commit()
    return detail


@router.get("/{batch_id}", response_model=BatchDetailOut)
async def get_batch(batch_id: str, current: CurrentUser, db: DbSession):
    await _guard(db, current.org_id)
    b = await _load(db, current.org_id, batch_id)
    return await _detail(db, current.org_id, b)


@router.post("/{batch_id}/pay", response_model=BatchDetailOut)
async def pay_batch(
    batch_id: str,
    body: BatchPay,
    current: CurrentUser,
    db: DbSession,
    command_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=120),
):
    await _guard(db, current.org_id)
    command = await reimbursement_commands.prepare_pay(db, current, batch_id, body, command_key)
    if command.previous is not None:
        return command.previous
    b = await _load(db, current.org_id, batch_id, lock=True)
    if body.version != b.version:
        raise ConflictError(
            f"Batch changed (your {body.version}, current {b.version}).",
            code="reimbursement_payment_conflict",
        )
    try:
        await reimbursement.mark_paid(
            db,
            current.org_id,
            b,
            reference=body.reference,
            method=body.method,
            actor_id=current.id,
            actor_email=current.email,
            is_platform_admin=bool(current.is_platform_admin),
            override_sod=body.override_sod,
            expected_total_eur=body.expected_total_eur,
            report_versions=body.report_versions or None,
        )
    except reimbursement.ReimbursementError as e:
        raise ConflictError(str(e), code="reimbursement_payment_conflict") from e
    detail = await reimbursement_commands.record_pay(db, current, b, command)
    await db.commit()
    return detail


async def _cancel(
    batch_id: str,
    body: BatchCancel | None,
    current: CurrentUser,
    db: DbSession,
    command_key: str | None,
) -> BatchCancelOut:
    await _guard(db, current.org_id)
    command = await reimbursement_commands.prepare_cancel(db, current, batch_id, body, command_key)
    if command.previous is not None:
        return command.previous
    b = await _load(db, current.org_id, batch_id, lock=True)
    if body is not None and body.version != b.version:
        raise ConflictError(
            f"Batch changed (your {body.version}, current {b.version}).",
            code="reimbursement_cancellation_conflict",
        )
    try:
        released = await reimbursement.cancel_batch(
            db,
            current.org_id,
            b,
            report_versions=body.report_versions if body is not None else None,
        )
    except reimbursement.ReimbursementError as e:
        raise ConflictError(str(e), code="reimbursement_cancellation_conflict") from e
    result = await reimbursement_commands.record_cancel(
        db,
        current,
        b,
        released,
        command,
        reason=body.reason if body is not None else None,
    )
    await db.commit()
    return result


@router.post("/{batch_id}/cancel", response_model=BatchCancelOut)
async def cancel_batch_command(
    batch_id: str,
    body: BatchCancel,
    current: CurrentUser,
    db: DbSession,
    command_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=120),
):
    """Cancel the exact reviewed batch; replay returns its original released set."""
    return await _cancel(batch_id, body, current, db, command_key)


@router.delete("/{batch_id}", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_batch(
    batch_id: str,
    current: CurrentUser,
    db: DbSession,
    body: BatchCancel | None = None,
    command_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=120),
):
    """Compatibility adapter; old no-key attempts retain their no-replay semantics."""
    await _cancel(batch_id, body, current, db, command_key)


# BE-010 (audit 2026-09-05): producing a bank file advances the export-once
# counter and writes an audit event — a state change, so the verb is POST.
# As a GET it was replayable by a browser prefetch, a link preview or a proxy
# revalidation, each one consuming "the one export" and leaving the treasurer
# to confirm a re-export they never made.
@router.post("/{batch_id}/export")
async def export_batch(
    batch_id: str, current: CurrentUser, db: DbSession, confirm_reexport: bool = False
):
    """The batch's bank/payroll CSV. WO-9 export guard (same treatment as payment
    runs): never a cancelled batch, export-once (`confirm_reexport` for a second
    file), every export audited. Loaded FOR UPDATE to serialize the counter."""
    await _guard(db, current.org_id)
    b = await _load(db, current.org_id, batch_id, lock=True)
    reimbursement.assert_exportable(b, confirm_reexport=confirm_reexport)
    reports = await reimbursement.batch_reports(db, current.org_id, b.id)
    csv_text = reimbursement.export_csv(b, reports)
    reimbursement.record_export(b, msg_id=None)
    await audit.record(
        db,
        audit.A.REIMBURSE_EXPORTED,
        target_type="reimbursement_batch",
        target_id=b.id,
        meta={
            "format": "csv",
            "export_count": b.export_count,
            "msg_id": None,
            "payees": len(reports),
            "total_eur": str(b.total_eur),
        },
    )
    await db.commit()
    fname = f"reimbursement-{(b.reference or b.id)}.csv"
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={
            "Content-Disposition": content_disposition(fname),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/{batch_id}/sepa")
async def export_batch_sepa(
    batch_id: str,
    current: CurrentUser,
    db: DbSession,
    confirm_reexport: bool = False,
    acknowledge_skipped: bool = False,
):
    """Export the batch as an ISO 20022 pain.001 SEPA credit-transfer file (paid to
    employees with an IBAN on file). 422 if the issuer has no IBAN or no payee has
    one. WO-9: export-once (`confirm_reexport`), a UNIQUE MsgId per generation, and
    employees without an IBAN are NAMED in a 409 refusal (`acknowledge_skipped` to
    export without them) — never only counted in a header. The `X-Skipped` header
    is kept for compatibility (the test suite reads it; the SPA never did)."""
    await _guard(db, current.org_id)
    b = await _load(db, current.org_id, batch_id, lock=True)
    reimbursement.assert_exportable(b, confirm_reexport=confirm_reexport)
    msg_id = sepa.new_msg_id("REIMB", b.id, b.export_count + 1)
    try:
        xml, skipped = await reimbursement.batch_sepa(
            db, current.org_id, b, msg_id=msg_id, acknowledge_skipped=acknowledge_skipped
        )
    except (reimbursement.ReimbursementError, sepa.SepaError) as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e))
    reimbursement.record_export(b, msg_id=msg_id)
    reports = await reimbursement.batch_reports(db, current.org_id, b.id)
    await audit.record(
        db,
        audit.A.REIMBURSE_EXPORTED,
        target_type="reimbursement_batch",
        target_id=b.id,
        meta={
            "format": "sepa",
            "export_count": b.export_count,
            "msg_id": msg_id,
            "payees": len(reports) - len(skipped),
            "skipped": skipped,
            "total_eur": str(b.total_eur),
        },
    )
    await db.commit()
    fname = f"reimbursement-{(b.reference or b.id)}.xml"
    return Response(
        content=xml,
        media_type="application/xml",
        headers={
            "Content-Disposition": content_disposition(fname),
            "X-Content-Type-Options": "nosniff",
            "X-Skipped": str(len(skipped)),
        },
    )
