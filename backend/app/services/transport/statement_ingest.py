"""Fuel-card statement ingestion (G3.2 slice 1) — the ONE service function
that turns a raw statement file into `fuel_transactions` rows (G1.2,
`fuel_ingest.ingest_transaction`) and learned per-country seller entities
(G3.1, `supplier_entity.learn_registration`). This is the real caller both
of those functions have been waiting for: `fuel_ingest.ingest_transaction`
(WO-50) and `learn_registration` (WO-61) each shipped with "no real caller
yet" — this module is that caller.

TWO-PHASE WRITE: RESOLVE EVERYTHING BEFORE TOUCHING THE DATABASE
------------------------------------------------------------------------
`ingest_statement` resolves EVERY line's EUR figures (§4.15's FX
convention) in a first pass, entirely in memory, before calling
`fuel_ingest.ingest_transaction` for any of them. A statement with 40 lines
where line 37 cannot be converted (no cached ECB rate for its currency on
its date) therefore writes ZERO rows, not 36 — "nothing partially applied"
without depending on a job-framework rollback wrapper this order does not
have (unlike `close.run_close`, which gets that guarantee for free from
`jobs.run_once`; see that module's docstring). This mirrors
`app.services.expenses.build_items`' own behaviour: it loops calling
`apply_item_fx` per item with NO try/except, so one unconvertible foreign
item aborts the whole report build — the established codebase precedent
for "a batch of money lines is all-or-nothing on FX", not "skip the bad one
silently".

FX PROVENANCE
--------------
EUR-currency lines are the identity conversion (`fx_source="eur"`, no ECB
rate involved). A non-EUR line converts via `app.services.fx.to_eur` at the
line's OWN `txn_date` — never a guessed/blended rate — and BOTH `net_local`
and `vat_local` convert using the SAME resolved rate (one ECB lookup per
line, not two; the platform's ECB convention divides foreign units by the
rate to reach EUR). No cached rate for that (currency, date) ⇒ `to_eur`
returns `(None, None)` ⇒ `ValidationError(code="fx_rate_unavailable")` —
never a stored NULL (`fuel_transactions.net_eur`/`vat_eur` are NOT NULL)
and never a guessed number (master-context §4.15 verbatim).

THE SUPPLIER-STATED-EUR BRANCH (WO-65/G3.2 slice 4 — DKV's money model)
------------------------------------------------------------------------
A parser whose network's harvested money model is "trust the supplier's
per-line EUR" (DKV: `BA_fleet_fuel.md` §5.1 "trusts the supplier's
per-line EUR and pro-rates") carries that figure in
`ParsedFuelLine.stated_net_eur`. For such a line the STATEMENT DOCUMENT is
the conversion's source of truth — the same `FxSource.stated` convention
`app.services.expenses.apply_item_fx` already ships ("the claimant stated
the converted amount... record the implied rate so the conversion is fully
documented", ADR-0010): `net_eur` is the stated figure AS GIVEN (never
recomputed via ECB — the invoice is what a refund state audits against);
`vat_eur` is PRO-RATED at the same implied basis (`vat_local *
stated_net_eur / net_local`, pure Decimal, the harvested "and pro-rates"
clause); `fx_rate` records the implied APPLIED rate (`net_local /
stated_net_eur`, foreign units per 1 EUR — `BA_fleet_fuel.md` §4.2's own
definition of the applied-rate column, quantized to the storage column's
6 dp); `fx_ecb_rate`/`fx_ecb_date` freeze the OFFICIAL reference rate
BEST-EFFORT when the cache has one (R56: both the applied and the official
rate frozen per line, so drift is auditable) and stay NULL when it does
not — a stated line NEVER fails on missing ECB coverage ("no coverage ⇒
NULL is stored, never a fabricated pass", §4.2 verbatim), which is the
observable difference from the ECB branch. Fail-CLOSED guard: an implied
rate that is not strictly positive (a zero `net_local`, a zero
`stated_net_eur`, or a sign flip between them) is refused with
`ValidationError(code="fx_stated_inconsistent")` in phase 1 — a
nonsensical implied rate would poison the pro-rated VAT figure that feeds
the claim, so the whole statement writes zero rows (the existing two-phase
guarantee, unchanged).

THE CAPTURE REVIEW GATE (WO-66/G3.3 — R25's first regime)
------------------------------------------------------------
After parsing and BEFORE any FX resolution, the parsed batch passes
through `capture_review.review_statement` — the harvested nine-rule
verdict lattice plus the optional batch tie-out against the invoice
coversheet total (`coversheet_total`, the figure printed on the statement
document, captured by the operator; `None` = no coversheet figure, the
tie constraint simply does not apply). `can_commit=False` (any ERROR
finding, or a tie-out off by more than €0.02 on quantized Decimals)
refuses the WHOLE statement with `code="capture_review_blocked"` before a
single row is written — fail-CLOSED, and deliberately placed ahead of the
FX machinery so the refusal names the actual capture defect ("net must be
> 0") rather than a downstream FX symptom (`fx_stated_inconsistent`).
WARN findings NEVER block (§2.1a verbatim) — they append to
`StatementIngestResult.warnings`, the review surface of this slice.

THE ANTI-DRIFT EXTRACTION BASELINE (WO-70/G3.3 slice 2 — advisory)
--------------------------------------------------------------------
A statement's FIRST fully successful registration records its parsed
per-currency aggregates (line count, q2'd net/vat totals in LOCAL
currency) as the known-good `fuel_extraction_baselines` row set, keyed
by the content's SHA-256 (`BA_fleet_fuel.md` §2.1a "Anti-drift"). A
LATER ingest of the same bytes COMPARES instead of re-recording: net or
vat moved by MORE than 0.02, a changed line count, or a changed
currency set appends an "extraction drift: ..." WARNING — never a block
(the harvested verb is "flags"; §4.19 advisory semantics — see
`extraction_baseline`'s module docstring). This matters doubly here
because `ingest_transaction` is insert-or-no-op on the natural key: a
drifted re-parse writes nothing, so the drift warning is the ONLY
visibility that the parser no longer reproduces the registered figures.
The record/compare step runs AFTER phase 2 + entity learning — a
statement refused by the capture gate or FX resolution leaves no
baseline (only a CONFIRMED extraction is known-good).

WHAT "FLAGGED FOR REVIEW" MEANS (WO-Z — no longer just this response)
------------------------------------------------------------------------
This section used to say the returned `warnings` list "IS the review
surface until a persisted one exists". It exists:
`vat_statement_findings`, written by `statement_review`. The returned
list is unchanged — it is still what the uploader is shown — but it is
now a VIEW of rows that outlive the response rather than the only place
the finding ever was.

A parser-level AMBIGUITY that is not fatal to the figures (no seller
entity detected in the footer, say) still warns instead of blocking, and
is now also a row. A parse or FX failure still RAISES, and the route
records what it can before re-raising.

The refusal carries its verdict rather than describing it: the capture
gate raises `StatementRefused`, a `ValidationError` subclass holding the
structured findings, so the queue can persist which line failed under
which rule instead of parsing that back out of a message string.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import PermissionError, ValidationError
from app.models.transport.fuel_transaction import FuelTransaction
from app.models.transport.supplier_registration import SupplierVatRegistration
from app.services import fx, modules
from app.services.transport import (
    capture_checks,
    capture_review,
    extraction_baseline,
    fuel_card_parser,
    fuel_ingest,
    supplier_entity,
)
from app.services.transport.fuel_card_parser import ParsedFuelLine

_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


@dataclass
class _ResolvedLine:
    line: ParsedFuelLine
    net_eur: Decimal
    vat_eur: Decimal
    fx_rate: Decimal | None
    fx_ecb_rate: Decimal | None
    fx_ecb_date: date | None
    fx_source: str


class StatementRefused(ValidationError):
    """The capture gate blocked registration, WITH the findings that did it.

    Subclasses `ValidationError` deliberately, so every existing
    `except ValidationError` and the AppError handler behave exactly as before
    — the same additive move `capture_failures.CaptureError` made over
    `ValueError`. What it adds is the structured verdict: until WO-Z the
    findings were flattened into the message string at the raise, so the caller
    that most needed them (the review queue) could only have got them back by
    parsing prose.

    `extra()` also puts them on the wire. A refusal the client must ACT on —
    "these lines are why, fix them and re-send" — is exactly the case
    `AppError.extra` exists for.
    """

    code = "capture_review_blocked"

    def __init__(
        self,
        message: str,
        *,
        findings: tuple[capture_review.Finding, ...],
        tie: capture_review.BatchTie | None,
    ):
        super().__init__(message, code=self.code)
        self.findings = findings
        self.tie = tie

    def extra(self) -> dict:
        return {
            "findings": [
                {
                    "severity": f.severity,
                    "code": f.code,
                    "message": f.message,
                    "line_seq": f.line_seq,
                }
                for f in self.findings
                if f.severity == capture_review.ERROR
            ]
        }


@dataclass
class StatementIngestResult:
    """What `ingest_statement` returns — the review surface for this slice
    (see module docstring)."""

    network: str
    period: str
    lines: list[FuelTransaction] = field(default_factory=list)
    entities: list[SupplierVatRegistration] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # WO-Z — the same advisory content, kept SPLIT for the review queue.
    # `warnings` above is the assembled, human-ordered list the uploader is
    # shown and stays exactly as it was. These two are what the queue persists:
    # the capture-review findings still typed (so a row keeps the rule code and
    # the LINE it fired on), and the other three producers' prose, which is
    # about the batch. Re-deriving the split by parsing prefixes back off the
    # assembled list would make the queue's dedup key hostage to a reworded
    # sentence.
    review_findings: tuple[capture_review.Finding, ...] = ()
    queue_warnings: list[str] = field(default_factory=list)


def _validate_period(period: str) -> None:
    if not _PERIOD_RE.match(period):
        raise ValidationError(
            f"'{period}' is not a valid period — use YYYY-MM", code="invalid_period"
        )


async def _resolve_line(db: AsyncSession, line: ParsedFuelLine) -> _ResolvedLine:
    """Resolve ONE line's EUR figures. Raises `ValidationError` (never
    returns a guessed or NULL EUR amount) when no rate is available — see
    module docstring."""
    if line.currency == "EUR":
        return _ResolvedLine(
            line=line,
            net_eur=line.net_local,
            vat_eur=line.vat_local,
            fx_rate=Decimal(1),
            fx_ecb_rate=None,
            fx_ecb_date=None,
            fx_source="eur",
        )

    if line.stated_net_eur is not None:
        # The supplier-stated-EUR branch — see the module docstring. The
        # document's own figure is the conversion; refuse an implied rate
        # that is not strictly positive rather than pro-rate VAT off it.
        stated = line.stated_net_eur
        if line.net_local == 0 or stated == 0 or (line.net_local > 0) != (stated > 0):
            raise ValidationError(
                f"Statement line {line.line_seq} states net_eur={stated} against "
                f"net_local={line.net_local} {line.currency} — the implied exchange "
                "rate is not a positive number, so the stated conversion cannot be "
                "trusted or pro-rated.",
                code="fx_stated_inconsistent",
            )
        # Pro-rate VAT at the EXACT stated basis (unrounded until the
        # ingestion service's q2); the stored applied rate is the implied
        # foreign-units-per-1-EUR figure at the column's 6 dp.
        vat_eur = line.vat_local * stated / line.net_local
        implied_rate = (line.net_local / stated).quantize(Decimal("0.000001"))
        # Best-effort OFFICIAL-reference freeze (R56) — never a failure path:
        # the stated figure does not depend on ECB coverage existing.
        reference = await fx.resolve_rate(db, line.currency, line.txn_date)
        return _ResolvedLine(
            line=line,
            net_eur=stated,
            vat_eur=vat_eur,
            fx_rate=implied_rate,
            fx_ecb_rate=reference.rate if reference is not None else None,
            fx_ecb_date=reference.rate_date if reference is not None else None,
            fx_source="stated",
        )

    net_eur, resolved = await fx.to_eur(db, line.net_local, line.currency, line.txn_date)
    if net_eur is None or resolved is None:
        raise ValidationError(
            f"No ECB exchange rate is available for {line.currency} on {line.txn_date} "
            f"(statement line {line.line_seq}) — refresh the ECB rates before retrying.",
            code="fx_rate_unavailable",
        )
    # Same resolved rate converts vat_local — one ECB lookup per line, not
    # two; both amounts are as-of the SAME txn_date.
    vat_eur = line.vat_local / resolved.rate
    return _ResolvedLine(
        line=line,
        net_eur=net_eur,
        vat_eur=vat_eur,
        fx_rate=resolved.rate,
        fx_ecb_rate=resolved.rate,
        fx_ecb_date=resolved.rate_date,
        fx_source="ecb",
    )


async def ingest_statement(
    db: AsyncSession,
    org_id: str,
    *,
    entity_id: str,
    period: str,
    filename: str,
    content: bytes,
    coversheet_total: Decimal | None = None,
) -> StatementIngestResult:
    """Parse a fuel-card statement (network auto-detected — never
    caller-asserted, so a mislabeled upload can't be miscategorized; see
    `fuel_card_parser`'s "fail-closed network detection") and ingest every
    line + learn every detected per-country seller entity.

    Gated on the `transport` module entitlement FIRST, before parsing —
    identical discipline to every other transport service (`ingest_
    transaction`, `learn_registration`, `run_close`)."""
    if not await modules.is_enabled(db, org_id, "transport"):
        m = modules.MODULES_BY_KEY["transport"]
        raise PermissionError(f"The {m.name} module is not activated.", code="module_not_enabled")

    _validate_period(period)

    try:
        parsed = await fuel_card_parser.run_async(filename, content)
    except ValueError as exc:
        raise ValidationError(str(exc), code="unrecognized_fuel_card_statement") from exc

    # The capture review gate (R25's first regime) — see the module
    # docstring. Runs on the PARSED batch before any FX work; a blocked
    # batch writes zero rows. The refusal message lists EVERY error (and
    # the failed tie-out) so the operator sees all failures at once.
    review = capture_review.review_statement(parsed, coversheet_total=coversheet_total)
    if not review.can_commit:
        problems = [
            f"line {f.line_seq}: {f.message}" if f.line_seq is not None else f.message
            for f in review.findings
            if f.severity == capture_review.ERROR
        ]
        if review.tie is not None and not review.tie.ok:
            problems.append(
                f"batch tie-out failed: lines total {review.tie.computed_total} vs "
                f"coversheet {review.tie.coversheet_total} "
                f"(diff {review.tie.diff} > {capture_review.TIE_TOLERANCE})"
            )
        raise StatementRefused(
            "Capture review blocked registration: " + "; ".join(problems),
            findings=review.findings,
            tie=review.tie,
        )
    review_warnings = [
        f"capture review: line {f.line_seq}: {f.message}"
        if f.line_seq is not None
        else f"capture review: {f.message}"
        for f in review.findings
        if f.severity == capture_review.WARN
    ]

    # Deterministic advisory post-capture checks (WO-71/G3.4 — R26). Run on
    # the parsed batch BEFORE any write (pre-write placement keeps the
    # duplicate scan's self-rows question moot; the current-group exclusion
    # makes ordering irrelevant for correctness). Findings are ADVISORY —
    # rendered as warnings, an `error`-severity finding still never blocks
    # (§4.19; they deliberately do NOT feed `capture_review`'s lattice).
    check_findings = await capture_checks.run_checks(
        db,
        org_id,
        entity_id=entity_id,
        supplier=parsed.network,
        period=period,
        lines=parsed.lines,
        vat_ids=tuple(detected.vat_number for detected in parsed.entities),
    )
    check_warnings = [f"post-capture check ({f.severity}): {f.message}" for f in check_findings]

    # Phase 1 — resolve every line's EUR figures FIRST, no DB writes yet (see
    # module docstring "two-phase write"). Any unconvertible line aborts the
    # whole statement before a single row is added.
    resolved_lines = [await _resolve_line(db, line) for line in parsed.lines]

    # Phase 2 — only now write. `ingest_transaction` is itself insert-or-
    # no-op on the natural key (WO-50), so re-ingesting the identical file is
    # safe to retry.
    transactions: list[FuelTransaction] = []
    for r in resolved_lines:
        line = r.line
        txn = await fuel_ingest.ingest_transaction(
            db,
            org_id,
            entity_id=entity_id,
            supplier=parsed.network,
            period=period,
            line_seq=line.line_seq,
            country=line.country,
            vehicle_ref=line.vehicle_ref,
            txn_date=line.txn_date,
            txn_time=line.txn_time,
            station=line.station,
            product=line.product,
            qty=line.qty,
            currency=line.currency,
            net_local=line.net_local,
            vat_local=line.vat_local,
            gross_local=line.gross_local,
            net_eur=r.net_eur,
            vat_eur=r.vat_eur,
            invoice_ref=line.invoice_ref,
            provenance_note=line.provenance_note,
            fx_rate=r.fx_rate,
            fx_ecb_rate=r.fx_ecb_rate,
            fx_ecb_date=r.fx_ecb_date,
            fx_source=r.fx_source,
        )
        transactions.append(txn)

    entities: list[SupplierVatRegistration] = []
    for detected in parsed.entities:
        row = await supplier_entity.learn_registration(
            db,
            org_id,
            supplier=parsed.network,
            country=detected.country,
            vat_number=detected.vat_number,
            entity_name=detected.entity_name,
        )
        entities.append(row)

    # The anti-drift extraction baseline (WO-70) — record on first sight
    # of these bytes, compare (ADVISORY — never a block) on a re-seen
    # digest. See the module docstring; ordering is stable: parser
    # warnings, then review warnings, then drift warnings.
    sha = extraction_baseline.digest(content)
    baselines = await extraction_baseline.get_baselines(db, org_id, sha)
    drift_warnings: list[str] = []
    if baselines:
        drift_warnings = [
            f"extraction drift: {f.message}"
            for f in extraction_baseline.compare(baselines, extraction_baseline.aggregate(parsed))
        ]
    else:
        await extraction_baseline.record(
            db, org_id, parsed, statement_sha256=sha, filename=filename
        )

    return StatementIngestResult(
        network=parsed.network,
        period=period,
        lines=transactions,
        entities=entities,
        warnings=list(parsed.warnings) + review_warnings + check_warnings + drift_warnings,
        review_findings=review.findings,
        queue_warnings=list(parsed.warnings) + check_warnings + drift_warnings,
    )
