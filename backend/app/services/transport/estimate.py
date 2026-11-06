"""The refund-estimate funnel (G4.8; `BA_fleet_fuel.md` §2.3's `/estimate`
row, R43, R53).

*"Upload last quarter → see your refund opportunity."* A prospect hands over a
fuel-card statement; this parses it in memory and says roughly how much VAT is
sitting in it, per refund country, with the Art. 17 minimum flagged.

WHAT MAKES THIS DIFFERENT FROM EVERY OTHER INTAKE PATH
--------------------------------------------------------
It writes NOTHING. `statement_ingest` exists to turn a statement into
`fuel_transactions` rows that a claim is eventually built from; this turns the
same bytes into a number on a screen and then forgets them. R43 states the rule
verbatim — **in-memory only, no product-DB write** — and the reason is not
tidiness: a sales preview that quietly created rows would put a prospect's data
into a workspace's real tables before anyone decided to become a customer.
`test_wo_ac_estimate.py` asserts the absence directly rather than trusting this
docstring.

The one DB access here is `fx.to_eur`, which READS the ECB rate cache. A read
is not a write, and the alternative — carrying a second conversion path — is
the thing this codebase refuses everywhere else.

WHY `recoverable_eur` IS SIMPLY THE INVOICED VAT
--------------------------------------------------
§2.3, verbatim: `recoverable_eur = vat_eur`, *"invoiced VAT assumed
recoverable"*. That is a deliberately generous assumption and it is what makes
this a SALES PREVIEW and not a filing: the real pipeline applies supplier
registration (R15), the receipt-control gate, the document gate, waivers,
Art. 17 minimums and the fee before a euro is claimable. Every one of those can
only reduce the figure. The estimate therefore reads as an upper bound, and
`CAVEAT` says so on every response — R53's *"indicative / advisory — verify
before relying"*, which that rule forbids flattening into the language used for
a contractual claim-back.

WHAT THE PARSER ALREADY GUARANTEES, SO THIS MODULE DOES NOT RE-CHECK
-----------------------------------------------------------------------
Every shipped parser validates the country and currency codes structurally and
raises on a bad one (`eurowag.py`: *"row 2: invalid country code ''"*), which
refuses the WHOLE file. An earlier draft of this module carried its own
"a line with no country is counted as unattributable" branch; it was dead code,
and worse than dead — counting such a line would have been a quieter, weaker
answer than the refusal the parser already gives. The funnel inherits that
guarantee rather than re-implementing a softer version of it.

WHY A LINE THAT CANNOT BE CONVERTED IS COUNTED, NOT DROPPED
--------------------------------------------------------------
`fx.to_eur` returns `None` when no ECB rate exists for that currency and date —
never a guessed number (`app/models/fx.py`'s platform-wide rule). A funnel that
silently skipped those lines would report a smaller opportunity than the file
contains and give no sign it had done so, which is the failure mode that turns
a sales tool into a misleading one. Unconvertible lines are counted in
`unconverted_lines` and named in the warnings, so the number is always
accompanied by what it could not see.

WHY `below_minimum` CAN BE `None`
-----------------------------------
`minimum.below_minimum` compares in the CORRECT currency for the country —
`vat_local` for Sweden and Denmark, `vat_eur` for everyone else. A country
whose lines arrive in more than one currency has no single `vat_local` to
compare, so the honest answer is "not compared", not `False`. Three states,
never two collapsed: `True` (below), `False` (clears it), `None` (could not
tell). The same discipline `excise.py` applies to a missing rate versus a
placeholder one.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import PermissionError, ValidationError
from app.core.money import q2
from app.services import fx, modules
from app.services.transport import fuel_card_parser
from app.services.transport.claim import validate_ref_period
from app.services.transport.minimum import below_minimum as _below_minimum
from app.services.transport.minimum import min_for

# R53's framing for this analysis, verbatim. `savings.py` carries the
# claim-back and negotiation-evidence wordings; this is the third, and the rule
# is that they are NOT interchangeable.
CAVEAT = (
    "Indicative — verify before relying. This is a sales preview, never a filed "
    "figure: it assumes every invoiced euro of VAT is recoverable, before "
    "supplier registration, receipt control, document checks, waivers, national "
    "minimums and fees are applied. Each of those can only reduce it."
)


@dataclass(frozen=True)
class CountryEstimate:
    """One refund country's share of an uploaded statement."""

    country: str
    lines: int
    litres: Decimal
    # The headline figure: invoiced VAT, converted to EUR. Excludes any line
    # `fx` could not convert — see `unconverted_lines`.
    vat_eur: Decimal
    # Present only when every line for this country shares ONE currency; that
    # is what makes a local-basis (SE/DK) minimum comparison possible at all.
    vat_local: Decimal | None
    currency: str | None
    # True = below the Art. 17 threshold, False = clears it, None = could not
    # be compared in the country's own currency. See the module docstring.
    below_minimum: bool | None
    threshold: Decimal
    threshold_currency: str
    unconverted_lines: int


