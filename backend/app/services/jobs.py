"""A durable, DB-backed job queue with at-most-one-worker claim + retries.

Rows in the `jobs` table ARE the queue. This module is the whole contract:

  enqueue()      append a job (idempotent when an idempotency_key is given)
  claim()        atomically lease the oldest ready job to one worker
  run_once()     claim → dispatch to the registered handler → complete/fail
  reclaim_stale() return jobs whose worker died (stale lease) to the queue

Design goals: portable (SQLite + Postgres), idempotent (a re-enqueue with the
same key is a no-op; a handler that runs twice must be safe), and never-lost (a
crashed worker's job is reclaimed, a failing job retries with exponential
backoff and finally lands in the `dead` dead-letter state).

Handlers register with @handler("kind") and receive (db, payload, job). The job
runs INSIDE its tenant's scope (org context is set around dispatch) so handler
queries and audit attribution are tenant-correct.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import CursorResult, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.observability import job_id_ctx, job_kind_ctx
from app.core.tenant import reset_current_org, set_current_org
from app.models import job as jobmodel
from app.models.job import Job

log = logging.getLogger("invoiceiq.jobs")

# Retry backoff: base_seconds * 2**(attempt-1), capped. attempt is 1-based.
_BACKOFF_BASE_SECONDS = 30
_BACKOFF_CAP_SECONDS = 3600
# A job leased longer than this is presumed abandoned (worker crashed).
STALE_LEASE_SECONDS = 300
# STIR-P1-01 (Stirling-PDF, reference integration 2026-09-07): a job that is
# ALIVE but slow — a 20-page OCR at 300 dpi, a large archive export — used to
# look exactly like a crashed worker once its lease passed STALE_LEASE_SECONDS,
# so `reclaim_stale` handed it to a second worker while the first was still
# running it. The worker now renews its lease well inside the cutoff: one
# UPDATE a minute, and only for jobs that actually run longer than a minute.
LEASE_HEARTBEAT_SECONDS = 60.0
# How long `run_once` waits for an in-flight renewal after the handler returns
# before cancelling it. Without a bound a half-open database connection inside
# `_renew_lease` would park the (sequential) worker on `await heartbeat_task`.
HEARTBEAT_JOIN_SECONDS = 5.0
# A downstream `Retry-After` may lengthen the backoff, but only up to this. An
# unbounded hint (`Retry-After: 315360000`) would park a delivery for a decade
# outside every dead-letter and lag gauge — a denial of delivery by the receiver.
RETRY_AFTER_CAP_SECONDS = 86400.0

Handler = Callable[[AsyncSession, dict, Job], Awaitable[dict | None]]


@dataclass(frozen=True)
class _Registered:
    """One registered handler and the deadline it asked for (None = the default).

    The deadline lives HERE rather than in a parallel dict because several tests
    snapshot and restore `_HANDLERS` wholesale to register a temporary kind; a
    second dict would leak a budget past the restore."""

    fn: Handler
    deadline_seconds: float | None


_HANDLERS: dict[str, _Registered] = {}


class PermanentJobError(RuntimeError):
    """A handler failure that no retry can cure (a precondition the job itself
    encodes no longer holds — e.g. a billing customer that no longer resolves
    to the queued tenant). `_fail` dead-letters it on the first attempt with
    the reason, instead of burning `max_attempts` × backoff on a certainty."""


class JobDeadlineExceeded(RuntimeError):
    """The handler ran past its per-kind budget and was cancelled (BE-024).

    Deliberately NOT a `PermanentJobError`. A deadline is evidence about THIS
    attempt — a slow provider, an unusually large file, a lock held elsewhere —
    not a certainty about the job, so it retries with the ordinary backoff and
    dead-letters only after it has used its attempts, like any other failure."""


class RetryAfterError(RuntimeError):
    """A handler failure carrying the downstream's minimum retry delay.

    PAT-004 (Scrapling, reference integration 2026-09-07): a receiver that
    answers 429/503 with `Retry-After` is telling us when it will accept the
    next attempt. Retrying sooner is wasted work that burns an attempt; the
    hint may LENGTHEN the local backoff but never shorten it (see `_fail`)."""

    def __init__(self, message: str, *, retry_after_seconds: float) -> None:
        super().__init__(message)
        delay = float(retry_after_seconds)
        if not math.isfinite(delay) or delay < 0:
            raise ValueError("retry_after_seconds must be a finite, non-negative number")
        self.retry_after_seconds = delay


def handler(kind: str, *, deadline_seconds: float | None = None) -> Callable[[Handler], Handler]:
    """Register the coroutine that processes jobs of `kind`.

    `deadline_seconds` overrides `settings.job_deadline_seconds` for this kind
    (BE-024). Declare one only for work that legitimately runs longer than the
    default, and say why at the call site — a budget with no reason next to it
    is the thing that gets raised the first time it fires."""

    def deco(fn: Handler) -> Handler:
        if deadline_seconds is not None and (
            not math.isfinite(deadline_seconds) or deadline_seconds <= 0
        ):
            raise ValueError(f"deadline_seconds for '{kind}' must be finite and positive")
        _HANDLERS[kind] = _Registered(fn, deadline_seconds)
        return fn

    return deco


def registered_kinds() -> tuple[str, ...]:
    return tuple(sorted(_HANDLERS))


def deadline_for(kind: str) -> float:
    """The budget `run_once` will give this kind — its own, or the default."""
    reg = _HANDLERS.get(kind)
    if reg is not None and reg.deadline_seconds is not None:
        return reg.deadline_seconds
    return float(settings.job_deadline_seconds)


def _now(now: datetime | None = None) -> datetime:
    return now or datetime.now(UTC)


def _backoff(attempts: int) -> int:
    return min(_BACKOFF_CAP_SECONDS, _BACKOFF_BASE_SECONDS * (2 ** max(0, attempts - 1)))


async def enqueue(
    db: AsyncSession,
    kind: str,
    payload: dict | None = None,
    *,
    org_id: str,
    idempotency_key: str | None = None,
    max_attempts: int = 5,
    run_after: datetime | None = None,
    commit: bool = True,
) -> Job:
    """Append a job. With an `idempotency_key`, a matching job with that key is
    returned instead of inserting a duplicate — a LIVE one (queued/running) by
    the pre-check, a TERMINAL one (succeeded/failed/dead) by the unique index.
    Callers that need to know which happened use `enqueue_with_outcome`."""
    job, _created = await enqueue_with_outcome(
        db,
        kind,
        payload,
        org_id=org_id,
        idempotency_key=idempotency_key,
        max_attempts=max_attempts,
        run_after=run_after,
        commit=commit,
    )
    return job


async def enqueue_with_outcome(
    db: AsyncSession,
    kind: str,
    payload: dict | None = None,
    *,
    org_id: str,
    idempotency_key: str | None = None,
    max_attempts: int = 5,
    run_after: datetime | None = None,
    commit: bool = True,
) -> tuple[Job, bool]:
    """`enqueue`, plus whether a NEW row was written.

    BE-004 (audit 2026-09-05): the unique index on (org, kind, key) is
    unconditional while the pre-check looks at live rows only, so an enqueue
    that repeats the key of a SUCCEEDED or DEAD job returns that finished job.
    That is idempotency working as designed — the same key names the same unit
    of work, and a client that wants the work done again names a new key or
    calls `retry` — but the route used to answer `201 Created` for it, which
    told the client something had been scheduled when nothing had. The second
    element is False for BOTH dedupe paths so the route can say so."""
    if idempotency_key is not None:
        existing = await db.scalar(
            select(Job).where(
                Job.org_id == org_id,
                Job.kind == kind,
                Job.idempotency_key == idempotency_key,
                Job.status.in_((jobmodel.QUEUED, jobmodel.RUNNING)),
            )
        )
        if existing is not None:
            return existing, False

    job = Job(
        org_id=org_id,
        kind=kind,
        payload_json=json.dumps(payload or {}),
        idempotency_key=idempotency_key,
        status=jobmodel.QUEUED,
        attempts=0,
        max_attempts=max_attempts,
        run_after=run_after or _now(),
    )
    # SAVEPOINT (BE-003, audit 2026-09-05): a unique-key collision must unwind
    # THIS row only. This used to `db.rollback()` the whole session — and every
    # `commit=False` caller (email intake, webhook emit inside pay_run …) hands
    # over a session that already holds its real business work, so a collision
    # silently discarded that work while the route went on to commit an empty
    # transaction and answer 2xx. Same pattern as `webhooks.emit`.
    try:
        async with db.begin_nested():
            db.add(job)
            await db.flush()
    except IntegrityError:
        if idempotency_key is None:
            # Not the idempotency race — an FK or NOT NULL violation. Never
            # answer that with somebody else's job.
            raise
        # A concurrent enqueue won the unique (org, kind, key) race — return theirs.
        winner = await db.scalar(
            select(Job).where(
                Job.org_id == org_id, Job.kind == kind, Job.idempotency_key == idempotency_key
            )
        )
        if winner is None:  # pragma: no cover - the unique conflict guarantees a row
            raise
        if commit:
            await db.commit()
        return winner, False
    if commit:
        await db.commit()
        await db.refresh(job)
    return job, True


async def claim(
    db: AsyncSession,
    worker_id: str,
    *,
    kinds: tuple[str, ...] | None = None,
    exclude: tuple[str, ...] | None = None,
    now: datetime | None = None,
) -> Job | None:
    """Atomically lease the oldest ready job to `worker_id`, or return None.

    Uses an optimistic guarded UPDATE (WHERE status='queued') so two workers
    that pick the same candidate can't both win — the loser's UPDATE matches
    zero rows and it tries the next candidate. Portable across SQLite/Postgres.

    `kinds` / `exclude` scope a worker to a LANE (resource isolation): a worker
    started with `kinds=('email.extract',)` only leases OCR jobs, so a heavy
    lane can run on its own pool and never starve (or be starved by) another. A
    worker with neither filter drains every kind — the default, unchanged.
    """
    now = _now(now)
    for _ in range(10):  # bounded retries against contention
        stmt = (
            select(Job.id)
            .where(Job.status == jobmodel.QUEUED, Job.run_after <= now)
            .order_by(Job.run_after.asc(), Job.created_at.asc())
            .limit(1)
        )
        if kinds:
            stmt = stmt.where(Job.kind.in_(kinds))
        if exclude:
            stmt = stmt.where(Job.kind.not_in(exclude))
        candidate = await db.scalar(stmt)
        if candidate is None:
            return None
        result = await db.execute(
            update(Job)
            .where(Job.id == candidate, Job.status == jobmodel.QUEUED)
            .values(
                status=jobmodel.RUNNING,
                attempts=Job.attempts + 1,
                locked_at=now,
                locked_by=worker_id,
                updated_at=now,
            )
        )
        if cast(CursorResult, result).rowcount == 1:
            await db.commit()
            return await db.get(Job, candidate)
        await db.rollback()  # lost the race — try the next candidate
    return None


async def _renew_lease(job_id: str, worker_id: str, org_id: str) -> bool:
    """Renew one live lease in its own session/transaction, so the renewal
    neither waits on nor disturbs the handler's transaction on the main session.
    The predicate (id + RUNNING + locked_by == us) means a lease that was
    already reclaimed by another worker is NOT re-taken — the renewal simply
    reports False and the heartbeat stops."""
    from app.core.database import SessionLocal

    token = set_current_org(org_id)
    try:
        now = _now()
        async with SessionLocal() as heartbeat_db:
            result = await heartbeat_db.execute(
                update(Job)
                .where(
                    Job.id == job_id,
                    Job.org_id == org_id,  # explicit even where RLS already scopes (SQLite)
                    Job.status == jobmodel.RUNNING,
                    Job.locked_by == worker_id,
                )
                .values(locked_at=now, updated_at=now)
            )
            await heartbeat_db.commit()
            return cast(CursorResult, result).rowcount == 1
    finally:
        reset_current_org(token)


async def _heartbeat_loop(
    job_id: str,
    worker_id: str,
    org_id: str,
    stop: asyncio.Event,
    on_tick: Callable[[], None] | None = None,
) -> None:
    """Keep a claimed job's lease live until its handler returns (`stop` is set).
    A failed renewal is logged and retried on the next tick; a renewal that
    finds the lease no longer ours ends the loop — the reclaim already happened
    and re-taking the row would put two workers on one job. `on_tick` runs on
    every tick before the renewal — the worker's liveness heartbeat, so a probe
    sees the loop turning during a long job (app.worker_probe)."""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=LEASE_HEARTBEAT_SECONDS)
            return
        except TimeoutError:
            pass
        if on_tick is not None:
            try:
                on_tick()
            except Exception as exc:  # noqa: BLE001 — liveness bookkeeping never fails work
                log.warning("job %s liveness tick failed: %s", job_id, exc)
        try:
            if not await _renew_lease(job_id, worker_id, org_id):
                # Either the job just completed (benign race with `_complete`) or
                # the lease was reclaimed and re-taken — in both cases renewing
                # again would be wrong, so the loop ends.
                log.info(
                    "job %s renewal matched no live lease for %s; heartbeat stops",
                    job_id,
                    worker_id,
                )
                return
        except Exception as exc:  # noqa: BLE001 — the heartbeat must never fail business work
            log.warning("job %s heartbeat failed: %s", job_id, exc)


async def _complete(db: AsyncSession, job: Job, result: dict | None) -> None:
    job.status = jobmodel.SUCCEEDED
    job.result_json = json.dumps(result or {})
    job.last_error = None
    job.locked_at = None
    job.locked_by = None
    await db.commit()


async def _fail(
    db: AsyncSession,
    job: Job,
    error: str,
    *,
    now: datetime | None = None,
    retry_after_seconds: float | None = None,
    permanent: bool = False,
) -> None:
    now = _now(now)
    job.last_error = error[:2000]
    job.locked_at = None
    job.locked_by = None
    if permanent or job.attempts >= job.max_attempts:
        job.status = jobmodel.DEAD
    else:
        job.status = jobmodel.QUEUED
        delay = float(_backoff(job.attempts))
        if retry_after_seconds is not None:
            # The downstream's hint can only LENGTHEN the wait — shortening it
            # would let a receiver drive our retry cadence below the backoff
            # curve — and only up to the cap: a receiver must not be able to
            # park our delivery indefinitely (or overflow the datetime).
            delay = max(delay, min(retry_after_seconds, RETRY_AFTER_CAP_SECONDS))
        job.run_after = now + timedelta(seconds=delay)
    await db.commit()


async def _run_handler(
    registered: _Registered, db: AsyncSession, payload: dict, job: Job
) -> dict | None:
    """Await one handler under its per-kind deadline (BE-024).

    STIR-P1-01 gave a running job a lease heartbeat so that a SLOW job stops
    looking like a CRASHED one. The mirror-image cost, found by the R6 panels:
    a job that is not slow but HUNG — a provider SDK call with no timeout of
    its own, a lost `await` — now renews its lease for ever. `reclaim_stale`
    sees a healthy lease and leaves it; the R6 liveness probe sees a loop that
    is still ticking, because the heartbeat is what touches the file. One hung
    handler removed a worker from the fleet with no signal at all except
    `/health/queue`'s backlog age fifteen minutes later, which says that the
    queue is behind and not which job is sitting on it.

    HONEST LIMIT: `wait_for` cancels the awaiting coroutine, which frees the
    WORKER LOOP. Work already handed to a thread (`run_in_threadpool`, which is
    how OCR runs) keeps running in that thread until it finishes — the loop
    moves on, the thread does not stop. Native OCR has its own per-invocation
    bound for exactly that reason (STIR-P2-01); this deadline is the loop's
    guarantee, not a kill switch for every kind of work.
    """
    budget = deadline_for(job.kind)
    try:
        return await asyncio.wait_for(registered.fn(db, payload, job), timeout=budget)
    except TimeoutError as exc:
        raise JobDeadlineExceeded(
            f"handler for '{job.kind}' exceeded its {budget:g}s deadline and was cancelled"
        ) from exc


async def run_once(
    db: AsyncSession,
    worker_id: str,
    *,
    kinds: tuple[str, ...] | None = None,
    exclude: tuple[str, ...] | None = None,
    now: datetime | None = None,
    on_liveness_tick: Callable[[], None] | None = None,
) -> Job | None:
    """Claim and process a single job. Returns the job (in its terminal/retry
    state) or None if the queue was empty. `kinds`/`exclude` scope the lane
    (see `claim`). `on_liveness_tick` is called on every lease-heartbeat tick while
    the handler runs (the worker's liveness file)."""
    job = await claim(db, worker_id, kinds=kinds, exclude=exclude, now=now)
    if job is None:
        return None

    registered = _HANDLERS.get(job.kind)
    if registered is None:
        # A missing handler is a permanent misconfiguration for that kind —
        # retrying within the same deployment can't help, so dead-letter now.
        job.status = jobmodel.DEAD
        job.last_error = f"no handler registered for kind '{job.kind}'"
        job.locked_at = None
        job.locked_by = None
        await db.commit()
        return job

    payload = json.loads(job.payload_json or "{}")
    job_id, org_id, kind = job.id, job.org_id, job.kind
    tenant_token = set_current_org(org_id)  # run the handler in its tenant's scope
    job_id_token = job_id_ctx.set(job_id)  # …and stamp its log lines (PAT-030)
    job_kind_token = job_kind_ctx.set(kind)
    # The heartbeat task is created AFTER the context is set so it inherits the
    # tenant and job ids (asyncio copies the context at task creation).
    heartbeat_stop = asyncio.Event()
    heartbeat_task = asyncio.create_task(
        _heartbeat_loop(job_id, worker_id, org_id, heartbeat_stop, on_liveness_tick)
    )
    try:
        result = await _run_handler(registered, db, payload, job)
        await _complete(db, job, result)
    except Exception as exc:  # noqa: BLE001 — any handler error retries/dead-letters
        # A rollback expires every attribute on `job`, so reload it before
        # touching attempts/status (accessing an expired attr would do sync IO).
        await db.rollback()
        job = await db.get(Job, job_id)
        assert job is not None  # the row exists — we loaded it moments ago
        log.warning("job %s (%s) failed on attempt %s: %s", job_id, kind, job.attempts, exc)
        retry_after = exc.retry_after_seconds if isinstance(exc, RetryAfterError) else None
        await _fail(
            db,
            job,
            f"{type(exc).__name__}: {exc}",
            now=now,
            retry_after_seconds=retry_after,
            permanent=isinstance(exc, PermanentJobError),
        )
    finally:
        heartbeat_stop.set()
        try:
            await asyncio.wait_for(heartbeat_task, timeout=HEARTBEAT_JOIN_SECONDS)
        except TimeoutError:
            # A renewal stuck on a dead connection must not hold the worker.
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
        job_kind_ctx.reset(job_kind_token)
        job_id_ctx.reset(job_id_token)
        reset_current_org(tenant_token)
    return job


LEASE_EXPIRED_ERROR = "lease expired: the worker died mid-run"


async def reclaim_stale(db: AsyncSession, *, now: datetime | None = None) -> int:
    """Return jobs with an expired lease (crashed worker) to the queue — or to
    the dead-letter state once they have used their attempts.

    BE-002 (audit 2026-09-05): the attempt counter is incremented on CLAIM, but
    this used to requeue every stale lease unconditionally and immediately. A
    job that KILLS its worker (OOM in OCR, a native-parser crash) never reaches
    `_fail`'s dead-letter branch, so it was reclaimed, re-run, killed the next
    worker, and repeated forever — starving every other job in its lane. Now a
    stale lease at `max_attempts` dead-letters with a named reason, and one
    with attempts left backs off like an ordinary failure instead of hot-looping.
    Returns the number of leases touched (requeued + dead-lettered)."""
    now = _now(now)
    cutoff = now - timedelta(seconds=STALE_LEASE_SECONDS)
    stale = list(
        await db.scalars(
            select(Job).where(
                Job.status == jobmodel.RUNNING,
                or_(Job.locked_at.is_(None), Job.locked_at < cutoff),
            )
        )
    )
    reclaimed = 0
    for job in stale:
        values = {
            "locked_at": None,
            "locked_by": None,
            "updated_at": now,
            "last_error": LEASE_EXPIRED_ERROR,
        }
        if job.attempts >= job.max_attempts:
            values["status"] = jobmodel.DEAD
        else:
            values["status"] = jobmodel.QUEUED
            values["run_after"] = now + timedelta(seconds=_backoff(job.attempts))
        # Selection is only a candidate snapshot. A worker can finish/renew, or
        # another sweep can reclaim, before this UPDATE obtains the row lock.
        # Match the observed lease so none of those writes/evidence is replaced.
        result = await db.execute(
            update(Job)
            .where(
                Job.id == job.id,
                Job.org_id == job.org_id,
                Job.status == jobmodel.RUNNING,
                Job.locked_at == job.locked_at,
                Job.locked_by == job.locked_by,
                Job.attempts == job.attempts,
                Job.max_attempts == job.max_attempts,
                or_(Job.locked_at.is_(None), Job.locked_at < cutoff),
            )
            .values(**values)
            .execution_options(synchronize_session="fetch")
        )
        reclaimed += cast(CursorResult, result).rowcount
    await db.commit()
    return reclaimed


class JobNotRetryable(Exception):
    """`retry` was asked to requeue a job that is not dead, failed or waiting."""


# The statuses `retry` may act on. RUNNING is the one that matters (BE-001,
# audit 2026-09-05): clearing `locked_by` on a job a worker is executing makes
# it claimable by a second worker, so the SAME job runs twice in parallel —
# for `everypay.charge_mit` that is a double card charge. SUCCEEDED is refused
# because re-running finished work is a new job, not a retry.
RETRYABLE_STATUSES = frozenset({jobmodel.DEAD, jobmodel.FAILED, jobmodel.QUEUED})


async def retry(db: AsyncSession, job: Job, *, now: datetime | None = None) -> Job:
    """Manually requeue a dead/failed job (resets the attempt counter).
    Refuses a RUNNING or SUCCEEDED job — see `RETRYABLE_STATUSES`."""
    if job.status not in RETRYABLE_STATUSES:
        raise JobNotRetryable(
            f"job {job.id} is {job.status}; only "
            f"{', '.join(sorted(RETRYABLE_STATUSES))} jobs can be retried"
        )
    job.status = jobmodel.QUEUED
    job.attempts = 0
    job.run_after = _now(now)
    job.last_error = None
    job.locked_at = None
    job.locked_by = None
    await db.commit()
    await db.refresh(job)
    return job
