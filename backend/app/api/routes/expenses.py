from __future__ import annotations

import json
import logging
import re
from datetime import date
from decimal import Decimal
from urllib.parse import quote

from fastapi import (
    APIRouter,
    Depends,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.api.deps import CurrentUser, DbSession, refresh_current_identity, require_perm
from app.api.upload_read import read_upload
from app.core import authz, storage
from app.core.dimensions import DIMENSION_KEYS
from app.core.errors import AppError
from app.core.identity import Principal
from app.core.roles import is_admin_or_above
from app.models.document_version import OWNER_EXPENSE_RECEIPT
from app.models.expense import (
    ExpenseComment,
    ExpenseItem,
    ExpenseReport,
    ExpenseTransaction,
)
from app.models.user import User
from app.schemas.document_version import DocumentVersionOut
from app.schemas.expense import (
    ApprovalStepOut,
    BankImportResult,
    CategoryTotal,
    ExpenseApprovalPolicyIn,
    ExpenseApprovalPolicyOut,
    ExpenseApprovalPolicyUpdate,
    ExpenseCommandOut,
    ExpenseCommentIn,
    ExpenseCommentOut,
    ExpenseDecision,
    ExpenseItemIn,
    ExpenseItemOut,
    ExpenseItemPatch,
    ExpensePolicyIn,
    ExpensePolicyOut,
    ExpenseReportCreate,
    ExpenseReportDetail,
    ExpenseReportListOut,
    ExpenseReportOut,
    ExpenseReportUpdate,
    ExpenseSummary,
    ExpenseTransactionOut,
    ItemFromTransaction,
    MatchTransaction,
    PolicyCheckOut,
    PolicyViolation,
    ReassignIn,
    ReceiptScanOut,
)
from app.services import (
    audit,
    bank_statement,
    costing,
    document_versions,
    documents,
    expense_approval,
    expense_commands,
    expense_policy,
    expense_state,
    expense_transaction_amounts,
    expenses,
    filesec,
    fx,
    image_ocr,
    modules,
    receipt_ocr,
    webhooks,
)
from app.services import bin as bin_svc
from app.services.pdf_ocr import OCR_TIMED_OUT_DETAIL

# Structural authorization (ADR-0024): every expense route needs at least
# EXPENSE_READ (router-level). Claimant actions (create/edit/submit/withdraw and
# the transaction inbox) declare EXPENSE_WRITE; approver decisions declare
# EXPENSE_APPROVE (the in-handler assigned-approver + segregation-of-duties
# checks REMAIN — they are stricter than any permission); policy administration
# declares SETTINGS_MANAGE (matching the existing is_admin_or_above checks,
# which remain as defence in depth).
log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/expenses",
    tags=["expenses"],
    dependencies=[Depends(require_perm(authz.Permission.EXPENSE_READ))],
)
_WRITE = [Depends(require_perm(authz.Permission.EXPENSE_WRITE))]
_APPROVE = [Depends(require_perm(authz.Permission.EXPENSE_APPROVE))]
_ADMIN = [Depends(require_perm(authz.Permission.SETTINGS_MANAGE))]


async def _guard(db: DbSession, org_id: str):
    await modules.require_enabled(db, org_id, "expenses")


async def _link_items(db: DbSession, org_id: str, items, dimensions=DIMENSION_KEYS) -> None:
    """Resolve each item's cost-allocation tags to master links at write time
    (Slice 3b — the expense-side twin of the invoice write-path resolution)."""
    for it in items:
        await costing.apply_links(db, org_id, it, dimensions)


def _is_approver(user: User | Principal) -> bool:
    # Designated expense approvers may approve/reject/queue reimbursement. The owner
    # (first-registered user) is one by default; others are appointed on /team.
    return bool(getattr(user, "is_expense_approver", False))


def _can_oversee(user: User | Principal) -> bool:
    # See every report in the tenant + the pending-approvals queue: approvers,
    # plus admins for oversight.
    return _is_approver(user) or is_admin_or_above(user)


def _detail(r: ExpenseReport) -> ExpenseReportDetail:
    d = ExpenseReportDetail.model_validate(r)
    items = []
    for it in r.items:
        # from_attributes carries the dimension tags + base fields; the two
        # derived flags are computed here.
        out = ExpenseItemOut.model_validate(it)
        out.has_receipt = it.receipt_sha256 is not None
        out.verified = it.bank_reference is not None
        items.append(out)
    d.items = items
    return d


async def _detail_with_steps(r: ExpenseReport, db: DbSession, org_id: str) -> ExpenseReportDetail:
    """Report detail enriched with its approval-chain steps."""
    d = _detail(r)
    steps = await expense_approval.steps_for(db, org_id, r.id)
    d.approval_steps = [ApprovalStepOut.model_validate(s) for s in steps]
    return d


async def _load(db: DbSession, org_id: str, report_id: str, *, lock: bool = False) -> ExpenseReport:
    stmt = (
        select(ExpenseReport)
        .where(ExpenseReport.id == report_id, ExpenseReport.org_id == org_id)
        .options(selectinload(ExpenseReport.items))
    )
    if lock:
        # Owner edits/transitions and decisions share one review boundary. A
        # pre-wait identity-map instance must not authorize a stale editable
        # state or replace the contents/approval chain of a submitted report.
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    r = await db.scalar(stmt)
    if r is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Expense report not found")
    return r


def _require_view(r: ExpenseReport, user: User | Principal):
    if not _can_oversee(user) and r.employee_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Expense report not found")


def _require_owner_editable(r: ExpenseReport, user: User | Principal):
    if r.employee_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You can only edit your own reports")
    if not expense_state.is_editable(r.status):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Only draft or returned reports can be edited"
        )


async def _review_audit(db: DbSession, action: str, **kwargs) -> None:
    if await audit.record(db, action, **kwargs) is None:
        raise AppError(
            "The report change could not be recorded. Refresh the report before retrying.",
            code="expense_review_unavailable",
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )


