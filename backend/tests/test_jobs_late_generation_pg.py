"""Real separate-connection barriers for a handler that outlives its lease.

Each case owns a nonprivileged, FORCE-RLS PostgreSQL schema. Completion must
preserve the successor's entire evidence and discard only uncommitted work.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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


@pytest.fixture(autouse=True)
def restore_handlers():
    original = dict(jobs._HANDLERS)
    yield
    jobs._HANDLERS.clear()
    jobs._HANDLERS.update(original)


async def snapshot(sm, job_id):
    async with sm() as db:
        row = await db.get(Job, job_id)
        return {
            field: getattr(row, field)
            for field in (
                "org_id",
                "kind",
                "status",
                "attempts",
                "max_attempts",
                "run_after",
                "locked_at",
                "locked_by",
                "lease_token",
                "last_error",
                "result_json",
                "updated_at",
            )
        }


async def seed(sm, *, max_attempts=3):
    org_id, now = str(uuid.uuid4()), datetime.now(UTC)
    async with sm() as db:
        db.add(Organization(id=org_id, name="Disposable lease-generation proof"))
        await db.commit()
        row = await jobs.enqueue(
            db,
            "test.late-generation",
            org_id=org_id,
            run_after=now,
            max_attempts=max_attempts,
        )
        return org_id, row.id, now


def outcome(mode):
    if mode == "failure":
        raise RuntimeError("original generation failed")
    if mode == "permanent":
        raise jobs.PermanentJobError("original generation permanently failed")
    if mode == "retry-after":
        raise jobs.RetryAfterError("original provider delay", retry_after_seconds=180)
    return {"worker": "original", "generation": 1}


@pytest.mark.parametrize("mode", ["success", "failure", "permanent", "retry-after"])
@pytest.mark.parametrize(
    "successor", ["queued", "dead", "running", "success", "failure", "manual-retry", "frozen-reset"]
)
async def test_late_handler_preserves_successor_evidence(isolated_pg, mode, successor):
    _engine, sm = isolated_pg
    org_id, job_id, now = await seed(sm, max_attempts=1 if successor == "dead" else 3)
    started, resume = asyncio.Event(), asyncio.Event()

    @jobs.handler("test.late-generation")
    async def delayed(db, payload, job):
        # Pending domain facts must not escape when this generation loses its lease.
        db.add(EcbRate(rate_date=date(2026, 11, 5), currency="USD", rate=Decimal("1.1")))
        started.set()
        await asyncio.wait_for(resume.wait(), timeout=10)
        return outcome(mode)

    async def original():
        async with sm() as db:
            return await jobs.run_once(db, "reused-worker", now=now)

    token = set_current_org(org_id)
    task = asyncio.create_task(original())
    try:
        await asyncio.wait_for(started.wait(), timeout=10)
        original_token = (await snapshot(sm, job_id))["lease_token"]
        assert str(uuid.UUID(original_token)) == original_token
        expired = now + timedelta(seconds=jobs.STALE_LEASE_SECONDS + 1)
        async with sm() as db:
            assert await jobs.reclaim_stale(db, now=expired) == 1
            if successor in {"manual-retry", "frozen-reset"}:
                row = await db.get(Job, job_id)
                await jobs.retry(
                    db,
                    row,
                    now=now if successor == "frozen-reset" else expired + timedelta(seconds=1),
                )
            if successor not in {"queued", "dead"}:
                row = await jobs.claim(db, "reused-worker", now=expired + timedelta(seconds=31))
                assert row.id == job_id
                assert row.attempts == (1 if successor in {"manual-retry", "frozen-reset"} else 2)
                assert row.lease_token != original_token
                if successor == "success":
                    await jobs._complete(db, row, {"worker": "successor", "generation": 2})
                elif successor == "failure":
                    await jobs._fail(db, row, "successor provider evidence", now=expired)
        expected = await snapshot(sm, job_id)
        resume.set()
        returned = await asyncio.wait_for(task, timeout=10)
        assert await snapshot(sm, job_id) == expected
        assert returned.status == expected["status"]
        assert returned.attempts == expected["attempts"]
        async with sm() as db:
            assert await db.scalar(select(EcbRate.id)) is None
    finally:
        resume.set()
        await asyncio.gather(task, return_exceptions=True)
        reset_current_org(token)


async def test_selected_reclaimer_cannot_replace_same_fields_new_token(isolated_pg):
    engine, sm = isolated_pg
    org_id, job_id, now = await seed(sm)
    selected, resume = asyncio.Event(), asyncio.Event()
    expired = now + timedelta(seconds=jobs.STALE_LEASE_SECONDS + 1)

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
        async with sm() as db:
            original = await jobs.claim(db, "same-worker", now=now)
            original_token = original.lease_token
        async with paused_sm() as delayed:
            task = asyncio.create_task(jobs.reclaim_stale(delayed, now=expired))
            await asyncio.wait_for(selected.wait(), timeout=10)
            async with sm() as db:
                assert await jobs.reclaim_stale(db, now=expired) == 1
                row = await db.get(Job, job_id)
                await jobs.retry(db, row, now=now)
                row = await jobs.claim(db, "same-worker", now=now)
                assert row.attempts == 1 and row.lease_token != original_token
            expected = await snapshot(sm, job_id)
            resume.set()
            assert await asyncio.wait_for(task, timeout=10) == 0
            assert await snapshot(sm, job_id) == expected
    finally:
        resume.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        reset_current_org(token)


async def test_claim_cannot_reload_a_successor_as_its_own_identity(isolated_pg):
    engine, sm = isolated_pg
    org_id, job_id, now = await seed(sm)
    ready, resume = asyncio.Event(), asyncio.Event()

    class DelayedPostCommitRead(AsyncSession):
        claim_committed = False
        first_job_read = True

        async def commit(self):
            await super().commit()
            self.claim_committed = True

        async def get(self, entity, ident, **kwargs):
            if entity is Job and self.first_job_read:
                self.first_job_read = False
                if self.claim_committed:
                    ready.set()
                    await asyncio.wait_for(resume.wait(), timeout=10)
            return await super().get(entity, ident, **kwargs)

    delayed_sm = async_sessionmaker(
        engine, class_=DelayedPostCommitRead, expire_on_commit=False, autoflush=False
    )

    @jobs.handler("test.late-generation")
    async def handler(db, payload, job):
        ready.set()
        await asyncio.wait_for(resume.wait(), timeout=10)
        return {"original": "late"}

    async def original():
        async with delayed_sm() as db:
            return await jobs.run_once(db, "original-worker", now=now)

    token = set_current_org(org_id)
    task = asyncio.create_task(original())
    try:
        await asyncio.wait_for(ready.wait(), timeout=10)
        expired = now + timedelta(seconds=jobs.STALE_LEASE_SECONDS + 1)
        async with sm() as db:
            assert await jobs.reclaim_stale(db, now=expired) == 1
            successor = await jobs.claim(db, "successor", now=expired + timedelta(seconds=31))
            await jobs._complete(db, successor, {"successor": "durably completed"})
        expected = await snapshot(sm, job_id)
        resume.set()
        returned = await asyncio.wait_for(task, timeout=10)
        assert returned.status == jobmodel.SUCCEEDED
        assert await snapshot(sm, job_id) == expected
    finally:
        resume.set()
        await asyncio.gather(task, return_exceptions=True)
        reset_current_org(token)


async def test_claim_materializes_real_attempt_over_a_stale_identity_map(isolated_pg):
    _engine, sm = isolated_pg
    org_id, job_id, now = await seed(sm)
    token = set_current_org(org_id)
    try:
        async with sm() as stale:
            cached = await stale.get(Job, job_id)
            assert cached.attempts == 0
            async with sm() as other:
                first = await jobs.claim(other, "first-worker", now=now)
                await jobs._fail(other, first, "first provider evidence", now=now)
            claimed = await jobs.claim(stale, "second-worker", now=now + timedelta(seconds=31))
            assert claimed.attempts == 2
            assert claimed.lease_token == (await snapshot(sm, job_id))["lease_token"]
            completed = await jobs._complete(stale, claimed, {"second": "completed"})
            assert completed.status == jobmodel.SUCCEEDED and completed.attempts == 2
    finally:
        reset_current_org(token)


async def test_handler_rollback_before_deadline_keeps_original_failure_evidence(isolated_pg):
    _engine, sm = isolated_pg
    org_id, _job_id, now = await seed(sm)

    @jobs.handler("test.late-generation", deadline_seconds=0.02)
    async def handler(db, payload, job):
        await db.rollback()
        await asyncio.Event().wait()

    token = set_current_org(org_id)
    try:
        async with sm() as db:
            row = await jobs.run_once(db, "deadline-owner", now=now)
            assert row.status == jobmodel.QUEUED and row.attempts == 1
            assert row.last_error.startswith("JobDeadlineExceeded:")
            assert "test.late-generation" in row.last_error
    finally:
        reset_current_org(token)


async def test_legacy_null_lease_can_renew_and_finalize_without_inventing_identity(
    isolated_pg, monkeypatch
):
    _engine, sm = isolated_pg
    org_id, job_id, now = await seed(sm)
    token = set_current_org(org_id)
    monkeypatch.setattr("app.core.database.SessionLocal", sm)
    try:
        async with sm() as db:
            row = await db.get(Job, job_id)
            row.status, row.attempts = jobmodel.RUNNING, 1
            row.locked_at, row.locked_by = now, "pre-migration-worker"
            await db.commit()
            lease = jobs._Lease.from_job(row)
            assert lease.token is None
            assert await jobs._renew_lease(lease) is True
            completed = await jobs._complete(db, row, {"legacy": "completed"}, lease=lease)
            assert completed.status == jobmodel.SUCCEEDED and completed.lease_token is None
            assert await jobs._renew_lease(lease) is False
    finally:
        reset_current_org(token)


@pytest.mark.parametrize("mode", ["success", "failure", "permanent", "retry-after"])
@pytest.mark.parametrize("domain_transaction", ["pending", "committed", "rolled-back"])
async def test_owned_generation_preserves_handler_transaction_contract(
    isolated_pg, mode, domain_transaction
):
    _engine, sm = isolated_pg
    org_id, job_id, now = await seed(sm)

    @jobs.handler("test.late-generation")
    async def handler(db, payload, job):
        db.add(EcbRate(rate_date=date(2026, 11, 5), currency="USD", rate=Decimal("1.1")))
        if domain_transaction == "committed":
            await db.commit()
        elif domain_transaction == "rolled-back":
            await db.rollback()
        return outcome(mode)

    token = set_current_org(org_id)
    try:
        async with sm() as db:
            returned = await jobs.run_once(db, "owner", now=now)
            assert returned.id == job_id
            assert returned.attempts == 1
            assert str(uuid.UUID(returned.lease_token)) == returned.lease_token
            assert (
                returned.status
                == {
                    "success": jobmodel.SUCCEEDED,
                    "failure": jobmodel.QUEUED,
                    "permanent": jobmodel.DEAD,
                    "retry-after": jobmodel.QUEUED,
                }[mode]
            )
            assert returned.locked_at is None and returned.locked_by is None
            if mode in {"failure", "retry-after"}:
                assert returned.run_after == now + timedelta(
                    seconds=180 if mode == "retry-after" else 30
                )
        async with sm() as db:
            written = await db.scalar(select(EcbRate.id))
            assert (written is not None) == (
                domain_transaction == "committed"
                or (domain_transaction == "pending" and mode == "success")
            )
    finally:
        reset_current_org(token)


@pytest.mark.parametrize("mode", ["success", "failure"])
async def test_lost_generation_keeps_previously_committed_domain_effects(isolated_pg, mode):
    _engine, sm = isolated_pg
    org_id, job_id, now = await seed(sm)
    started, resume = asyncio.Event(), asyncio.Event()

    @jobs.handler("test.late-generation")
    async def handler(db, payload, job):
        db.add(EcbRate(rate_date=date(2026, 11, 5), currency="USD", rate=Decimal("1.1")))
        await db.commit()
        started.set()
        await asyncio.wait_for(resume.wait(), timeout=10)
        return outcome(mode)

    async def original():
        async with sm() as db:
            return await jobs.run_once(db, "old-worker", now=now)

    token = set_current_org(org_id)
    task = asyncio.create_task(original())
    try:
        await asyncio.wait_for(started.wait(), timeout=10)
        async with sm() as db:
            assert (
                await jobs.reclaim_stale(
                    db, now=now + timedelta(seconds=jobs.STALE_LEASE_SECONDS + 1)
                )
                == 1
            )
        expected = await snapshot(sm, job_id)
        resume.set()
        await asyncio.wait_for(task, timeout=10)
        assert await snapshot(sm, job_id) == expected
        async with sm() as db:
            assert await db.scalar(select(EcbRate.id)) is not None
    finally:
        resume.set()
        await asyncio.gather(task, return_exceptions=True)
        reset_current_org(token)


async def test_unknown_handler_cannot_dead_letter_a_newer_completion(isolated_pg, monkeypatch):
    _engine, sm = isolated_pg
    org_id, job_id, now = await seed(sm)
    claimed, resume = asyncio.Event(), asyncio.Event()
    real_claim = jobs.claim

    async def pause_after_claim(*args, **kwargs):
        row = await real_claim(*args, **kwargs)
        claimed.set()
        await asyncio.wait_for(resume.wait(), timeout=10)
        return row

    monkeypatch.setattr(jobs, "claim", pause_after_claim)

    async def original():
        async with sm() as db:
            return await jobs.run_once(db, "old-worker", now=now)

    token = set_current_org(org_id)
    task = asyncio.create_task(original())
    try:
        await asyncio.wait_for(claimed.wait(), timeout=10)
        expired = now + timedelta(seconds=jobs.STALE_LEASE_SECONDS + 1)
        async with sm() as db:
            assert await jobs.reclaim_stale(db, now=expired) == 1
            row = await real_claim(db, "new-worker", now=expired + timedelta(seconds=31))
            await jobs._complete(db, row, {"successor": "completed with its deployed handler"})
        expected = await snapshot(sm, job_id)
        resume.set()
        await asyncio.wait_for(task, timeout=10)
        assert await snapshot(sm, job_id) == expected
    finally:
        resume.set()
        await asyncio.gather(task, return_exceptions=True)
        reset_current_org(token)


@pytest.mark.parametrize("successor", ["owned", "same-worker", "manual-retry", "frozen-reset"])
async def test_heartbeat_renews_only_original_generation(isolated_pg, monkeypatch, successor):
    _engine, sm = isolated_pg
    org_id, job_id, now = await seed(sm)
    started, resume = asyncio.Event(), asyncio.Event()
    heartbeat_waiting, allow_renewal, renewed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    real_renew, renew_results = jobs._renew_lease, []
    renewed_at = now + timedelta(seconds=jobs.STALE_LEASE_SECONDS + 40)

    @jobs.handler("test.late-generation")
    async def handler(db, payload, job):
        started.set()
        await asyncio.wait_for(resume.wait(), timeout=10)
        return {"original": True}

    async def delayed_renew(*args, **kwargs):
        heartbeat_waiting.set()
        await asyncio.wait_for(allow_renewal.wait(), timeout=10)
        result = await real_renew(*args, **kwargs)
        renew_results.append(result)
        renewed.set()
        return result

    monkeypatch.setattr(jobs, "_renew_lease", delayed_renew)
    monkeypatch.setattr(jobs, "LEASE_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(jobs, "_now", lambda supplied=None: supplied or renewed_at)
    monkeypatch.setattr("app.core.database.SessionLocal", sm)

    async def original():
        async with sm() as db:
            return await jobs.run_once(db, "same-worker", now=now)

    token = set_current_org(org_id)
    task = asyncio.create_task(original())
    try:
        await asyncio.wait_for(started.wait(), timeout=10)
        await asyncio.wait_for(heartbeat_waiting.wait(), timeout=10)
        if successor != "owned":
            expired = now + timedelta(seconds=jobs.STALE_LEASE_SECONDS + 1)
            async with sm() as db:
                assert await jobs.reclaim_stale(db, now=expired) == 1
                if successor in {"manual-retry", "frozen-reset"}:
                    row = await db.get(Job, job_id)
                    await jobs.retry(
                        db,
                        row,
                        now=now if successor == "frozen-reset" else expired + timedelta(seconds=1),
                    )
                row = await jobs.claim(db, "same-worker", now=expired + timedelta(seconds=31))
                assert row.attempts == (1 if successor in {"manual-retry", "frozen-reset"} else 2)
        expected = await snapshot(sm, job_id)
        allow_renewal.set()
        await asyncio.wait_for(renewed.wait(), timeout=10)
        assert renew_results == [successor == "owned"]
        if successor != "owned":
            assert await snapshot(sm, job_id) == expected
        else:
            assert (await snapshot(sm, job_id))["locked_at"] == renewed_at
        resume.set()
        returned = await asyncio.wait_for(task, timeout=10)
        if successor == "owned":
            assert returned.status == jobmodel.SUCCEEDED
        else:
            assert await snapshot(sm, job_id) == expected
    finally:
        allow_renewal.set()
        resume.set()
        await asyncio.gather(task, return_exceptions=True)
        reset_current_org(token)
