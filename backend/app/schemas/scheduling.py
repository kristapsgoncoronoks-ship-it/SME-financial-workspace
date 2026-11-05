"""Typed planning inputs and immutable command outcomes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class AssignmentIn(BaseModel):
    project_id: str = Field(min_length=1, max_length=120)
    assignee_user_id: str = Field(min_length=1, max_length=120)
    starts_at: datetime
    ends_at: datetime
    all_day: bool = False
    note: str | None = Field(default=None, max_length=2000)
    remind_hours_before: int | None = Field(default=None, ge=1, le=336)
    client_notice_hours_before: int | None = Field(default=None, ge=1, le=336)
    time_zone: str = Field(default="UTC", min_length=1, max_length=64)
    notify_assignee: bool = True
    reminders_enabled: bool = True
    client_notice_enabled: bool | None = None

    @field_validator("project_id", "assignee_user_id")
    @classmethod
    def canonical_id(cls, value: str) -> str:
        return str(UUID(value.strip()))

    @field_validator("starts_at", "ends_at")
    @classmethod
    def canonical_instant(cls, value: datetime) -> datetime:
        # Preserve the service's legacy naive-is-UTC convention. Never resolve
        # reminder due times or workspace defaults into command identity.
        return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)


class AssignmentOut(BaseModel):
    id: str
    project_id: str
    project_code: str | None = None
    project_name: str | None = None
    assignee_user_id: str
    assignee_email: str
    starts_at: str
    ends_at: str
    all_day: bool
    status: str
    note: str | None
    created_by: str
    remind_hours_before: int | None = None
    client_notice_hours_before: int | None = None
    time_zone: str
    revision: int
    notify_assignee: bool
    reminders_enabled: bool
    client_notice_enabled: bool | None
    notification_status: str | None = None


class AssignmentWriteOut(BaseModel):
    """A write's result plus the ADVISORY overlap warnings (never blocking)."""

    assignment: AssignmentOut
    overlaps: list[AssignmentOut]


class PlanningCommandOut(BaseModel):
    id: str
    org_id: str
    actor_user_id: str
    command_key: str
    operation: Literal["assignment_created"] = "assignment_created"
    status: Literal["committed"] = "committed"
    target_id: str
    payload: AssignmentIn
    result: AssignmentWriteOut