async def _load_available_txns(
    db: DbSession, org_id: str, user_id: str, ids: list[str]
) -> list[ExpenseTransaction]:
    return await expense_commands.available(db, org_id, user_id, ids)


def _txn_reference(t: ExpenseTransaction) -> str:
    return expense_commands.transaction_reference(t)


@router.post(
    "",
    response_model=ExpenseReportDetail,
    status_code=status.HTTP_201_CREATED,
    dependencies=_WRITE,
)
async def create_report(
    body: ExpenseReportCreate,
    current: CurrentUser,
    db: DbSession,
    command_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=120),
):
    await _guard(db, current.org_id)
    outcome = await expense_commands.create_report(db, current, body, command_key)
    if outcome.previous is not None:
        return outcome.previous
    assert outcome.report is not None
    detail = _detail(outcome.report)
    await expense_commands.record(
        db,
        current,
        outcome.key,
        "report_created",
        outcome.payload_hash,
        outcome.report.id,
        detail.model_dump(mode="json"),
    )
    await db.commit()
    return detail


@router.post("/import/bank-statement", response_model=BankImportResult, dependencies=_WRITE)
async def import_bank_statement(
    current: CurrentUser,
    db: DbSession,
    file: UploadFile,
    account_reference: str | None = Form(default=None, max_length=120),
    statement_currency: str | None = Form(default=None, max_length=3),
    command_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=120),
):
    try:
        await _guard(db, current.org_id)
        content = await read_upload(file)
        try:
            await filesec.check_async(
                file.filename or "statement", content, allowed=frozenset({"pdf", "csv"})
            )
        except filesec.FileRejected as exc:
            raise HTTPException(
                status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, f"{exc}. Use a PDF or CSV statement."
            ) from exc
        try:
            parsed = await run_in_threadpool(
                bank_statement.parse, file.filename or "statement", content
            )
        except bank_statement.pdf_ocr.OcrUnavailable as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, f"OCR unavailable: {exc}"
            ) from exc
        except bank_statement.pdf_ocr.OcrTimedOut as exc:
            log.warning("statement OCR timed out: %s", exc)
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, OCR_TIMED_OUT_DETAIL) from exc
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
        result = await expense_commands.import_statement(
            db, current, parsed, content, account_reference, statement_currency, command_key
        )
        await db.commit()
        return result
    finally:
        await file.close()


@router.post("/receipt-scan", response_model=ReceiptScanOut, dependencies=_WRITE)
async def receipt_scan(current: CurrentUser, db: DbSession, file: UploadFile):
    """Advisory OCR of a receipt image/PDF → suggested item fields (merchant, date,
    amount, tax, currency). Reads only — writes nothing; the user confirms."""
    await _guard(db, current.org_id)
    content = await read_upload(file, "receipt")
    try:
        await filesec.check_async(
            file.filename or "receipt", content, allowed=filesec.RECEIPT_KINDS
        )
    except filesec.FileRejected as exc:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, str(exc))
    try:
        s = await image_ocr.run_reading(receipt_ocr.suggest, file.filename or "receipt", content)
    except image_ocr.ImageOcrError as exc:
        if exc.code == "processing_timeout":
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, OCR_TIMED_OUT_DETAIL) from exc
        raise AppError(
            str(exc),
            code=exc.code,
            status=(503 if exc.code in {"capture_unavailable", "image_reading_busy"} else 422),
            headers={"Cache-Control": "private, no-store"},
        ) from exc
    except receipt_ocr.pdf_ocr.OcrUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"OCR unavailable: {exc}")
    except receipt_ocr.pdf_ocr.OcrTimedOut as exc:
        log.warning("receipt OCR timed out: %s", exc)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, OCR_TIMED_OUT_DETAIL)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))
    return ReceiptScanOut(
        merchant=s.merchant,
        spend_date=s.spend_date,
        amount=s.amount,
        vat_amount=s.vat_amount,
        currency=s.currency,
        method=s.method,
        text_preview=s.text_preview,
        reading_preparation=s.reading_preparation,
        warnings=s.warnings,
    )


@router.get("/transactions", response_model=list[ExpenseTransactionOut])
async def list_transactions(
    current: CurrentUser, db: DbSession, status_: str = Query(default="available", alias="status")
):
    """The employee's 'available expenses' inbox."""
    await _guard(db, current.org_id)
    rows = await db.scalars(
        select(ExpenseTransaction)
        .where(
            ExpenseTransaction.org_id == current.org_id,
            ExpenseTransaction.employee_id == current.id,
            ExpenseTransaction.status == status_,
        )
        .order_by(ExpenseTransaction.txn_date.desc())
    )
    return [ExpenseTransactionOut.model_validate(t) for t in rows]


@router.delete(
    "/transactions/{txn_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=_WRITE
)
async def delete_transaction(
    txn_id: str,
    current: CurrentUser,
    db: DbSession,
    version: int | None = Query(default=None, ge=1),
    command_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=120),
):
    await _guard(db, current.org_id)
    await expense_commands.remove_transaction(db, current, txn_id, version, command_key)
    await db.commit()


@router.get("/commands/{command_key}", response_model=ExpenseCommandOut, dependencies=_WRITE)
async def expense_command_status(command_key: str, current: CurrentUser, db: DbSession):
    await _guard(db, current.org_id)
    row = await expense_commands.lookup(db, current.org_id, current.id, command_key)
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "No committed outcome is visible yet. Check again or retry the original command key.",
        )
    if row.operation in {"reimbursement_created", "reimbursement_paid", "reimbursement_cancelled"}:
        # Receipts share one actor/key identity. A former approver must not use
        # this employee-write endpoint to recover other employees' payout data.
        # Keep these operation guards on rollback once receipts exist.
        authz.require(current, authz.Permission.EXPENSE_APPROVE)
        await expense_commands.lock_actor(db, current, permission=authz.Permission.EXPENSE_APPROVE)
    return ExpenseCommandOut(
        id=row.id,
        command_key=row.command_key,
        operation=row.operation,
        target_id=row.target_id,
        result=json.loads(row.result_json),
    )


