"""The load / large-dataset harness (WO-R, audit item R15).

WHAT R15 ACTUALLY ASKS FOR, AND WHAT THIS ANSWERS
---------------------------------------------------
`docs/RELEASE-READINESS.md` §3.5, verbatim: *"Performance is untested beyond
current fixture scale. `expected_rebate` loads a tenant's whole transaction
history into memory to learn medians — fine now, unmeasured at scale."* So the
requirement is two things, and the second is the one with teeth:

1. **latency** on the read paths a real workspace hits every day, and
2. **large-dataset behaviour** — what happens to a query that is O(history)
   when the history stops being a fixture.

This harness answers both by driving the REAL ASGI app over the REAL router
stack (auth, the tenant guard, RLS-shaped scoping, the service layer) against a
dataset it scales itself. Nothing is mocked: a number this prints is a number
the product produced.

WHY NO k6 AND NO LOCUST (a deliberate deviation from the plan)
----------------------------------------------------------------
`docs/plan/DEVELOPMENT-PLAN-2026-08.md` sized WO-R as *"k6 or locust"*. Both
were rejected after looking at what they would cost here:

- **k6** is a Go binary with no presence in this toolchain; CI would have to
  fetch and pin it, and it cannot drive an ASGI app in-process, so every run
  would need a live server plus a live database — a second, weaker environment
  to keep true.
- **locust** installs cleanly but drags `gevent`, `flask` and `werkzeug` into a
  backend that has added no dependency without cause all arc, and its worker
  model conflicts with an async SQLAlchemy session.

`httpx` + `asyncio` are already in `requirements.txt` and already drive every
API test in this suite. So the harness is dependency-free, runs anywhere the
tests run, and measures the same stack the tests measure. The deviation is
recorded in the plan rather than made quietly.

WHAT IT MEASURES, AND WHAT A NUMBER HERE MEANS
------------------------------------------------
Per scenario: p50, p95 and max wall-clock over N repetitions of one real HTTP
call, plus the dataset size that produced them. p95 is the budgeted figure
because a mean hides exactly the tail a user notices.

These are **relative** numbers, honest about it: a CI runner and a laptop are
different machines, so an absolute millisecond figure is only meaningful
against a baseline recorded on the same class of machine. What the budgets
catch is a REGRESSION — an endpoint that was linear becoming quadratic, or a
new N+1 — which is machine-independent in shape even when it is not in
magnitude. `docs/perf/` holds the recorded baseline and the machine it came
from.

POSTGRES ONLY, AND THAT IS THE POINT
--------------------------------------
`DATABASE_URL` must name a MIGRATED Postgres database. The SQLite harness the
unit suite uses is a different engine with a different planner: a number
measured there would describe a database this product never runs on, and the
one finding R15 actually cares about — a whole-history scan — is exactly the
shape whose cost differs most between the two. The harness refuses to run
against SQLite rather than print a comfortable, meaningless figure.

USAGE (from backend/, or `make perf` / `make perf-shape` from the repo root)
    DATABASE_URL=postgresql+asyncpg://user:pw@host:port/db \\
        python scripts/perf_harness.py                 # default scale
    ... --scale 5000                                   # large-dataset run
    ... --shape                                        # THE GATE: 4× the data,
                                                       #   cap the slowdown
    ... --json out.json                                # machine-readable

The recorded baseline, the machine it came from, and what the numbers mean live
in `docs/perf/`. The gate that consumes this is `tests/test_perf_shape.py`.
"""

# ruff: noqa: T201 — a CLI tool talks through stdout
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

#: The p95 ceiling per scenario, in milliseconds, at DEFAULT_SCALE on the
#: baseline machine. Deliberately generous — a millisecond figure is only
#: comparable against the same class of machine, so this is a SMOKE check
#: ("nothing became pathological"), not the real gate. The real gate is
#: GROWTH_CEILING below. Raise one only WITH a reason, in the same commit.
BUDGETS_MS: dict[str, float] = {
    "dashboard": 1500.0,
    "invoice_list": 1200.0,
    "ap_aging": 1500.0,
    "cash_position": 1500.0,
    "explore_by_vendor": 2000.0,
    "transport_reliability": 2500.0,
}

