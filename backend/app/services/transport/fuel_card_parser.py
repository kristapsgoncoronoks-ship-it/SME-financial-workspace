"""The fuel-card parser registry (G3.2 slice 1; `BA_fleet_fuel.md` §5.1 "seven
networks with real, learned format quirks", §3.I "extraction is
deterministic-first").

WHY THIS MIRRORS `app.services.extraction_provider`, NOT A NEW SHAPE
--------------------------------------------------------------------------
`ARCH_plan.md`'s own G3.2 description says "deterministic per-supplier
parsers behind the existing `ExtractionProvider` registry" — but that
registry's output type (`ProviderResult` -> `InvoiceCreate`) is AP-invoice
shaped (header fields, line items with a description/category/unit_price),
not fuel-transaction shaped. Reusing it literally would mean forcing a fuel
line into an invoice-line schema it does not fit (no `description`, a
different money model — `net_local`/`vat_local`/`gross_local` per line, not
a single header total). This module is a SEPARATE registry with the SAME
*pattern* `extraction_provider.py` established (deterministic-first,
registration-order-is-try-order, the first `handles()` match wins,
`register()`/`select()`/`run()`), producing the shape THIS domain's write
path (`fuel_ingest.ingest_transaction`, `supplier_entity.learn_registration`)
actually needs. This is the honest reading of "behind the existing registry"
— same architectural seam, not the same Python type.

WHAT A PARSER PRODUCES
-----------------------
A `ParsedStatement`: zero-or-more `ParsedFuelLine`s (one per fuel-card
transaction, feeding `fuel_ingest.ingest_transaction` — G1.2/WO-50) and
zero-or-more `DetectedEntity`s (the per-country SELLER legal entity read off
the document, feeding `supplier_entity.learn_registration` — G3.1/WO-61,
closing R20 for real for the first time). `warnings` surfaces anything
ambiguous or best-effort for a human to review — deliberately NOT swallowed,
per master-context §10's "never `except: pass`" discipline extended here:
this codebase has no fuel-statement review-queue table yet (that is a
FUTURE slice, mirroring how G3.3 "the capture review gate" is its own,
later, board item that DEPENDS on G3.2 — `ARCH_plan.md` line 945), so a
warning surfacing here IS the review surface until one exists.

FAIL-CLOSED NETWORK DETECTION
------------------------------
`select()` raises `ValueError` when no registered parser's `handles()`
returns True for a file — it NEVER falls back to "try the first one anyway"
or guesses which network a statement belongs to. Money/tax correctness
(master-context §4.9/§4.14) applies to fuel-transaction capture exactly as
it does to a claim gate: parsing a Q8 file as if it were a DKV statement
would silently apply the wrong column meanings (a different money model,
wrong per-line country/currency handling) — structurally indistinguishable
from inventing figures. See `EurowagParser.handles()` for the concrete
marker this order's own network requires.
"""

from __future__ import annotations

import asyncio
import threading
from abc import ABC, abstractmethod
from concurrent.futures import Future
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import date
from decimal import Decimal

from fastapi.concurrency import run_in_threadpool

from app.core.errors import AppError

# The synchronous request path admitted one parser at a time per process.
# Offloading must not multiply that CPU/memory concurrency or queue uploads.
_PARSE_SLOT = threading.Lock()


class StatementReadingBusy(AppError):
    status = 503
    code = "statement_reading_busy"

    def __init__(self, *, original_stored: bool = False):
        message = (
            "Statement reading is busy. Wait for the current reading to finish, then try again."
        )
        if original_stored:
            message = (
                "Statement reading is busy. The original file has been retained, but this attempt "
                "did not register transactions. Wait for the current reading to finish, then "
                "try again with the same file."
            )
        super().__init__(message)
        self.original_stored = original_stored

    def extra(self) -> dict:
        extra = {"retryable": True}
        if self.original_stored:
            extra["original_stored"] = True
        return extra


