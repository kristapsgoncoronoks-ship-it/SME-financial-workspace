"""Schemas for expense reimbursement payout batches (Phase 09)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.money import q2

ReportVersion = Annotated[int, Field(strict=True, ge=1)]


def _utc_recording_time(value: datetime | None) -> datetime | None:
    # Server recording clocks are UTC. SQLite drops their timezone marker.
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class BatchCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report_ids: list[str] = Field(min_length=1, max_length=1000)
    report_versions: dict[str, ReportVersion] = Field(default_factory=dict, max_length=1000)
    method: str = Field(default="bank_transfer", min_length=1, max_length=40)
    note: str | None = Field(default=None, max_length=1000)

    @field_validator("report_ids")
    @classmethod
    def valid_report_ids(cls, value: list[str]) -> list[str]:
        ids = [str(UUID(item)) for item in value]
        if len(ids) != len(set(ids)):
            raise ValueError("Select each report once")
        return ids

    @field_validator("report_versions")
    @classmethod
    def valid_version_ids(cls, value: dict[str, int]) -> dict[str, int]:
        canonical = {str(UUID(key)): version for key, version in value.items()}
        if len(canonical) != len(value):
            raise ValueError("Send each report version once")
        return canonical

    @model_validator(mode="after")
    def complete_versions(self):
        if self.report_versions and set(self.report_versions) != set(self.report_ids):
            raise ValueError("Send the current version of every selected report")
        return self


class BatchPay(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(strict=True, ge=1)
    reference: str | None = Field(default=None, max_length=200)
    method: str | None = Field(default=None, max_length=40)
    expected_total_eur: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    report_versions: dict[str, ReportVersion] = Field(default_factory=dict, max_length=1000)
    # Board R6 — the audited, explicit platform-admin escape hatch for the
    # maker≠checker control (mirrors payment_run's RunPay.override_sod).
    override_sod: bool = False

    @field_validator("report_versions")
    @classmethod
    def valid_version_ids(cls, value: dict[str, int]) -> dict[str, int]:
        canonical = {str(UUID(key)): version for key, version in value.items()}
        if len(canonical) != len(value):
            raise ValueError("Send each report version once")
        return canonical

    @field_validator("expected_total_eur")
    @classmethod
    def canonical_expected_total(cls, value: Decimal | None) -> Decimal | None:
        return q2(value) if value is not None else None


class BatchCancel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(strict=True, ge=1)
    report_versions: dict[str, ReportVersion] = Field(max_length=1000)
    reason: str | None = Field(default=None, max_length=1000)

    @field_validator("report_versions")
    @classmethod
    def valid_version_ids(cls, value: dict[str, int]) -> dict[str, int]:
        canonical = {str(UUID(key)): version for key, version in value.items()}
        if len(canonical) != len(value):
            raise ValueError("Send each report version once")
        return canonical

    @field_validator("reason")
    @classmethod
    def canonical_reason(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None


class PaymentEvidence(BaseModel):
    basis: Literal["operator_recorded", "legacy_unknown", "not_recorded", "unavailable"] = (
        "not_recorded"
    )
    bank_confirmation: Literal["unknown"] = "unknown"
    recorded_by_id: str | None = None
    recorded_at: datetime | None = None

    @field_validator("recorded_at")
    @classmethod
    def utc_recording_time(cls, value: datetime | None) -> datetime | None:
        # These timestamps originate from the server's UTC recording clock.
        # SQLite drops the timezone marker while PostgreSQL preserves it;
        # restore that known storage convention rather than let a browser
        # reinterpret a naive value in its own local timezone.
        return _utc_recording_time(value)


class ReimbursementReportOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    employee_name: str
    total: Decimal
    currency: str
    total_eur: Decimal | None
    status: str
    payment_reference: str | None
    reimbursed_at: datetime | None
    # Pre-EX20 frozen create receipts did not record report versions. Current
    # ORM detail rows supply their actual version; legacy receipts stay unknown.
    version: int | None = None

    @field_validator("reimbursed_at")
    @classmethod
    def utc_reimbursed_recording_time(cls, value: datetime | None) -> datetime | None:
        return _utc_recording_time(value)


class BatchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    reference: str | None
    method: str
    status: str
    note: str | None
    total_eur: Decimal
    paid_at: datetime | None
    created_by: str | None
    exported_at: datetime | None = None
    export_count: int = 0
    last_msg_id: str | None = None
    version: int
    created_at: datetime
    report_count: int
    payment_evidence: PaymentEvidence = Field(default_factory=PaymentEvidence)

    @field_validator("paid_at")
    @classmethod
    def utc_paid_recording_time(cls, value: datetime | None) -> datetime | None:
        return _utc_recording_time(value)


class BatchDetailOut(BatchOut):
    reports: list[ReimbursementReportOut]


class BatchCancelOut(BaseModel):
    batch: BatchDetailOut
    # Frozen at cancellation, even after reports enter another batch later.
    released_reports: list[ReimbursementReportOut]
    reason: str | None = None


class EligibleReportOut(BaseModel):
    id: str
    title: str
    employee_name: str
    total: Decimal
    currency: str
    total_eur: Decimal | None
    status: str
    version: int


class EligibleReportPage(BaseModel):
    items: list[EligibleReportOut]
    total: int
    page: int
    page_size: int


class BatchPage(BaseModel):
    items: list[BatchOut]
    total: int
    page: int
    page_size: int