#: The REAL gate, and the one R15 actually asked for. Measure the same endpoint
#: at scale S and at scale S·SHAPE_FACTOR, and cap how much slower the big one
#: may be. This is MACHINE-INDEPENDENT in a way a millisecond budget is not: a
#: slow runner makes both numbers bigger and leaves the RATIO alone. A ceiling
#: of 8 against a 4× data increase says "grow linearly, with room for constant
#: overhead and noise — but a quadratic endpoint (16×) fails here."
#:
#: The recorded figures behind each ceiling are in `docs/perf/`. Where a
#: scenario already grows faster than its data, the ceiling says so out loud
#: rather than being quietly widened.
SHAPE_FACTOR = 4
GROWTH_CEILING: dict[str, float] = {
    # Windowed and indexed: barely notices the data.
    "dashboard": 4.0,
    # Paginated — page 1 of 50 costs the same whatever the table holds, modulo
    # the COUNT.
    "invoice_list": 5.0,
    # RE-BASED 2026-09-05 (PERF-004/005). Until then the harness seeded no
    # payable workflow_state, so this scenario measured an EMPTY set and its
    # 4.0 ceiling described nothing. With real rows the unbounded worklist route
    # returned every open payable and grew 6.8–8.8× across 4× of data — the
    # p95 tail of hydrating and serialising thousands of rows. The route now
    # lists the soonest-due WORKLIST_LIMIT rows with the total beside them and
    # takes its summary from SQL over all rows; measured 1.23× at 1,200 → 4,800
    # afterwards. The ceiling is therefore back where it was, now held by a
    # measurement rather than by an empty set.
    "ap_aging": 4.0,
    # Both dashboard scalars aggregate in SQL since 2026-09-05 (PERF-002/003):
    # measured 1.5× across 4× of data with real payables and receivables
    # seeded — these two ceilings are now held by measurement, not by an
    # empty set (they read 4.5× and 5.5× before the aggregation moved).
    "cash_position": 4.0,
    # A whole-table group-by with a sort — the fastest-growing of the analytics
    # reads. Measured 2.5–2.8× at 2k→8k and 6.3× at 5k→20k: superlinear, not
    # quadratic. The ceiling sits above the worst measurement, not at it.
    "explore_by_vendor": 9.0,
    # THE §3.5 SCENARIO: `expected_rebate` walking a supplier's whole history.
    # Across 50× of data (400 → 20,000 rows) the p95 grew 17× — SUBLINEAR, so
    # the fear the audit recorded is not realised. Per-4×-step it measured
    # between 2.6× and 5.9×, and CI read 7.47× then 12.24× on ONE commit
    # (#544/#545, 2026-09-06). That spread was attributed to Postgres plan
    # choice; it was not. PERF-016 profiled it: the endpoint hydrates every
    # fuel row in the window, and the allocation fired a gen-2 collection over
    # the whole imported app (~140 ms) in about one call in three at the large
    # scale — a tail proportional to the process heap, not to the data. With
    # the startup heap frozen (as production now runs) the same measurement
    # reads 2.87× at p95 and 3.0× at p50. The ceiling stays at 8: still
    # comfortably below the 16× a genuinely quadratic walk would produce, and
    # now held by a measurement of the endpoint rather than of the collector.
    "transport_reliability": 8.0,
}

DEFAULT_SCALE = 400
#: Enough repetitions that the p95 is not simply the max. Nearest-rank p95 of N
#: samples is the round(0.95·N)-th slowest, so at N=4 the "p95" IS the max and
#: reports a cold-start artifact as a tail; at N=12 it is the second-slowest,
#: which is a tail.
DEFAULT_REPS = 12
#: Discarded before measuring. The FIRST call to an endpoint pays for lazy
#: imports, the connection pool filling and Postgres planning a statement it has
#: never seen — real costs, but paid once per process, not once per user.
DEFAULT_WARMUP = 2

# --- Concurrency measurement (audit 2026-09-05: "concurrency and write-path
# performance unmeasured"). N requests in flight against ONE event loop and ONE
# connection pool — the shape of a single uvicorn worker under load. The verdict
# is a RATIO again: p95 with N in flight over p95 serial. A ratio above N means
# the requests interfered with each other beyond merely queueing (lock waits,
# pool starvation) — concurrency made things worse than a queue would have.
DEFAULT_CONCURRENCY = 8
DEFAULT_ROUNDS = 4
# The write path the product exercises most: one supplier invoice created with
# one line — the same POST the batch-upload confirm and the API both use.
WRITE_SCENARIOS: list[str] = ["invoice_create_same_org", "invoice_create_across_orgs"]


@dataclass
class ScenarioResult:
    name: str
    scale: int
    reps: int
    p50_ms: float
    p95_ms: float
    max_ms: float
    status: int
    budget_ms: float | None
    within_budget: bool | None


@dataclass
class ConcurrencyResult:
    name: str
    scale: int
    concurrency: int
    requests: int
    serial_p95_ms: float
    concurrent_p50_ms: float
    concurrent_p95_ms: float
    concurrent_max_ms: float
    throughput_rps: float
    errors: int
    # concurrent p95 / serial p95; the ceiling is the concurrency itself.
    degradation: float
    within_ceiling: bool


