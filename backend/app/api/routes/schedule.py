"""Work-planning schedule routes (WO-A, docs/design/work-calendar.md phase A).

Permission shape, decided in the design doc and mirrored here structurally:

- READ is INVOICE_READ — every business role has it, including employees.
  The list endpoint then narrows: without planning rights (INVOICE_WRITE)
  a caller receives only their OWN assignments, which IS the "My work" view.
- PLANNING (create/edit) is INVOICE_WRITE — scheduling people is bookkeeping-
  grade day-to-day work, not org configuration (the costing precedent).
- TRANSITION is INVOICE_READ structurally: the assignee may confirm/finish
  their own assignment; anything further requires planning rights — enforced
  in the service, failing OPAQUELY (§4.4) so probing ids learns nothing.
"""

from __future__ import annotations

from datetime import datetime
from typing import NoReturn

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import or_, select

from app.api.deps import CurrentUser, DbSession, require_perm
from app.core import authz
from app.core.config import settings
from app.core.errors import AppError
from app.models.costing import Project
from app.models.email_message import EmailMessage
from app.schemas.scheduling import (
    AssignmentIn,
    AssignmentOut,
    AssignmentWriteOut,
    PlanningCommandOut,
)
from app.services import audit, ics, planning_commands, schedule_email, scheduling, team

router = APIRouter(
    prefix="/schedule",
    tags=["schedule"],
    dependencies=[Depends(require_perm(authz.Permission.INVOICE_READ))],
)

_PLANNING = [Depends(require_perm(authz.Permission.INVOICE_WRITE))]


class AssignmentPatch(BaseModel):
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    all_day: bool | None = None
    note: str | None = Field(default=None, max_length=2000)
    clear_note: bool = False
    assignee_user_id: str | None = None
    remind_hours_before: int | None = Field(default=None, ge=1, le=336)
    set_remind: bool = False
    client_notice_hours_before: int | None = Field(default=None, ge=1, le=336)
    set_client_notice: bool = False
    time_zone: str | None = Field(default=None, min_length=1, max_length=64)
    notify_assignee: bool | None = None
    reminders_enabled: bool | None = None
    client_notice_enabled: bool | None = None
    expected_revision: int | None = Field(default=None, ge=1)


class TransitionIn(BaseModel):
    status: str
    expected_revision: int | None = Field(default=None, ge=1)


class MemberOut(BaseModel):
    user_id: str
    email: str
    name: str | None


def _out(a, projects: dict | None = None, notices: dict | None = None) -> AssignmentOut:
    return AssignmentOut(
        id=a.id,
        project_id=a.project_id,
        project_code=projects[a.project_id].code if projects and a.project_id in projects else None,
        project_name=projects[a.project_id].name if projects and a.project_id in projects else None,
        assignee_user_id=a.assignee_user_id,
        assignee_email=a.assignee_email,
        starts_at=a.starts_at.isoformat(),
        ends_at=a.ends_at.isoformat(),
        all_day=a.all_day,
        status=a.status,
        note=a.note,
        created_by=a.created_by,
        remind_hours_before=a.remind_hours_before,
        client_notice_hours_before=a.client_notice_hours_before,
        time_zone=a.time_zone,
        revision=a.revision,
        notify_assignee=a.notify_assignee,
        reminders_enabled=a.reminders_enabled,
        client_notice_enabled=a.client_notice_enabled,
        notification_status="off" if not a.notify_assignee else (notices or {}).get(a.id),
    )


async def _outputs(db, rows) -> list[AssignmentOut]:
    if not rows:
        return []
    org_id = rows[0].org_id
    projects = {
        p.id: p
        for p in await db.scalars(
            select(Project).where(
                Project.org_id == org_id,
                Project.id.in_({row.project_id for row in rows}),
            )
        )
    }
    notices: dict[str, str] = {}
    messages = await db.execute(
        select(EmailMessage.command_key, EmailMessage.status)
        .where(
            EmailMessage.org_id == org_id,
            EmailMessage.kind == "assignment",
            ~EmailMessage.command_key.contains(":reassigned:"),
            or_(
                *(
                    EmailMessage.command_key.startswith(schedule_email.prefix(row.id, row.revision))
                    for row in rows
                )
            ),
        )
        .order_by(EmailMessage.created_at.desc())
    )
    for key, state in messages:
        if key is not None:
            notices.setdefault(key.split(":")[1], state)
    return [_out(row, projects, notices) for row in rows]