@dataclass(frozen=True)
class EstimateResult:
    network: str
    period: str
    lines: int
    countries: list[CountryEstimate]
    # The sum of every country's `vat_eur` — the "refund opportunity" headline.
    recoverable_eur: Decimal
    unconverted_lines: int
    warnings: list[str] = field(default_factory=list)
    caveat: str = CAVEAT


async def _gate(db: AsyncSession, org_id: str) -> None:
    """The WO-49 convention: no transport service entry point trusts its caller
    to have checked the entitlement (ADR-P3 rule 3)."""
    if not await modules.is_enabled(db, org_id, "transport"):
        m = modules.MODULES_BY_KEY["transport"]
        raise PermissionError(f"The {m.name} module is not activated.", code="module_not_enabled")


async def estimate(
    db: AsyncSession,
    org_id: str,
    *,
    filename: str,
    content: bytes,
    period: str,
) -> EstimateResult:
    """Parse `content` in memory and report the per-country VAT sitting in it.

    WRITES NOTHING. The caller must not commit on this path; the route does
    not, and `test_wo_ac_estimate.py` proves the session stays clean.

    `period` is the claim period the estimate is FRAMED as (`YYYY-Qn` /
    `YYYY-YEAR`), because Art. 17's threshold is €400 quarterly and €50 for a
    full year — a preview that used the wrong one would flag the wrong
    countries as too small to bother with. It is validated by the SAME
    `claim.validate_ref_period` a real claim uses, never a second regex.
    """
    await _gate(db, org_id)
    validate_ref_period(period)

    try:
        parsed = await fuel_card_parser.run_async(filename, content)
    except ValueError as exc:
        # The parser's own fail-closed network detection. Surfaced as the
        # sentence it wrote (it names the supported networks), not flattened.
        raise ValidationError(str(exc), code="unrecognized_statement") from exc

    litres: dict[str, Decimal] = defaultdict(Decimal)
    vat_eur: dict[str, Decimal] = defaultdict(Decimal)
    vat_local: dict[str, Decimal] = defaultdict(Decimal)
    line_count: dict[str, int] = defaultdict(int)
    unconverted: dict[str, int] = defaultdict(int)
    currencies: dict[str, set[str]] = defaultdict(set)

    for line in parsed.lines:
        # The parser has already refused a blank or malformed code — see the
        # module docstring on what it guarantees.
        country = line.country.upper()
        line_count[country] += 1
        litres[country] += line.qty
        vat_local[country] += line.vat_local
        currencies[country].add((line.currency or "EUR").upper())

        eur, _provenance = await fx.eur_total(
            db, line.vat_local, line.currency or "EUR", line.txn_date, None
        )
        if eur is None:
            unconverted[country] += 1
            continue
        vat_eur[country] += eur

    countries: list[CountryEstimate] = []
    for country in sorted(line_count):
        codes = currencies[country]
        one_currency = codes.pop() if len(codes) == 1 else None
        if one_currency is not None:
            codes.add(one_currency)  # `pop` mutated the set; put it back.

        threshold_currency, threshold, basis = min_for(country, is_annual=period.endswith("YEAR"))
        local = q2(vat_local[country]) if one_currency is not None else None

        # A local-basis country needs a single local figure IN ITS OWN
        # currency. Mixed currencies, or a currency that is not the threshold's,
        # means the comparison cannot be made — say so rather than guess.
        if basis == "local" and (local is None or one_currency != threshold_currency):
            below: bool | None = None
        else:
            below = _below_minimum(
                country=country,
                ref_period=period,
                vat_eur=q2(vat_eur[country]),
                vat_local=local,
            )

        countries.append(
            CountryEstimate(
                country=country,
                lines=line_count[country],
                litres=q2(litres[country]),
                vat_eur=q2(vat_eur[country]),
                vat_local=local,
                currency=one_currency,
                below_minimum=below,
                threshold=threshold,
                threshold_currency=threshold_currency,
                unconverted_lines=unconverted[country],
            )
        )

    warnings = list(parsed.warnings)
    total_unconverted = sum(unconverted.values())
    if total_unconverted:
        warnings.append(
            f"{total_unconverted} line(s) could not be converted to EUR (no exchange "
            "rate on file for that currency and date) and are NOT in the figures "
            "above — the real opportunity is larger than this estimate shows."
        )

    return EstimateResult(
        network=parsed.network,
        period=period,
        lines=len(parsed.lines),
        countries=countries,
        recoverable_eur=q2(sum((c.vat_eur for c in countries), Decimal("0"))),
        unconverted_lines=total_unconverted,
        warnings=warnings,
    )
