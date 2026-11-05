"""Expense command receipts and exact source identity.

The caller owns the transaction. No helper commits. Membership locks serialize
one employee's commands; candidate locks coordinate assignment/bin/restore.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import authz, tenant
from app.core.dimensions import DIMENSION_KEYS
from app.core.errors import (
    AppError,
    ConflictError,
    NotFoundError,
    PermissionError,
    ValidationError,
)
from app.core.identity import Principal
from app.models.base import new_uuid
from app.models.expense import (
    ExpenseCommand,
    ExpenseImportBatch,
    ExpenseItem,
    ExpenseReport,
    ExpenseTransaction,
)
from app.models.membership import Membership
from app.schemas.expense import ExpenseReportCreate, ExpenseTransactionOut
from app.services import audit, bank_statement, costing, expenses, modules
from app.services import bin as bin_svc
from app.services.issued_commands import fingerprint

PARSER_VERSION = "statement-r010-v1"
MAX_IMPORT_ROWS = 5000


def command_key(value: str | None, operation: str, payload_hash: str) -> str:
    if value is None:
        # Transitional deterministic identity for old clients. A new identical
        # intention after restore/unmatch needs an explicit client key.
        return f"legacy-{operation}:{payload_hash}"
    if not re.fullmatch(r"[A-Za-z0-9:_-]{1,120}", value):
        raise ValidationError("Invalid command key", code="expense_command_key_invalid")
    return value


def currency(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    value = value.strip().upper()
    if re.fullmatch(r"[A-Z]{3}", value) is None:
        raise ValidationError("Use a three-letter currency code", code="expense_currency_invalid")
    return value


async def lock_actor(
    db: AsyncSession,
    current: Principal,
    *,
    permission: authz.Permission = authz.Permission.EXPENSE_WRITE,
) -> None:
    membership = await db.scalar(
        select(Membership)
        .where(Membership.org_id == current.org_id, Membership.user_id == current.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if membership is None or membership.status != "active":
        raise PermissionError(
            "Expense access is unavailable. Refresh your account or ask an administrator."
        )
    authority = replace(
        current, role=membership.role, is_expense_approver=membership.is_expense_approver
    )
    if not authz.has(authority, permission):
        raise PermissionError(
            "Expense access is unavailable. Refresh your account or ask an administrator."
        )
    await modules.require_enabled(db, current.org_id, "expenses")


async def lookup(
    db: AsyncSession,
    org_id: str,
    employee_id: str,
    key: str,
    *,
    operation: str | None = None,
    expected: str | None = None,
) -> ExpenseCommand | None:
    row = await db.scalar(
        select(ExpenseCommand).where(
            ExpenseCommand.org_id == org_id,
            ExpenseCommand.employee_id == employee_id,
            ExpenseCommand.command_key == key,
        )
    )
    if row is not None and (
        (operation is not None and row.operation != operation)
        or (expected is not None and row.payload_hash != expected)
    ):
        raise ConflictError(
            "This command already committed with other inputs. Check its recorded outcome.",
            code="expense_command_payload_conflict",
        )
    return row


async def record(
    db: AsyncSession,
    current: Principal,
    key: str,
    operation: str,
    payload_hash: str,
    target_id: str,
    result: dict,
    *,
    recorded_at: datetime | None = None,
) -> None:
    db.add(
        ExpenseCommand(
            org_id=current.org_id,
            employee_id=current.id,
            command_key=key,
            operation=operation,
            payload_hash=payload_hash,
            target_id=target_id,
            result_json=json.dumps(result, sort_keys=True, separators=(",", ":")),
            **({"created_at": recorded_at} if recorded_at is not None else {}),
        )
    )
    event = await audit.record(
        db,
        (
            "expense.statement_import_replayed"
            if operation == "statement_imported" and result.get("replayed")
            else (
                audit.A.REIMBURSE_BATCH
                if operation == "reimbursement_created"
                else (
                    audit.A.REIMBURSE_PAID
                    if operation == "reimbursement_paid"
                    else (
                        audit.A.REIMBURSE_CANCEL
                        if operation == "reimbursement_cancelled"
                        else f"expense.{operation}"
                    )
                )
            )
        ),
        org_id=current.org_id,
        actor=(current.id, current.email),
        target_type={
            "statement_imported": "expense_import_batch",
            "report_created": "expense_report",
            "transaction_binned": "expense_transaction",
            "reimbursement_created": "reimbursement_batch",
            "reimbursement_paid": "reimbursement_batch",
            "reimbursement_cancelled": "reimbursement_batch",
        }[operation],
        target_id=target_id,
        meta={
            "command_key": key,
            **(
                {"reports": result["report_count"], "method": result["method"]}
                if operation == "reimbursement_created"
                else {}
            ),
            **(
                {
                    "reports": result["report_count"],
                    "reference": result["reference"],
                    "total_eur": result["total_eur"],
                    "evidence_basis": "operator_recorded",
                }
                if operation == "reimbursement_paid"
                else {}
            ),
            **(
                {
                    "reports": len(result["released_reports"]),
                    "released_report_ids": [r["id"] for r in result["released_reports"]],
                    "reason": result["reason"],
                }
                if operation == "reimbursement_cancelled"
                else {}
            ),
        },
    )
    if event is None:
        raise AppError(
            "The expense audit could not be recorded. No change was committed.",
            code="expense_audit_unavailable",
            status=503,
        )


async def available(
    db: AsyncSession,
    org_id: str,
    employee_id: str,
    ids: list[str],
    versions: dict[str, int] | None = None,
) -> list[ExpenseTransaction]:
    if not ids:
        return []
    try:
        ids = [str(UUID(value)) for value in ids]
    except ValueError as exc:
        raise ValidationError("Select valid transaction identifiers") from exc
    if len(ids) != len(set(ids)):
        raise ValidationError("Select each transaction once")
    rows = list(
        await db.scalars(
            select(ExpenseTransaction)
            .where(
                ExpenseTransaction.org_id == org_id,
                ExpenseTransaction.employee_id == employee_id,
                ExpenseTransaction.id.in_(ids),
                ExpenseTransaction.status == "available",
            )
            .order_by(ExpenseTransaction.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    if len(rows) != len(set(ids)):
        if versions:
            raise ConflictError(
                "A selected transaction changed or was used. Refresh the inbox and review it.",
                code="expense_selection_conflict",
            )
        raise NotFoundError("One or more transactions were not found or already used")
    if versions and any(versions.get(row.id) != row.version for row in rows):
        raise ConflictError(
            "A selected transaction changed. Refresh its current version before reporting it.",
            code="expense_selection_conflict",
        )
    return rows


def transaction_reference(row: ExpenseTransaction) -> str:
    who = row.merchant or row.description[:60]
    return f"{row.txn_date.isoformat()} · {who} · {row.currency} {expenses.q(row.amount)}"


def require_report_currency(row: ExpenseTransaction, report_currency: str) -> None:
    if currency(row.currency) != report_currency or (
        row.source == "bank_statement"
        and row.import_batch_id is None
        and row.currency_source is None
    ):
        raise ValidationError(
            "Select verified transactions in the report currency only. Review legacy "
            "statement currency against its source; mixed currencies need separate reports.",
            code="expense_currency_mismatch",
        )


async def item_from_transaction(
    db: AsyncSession,
    row: ExpenseTransaction,
    report_currency: str,
    category: str = "other",
    vat: Decimal = Decimal("0"),
) -> ExpenseItem:
    require_report_currency(row, report_currency)
    if vat > row.amount:
        raise ValidationError("VAT cannot exceed the expense amount")
    item = ExpenseItem(
        spend_date=row.txn_date,
        category=category,
        description=row.description[:300],
        merchant=row.merchant,
        amount=expenses.q(row.amount),
        vat_amount=expenses.q(vat),
        currency=row.currency,
        original_amount=expenses.q(row.amount),
        payment_method="company_card" if row.source in ("bank_statement", "card") else "personal",
        bank_reference=transaction_reference(row),
    )
    await expenses.apply_item_fx(db, item, report_currency)
    return item


@dataclass
class ReportOutcome:
    key: str
    payload_hash: str
    report: ExpenseReport | None = None
    previous: dict | None = None


async def create_report(
    db: AsyncSession,
    current: Principal,
    body: ExpenseReportCreate,
    supplied_key: str | None,
) -> ReportOutcome:
    if supplied_key is not None and body.transaction_ids and not body.transaction_versions:
        raise ValidationError(
            "Reload the inbox and send the current version of every selected transaction.",
            code="expense_version_required",
        )
    payload = body.model_dump(mode="json")
    payload["transaction_ids"] = sorted(body.transaction_ids)
    digest = fingerprint(payload)
    key = (
        f"legacy-manual:{new_uuid()}"
        if not body.transaction_ids and supplied_key is None
        else command_key(supplied_key, "report", digest)
    )
    await lock_actor(db, current)
    old = await lookup(
        db, current.org_id, current.id, key, operation="report_created", expected=digest
    )
    if old is not None:
        return ReportOutcome(key, digest, previous=json.loads(old.result_json))
    rows = await available(
        db, current.org_id, current.id, body.transaction_ids, body.transaction_versions
    )
    for row in rows:
        require_report_currency(row, body.currency)
    if rows and any(
        item.currency and currency(item.currency) != body.currency for item in body.items
    ):
        raise ValidationError(
            "Selected-transaction reports accept one currency. Use separate reports.",
            code="expense_currency_mismatch",
        )
    items = await expenses.build_items(db, body.items, body.currency)
    txn_items = [await item_from_transaction(db, row, body.currency) for row in rows]
    items += txn_items
    for item in items:
        await costing.apply_links(db, current.org_id, item, DIMENSION_KEYS)
    total, vat = expenses.compute_totals(items)
    if total > Decimal("999999999999.99") or vat > Decimal("999999999999.99"):
        raise ValidationError(
            "This report exceeds the supported money range. Split the report.",
            code="expense_total_out_of_range",
        )
    report = ExpenseReport(
        org_id=current.org_id,
        employee_id=current.id,
        employee_name=current.name,
        title=body.title,
        currency=body.currency,
        note=body.note,
        total=total,
        vat_total=vat,
        items=items,
    )
    db.add(report)
    await db.flush()
    for row, item in zip(rows, txn_items, strict=True):
        row.status, row.item_id = "assigned", item.id
        row.version += 1
    return ReportOutcome(key, digest, report=report)


async def import_result(
    db: AsyncSession,
    current: Principal,
    batch: ExpenseImportBatch,
    key: str,
    *,
    replayed: bool,
) -> dict:
    with tenant.include_deleted():
        rows = list(
            await db.scalars(
                select(ExpenseTransaction)
                .where(
                    ExpenseTransaction.org_id == current.org_id,
                    ExpenseTransaction.employee_id == current.id,
                    ExpenseTransaction.import_batch_id == batch.id,
                )
                .order_by(ExpenseTransaction.source_row)
                .execution_options(populate_existing=True)
            )
        )
    visible = [row for row in rows if row.deleted_at is None]
    warnings = json.loads(batch.warnings_json)
    if batch.row_count == 0:
        warnings.append(
            "No expense debits were found. Credits are excluded; check the source rows."
        )
    if replayed:
        warnings.append(
            "This exact statement was already imported for this account reference. "
            "No candidates were recreated; use the permitted bin restore for removed rows."
        )
    return {
        "method": batch.method,
        "imported": 0 if replayed else batch.row_count,
        "transactions": [
            ExpenseTransactionOut.model_validate(row).model_dump(mode="json") for row in visible
        ],
        "warnings": warnings,
        "batch_id": batch.id,
        "command_key": key,
        "replayed": replayed,
        "skipped_credits": batch.credit_count,
        "available": sum(row.status == "available" for row in visible),
        "assigned": sum(row.status == "assigned" for row in visible),
        "binned": sum(row.deleted_at is not None for row in rows),
        "unavailable": batch.row_count - len(rows),
    }


async def import_statement(
    db: AsyncSession,
    current: Principal,
    parsed: bank_statement.StatementResult,
    content: bytes,
    account_reference: str | None,
    statement_currency: str | None,
    supplied_key: str | None,
) -> dict:
    account = (account_reference or "").strip()
    if not account or len(account) > 120:
        raise ValidationError(
            "Enter a consistent account reference for this statement (up to120 characters).",
            code="expense_account_required",
        )
    confirmed = currency(statement_currency)
    source_sha = hashlib.sha256(content).hexdigest()
    payload_hash = fingerprint({"sha256": source_sha, "account": account, "currency": confirmed})
    key = command_key(supplied_key, "import", payload_hash)
    await lock_actor(db, current)
    old = await lookup(
        db, current.org_id, current.id, key, operation="statement_imported", expected=payload_hash
    )
    batch = await db.scalar(
        select(ExpenseImportBatch).where(
            ExpenseImportBatch.org_id == current.org_id,
            ExpenseImportBatch.employee_id == current.id,
            ExpenseImportBatch.account_reference == account,
            ExpenseImportBatch.source_sha256 == source_sha,
        )
    )
    if batch is not None:
        if batch.statement_currency != confirmed:
            raise ConflictError(
                "These bytes already have a currency confirmation. Review the original import "
                "instead of changing its currency.",
                code="expense_import_currency_conflict",
            )
        result = await import_result(db, current, batch, key, replayed=True)
        if old is None:
            await record(db, current, key, "statement_imported", payload_hash, batch.id, result)
        return result
    if old is not None:
        raise ConflictError(
            "The committed source import is unavailable. Review its receipt before importing.",
            code="expense_result_unavailable",
        )
    if len(parsed.transactions) > MAX_IMPORT_ROWS:
        raise ValidationError("Split the statement into at most5000 rows per import")
    debits = [(i, row) for i, row in enumerate(parsed.transactions) if row.direction == "debit"]
    resolved = []
    for ordinal, row in debits:
        stated = currency(row.currency)
        if stated is not None and confirmed is not None and stated != confirmed:
            raise ValidationError(
                "The statement contains a different currency. Confirm its source and split "
                "currencies before importing.",
                code="expense_currency_mismatch",
            )
        ccy = stated or confirmed
        if ccy is None:
            raise ValidationError(
                "The statement does not state currency. Confirm it before importing.",
                code="expense_currency_required",
            )
        if not row.amount.is_finite() or not Decimal("0") < row.amount <= Decimal(
            "999999999999.99"
        ):
            raise ValidationError("A statement amount is outside the supported money range")
        resolved.append((ordinal, row, ccy, "statement" if stated else "confirmed"))
    batch = ExpenseImportBatch(
        org_id=current.org_id,
        employee_id=current.id,
        account_reference=account,
        source_sha256=source_sha,
        parser_version=PARSER_VERSION,
        method=parsed.method,
        statement_currency=confirmed,
        row_count=len(debits),
        credit_count=sum(row.direction == "credit" for row in parsed.transactions),
        warnings_json=json.dumps(parsed.warnings),
    )
    db.add(batch)
    await db.flush()
    for ordinal, row, ccy, provenance in resolved:
        db.add(
            ExpenseTransaction(
                org_id=current.org_id,
                employee_id=current.id,
                txn_date=row.date,
                description=row.description[:300],
                amount=expenses.q(row.amount),
                currency=ccy,
                direction="debit",
                source="bank_statement",
                status="available",
                import_batch_id=batch.id,
                source_row=ordinal,
                currency_source=provenance,
            )
        )
    await db.flush()
    result = await import_result(db, current, batch, key, replayed=False)
    await record(db, current, key, "statement_imported", payload_hash, batch.id, result)
    return result


async def remove_transaction(
    db: AsyncSession,
    current: Principal,
    target: str,
    version: int | None,
    supplied_key: str | None,
) -> None:
    try:
        target = str(UUID(target))
    except ValueError as exc:
        raise ValidationError("Use a valid transaction identifier") from exc
    if supplied_key is not None and version is None:
        raise ValidationError(
            "Reload the inbox and send the transaction version before removing.",
            code="expense_version_required",
        )
    digest = fingerprint({"transaction": target, "version": version})
    key = command_key(supplied_key, "remove", digest)
    await lock_actor(db, current)
    old = await lookup(
        db, current.org_id, current.id, key, operation="transaction_binned", expected=digest
    )
    if old is not None:
        return
    with tenant.include_deleted():
        row = await db.scalar(
            select(ExpenseTransaction)
            .where(
                ExpenseTransaction.org_id == current.org_id,
                ExpenseTransaction.employee_id == current.id,
                ExpenseTransaction.id == target,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    if row is None:
        raise NotFoundError("Transaction not found")
    if row.deleted_at is not None or row.status != "available" or row.item_id is not None:
        raise ConflictError(
            "The transaction is binned or assigned. Refresh the inbox and review its outcome.",
            code="expense_selection_conflict",
        )
    if version is not None and row.version != version:
        raise ConflictError(
            "The transaction changed. Refresh it before removing.",
            code="expense_selection_conflict",
        )
    bin_svc.stamp(row, current.email)
    row.version += 1
    await record(
        db,
        current,
        key,
        "transaction_binned",
        digest,
        row.id,
        {"id": row.id, "version": row.version, "state": "binned"},
    )