async def _command_outputs(db, rows) -> list[AssignmentOut]:
    # The new receipt/current-read contract restores the known UTC convention
    # SQLite drops from stored timestamps. Preserve legacy route serializers.
    return [
        output.model_copy(
            update={
                "starts_at": scheduling._as_utc(
                    datetime.fromisoformat(output.starts_at)
                ).isoformat(),
                "ends_at": scheduling._as_utc(datetime.fromisoformat(output.ends_at)).isoformat(),
            }
        )
        for output in await _outputs(db, rows)
    ]


def _raise(exc: scheduling.SchedulingError) -> NoReturn:
    if isinstance(exc, scheduling.StaleError):
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    if isinstance(exc, scheduling.NotFoundError):
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


@router.get("/assignments", response_model=list[AssignmentOut])
async def list_assignments(
    start: datetime,
    end: datetime,
    current: CurrentUser,
    db: DbSession,
    assignee_user_id: str | None = None,
    project_id: str | None = None,
):
    """The calendar read. Planners see the workspace; everyone else sees
    exactly their own rows regardless of the filters they pass."""
    if not authz.has(current, authz.Permission.INVOICE_WRITE):
        assignee_user_id = current.id
    rows = await scheduling.list_window(
        db,
        current.org_id,
        start=start,
        end=end,
        assignee_user_id=assignee_user_id,
        project_id=project_id,
    )
    return await _outputs(db, rows)


@router.get("/members", response_model=list[MemberOut], dependencies=_PLANNING)
async def plannable_members(current: CurrentUser, db: DbSession):
    """The assignee picker. Planning-rights-gated (a finance manager holds no
    MEMBER_READ; this exposes exactly what a planner needs: who can be
    scheduled, nothing about roles or invitations)."""
    members = await team.list_members(db, current.org_id)
    return [
        MemberOut(user_id=m.user_id, email=m.email or "", name=m.name)
        for m in members
        if m.status == "active"
    ]


@router.post(
    "/assignments",
    response_model=AssignmentWriteOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=_PLANNING,
)
async def create_assignment(
    body: AssignmentIn,
    current: CurrentUser,
    db: DbSession,
    command_key: str | None = Header(
        default=None, alias="Idempotency-Key", min_length=1, max_length=120
    ),
):
    command = await planning_commands.prepare_create(db, current, body, command_key)
    if command is not None and command.previous is not None:
        return command.previous
    try:
        row, overlaps = await scheduling.create(
            db,
            current.org_id,
            project_id=body.project_id,
            assignee_user_id=body.assignee_user_id,
            starts_at=body.starts_at,
            ends_at=body.ends_at,
            all_day=body.all_day,
            note=body.note,
            created_by=current.email,
            remind_hours_before=body.remind_hours_before,
            client_notice_hours_before=body.client_notice_hours_before,
            time_zone=body.time_zone,
            notify_assignee=body.notify_assignee,
            reminders_enabled=body.reminders_enabled,
            client_notice_enabled=body.client_notice_enabled,
        )
    except scheduling.SchedulingError as exc:
        _raise(exc)
    event = await audit.record(
        db,
        "assignment.create",
        target_type="project",
        target_id=row.project_id,
        meta={
            "assignment_id": row.id,
            "assignee": row.assignee_email,
            "starts_at": row.starts_at.isoformat(),
            "ends_at": row.ends_at.isoformat(),
            "revision": row.revision,
            "time_zone": row.time_zone,
            "notify_assignee": row.notify_assignee,
            "reminders_enabled": row.reminders_enabled,
            "client_notice_enabled": row.client_notice_enabled,
            **({"command_key": command.key} if command is not None else {}),
        },
    )
    if event is None:
        if command is not None:
            raise AppError(
                "The planning audit could not be recorded. No assignment was committed.",
                code="planning_command_unavailable",
                status=503,
            )
        raise RuntimeError("Assignment audit could not be recorded.")
    if command is not None:
        outputs = await _command_outputs(db, [row, *overlaps])
        result = AssignmentWriteOut(assignment=outputs[0], overlaps=outputs[1:])
        await planning_commands.record_create(db, current, command, result)
        await db.commit()
        return result
    await db.commit()
    await db.refresh(row)
    outputs = await _outputs(db, [row, *overlaps])
    return AssignmentWriteOut(assignment=outputs[0], overlaps=outputs[1:])


