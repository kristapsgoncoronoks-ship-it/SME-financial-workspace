"""The OFF-INVOICE rebate layer — recording it, merging it into `net_eur_eff`,
and guarding its source (G4.2; R50; `BA_fleet_fuel.md` §4.2, §5.1, §2.5).

THE HARVESTED DEFINITION, VERBATIM
-----------------------------------
`BA_fleet_fuel.md` §4.2's *"`net_eur_eff` — the two-tier discount model (a
business rule, not a technicality)"*:

    "**On-invoice** discounts (TFC hub discount -0.205/L, E100 station-colour
     tiers 0.08-0.23/L, MOEVE PRN off pump PVP, DKV 1.30 SEK/L + 5.63% service
     fee) are already inside `net_eur`.
     **Off-invoice** rebates land ONLY in `net_eur_eff`. **The canonical case is
     Q8/Port One:** Q8 invoices at **LIST price** per country; Port One issues a
     **SEPARATE rebate invoice per country**. The pair must always be
     reconciled. This is the entire reason the `net_eur_eff` column exists."

and R50: *"**`net_eur_eff` carries off-invoice rebate layers** … **Guard the
input source** so a raw file cannot silently replace an adjusted one."*
Acceptance: *"Feed the un-adjusted source ⇒ the pipeline **fails or warns
loudly**, it does not silently produce list-price analytics."*

WHAT WAS ACTUALLY TRUE BEFORE THIS MODULE
-------------------------------------------
`net_eur_eff` had exactly one writer — `fuel_ingest.ingest_transaction`'s
`q2(net_eur_eff if net_eur_eff is not None else net_eur)` — and no production
caller ever passed a value: `statement_ingest` omits the argument and all seven
parsers say in as many words that the merge is G4.2's. So `net_eur_eff` was an
exact copy of `net_eur` on every stored row, while `contract_audit._breach_for`
already read it as though a merge had happened. Its `applied = eur_l_doc -
eur_l_eff` term was therefore identically zero and its `short discount` flag
over-claimed by the WHOLE contracted rebate — a figure `overcharge.open_claim`
freezes and `overcharge_pack.build_claim_letter` prints in a 30-day payment
demand. This module is what makes that arithmetic true.

THE TWO HALVES OF THE SOURCE GUARD
------------------------------------
§4.2 states the hazard R50 guards against: *"the Q8 rebate layer depends on
`month_config.FILES["Q8"]` pointing at the *adjusted* workbook. There is no
assertion that it does — swapping in the raw file **silently loses the rebate
layer**, corrupting every price/benchmark/overpay figure."* Two failure modes,
two answers:

1. **A rebate must never be INFERRED.** `merge_period` reads recorded
   `VatOffInvoiceRebate` rows and nothing else — there is no code path here
   that derives, estimates or carries a rebate forward. `record_rebate` refuses
   a blank `source_ref`/`source_party` (fail-CLOSED, `rebate_source_required`)
   before any query, and the table repeats both as `CHECK`s.
2. **A rebate layer must never silently DISAPPEAR.** That is a question about a
   period with NO recorded row, so no constraint can answer it.
   `missing_source_warnings` does, and the expectation is learned from history
   exactly as §2.5's "Expected rebate" row prescribes: *"Learns the typical €/L
   rebate per (supplier, country) from all history; flags a line where it is
   **missing** … Rationale: catches rebates that arrive on a **separate invoice
   we often don't see** (the Q8/Port One case)."* A `(supplier, country)` with a
   recorded source in an EARLIER period, litres in THIS period and no source for
   it is precisely "the raw file was swapped in".

Per master-context §4.19 that warning is ADVISORY — it never blocks and changes
no euro. R50 says *"fails **or** warns loudly"*: this module warns at the
analytics surface and FAILS everywhere a wrong figure would otherwise be
written (see "fail-closed refusals" below).

THE ALLOCATION — A DOCUMENTED INTERPRETATION
----------------------------------------------
The spec gives the LAYER but no allocation formula: a rebate invoice is one
lump per (country, period) while `net_eur_eff` is per line. This module states
its rule rather than guessing one (the same discipline as `contract_audit`'s
most-specific-wins term tie-break):

    **pro-rata by `qty` (litres).**

Three reasons, all checkable: (a) §5.1's rebate vocabulary is uniformly €/L
(0.08-0.23/L, 0.205/L, 1.30 SEK/L) — a fuel rebate is a per-litre instrument;
(b) it makes `applied = eur_l_doc - eur_l_eff` a CONSTANT €/L across the
country's lines, which is the shape `contract_audit` compares against a €/L
`expected_discount_eur_l`; (c) a value-weighted alternative would make `applied`
vary with the pump price and so manufacture short-discount findings on exactly
the expensive lines. Lines with `qty == 0` (§4.2 row 9's promo corrections)
receive nothing — they have no litres to rebate.

ROUNDING (§4.9) — WHY THE ALLOCATION IS CUMULATIVE
----------------------------------------------------
Quantizing each line's share independently loses or invents cents: the shares
would not sum to the recorded rebate, and the difference would silently become
an unexplained euro on a price surface. Instead the allocation walks the lines
in `line_seq` order carrying a CUMULATIVE litre count and quantizes the running
total:

    cum_eur_i = q2(rebate_total * cum_qty_i / total_qty)
    share_i   = cum_eur_i - cum_eur_(i-1)

so `Σ share_i == rebate_total` EXACTLY, by construction, with one `q2`
(ROUND_HALF_UP) per line and no residual fudge. Every value is `Decimal`; `qty`
stays un-quantized (§4.2 row 9 / master-context §4.9's own €/L-denominator
carve-out).

IDEMPOTENCY — RECOMPUTE FROM `net_eur`, NEVER FROM `net_eur_eff`
------------------------------------------------------------------
`merge_period` always derives the new effective net from the AS-INVOICED
`net_eur`, never from the column's current value. That one rule is what makes
re-running the close a no-op: the second run computes the same figures, finds
every row already equal, writes nothing and audits nothing. It is also what
makes a recorded-rebate CORRECTION self-healing — the next close simply
recomputes from the invoiced net with the corrected rebate, rather than
compounding a delta onto an already-adjusted figure.

WHO IS ALLOWED TO WRITE THE FIGURE (§3.H, the engine boundary)
----------------------------------------------------------------
`record_rebate` is web-reachable and writes ONLY the rebate registry — it never
touches a transaction. The merge itself is a CLOSE STAGE (`close.run_close`),
because `net_eur_eff` on an already-validated transaction is product data the
ENGINE owns; a web request must not mutate it. A merge refusal propagates to
`jobs.run_once`'s whole-session rollback, which is what makes "halts on the
first failure, nothing partially applied" literal for free (see `close.py`'s
docstring).

FAIL-CLOSED REFUSALS (resolved BEFORE any write — the `statement_ingest`
two-phase guarantee: a refused merge leaves ZERO rows changed)
-------------------------------------------------------------------------
- `rebate_has_no_transactions` — a rebate recorded against a (supplier, country,
  period) with no lines. The rebate would vanish; vanishing is the disappearance
  R50 exists to prevent.
- `rebate_no_litres_to_allocate` — every line of that country has `qty == 0`.
  There is nothing to allocate per litre and silently dropping the rebate is the
  same disappearance.
- `rebate_exceeds_net` — an allocated share that would drive a line's effective
  net to zero or below. A non-positive effective net corrupts every €/L computed
  from it, and a rebate bigger than the invoiced net is a data error, not a
  discount.

§4.14 — WHY NO CROSS-CURRENCY SUM CAN HAPPEN HERE
---------------------------------------------------
`merge_period` totals `amount_eur` only. A rebate that cannot reach EUR is
refused at RECORDING (`fx_rate_unavailable`, §4.15), so a non-EUR figure can
never enter the sum — the same structural argument `contract_audit` makes about
`net_eur`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import PermissionError, ValidationError
from app.core.money import q2
from app.models.transport.fuel_transaction import FuelTransaction
from app.models.transport.off_invoice_rebate import VatOffInvoiceRebate
from app.services import audit, fx, modules
from app.services.transport import queries

_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_COUNTRY_RE = re.compile(r"^[A-Z]{2}$")

# The `fx_source` provenance values this module can produce — `app/models/fx.py`
# / ADR-0010's platform-wide enum. There is deliberately no `stated`: a rebate
# invoice states an AMOUNT, never a rate (see the module docstring).
FX_SOURCE_EUR = "eur"
FX_SOURCE_ECB = "ecb"

# Read-only interactive capacity limits, not close-engine accounting limits.
# Bound allocation objects, document grouping and warning inputs separately;
# the document cap also bounds the paired fuel filter to 10,000 SQL binds.
# Every bounded read fetches one extra row and refuses the WHOLE preview on
# overflow; a partial source population must never produce an apparent total.
PREVIEW_MAX_FUEL_ROWS = 20_000
PREVIEW_MAX_REBATE_ROWS = 5_000
PREVIEW_MAX_HISTORY_ROWS = 20_000
PREVIEW_MAX_ACTIVE_PAIRS = 20_000


@dataclass(frozen=True)
class RebateAllocation:
    """One transaction's share of a country's recorded rebate total."""

    fuel_transaction_id: str
    supplier: str
    country: str
    qty: Decimal
    net_eur: Decimal
    share_eur: Decimal
    old_net_eur_eff: Decimal
    new_net_eur_eff: Decimal


@dataclass(frozen=True)
class MergeResult:
    """What one `merge_period` run did. `changed` counts rows whose stored
    `net_eur_eff` actually MOVED — a re-run reports 0 with the same
    `rebate_total_eur`, which is the observable form of idempotency."""

    period: str
    rebate_total_eur: Decimal
    lines_allocated: int
    changed: int
    allocations: tuple[RebateAllocation, ...]
    source_warnings: tuple[str, ...]


async def _require_module(db: AsyncSession, org_id: str) -> None:
    """`modules.is_enabled`, never `require_enabled` — a service signals with
    `AppError`, not `fastapi.HTTPException` (ADR-P3 rule 3). Fails CLOSED."""
    if not await modules.is_enabled(db, org_id, "transport"):
        m = modules.MODULES_BY_KEY["transport"]
        raise PermissionError(f"The {m.name} module is not activated.", code="module_not_enabled")


def validate_period(period: str) -> None:
    """`"YYYY-MM"` — `FuelTransaction.period`'s own shape, the same refusal code
    `contract_audit.validate_period` raises. Fails CLOSED: merging a typo'd
    month would silently leave the real month at list price, which looks exactly
    like "this supplier grants no rebate"."""
    if not _PERIOD_RE.match(period):
        raise ValidationError(
            f"'{period}' is not a valid period — use YYYY-MM", code="invalid_period"
        )


def _validate_country(country: str) -> str:
    up = (country or "").upper()
    if not _COUNTRY_RE.match(up):
        raise ValidationError(
            f"'{country}' is not an ISO 3166-1 alpha-2 country", code="invalid_country"
        )
    return up


def _require_source(source_ref: str, source_party: str) -> tuple[str, str]:
    """R50's identified-source guard, fail-CLOSED and BEFORE any query.

    Both are stripped first: `"  "` is what an unchecked form post produces and
    it is exactly as unsourced as `""`. A rebate with no traceable document is
    the "raw file silently replaced the adjusted one" state, so it is refused
    rather than recorded with a blank provenance."""
    ref = (source_ref or "").strip()
    party = (source_party or "").strip()
    if not ref or not party:
        raise ValidationError(
            "An off-invoice rebate must name its source document and the party "
            "that issued it — a rebate is never inferred (R50).",
            code="rebate_source_required",
        )
    return ref, party


async def _resolve_eur(
    db: AsyncSession, *, amount_local: Decimal, currency: str, on_date: date
) -> tuple[Decimal, Decimal | None, Decimal | None, date | None, str]:
    """(amount_eur, fx_rate, fx_ecb_rate, fx_ecb_date, fx_source) — §4.15.

    EUR is the identity and involves no rate at all. Anything else resolves
    through `fx.to_eur` at the DOCUMENT's own date (ECB publishes units per 1
    EUR; converting to EUR DIVIDES). No cached rate ⇒ refuse: `amount_eur` is
    NOT NULL, so "never a guessed number" here means "no row at all", the same
    branch and the same `fx_rate_unavailable` code `statement_ingest` raises."""
    cur = (currency or "").upper()
    if cur == "EUR":
        return q2(amount_local), None, None, None, FX_SOURCE_EUR

    eur, resolved = await fx.to_eur(db, amount_local, cur, on_date)
    if eur is None or resolved is None:
        raise ValidationError(
            f"No ECB rate is available for {cur} on {on_date.isoformat()} — the "
            "rebate is refused rather than converted at a guessed rate.",
            code="fx_rate_unavailable",
        )
    return q2(eur), resolved.rate, resolved.rate, resolved.rate_date, FX_SOURCE_ECB


async def record_rebate(
    db: AsyncSession,
    org_id: str,
    *,
    supplier: str,
    country: str,
    period: str,
    source_ref: str,
    source_party: str,
    rebate_date: date,
    currency: str,
    amount_local: Decimal,
    note: str | None = None,
) -> VatOffInvoiceRebate:
    """Record (or correct) ONE off-invoice rebate document.

    Writes the rebate REGISTRY only — no `fuel_transactions` row is touched
    here. The effective net is recomputed by the ENGINE at the close
    (`close.run_close` → `merge_period`), because `net_eur_eff` on an
    already-validated transaction is product data a web request must not mutate
    (§3.H).

    Re-recording the same `(supplier, country, period, source_ref)` is the SAME
    document — a correction — and updates it in place, audited field-by-field
    old→new (§4.16). A different `source_ref` for the same country/period is a
    genuinely different document (a rebate plus a later credit note) and becomes
    a second row; `merge_period` sums them.

    Gate order, fails CLOSED: module entitlement → period/country shape → the
    R50 source guard → the amount → FX. Nothing is written until all five pass.
    """
    await _require_module(db, org_id)
    validate_period(period)
    ctry = _validate_country(country)
    ref, party = _require_source(source_ref, source_party)

    amount = q2(amount_local)
    if amount <= 0:
        raise ValidationError(
            "A rebate amount must be positive — a zero or negative rebate is not a rebate.",
            code="rebate_amount_invalid",
        )

    amount_eur, fx_rate, fx_ecb_rate, fx_ecb_date, fx_source = await _resolve_eur(
        db, amount_local=amount, currency=currency, on_date=rebate_date
    )

    existing = await db.scalar(
        select(VatOffInvoiceRebate).where(
            VatOffInvoiceRebate.org_id == org_id,
            VatOffInvoiceRebate.supplier == supplier,
            VatOffInvoiceRebate.country == ctry,
            VatOffInvoiceRebate.period == period,
            VatOffInvoiceRebate.source_ref == ref,
        )
    )

    if existing is not None:
        changes: dict[str, dict[str, str | None]] = {}
        for field, new_value in (
            ("source_party", party),
            ("rebate_date", rebate_date),
            ("currency", (currency or "").upper()),
            ("amount_local", amount),
            ("amount_eur", amount_eur),
            ("fx_rate", fx_rate),
            ("fx_ecb_rate", fx_ecb_rate),
            ("fx_ecb_date", fx_ecb_date),
            ("fx_source", fx_source),
            ("note", note),
        ):
            old_value = getattr(existing, field)
            if old_value != new_value:
                changes[field] = {
                    "old": None if old_value is None else str(old_value),
                    "new": None if new_value is None else str(new_value),
                }
                setattr(existing, field, new_value)
        if changes:
            await db.flush()
            await audit.record(
                db,
                audit.A.TRANSPORT_REBATE_UPDATE,
                target_type="off_invoice_rebate",
                target_id=existing.id,
                meta={
                    "supplier": supplier,
                    "country": ctry,
                    "period": period,
                    "source_ref": ref,
                    "changes": changes,
                },
                org_id=org_id,
            )
        return existing

    row = VatOffInvoiceRebate(
        org_id=org_id,
        supplier=supplier,
        country=ctry,
        period=period,
        source_ref=ref,
        source_party=party,
        rebate_date=rebate_date,
        currency=(currency or "").upper(),
        amount_local=amount,
        amount_eur=amount_eur,
        fx_rate=fx_rate,
        fx_ecb_rate=fx_ecb_rate,
        fx_ecb_date=fx_ecb_date,
        fx_source=fx_source,
        note=note,
    )
    db.add(row)
    await db.flush()
    await audit.record(
        db,
        audit.A.TRANSPORT_REBATE_RECORD,
        target_type="off_invoice_rebate",
        target_id=row.id,
        meta={
            "supplier": supplier,
            "country": ctry,
            "period": period,
            "source_ref": ref,
            "source_party": party,
            "amount_local": str(amount),
            "currency": row.currency,
            "amount_eur": str(amount_eur),
            "fx_source": fx_source,
        },
        org_id=org_id,
    )
    return row


async def list_rebates(
    db: AsyncSession,
    org_id: str,
    *,
    supplier: str | None = None,
    period: str | None = None,
    country: str | None = None,
) -> list[VatOffInvoiceRebate]:
    """Every recorded rebate document matching the filters, newest period
    first. Read-only; org-scoped like every transport query."""
    await _require_module(db, org_id)
    if period is not None:
        validate_period(period)

    stmt = select(VatOffInvoiceRebate).where(VatOffInvoiceRebate.org_id == org_id)
    if supplier is not None:
        stmt = stmt.where(VatOffInvoiceRebate.supplier == supplier)
    if period is not None:
        stmt = stmt.where(VatOffInvoiceRebate.period == period)
    if country is not None:
        stmt = stmt.where(VatOffInvoiceRebate.country == _validate_country(country))

    stmt = stmt.order_by(
        VatOffInvoiceRebate.period.desc(),
        VatOffInvoiceRebate.supplier,
        VatOffInvoiceRebate.country,
        VatOffInvoiceRebate.source_ref,
    )
    return list(await db.scalars(stmt))


def _allocate(rows: list[FuelTransaction], rebate_total: Decimal) -> list[RebateAllocation]:
    """The pro-rata-by-litres allocation, cumulative so the shares sum EXACTLY
    to `rebate_total` (see the module docstring for both choices).

    Pure and synchronous: it takes already-loaded rows and returns the intended
    new figures WITHOUT writing any of them, which is what lets `merge_period`
    resolve every country before touching the database.
    """
    eligible = [r for r in rows if Decimal(r.qty) > 0]
    total_qty = sum((Decimal(r.qty) for r in eligible), Decimal("0"))
    if total_qty <= 0:
        raise ValidationError(
            "Every line of this supplier/country/period has zero litres, so a "
            "per-litre rebate cannot be allocated — the rebate is refused rather "
            "than silently dropped (R50).",
            code="rebate_no_litres_to_allocate",
        )

    allocations: list[RebateAllocation] = []
    cum_qty = Decimal("0")
    cum_eur = Decimal("0.00")
    # line_seq is unique per entity, while this allocation spans entities.
    # Resolve previously unordered ties identically in a preview and a close.
    for row in sorted(eligible, key=lambda r: (r.line_seq, r.id)):
        cum_qty += Decimal(row.qty)
        next_cum_eur = q2(rebate_total * cum_qty / total_qty)
        share = next_cum_eur - cum_eur
        cum_eur = next_cum_eur

        net_eur = Decimal(row.net_eur)
        new_eff = q2(net_eur - share)
        if new_eff <= 0:
            raise ValidationError(
                f"The rebate allocated to line {row.line_seq} ({share}) is not "
                f"smaller than its invoiced net ({net_eur}) — a non-positive "
                "effective net would corrupt every price derived from it.",
                code="rebate_exceeds_net",
            )
        allocations.append(
            RebateAllocation(
                fuel_transaction_id=row.id,
                supplier=row.supplier,
                country=row.country,
                qty=Decimal(row.qty),
                net_eur=net_eur,
                share_eur=share,
                old_net_eur_eff=Decimal(row.net_eur_eff),
                new_net_eur_eff=new_eff,
            )
        )
    return allocations


def _preview_too_large() -> ValidationError:
    return ValidationError(
        "This rebate preview exceeds the interactive source limit. Select one "
        "supplier, or use the existing monthly close for the complete period. "
        "No partial preview has been calculated.",
        code="rebate_preview_too_large",
        status=413,
    )


async def _plan_period(
    db: AsyncSession,
    org_id: str,
    period: str,
    *,
    supplier: str | None = None,
    preview: bool = False,
) -> tuple[MergeResult, dict[tuple[str, str], list[VatOffInvoiceRebate]]]:
    """The shared, no-write phase of preview and monthly close.

    Resolve ALL supplier/country groups before paging or writing. Preview adds
    bounded source reads only; it never changes the financial denominator.
    """
    await _require_module(db, org_id)
    validate_period(period)

    stmt = select(VatOffInvoiceRebate).where(
        VatOffInvoiceRebate.org_id == org_id, VatOffInvoiceRebate.period == period
    )
    if supplier is not None:
        stmt = stmt.where(VatOffInvoiceRebate.supplier == supplier)
    if preview:
        stmt = stmt.limit(PREVIEW_MAX_REBATE_ROWS + 1)
    rebates = list(await db.scalars(stmt))
    if preview and len(rebates) > PREVIEW_MAX_REBATE_ROWS:
        raise _preview_too_large()

    warnings = await missing_source_warnings(
        db, org_id, period=period, supplier=supplier, _preview=preview
    )

    if not rebates:
        return MergeResult(
            period=period,
            rebate_total_eur=Decimal("0.00"),
            lines_allocated=0,
            changed=0,
            allocations=(),
            source_warnings=warnings,
        ), {}

    groups: dict[tuple[str, str], list[VatOffInvoiceRebate]] = {}
    for r in rebates:
        groups.setdefault((r.supplier, r.country), []).append(r)

    preview_rows: dict[tuple[str, str], list[FuelTransaction]] = {}
    if preview:
        # One bounded read rather than a round trip for every source group.
        # Canonical scope + recorded pairs selects exactly the union of the
        # close engine's per-group populations, including zero-litre rows.
        rows = list(
            await db.scalars(
                queries.fuel_transactions(org_id, period=period, supplier=supplier)
                .where(tuple_(FuelTransaction.supplier, FuelTransaction.country).in_(list(groups)))
                .limit(PREVIEW_MAX_FUEL_ROWS + 1)
            )
        )
        if len(rows) > PREVIEW_MAX_FUEL_ROWS:
            raise _preview_too_large()
        for row in rows:
            preview_rows.setdefault((row.supplier, row.country), []).append(row)

    # Phase 1 — resolve every group before writing anything.
    planned: list[RebateAllocation] = []
    total_eur = Decimal("0.00")
    for (sup, ctry), group in sorted(groups.items()):
        group_total = q2(sum((Decimal(r.amount_eur) for r in group), Decimal("0")))
        if preview:
            rows = preview_rows.get((sup, ctry), [])
        else:
            rows = list(
                await db.scalars(
                    queries.fuel_transactions(org_id, supplier=sup, country=ctry, period=period)
                )
            )
        if not rows:
            raise ValidationError(
                f"A rebate is recorded for {sup}/{ctry} {period} but no fuel "
                "transaction exists for it — the rebate would vanish (R50).",
                code="rebate_has_no_transactions",
            )
        planned.extend(_allocate(rows, group_total))
        total_eur += group_total

    return MergeResult(
        period=period,
        rebate_total_eur=q2(total_eur),
        lines_allocated=len(planned),
        changed=sum(a.old_net_eur_eff != a.new_net_eur_eff for a in planned),
        allocations=tuple(planned),
        source_warnings=warnings,
    ), groups


async def preview_period(
    db: AsyncSession, org_id: str, period: str, *, supplier: str | None = None
) -> MergeResult:
    """A bounded, no-write observation, never authority for a later close."""
    result, _groups = await _plan_period(db, org_id, period, supplier=supplier, preview=True)
    return result


async def merge_period(
    db: AsyncSession,
    org_id: str,
    period: str,
    *,
    supplier: str | None = None,
) -> MergeResult:
    """Re-plan current sources and merge them, only inside the close engine.

    The as-invoiced net, Decimal cumulative-cent allocation, all-groups-before-
    write refusals and idempotent no-audit rerun behavior remain the same. A
    prior web preview is never an input. Interactive preview limits do not
    constrain monthly close.
    """
    result, groups = await _plan_period(db, org_id, period, supplier=supplier)
    planned = result.allocations
    if not planned:
        return result

    # Phase 2 — write only the rows whose figure actually moves.
    by_id = {a.fuel_transaction_id: a for a in planned}
    changed = 0
    for txn in await db.scalars(queries.fuel_transactions(org_id, ids=list(by_id))):
        alloc = by_id[txn.id]
        if Decimal(txn.net_eur_eff) == alloc.new_net_eur_eff:
            continue  # idempotent re-run — no write, no audit row
        txn.net_eur_eff = alloc.new_net_eur_eff
        changed += 1
        refs = sorted(r.source_ref for r in groups[(alloc.supplier, alloc.country)])
        await audit.record(
            db,
            audit.A.TRANSPORT_REBATE_MERGE,
            target_type="fuel_transaction",
            target_id=txn.id,
            meta={
                "period": period,
                "supplier": alloc.supplier,
                "country": alloc.country,
                "old_net_eur_eff": str(alloc.old_net_eur_eff),
                "new_net_eur_eff": str(alloc.new_net_eur_eff),
                "rebate_share_eur": str(alloc.share_eur),
                "source_refs": refs,
            },
            org_id=org_id,  # explicit — runs inside the close job, not a request
        )
    await db.flush()

    return replace(result, changed=changed)


async def missing_source_warnings(
    db: AsyncSession,
    org_id: str,
    *,
    period: str,
    supplier: str | None = None,
    _preview: bool = False,
) -> tuple[str, ...]:
    """R50's second half — "the adjusted file was silently replaced by the raw
    one", stated as a warning per §2.5's "Expected rebate" rationale.

    A `(supplier, country)` is EXPECTED to carry an off-invoice rebate layer once
    one has ever been recorded for it — the expectation is learned from history,
    which is §2.5's own mechanism (*"Learns the typical €/L rebate per (supplier,
    country) from all history; flags a line where it is missing … catches rebates
    that arrive on a separate invoice we often don't see"*). This function flags
    the pair when it has litres in `period` and NO recorded source for it.

    ADVISORY (§4.19): it returns strings. It blocks nothing, gates nothing and
    changes no figure — it exists so that R50's *"it does not silently produce
    list-price analytics"* is literally true at the surface that produces them
    (`contract_audit.audit`, which carries these on its result).

    Fails toward SILENCE for a supplier that has never had a rebate layer: with
    no history there is no expectation, and crying wolf on every ordinary
    supplier would train an operator to ignore the one warning that matters.
    """
    await _require_module(db, org_id)
    validate_period(period)

    hist_stmt = select(
        VatOffInvoiceRebate.supplier,
        VatOffInvoiceRebate.country,
        VatOffInvoiceRebate.period,
    ).where(VatOffInvoiceRebate.org_id == org_id)
    if supplier is not None:
        hist_stmt = hist_stmt.where(VatOffInvoiceRebate.supplier == supplier)
    if _preview:
        hist_stmt = hist_stmt.where(VatOffInvoiceRebate.period <= period).limit(
            PREVIEW_MAX_HISTORY_ROWS + 1
        )

    history = list(await db.execute(hist_stmt))
    if _preview and len(history) > PREVIEW_MAX_HISTORY_ROWS:
        raise _preview_too_large()

    expected: dict[tuple[str, str], str] = {}
    present: set[tuple[str, str]] = set()
    for sup, ctry, per in history:
        if per == period:
            present.add((sup, ctry))
        elif per < period:
            # Keep the most recent earlier period — it is what the warning
            # quotes so an operator knows which document to go and find.
            prior = expected.get((sup, ctry))
            if prior is None or per > prior:
                expected[(sup, ctry)] = per

    missing = sorted(k for k in expected if k not in present)
    if not missing:
        return ()

    active_stmt = queries.fuel_transactions(
        org_id, period=period, supplier=supplier
    ).with_only_columns(FuelTransaction.supplier, FuelTransaction.country)
    if _preview:
        active_stmt = active_stmt.distinct().limit(PREVIEW_MAX_ACTIVE_PAIRS + 1)
    active_rows = list(await db.execute(active_stmt))
    if _preview and len(active_rows) > PREVIEW_MAX_ACTIVE_PAIRS:
        raise _preview_too_large()
    active = {(s, c) for s, c in active_rows}

    return tuple(
        f"off-invoice rebate source missing: {sup}/{ctry} has fuel activity in "
        f"{period} and carried a recorded rebate in {expected[(sup, ctry)]}, but "
        "no rebate document is recorded for this period — prices for it are at "
        "LIST, not effective (R50)."
        for sup, ctry in missing
        if (sup, ctry) in active
    )


__all__ = [
    "FX_SOURCE_ECB",
    "FX_SOURCE_EUR",
    "MergeResult",
    "RebateAllocation",
    "list_rebates",
    "merge_period",
    "missing_source_warnings",
    "preview_period",
    "record_rebate",
    "validate_period",
]
