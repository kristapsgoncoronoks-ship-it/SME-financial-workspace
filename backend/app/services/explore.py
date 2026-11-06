"""Self-service analytics — a Power BI / Tableau-style pivot engine.

Instead of fixed reports, the caller picks a **measure** and up to two
**dimensions** with **filters**, and the query is assembled from a whitelist and
aggregated entirely in the database.

Everything is modelled over one fact grain — the **line item** (line_items ⋈
invoices ⋈ vendors) — so measures stay additive across any dimension (grouping
invoice totals by a line attribute like category would double-count; grouping
line amounts never does). Invoice-level counts use COUNT(DISTINCT invoice).

Scale: all grouping/aggregation runs in the DB; the fact join is indexed
(org_id+issue_date, invoice_id+category). For very large tenants the same engine
can read a materialised daily rollup with no API change.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import ColumnElement, Integer, String, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import dimensions as core_dimensions
from app.core.config import settings
from app.core.errors import ValidationError
from app.models.invoice import Invoice, InvoiceStatus, LineItem
from app.models.vendor import Vendor


@dataclass(frozen=True)
class Dimension:
    key: str
    label: str
    expr: Callable[[], Any]
    temporal: bool = False


@dataclass(frozen=True)
class Measure:
    key: str
    label: str
    expr: Callable[[], ColumnElement]
    unit: str  # "money" | "number" | "count"


def _month():
    if settings.is_sqlite:
        return func.strftime("%Y-%m", Invoice.issue_date)
    return func.to_char(Invoice.issue_date, "FMYYYY-MM")


def _year():
    if settings.is_sqlite:
        return func.strftime("%Y", Invoice.issue_date)
    return func.to_char(Invoice.issue_date, "YYYY")


def _quarter():
    if settings.is_sqlite:
        # 'YYYY-Q<n>' — quarter number derived from the month.
        qnum = (cast(func.strftime("%m", Invoice.issue_date), Integer) + 2) / 3
        return func.strftime("%Y", Invoice.issue_date) + "-Q" + cast(cast(qnum, Integer), String)
    return (
        func.to_char(Invoice.issue_date, "YYYY")
        + "-Q"
        + func.to_char(func.extract("quarter", Invoice.issue_date), "FM9")
    )


def _cost_dimension(key: str, label: str) -> Dimension:
    """A cost-allocation dimension, derived from the one registry in
    ``app.core.dimensions`` (ADR-0026 — one dimension registry; board C1.6).

    The tags live as columns on ``Invoice``, so grouping *line amounts* by them
    never double-counts (each line belongs to exactly one invoice — the same
    additivity rule as the other invoice-level dimensions above). The coalesce
    happens SQL-side so ``GROUP BY`` merges untagged rows into the same
    ``UNASSIGNED`` bucket the fixed by-dimension report uses — the two read
    paths must return identical numbers for the same cut.

    ``key``/``label`` are bound as parameters (never a closure over a loop
    variable — late binding would point every lambda at the last key).
    """
    return Dimension(
        key, label, lambda: func.coalesce(getattr(Invoice, key), core_dimensions.UNASSIGNED)
    )


DIMENSIONS: dict[str, Dimension] = {
    d.key: d
    for d in [
        Dimension("vendor", "Vendor", lambda: Vendor.name),
        Dimension("country", "Vendor country", lambda: func.coalesce(Vendor.country, "—")),
        Dimension("category", "Category", lambda: LineItem.category),
        Dimension("status", "Invoice status", lambda: Invoice.status),
        Dimension("validation", "Validation", lambda: Invoice.validation_status),
        Dimension("currency", "Currency", lambda: Invoice.currency),
        Dimension("month", "Month", _month, temporal=True),
        Dimension("quarter", "Quarter", _quarter, temporal=True),
        Dimension("year", "Year", _year, temporal=True),
        # The five cost-allocation dimensions come FROM the core registry — a
        # dimension added there appears here (and in /analytics/dimensions and
        # the fixed report) with no further change. Never hand-list them here.
        *(_cost_dimension(k, label) for k, label in core_dimensions.DIMENSIONS.items()),
    ]
}

_net = lambda: func.coalesce(func.sum(LineItem.amount), 0)  # noqa: E731
_tax = lambda: func.coalesce(func.sum(LineItem.amount * LineItem.tax_rate / 100), 0)  # noqa: E731

MEASURES: dict[str, Measure] = {
    m.key: m
    for m in [
        Measure("net", "Net spend", _net, "money"),
        Measure("tax", "Tax", _tax, "money"),
        Measure(
            "gross",
            "Gross spend",
            lambda: func.coalesce(
                func.sum(LineItem.amount + LineItem.amount * LineItem.tax_rate / 100), 0
            ),
            "money",
        ),
        Measure(
            "quantity", "Quantity", lambda: func.coalesce(func.sum(LineItem.quantity), 0), "number"
        ),
        Measure("lines", "Line items", lambda: func.count(LineItem.id), "count"),
        Measure("invoices", "Invoices", lambda: func.count(func.distinct(Invoice.id)), "count"),
    ]
}


def catalog() -> dict:
    return {
        "dimensions": [
            {"key": d.key, "label": d.label, "temporal": d.temporal} for d in DIMENSIONS.values()
        ],
        "measures": [{"key": m.key, "label": m.label, "unit": m.unit} for m in MEASURES.values()],
    }


@dataclass
class ExploreQuery:
    measure: str
    dimensions: list[str]
    start: date | None = None
    end: date | None = None
    status: str | None = None
    category: str | None = None
    currency: str | None = None
    country: str | None = None
    vendor_id: str | None = None
    sort: str = "auto"  # auto | value_desc | value_asc | dim
    limit: int = 100


def _fmt(value, unit: str) -> str:
    if value is None:
        return "0"
    if unit == "count":
        return str(int(value))
    dec = Decimal(str(value))
    return str(dec.quantize(Decimal("0.001") if unit == "number" else Decimal("0.01")))


async def run(db: AsyncSession, org_id: str, q: ExploreQuery) -> dict:
    if q.measure not in MEASURES:
        raise ValueError(f"Unknown measure '{q.measure}'")
    dims = [d for d in q.dimensions if d][:2]
    for d in dims:
        if d not in DIMENSIONS:
            raise ValueError(f"Unknown dimension '{d}'")

    measure = MEASURES[q.measure]
    currency = q.currency
    if currency is not None:
        if not isinstance(currency, str) or re.fullmatch(r"[A-Za-z]{3}", currency) is None:
            raise ValidationError(
                "Enter a three-letter native currency code, such as USD.",
                code="explore_currency_invalid",
            )
        currency = currency.upper()
    money = measure.unit == "money"
    if money and currency is None:
        raise ValidationError(
            "Choose a native currency before querying money. Add currency=USD, for example; "
            "amounts are not converted between currencies.",
            code="explore_currency_required",
        )
    dim_specs = [DIMENSIONS[d] for d in dims]
    dim_cols = [spec.expr().label(f"d{i}") for i, spec in enumerate(dim_specs)]
    value_col = measure.expr().label("value")

    stmt = (
        select(*dim_cols, value_col)
        .select_from(LineItem)
        .join(Invoice, Invoice.id == LineItem.invoice_id)
        .join(Vendor, Vendor.id == Invoice.vendor_id)
        .where(Invoice.org_id == org_id)
    )
    if q.start:
        stmt = stmt.where(Invoice.issue_date >= q.start)
    if q.end:
        stmt = stmt.where(Invoice.issue_date <= q.end)
    if q.status:
        stmt = stmt.where(Invoice.status == InvoiceStatus(q.status))
    if q.category:
        stmt = stmt.where(LineItem.category == q.category)
    if currency:
        stmt = stmt.where(Invoice.currency == currency)
    if q.country:
        stmt = stmt.where(Vendor.country == q.country.upper())
    if q.vendor_id:
        stmt = stmt.where(Invoice.vendor_id == q.vendor_id)

    raw_dim_exprs = [spec.expr() for spec in dim_specs]
    if raw_dim_exprs:
        stmt = stmt.group_by(*raw_dim_exprs)

    # Ordering: temporal first-dimension → chronological; else by value desc.
    temporal_first = dim_specs and dim_specs[0].temporal
    sort = q.sort
    if sort == "auto":
        sort = "dim" if temporal_first else "value_desc"
    if sort == "dim" and raw_dim_exprs:
        stmt = stmt.order_by(*raw_dim_exprs)
    elif sort == "value_asc":
        stmt = stmt.order_by(value_col.asc())
    else:
        stmt = stmt.order_by(value_col.desc())

    limit = max(1, min(q.limit, 1000))
    # One extra grouped row answers only whether this bounded cut omitted
    # groups. No count-all query or unbounded materialization is needed.
    stmt = stmt.limit(limit + 1)
    rows = (await db.execute(stmt)).all()
    truncated = len(rows) > limit

    out_rows = []
    for r in rows[:limit]:
        rec = {}
        for i, spec in enumerate(dim_specs):
            val = r[i]
            rec[spec.key] = (
                val.value if hasattr(val, "value") else (str(val) if val is not None else "—")
            )
        rec["value"] = _fmt(r[len(dim_specs)], measure.unit)
        out_rows.append(rec)

    return {
        "measure": {"key": measure.key, "label": measure.label, "unit": measure.unit},
        "dimensions": [{"key": s.key, "label": s.label, "temporal": s.temporal} for s in dim_specs],
        "rows": out_rows,
        "scope": "bounded_groups",
        "limit": limit,
        "returned_rows": len(out_rows),
        "truncated": truncated,
        "currency": currency if money else None,
        "currency_basis": "native" if money else "not_applicable",
    }


def to_csv(result: dict) -> str:
    import csv
    import io

    from app.core.csv_safety import sanitize_cell

    dims = result["dimensions"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([d["label"] for d in dims] + [result["measure"]["label"]])
    for row in result["rows"]:
        # Dimension values (vendor name, category, cost center, ...) are
        # free text that can originate from a supplier document a human
        # transcribes — sanitized against CSV/Excel formula injection
        # (board R1, CWE-1236). `row["value"]` is the already-formatted
        # measure figure (money/percent/count) and is left untouched: a
        # legitimate negative figure must round-trip as a real number, not
        # be turned into text by a stray quote prefix.
        w.writerow([sanitize_cell(row.get(d["key"], "")) for d in dims] + [row["value"]])
    return buf.getvalue()