def _pct(values: list[float], pct: float) -> float:
    """The pct-th percentile, nearest-rank — no interpolation, so a reported
    figure is always a measurement that actually happened."""
    if not values:
        return 0.0
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round(pct / 100 * len(ordered))) - 1))
    return ordered[k]


async def _seed_scale(db, org_id: str, entity_id: str, scale: int) -> None:
    """A dataset big enough to change an algorithm's mind.

    Deliberately SYNTHETIC and deliberately shaped: `scale` invoices spread
    over two years (so date-ranged reads cannot trivially scan nothing) and
    `scale` fuel transactions for ONE supplier (so `expected_rebate`'s
    whole-history median walk — the specific §3.5 concern — actually has a
    history to walk).
    """
    from app.models.approval import KIND_APPROVER, STEP_PENDING, ApprovalStep
    from app.models.base import new_uuid
    from app.models.invoice import Invoice, InvoiceStatus, LineItem, WorkflowState
    from app.models.issued_invoice import IssuedInvoice
    from app.models.payment import Payment
    from app.models.transport.fuel_transaction import FuelTransaction
    from app.models.vendor import Vendor

    # PERF-004 (audit 2026-09-05). Until this change the seed set only the
    # legacy `status`, so every row took the column default workflow_state =
    # draft — which is NOT a payable state. `dashboard`, `ap_aging` and
    # `cash_position` filter on the payable states, so their dominant code path
    # processed ZERO rows at 400 and ZERO rows at 20,000, and the recorded
    # "sub-linear" ratios for those three scenarios measured empty sets. The
    # spread below puts most rows into the states those reads actually walk.
    # Likewise no IssuedInvoice existed, so `issued_reports.receivables` (also
    # on the dashboard) reduced nothing. Both blind spots are closed here.
    _WORKFLOW = (
        WorkflowState.approved,
        WorkflowState.scheduled_for_payment,
        WorkflowState.partially_paid,
        WorkflowState.paid,
        WorkflowState.submitted,
    )

    vendors = [Vendor(org_id=org_id, name=f"Perf Vendor {i:03d}") for i in range(10)]
    db.add_all(vendors)
    await db.flush()

    today = date.today()
    steps: list[ApprovalStep] = []
    # The id is assigned HERE rather than left to the column default, which
    # SQLAlchemy only applies at INSERT time. Naming it up front lets a line
    # item point at its invoice without a flush per row — the difference
    # between seeding 5,000 invoices in seconds and in minutes.
    for i in range(scale):
        v = vendors[i % len(vendors)]
        age = i % 730
        workflow = WorkflowState.paid if i % 3 == 0 else _WORKFLOW[i % 5]
        submitted = workflow in (WorkflowState.submitted, WorkflowState.partially_approved)
        inv = Invoice(
            id=new_uuid(),
            org_id=org_id,
            vendor_id=v.id,
            invoice_number=f"PERF-{i:06d}",
            issue_date=today - timedelta(days=age),
            due_date=today - timedelta(days=age - 30),
            currency="EUR",
            subtotal=Decimal("100.00"),
            tax_amount=Decimal("21.00"),
            total=Decimal("121.00"),
            # A spread across the aging states, so ap-aging and cash-position
            # both have something to bucket rather than one degenerate column.
            status=(InvoiceStatus.paid if i % 3 == 0 else InvoiceStatus.pending),
            amount_paid=(Decimal("121.00") if i % 3 == 0 else Decimal("0.00")),
            paid_date=(today - timedelta(days=max(age - 10, 0)) if i % 3 == 0 else None),
            workflow_state=workflow,
            # Submitted at the issue date by nobody in particular: the AP inbox
            # excludes what the reader submitted (segregation of duties), and
            # the measuring user must SEE these rows, not be excluded from them.
            submitted_by=None,
            submitted_at=(
                datetime.combine(today - timedelta(days=age), clock_time(9, 0), tzinfo=UTC)
                if submitted
                else None
            ),
        )
        db.add(inv)
        if submitted:
            # Reference R5 (P-4): until this step existed the dashboard's AP
            # inbox (`approval_policy.waiting_for`, PERF-DUCK-001) reduced an
            # EMPTY set at every scale, so its pushdown had no `--shape`
            # datapoint of its own. One generic pending step per submitted
            # invoice — exactly what `build_chain` creates when no policy
            # matches — is what a workspace without approval policies looks
            # like after its members press Submit. Collected and added AFTER
            # the invoices are flushed: no relationship links the two mappers,
            # so the unit of work would not order the inserts for the
            # composite FK (the suite runs with foreign keys enforced, QA-011).
            steps.append(
                ApprovalStep(
                    org_id=org_id,
                    invoice_id=inv.id,
                    policy_id=None,
                    seq=0,
                    kind=KIND_APPROVER,
                    approver_id=None,
                    status=STEP_PENDING,
                )
            )
        db.add(
            # No org_id: line_items is reachable only through its org-scoped
            # invoice, by design (see the model's note).
            LineItem(
                invoice_id=inv.id,
                description="Perf line",
                quantity=Decimal("1"),
                unit_price=Decimal("100.00"),
                amount=Decimal("100.00"),
                tax_rate=Decimal("21.00"),
            )
        )
        if i % 500 == 0:
            await db.flush()
    await db.flush()
    db.add_all(steps)
    await db.flush()

    # Receivables: `scale` issued invoices over the same two years, a third of
    # them paid through the payments ledger, so the AR reads on the dashboard
    # have a history to reduce.
    for i in range(scale):
        age = i % 730
        issued = IssuedInvoice(
            id=new_uuid(),
            org_id=org_id,
            number=f"PERF-AR-{i:06d}",
            issue_date=today - timedelta(days=age),
            due_date=today - timedelta(days=age - 30),
            currency="EUR",
            buyer_name=f"Perf Buyer {i % 25:02d}",
            seller_json="{}",
            subtotal=Decimal("100.00"),
            tax_total=Decimal("21.00"),
            total=Decimal("121.00"),
            amount_paid=(Decimal("121.00") if i % 3 == 0 else Decimal("0.00")),
        )
        db.add(issued)
        if i % 3 == 0:
            db.add(
                Payment(
                    org_id=org_id,
                    issued_invoice_id=issued.id,
                    amount=Decimal("121.00"),
                    paid_on=today - timedelta(days=max(age - 10, 0)),
                )
            )
        if i % 500 == 0:
            await db.flush()
    await db.flush()

    for i in range(scale):
        d = today - timedelta(days=i % 730)
        db.add(
            FuelTransaction(
                org_id=org_id,
                entity_id=entity_id,
                supplier="PERFCARD",
                period=f"{d.year:04d}-{d.month:02d}",
                line_seq=i,
                country="LV",
                vehicle_ref=f"Perf-{i % 40:03d}",
                txn_date=d,
                station="Perf Station",
                product="DIESEL",
                product_group="Diesel",
                qty=Decimal("500.000"),
                currency="EUR",
                net_local=Decimal("700.00"),
                vat_local=Decimal("147.00"),
                gross_local=Decimal("847.00"),
                net_eur=Decimal("700.00"),
                vat_eur=Decimal("147.00"),
                net_eur_eff=Decimal("700.00"),
                fx_source="eur",
            )
        )
        if i % 200 == 0:
            await db.flush()
    await db.commit()