@router.get("/commands/{command_key}/result", response_model=dict, dependencies=_WRITE)
async def expense_command_result(command_key: str, current: CurrentUser, db: DbSession):
    await _guard(db, current.org_id)
    row = await expense_commands.lookup(db, current.org_id, current.id, command_key)
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "No committed outcome is visible yet. Check again or retry the original key.",
        )
    if row.operation in {"reimbursement_created", "reimbursement_paid", "reimbursement_cancelled"}:
        authz.require(current, authz.Permission.EXPENSE_APPROVE)
        await expense_commands.lock_actor(db, current, permission=authz.Permission.EXPENSE_APPROVE)
    return json.loads(row.result_json)


@router.get("", response_model=ExpenseReportListOut)
async def list_reports(
    current: CurrentUser,
    db: DbSession,
    status_: str | None = Query(default=None, alias="status"),
    mine: bool = False,
    employee_id: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=500),
):
    await _guard(db, current.org_id)
    filters = [ExpenseReport.org_id == current.org_id]
    if not _can_oversee(current) or mine:
        filters.append(ExpenseReport.employee_id == current.id)
    elif employee_id:
        filters.append(ExpenseReport.employee_id == employee_id)
    if status_:
        filters.append(ExpenseReport.status == status_)

    total = await db.scalar(select(func.count(ExpenseReport.id)).where(*filters))
    rows = await db.scalars(
        select(ExpenseReport)
        .where(*filters)
        .order_by(ExpenseReport.created_at.desc(), ExpenseReport.id.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return ExpenseReportListOut(
        items=[ExpenseReportOut.model_validate(r) for r in rows], total=total or 0
    )


@router.get("/summary", response_model=ExpenseSummary)
async def summary(current: CurrentUser, db: DbSession):
    await _guard(db, current.org_id)
    mine = ExpenseReport.employee_id == current.id

    my_draft = (
        await db.scalar(select(func.count()).where(mine, ExpenseReport.status == "draft")) or 0
    )
    my_submitted = (
        await db.scalar(select(func.count()).where(mine, ExpenseReport.status == "submitted")) or 0
    )
    my_reimbursable = await db.scalar(
        select(func.coalesce(func.sum(ExpenseReport.total), 0)).where(
            mine, ExpenseReport.status.in_(("approved", "marked_for_reimbursement"))
        )
    ) or Decimal("0")
    reclaimable_vat = await expenses.reclaimable_vat_total(db, current.org_id, current.id)
    pending = 0
    if _can_oversee(current):
        # One definition (WO-16): the dashboard projects this same count.
        pending = await expense_approval.pending_report_count(db, current.org_id)

    cat_rows = (
        await db.execute(
            select(ExpenseItem.category, func.coalesce(func.sum(ExpenseItem.amount), 0))
            .join(ExpenseReport, ExpenseReport.id == ExpenseItem.report_id)
            .where(ExpenseReport.employee_id == current.id)
            .group_by(ExpenseItem.category)
            .order_by(func.sum(ExpenseItem.amount).desc())
        )
    ).all()

    return ExpenseSummary(
        my_draft=my_draft,
        my_submitted=my_submitted,
        my_reimbursable=my_reimbursable,
        reclaimable_vat=reclaimable_vat,
        pending_approvals=pending,
        by_category=[CategoryTotal(category=c, total=t) for c, t in cat_rows],
    )


def _policy_out(p) -> ExpensePolicyOut:
    return ExpensePolicyOut(
        active=p.active if p else False,
        max_item_amount=p.max_item_amount if p else None,
        receipt_required_over=p.receipt_required_over if p else None,
        category_caps=expense_policy.caps_of(p),
        allowed_categories=expense_policy.allowed_categories_of(p),
        allowed_currencies=expense_policy.allowed_currencies_of(p),
        warn_weekend=bool(p.warn_weekend) if p else False,
        duplicate_detection=bool(p.duplicate_detection) if p else True,
        mileage_rate=p.mileage_rate if p else None,
        mileage_rate_tolerance=p.mileage_rate_tolerance if p else None,
        require_purpose_over=p.require_purpose_over if p else None,
        late_submission_days=p.late_submission_days if p else None,
        blocking_rules=expense_policy.blocking_rules_of(p),
        version=p.version if p else 0,
    )


@router.get("/policy", response_model=ExpensePolicyOut)
async def get_policy(current: CurrentUser, db: DbSession):
    """The org's expense spending policy (visible to everyone; edited by admins)."""
    await _guard(db, current.org_id)
    return _policy_out(await expense_policy.get(db, current.org_id))


@router.put("/policy", response_model=ExpensePolicyOut, dependencies=_ADMIN)
async def set_policy(body: ExpensePolicyIn, current: CurrentUser, db: DbSession):
    await _guard(db, current.org_id)
    if not is_admin_or_above(current):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only an admin can set the expense policy")
    p = await expense_policy.upsert(
        db,
        current.org_id,
        active=body.active,
        max_item_amount=body.max_item_amount,
        receipt_required_over=body.receipt_required_over,
        category_caps=body.category_caps,
        allowed_categories=body.allowed_categories,
        allowed_currencies=body.allowed_currencies,
        warn_weekend=body.warn_weekend,
        duplicate_detection=body.duplicate_detection,
        mileage_rate=body.mileage_rate,
        mileage_rate_tolerance=body.mileage_rate_tolerance,
        require_purpose_over=body.require_purpose_over,
        late_submission_days=body.late_submission_days,
        blocking_rules=body.blocking_rules,
    )
    await audit.record(
        db,
        audit.A.EXPENSE_POLICY_SET,
        target_type="expense_policy",
        target_id=p.id,
        meta={"active": p.active},
    )
    await db.commit()
    await db.refresh(p)
    return _policy_out(p)


# --------------------------------------------------------------------------- #
# Multi-step approval-routing policies (mirrors /approval-policies for invoices)
# --------------------------------------------------------------------------- #
def _appr_policy_out(p) -> ExpenseApprovalPolicyOut:
    return ExpenseApprovalPolicyOut(
        id=p.id,
        name=p.name,
        active=p.active,
        priority=p.priority,
        min_amount=p.min_amount,
        approver_ids=expense_approval.approver_ids_of(p),
        finance_final=p.finance_final,
        finance_approver_id=p.finance_approver_id,
        version=p.version,
    )


@router.get("/approval-policies", response_model=list[ExpenseApprovalPolicyOut])
async def list_approval_policies(current: CurrentUser, db: DbSession):
    await _guard(db, current.org_id)
    from app.models.expense_approval import ExpenseApprovalPolicy

    rows = await db.scalars(
        select(ExpenseApprovalPolicy)
        .where(ExpenseApprovalPolicy.org_id == current.org_id)
        .order_by(ExpenseApprovalPolicy.priority.asc(), ExpenseApprovalPolicy.created_at.asc())
    )
    return [_appr_policy_out(p) for p in rows]


@router.post(
    "/approval-policies",
    response_model=ExpenseApprovalPolicyOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=_ADMIN,
)
async def create_approval_policy(
    body: ExpenseApprovalPolicyIn, current: CurrentUser, db: DbSession
):
    await _guard(db, current.org_id)
    if not is_admin_or_above(current):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only an admin can manage approval policies")
    from app.models.expense_approval import ExpenseApprovalPolicy

    p = ExpenseApprovalPolicy(
        org_id=current.org_id,
        name=body.name,
        active=body.active,
        priority=body.priority,
        min_amount=body.min_amount,
        approver_ids=json.dumps(body.approver_ids) if body.approver_ids else None,
        finance_final=body.finance_final,
        finance_approver_id=body.finance_approver_id,
    )
    db.add(p)
    await audit.record(
        db,
        audit.A.EXPENSE_APPROVAL_POLICY,
        target_type="expense_approval_policy",
        meta={"op": "create"},
    )
    await db.commit()
    await db.refresh(p)
    return _appr_policy_out(p)


@router.patch(
    "/approval-policies/{policy_id}",
    response_model=ExpenseApprovalPolicyOut,
    dependencies=_ADMIN,
)
async def update_approval_policy(
    policy_id: str, body: ExpenseApprovalPolicyUpdate, current: CurrentUser, db: DbSession
):
    await _guard(db, current.org_id)
    if not is_admin_or_above(current):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only an admin can manage approval policies")
    from app.models.expense_approval import ExpenseApprovalPolicy

    p = await db.scalar(
        select(ExpenseApprovalPolicy).where(
            ExpenseApprovalPolicy.id == policy_id, ExpenseApprovalPolicy.org_id == current.org_id
        )
    )
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Policy not found")
    if body.version != p.version:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Policy changed (your {body.version}, current {p.version})"
        )
    p.name = body.name
    p.active = body.active
    p.priority = body.priority
    p.min_amount = body.min_amount
    p.approver_ids = json.dumps(body.approver_ids) if body.approver_ids else None
    p.finance_final = body.finance_final
    p.finance_approver_id = body.finance_approver_id
    p.version += 1
    await audit.record(
        db,
        audit.A.EXPENSE_APPROVAL_POLICY,
        target_type="expense_approval_policy",
        target_id=p.id,
        meta={"op": "update"},
    )
    await db.commit()
    await db.refresh(p)
    return _appr_policy_out(p)


@router.delete(
    "/approval-policies/{policy_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=_ADMIN
)
async def delete_approval_policy(policy_id: str, current: CurrentUser, db: DbSession):
    await _guard(db, current.org_id)
    if not is_admin_or_above(current):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only an admin can manage approval policies")
    from app.models.expense_approval import ExpenseApprovalPolicy

    p = await db.scalar(
        select(ExpenseApprovalPolicy).where(
            ExpenseApprovalPolicy.id == policy_id, ExpenseApprovalPolicy.org_id == current.org_id
        )
    )
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Policy not found")
    await db.delete(p)
    await audit.record(
        db,
        audit.A.EXPENSE_APPROVAL_POLICY,
        target_type="expense_approval_policy",
        target_id=policy_id,
        meta={"op": "delete"},
    )
    await db.commit()


@router.get("/{report_id}", response_model=ExpenseReportDetail)
async def get_report(report_id: str, current: CurrentUser, db: DbSession):
    await _guard(db, current.org_id)
    r = await _load(db, current.org_id, report_id)
    _require_view(r, current)
    detail = await _detail_with_steps(r, db, current.org_id)
    # Full configurable rule set for the reviewer (advisory unless a rule is
    # explicitly configured to block — see the submit gate).
    policy = await expense_policy.get(db, current.org_id)
    detail.policy_violations = [PolicyViolation(**v) for v in expense_policy.evaluate(policy, r)]
    return detail


@router.get("/{report_id}/policy-check", response_model=PolicyCheckOut)
async def policy_check(report_id: str, current: CurrentUser, db: DbSession):
    """Dry-run the policy engine over a report — every finding plus whether a
    configured hard-block would stop submission."""
    await _guard(db, current.org_id)
    r = await _load(db, current.org_id, report_id)
    _require_view(r, current)
    policy = await expense_policy.get(db, current.org_id)
    findings = expense_policy.evaluate(policy, r)
    blockers = expense_policy.blocking(findings)
    return PolicyCheckOut(
        violations=[PolicyViolation(**v) for v in findings],
        blocking=bool(blockers),
        can_submit=not blockers,
    )


@router.patch("/{report_id}", response_model=ExpenseReportDetail, dependencies=_WRITE)
async def update_report(
    report_id: str, body: ExpenseReportUpdate, current: CurrentUser, db: DbSession
):
    await _guard(db, current.org_id)
    r = await _load(db, current.org_id, report_id, lock=True)
    _require_owner_editable(r, current)

    if body.title is not None:
        r.title = body.title
    if body.currency is not None:
        r.currency = body.currency.upper()
    if body.note is not None:
        r.note = body.note
    if body.items is not None:
        r.items.clear()
        for item in await expenses.build_items(db, body.items, r.currency):
            r.items.append(item)
        await _link_items(db, current.org_id, r.items)
        r.total, r.vat_total = expenses.compute_totals(r.items)
    await db.commit()
    await db.refresh(r, attribute_names=["items"])
    return _detail(r)


@router.post("/{report_id}/submit", response_model=ExpenseReportDetail, dependencies=_WRITE)
async def submit_report(report_id: str, current: CurrentUser, db: DbSession):
    await _guard(db, current.org_id)
    r = await _load(db, current.org_id, report_id, lock=True)
    if r.employee_id != current.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You can only submit your own reports")
    # State machine: submit is legal from draft or returned (409 otherwise).
    target = expense_state.target_for("submit", r.status)
    if not r.items:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Add at least one expense before submitting"
        )

    # Compliance gate: every entry must carry a business purpose AND a receipt
    # (or a missing-receipt declaration; mileage/per-diem are exempt).
    incomplete = expenses.incomplete_items(r)
    if incomplete:
        detail = "; ".join(
            f"{it.description} (needs {', '.join(missing)})" for it, missing in incomplete
        )
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Each expense must have a business purpose and an attached receipt. Missing — {detail}",
        )

    # Policy gate: findings are advisory unless a rule is configured to hard-block.
    # Suspicious expenses are flagged for review, never auto-rejected.
    policy = await expense_policy.get(db, current.org_id)
    blockers = expense_policy.blocking(expense_policy.evaluate(policy, r))
    if blockers:
        detail = "; ".join(b["message"] for b in blockers)
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Submission blocked by policy — {detail}",
        )

    r.status = target
    r.submitted_at = expenses.now()
    r.decided_at = None
    r.decided_by = None
    r.decision_note = None
    if r.currency.upper() == "EUR":
        r.total_eur = r.total
    else:
        eur, _resolved = await fx.to_eur(db, r.total, r.currency, date.today())
        r.total_eur = eur

    # Build a fresh multi-step approval chain (a resubmit replaces any old one).
    # No policy ⇒ one generic open step = the legacy single-approver behaviour.
    await expense_approval.delete_chain(db, current.org_id, r.id)
    appr_policy = await expense_approval.evaluate(db, current.org_id, r)
    await expense_approval.build_chain(db, current.org_id, r, appr_policy)

    await _review_audit(
        db, audit.A.EXPENSE_SUBMIT, target_type="expense_report", target_id=r.id, meta={}
    )
    await webhooks.emit(
        db,
        current.org_id,
        "expense.submitted",
        {
            "id": r.id,
            "title": r.title,
            "employee_name": r.employee_name,
            "total": str(r.total),
            "currency": r.currency,
        },
    )
    await db.commit()
    await db.refresh(r, attribute_names=["items"])
    return await _detail_with_steps(r, db, current.org_id)


