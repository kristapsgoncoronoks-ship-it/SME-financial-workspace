from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.concurrency import run_in_threadpool

from app.api.deps import CurrentUser, DbSession, require_perm
from app.core import authz
from app.core.dimensions import DIMENSION_KEYS, DIMENSIONS, is_dimension
from app.core.security_headers import content_disposition
from app.schemas.analytics import (
    ByCategoryOut,
    ByStatusOut,
    DimensionBreakdown,
    ExploreResultOut,
    SpendOverTimeOut,
    SummaryOut,
    TopVendorsOut,
)
from app.schemas.ap_aging import ApAgingOut, WorklistItemOut
from app.schemas.benchmark import CombinedBenchmark, SupplierBenchmarkListOut
from app.schemas.cash_flow import CashFlowPointOut
from app.schemas.cash_position import CashPositionOut
from app.services import (
    analytics,
    ap_aging,
    benchmark,
    cash_flow,
    cash_position,
    explore,
    report_writers,
    supplier_costs,
)

# Structural authorization (ADR-0024): the whole analytics surface reads the
# org's aggregated money data — router-level REPORT_READ. Previously only three
# endpoints checked it in-handler; the rest were open to any member (the
# security gap this order closes).
router = APIRouter(
    prefix="/analytics",
    tags=["analytics"],
    dependencies=[Depends(require_perm(authz.Permission.REPORT_READ))],
)


@router.get("/cash-position", response_model=CashPositionOut)
async def get_cash_position(current: CurrentUser, db: DbSession):
    """AR + AP + reconciliation roll-up for the cash-position dashboard (Phase 15)."""
    return await cash_position.summary(db, current.org_id)


@router.get("/cash-flow", response_model=list[CashFlowPointOut])
async def get_cash_flow(
    current: CurrentUser,
    db: DbSession,
    months: int = Query(default=12, ge=1, le=36),
):
    """Monthly inflow / outflow / net cash-flow trend (Phase 19)."""
    return await cash_flow.monthly(db, current.org_id, months=months)


@router.get("/ap-aging", response_model=ApAgingOut)
async def get_ap_aging(current: CurrentUser, db: DbSession):
    """Payables worklist: open supplier invoices due soon / overdue (Phase 16b)."""
    # The summary is computed in SQL over EVERY open payable (PERF-002); the
    # listed rows are the soonest-due `WORKLIST_LIMIT`, with the total beside
    # them (PERF-005/010). Summarising the truncated list would undercount.
    s = await ap_aging.due_summary(db, current.org_id)
    total = await ap_aging.open_payable_count(db, current.org_id)
    items = await ap_aging.worklist(db, current.org_id, limit=ap_aging.WORKLIST_LIMIT)
    return ApAgingOut(
        currency=s.currency,
        due_soon_count=s.due_soon_count,
        due_soon_amount=s.due_soon_amount,
        overdue_count=s.overdue_count,
        overdue_amount=s.overdue_amount,
        other_currencies=list(s.other_currencies),
        items=[WorklistItemOut(**vars(it)) for it in items],
        items_total=total,
        items_limit=ap_aging.WORKLIST_LIMIT,
        truncated=total > len(items),
    )


# C1.7/WO-24: every report below sums money, so every one takes an optional
# `currency` filter — an explicit request wins, else the AR-reports pattern
# (`_pick_currency`) resolves the tenant's most-used currency. Never omit it
# silently: the response always names which currency was used and which
# others exist (`available_currencies`), never a blended cross-currency total.
_CurrencyQ = Query(default=None, min_length=3, max_length=3)


@router.get("/summary", response_model=SummaryOut)
async def get_summary(
    current: CurrentUser,
    db: DbSession,
    start: date | None = None,
    end: date | None = None,
    currency: str | None = _CurrencyQ,
):
    return await analytics.summary(db, current.org_id, start, end, currency)


@router.get("/spend-over-time", response_model=SpendOverTimeOut)
async def get_spend_over_time(
    current: CurrentUser,
    db: DbSession,
    start: date | None = None,
    end: date | None = None,
    currency: str | None = _CurrencyQ,
):
    return await analytics.spend_over_time(db, current.org_id, start, end, currency)


@router.get("/top-vendors", response_model=TopVendorsOut)
async def get_top_vendors(
    current: CurrentUser,
    db: DbSession,
    start: date | None = None,
    end: date | None = None,
    limit: int = Query(default=10, ge=1, le=50),
    currency: str | None = _CurrencyQ,
):
    return await analytics.top_vendors(db, current.org_id, start, end, limit, currency)


