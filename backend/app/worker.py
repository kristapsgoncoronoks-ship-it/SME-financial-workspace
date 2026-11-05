"""Background worker: drains the durable job queue.

Run one (or several) alongside the API:

    python -m app.worker

Each loop reclaims abandoned jobs (crashed workers), then processes ready jobs
until the queue is empty, then sleeps briefly. Multiple workers are safe — the
claim is atomic, so a job runs on exactly one worker at a time. The worker runs
UNSCOPED (no tenant context); each job is dispatched inside its own tenant scope.

Liveness: every tick touches `WORKER_LIVENESS_PATH`; `python -m app.worker_probe`
(the container's healthcheck / livenessProbe) fails when that file goes stale.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import socket
from datetime import date

from app import worker_probe
from app.core.config import log_production_warnings, settings
from app.core.database import SessionLocal
from app.core.observability import configure_logging
from app.services import (
    job_handlers,  # noqa: F401 — registers the handlers
    jobs,
    scheduler,
)

log = logging.getLogger("invoiceiq.worker")

IDLE_SLEEP_SECONDS = 2.0
# How often (in loops) to run the stale-lease reclaim sweep.
_RECLAIM_EVERY = 10


def _worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _touch_liveness() -> None:
    """Liveness for the container probe (`python -m app.worker_probe`): the
    file's mtime is 'the loop last turned'. Touched at the top of every tick,
    after every job, and on every lease heartbeat inside a long job. Failing to
    touch it (read-only path, disk full) is logged once per loop and never
    stops work — a probe that then restarts us is the honest outcome."""
    worker_probe.touch(settings.worker_liveness_path)


async def run_forever(
    poll_interval: float = IDLE_SLEEP_SECONDS,
    *,
    kinds: tuple[str, ...] | None = None,
    exclude: tuple[str, ...] | None = None,
) -> None:
    """Drain the queue forever. `kinds`/`exclude` scope this worker to a LANE so a
    resource-heavy kind (OCR, PDF generation) can run on its own dedicated pool
    without starving light jobs — a lane-less worker (both None) drains all."""
    worker_id = _worker_id()
    stop = asyncio.Event()

    def _request_stop(*_a):
        log.info("worker %s stopping…", worker_id)
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:  # pragma: no cover — e.g. Windows
            pass

    lane = f"kinds={kinds}" if kinds else (f"exclude={exclude}" if exclude else "all")
    log.info("worker %s started; lane=%s; handlers=%s", worker_id, lane, jobs.registered_kinds())
    tick = 0
    last_scheduled: date | None = None
    metrics_failed = False
    while not stop.is_set():
        tick += 1
        try:
            _touch_liveness()
        except OSError as exc:
            log.warning(
                "liveness heartbeat not written to %s: %s", settings.worker_liveness_path, exc
            )
        try:
            async with SessionLocal() as db:
                if tick % _RECLAIM_EVERY == 1:
                    reclaimed = await jobs.reclaim_stale(db)
                    if reclaimed:
                        log.info("reclaimed %s stale job(s)", reclaimed)
                # Once per calendar day, enqueue the periodic jobs (idempotent).
                today = date.today()
                if last_scheduled != today:
                    created = await scheduler.enqueue_daily(db, today=today)
                    last_scheduled = today
                    if created:
                        log.info("scheduled %s daily job(s) for %s", created, today)
                # Refresh the queue-health gauges (dead-letter depth, queue lag)
                # so /metrics stays warm even when the API tier is idle.
                metrics_session_usable = True
                try:
                    from app.services import queue_health

                    await queue_health.snapshot(db)
                except Exception:  # noqa: BLE001 — never let metrics break the loop
                    if not metrics_failed:
                        # Exceptions may contain SQL, bound tenant data or DB
                        # credentials. Emit one fixed warning per outage only.
                        log.warning("queue health metrics snapshot failed; recovering transaction")
                    metrics_failed = True
                    try:
                        # Snapshot only reads. Reclaim and the scheduler have
                        # already committed before this point, so a failed PG
                        # read can be recovered without undoing business work.
                        await db.rollback()
                    except Exception:  # noqa: BLE001 — discard an unrecoverable session
                        metrics_session_usable = False
                else:
                    if metrics_failed:
                        log.info("queue health metrics snapshot recovered")
                    metrics_failed = False
                # Drain everything currently ready before sleeping.
                processed = 0
                while metrics_session_usable and not stop.is_set():
                    job = await jobs.run_once(
                        db,
                        worker_id,
                        kinds=kinds,
                        exclude=exclude,
                        on_liveness_tick=_touch_liveness,
                    )
                    if job is None:
                        break
                    processed += 1
                    log.info("job %s (%s) → %s", job.id, job.kind, job.status)
                    try:
                        _touch_liveness()
                    except OSError:
                        pass  # logged at the top of the next tick
        except Exception:  # noqa: BLE001 — never let the loop die
            log.exception("worker loop error")
            processed = 0
        if processed == 0:
            try:
                await asyncio.wait_for(stop.wait(), timeout=poll_interval)
            except TimeoutError:
                pass
    log.info("worker %s stopped", worker_id)


def main() -> None:
    parser = argparse.ArgumentParser(description="InvoiceIQ background worker")
    parser.add_argument(
        "--poll",
        type=float,
        default=IDLE_SLEEP_SECONDS,
        help="seconds to sleep when the queue is empty",
    )
    parser.add_argument(
        "--kinds",
        default="",
        help="comma-separated job kinds this worker handles (a LANE). "
        "Empty = every kind. e.g. --kinds email.extract",
    )
    parser.add_argument(
        "--exclude",
        default="",
        help="comma-separated job kinds this worker SKIPS (the complement lane). "
        "e.g. --exclude email.extract for the light/general pool",
    )
    args = parser.parse_args()

    def _split(v: str) -> tuple[str, ...] | None:
        parts = tuple(k.strip() for k in v.split(",") if k.strip())
        return parts or None

    kinds, exclude = _split(args.kinds), _split(args.exclude)
    if kinds and exclude:
        parser.error("pass --kinds OR --exclude, not both")

    configure_logging(settings.structured_logs)
    log_production_warnings(log)
    asyncio.run(run_forever(args.poll, kinds=kinds, exclude=exclude))


if __name__ == "__main__":
    main()