@router.post("/{report_id}/decision", response_model=ExpenseReportDetail, dependencies=_APPROVE)
async def decide(report_id: str, body: ExpenseDecision, current: CurrentUser, db: DbSession):
    """An approver/finance action on a report: approve, reject, return for
    correction, or mark for reimbursement. Legacy direct-payment actions return
    a conflict directing the caller to payout batches. The state machine validates
    legality; suspicious expenses are returned/flagged, never silently dropped."""
    await _guard(db, current.org_id)
    current = await expense_commands.lock_actor(
        db, current, permission=authz.Permission.EXPENSE_APPROVE
    )
    authz.require(current, authz.Permission.EXPENSE_READ, authz.Permission.EXPENSE_APPROVE)
    if not _is_approver(current):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Only a designated expense approver can decide on expenses"
        )
    # Locked FOR UPDATE (R4): the version check + step/status write below must be
    # atomic across two genuinely concurrent decisions on the same pending step.
    r = await _load(db, current.org_id, report_id, lock=True)
    if body.version != r.version:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"This report was changed by someone else (your version {body.version}, "
            f"current {r.version}). Reload and re-apply your decision.",
        )

    # Segregation of duties: an approver cannot decide on their own report.
    if r.employee_id == current.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You cannot approve your own expense report")

    action = expense_state.canonical(body.action)

    if action in ("approve", "reject", "return_for_correction"):
        # Multi-step chain decision: act on the current pending step only.
        if r.status not in expense_state.IN_APPROVAL:
            raise HTTPException(
                status.HTTP_409_CONFLICT, f"A {r.status} report is not awaiting approval"
            )
        steps = await expense_approval.steps_for(db, current.org_id, r.id)
        step = expense_approval.pending_step(steps)
        if step is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "No pending approval step")
        if not expense_approval.is_assigned_approver(step, current):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "This step is assigned to another approver — you cannot decide it yet.",
            )
        if action == "approve":
            expense_approval.decide_step(step, current, approved=True, note=body.note)
            new_status = expense_approval.chain_state(steps)
        elif action == "reject":
            expense_approval.decide_step(step, current, approved=False, note=body.note)
            expense_approval.skip_pending(steps)
            new_status = "rejected"
        else:  # return_for_correction
            expense_approval.skip_pending(steps)
            new_status = "returned"
        r.status = new_status
        r.decided_at = expenses.now()
        r.decided_by = current.email
        r.decision_note = body.note
        _audit_action = {
            "approve": audit.A.EXPENSE_APPROVE,
            "reject": audit.A.EXPENSE_REJECT,
            "return_for_correction": audit.A.EXPENSE_RETURN,
        }[action]
        await _review_audit(
            db,
            _audit_action,
            target_type="expense_report",
            target_id=r.id,
            meta={"to": new_status, "step_seq": step.seq},
        )
    else:
        # Queue progression only; direct payment actions fail before any mutation.
        target = expense_state.target_for(action, r.status)  # 409 if illegal
        r.status = target
        r.decided_at = expenses.now()
        r.decided_by = current.email
        r.decision_note = body.note
        await _review_audit(
            db,
            audit.A.EXPENSE_TRANSITION,
            target_type="expense_report",
            target_id=r.id,
            meta={"action": action, "to": target},
        )

    # R4: bump AFTER the decision is applied (both branches) so the next reader's
    # version reflects this decision — one bump point covers every action.
    r.version += 1

    if r.status in ("approved", "rejected", "returned"):
        await webhooks.emit(
            db,
            current.org_id,
            f"expense.{r.status}",
            {
                "id": r.id,
                "title": r.title,
                "employee_name": r.employee_name,
                "total": str(r.total),
                "currency": r.currency,
                "decided_by": r.decided_by,
            },
        )
    await db.commit()
    await db.refresh(r, attribute_names=["items"])
    return await _detail_with_steps(r, db, current.org_id)