SCENARIOS: list[tuple[str, str]] = [
    ("dashboard", "/api/v1/dashboard"),
    ("invoice_list", "/api/v1/invoices?page=1&page_size=50"),
    ("ap_aging", "/api/v1/analytics/ap-aging"),
    ("cash_position", "/api/v1/analytics/cash-position"),
    ("explore_by_vendor", "/api/v1/analytics/explore?measure=net&dim=vendor&currency=EUR"),
    ("transport_reliability", "/api/v1/transport/reliability"),
]


def require_postgres() -> None:
    """Refuse to measure on SQLite. See the module docstring."""
    from app.core.config import settings

    if "postgresql" not in settings.database_url:
        raise SystemExit(
            "perf_harness needs a MIGRATED Postgres DATABASE_URL — measuring on "
            "SQLite would describe an engine this product never runs on.\n"
            f"got: {settings.database_url.split('://')[0]}://…"
        )


async def _prepare_workspace(client, scale: int, *, label: str = "Perf") -> tuple[str, str]:
    """Register a fresh workspace on `client`, enable transport, seed `scale`
    rows. Returns (org_id, entity_id). Shared by every measurement mode."""
    from app.core.database import SessionLocal
    from app.core.tenant import reset_current_org, set_current_org
    from app.models.issuer import IssuerProfile
    from app.services import modules as modules_svc

    suffix = uuid4().hex[:10]
    reg = await client.post(
        "/api/v1/auth/register",
        json={
            "organization_name": f"{label} Workspace {suffix}",
            "name": f"{label} Owner",
            "email": f"perf-{suffix}@invoiceiq.app",
            "password": "supersecret",
        },
    )
    assert reg.status_code == 201, reg.text
    client.headers["Authorization"] = f"Bearer {reg.json()['token']['access_token']}"
    org_id = reg.json()["organization"]["id"]
    token = set_current_org(org_id)
    try:
        async with SessionLocal() as db:
            await modules_svc.set_enabled(db, org_id, "transport", True)
            # PERF-018 (2026-09-09): `issuing` is default-OFF, so every earlier
            # run measured a dashboard with its receivables section skipped —
            # and skipped is exactly the shape that hid the duplicate read
            # (the composed dashboard called the canonical receivables report
            # once for its own section and once more inside the cash-position
            # roll-up). The seed fills `issued_invoices` either way, so the
            # module was the only thing between the harness and the shape a
            # workspace with AR actually runs. Same blind spot as PERF-004.
            await modules_svc.set_enabled(db, org_id, "issuing", True)
            # The workspace registers on the trial plan, whose monthly invoice
            # cap is 10: the first CI datapoint measured 402s, not latency.
            # A cap is a business answer; the measurement DB is throwaway, so
            # the plan the perf workspace sits on is uncapped here.
            from app.services import access as access_svc

            await access_svc.set_limits(db, reg.json()["organization"]["plan"], 0, 0)
            entity = IssuerProfile(
                org_id=org_id, name=f"{label} Entity", legal_name=f"{label} Entity OU"
            )
            db.add(entity)
            await db.commit()
            await db.refresh(entity)
            if scale:
                print(f"seeding {scale} invoices + {scale} fuel transactions…", flush=True)
                t0 = time.perf_counter()
                await _seed_scale(db, org_id, entity.id, scale)
                await _analyze_after_seed(db)
                print(f"seeded in {time.perf_counter() - t0:.1f}s", flush=True)
            entity_id = entity.id
    finally:
        reset_current_org(token)
    return org_id, entity_id


