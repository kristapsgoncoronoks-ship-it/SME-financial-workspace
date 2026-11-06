"""One reviewed bank-line transition, immutable outcome and audit in one transaction.

Membership -> source line -> cash target -> tenant audit is the write lock order.
The request's refresh callback validates credentials without updating Session, and
runs only while the session has no pending domain changes. Commit transport loss
is an unknown outcome: the durable actor-owned key is the recovery mechanism.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

from pydantic import ValidationError as SchemaError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import authz
from app.core.errors import AppError, ConflictError, NotFoundError, PermissionError, ValidationError
from app.core.identity import Principal
from app.models.bank_import import BankLine
from app.models.base import new_uuid
from app.models.membership import Membership
from app.models.reconciliation_command import MAX_VERSION, ReconciliationCommand
from app.schemas.reconciliation_commands import (
    CommandIntent,
    CommandReceipt,
    CurrentLine,
    FrozenLine,
    FrozenResult,
    HistoryEvent,
    HistoryPage,
    LineState,
)
from app.services import audit, modules, reconciliation

Refresh = Callable[[], Awaitable[Principal]]


def unavailable() -> AppError:
    return AppError(
        "The command outcome is unavailable. Check the recorded outcome using the same key before trying another intention.",
        status=503,
        code="reconciliation_command_unavailable",
    )


def command_key(value: str | None) -> str:
    if value is None:
        raise AppError(
            "A reviewed version and command key are required.",
            status=428,
            code="reconciliation_command_preconditions_required",
        )
    if re.fullmatch(r"[A-Za-z0-9:_-]{1,120}", value) is None:
        raise ValidationError("Invalid command key", code="reconciliation_command_key_invalid")
    return value


def canonical(value: dict) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def payload(line_id: str, intent: CommandIntent) -> str:
    value = canonical(
        {"contract_version": 1, "line_id": str(UUID(line_id)), **intent.model_dump(mode="json")}
    )
    if len(value.encode("utf-8")) > 8192:
        raise ValidationError("The command is too large", code="reconciliation_command_too_large")
    return value


def state(line: BankLine) -> LineState:
    try:
        return LineState.model_validate(
            {
                "status": line.status,
                "matched_kind": line.matched_kind,
                "matched_id": line.matched_id,
            }
        )
    except (SchemaError, ValueError, TypeError):
        raise unavailable() from None


def _time(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def receipt(row: ReconciliationCommand) -> CommandReceipt:
    """Treat stored outcome corruption as unavailable, never as a missing command."""
    try:
        stored_payload = json.loads(row.payload_json)
        stored_result = json.loads(row.result_json)
        if not isinstance(stored_payload, dict) or not isinstance(stored_result, dict):
            raise ValueError("shape")
        if (
            canonical(stored_payload) != row.payload_json
            or canonical(stored_result) != row.result_json
        ):
            raise ValueError("encoding")
        if (
            fingerprint(row.payload_json) != row.payload_hash
            or fingerprint(row.result_json) != row.result_hash
        ):
            raise ValueError("digest")
        if stored_payload.pop("contract_version") != 1:
            raise ValueError("contract")
        payload_line = str(UUID(stored_payload.pop("line_id")))
        intent = CommandIntent.model_validate(stored_payload)
        result = FrozenResult.model_validate(stored_result)
        before, after = result.before.model_dump(mode="json"), result.after.model_dump(mode="json")
        expected_after = (
            {
                "status": "matched",
                "matched_kind": intent.target.kind,
                "matched_id": str(intent.target.id),
            }
            if intent.target is not None
            else {
                "status": "ignored" if intent.operation == "ignore" else "unmatched",
                "matched_kind": None,
                "matched_id": None,
            }
        )
        if any(
            (
                row.contract_version != 1,
                str(result.command_id) != row.id,
                result.command_key != row.command_key,
                str(result.org_id) != row.org_id,
                str(result.actor_user_id) != row.actor_user_id,
                str(result.line_id) != row.line_id,
                payload_line != row.line_id,
                intent.operation != row.operation or result.operation != row.operation,
                result.payload_hash != row.payload_hash,
                intent.expected_version != row.expected_version,
                row.expected_version != row.before_version,
                row.before_version != result.before_version,
                row.after_version != result.after_version,
                row.after_version != row.before_version + 1,
                before != intent.expected.model_dump(mode="json"),
                after != expected_after,
                canonical(before) != row.before_state_json,
                canonical(after) != row.after_state_json,
                result.reason != row.reason or intent.reason != row.reason,
                str(result.line.id) != row.line_id or result.line.version != row.after_version,
                LineState.model_validate(
                    {name: getattr(result.line, name) for name in LineState.model_fields}
                ).model_dump(mode="json")
                != after,
                _time(result.recorded_at) != _time(row.recorded_at),
            )
        ):
            raise ValueError("identity")
        return CommandReceipt(**result.model_dump(), result_hash=row.result_hash)
    except (ValueError, TypeError, KeyError, UnicodeError, SchemaError, AttributeError):
        raise unavailable() from None


async def lock_actor(
    db: AsyncSession, current: Principal, permission: authz.Permission
) -> Principal:
    member = await db.scalar(
        select(Membership)
        .where(Membership.org_id == current.org_id, Membership.user_id == current.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if member is None or member.status != "active":
        raise PermissionError("Reconciliation access is unavailable. Refresh your account.")
    fresh = replace(current, role=member.role, is_expense_approver=member.is_expense_approver)
    await admission(db, fresh, permission)
    return fresh


async def admission(db: AsyncSession, current: Principal, permission: authz.Permission) -> None:
    if not authz.has(current, permission):
        raise PermissionError("Reconciliation access is unavailable. Refresh your account.")
    if not await modules.is_enabled(db, current.org_id, "issuing"):
        raise PermissionError("The Invoice issuing module is not activated.")


async def refresh_admission(
    db: AsyncSession, current: Principal, refresh: Refresh, permission: authz.Permission
) -> Principal:
    # refresh_current_identity flushes before expiring: no pending domain work may
    # reach it. It performs read-only live Session selection (touch_session=False).
    if db.new or db.dirty or db.deleted:
        raise RuntimeError("Reconciliation identity refresh requires a clean transaction")
    fresh = await refresh()
    if (fresh.id, fresh.org_id) != (current.id, current.org_id):
        raise PermissionError("The active workspace changed. Refresh your account.")
    await admission(db, fresh, permission)
    return fresh


async def lookup(db: AsyncSession, current: Principal, key: str) -> ReconciliationCommand | None:
    return await db.scalar(
        select(ReconciliationCommand)
        .where(
            ReconciliationCommand.org_id == current.org_id,
            ReconciliationCommand.actor_user_id == current.id,
            ReconciliationCommand.command_key == key,
        )
        .execution_options(populate_existing=True)
    )


async def _line(
    db: AsyncSession, current: Principal, line_id: str, *, lock: bool = False
) -> BankLine:
    query = select(BankLine).where(BankLine.org_id == current.org_id, BankLine.id == line_id)
    if lock:
        query = query.with_for_update()
    line = await db.scalar(query.execution_options(populate_existing=True))
    if line is None:
        raise NotFoundError("Bank line not found")
    return line


async def rollback(db: AsyncSession) -> None:
    # Keep ownership until rollback finishes even if a caller is cancelled again.
    task = asyncio.create_task(db.rollback())
    while True:
        try:
            await asyncio.shield(task)
            return
        except asyncio.CancelledError:
            if task.done():
                task.result()
                return


async def execute(
    db: AsyncSession,
    current: Principal,
    line_id: str,
    key: str,
    intent: CommandIntent,
    refresh: Refresh,
) -> CommandReceipt:
    line_id = str(UUID(line_id))
    value = payload(line_id, intent)
    digest = fingerprint(value)
    committing = False
    try:
        current = await lock_actor(db, current, authz.Permission.PAYMENT_WRITE)
        current = await refresh_admission(db, current, refresh, authz.Permission.PAYMENT_WRITE)
        previous = await lookup(db, current, key)
        if previous is not None:
            replayed = receipt(previous)
            if (
                previous.line_id != line_id
                or previous.operation != intent.operation
                or previous.payload_hash != digest
            ):
                raise ConflictError(
                    "This key already recorded a different intention.",
                    code="reconciliation_command_payload_conflict",
                )
            await refresh_admission(db, current, refresh, authz.Permission.PAYMENT_WRITE)
            await db.rollback()
            return replayed
        line = await _line(db, current, line_id, lock=True)
        before = state(line)
        if line.version != intent.expected_version or before != intent.expected:
            raise ConflictError(
                "The bank line changed. Review its current state.", code="reconciliation_line_stale"
            )
        if line.version == MAX_VERSION:
            raise ConflictError(
                "The bank line version is exhausted.", code="reconciliation_version_exhausted"
            )
        if intent.target is not None:
            try:
                await reconciliation.validate_match(
                    db, current.org_id, line, intent.target.kind, str(intent.target.id)
                )
            except reconciliation.ReconError as exc:
                raise AppError(str(exc), code="reconciliation_match_invalid", status=400) from None
        current = await refresh_admission(db, current, refresh, authz.Permission.PAYMENT_WRITE)
        # The helper expired every ORM row. Reload using pinned primitive IDs;
        # source and target locks remain held until the one transaction ends.
        line = await _line(db, current, line_id, lock=True)
        if line.version != intent.expected_version or state(line) != intent.expected:
            raise ConflictError(
                "The bank line changed. Review its current state.", code="reconciliation_line_stale"
            )
        after = (
            LineState(
                status="matched", matched_kind=intent.target.kind, matched_id=intent.target.id
            )
            if intent.target
            else LineState(status="ignored" if intent.operation == "ignore" else "unmatched")
        )
        changed = await db.execute(
            update(BankLine)
            .where(
                BankLine.org_id == current.org_id,
                BankLine.id == line_id,
                BankLine.version == intent.expected_version,
                BankLine.status == line.status,
                BankLine.matched_kind.is_(None)
                if line.matched_kind is None
                else BankLine.matched_kind == line.matched_kind,
                BankLine.matched_id.is_(None)
                if line.matched_id is None
                else BankLine.matched_id == line.matched_id,
            )
            .values(
                version=BankLine.version + 1,
                status=after.status,
                matched_kind=after.matched_kind,
                matched_id=str(after.matched_id) if after.matched_id else None,
            )
            .returning(BankLine.id)
            .execution_options(synchronize_session=False)
        )
        if changed.scalar_one_or_none() is None:
            raise ConflictError(
                "The bank line changed. Review its current state.", code="reconciliation_line_stale"
            )
        command_id, recorded_at = new_uuid(), datetime.now(UTC)
        result = FrozenResult(
            command_id=UUID(command_id),
            command_key=key,
            org_id=UUID(current.org_id),
            actor_user_id=UUID(current.id),
            line_id=UUID(line_id),
            operation=intent.operation,
            payload_hash=digest,
            before_version=intent.expected_version,
            after_version=intent.expected_version + 1,
            before=before,
            after=after,
            reason=intent.reason,
            recorded_at=recorded_at,
            line=FrozenLine(
                id=UUID(line_id),
                statement_id=UUID(line.statement_id),
                version=intent.expected_version + 1,
                **after.model_dump(),
            ),
        )
        result_value = canonical(result.model_dump(mode="json"))
        result_hash = fingerprint(result_value)
        db.add(
            ReconciliationCommand(
                id=command_id,
                org_id=current.org_id,
                actor_user_id=current.id,
                line_id=line_id,
                command_key=key,
                contract_version=1,
                operation=intent.operation,
                payload_json=value,
                payload_hash=digest,
                result_json=result_value,
                result_hash=result_hash,
                expected_version=intent.expected_version,
                before_version=intent.expected_version,
                after_version=intent.expected_version + 1,
                before_state_json=canonical(before.model_dump(mode="json")),
                after_state_json=canonical(after.model_dump(mode="json")),
                reason=intent.reason,
                recorded_at=recorded_at,
            )
        )
        # Flush the owned outcome before audit.record: an actual uniqueness
        # failure belongs to our rollback/fresh-read path, and must not send the
        # source-owned reason/payload into audit's generic SQL error logger.
        await db.flush()
        try:
            event = await audit.record(
                db,
                "reconciliation.command_committed",
                org_id=current.org_id,
                actor=(current.id, current.email),
                target_type="bank_line",
                target_id=line_id,
                meta={
                    "command_id": command_id,
                    "payload_hash": digest,
                    "result_hash": result_hash,
                    "operation": intent.operation,
                    "before_version": intent.expected_version,
                    "after_version": intent.expected_version + 1,
                    "before": before.model_dump(mode="json"),
                    "after": after.model_dump(mode="json"),
                    "reason_hash": fingerprint(intent.reason)
                    if intent.reason is not None
                    else None,
                },
            )
        except IntegrityError:
            raise
        except Exception:
            raise unavailable() from None
        if event is None:
            raise unavailable()
        await db.flush()
        response = CommandReceipt(**result.model_dump(), result_hash=result_hash)
        committing = True
        await db.commit()
        return response
    except IntegrityError:
        await rollback(db)
        if committing:
            raise unavailable() from None
        # A failed transaction must be discarded before a fresh authorized read.
        await lock_actor(db, current, authz.Permission.PAYMENT_WRITE)
        await refresh_admission(db, current, refresh, authz.Permission.PAYMENT_WRITE)
        row = await lookup(db, current, key)
        if row is None:
            await rollback(db)
            raise unavailable() from None
        response = receipt(row)
        if (
            row.line_id != line_id
            or row.payload_hash != digest
            or row.operation != intent.operation
        ):
            await rollback(db)
            raise ConflictError(
                "This key already recorded a different intention.",
                code="reconciliation_command_payload_conflict",
            ) from None
        await refresh_admission(db, current, refresh, authz.Permission.PAYMENT_WRITE)
        await rollback(db)
        return response
    except SQLAlchemyError:
        # SQL errors may contain the source-owned payload as bound parameters.
        # Keep this command failure out of generic exception/SQL payload logs.
        await rollback(db)
        raise unavailable() from None
    except BaseException as exc:
        await rollback(db)
        if committing and isinstance(exc, Exception):
            raise unavailable() from None
        raise


async def own_outcome(
    db: AsyncSession, current: Principal, line_id: str, key: str, refresh: Refresh
) -> CommandReceipt:
    await admission(db, current, authz.Permission.PAYMENT_READ)
    row = await lookup(db, current, key)
    if row is None or row.line_id != str(UUID(line_id)):
        raise NotFoundError("Command outcome not found", code="reconciliation_command_not_found")
    result = receipt(row)
    await refresh_admission(db, current, refresh, authz.Permission.PAYMENT_READ)
    return result


async def current_line(
    db: AsyncSession, current: Principal, line_id: str, refresh: Refresh
) -> CurrentLine:
    await admission(db, current, authz.Permission.PAYMENT_READ)
    result = CurrentLine.model_validate(await _line(db, current, str(UUID(line_id))))
    await refresh_admission(db, current, refresh, authz.Permission.PAYMENT_READ)
    return result


async def history(
    db: AsyncSession,
    current: Principal,
    line_id: str,
    refresh: Refresh,
    *,
    limit: int = 20,
    before_version: int | None = None,
) -> HistoryPage:
    await admission(db, current, authz.Permission.PAYMENT_READ)
    await _line(db, current, str(UUID(line_id)))
    query = select(ReconciliationCommand).where(
        ReconciliationCommand.org_id == current.org_id,
        ReconciliationCommand.line_id == str(UUID(line_id)),
    )
    if before_version is not None:
        query = query.where(ReconciliationCommand.after_version < before_version)
    rows = list(
        await db.scalars(
            query.order_by(ReconciliationCommand.after_version.desc()).limit(limit + 1)
        )
    )
    events = []
    for row in rows[:limit]:
        verified = receipt(row)
        events.append(
            HistoryEvent(**{name: getattr(verified, name) for name in HistoryEvent.model_fields})
        )
    result = HistoryPage(
        events=events, next_before_version=events[-1].after_version if len(rows) > limit else None
    )
    await refresh_admission(db, current, refresh, authz.Permission.PAYMENT_READ)
    return result
