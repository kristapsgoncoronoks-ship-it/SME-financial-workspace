"""A real PostgreSQL metrics fault must not poison the first job dispatch."""

from __future__ import annotations

import asyncio
import os
import signal
import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from app import worker
from app.models import job as jobmodel
from app.models.fx import EcbRate
from app.models.job import Job
from app.models.organization import Organization
from app.services import jobs, queue_health
from tests.test_jobs_startup_concurrency import isolated_pg as isolated_pg
from tests.test_worker_metrics_recovery import SECRET, wire_worker

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.environ.get("RLS_TEST_DATABASE_URL"), reason="native PostgreSQL"),
]


async def test_failed_read_rolls_back_before_first_actual_dispatch_and_retains_committed_scheduler_work(
    isolated_pg, monkeypatch, caplog
):
    _engine, sm = isolated_pg
    org_id = str(uuid.uuid4())
    async with sm() as db:
        db.add(Organization(id=org_id, name="Disposable metrics recovery"))
        await db.commit()
        queued = await jobs.enqueue(db, "test.metrics-recovery", org_id=org_id)
        job_id = queued.id

    signals, _liveness = wire_worker(monkeypatch, sm)
    metrics_calls, scheduler_calls, dispatch, handled = [], [], [], []
    original_handlers = dict(jobs._HANDLERS)
    real_run, real_snapshot = jobs.run_once, queue_health.snapshot

    async def scheduled(db, **kwargs):
        # The real enqueue_daily commits its batch before metrics are read.
        # A real committed fact here proves recovery does not undo that batch.
        scheduler_calls.append(1)
        db.add(EcbRate(rate_date=date(2026, 11, 5), currency="USD", rate=Decimal("1.1")))
        await db.commit()
        return 0

    async def snapshot(db):
        metrics_calls.append(1)
        if len(metrics_calls) == 1:
            # This reaches PostgreSQL and aborts the transaction. Bound secret
            # data exists solely to ensure no exception/SQL/params are logged.
            await db.execute(
                text("SELECT 1 / (length(CAST(:secret AS TEXT)) - length(CAST(:secret AS TEXT)))"),
                {"secret": SECRET},
            )
        return await real_snapshot(db)

    async def observed_dispatch(db, *args, **kwargs):
        try:
            result = await real_run(db, *args, **kwargs)
        except Exception:
            dispatch.append("failed")
            raise
        dispatch.append(result.status if result else "empty")
        if result is None and len(metrics_calls) >= 2:
            signals[signal.SIGTERM]()
        return result

    @jobs.handler("test.metrics-recovery")
    async def handler(db, payload, job):
        handled.append(job.id)
        return {"domain": "completed"}

    monkeypatch.setattr(worker.scheduler, "enqueue_daily", scheduled)
    monkeypatch.setattr(queue_health, "snapshot", snapshot)
    monkeypatch.setattr(jobs, "run_once", observed_dispatch)
    caplog.set_level("INFO", logger="invoiceiq.worker")
    try:
        await asyncio.wait_for(worker.run_forever(0), timeout=10)
        assert dispatch[0] == jobmodel.SUCCEEDED
        assert handled == [job_id] and scheduler_calls == [1]
        async with sm() as db:
            row = await db.get(Job, job_id)
            assert row.status == jobmodel.SUCCEEDED and row.attempts == 1
            assert await db.scalar(select(EcbRate.id)) is not None
        warnings = [
            r for r in caplog.records if r.name == "invoiceiq.worker" and r.levelname == "WARNING"
        ]
        assert len(warnings) == 1 and not warnings[0].exc_info and not warnings[0].args
        assert "queue health metrics snapshot recovered" in caplog.text
        assert "METRICS_PRIVATE" not in caplog.text and "password" not in caplog.text
        assert "worker loop error" not in caplog.text
    finally:
        jobs._HANDLERS.clear()
        jobs._HANDLERS.update(original_handlers)