@router.post("/{report_id}/reassign", response_model=ExpenseReportDetail, dependencies=_APPROVE)
async def reassign_step(report_id: str, body: ReassignIn, current: CurrentUser, db: DbSession):
    """Reassign a pending approval step to a different approver (an approver may
    delegate/route). Defaults to the current pending step."""
    await _guard(db, current.org_id)
    current = await expense_commands.lock_actor(
        db, current, permission=authz.Permission.EXPENSE_APPROVE
    )
    authz.require(current, authz.Permission.EXPENSE_READ, authz.Permission.EXPENSE_APPROVE)
    if not _is_approver(current):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only an expense approver can reassign")
    r = await _load(db, current.org_id, report_id, lock=True)
    if r.status not in expense_state.IN_APPROVAL:
        raise HTTPException(status.HTTP_409_CONFLICT, "Report is not awaiting approval")
    steps = await expense_approval.steps_for(db, current.org_id, r.id)
    if body.step_id:
        step = next((s for s in steps if s.id == body.step_id), None)
    else:
        step = expense_approval.pending_step(steps)
    if step is None or step.status != "pending":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such pending step")
    new_approver = await db.get(User, body.approver_id)
    if new_approver is None or new_approver.org_id != current.org_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Approver not found")
    if (step.approver_id, step.approver_email) != (new_approver.id, new_approver.email):
        r.version += 1
    step.approver_id = new_approver.id
    step.approver_email = new_approver.email
    await _review_audit(
        db,
        audit.A.EXPENSE_REASSIGN,
        target_type="expense_report",
        target_id=r.id,
        meta={"step_seq": step.seq, "to": new_approver.email},
    )
    await webhooks.emit(
        db,
        current.org_id,
        "expense.reassigned",
        {"id": r.id, "title": r.title, "approver": new_approver.email},
    )
    await db.commit()
    await db.refresh(r, attribute_names=["items"])
    return await _detail_with_steps(r, db, current.org_id)


