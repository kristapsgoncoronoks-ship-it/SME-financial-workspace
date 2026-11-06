from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.services.reconciliation_amounts import bank_decimal


class BankLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    version: int = Field(strict=True, ge=1, le=2147483647)
    statement_id: str
    line_date: date
    description: str
    amount: Decimal | None  # signed when valid; unavailable is never zero
    amount_status: Literal["valid", "invalid"]
    currency: str | None = None  # source-stated; never infer EUR for an unknown unit
    direction: str
    balance: Decimal | None = None
    balance_status: Literal["valid", "invalid", "unprovided"]
    status: str
    matched_kind: str | None = None
    matched_id: str | None = None

    @model_validator(mode="before")
    @classmethod
    def read_stored_money(cls, value: Any) -> dict:
        # Copy response values; never assign to the ORM instance or repair data.
        data = (
            dict(value)
            if isinstance(value, dict)
            else {name: getattr(value, name) for name in cls.model_fields if hasattr(value, name)}
        )
        raw_amount, raw_balance = data.get("amount"), data.get("balance")
        data["amount"] = bank_decimal(raw_amount)
        data["amount_status"] = "valid" if data["amount"] is not None else "invalid"
        data["balance"] = bank_decimal(raw_balance)
        data["balance_status"] = (
            "unprovided"
            if raw_balance is None
            else "valid"
            if data["balance"] is not None
            else "invalid"
        )
        return data


class BankStatementOut(BaseModel):
    id: str
    filename: str
    source_format: str
    method: str
    line_count: int
    created_by: str | None = None
    created_at: datetime
    unmatched: int = 0
    matched: int = 0
    ignored: int = 0


class ImportResult(BaseModel):
    statement_id: str
    method: str
    imported: int
    warnings: list[str] = []


class MatchCandidateOut(BaseModel):
    kind: str
    id: str
    amount: Decimal
    currency: str
    date: date
    reference: str | None = None
    days_off: int
    score: int


class MatchRequest(BaseModel):
    kind: str = Field(pattern="^(receipt|reimbursement|payment_run|issued_payment)$")
    target_id: str