#: The tables the seed fills. Analysed right after seeding (below) so the
#: planner sees the statistics production's autovacuum would have produced.
_SEEDED_TABLES = (
    "vendors",
    "invoices",
    "line_items",
    "approval_steps",
    "issued_invoices",
    "payments",
    "fuel_transactions",
)


async def _analyze_after_seed(db) -> None:
    """PERF-020 (reference R6, 2026-09-08): a bulk seed into a table whose
    statistics were gathered when it was EMPTY hands the planner a row
    estimate of 1 for the new tenant. On the AP-inbox read that turned the
    correlated NOT EXISTS into a nested-loop anti join over every pending step
    (1,067 × 1,067 rows, 1.1 M buffer hits, 631 ms) — an 8.08× growth reading
    at 2,000 → 8,000 that vanished with `ANALYZE` (84 ms per request; the
    statement fell below 2 ms). Production is never in that state for long:
    autovacuum analyses after 50 rows + 10 % of the table change, and a tenant
    the statistics have not seen is estimated from the distinct-count of the
    ones they have. Measuring on unanalysed tables therefore measures a plan
    the product does not run in steady state, so the harness analyses its
    seed the way autovacuum would. Postgres only; the SQLite suite exercises
    the seed's data, not its plans."""
    from sqlalchemy import text

    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        return
    await db.execute(text("ANALYZE " + ", ".join(_SEEDED_TABLES)))
    await db.commit()


_startup_heap_frozen = False


def _freeze_like_production() -> None:
    """What `app.main.lifespan` does last, done here because httpx's
    ASGITransport runs no lifespan (PERF-016). Without it the harness measures
    a heap production never serves from: a gen-2 collection over the imported
    app costs ~140 ms, fires about every third request that hydrates a few
    thousand rows, and read as an 11.8× growth ratio on an endpoint whose own
    p95 grows 2.9×. See `app/core/gc_tuning.py` for the figures.

    Once per process, like a worker: `shape` calls `run` twice, and a second
    freeze would park the first run's seed garbage for good. Track our own
    initialization: an embedding runtime may have frozen a small heap before
    the app was imported, which says nothing about this startup work."""
    global _startup_heap_frozen

    from app.core.gc_tuning import freeze_startup_heap

    if not _startup_heap_frozen:
        freeze_startup_heap()
        _startup_heap_frozen = True


def _collect_seed_garbage() -> None:
    """Seeding a workspace leaves ~130k dead ORM objects behind (the instance ↔
    state cycles SQLAlchemy keeps), and the first full collection inside the
    measured window would pay ~30 ms to reclaim them — a tail the harness
    made, not the endpoint. Reclaim it before the stopwatch starts."""
    import gc

    gc.collect()


