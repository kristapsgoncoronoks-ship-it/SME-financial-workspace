"""STIR-P1-01 (Stirling-PDF, reference integration 2026-09-07) — the lease
heartbeat on Postgres, under row-level security, with the handler's own
transaction open.

The SQLite tests prove the logic; this proves the two things only Postgres can:

  1. the renewal runs on a SEPARATE connection while the worker's session holds
     an open transaction on the same row's table (a real handler does), and
     neither blocks the other — no lock wait, no deadlock, no serialization
     failure;
  2. the renewal's own session is scoped to the job's tenant, so the UPDATE
     is visible under FORCE ROW LEVEL SECURITY (the `jobs` table is in the
     RLS set) and the row it touches is the tenant's own.

Postgres-gated (`RLS_TEST_DATABASE_URL`), like the other PG-only tests.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.tenant import reset_current_org, set_current_org
from app.models import job as jobmodel
from app.models.job import Job
from app.models.organization import Organization
from app.services import jobs

PG_URL = os.environ.get("RLS_TEST_DATABASE_URL")
pg_only = pytest.mark.skipif(
    not PG_URL,
    reason="set RLS_TEST_DATABASE_URL (a Postgres URL) to run the heartbeat test",
)


@pytest.fixture(autouse=True)
def _clean_handlers():
    saved = dict(jobs._HANDLERS)
    yield
    jobs._HANDLERS.clear()
    jobs._HANDLERS.update(saved)


@pg_only
@pytest.mark.asyncio
async def test_stir_p1_heartbeat_renews_on_a_second_connection_while_the_handler_runs(
    monkeypatch,
):
    engine = create_async_engine(PG_URL)
    sm = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    import app.core.database as database

    monkeypatch.setattr(database, "SessionLocal", sm)
    monkeypatch.setattr(jobs, "LEASE_HEARTBEAT_SECONDS", 0.05)

    org_id = str(uuid.uuid4())
    tok = set_current_org(org_id)
    renewals: list[datetime] = []
    release = asyncio.Event()

    @jobs.handler("test.pg-long")
    async def _long(db, payload, job):
        # A real handler: reads inside its own open transaction, then waits.
        await db.scalar(select(Job.id).where(Job.id == job.id))
        await release.wait()
        return {"ok": True}

    real_renew = jobs._renew_lease

    async def _observed(lease):
        ok = await real_renew(lease)
        if ok:
            renewals.append(datetime.now(UTC))
        return ok

    monkeypatch.setattr(jobs, "_renew_lease", _observed)
    try:
        async with sm() as s:
            s.add(Organization(id=org_id, name="Haulage Heartbeat Co"))
            await s.commit()
        async with sm() as worker_db:
            queued = await jobs.enqueue(worker_db, "test.pg-long", {}, org_id=org_id)
            task = asyncio.create_task(jobs.run_once(worker_db, "pg-worker"))
            # Wait for at least two renewals to land while the handler is parked.
            for _ in range(200):
                if len(renewals) >= 2:
                    break
                await asyncio.sleep(0.02)
            assert len(renewals) >= 2, "no lease renewal happened while the handler ran"
            async with sm() as observer:
                live = await observer.get(Job, queued.id)
                assert live is not None and live.status == jobmodel.RUNNING
                assert live.locked_by == "pg-worker"
                assert live.locked_at is not None
                assert live.locked_at.replace(tzinfo=UTC) >= renewals[0] - timedelta(seconds=1)
            release.set()
            done = await asyncio.wait_for(task, timeout=5.0)
            assert done is not None and done.status == jobmodel.SUCCEEDED
            assert done.locked_by is None and done.locked_at is None
    finally:
        reset_current_org(tok)
        async with sm() as s:
            for row in await s.scalars(select(Job).where(Job.org_id == org_id)):
                await s.delete(row)
            org = await s.get(Organization, org_id)
            if org is not None:
                await s.delete(org)
            await s.commit()
        await engine.dispose()


@pg_only
@pytest.mark.asyncio
async def test_stir_p1_renewal_is_tenant_scoped_under_rls(monkeypatch):
    """The renewal session carries the job's tenant: the UPDATE matches the
    row under RLS. Called for a DIFFERENT tenant's job id it matches nothing —
    RLS hides the row — and reports False instead of touching it."""
    engine = create_async_engine(PG_URL)
    sm = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    import app.core.database as database

    monkeypatch.setattr(database, "SessionLocal", sm)
    org_a, org_b = str(uuid.uuid4()), str(uuid.uuid4())
    tok = set_current_org(org_a)
    try:
        async with sm() as s:
            s.add(Organization(id=org_a, name="Site Crew A"))
            s.add(Organization(id=org_b, name="Site Crew B"))
            await s.commit()
        async with sm() as s:
            await jobs.enqueue(s, "recurring.generate", {}, org_id=org_a)
            claimed = await jobs.claim(s, "w-a")
            assert claimed is not None
            stale = datetime.now(UTC) - timedelta(seconds=jobs.STALE_LEASE_SECONDS + 60)
            claimed.locked_at = stale
            await s.commit()
            job_id = claimed.id
            lease = jobs._Lease.from_job(claimed)
        assert await jobs._renew_lease(lease) is True
        from dataclasses import replace

        assert await jobs._renew_lease(replace(lease, org_id=org_b)) is False
        async with sm() as s:
            live = await s.get(Job, job_id)
            assert live is not None
            assert live.locked_at.replace(tzinfo=UTC) > stale
    finally:
        reset_current_org(tok)
        async with sm() as s:
            for oid in (org_a, org_b):
                t = set_current_org(oid)
                try:
                    for row in await s.scalars(select(Job).where(Job.org_id == oid)):
                        await s.delete(row)
                    await s.commit()
                finally:
                    reset_current_org(t)
            for oid in (org_a, org_b):
                org = await s.get(Organization, oid)
                if org is not None:
                    await s.delete(org)
            await s.commit()
        await engine.dispose()
