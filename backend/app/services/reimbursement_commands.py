"""Bounded reimbursement reads and recoverable payout-batch creation.

The transaction belongs to the route. These functions never commit. Creation
shares expense_commands' tenant/actor/key identity; its membership lock guards
same-actor replay, and reimbursement's ordered report locks guard overlapping
selections made by different actors.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC
from uuid import UUID

from sqlalchemy import String, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import authz
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.identity import Principal
from app.models.base import new_uuid
from app.models.expense import BATCH_PAID, ExpenseCommand, ExpenseReport, ReimbursementBatch
from app.schemas.expense import ExpenseCommandOut
from app.schemas.reimbursement import (
    BatchCancel,
    BatchCancelOut,
    BatchCreate,
    BatchDetailOut,
    BatchOut,
    BatchPage,
    BatchPay,
    EligibleReportOut,
    EligibleReportPage,
    PaymentEvidence,
    ReimbursementReportOut,
)
from app.services import expense_commands, reimbursement, webhooks
from app.services.issued_commands import fingerprint

CREATE_OPERATION = "reimbursement_created"
PAY_OPERATION = "reimbursement_paid"
CANCEL_OPERATION = "reimbursement_cancelled"
REIMBURSEMENT_OPERATIONS = frozenset({CREATE_OPERATION, PAY_OPERATION, CANCEL_OPERATION})


@dataclass
class CancelCommand:
    key: str
    payload_hash: str
    previous: BatchCancelOut | None = None


async def prepare_cancel(
    db: AsyncSession,
    current: Principal,
    batch_id: str,
    body: BatchCancel | None,
    supplied_key: str | None,
) -> CancelCommand:
    if supplied_key is not None and body is None:
        raise ValidationError(
            "Refresh the batch and send its reviewed version and every report version.",
            code="reimbursement_cancellation_evidence_required",
        )
    try:
        canonical_batch_id = str(UUID(batch_id))
    except ValueError as exc:
        raise ValidationError("Select a valid reimbursement batch") from exc
    digest = fingerprint(
        {
            "batch_id": canonical_batch_id,
            **(body.model_dump(mode="json") if body else {"legacy": True}),
        }
    )
    # Old DELETE clients mean a fresh attempt, never an implicit replay of an
    # earlier cancellation after the released reports have been batched again.
    key = (
        f"legacy-reimbursement-cancel:{new_uuid()}"
        if supplied_key is None
        else expense_commands.command_key(supplied_key, CANCEL_OPERATION, digest)
    )
    await expense_commands.lock_actor(db, current, permission=authz.Permission.EXPENSE_APPROVE)
    previous = await expense_commands.lookup(
        db, current.org_id, current.id, key, operation=CANCEL_OPERATION, expected=digest
    )
    return CancelCommand(
        key,
        digest,
        BatchCancelOut.model_validate(json.loads(previous.result_json))
        if previous is not None
        else None,
    )


async def record_cancel(
    db: AsyncSession,
    current: Principal,
    batch: ReimbursementBatch,
    released: list[ExpenseReport],
    command: CancelCommand,
    *,
    reason: str | None,
) -> BatchCancelOut:
    await db.flush()
    result = BatchCancelOut(
        batch=await batch_detail(db, current.org_id, batch),
        released_reports=[ReimbursementReportOut.model_validate(r) for r in released],
        reason=reason,
    )
    await expense_commands.record(
        db,
        current,
        command.key,
        CANCEL_OPERATION,
        command.payload_hash,
        batch.id,
        result.model_dump(mode="json"),
    )
    # Cancellation has no external event. Receipt and strict audit are its
    # durable transaction; do not invent a webhook or a no-op job.
    return result


def batch_summary(
    batch: ReimbursementBatch, report_count: int, evidence: PaymentEvidence
) -> BatchOut:
    return BatchOut.model_validate(
        {
            **{
                name: getattr(batch, name)
                for name in BatchOut.model_fields
                if name not in {"report_count", "payment_evidence"}
            },
            "report_count": report_count,
            "payment_evidence": evidence,
        }
    )


async def batch_detail(
    db: AsyncSession,
    org_id: str,
    batch: ReimbursementBatch,
    *,
    evidence: PaymentEvidence | None = None,
) -> BatchDetailOut:
    reports = await reimbursement.batch_reports(db, org_id, batch.id)
    if evidence is None:
        evidence = (await payment_evidence(db, org_id, [batch]))[batch.id]
    summary = batch_summary(batch, len(reports), evidence)
    return BatchDetailOut(
        **summary.model_dump(),
        reports=[ReimbursementReportOut.model_validate(report) for report in reports],
    )


async def payment_evidence(
    db: AsyncSession, org_id: str, batches: list[ReimbursementBatch]
) -> dict[str, PaymentEvidence]:
    """Read only bounded receipt metadata; never parse large frozen results.

    The authenticated caller separately owns APPROVE and module guards. This
    projection reveals neither another actor's command key nor its payload.
    Duplicate/contradictory records are unavailable evidence, not a newest-row
    guess that silently certifies historical data.
    """
    if not batches:
        return {}
    rows = (
        await db.execute(
            select(
                ExpenseCommand.target_id,
                func.count(ExpenseCommand.id).label("record_count"),
                func.min(cast(ExpenseCommand.employee_id, String)).label("recorded_by_id"),
                func.min(ExpenseCommand.created_at).label("recorded_at"),
            )
            .where(
                ExpenseCommand.org_id == org_id,
                ExpenseCommand.operation == PAY_OPERATION,
                ExpenseCommand.target_id.in_([batch.id for batch in batches]),
            )
            .group_by(ExpenseCommand.target_id)
        )
    ).all()
    grouped = {row.target_id: row for row in rows}
    out = {}
    for batch in batches:
        record = grouped.get(batch.id)
        if record is None:
            out[batch.id] = PaymentEvidence(
                basis="legacy_unknown" if batch.status == BATCH_PAID else "not_recorded"
            )
        elif record.record_count == 1 and batch.status == BATCH_PAID:
            projected = PaymentEvidence(
                basis="operator_recorded",
                recorded_by_id=record.recorded_by_id,
                recorded_at=record.recorded_at,
            )
            batch_recording_time = batch.paid_at
            if batch_recording_time is not None and batch_recording_time.tzinfo is None:
                batch_recording_time = batch_recording_time.replace(tzinfo=UTC)
            out[batch.id] = (
                projected
                if batch_recording_time is not None
                and projected.recorded_at == batch_recording_time
                else PaymentEvidence(basis="unavailable")
            )
        else:
            out[batch.id] = PaymentEvidence(basis="unavailable")
    return out


@dataclass
class PayCommand:
    key: str
    payload_hash: str
    previous: BatchDetailOut | None = None


async def prepare_pay(
    db: AsyncSession,
    current: Principal,
    batch_id: str,
    body: BatchPay,
    supplied_key: str | None,
) -> PayCommand:
    if supplied_key is not None and (body.expected_total_eur is None or not body.report_versions):
        raise ValidationError(
            "Refresh the batch and send its reviewed EUR total and every current report version.",
            code="reimbursement_payment_evidence_required",
        )
    try:
        canonical_batch_id = str(UUID(batch_id))
    except ValueError as exc:
        raise ValidationError("Select a valid reimbursement batch") from exc
    digest = fingerprint({"batch_id": canonical_batch_id, **body.model_dump(mode="json")})
    key = (
        f"legacy-reimbursement-pay:{new_uuid()}"
        if supplied_key is None
        else expense_commands.command_key(supplied_key, PAY_OPERATION, digest)
    )
    await expense_commands.lock_actor(db, current, permission=authz.Permission.EXPENSE_APPROVE)
    previous = await expense_commands.lookup(
        db, current.org_id, current.id, key, operation=PAY_OPERATION, expected=digest
    )
    return PayCommand(
        key,
        digest,
        BatchDetailOut.model_validate(json.loads(previous.result_json))
        if previous is not None
        else None,
    )


async def record_pay(
    db: AsyncSession,
    current: Principal,
    batch: ReimbursementBatch,
    command: PayCommand,
) -> BatchDetailOut:
    if batch.status != BATCH_PAID or batch.paid_at is None:
        raise ConflictError(
            "The payment record could not be verified. No change was committed.",
            code="reimbursement_payment_conflict",
        )
    # paid_at is this server's recording time, not an assertion of bank settlement.
    # Store that same captured fact on the receipt so frozen/current projections
    # do not disagree by a few microseconds after the transaction commits.
    evidence = PaymentEvidence(
        basis="operator_recorded",
        recorded_by_id=current.id,
        recorded_at=batch.paid_at,
    )
    await db.flush()
    detail = await batch_detail(db, current.org_id, batch, evidence=evidence)
    await expense_commands.record(
        db,
        current,
        command.key,
        PAY_OPERATION,
        command.payload_hash,
        batch.id,
        detail.model_dump(mode="json"),
        recorded_at=batch.paid_at,
    )
    await webhooks.emit(
        db,
        current.org_id,
        "expense.reimbursed",
        {
            "id": batch.id,
            "reports": detail.report_count,
            "reference": batch.reference,
            "total_eur": str(batch.total_eur),
        },
        idempotency_key=f"reimbursement-paid:{batch.id}",
        strict=True,
    )
    return detail


async def create(
    db: AsyncSession, current: Principal, body: BatchCreate, supplied_key: str | None
) -> BatchDetailOut:
    if supplied_key is not None and not body.report_versions:
        raise ValidationError(
            "Reload the eligible reports and send the current version of every selection.",
            code="reimbursement_version_required",
        )
    payload = body.model_dump(mode="json")
    payload["report_ids"] = sorted(body.report_ids)
    digest = fingerprint(payload)
    # No-key callers retain their previous new-intent/refusal behavior. A stable
    # fallback from IDs alone would incorrectly replay an old cancelled batch
    # when a legacy client legitimately batches the released reports again.
    # Recovery therefore requires an explicit client key, as for manual expenses.
    key = (
        f"legacy-reimbursement:{new_uuid()}"
        if supplied_key is None
        else expense_commands.command_key(supplied_key, CREATE_OPERATION, digest)
    )
    await expense_commands.lock_actor(db, current, permission=authz.Permission.EXPENSE_APPROVE)
    previous = await expense_commands.lookup(
        db, current.org_id, current.id, key, operation=CREATE_OPERATION, expected=digest
    )
    if previous is not None:
        return BatchDetailOut.model_validate(json.loads(previous.result_json))

    batch = await reimbursement.create_batch(
        db,
        current.org_id,
        body.report_ids,
        method=body.method,
        note=body.note,
        created_by=current.email,
        created_by_id=current.id,
        report_versions=body.report_versions,
    )
    await db.flush()
    detail = await batch_detail(db, current.org_id, batch, evidence=PaymentEvidence())
    await expense_commands.record(
        db,
        current,
        key,
        CREATE_OPERATION,
        digest,
        batch.id,
        detail.model_dump(mode="json"),
    )
    await webhooks.emit(
        db,
        current.org_id,
        "expense.reimbursement_created",
        {"id": batch.id, "reports": detail.report_count, "total_eur": str(batch.total_eur)},
        idempotency_key=f"reimbursement-created:{current.id}:{key}",
        strict=True,
    )
    return detail


async def command_status(db: AsyncSession, current: Principal, key: str) -> ExpenseCommandOut:
    key = expense_commands.command_key(key, CREATE_OPERATION, "")
    await expense_commands.lock_actor(db, current, permission=authz.Permission.EXPENSE_APPROVE)
    row = await expense_commands.lookup(db, current.org_id, current.id, key)
    if row is not None and row.operation not in REIMBURSEMENT_OPERATIONS:
        raise ConflictError(
            "This key belongs to another expense operation. Check its original outcome.",
            code="expense_command_payload_conflict",
        )
    if row is None:
        raise NotFoundError(
            "No committed batch outcome is visible yet. Check again or retry the original "
            "command key and inputs.",
            code="reimbursement_command_not_found",
        )
    return ExpenseCommandOut(
        id=row.id,
        command_key=row.command_key,
        operation=row.operation,
        target_id=row.target_id,
        result=json.loads(row.result_json),
    )


async def eligible_page(
    db: AsyncSession, org_id: str, *, page: int, page_size: int
) -> EligibleReportPage:
    filters = (
        ExpenseReport.org_id == org_id,
        ExpenseReport.status.in_(("approved", "marked_for_reimbursement")),
        ExpenseReport.payout_batch_id.is_(None),
    )
    total = int(await db.scalar(select(func.count(ExpenseReport.id)).where(*filters)) or 0)
    rows = list(
        await db.scalars(
            select(ExpenseReport)
            .where(*filters)
            .order_by(ExpenseReport.created_at.desc(), ExpenseReport.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    return EligibleReportPage(
        items=[
            EligibleReportOut(
                id=row.id,
                title=row.title,
                employee_name=row.employee_name,
                total=row.total,
                currency=row.currency,
                total_eur=reimbursement.eur_or_none(row),
                status=row.status,
                version=row.version,
            )
            for row in rows
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


async def batch_page(db: AsyncSession, org_id: str, *, page: int, page_size: int) -> BatchPage:
    total = int(
        await db.scalar(
            select(func.count(ReimbursementBatch.id)).where(ReimbursementBatch.org_id == org_id)
        )
        or 0
    )
    # A bounded indexed scalar count keeps room for one bulk provenance read
    # in the existing three-query page contract. It does not load report rows.
    report_count = (
        select(func.count(ExpenseReport.id))
        .where(
            ExpenseReport.org_id == org_id,
            ExpenseReport.payout_batch_id == ReimbursementBatch.id,
            ExpenseReport.deleted_at.is_(None),
        )
        .correlate(ReimbursementBatch)
        .scalar_subquery()
    )
    rows = (
        await db.execute(
            select(ReimbursementBatch, report_count.label("report_count"))
            .where(ReimbursementBatch.org_id == org_id)
            .order_by(ReimbursementBatch.created_at.desc(), ReimbursementBatch.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    evidence = await payment_evidence(db, org_id, [row[0] for row in rows])
    return BatchPage(
        items=[batch_summary(batch, count, evidence[batch.id]) for batch, count in rows],
        total=total,
        page=page,
        page_size=page_size,
    )