@router.post("/{report_id}/withdraw", response_model=ExpenseReportDetail, dependencies=_WRITE)
async def withdraw_report(report_id: str, current: CurrentUser, db: DbSession):
    """Pull a submitted report back to draft before any decision is made
    (owner-only)."""
    await _guard(db, current.org_id)
    r = await _load(db, current.org_id, report_id, lock=True)
    if r.employee_id != current.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You can only withdraw your own reports")
    r.status = expense_state.target_for("withdraw", r.status)  # 409 if not submitted
    # A later edited resubmission must not accept a decision reviewed against
    # this submission. Keep the existing first-submission version convention.
    r.version += 1
    r.submitted_at = None
    # A withdrawn report goes back to the drawing board — discard its chain.
    await expense_approval.delete_chain(db, current.org_id, r.id)
    await _review_audit(
        db,
        audit.A.EXPENSE_TRANSITION,
        target_type="expense_report",
        target_id=r.id,
        meta={"action": "withdraw", "to": r.status},
    )
    await db.commit()
    await db.refresh(r, attribute_names=["items"])
    return _detail(r)


@router.post(
    "/{report_id}/items",
    response_model=ExpenseReportDetail,
    status_code=status.HTTP_201_CREATED,
    dependencies=_WRITE,
)
async def add_item(report_id: str, body: ExpenseItemIn, current: CurrentUser, db: DbSession):
    """Add a manual expense entry (standard / mileage / per-diem) to a draft or
    returned report."""
    await _guard(db, current.org_id)
    r = await _load(db, current.org_id, report_id, lock=True)
    _require_owner_editable(r, current)
    item = (await expenses.build_items(db, [body], r.currency))[0]
    r.items.append(item)
    await _link_items(db, current.org_id, [item])
    r.total, r.vat_total = expenses.compute_totals(r.items)
    await db.commit()
    await db.refresh(r, attribute_names=["items"])
    return _detail(r)