async def run(scale: int, reps: int, warmup: int) -> list[ScenarioResult]:
    from httpx import ASGITransport, AsyncClient

    require_postgres()

    from app.main import app

    _freeze_like_production()

    # Every run gets its OWN workspace. Two reasons, and the second matters
    # more: a re-run must not fail on a duplicate email, and measuring inside a
    # database that already holds other tenants' rows is the honest case — the
    # tenant guard and the RLS predicates are part of what is being timed, and
    # they cost nothing to evaluate against an empty neighbourhood.
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://perf", timeout=120.0
    ) as client:
        await _prepare_workspace(client, scale)
        _collect_seed_garbage()
        results: list[ScenarioResult] = []
        for name, path in SCENARIOS:
            timings: list[float] = []
            status = 0
            for _ in range(warmup):
                await client.get(path)
            for _ in range(reps):
                t = time.perf_counter()
                r = await client.get(path)
                timings.append((time.perf_counter() - t) * 1000)
                status = r.status_code
            # A fast 500 is not a fast endpoint. Refuse to report a timing for a
            # call that did not do the work.
            if status != 200:
                raise SystemExit(f"scenario {name!r} returned HTTP {status} for {path}")
            budget = BUDGETS_MS.get(name)
            p95 = _pct(timings, 95)
            results.append(
                ScenarioResult(
                    name=name,
                    scale=scale,
                    reps=reps,
                    p50_ms=round(statistics.median(timings), 1),
                    p95_ms=round(p95, 1),
                    max_ms=round(max(timings), 1),
                    status=status,
                    budget_ms=budget,
                    within_budget=None if budget is None else p95 <= budget,
                )
            )
        return results


def _invoice_body(number: str) -> dict:
    return {
        "vendor_name": "Perf Fuels OU",
        "invoice_number": number,
        "issue_date": "2026-05-01",
        "due_date": "2026-06-01",
        "currency": "EUR",
        "line_items": [
            {"description": "Diesel", "quantity": "100", "unit_price": "1.50", "tax_rate": "22"}
        ],
    }


async def _timed(client, method: str, path: str, **kw) -> tuple[float, int]:
    t = time.perf_counter()
    r = await client.request(method, path, **kw)
    return (time.perf_counter() - t) * 1000, r.status_code


async def concurrency(scale: int, n: int, rounds: int, warmup: int) -> list[ConcurrencyResult]:
    """Every read scenario, then the two write scenarios, each measured serial
    and with `n` requests in flight. One process, one event loop, one pool —
    what a single worker sees. Errors are counted, never hidden: a 5xx under
    load is the finding this mode exists to surface."""
    from httpx import ASGITransport, AsyncClient

    require_postgres()
    from app.main import app

    _freeze_like_production()

    out: list[ConcurrencyResult] = []
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://perf",
        timeout=300.0,
    ) as client:
        await _prepare_workspace(client, scale)
        _collect_seed_garbage()

        async def measure(name: str, make: list[tuple], *, serial_from: list[tuple]) -> None:
            # serial baseline
            for _ in range(warmup):
                await client.request(*serial_from[0][:2], **serial_from[0][2])
            serial: list[float] = []
            for method, path, kw in serial_from:
                ms, _status = await _timed(client, method, path, **kw)
                serial.append(ms)
            # concurrent rounds
            timings: list[float] = []
            errors = 0
            t0 = time.perf_counter()
            for batch in make:
                results = await asyncio.gather(
                    *(_timed(c, method, path, **kw) for c, method, path, kw in batch)
                )
                for ms, status in results:
                    timings.append(ms)
                    if status >= 400:
                        errors += 1
            wall = max(time.perf_counter() - t0, 1e-6)
            serial_p95 = max(_pct(serial, 95), 1.0)
            conc_p95 = _pct(timings, 95)
            degradation = conc_p95 / serial_p95
            out.append(
                ConcurrencyResult(
                    name=name,
                    scale=scale,
                    concurrency=n,
                    requests=len(timings),
                    serial_p95_ms=round(serial_p95, 1),
                    concurrent_p50_ms=round(statistics.median(timings), 1),
                    concurrent_p95_ms=round(conc_p95, 1),
                    concurrent_max_ms=round(max(timings), 1),
                    throughput_rps=round(len(timings) / wall, 1),
                    errors=errors,
                    degradation=round(degradation, 2),
                    within_ceiling=degradation <= n and errors == 0,
                )
            )

        for name, path in SCENARIOS:
            serial_from = [("GET", path, {}) for _ in range(n)]
            batches = [[(client, "GET", path, {}) for _ in range(n)] for _ in range(rounds)]
            await measure(name, batches, serial_from=serial_from)

        # Writes in ONE workspace: the contention case (same tenant, same
        # sequences, same RLS scope, same vendor row resolved by name).
        tag = uuid4().hex[:6]
        serial_from = [
            ("POST", "/api/v1/invoices", {"json": _invoice_body(f"S-{tag}-{i}")}) for i in range(n)
        ]
        batches = [
            [
                (client, "POST", "/api/v1/invoices", {"json": _invoice_body(f"C-{tag}-{r}-{i}")})
                for i in range(n)
            ]
            for r in range(rounds)
        ]
        await measure("invoice_create_same_org", batches, serial_from=serial_from)

        # Writes ACROSS workspaces: n tenants, one request each per round —
        # the multi-tenant steady state, where nothing should contend.
        clients = []
        for _i in range(n):
            c = AsyncClient(
                transport=ASGITransport(app=app, raise_app_exceptions=False),
                base_url="http://perf",
                timeout=300.0,
            )
            await _prepare_workspace(c, 0, label="Tenant")
            clients.append(c)
        try:
            serial_from = [
                ("POST", "/api/v1/invoices", {"json": _invoice_body(f"X-{tag}-{i}")})
                for i in range(n)
            ]
            batches = [
                [
                    (c, "POST", "/api/v1/invoices", {"json": _invoice_body(f"T-{tag}-{r}-{i}")})
                    for i, c in enumerate(clients)
                ]
                for r in range(rounds)
            ]
            await measure("invoice_create_across_orgs", batches, serial_from=serial_from)
        finally:
            for c in clients:
                await c.aclose()
    return out