@router.get("/commands/{command_key}", response_model=PlanningCommandOut, dependencies=_PLANNING)
async def planning_command_status(command_key: str, current: CurrentUser, db: DbSession):
    return await planning_commands.status(db, current, command_key)


@router.get("/assignments/{assignment_id}", response_model=AssignmentOut, dependencies=_PLANNING)
async def current_assignment(assignment_id: str, current: CurrentUser, db: DbSession):
    await planning_commands.lock_memberships(db, current)
    try:
        row = await scheduling.get(db, current.org_id, assignment_id)
    except scheduling.SchedulingError as exc:
        _raise(exc)
    return (await _command_outputs(db, [row]))[0]


@router.patch(
    "/assignments/{assignment_id}", response_model=AssignmentWriteOut, dependencies=_PLANNING
)
async def update_assignment(
    assignment_id: str, body: AssignmentPatch, current: CurrentUser, db: DbSession
):
    try:
        row, overlaps = await scheduling.update(
            db,
            current.org_id,
            assignment_id,
            starts_at=body.starts_at,
            ends_at=body.ends_at,
            all_day=body.all_day,
            note=body.note,
            clear_note=body.clear_note,
            assignee_user_id=body.assignee_user_id,
            remind_hours_before=body.remind_hours_before,
            set_remind=body.set_remind,
            client_notice_hours_before=body.client_notice_hours_before,
            set_client_notice=body.set_client_notice,
            time_zone=body.time_zone,
            notify_assignee=body.notify_assignee,
            reminders_enabled=body.reminders_enabled,
            client_notice_enabled=body.client_notice_enabled,
            set_client_notice_enabled="client_notice_enabled" in body.model_fields_set,
            expected_revision=body.expected_revision,
        )
    except scheduling.SchedulingError as exc:
        _raise(exc)
    event = await audit.record(
        db,
        "assignment.update",
        target_type="project",
        target_id=row.project_id,
        meta={
            "assignment_id": row.id,
            "assignee": row.assignee_email,
            "starts_at": row.starts_at.isoformat(),
            "ends_at": row.ends_at.isoformat(),
            "revision": row.revision,
            "time_zone": row.time_zone,
            "notify_assignee": row.notify_assignee,
            "reminders_enabled": row.reminders_enabled,
            "client_notice_enabled": row.client_notice_enabled,
        },
    )
    if event is None:
        raise RuntimeError("Assignment audit could not be recorded.")
    await db.commit()
    await db.refresh(row)
    outputs = await _outputs(db, [row, *overlaps])
    return AssignmentWriteOut(assignment=outputs[0], overlaps=outputs[1:])


@router.post("/assignments/{assignment_id}/transition", response_model=AssignmentOut)
async def transition_assignment(
    assignment_id: str, body: TransitionIn, current: CurrentUser, db: DbSession
):
    """Lifecycle move. Structurally INVOICE_READ: an assignee confirms or
    finishes THEIR OWN assignment; every other move needs planning rights
    (enforced in the service, opaque on failure)."""
    try:
        row = await scheduling.transition(
            db,
            current.org_id,
            assignment_id,
            body.status,
            actor_user_id=current.id,
            actor_may_plan=authz.has(current, authz.Permission.INVOICE_WRITE),
            expected_revision=body.expected_revision,
        )
    except scheduling.SchedulingError as exc:
        _raise(exc)
    event = await audit.record(
        db,
        "assignment.transition",
        target_type="project",
        target_id=row.project_id,
        meta={
            "assignment_id": row.id,
            "status": row.status,
            "assignee": row.assignee_email,
            "revision": row.revision,
        },
    )
    if event is None:
        raise RuntimeError("Assignment audit could not be recorded.")
    await db.commit()
    await db.refresh(row)
    return (await _outputs(db, [row]))[0]