@router.delete("/{report_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=_WRITE)
async def delete_report(report_id: str, current: CurrentUser, db: DbSession):
    await _guard(db, current.org_id)
    r = await _load(db, current.org_id, report_id, lock=True)
    if r.employee_id != current.id or r.status != "draft":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only your own draft reports can be deleted")
    # WO-M: binned, not destroyed — restorable from the Trash screen.
    bin_svc.stamp(r, current.email)
    await _review_audit(
        db,
        "expense.report_binned",
        target_type="expense_report",
        target_id=r.id,
        meta={"title": r.title, "employee": r.employee_name},
    )
    await db.commit()


@router.post(
    "/{report_id}/items/from-transaction",
    response_model=ExpenseReportDetail,
    dependencies=_WRITE,
)
async def add_item_from_transaction(
    report_id: str, body: ItemFromTransaction, current: CurrentUser, db: DbSession
):
    """Add an inbox transaction to a draft report as an expense entry."""
    await _guard(db, current.org_id)
    r = await _load(db, current.org_id, report_id, lock=True)
    _require_owner_editable(r, current)
    txns = await _load_available_txns(db, current.org_id, current.id, [body.transaction_id])
    txn = txns[0]
    item = await expense_commands.item_from_transaction(
        db, txn, r.currency, body.category, body.vat_amount
    )
    r.items.append(item)
    await _link_items(db, current.org_id, [item])
    r.total, r.vat_total = expenses.compute_totals(r.items)
    await db.flush()
    txn.status = "assigned"
    txn.item_id = item.id
    txn.version += 1
    await db.commit()
    await db.refresh(r, attribute_names=["items"])
    return _detail(r)


@router.get("/{report_id}/comments", response_model=list[ExpenseCommentOut])
async def list_comments(report_id: str, current: CurrentUser, db: DbSession):
    await _guard(db, current.org_id)
    r = await _load(db, current.org_id, report_id)
    _require_view(r, current)
    rows = await db.scalars(
        select(ExpenseComment)
        .where(ExpenseComment.report_id == report_id)
        .order_by(ExpenseComment.created_at)
    )
    return [ExpenseCommentOut.model_validate(c) for c in rows]


@router.post(
    "/{report_id}/comments", response_model=ExpenseCommentOut, status_code=status.HTTP_201_CREATED
)
async def add_comment(report_id: str, body: ExpenseCommentIn, current: CurrentUser, db: DbSession):
    """Comment on a report — the employee↔approver thread (either side can post)."""
    await _guard(db, current.org_id)
    r = await _load(db, current.org_id, report_id)
    _require_view(r, current)
    c = ExpenseComment(
        org_id=current.org_id,
        report_id=report_id,
        author_id=current.id,
        author_name=current.name,
        body=body.body,
    )
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return ExpenseCommentOut.model_validate(c)


@router.patch(
    "/{report_id}/items/{item_id}", response_model=ExpenseReportDetail, dependencies=_WRITE
)
async def update_item(
    report_id: str, item_id: str, body: ExpenseItemPatch, current: CurrentUser, db: DbSession
):
    """Edit a draft/returned item — money, flags, declarations, purpose, and
    cost dimensions. Only fields explicitly present in the body are applied."""
    await _guard(db, current.org_id)
    r = await _load(db, current.org_id, report_id, lock=True)
    _require_owner_editable(r, current)
    item = next((i for i in r.items if i.id == item_id), None)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found")
    fields = body.model_fields_set
    # Scalar fields applied verbatim when present (a null clears an optional value).
    for key in (
        "description",
        "merchant",
        "spend_date",
        "reclaimable_tax",
        "payment_method",
        "customer_billable",
        "billable_customer",
        "comment",
        "missing_receipt_declaration",
        "category",
    ):
        if key in fields:
            setattr(item, key, getattr(body, key))
    # Money fields are quantized before storage.
    if "amount" in fields and body.amount is not None:
        item.amount = expenses.q(body.amount)
        # A repriced foreign-currency entry re-derives its FX provenance (WO-8):
        # the new amount is a stated conversion; the implied rate is recorded.
        if item.original_amount is not None and item.currency:
            await expenses.apply_item_fx(db, item, r.currency)
    if "vat_amount" in fields and body.vat_amount is not None:
        item.vat_amount = expenses.q(body.vat_amount)
    # Cost dimensions: apply only fields explicitly present.
    changed = [key for key in DIMENSION_KEYS if key in fields]
    for key in changed:
        setattr(item, key, getattr(body, key))
    await _link_items(db, current.org_id, [item], changed)
    # Money may have changed → refresh the report totals from the live items.
    if fields & {"amount", "vat_amount"}:
        r.total, r.vat_total = expenses.compute_totals(r.items)
    await db.commit()
    await db.refresh(r, attribute_names=["items"])
    return _detail(r)


# Amount tolerance (in currency units) when matching an item to a statement line.
_MATCH_TOLERANCE = Decimal("0.01")


@router.get(
    "/{report_id}/items/{item_id}/match-candidates", response_model=list[ExpenseTransactionOut]
)
async def match_candidates(report_id: str, item_id: str, current: CurrentUser, db: DbSession):
    """Available bank/card statement lines that plausibly match this item (same
    amount, within a few days), for reconciliation."""
    await _guard(db, current.org_id)
    r = await _load(db, current.org_id, report_id)
    _require_owner_editable(r, current)
    item = next((i for i in r.items if i.id == item_id), None)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found")
    rows = list(
        await db.scalars(
            select(ExpenseTransaction).where(
                ExpenseTransaction.org_id == current.org_id,
                ExpenseTransaction.employee_id == current.id,
                ExpenseTransaction.status == "available",
            )
        )
    )
    amt = Decimal(item.amount)
    cands = []
    for t in rows:
        if t.currency != r.currency or (t.source == "bank_statement" and t.import_batch_id is None):
            continue
        amount = expense_transaction_amounts.read_amount(t.amount)
        if amount is not None and abs(amount - amt) <= _MATCH_TOLERANCE:
            cands.append(t)
    cands.sort(key=lambda t: abs((t.txn_date - item.spend_date).days))
    return [ExpenseTransactionOut.model_validate(t) for t in cands[:10]]


@router.post(
    "/{report_id}/items/{item_id}/match", response_model=ExpenseReportDetail, dependencies=_WRITE
)
async def match_item(
    report_id: str, item_id: str, body: MatchTransaction, current: CurrentUser, db: DbSession
):
    """Reconcile a draft item against a bank/card statement transaction."""
    await _guard(db, current.org_id)
    r = await _load(db, current.org_id, report_id, lock=True)
    _require_owner_editable(r, current)
    item = next((i for i in r.items if i.id == item_id), None)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found")
    txns = await _load_available_txns(db, current.org_id, current.id, [body.transaction_id])
    txn = txns[0]
    expense_commands.require_report_currency(txn, r.currency)
    amount = expense_transaction_amounts.require_amount(txn)
    if abs(amount - Decimal(item.amount)) > _MATCH_TOLERANCE:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Amounts don't match: entry is {item.amount}, statement line is {txn.amount}.",
        )
    item.bank_reference = _txn_reference(txn)
    txn.status = "assigned"
    txn.item_id = item.id
    txn.version += 1
    await db.commit()
    await db.refresh(r, attribute_names=["items"])
    return _detail(r)


