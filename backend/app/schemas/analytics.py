from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel


class ExploreMeasureOut(BaseModel):
    key: str
    label: str
    unit: Literal["money", "number", "count"]


class ExploreDimensionOut(BaseModel):
    key: str
    label: str
    temporal: bool


class ExploreResultOut(BaseModel):
    measure: ExploreMeasureOut
    dimensions: list[ExploreDimensionOut]
    rows: list[dict[str, str]]
    scope: Literal["bounded_groups"]
    limit: int
    returned_rows: int
    truncated: bool


class SummaryOut(BaseModel):
    total_invoices: int
    total_spend: Decimal
    total_tax: Decimal
    unpaid_amount: Decimal
    avg_invoice: Decimal
    vendor_count: int
    currency: str
    # C1.7/WO-24: the AR-reports pattern (`issued_reports._pick_currency`) —
    # every amount above is scoped to `currency` alone (the tenant's most-used,
    # or an explicit `?currency=` filter); every OTHER currency the tenant has
    # invoices in is listed here, never folded into the same total (§4.14).
    available_currencies: list[str] = []


class TimeBucket(BaseModel):
    period: str  # YYYY-MM
    total: Decimal
    invoice_count: int


class VendorSpend(BaseModel):
    vendor_id: str
    vendor_name: str
    total: Decimal
    invoice_count: int


class CategorySpend(BaseModel):
    category: str
    total: Decimal


class StatusBucket(BaseModel):
    status: str
    count: int
    total: Decimal


class DimensionSpend(BaseModel):
    """Spend grouped by a cost-allocation dimension value (invoices)."""

    value: str  # the tag value, or "(unassigned)" for untagged spend
    total: Decimal
    invoice_count: int


class DimensionBreakdown(BaseModel):
    dimension: str  # the dimension key (cost_center, vehicle, …)
    label: str  # human label
    rows: list[DimensionSpend]
    total: Decimal
    # C1.7/WO-24: see SummaryOut.available_currencies — `total` above sums ONLY
    # `currency`.
    currency: str = "EUR"
    available_currencies: list[str] = []


# --------------------------------------------------------------------------- #
# C1.7/WO-24 — row-shaped endpoints (spend_over_time / top_vendors /
# by_category / by_status) used to return a bare `list[X]` aggregated with NO
# currency filter at all: a EUR+USD tenant's "top vendor" total silently
# blended both currencies into one meaningless number, and the SPA rendered it
# with a hard-coded € sign (`format.money()`'s default). Wrapping each in this
# envelope is a WIRE-SHAPE CHANGE (list → object) — coordinated with the route
# `response_model` and the SPA consumer in the same commit, per the WO-8
# precedent for ap_aging's DueSummary. `rows` matches DimensionBreakdown's
# existing field name for the same shape of thing in this module.
# --------------------------------------------------------------------------- #
class SpendOverTimeOut(BaseModel):
    currency: str
    available_currencies: list[str]
    rows: list[TimeBucket]


class TopVendorsOut(BaseModel):
    currency: str
    available_currencies: list[str]
    rows: list[VendorSpend]


class ByCategoryOut(BaseModel):
    currency: str
    available_currencies: list[str]
    rows: list[CategorySpend]


class ByStatusOut(BaseModel):
    currency: str
    available_currencies: list[str]
    rows: list[StatusBucket]
