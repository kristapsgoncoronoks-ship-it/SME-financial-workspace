"""Supplier payment-run routes (Phase 14).

Group scheduled-for-payment supplier invoices into a payout run, mark it paid
(which settles each invoice via the AP payment ledger and advances it to `paid`),
and export it as a bank CSV. Gated on the PAYMENT_* permissions (cash application):
router-level PAYMENT_READ, PAYMENT_WRITE on the mutations. Every action is audited.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import NotNullable, func, select

from app.api.deps import CurrentUser, DbSession, require_perm
from app.core import authz
from app.core.security_headers import content_disposition
from app.models.invoice import Invoice
from app.models.payment_run import PaymentRun
from app.schemas.payment_run import (
    RunApprove,
    RunCreate,
    RunDetailOut,
    RunInvoiceOut,
    RunOut,
    RunPageOut,
    RunPay,
)
from app.services import audit, payment_run, sepa, webhooks

# Structural authorization (ADR-0024): router-level PAYMENT_READ; the mutating
# routes declare the stricter PAYMENT_WRITE per-route below.
router = APIRouter(
    prefix="/payment-runs",
    tags=["payment-runs"],
    dependencies=[Depends(require_perm(authz.Permission.PAYMENT_READ))],
)
_WRITE = [Depends(require_perm(authz.Permission.PAYMENT_WRITE))]


def _invoice_out(inv) -> RunInvoiceOut:
    return RunInvoiceOut(
        id=inv.id,
        invoice_number=inv.invoice_number,
        vendor_name=inv.vendor.name if inv.vendor else None,
        total=inv.total,
        currency=inv.currency,
        total_eur=inv.total_eur,
        workflow_state=inv.workflow_state.value,
    )


def _run_out(run: PaymentRun, invoice_count: int) -> RunOut:
    return RunOut(
        id=run.id,
        reference=run.reference,
        method=run.method,
        status=run.status,
        note=run.note,
        total_eur=run.total_eur,
        paid_at=run.paid_at,
        created_by=run.created_by,
        approved_by=run.approved_by,
        approved_at=run.approved_at,
        exported_at=run.exported_at,
        export_count=run.export_count,
        last_msg_id=run.last_msg_id,
        version=run.version,
        created_at=run.created_at,
        invoice_count=invoice_count,
    )


async def _load(db: DbSession, org_id: str, run_id: str, *, lock: bool = False) -> PaymentRun:
    stmt = select(PaymentRun).where(PaymentRun.id == run_id, PaymentRun.org_id == org_id)
    if lock:
        # Serialize pay/export/cancel and refresh any identity-map copy after the
        # lock wait so each decision sees the winner's committed state. SQLite
        # ignores FOR UPDATE (writes already serialize); Postgres takes the row lock.
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    r = await db.scalar(stmt)
    if r is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Payment run not found")
    return r


async def _detail(db: DbSession, org_id: str, run: PaymentRun) -> RunDetailOut:
    invoices = await payment_run.run_invoices(db, org_id, run.id)
    return RunDetailOut(
        **_run_out(run, len(invoices)).model_dump(),
        invoices=[_invoice_out(i) for i in invoices],
    )


@router.get("/payable", response_model=list[RunInvoiceOut])
async def list_payable(current: CurrentUser, db: DbSession):
    """Scheduled-for-payment invoices not yet in a run (the run candidate pool)."""
    rows = await payment_run.payable_invoices(db, current.org_id)
    return [_invoice_out(i) for i in rows]


@router.get("", response_model=list[RunOut])
async def list_runs(
    current: CurrentUser,
    db: DbSession,
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    """Payment runs, newest first. BE-011 (audit 2026-09-05): the invoice
    count per run used to be one query PER RUN (with the vendor loaded for
    each invoice — two round trips per run) and the list had no bound. Now
    one grouped count for the page, and a bound the SPA can page through;
    the default of 200 is above any workspace's run count seen so far, so
    the list a client sees is unchanged."""
    return await _history_items(db, current.org_id, limit=limit, offset=offset)


async def _history_items(db: DbSession, org_id: str, *, limit: int, offset: int) -> list[RunOut]:
    """Shared legacy/page rows and one grouped child count, independent of size."""
    rows = list(
        await db.scalars(
            select(PaymentRun)
            .where(PaymentRun.org_id == org_id)
            .order_by(PaymentRun.created_at.desc(), PaymentRun.id)
            .offset(offset)
            .limit(limit)
        )
    )
    counts: dict[str, int] = {}
    if rows:
        grouped = await db.execute(
            select(NotNullable(Invoice.payment_run_id), func.count())
            .where(
                Invoice.org_id == org_id,
                Invoice.payment_run_id.in_([r.id for r in rows]),
            )
            .group_by(Invoice.payment_run_id)
        )
        counts = {run_id: int(n) for run_id, n in grouped}
    return [_run_out(r, counts.get(r.id, 0)) for r in rows]


@router.get("/page", response_model=RunPageOut)
async def list_run_page(
    current: CurrentUser,
    db: DbSession,
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    """History navigation with an exact scoped total, before /{run_id}.

    This display population is independent of the payable invoice selector and
    of every ID-scoped approve/pay/export/cancel command. No financial action
    consumes the displayed page as its scope.
    """
    total = await db.scalar(
        select(func.count()).select_from(PaymentRun).where(PaymentRun.org_id == current.org_id)
    )
    return RunPageOut(
        items=await _history_items(db, current.org_id, limit=limit, offset=offset),
        total=total or 0,
    )


@router.post(
    "", response_model=RunDetailOut, status_code=status.HTTP_201_CREATED, dependencies=_WRITE
)
async def create_run(body: RunCreate, current: CurrentUser, db: DbSession):
    try:
        run = await payment_run.create_run(
            db,
            current.org_id,
            body.invoice_ids,
            method=body.method,
            note=body.note,
            created_by=current.email,
            created_by_id=current.id,
            confirm_provisional=body.confirm_provisional,
        )
    except payment_run.PaymentRunError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e))
    await audit.record(
        db,
        audit.A.AP_RUN_CREATE,
        target_type="payment_run",
        target_id=run.id,
        meta={"invoices": len(body.invoice_ids), "method": run.method},
    )
    await db.commit()
    await db.refresh(run)
    return await _detail(db, current.org_id, run)


@router.get("/{run_id}", response_model=RunDetailOut)
async def get_run(run_id: str, current: CurrentUser, db: DbSession):
    run = await _load(db, current.org_id, run_id)
    return await _detail(db, current.org_id, run)


@router.post("/{run_id}/approve", response_model=RunDetailOut, dependencies=_WRITE)
async def approve_run(run_id: str, body: RunApprove, current: CurrentUser, db: DbSession):
    """The checker's step (WO-9): a SECOND user reviews the run. The creator gets
    403 `maker_is_checker` — the service enforces it, incl. the audited
    platform-admin override."""
    run = await _load(db, current.org_id, run_id, lock=True)
    if body.version != run.version:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Run changed (your {body.version}, current {run.version})."
        )
    try:
        await payment_run.approve_run(
            db,
            current.org_id,
            run,
            actor_id=current.id,
            actor_email=current.email,
            is_platform_admin=bool(current.is_platform_admin),
            override_sod=body.override_sod,
        )
    except payment_run.PaymentRunError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e))
    await audit.record(
        db,
        audit.A.AP_RUN_APPROVE,
        target_type="payment_run",
        target_id=run.id,
        meta={"total_eur": str(run.total_eur)},
    )
    await db.commit()
    await db.refresh(run)
    return await _detail(db, current.org_id, run)


@router.post("/{run_id}/pay", response_model=RunDetailOut, dependencies=_WRITE)
async def pay_run(run_id: str, body: RunPay, current: CurrentUser, db: DbSession):
    run = await _load(db, current.org_id, run_id, lock=True)
    if body.version != run.version:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Run changed (your {body.version}, current {run.version})."
        )
    try:
        invoices = await payment_run.mark_paid(
            db,
            current.org_id,
            run,
            reference=body.reference,
            method=body.method,
            actor_id=current.id,
            actor_email=current.email,
            is_platform_admin=bool(current.is_platform_admin),
            override_sod=body.override_sod,
        )
    except payment_run.PaymentRunError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e))
    await audit.record(
        db,
        audit.A.AP_RUN_PAID,
        target_type="payment_run",
        target_id=run.id,
        meta={
            "invoices": len(invoices),
            "reference": body.reference,
            "total_eur": str(run.total_eur),
        },
    )
    for inv in invoices:
        await webhooks.emit(
            db, current.org_id, "invoice.paid", {"id": inv.id, "invoice_number": inv.invoice_number}
        )
    await db.commit()
    await db.refresh(run)
    return await _detail(db, current.org_id, run)


@router.delete("/{run_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=_WRITE)
async def cancel_run(run_id: str, current: CurrentUser, db: DbSession):
    run = await _load(db, current.org_id, run_id, lock=True)
    try:
        await payment_run.cancel_run(db, current.org_id, run)
    except payment_run.PaymentRunError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e))
    await audit.record(
        db, audit.A.AP_RUN_CANCEL, target_type="payment_run", target_id=run.id, meta={}
    )
    await db.commit()


# BE-010 (audit 2026-09-05): producing a bank file advances the export-once
# counter and writes an audit event — a state change, so the verb is POST.
# As a GET it was replayable by a browser prefetch, a link preview or a proxy
# revalidation, each one consuming "the one export" and leaving the treasurer
# to confirm a re-export they never made.
@router.post("/{run_id}/export", dependencies=_WRITE)
async def export_run(
    run_id: str, current: CurrentUser, db: DbSession, confirm_reexport: bool = False
):
    """The run's bank CSV. WO-9 export guard: PAYMENT_WRITE (producing a payment
    file is a treasury act, not a read), approved/paid runs only, export-once (a
    second file needs `confirm_reexport=true`), every export audited. Loaded
    FOR UPDATE so concurrent exports serialize on the export counter."""
    run = await _load(db, current.org_id, run_id, lock=True)
    payment_run.assert_exportable(run, confirm_reexport=confirm_reexport)
    invoices = await payment_run.run_invoices(db, current.org_id, run.id)
    csv_text = payment_run.export_csv(run, invoices)
    payment_run.record_export(run, msg_id=None)
    await audit.record(
        db,
        audit.A.AP_RUN_EXPORTED,
        target_type="payment_run",
        target_id=run.id,
        meta={
            "format": "csv",
            "export_count": run.export_count,
            "msg_id": None,
            "payees": len(invoices),
            "total_eur": str(run.total_eur),
        },
    )
    await db.commit()
    fname = f"payment-run-{(run.reference or run.id)}.csv"
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={
            "Content-Disposition": content_disposition(fname),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/{run_id}/sepa", dependencies=_WRITE)
async def export_sepa(
    run_id: str,
    current: CurrentUser,
    db: DbSession,
    confirm_reexport: bool = False,
    acknowledge_skipped: bool = False,
):
    """The run as an ISO 20022 pain.001 SEPA credit-transfer file for the bank.
    WO-9 export guard: PAYMENT_WRITE, approved/paid runs only, export-once
    (`confirm_reexport`), a UNIQUE MsgId per generation, refusal NAMING payees
    that would be silently dropped (`acknowledge_skipped` to proceed), audited."""
    run = await _load(db, current.org_id, run_id, lock=True)
    payment_run.assert_exportable(run, confirm_reexport=confirm_reexport)
    msg_id = sepa.new_msg_id("RUN", run.id, run.export_count + 1)
    try:
        xml, skipped = await sepa.payment_run_sepa(
            db, current.org_id, run, msg_id=msg_id, acknowledge_skipped=acknowledge_skipped
        )
    except sepa.SepaError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e))
    payment_run.record_export(run, msg_id=msg_id)
    invoices = await payment_run.run_invoices(db, current.org_id, run.id)
    await audit.record(
        db,
        audit.A.AP_RUN_EXPORTED,
        target_type="payment_run",
        target_id=run.id,
        meta={
            "format": "sepa",
            "export_count": run.export_count,
            "msg_id": msg_id,
            "payees": len(invoices) - len(skipped),
            "skipped": skipped,
            "total_eur": str(run.total_eur),
        },
    )
    await db.commit()
    fname = f"payment-run-{(run.reference or run.id)}.xml"
    return Response(
        content=xml,
        media_type="application/xml",
        headers={
            "Content-Disposition": content_disposition(fname),
            "X-Content-Type-Options": "nosniff",
        },
    )