@router.delete(
    "/{report_id}/items/{item_id}/match",
    response_model=ExpenseReportDetail,
    dependencies=_WRITE,
)
async def unmatch_item(report_id: str, item_id: str, current: CurrentUser, db: DbSession):
    """Undo a bank-statement reconciliation, returning the line to the inbox."""
    await _guard(db, current.org_id)
    r = await _load(db, current.org_id, report_id, lock=True)
    _require_owner_editable(r, current)
    item = next((i for i in r.items if i.id == item_id), None)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found")
    txn = await db.scalar(
        select(ExpenseTransaction)
        .where(
            ExpenseTransaction.item_id == item_id,
            ExpenseTransaction.org_id == current.org_id,
            ExpenseTransaction.employee_id == current.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if txn is not None:
        txn.status = "available"
        txn.item_id = None
        txn.version += 1
    item.bank_reference = None
    await db.commit()
    await db.refresh(r, attribute_names=["items"])
    return _detail(r)


@router.post(
    "/{report_id}/items/{item_id}/receipt",
    response_model=ExpenseReportDetail,
    dependencies=_WRITE,
)
async def upload_receipt(
    report_id: str, item_id: str, current: CurrentUser, db: DbSession, file: UploadFile
):
    await _guard(db, current.org_id)
    r = await _load(db, current.org_id, report_id, lock=True)
    _require_owner_editable(r, current)
    item = next((i for i in r.items if i.id == item_id), None)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found")
    content = await read_upload(file, "receipt")
    # Security gate: validate the real type (PNG/JPEG/PDF) + malware-scan.
    try:
        kind = await filesec.check_async(
            file.filename or "receipt", content, allowed=filesec.RECEIPT_KINDS
        )
    except filesec.FileRejected as exc:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, str(exc))
    mime = {"png": "image/png", "jpeg": "image/jpeg", "pdf": "application/pdf"}[kind]
    sha, size = await documents.store(
        documents.RECEIPTS,
        current.org_id,
        content,
        mime,
        db=db,
        filename=file.filename,
        uploaded_by=current.email,
    )
    await document_versions.record(
        db,
        current.org_id,
        OWNER_EXPENSE_RECEIPT,
        item.id,
        sha256=sha,
        size=size,
        mime=mime,
        filename=file.filename,
        uploaded_by=current.email,
    )
    item.receipt_mime = mime
    item.receipt_sha256 = sha
    item.receipt_size = size
    await db.commit()
    await db.refresh(r, attribute_names=["items"])
    return _detail(r)


@router.get("/{report_id}/items/{item_id}/receipt")
async def get_receipt(
    report_id: str, item_id: str, current: CurrentUser, db: DbSession, request: Request
):
    await _guard(db, current.org_id)
    r = await _load(db, current.org_id, report_id)
    _require_view(r, current)
    item = next((i for i in r.items if i.id == item_id), None)
    if item is None or not item.receipt_sha256:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Receipt not found")
    identity = (item.receipt_sha256, item.receipt_size)
    try:
        content = await documents.load_verified_original(
            documents.RECEIPTS, current.org_id, item.receipt_sha256, size=item.receipt_size
        )
    except storage.StorageReadError:
        raise
    except storage.StorageError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Stored receipt missing") from None
    fresh, _ = await refresh_current_identity(request, db, current)
    authz.require(fresh, authz.Permission.EXPENSE_READ)
    await _guard(db, current.org_id)
    r = await _load(db, current.org_id, report_id)
    _require_view(r, fresh)
    item = next((i for i in r.items if i.id == item_id), None)
    if item is None or (item.receipt_sha256, item.receipt_size) != identity:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Receipt changed while preparing the download"
        )
    if content is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Stored receipt missing")
    await audit.record(
        db,
        audit.A.DOC_DOWNLOAD,
        target_type="receipt",
        target_id=item_id,
        meta={"report_id": report_id},
    )
    await db.commit()
    # Serve inert: force download and stop MIME sniffing.
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": "attachment",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


@router.get(
    "/{report_id}/items/{item_id}/receipt/versions",
    response_model=list[DocumentVersionOut],
)
async def receipt_versions(report_id: str, item_id: str, current: CurrentUser, db: DbSession):
    """The supersession history of this item's receipt (newest first) — an audit
    trail of every file that was uploaded to the slot, including replacements."""
    await _guard(db, current.org_id)
    r = await _load(db, current.org_id, report_id)
    _require_view(r, current)
    item = next((i for i in r.items if i.id == item_id), None)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found")
    rows = await document_versions.history(db, current.org_id, OWNER_EXPENSE_RECEIPT, item_id)
    return [DocumentVersionOut.model_validate(row) for row in rows]


@router.get("/{report_id}/pdf")
async def report_pdf(report_id: str, current: CurrentUser, db: DbSession):
    await _guard(db, current.org_id)
    r = await _load(db, current.org_id, report_id)
    _require_view(r, current)
    try:
        pdf = await run_in_threadpool(expenses.build_pdf, r)
    except expenses.PdfUnavailable as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"PDF generation unavailable: {e}")
    # HTTP headers are latin-1 only, but titles can contain any Unicode (e.g. an
    # em dash). Provide an ASCII-safe `filename` plus an RFC 5987 UTF-8 `filename*`.
    stem = r.title[:40].strip()
    ascii_name = re.sub(r"[^A-Za-z0-9_.-]", "_", stem.replace(" ", "_")) or "report"
    disposition = (
        f'attachment; filename="expense-{ascii_name}.pdf"; '
        f"filename*=UTF-8''{quote(f'expense-{stem}.pdf')}"
    )
    return Response(
        content=pdf, media_type="application/pdf", headers={"Content-Disposition": disposition}
    )
