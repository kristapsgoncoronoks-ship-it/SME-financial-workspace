"""Wire schemas for the off-invoice rebate registry (WO-84, G4.2/R50).

Registry fields follow `app/models/transport/off_invoice_rebate.py`; preview
fields follow the shared close planner. This module shapes the wire and
computes nothing (engineering-rules §3). In
particular it does NOT accept `amount_eur`: the EUR figure is RESOLVED by the
service through `fx.to_eur` at the document's own date and refused when no rate
exists (master-context §4.10 "the server recomputes every total", §4.15 "never a
guessed number"). A client states the document; the server states the euro.

Every amount is typed `Decimal`, so pydantic v2 serializes it as a JSON STRING
and no float ever appears in a money path (§4.9).

`source_ref` and `source_party` carry `min_length=1` here as a SHAPE check only.
The real guard is `rebate._require_source`, which strips first (so `"  "` is
refused too) and raises the service's own `rebate_source_required`, and the
`CHECK` constraints on the table behind it. The schema catches shape; the
service catches the business rule (definition-of-done §3).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class RebateIn(BaseModel):
    """One recorded off-invoice rebate document — §4.2's canonical *"SEPARATE
    rebate invoice per country"*.

    `period` is the fuel period the rebate ADJUSTS (`FuelTransaction.period`),
    while `rebate_date` is the rebate document's own date and is what the FX
    conversion is audited against. They are deliberately separate: a rebate
    invoice for March routinely carries an April date.
    """

    supplier: str = Field(min_length=1, max_length=60)
    country: str = Field(min_length=2, max_length=2)
    period: str = Field(min_length=7, max_length=7, description="YYYY-MM — the period adjusted")
    source_ref: str = Field(min_length=1, max_length=120, description="the rebate document number")
    source_party: str = Field(min_length=1, max_length=120, description="who issued it")
    rebate_date: date
    currency: str = Field(min_length=3, max_length=3)
    amount_local: Decimal
    note: str | None = None


class RebateOut(BaseModel):
    id: str
    supplier: str
    country: str
    period: str
    source_ref: str
    source_party: str
    rebate_date: date
    currency: str
    amount_local: Decimal
    # Server-resolved (§4.10). `fx_source` is the platform-wide provenance enum
    # — `"eur"` for the identity, `"ecb"` for a dated reference-rate conversion.
    # There is no `"unknown"` here: a rebate that could not reach EUR was
    # refused rather than stored (§4.15).
    amount_eur: Decimal
    fx_rate: Decimal | None = None
    fx_ecb_rate: Decimal | None = None
    fx_ecb_date: date | None = None
    fx_source: str | None = None
    note: str | None = None


class RebateAllocationOut(BaseModel):
    """Per-transaction NET EUR totals; no browser FX or price calculation."""

    model_config = ConfigDict(from_attributes=True)

    fuel_transaction_id: str
    supplier: str
    country: str
    qty: Decimal
    net_eur: Decimal
    share_eur: Decimal
    old_net_eur_eff: Decimal
    new_net_eur_eff: Decimal


class RebatePreviewOut(BaseModel):
    """Whole-scope observed summary with only its allocation display paged.

    `changed` is the number that WOULD change at observation time. This is not
    a saved plan or an apply token; monthly close re-reads its own sources.
    """

    period: str
    supplier: str | None
    rebate_total_eur: Decimal
    lines_allocated: int
    changed: int
    source_warnings: tuple[str, ...]
    allocations: list[RebateAllocationOut]
    page: int
    page_size: int
