"""Transport routes slice 6 — the off-invoice rebate registry (WO-84, G4.2/R50).

`BA_fleet_fuel.md` §4.2's second discount tier arrives on a document no
fuel-card statement parser ever sees: *"Q8 invoices at **LIST price** per
country; Port One issues a **SEPARATE rebate invoice per country**. The pair
must always be reconciled."* This surface is how that second document gets
RECORDED, which is the only way a euro is ever allowed to reach `net_eur_eff`
(R50's *"guard the input source"*).

WHAT THIS SURFACE DELIBERATELY CANNOT DO
------------------------------------------
It cannot change a transaction. `POST` writes the rebate REGISTRY only; the
recompute of `fuel_transactions.net_eur_eff` is a CLOSE stage
(`close.run_close` → `rebate.merge_period`), because an already-validated
transaction's derived figure is product data the ENGINE owns and a web request
must not mutate it (`BA_fleet_fuel.md` §3.H, the engine ↔ app write boundary).
There is therefore no "merge now" verb here, by design. The read-only preview
observes the same planner; monthly close independently re-plans before applying.

It also cannot state the euro. `RebateIn` has no `amount_eur` field: the server
resolves it through `fx.to_eur` at the document's own date and REFUSES when the
ECB cache has no rate (§4.10, §4.15). A client supplies a document, never a
converted total.

Thin controllers ONLY (engineering-rules §3), the WO-76/WO-77/WO-81/WO-82
pattern: parse → structural permission gate → call the already-gated service →
shape the response. Every refusal a client can see (`module_not_enabled`,
`invalid_period`, `invalid_country`, `rebate_source_required`,
`rebate_amount_invalid`, `fx_rate_unavailable`) is raised BY THE SERVICE as an
`app.core.errors.AppError` and rendered by the one `app.main` handler as
`{"detail", "code"}` — this module maps nothing, so the wire vocabulary cannot
drift from the service layer (master-context §4.20).

AUTHORIZATION (ADR-0024, structural)
--------------------------------------
Router-level **`TRANSPORT_READ`**; the write verb overrides to **`VAT_WRITE`** —
identical to `overcharges.py`, and for the same reasons that module states at
length: `TRANSPORT_READ` is the permission `fuel.py` reserved for the derived
analytics slices, and `VAT_WRITE` is this codebase's ONE transport write
permission. `VAT_SUBMIT` is deliberately NOT used: recording a rebate acquires
and releases no invoice LOCK on a VAT claim and touches no VAT figure — the
rebate layer is a PRICE concern, not a refund one. **No permission member is
added** (§10).

THE PRICE BASIS
-----------------
A rebate is a component of the NET EUR/L, final basis (§3.G G1: *"VAT excluded,
rebates applied"*). This surface serves the rebate DOCUMENT rather than a price,
so it states no €/L of its own. Preview amounts are per-transaction NET EUR
totals, with VAT excluded and the proposed rebate included. The €/L basis is
stated where the price is served
(`overcharges.py`'s `price_basis`, and both WO-83 artifacts).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.deps import CurrentUser, DbSession, require_perm
from app.core import authz
from app.models.transport.off_invoice_rebate import VatOffInvoiceRebate
from app.schemas.transport_rebate import (
    RebateAllocationOut,
    RebateIn,
    RebateOut,
    RebatePreviewOut,
)
from app.services.transport import rebate as rebate_svc

# Structural authorization (ADR-0024): router-level TRANSPORT_READ; the
# mutation declares the stricter VAT_WRITE per-route below.
router = APIRouter(
    prefix="/transport",
    tags=["transport"],
    dependencies=[Depends(require_perm(authz.Permission.TRANSPORT_READ))],
)
_WRITE = [Depends(require_perm(authz.Permission.VAT_WRITE))]


def _out(row: VatOffInvoiceRebate) -> RebateOut:
    return RebateOut(
        id=row.id,
        supplier=row.supplier,
        country=row.country,
        period=row.period,
        source_ref=row.source_ref,
        source_party=row.source_party,
        rebate_date=row.rebate_date,
        currency=row.currency,
        amount_local=row.amount_local,
        amount_eur=row.amount_eur,
        fx_rate=row.fx_rate,
        fx_ecb_rate=row.fx_ecb_rate,
        fx_ecb_date=row.fx_ecb_date,
        fx_source=row.fx_source,
        note=row.note,
    )


@router.get("/rebates", response_model=list[RebateOut])
async def list_rebates(
    current: CurrentUser,
    db: DbSession,
    supplier: str | None = Query(default=None, description="fuel-card / toll-network code"),
    period: str | None = Query(default=None, description="YYYY-MM"),
    country: str | None = Query(default=None, min_length=2, max_length=2),
):
    """Every recorded off-invoice rebate document, newest period first.

    This is also the operator's answer to a source-guard warning: a
    `(supplier, country)` flagged by `contract_audit`'s `source_warnings` is one
    whose document is missing from exactly this list.
    """
    rows = await rebate_svc.list_rebates(
        db, current.org_id, supplier=supplier, period=period, country=country
    )
    return [_out(r) for r in rows]


@router.post("/rebates", response_model=RebateOut, dependencies=_WRITE)
async def record_rebate(current: CurrentUser, db: DbSession, body: RebateIn):
    """Record (or correct) ONE off-invoice rebate document.

    Keyed on `(supplier, country, period, source_ref)`: re-posting the same
    document number is a CORRECTION and updates it in place, audited old→new
    (§4.16); a different `source_ref` for the same country/period is a genuinely
    different document (a rebate plus a later credit note) and becomes a second
    row that the merge sums.

    Writes NOTHING to `fuel_transactions` — the effective net is recomputed by
    the engine at the next close (see the module docstring).
    """
    row = await rebate_svc.record_rebate(
        db,
        current.org_id,
        supplier=body.supplier,
        country=body.country,
        period=body.period,
        source_ref=body.source_ref,
        source_party=body.source_party,
        rebate_date=body.rebate_date,
        currency=body.currency,
        amount_local=body.amount_local,
        note=body.note,
    )
    await db.commit()
    await db.refresh(row)
    return _out(row)


@router.get("/rebates/preview", response_model=RebatePreviewOut)
async def preview_rebates(
    current: CurrentUser,
    db: DbSession,
    period: str = Query(
        min_length=7, max_length=7, description="YYYY-MM — the complete period to preview"
    ),
    supplier: str | None = Query(default=None, max_length=60),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=100),
):
    """Observe the existing close planner without writing or applying anything.

    All countries/entities of period + optional exact supplier are planned
    before display paging. A source-limit or financial refusal returns no
    partial summary. The engine independently re-plans when close actually runs.
    """
    result = await rebate_svc.preview_period(db, current.org_id, period, supplier=supplier)
    start = (page - 1) * page_size
    return RebatePreviewOut(
        period=result.period,
        supplier=supplier,
        rebate_total_eur=result.rebate_total_eur,
        lines_allocated=result.lines_allocated,
        changed=result.changed,
        source_warnings=result.source_warnings,
        allocations=[
            RebateAllocationOut.model_validate(a)
            for a in result.allocations[start : start + page_size]
        ],
        page=page,
        page_size=page_size,
    )