async def _notice_assignment(db, current, assignment_id: str, *, for_update: bool = False):
    try:
        row = await scheduling.get(db, current.org_id, assignment_id, for_update=for_update)
    except scheduling.SchedulingError as exc:
        _raise(exc)
    if row.assignee_user_id != current.id and not authz.has(
        current, authz.Permission.INVOICE_WRITE
    ):
        raise HTTPException(404, "assignment not found")
    return row


def _notice_out(message: EmailMessage, row) -> dict:
    prefix = schedule_email.prefix(row.id, row.revision)
    category = (message.command_key or "").removeprefix(prefix).split(":", 1)[0]
    return {
        "id": message.id,
        "kind": message.kind,
        "status": message.status,
        "error": message.error,
        "sent_at": message.sent_at.isoformat() if message.sent_at else None,
        "created_at": message.created_at.isoformat(),
        "can_retry": settings.smtp_enabled
        and row.notify_assignee
        and message.status in {"blocked", "failed"}
        and (message.command_key or "").startswith(prefix)
        and category in {"assigned", "changed", "cancelled"},
    }


class AssignmentNotificationOut(BaseModel):
    id: str
    kind: str
    status: str
    error: str | None
    sent_at: str | None
    created_at: str
    can_retry: bool


class AssignmentNotificationsOut(BaseModel):
    smtp_configured: bool
    items: list[AssignmentNotificationOut]


@router.get("/assignments/{assignment_id}/notifications", response_model=AssignmentNotificationsOut)
async def assignment_notifications(assignment_id: str, current: CurrentUser, db: DbSession):
    row = await _notice_assignment(db, current, assignment_id)
    messages = await db.scalars(
        select(EmailMessage)
        .where(
            EmailMessage.org_id == current.org_id,
            EmailMessage.command_key.startswith(schedule_email.prefix(row.id)),
        )
        .order_by(EmailMessage.created_at.desc())
        .limit(50)
    )
    return {
        "smtp_configured": settings.smtp_enabled,
        "items": [_notice_out(m, row) for m in messages],
    }


@router.post(
    "/assignments/{assignment_id}/notifications/{message_id}/retry",
    response_model=AssignmentNotificationOut,
)
async def retry_assignment_notification(
    assignment_id: str, message_id: str, current: CurrentUser, db: DbSession
):
    row = await _notice_assignment(db, current, assignment_id, for_update=True)
    try:
        message = await schedule_email.retry(db, row, message_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    event = await audit.record(
        db,
        "assignment.notification_retry",
        target_type="project",
        target_id=row.project_id,
        meta={"assignment_id": row.id, "message_id": message.id, "revision": row.revision},
    )
    if event is None:
        raise RuntimeError("Assignment audit could not be recorded.")
    await db.commit()
    return _notice_out(message, row)


class FeedTokenOut(BaseModel):
    """The subscription URL parts. The token is a secret CAPABILITY —
    regenerating kills the old URL instantly."""

    token: str
    path: str


def _feed_out(token: str) -> FeedTokenOut:
    return FeedTokenOut(token=token, path=f"/api/v1/calendar/feed/{token}.ics")


@router.get("/feed-token", response_model=FeedTokenOut)
async def my_feed_token(current: CurrentUser, db: DbSession):
    """Get (or lazily create) YOUR calendar-feed token — INVOICE_READ, so an
    employee can put their own schedule on their own phone."""
    token = await scheduling.get_or_create_feed_token(db, current.org_id, current.id)
    await db.commit()
    return _feed_out(token)


@router.post("/feed-token/regenerate", response_model=FeedTokenOut)
async def regenerate_feed_token(current: CurrentUser, db: DbSession):
    token = await scheduling.regenerate_feed_token(db, current.org_id, current.id)
    await audit.record(db, "assignment.feed_regenerate", target_type="user", target_id=current.id)
    await db.commit()
    return _feed_out(token)


@router.get("/export.ics")
async def export_ics(current: CurrentUser, db: DbSession):
    """One-off .ics download of YOUR schedule (the feed is the live version)."""
    rows, names = await scheduling.feed_rows(db, current.org_id, current.id)
    return Response(
        content=ics.render(rows, names),
        media_type="text/calendar",
        headers={"Content-Disposition": 'attachment; filename="schedule.ics"'},
    )