@router.get("/by-category", response_model=ByCategoryOut)
async def get_by_category(
    current: CurrentUser,
    db: DbSession,
    start: date | None = None,
    end: date | None = None,
    currency: str | None = _CurrencyQ,
):
    return await analytics.by_category(db, current.org_id, start, end, currency)


@router.get("/dimensions")
async def get_dimensions(current: CurrentUser):
    """The cost-allocation dimensions available to slice spend by."""
    return [{"key": k, "label": v} for k, v in DIMENSIONS.items()]


@router.get("/by-dimension", response_model=DimensionBreakdown)
async def get_by_dimension(
    current: CurrentUser,
    db: DbSession,
    dimension: str = Query(..., description=" | ".join(DIMENSION_KEYS)),
    start: date | None = None,
    end: date | None = None,
    currency: str | None = _CurrencyQ,
):
    if not is_dimension(dimension):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown dimension '{dimension}'")
    return await analytics.by_dimension(db, current.org_id, dimension, start, end, currency)


@router.get("/by-status", response_model=ByStatusOut)
async def get_by_status(
    current: CurrentUser,
    db: DbSession,
    start: date | None = None,
    end: date | None = None,
    currency: str | None = _CurrencyQ,
):
    return await analytics.by_status(db, current.org_id, start, end, currency)


# --- Supplier cost analytics (WO-G phase 1) --------------------------------
# Read models over the tenant's own invoice lines; dict payloads (the service
# documents the shape) on the same REPORT_READ router — no new surface class.


@router.get("/supplier-costs/kpis")
async def get_supplier_cost_kpis(
    current: CurrentUser, db: DbSession, currency: str | None = _CurrencyQ
):
    return await supplier_costs.cost_kpis(db, current.org_id, currency=currency)


@router.get("/supplier-costs/changes")
async def get_supplier_cost_changes(
    current: CurrentUser,
    db: DbSession,
    currency: str | None = _CurrencyQ,
    window_days: int = Query(default=365, ge=30, le=1830),
    limit: int = Query(default=50, ge=1, le=500),
):
    return await supplier_costs.cost_changes(
        db, current.org_id, currency=currency, window_days=window_days, limit=limit
    )


@router.get("/supplier-costs/history")
async def get_supplier_cost_history(
    current: CurrentUser,
    db: DbSession,
    vendor_id: str,
    item: str,
    currency: str | None = _CurrencyQ,
    months: int = Query(default=12, ge=1, le=60),
):
    return await supplier_costs.price_history(
        db, current.org_id, vendor_id=vendor_id, item=item, currency=currency, months=months
    )


# --- Supplier agreed prices + overcharge worklist (WO-G phase 2) -----------
# Reads stay on the router's REPORT_READ; the two mutations ADDITIONALLY
# require SETTINGS_MANAGE — the price list is org configuration, exactly like
# the validation toggles it powers.

from datetime import date as _date  # noqa: E402
from decimal import Decimal as _Decimal  # noqa: E402

from pydantic import BaseModel, Field  # noqa: E402

from app.services import agreed_prices, audit  # noqa: E402

_SETTINGS = [Depends(require_perm(authz.Permission.SETTINGS_MANAGE))]


class AgreedPriceIn(BaseModel):
    vendor_id: str
    item: str = Field(min_length=1, max_length=500)
    agreed_price: _Decimal = Field(gt=0)
    currency: str = Field(default="EUR", min_length=3, max_length=3)
    valid_from: _date | None = None
    valid_to: _date | None = None
    note: str | None = Field(default=None, max_length=500)


@router.get("/supplier-costs/agreed")
async def list_agreed_prices(current: CurrentUser, db: DbSession, vendor_id: str | None = None):
    return await agreed_prices.list_prices(db, current.org_id, vendor_id=vendor_id)


@router.put("/supplier-costs/agreed", dependencies=_SETTINGS)
async def put_agreed_price(body: AgreedPriceIn, current: CurrentUser, db: DbSession):
    try:
        row = await agreed_prices.upsert(
            db,
            current.org_id,
            vendor_id=body.vendor_id,
            item=body.item,
            agreed_price=body.agreed_price,
            currency=body.currency,
            valid_from=body.valid_from,
            valid_to=body.valid_to,
            note=body.note,
            created_by=current.email,
        )
    except agreed_prices.NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except agreed_prices.AgreedPriceError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    await audit.record(
        db,
        "supplier_costs.agreed_price_set",
        target_type="supplier_agreed_price",
        target_id=row.id,
        meta={"vendor_id": row.vendor_id, "item": row.item, "price": str(row.agreed_price)},
    )
    await db.commit()
    return {"id": row.id}