def _print_concurrency(results: list[ConcurrencyResult], n: int) -> int:
    print(
        f"\n{'scenario':<28}{'serial p95':>11}{'conc p50':>10}{'conc p95':>10}{'max':>9}"
        f"{'req/s':>8}{'errors':>8}{'x':>7}  verdict"
    )
    print("-" * 100)
    worse = 0
    for r in results:
        verdict = "OK" if r.within_ceiling else ("ERRORS" if r.errors else "CONTENDED")
        worse += 0 if r.within_ceiling else 1
        print(
            f"{r.name:<28}{r.serial_p95_ms:>11.1f}{r.concurrent_p50_ms:>10.1f}"
            f"{r.concurrent_p95_ms:>10.1f}{r.concurrent_max_ms:>9.1f}{r.throughput_rps:>8.1f}"
            f"{r.errors:>8}{r.degradation:>7.2f}  {verdict}"
        )
    print(
        f"\nx = concurrent p95 / serial p95 at {n} in flight; a scenario is CONTENDED "
        f"above {n}.0 (worse than a queue) and ERRORS on any 4xx/5xx."
    )
    return worse


@dataclass
class GrowthResult:
    name: str
    small_scale: int
    large_scale: int
    small_p95_ms: float
    large_p95_ms: float
    ratio: float
    ceiling: float | None
    within_ceiling: bool | None
    # The medians ride beside the gated tail, INFORMATIONAL. When the p95 ratio
    # and the p50 ratio disagree by a lot, the tail is not the algorithm's —
    # PERF-016 read 11.8× against 3.0× and the difference was the garbage
    # collector. A quadratic endpoint moves both.
    small_p50_ms: float = 0.0
    large_p50_ms: float = 0.0
    p50_ratio: float = 0.0


async def shape(scale: int, factor: int, reps: int, warmup: int) -> list[GrowthResult]:
    """Measure the same endpoints at two dataset sizes and report the ratio.

    Each `run` seeds a NEW workspace, so the large measurement happens in a
    database that also holds the small run's rows. That is deliberate and it
    does not distort the ratio: every query in every scenario is org-scoped, so
    the neighbouring tenant costs an index predicate, not a scan — and if it
    ever stopped costing only that, this harness is exactly where it should
    show up.
    """
    small = {r.name: r for r in await run(scale, reps, warmup)}
    large = {r.name: r for r in await run(scale * factor, reps, warmup)}

    out: list[GrowthResult] = []
    for name, _path in SCENARIOS:
        s, big = small[name], large[name]
        # A sub-millisecond denominator would turn noise into a huge ratio;
        # floor it so the gate measures growth, not timer resolution.
        ratio = big.p95_ms / max(s.p95_ms, 1.0)
        ceiling = GROWTH_CEILING.get(name)
        out.append(
            GrowthResult(
                name=name,
                small_scale=s.scale,
                large_scale=big.scale,
                small_p95_ms=s.p95_ms,
                large_p95_ms=big.p95_ms,
                ratio=round(ratio, 2),
                ceiling=ceiling,
                within_ceiling=None if ceiling is None else ratio <= ceiling,
                small_p50_ms=s.p50_ms,
                large_p50_ms=big.p50_ms,
                p50_ratio=round(big.p50_ms / max(s.p50_ms, 1.0), 2),
            )
        )
    return out


