"""Versioned bank-line intentions and immutable, source-owned outcomes."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.reconciliation_command import MAX_VERSION
from app.schemas.reconciliation import BankLineOut

Version = Annotated[int, Field(strict=True, ge=1, le=MAX_VERSION)]
Kind = Literal["receipt", "reimbursement", "payment_run", "issued_payment"]
Operation = Literal["match", "ignore", "unmatch", "restore"]


class ExactModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Association(ExactModel):
    kind: Kind
    id: UUID


class LineState(ExactModel):
    status: Literal["unmatched", "matched", "ignored"]
    matched_kind: Kind | None = None
    matched_id: UUID | None = None

    @model_validator(mode="after")
    def coherent(self):
        if self.status == "matched":
            if self.matched_kind is None or self.matched_id is None:
                raise ValueError("A matched state requires its exact association")
        elif self.matched_kind is not None or self.matched_id is not None:
            raise ValueError("Only a matched state has an association")
        return self


class CommandIntent(ExactModel):
    operation: Operation
    expected_version: Version
    expected: LineState
    target: Association | None = None
    reason: str | None = Field(default=None, max_length=1000)

    @field_validator("reason")
    @classmethod
    def valid_reason(cls, value: str | None):
        if value is None:
            return None
        if "\x00" in value:
            raise ValueError("Reason contains an unsupported character")
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeError:
            raise ValueError("Reason must be valid UTF-8 text") from None
        return value.strip() or None

    @model_validator(mode="after")
    def transition(self):
        required = {
            "match": "unmatched",
            "ignore": "unmatched",
            "unmatch": "matched",
            "restore": "ignored",
        }
        if self.expected.status != required[self.operation]:
            raise ValueError("The operation requires the corresponding reviewed source state")
        if (self.operation == "match") != (self.target is not None):
            raise ValueError("Only Match requires a target")
        if self.operation == "ignore" and self.reason is None:
            raise ValueError("Explain why this line is ignored")
        return self


class CurrentLine(BankLineOut):
    version: Version


class FrozenLine(LineState):
    id: UUID
    statement_id: UUID
    version: Version


class FrozenResult(ExactModel):
    contract_version: Literal[1] = 1
    status: Literal["committed"] = "committed"
    command_id: UUID
    command_key: str
    org_id: UUID
    actor_user_id: UUID
    line_id: UUID
    operation: Operation
    payload_hash: str = Field(pattern="^[0-9a-f]{64}$")
    before_version: Version
    after_version: Version
    before: LineState
    after: LineState
    line: FrozenLine
    reason: str | None
    recorded_at: datetime


class CommandReceipt(FrozenResult):
    result_hash: str = Field(pattern="^[0-9a-f]{64}$")


class HistoryEvent(ExactModel):
    command_id: UUID
    actor_user_id: UUID
    operation: Operation
    before_version: Version
    after_version: Version
    before: LineState
    after: LineState
    reason: str | None
    recorded_at: datetime


class HistoryPage(ExactModel):
    events: list[HistoryEvent]
    next_before_version: int | None
