"""Manual retry is an observed-state command, never authority over a successor.

Real transactions under FORCE RLS; every case owns a temporary schema. The
barrier pauses the old command before either its ORM flush or guarded UPDATE.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.routes import jobs as jobs_route
from app.core.tenant import reset_current_org, set_current_org
from app.models import job as jobmodel
from app.models.fx import EcbRate
from app.models.job import Job
from app.models.organization import Organization
from app.services import jobs
from tests.test_jobs_startup_concurrency import isolated_pg as isolated_pg

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.environ.get("RLS_TEST_DATABASE_URL"), reason="native PostgreSQL"),
]


async def snapshot(sm, job_id):
    async with sm() as db:
        row = await db.get(Job, job_id)
        return {
            key: getattr(row, key)
            for key in (
                "org_id",
                "kind",
                "payload_json",
                "idempotency_key",
                "status",
                "attempts",
                "max_attempts",
                "run_after",
                "locked_at",
                "locked_by",
                "lease_token",
                "last_error",
                "result_json",
                "created_at",
                "updated_at",
            )
        }


async def seed(sm, status, *, attempts=3, token=None):
    org_id, job_id, now = str(uuid.uuid4()), str(uuid.uuid4()), datetime.now(UTC)
    async with sm() as db:
        db.add(Organization(id=org_id, name="Disposable manual-retry race"))
        await db.commit()
        db.add(
            Job(
                id=job_id,
                org_id=org_id,
                kind="test.manual-retry",
                payload_json='{"original":true}',
                idempotency_key="same-unit-of-work",
                status=status,
                attempts=attempts,
                max_attempts=7,
                run_after=now,
                lease_token=token,
                last_error="historical provider evidence",
                result_json='{"original":"retained result"}',
            )
        )
        await db.commit()
    return org_id, job_id, now


def capture_writes(engine):
    writes = []

    def observe(_connection, _cursor, _statement, _parameters, context, _many):
        compiled = getattr(context, "compiled", None)
        statement = getattr(compiled, "statement", None)
        if getattr(statement, "is_insert", False) or getattr(statement, "is_update", False):
            writes.append(statement.table.name)

    event.listen(engine.sync_engine, "before_cursor_execute", observe)
    return writes, observe


async def stale_retry_race(isolated_pg, initial, winner, *, route=False):
    engine, sm = isolated_pg
    original_identity = str(uuid.uuid4())
    original_attempts = 1 if winner == "same-attempt-dead" else 3
    org_id, job_id, now = await seed(
        sm, initial, attempts=original_attempts, token=original_identity
    )
    waiting, resume = asyncio.Event(), asyncio.Event()

    class PausedRetry(AsyncSession):
        write_seen = False

        async def before_write(self):
            if not self.write_seen:
                self.write_seen = True
                waiting.set()
                await asyncio.wait_for(resume.wait(), timeout=10)

        async def execute(self, statement, *args, **kwargs):
            if getattr(statement, "is_update", False) and statement.table.name == "jobs":
                await self.before_write()
            return await super().execute(statement, *args, **kwargs)

        async def commit(self):
            # The pre-fix implementation only sends its ORM writes at commit.
            await self.before_write()
            await super().commit()

    paused_sm = async_sessionmaker(
        engine, class_=PausedRetry, expire_on_commit=False, autoflush=True
    )
    writes, observer = capture_writes(engine)
    tenant, task = set_current_org(org_id), None
    try:
        async with paused_sm() as old:
            stale = await old.get(Job, job_id)
            assert (stale.status, stale.attempts, stale.lease_token) == (
                initial,
                original_attempts,
                original_identity,
            )
            old.add(EcbRate(rate_date=date(2026, 11, 5), currency="USD", rate=Decimal("1.1")))

            async def original_retry():
                try:
                    if route:
                        return await jobs_route.retry_job(
                            job_id, SimpleNamespace(org_id=org_id), old
                        )
                    return await jobs.retry(old, stale, now=now)
                except (jobs.JobNotRetryable, HTTPException) as exc:
                    return exc

            task = asyncio.create_task(original_retry())
            await asyncio.wait_for(waiting.wait(), timeout=10)
            async with sm() as newer:
                row = await newer.get(Job, job_id)
                await jobs.retry(newer, row, now=now)
                assert row.lease_token == original_identity  # resetting attempts retains evidence
                if winner != "queued-same-token":
                    row = await jobs.claim(newer, "same-worker-name", now=now)
                    assert row.attempts == 1 and row.lease_token != original_identity
                    if winner == "success":
                        await jobs._complete(newer, row, {"successor": "completed"})
                    elif winner in {"failure", "same-attempt-dead"}:
                        await jobs._fail(
                            newer,
                            row,
                            "successor provider evidence",
                            now=now,
                            permanent=winner == "same-attempt-dead",
                        )
            expected = await snapshot(sm, job_id)
            if winner == "same-attempt-dead" and initial == jobmodel.DEAD:
                assert (expected["status"], expected["attempts"], expected["run_after"]) == (
                    initial,
                    original_attempts,
                    now,
                )
            if winner == "queued-same-token":
                assert expected["lease_token"] == original_identity
            writes.clear()
            resume.set()
            result = await asyncio.wait_for(task, timeout=10)
            if route:
                assert isinstance(result, HTTPException) and result.status_code == 409
            else:
                assert isinstance(result, jobs.JobNotRetryable)
            assert await snapshot(sm, job_id) == expected
            # A lost command must not auto-flush caller facts before its CAS,
            # even when the supplied session has autoflush=True.
            assert "ecb_rates" not in writes
            async with sm() as check:
                assert await check.scalar(select(EcbRate.id)) is None
    finally:
        resume.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        event.remove(engine.sync_engine, "before_cursor_execute", observer)
        reset_current_org(tenant)


@pytest.mark.parametrize("initial", [jobmodel.DEAD, jobmodel.FAILED])
@pytest.mark.parametrize(
    "winner", ["running", "success", "failure", "queued-same-token", "same-attempt-dead"]
)
async def test_stale_manual_retry_preserves_newer_job_and_pending_facts(
    isolated_pg, initial, winner
):
    await stale_retry_race(isolated_pg, initial, winner)


async def test_native_route_maps_a_lost_manual_retry_to_existing_409(isolated_pg):
    await stale_retry_race(isolated_pg, jobmodel.DEAD, "success", route=True)


async def test_queued_retry_cannot_borrow_reset_attempt_even_with_same_uuid(isolated_pg):
    # Both observations are QUEUED, the UUID and supplied clock stay fixed;
    # the intervening reset alone makes the original attempt snapshot stale.
    await stale_retry_race(isolated_pg, jobmodel.QUEUED, "queued-same-token")


@pytest.mark.parametrize("initial", [jobmodel.DEAD, jobmodel.FAILED, jobmodel.QUEUED])
@pytest.mark.parametrize("legacy_null", [False, True])
async def test_owned_retry_preserves_budget_identity_result_and_commits_pending_facts(
    isolated_pg, initial, legacy_null
):
    engine, sm = isolated_pg
    identity = None if legacy_null else str(uuid.uuid4())
    org_id, job_id, now = await seed(sm, initial, token=identity)
    writes, observer = capture_writes(engine)
    tenant = set_current_org(org_id)
    autoflush_sm = async_sessionmaker(engine, expire_on_commit=False, autoflush=True)
    try:
        async with autoflush_sm() as db:
            row = await db.get(Job, job_id)
            db.add(EcbRate(rate_date=date(2026, 11, 5), currency="USD", rate=Decimal("1.1")))
            retried = await jobs.retry(db, row, now=now)
            assert retried.status == jobmodel.QUEUED and retried.attempts == 0
            assert retried.max_attempts == 7 and retried.run_after == now
            assert retried.lease_token == identity
            assert retried.idempotency_key == "same-unit-of-work"
            assert retried.payload_json == '{"original":true}'
            assert retried.result_json == '{"original":"retained result"}'
            assert (
                retried.last_error is None
                and retried.locked_by is None
                and retried.locked_at is None
            )
        # Establish authority before any pending domain INSERT reaches SQL.
        assert writes == ["jobs", "ecb_rates"]
        async with sm() as db:
            assert await db.scalar(select(EcbRate.id)) is not None
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", observer)
        reset_current_org(tenant)


async def test_retry_cas_cannot_update_another_tenant_under_force_rls(isolated_pg):
    _engine, sm = isolated_pg
    org_id, job_id, now = await seed(sm, jobmodel.DEAD, token=str(uuid.uuid4()))
    before = await snapshot(sm, job_id)
    original = set_current_org(org_id)
    try:
        async with sm() as db:
            loaded = await db.get(Job, job_id)
        wrong_tenant = set_current_org(str(uuid.uuid4()))
        try:
            # Begin a new transaction under the foreign tenant's real GUC;
            # attaching an old object must not confer cross-tenant authority.
            async with sm() as db:
                db.add(loaded)
                with pytest.raises(jobs.JobNotRetryable):
                    await jobs.retry(db, loaded, now=now)
        finally:
            reset_current_org(wrong_tenant)
        assert await snapshot(sm, job_id) == before
    finally:
        reset_current_org(original)