def _print_shape(results: list[GrowthResult], factor: int) -> int:
    print(
        f"\ngrowth in p95 for {factor}× the data "
        f"({results[0].small_scale} → {results[0].large_scale} rows per fact table)"
    )
    print(
        f"\n{'scenario':<24}{'small ms':>10}{'large ms':>10}{'ratio':>10}{'ceiling':>10}"
        f"{'p50 ratio':>11}  verdict"
    )
    print("-" * 89)
    breached = 0
    for r in results:
        verdict = "—"
        if r.within_ceiling is not None:
            verdict = "OK" if r.within_ceiling else "GREW TOO FAST"
            breached += 0 if r.within_ceiling else 1
        ceiling = "—" if r.ceiling is None else f"{r.ceiling:.1f}"
        print(
            f"{r.name:<24}{r.small_p95_ms:>10.1f}{r.large_p95_ms:>10.1f}"
            f"{r.ratio:>10.2f}{ceiling:>10}{r.p50_ratio:>11.2f}  {verdict}"
        )
    print("\nratio = p95 growth (gated); p50 ratio = median growth (informational — see PERF-016).")
    if breached:
        print(f"\n{breached} scenario(s) grew faster than their ceiling allows.")
    return breached


def _print_peak_rss() -> None:
    """PERF-017: the memory side of any collector setting — peak resident
    size of this process (Linux reports kB) and the thresholds in force, so a
    run with `GC_GEN2_THRESHOLD` set can be compared against one without."""
    import gc
    import resource

    peak_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print(
        f"\npeak RSS {peak_kb / 1024:.0f} MB; gc thresholds {gc.get_threshold()}; "
        f"frozen {gc.get_freeze_count()} objects"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scale", type=int, default=DEFAULT_SCALE)
    ap.add_argument("--reps", type=int, default=DEFAULT_REPS)
    ap.add_argument("--warmup", type=int, default=DEFAULT_WARMUP)
    ap.add_argument("--json", type=str, default=None)
    ap.add_argument(
        "--concurrency",
        type=int,
        default=None,
        metavar="N",
        help=(
            "measure every scenario serial and with N requests in flight (plus the "
            "two write scenarios); informational — prints the degradation ratio"
        ),
    )
    ap.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS)
    ap.add_argument(
        "--shape",
        action="store_true",
        help=(
            "measure at --scale and --scale × SHAPE_FACTOR and gate on the growth "
            "ratio instead of absolute milliseconds"
        ),
    )
    args = ap.parse_args()

    if args.concurrency:
        results_c = asyncio.run(concurrency(args.scale, args.concurrency, args.rounds, args.warmup))
        contended = _print_concurrency(results_c, args.concurrency)
        _print_peak_rss()
        if args.json:
            Path(args.json).write_text(json.dumps([asdict(r) for r in results_c], indent=2))
            print(f"\nwrote {args.json}")
        # Informational until a datapoint from the real host sets a ceiling
        # (docs/perf/CONCURRENCY-*.md); errors under load are the one hard fail.
        return 1 if any(r.errors for r in results_c) else (0 if not contended else 0)

    if args.shape:
        growth = asyncio.run(shape(args.scale, SHAPE_FACTOR, args.reps, args.warmup))
        breached = _print_shape(growth, SHAPE_FACTOR)
        _print_peak_rss()
        if args.json:
            Path(args.json).write_text(json.dumps([asdict(g) for g in growth], indent=2))
            print(f"\nwrote {args.json}")
        return 1 if breached else 0

    results = asyncio.run(run(args.scale, args.reps, args.warmup))

    print(f"\n{'scenario':<24}{'p50 ms':>10}{'p95 ms':>10}{'max ms':>10}{'budget':>10}  verdict")
    print("-" * 78)
    breached = 0
    for r in results:
        verdict = "—"
        if r.within_budget is not None:
            verdict = "OK" if r.within_budget else "OVER BUDGET"
            breached += 0 if r.within_budget else 1
        budget = "—" if r.budget_ms is None else f"{r.budget_ms:.0f}"
        print(
            f"{r.name:<24}{r.p50_ms:>10.1f}{r.p95_ms:>10.1f}{r.max_ms:>10.1f}{budget:>10}  {verdict}"
            + ("" if r.status == 200 else f"  (HTTP {r.status})")
        )
    if args.json:
        Path(args.json).write_text(json.dumps([asdict(r) for r in results], indent=2))
        print(f"\nwrote {args.json}")
    if breached:
        print(f"\n{breached} scenario(s) over budget.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
