"""Planning creation identity and receipts; the route owns its transaction."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import authz
from app.core.errors import ConflictError, NotFoundError, PermissionError, ValidationError
from app.core.identity import Principal
from app.models.membership import Membership
from app.models.planning_command import PlanningCommand
from app.schemas.scheduling import AssignmentIn, AssignmentWriteOut, PlanningCommandOut
from app.services.issued_commands import fingerprint

OPERATION = "assignment_created"


def command_key(value: str) -> str:
    value = value.strip()
    if re.fullmatch(r"[A-Za-z0-9:_-]{1,120}", value) is None:
        raise ValidationError(
            "Use a nonblank planning command key of at most 120 characters.",
            code="planning_command_key_invalid",
        )
    return value


async def lock_memberships(
    db: AsyncSession, current: Principal, assignee_id: str | None = None
) -> None:
    # Cross-assigning planners take their two memberships in the same order,
    # avoiding actor-A/assignee-B versus actor-B/assignee-A lock inversion.
    ids = sorted({current.id, *([assignee_id] if assignee_id else [])})
    rows = list(
        await db.scalars(
            select(Membership)
            .where(Membership.org_id == current.org_id, Membership.user_id.in_(ids))
            .order_by(Membership.user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    actor = next((row for row in rows if row.user_id == current.id), None)
    if (
        actor is None
        or actor.status != "active"
        or not authz.has(
            replace(current, role=actor.role, is_expense_approver=actor.is_expense_approver),
            authz.Permission.INVOICE_WRITE,
        )
    ):
        raise PermissionError(
            "Planning access is unavailable. Refresh your account or ask an administrator."
        )
    # scheduling.create owns the existing opaque assignee refusal. The selected
    # membership, if present, remains locked through that check and commit.


@dataclass
class CreateCommand:
    key: str
    payload: AssignmentIn
    payload_hash: str
    previous: AssignmentWriteOut | None = None


async def prepare_create(
    db: AsyncSession, current: Principal, body: AssignmentIn, supplied_key: str | None
) -> CreateCommand | None:
    if supplied_key is None:
        # An old ambiguous create is never silently reinterpreted as replayable.
        return None
    key = command_key(supplied_key)
    digest = fingerprint(body.model_dump(mode="json"))
    await lock_memberships(db, current, body.assignee_user_id)
    previous = await db.scalar(
        select(PlanningCommand).where(
            PlanningCommand.org_id == current.org_id,
            PlanningCommand.actor_user_id == current.id,
            PlanningCommand.command_key == key,
        )
    )
    if previous is not None and previous.payload_hash != digest:
        raise ConflictError(
            "This planning request committed with different inputs. Check its original result before starting another assignment.",
            code="planning_command_payload_conflict",
        )
    return CreateCommand(
        key,
        body,
        digest,
        AssignmentWriteOut.model_validate(json.loads(previous.result_json))
        if previous is not None
        else None,
    )


async def record_create(
    db: AsyncSession, current: Principal, command: CreateCommand, result: AssignmentWriteOut
) -> None:
    db.add(
        PlanningCommand(
            org_id=current.org_id,
            actor_user_id=current.id,
            command_key=command.key,
            payload_hash=command.payload_hash,
            payload_json=json.dumps(
                command.payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            ),
            assignment_id=result.assignment.id,
            result_json=json.dumps(
                result.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            ),
        )
    )
    await db.flush()


async def status(db: AsyncSession, current: Principal, supplied_key: str) -> PlanningCommandOut:
    key = command_key(supplied_key)
    await lock_memberships(db, current)
    row = await db.scalar(
        select(PlanningCommand).where(
            PlanningCommand.org_id == current.org_id,
            PlanningCommand.actor_user_id == current.id,
            PlanningCommand.command_key == key,
        )
    )
    if row is None:
        raise NotFoundError(
            "No committed planning result is visible yet. Check again or explicitly retry the original key and inputs.",
            code="planning_command_not_found",
        )
    return PlanningCommandOut(
        id=row.id,
        org_id=row.org_id,
        actor_user_id=row.actor_user_id,
        command_key=row.command_key,
        target_id=row.assignment_id,
        payload=AssignmentIn.model_validate(json.loads(row.payload_json)),
        result=AssignmentWriteOut.model_validate(json.loads(row.result_json)),
    )
