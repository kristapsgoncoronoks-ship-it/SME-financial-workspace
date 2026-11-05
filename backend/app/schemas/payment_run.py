from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class RunCreate(BaseModel):
    invoice_ids: list[str] = Field(min_length=1)
    method: str = "bank_transfer"
    note: str | None = None
    # WO-2: a PROVISIONAL vendor (bank identity captured at creation, never
    # verified) is refused from a run unless the maker explicitly confirms it.
    confirm_provisional: bool = False


class RunApprove(BaseModel):
    version: int
    # WO-9: the EXPLICIT platform-admin segregation-of-duties override — never
    # silent, always audited (`payment_run.sod_override`). Ignored for everyone else.
    override_sod: bool = False


class RunPay(BaseModel):
    version: int
    reference: str | None = None
    method: str | None = None
    # WO-9: explicit, audited platform-admin SoD override (see RunApprove).
    override_sod: bool = False


class RunInvoiceOut(BaseModel):
    id: str
    invoice_number: str
    vendor_name: str | None = None
    total: Decimal
    currency: str
    total_eur: Decimal | None = None
    workflow_state: str


class RunOut(BaseModel):
    id: str
    reference: str | None = None
    method: str
    status: str
    note: str | None = None
    total_eur: Decimal
    paid_at: datetime | None = None
    created_by: str | None = None
    approved_by: str | None = None
    approved_at: datetime | None = None
    exported_at: datetime | None = None
    export_count: int = 0
    last_msg_id: str | None = None
    version: int
    created_at: datetime
    invoice_count: int


class RunDetailOut(RunOut):
    invoices: list[RunInvoiceOut] = []


class RunPageOut(BaseModel):
    """Bounded history display; the legacy array endpoint remains compatible."""

    items: list[RunOut]
    total: int = Field(ge=0)