@router.delete("/supplier-costs/agreed/{price_id}", dependencies=_SETTINGS)
async def delete_agreed_price(price_id: str, current: CurrentUser, db: DbSession):
    try:
        row = await agreed_prices.remove(db, current.org_id, price_id)
    except agreed_prices.NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    await audit.record(
        db,
        "supplier_costs.agreed_price_delete",
        target_type="supplier_agreed_price",
        target_id=price_id,
        meta={"vendor_id": row.vendor_id, "item": row.item},
    )
    await db.commit()
    return {"deleted": price_id}


@router.get("/supplier-costs/overcharges")
async def get_overcharge_worklist(
    current: CurrentUser,
    db: DbSession,
    window_days: int = Query(default=365, ge=30, le=1830),
):
    return await agreed_prices.overcharge_worklist(db, current.org_id, window_days=window_days)


@router.get("/supplier-benchmark", response_model=SupplierBenchmarkListOut)
async def get_supplier_benchmark(
    current: CurrentUser,
    db: DbSession,
    start: date | None = None,
    end: date | None = None,
    currency: str | None = _CurrencyQ,
):
    """Independent per-supplier scorecards."""
    return await benchmark.supplier_benchmarks(db, current.org_id, start, end, currency)


@router.get("/combined-benchmark", response_model=CombinedBenchmark)
async def get_combined_benchmark(
    current: CurrentUser,
    db: DbSession,
    start: date | None = None,
    end: date | None = None,
    currency: str | None = _CurrencyQ,
):
    """Combined cross-supplier price benchmark per category + savings opportunity."""
    return await benchmark.combined_benchmark(db, current.org_id, start, end, currency)


@router.get("/fields")
async def get_explore_fields(current: CurrentUser):
    """The dimension + measure catalog for the self-service explore builder."""
    return explore.catalog()


@router.get("/explore", response_model=ExploreResultOut)
async def get_explore(
    current: CurrentUser,
    db: DbSession,
    response: Response,
    measure: str = Query(default="net"),
    dim: list[str] = Query(default_factory=list, description="up to 2 dimensions"),
    start: date | None = None,
    end: date | None = None,
    status_: str | None = Query(default=None, alias="status"),
    category: str | None = None,
    currency: str | None = None,
    country: str | None = None,
    vendor_id: str | None = None,
    sort: str = "auto",
    limit: int = Query(default=100, ge=1, le=1000),
    format: str = Query(default="json", pattern="^(json|csv|xlsx|pdf)$"),
):
    """Bounded current-data pivot, also serialized as CSV/XLSX/PDF.

    `truncated` means more groups match the filters. Downloads rerun the query;
    they are not a frozen snapshot or an unbounded complete filtered export.
    All formats disclose the effective scope through X-Explore-* headers.
    """
    q = explore.ExploreQuery(
        measure=measure,
        dimensions=dim,
        start=start,
        end=end,
        status=status_,
        category=category,
        currency=currency,
        country=country,
        vendor_id=vendor_id,
        sort=sort,
        limit=limit,
    )
    try:
        result = await explore.run(db, current.org_id, q)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))

    scope_headers = {
        "X-Explore-Scope": result["scope"],
        "X-Explore-Limit": str(result["limit"]),
        "X-Explore-Returned-Rows": str(result["returned_rows"]),
        "X-Explore-Truncated": str(result["truncated"]).lower(),
    }
    response.headers.update(scope_headers)
    filename = f"explore-up-to-{result['limit']}-groups"
    if format == "csv":
        return Response(
            content=explore.to_csv(result),
            media_type="text/csv",
            headers={
                **scope_headers,
                "Content-Disposition": content_disposition(f"{filename}.csv"),
            },
        )
    if format == "xlsx":
        return Response(
            content=await run_in_threadpool(report_writers.to_xlsx, result),  # PERF-007
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                **scope_headers,
                "Content-Disposition": content_disposition(f"{filename}.xlsx"),
            },
        )
    if format == "pdf":
        return Response(
            content=await run_in_threadpool(report_writers.to_pdf, result),  # PERF-007
            media_type="application/pdf",
            headers={
                **scope_headers,
                "Content-Disposition": content_disposition(f"{filename}.pdf"),
            },
        )
    return result