@dataclass(frozen=True)
class ParsedFuelLine:
    """One fuel-card transaction line, shaped for
    `app.services.transport.fuel_ingest.ingest_transaction` (G1.2). Money
    fields are UNROUNDED Decimals as read from the document — the ingestion
    service is the single place that quantizes (master-context §4.9's own
    "the service is the single point that rounds" convention, WO-50).
    `line_seq` is the 1-based position of this line within its SOURCE FILE
    for the (entity, supplier, period) group — assigned deterministically by
    the parser (never inferred by the caller), matching the natural-key
    contract `fuel_transactions` was built around.
    """

    line_seq: int
    country: str
    vehicle_ref: str
    txn_date: date
    station: str
    product: str
    qty: Decimal
    currency: str
    net_local: Decimal
    vat_local: Decimal
    gross_local: Decimal
    txn_time: str = ""
    invoice_ref: str | None = None
    provenance_note: str | None = None
    # The supplier's OWN per-line EUR figure, AS PRINTED on the statement —
    # set only by a network whose harvested money model is "trust the
    # supplier's per-line EUR" (DKV, `BA_fleet_fuel.md` §5.1: "trusts the
    # supplier's per-line EUR and pro-rates"; WO-65/G3.2 slice 4). `None`
    # (the default — every other parser) means the ingestion service
    # converts via its own EUR-identity/cached-ECB paths; a value here means
    # the ingestion service uses THIS figure verbatim (`fx_source="stated"`,
    # the platform's ADR-0010 convention) and pro-rates the VAT at the same
    # implied rate. Never set for an EUR-currency line (identity wins).
    stated_net_eur: Decimal | None = None


@dataclass(frozen=True)
class DetectedEntity:
    """The per-country SELLER legal entity read off the document (R20) —
    feeds `supplier_entity.learn_registration`. `entity_name` is the seller's
    legal name AS PRINTED, never normalized against any master-data table
    (marker-only matching happens downstream, in `supplier_entity`, R21).
    """

    country: str
    vat_number: str
    entity_name: str


@dataclass
class ParsedStatement:
    """What one fuel-card parser returns for one uploaded statement file."""

    network: str
    lines: list[ParsedFuelLine] = dc_field(default_factory=list)
    entities: list[DetectedEntity] = dc_field(default_factory=list)
    warnings: list[str] = dc_field(default_factory=list)


class FuelCardParser(ABC):
    """The interface every fuel-card network's deterministic parser
    implements — mirrors `app.services.extraction_provider.ExtractionProvider`
    (`handles()` + one parse method), adapted to this domain's output shape.
    """

    network: str = "unknown"

    @abstractmethod
    def handles(self, filename: str, content: bytes) -> bool:
        """Whether this parser recognizes the file as ITS network's format.
        Must be a STRICT, structural check (a marker string / header shape),
        never a soft heuristic — see the module docstring's "fail-closed
        network detection". Must never raise; an exception here is treated
        by `select()` as "does not handle this file", not a crash."""

    @abstractmethod
    def parse(self, filename: str, content: bytes) -> ParsedStatement:
        """Parse recognized bytes into a `ParsedStatement`. Raises
        `ValueError` for a structurally malformed statement (missing
        required column, unparseable amount/date) — the caller
        (`app.services.transport.statement_ingest.ingest_statement`) turns
        that into a `ValidationError` at the service boundary. Never invents
        a field to paper over a gap (master-context §4.19's "AI never
        invents a field" ethos, extended here to every deterministic
        parser too)."""


