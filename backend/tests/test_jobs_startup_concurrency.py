"""Native PostgreSQL barriers for reclaim/worker and simultaneous FX startup races.

Every test owns a temporary schema; no existing reference or tenant rows change.
The real services run on separate connections under the ordinary nonprivileged role.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from app.core.tenant import reset_current_org, set_current_org
from app.models import Base
from app.models import job as jobmodel
from app.models.fx import EcbRate
from app.models.job import Job
from app.models.organization import Organization
from app.services import fx, jobs

PG_URL = os.environ.get("RLS_TEST_DATABASE_URL")
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not PG_URL, reason="set RLS_TEST_DATABASE_URL for native concurrency proofs"
    ),
]


@pytest_asyncio.fixture
async def isolated_pg():
    schema = "jobs_startup_" + uuid.uuid4().hex
    engine = create_async_engine(
        PG_URL,
        pool_size=6,
        max_overflow=0,
        execution_options={"schema_translate_map": {None: schema}},
    )
    try:
        async with engine.begin() as connection:
            role = (
                await connection.execute(
                    text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
                )
            ).one()
            assert role == (False, False), "race proofs require the application role"
            await connection.execute(CreateSchema(schema))
            await connection.run_sync(
                lambda c: Base.metadata.create_all(
                    c, tables=[Organization.__table__, Job.__table__, EcbRate.__table__]
                )
            )
            await connection.execute(text(f'ALTER TABLE "{schema}".jobs ENABLE ROW LEVEL SECURITY'))
            await connection.execute(text(f'ALTER TABLE "{schema}".jobs FORCE ROW LEVEL SECURITY'))
            predicate = (
                "current_setting('app.current_org', true) IS NULL "
                "OR current_setting('app.current_org', true) = '' "
                "OR org_id::text = current_setting('app.current_org', true)"
            )
            await connection.execute(
                text(
                    f'CREATE POLICY tenant_isolation ON "{schema}".jobs '
                    f"USING ({predicate}) WITH CHECK ({predicate})"
                )
            )
        yield engine, async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    finally:
        async with engine.begin() as connection:
            await connection.execute(DropSchema(schema, cascade=True, if_exists=True))
        await engine.dispose()


async def _snapshot(sm, job_id):
    async with sm() as db:
        job = await db.get(Job, job_id)
        return {
            key: getattr(job, key)
            for key in (
                "status",
                "attempts",
                "run_after",
                "locked_at",
                "locked_by",
                "last_error",
                "result_json",
                "updated_at",
            )
        }


@pytest.mark.parametrize("winner", ["complete", "renew", "reclaim"])
async def test_reclaim_does_not_overwrite_a_changed_lease(isolated_pg, monkeypatch, winner):
    engine, sm = isolated_pg
    now = datetime.now(UTC)
    org_id, job_id = str(uuid.uuid4()), str(uuid.uuid4())
    async with sm() as db:
        db.add(Organization(id=org_id, name="Reclaim race"))
        await db.commit()
        db.add(
            Job(
                id=job_id,
                org_id=org_id,
                kind="test.race",
                status=jobmodel.RUNNING,
                locked_by="original-worker",
                locked_at=now - timedelta(minutes=10),
                attempts=1,
                max_attempts=3,
                run_after=now - timedelta(minutes=11),
            )
        )
        await db.commit()

    selected, resume = asyncio.Event(), asyncio.Event()

    class PausedReclaimer(AsyncSession):
        async def scalars(self, *args, **kwargs):
            rows = await super().scalars(*args, **kwargs)
            selected.set()
            await asyncio.wait_for(resume.wait(), timeout=10)
            return rows

    paused_sm = async_sessionmaker(
        engine, class_=PausedReclaimer, expire_on_commit=False, autoflush=False
    )
    token = set_current_org(org_id)
    task = None
    try:
        async with paused_sm() as reclaimer:
            task = asyncio.create_task(jobs.reclaim_stale(reclaimer, now=now))
            await asyncio.wait_for(selected.wait(), timeout=10)
            async with sm() as worker:
                if winner == "complete":
                    job = await worker.get(Job, job_id)
                    await jobs._complete(worker, job, {"durable": "completed result"})
                elif winner == "renew":
                    monkeypatch.setattr("app.core.database.SessionLocal", sm)
                    job = await worker.get(Job, job_id)
                    assert await jobs._renew_lease(jobs._Lease.from_job(job))
                else:
                    assert await jobs.reclaim_stale(worker, now=now) == 1
            expected = await _snapshot(sm, job_id)
            resume.set()
            reclaimed = await asyncio.wait_for(task, timeout=10)
            observed = await _snapshot(sm, job_id)
            assert (reclaimed, observed) == (0, expected)
    finally:
        resume.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        reset_current_org(token)


async def _rate_snapshot(sm):
    async with sm() as db:
        return list(
            (
                await db.execute(
                    select(EcbRate.rate_date, EcbRate.currency, EcbRate.rate).order_by(
                        EcbRate.rate_date, EcbRate.currency
                    )
                )
            ).all()
        )


@pytest.mark.parametrize("initial", ["empty", "partial-live"])
async def test_four_startups_match_serial_dated_rates_without_warnings(isolated_pg, initial):
    engine, sm = isolated_pg
    today = date(2026, 7, 18)

    async def initial_rows():
        if initial == "partial-live":
            async with sm() as db:
                await fx.load_rates(db, [(date(2026, 7, 1), "USD", Decimal("9.123456"))])

    await initial_rows()
    async with sm() as db:
        serial_written = await fx.ensure_seed_rates(db, today)
        serial_written += await fx.ensure_european_coverage(db, today)
    expected = await _rate_snapshot(sm)
    async with sm() as db:
        await db.execute(delete(EcbRate))
        await db.commit()
    await initial_rows()

    barrier = asyncio.Barrier(4)

    class SimultaneousStartup(AsyncSession):
        async def scalar(self, *args, **kwargs):
            value = await super().scalar(*args, **kwargs)
            # All workers observe the same initial store before any inserts.
            if not getattr(self, "initial_probe_seen", False):
                self.initial_probe_seen = True
                await asyncio.wait_for(barrier.wait(), timeout=10)
            return value

        async def scalars(self, *args, **kwargs):
            value = await super().scalars(*args, **kwargs)
            if initial == "partial-live" and args[0].is_select:
                await asyncio.wait_for(barrier.wait(), timeout=10)
            return value

    startup_sm = async_sessionmaker(
        engine, class_=SimultaneousStartup, expire_on_commit=False, autoflush=False
    )

    async def boot():
        async with startup_sm() as db:
            written = await fx.ensure_seed_rates(db, today)
            return written + await fx.ensure_european_coverage(db, today)

    outcomes = await asyncio.gather(*(boot() for _ in range(4)), return_exceptions=True)
    assert all(isinstance(result, int) for result in outcomes), [
        type(result).__name__ for result in outcomes
    ]
    assert sum(outcomes) == serial_written
    assert await _rate_snapshot(sm) == expected


@pytest.mark.parametrize("initializer", ["ensure_seed_rates", "ensure_european_coverage"])
async def test_seed_database_errors_propagate_and_roll_back_the_whole_snapshot(
    isolated_pg, initializer
):
    engine, sm = isolated_pg
    today = date(2026, 7, 18)
    if initializer == "ensure_european_coverage":
        async with sm() as db:
            await fx.load_rates(db, [(today, "USD", Decimal("9.123456"))])
    expected = await _rate_snapshot(sm)
    schema = engine.get_execution_options()["schema_translate_map"][None]
    async with engine.begin() as connection:
        # Reject a later insert batch, after earlier rows were sent to the DB.
        # Existing live data is retained; this is a disposable fault constraint.
        await connection.execute(
            text(
                f'ALTER TABLE "{schema}".ecb_rates ADD CONSTRAINT synthetic_seed_refusal '
                "CHECK (rate_date < DATE '2025-05-01') NOT VALID"
            )
        )
    async with sm() as db:
        with pytest.raises(IntegrityError, match="synthetic_seed_refusal"):
            await getattr(fx, initializer)(db, today)
        await db.rollback()
    assert await _rate_snapshot(sm) == expected


async def test_reclaim_keeps_other_tenant_leases_untouched_under_rls(isolated_pg):
    _engine, sm = isolated_pg
    now = datetime.now(UTC)
    orgs = [str(uuid.uuid4()), str(uuid.uuid4())]
    async with sm() as db:
        db.add_all([Organization(id=org, name="Scoped reclaim") for org in orgs])
        await db.commit()
        db.add_all(
            [
                Job(
                    org_id=org,
                    kind="test.scope",
                    status=jobmodel.RUNNING,
                    locked_at=now - timedelta(minutes=10),
                    locked_by="worker",
                    attempts=1,
                    max_attempts=3,
                )
                for org in orgs
            ]
        )
        await db.commit()
    token = set_current_org(orgs[0])
    try:
        async with sm() as db:
            assert await jobs.reclaim_stale(db, now=now) == 1
            assert await db.scalar(select(func.count()).select_from(Job)) == 1
    finally:
        reset_current_org(token)
    async with sm() as db:
        states = dict((await db.execute(select(Job.org_id, Job.status))).all())
    assert states == {orgs[0]: jobmodel.QUEUED, orgs[1]: jobmodel.RUNNING}