# Registration order = try order (deterministic-first), exactly like
# `extraction_provider._PROVIDERS`. Built-ins are constructed here (not via a
# side-effecting `register()` call at import time in another module) so the
# try order is visible in one place — the same choice `extraction_provider.py`
# makes with `_PROVIDERS: list[ExtractionProvider] = [PdfProvider(), ...]`.
def _default_parsers() -> list[FuelCardParser]:
    from app.services.transport.parsers.bp import BPParser
    from app.services.transport.parsers.dkv import DKVParser
    from app.services.transport.parsers.e100 import E100Parser
    from app.services.transport.parsers.eurowag import EurowagParser
    from app.services.transport.parsers.moeve import MoeveParser
    from app.services.transport.parsers.q8 import Q8Parser
    from app.services.transport.parsers.tfc import TFCParser

    # Registration order = try order. Each network's `handles()` marker is a
    # distinct literal first-line string, so no two can ever both match the
    # same file — append order does not affect correctness here — kept
    # Eurowag/E100-first to keep the existing behaviour visible (WO-63/G3.2
    # slice 2), Q8 appended (WO-64/G3.2 slice 3), DKV appended (WO-65/G3.2
    # slice 4), TFC appended (WO-67/G3.2 slice 5), Moeve appended
    # (WO-68/G3.2 slice 6), BP appended (WO-69/G3.2 slice 7 — the LAST
    # §5.1 network; G3.2 is closed).
    return [
        EurowagParser(),
        E100Parser(),
        Q8Parser(),
        DKVParser(),
        TFCParser(),
        MoeveParser(),
        BPParser(),
    ]


_PARSERS: list[FuelCardParser] = _default_parsers()


def register(parser: FuelCardParser, *, first: bool = False) -> None:
    """Add a parser. `first=True` gives it priority — e.g. a future
    higher-confidence detector that should be tried ahead of the built-ins."""
    if first:
        _PARSERS.insert(0, parser)
    else:
        _PARSERS.append(parser)


def parsers() -> list[FuelCardParser]:
    return list(_PARSERS)


def select(filename: str, content: bytes) -> FuelCardParser:
    """The first registered parser whose `handles()` returns True. Raises
    `ValueError` when NONE do — see the module docstring's "fail-closed
    network detection"; this function never guesses."""
    for p in _PARSERS:
        try:
            if p.handles(filename, content):
                return p
        except Exception:  # noqa: BLE001 - a parser's sniff must never break selection
            continue
    raise ValueError(
        "No registered fuel-card parser recognized this statement "
        f"({filename!r}). Supported networks: " + ", ".join(p.network for p in _PARSERS)
    )


def run(filename: str, content: bytes) -> ParsedStatement:
    """Select a parser and parse. Raises `ValueError` for an unrecognized
    network or a structurally malformed statement."""
    return select(filename, content).parse(filename, content)


async def run_async(filename: str, content: bytes) -> ParsedStatement:
    """Read immutable bytes off loop; own the one CPU slot until work ends.

    No session or domain work crosses this boundary. Busy callers are refused
    immediately. A cancelled request joins its already-owned worker before
    releasing admission, including repeated cancellation (the image reader's
    existing shield/join discipline). The synchronous registry API stays intact.
    """
    physical: Future[ParsedStatement] = Future()
    if not _PARSE_SLOT.acquire(blocking=False):
        raise StatementReadingBusy()
    # The asyncio/AnyIO wrapper can be cancelled before its callable exits.
    # This thread-safe future owns admission independently of that wrapper.
    physical.add_done_callback(lambda _: _PARSE_SLOT.release())

    def execute() -> ParsedStatement:
        # Atomic with cancel(): a cancelled queued submission must never parse
        # later or release a slot now owned by a different request.
        if not physical.set_running_or_notify_cancel():
            raise asyncio.CancelledError()
        try:
            result = run(filename, content)
        except BaseException as exc:
            physical.set_exception(exc)
            raise
        physical.set_result(result)
        return result

    try:
        coroutine = run_in_threadpool(execute)
        try:
            work = asyncio.create_task(coroutine)
        except BaseException:
            coroutine.close()
            raise
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            if physical.cancel():
                work.cancel()  # No callable started; remove the queued wrapper.
            completion = asyncio.wrap_future(physical)
            while not completion.done():
                try:
                    await asyncio.shield(completion)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not completion.cancelled():
                completion.exception()
            while not work.done():
                try:
                    await asyncio.shield(work)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not work.cancelled():
                work.exception()
            raise
    finally:
        # Submission failures release pending admission. Once running, only
        # execute() can complete physical and release it after the real parse.
        physical.cancel()
