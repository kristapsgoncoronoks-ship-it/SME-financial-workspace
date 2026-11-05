"""Durable job queue: enqueue, atomic claim, retries with backoff, dead-letter,
idempotency, stale-lease reclaim, and tenant isolation."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import job as jobmodel
from app.models.job import Job
from app.models.membership import Membership
from app.models.organization import Organization
from app.services import jobs


async def _org(db_session) -> str:
    return await db_session.scalar(select(Organization.id).limit(1))


@pytest.fixture(autouse=True)
def _clean_handlers():
    """Snapshot the handler registry so test handlers don't leak."""
    saved = dict(jobs._HANDLERS)
    yield
    jobs._HANDLERS.clear()
    jobs._HANDLERS.update(saved)


@pytest.mark.asyncio
async def test_enqueue_claim_run_success(auth_client, db_session):
    org = await _org(db_session)
    seen = []

    @jobs.handler("test.echo")
    async def _echo(db, payload, job):
        seen.append(payload)
        return {"ok": True, "n": payload.get("n")}

    await jobs.enqueue(db_session, "test.echo", {"n": 7}, org_id=org)
    job = await jobs.run_once(db_session, "w1")
    assert job is not None
    assert job.status == jobmodel.SUCCEEDED
    assert job.attempts == 1
    assert seen == [{"n": 7}]
    assert '"n": 7' in (job.result_json or "") or '"n":7' in (job.result_json or "")

    # Queue now empty.
    assert await jobs.run_once(db_session, "w1") is None


@pytest.mark.asyncio
async def test_idempotency_key_dedupes(auth_client, db_session):
    org = await _org(db_session)
    a = await jobs.enqueue(
        db_session, "recurring.generate", {}, org_id=org, idempotency_key="daily"
    )
    b = await jobs.enqueue(
        db_session, "recurring.generate", {}, org_id=org, idempotency_key="daily"
    )
    assert a.id == b.id
    count = await db_session.scalar(
        select(jobs.Job.id).where(Job.org_id == org, Job.idempotency_key == "daily")
    )
    assert count is not None
    rows = list(await db_session.scalars(select(Job).where(Job.idempotency_key == "daily")))
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_failure_retries_with_backoff_then_dead(auth_client, db_session):
    org = await _org(db_session)

    @jobs.handler("test.boom")
    async def _boom(db, payload, job):
        raise RuntimeError("kaboom")

    now = datetime(2026, 7, 20, 12, 0, 0, tzinfo=UTC)
    await jobs.enqueue(db_session, "test.boom", {}, org_id=org, max_attempts=2, run_after=now)

    job = await jobs.run_once(db_session, "w1", now=now)
    # First failure → requeued for a future retry, not yet dead.
    assert job.status == jobmodel.QUEUED
    assert job.attempts == 1
    assert job.run_after > now
    assert "kaboom" in job.last_error

    # Not yet due at `now` → claim returns nothing.
    assert await jobs.run_once(db_session, "w1", now=now) is None

    # After the backoff window, the second (final) attempt dead-letters it.
    later = job.run_after + timedelta(seconds=1)
    job2 = await jobs.run_once(db_session, "w1", now=later)
    assert job2.id == job.id
    assert job2.status == jobmodel.DEAD
    assert job2.attempts == 2


@pytest.mark.asyncio
async def test_unknown_kind_dead_letters(auth_client, db_session):
    org = await _org(db_session)
    await jobs.enqueue(db_session, "test.nohandler", {}, org_id=org, max_attempts=3)
    job = await jobs.run_once(db_session, "w1")
    assert job.status == jobmodel.DEAD
    assert "no handler" in job.last_error


@pytest.mark.asyncio
async def test_claim_is_exclusive(auth_client, db_session):
    """Two claims never return the same job."""
    org = await _org(db_session)

    @jobs.handler("test.slow")
    async def _noop(db, payload, job):
        return {}

    await jobs.enqueue(db_session, "test.slow", {"i": 1}, org_id=org)
    await jobs.enqueue(db_session, "test.slow", {"i": 2}, org_id=org)

    j1 = await jobs.claim(db_session, "w1")
    j2 = await jobs.claim(db_session, "w2")
    j3 = await jobs.claim(db_session, "w3")
    assert j1 is not None and j2 is not None
    assert j1.id != j2.id  # distinct jobs
    assert j3 is None  # only two were queued


@pytest.mark.asyncio
async def test_reclaim_stale_lease(auth_client, db_session):
    org = await _org(db_session)
    j = await jobs.enqueue(db_session, "recurring.generate", {}, org_id=org)
    # Simulate a crashed worker: claimed long ago, still 'running'.
    claimed = await jobs.claim(db_session, "dead-worker")
    claimed.locked_at = datetime.now(UTC) - timedelta(seconds=jobs.STALE_LEASE_SECONDS + 60)
    await db_session.commit()

    reclaimed = await jobs.reclaim_stale(db_session)
    assert reclaimed == 1
    refreshed = await db_session.get(Job, j.id)
    assert refreshed.status == jobmodel.QUEUED


@pytest.mark.asyncio
async def test_recurring_handler_generates_invoices(auth_client, db_session):
    """The real recurring.generate handler emits invoices when run via the queue."""
    # Activate issuing + a due schedule through the API.
    issuer = {
        "legal_name": "InvoiceIQ Demo BV",
        "vat_number": "NL123456789B01",
        "registration_number": "NL-KVK-12345678",
        "address_line1": "Keizersgracht 1",
        "city": "Amsterdam",
        "postal_code": "1015 CJ",
        "country": "NL",
        "email": "b@i.test",
    }
    assert (await auth_client.put("/api/v1/issuer", json=issuer)).status_code == 200
    await auth_client.put("/api/v1/modules/issuing", json={"enabled": True})
    await auth_client.post(
        "/api/v1/issued/recurring",
        json={
            "template": {
                "buyer_name": "Initech",
                "lines": [
                    {
                        "description": "Retainer",
                        "quantity": "1",
                        "unit_price": "100",
                        "vat_rate": "21",
                    }
                ],
            },
            "frequency": "monthly",
            "start_date": "2020-01-01",
            "end_date": "2020-01-15",
        },
    )

    org = await _org(db_session)
    await jobs.enqueue(db_session, "recurring.generate", {}, org_id=org)
    job = await jobs.run_once(db_session, "w1")
    assert job.status == jobmodel.SUCCEEDED
    assert '"generated": 1' in job.result_json


@pytest.mark.asyncio
async def test_jobs_api_enqueue_list_and_isolation(auth_client, client):
    # Owner can enqueue an allowlisted kind.
    r = await auth_client.post("/api/v1/jobs", json={"kind": "dunning.run"})
    assert r.status_code == 201, r.text
    job_id = r.json()["id"]
    assert r.json()["status"] == "queued"

    # A non-allowlisted kind is refused.
    bad = await auth_client.post("/api/v1/jobs", json={"kind": "test.boom"})
    assert bad.status_code == 400

    # Visible to its own tenant.
    lst = (await auth_client.get("/api/v1/jobs")).json()
    assert any(j["id"] == job_id for j in lst)

    # A second tenant sees none of it.
    reg = await client.post(
        "/api/v1/auth/register",
        json={
            "organization_name": "Other Co",
            "name": "O",
            "email": "o@o.io",
            "password": "supersecret2",
        },
    )
    client.headers["Authorization"] = f"Bearer {reg.json()['token']['access_token']}"
    assert (await client.get("/api/v1/jobs")).json() == []
    assert (await client.get(f"/api/v1/jobs/{job_id}")).status_code == 404


@pytest.mark.asyncio
async def test_scheduler_enqueues_daily_jobs_idempotently(auth_client, db_session):
    from datetime import date

    from app.services import scheduler

    today = date(2026, 7, 20)
    created = await scheduler.enqueue_daily(db_session, today=today)
    # One org × the daily kinds (recurring-generate, dunning-run, ap-due-alerts)
    # + the single GLOBAL fx.refresh job (WO-8: one per day total, not per org).
    n = len(scheduler.DAILY_KINDS) + 1
    assert created == n
    # Running again the same day adds nothing (idempotent per date).
    assert await scheduler.enqueue_daily(db_session, today=today) == 0
    # A new day enqueues fresh jobs.
    assert await scheduler.enqueue_daily(db_session, today=date(2026, 7, 21)) == n


@pytest.mark.asyncio
async def test_jobs_api_enqueue_requires_admin(auth_client, client, db_session):
    """A read-only user cannot enqueue jobs."""
    # Invite + accept a low-privilege user in the same org via direct role set.
    from sqlalchemy import update as _update

    from app.models.user import UserRole

    # Downgrade a freshly-registered second account is complex; instead assert the
    # allowlist + admin gate by flipping the current user's role to 'user'.
    org = await _org(db_session)
    await db_session.execute(
        _update(Membership).where(Membership.org_id == org).values(role=UserRole.user)
    )
    await db_session.commit()

    r = await auth_client.post("/api/v1/jobs", json={"kind": "dunning.run"})
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_lane_kinds_only_claims_matching(auth_client, db_session):
    """A worker scoped with `kinds` leases only that lane; other kinds are left
    for another pool (resource isolation for OCR/heavy modules)."""
    org = await _org(db_session)

    @jobs.handler("email.extract")
    async def _extract(db, payload, job):
        return {"lane": "extract"}

    @jobs.handler("dunning.run")
    async def _dunning(db, payload, job):
        return {"lane": "general"}

    await jobs.enqueue(db_session, "dunning.run", {}, org_id=org)
    await jobs.enqueue(db_session, "email.extract", {}, org_id=org)

    # The extract lane only ever claims email.extract, even though dunning is
    # older/ready — it is invisible to this worker.
    job = await jobs.run_once(db_session, "extract-worker", kinds=("email.extract",))
    assert job is not None and job.kind == "email.extract"
    # Nothing else in this lane.
    assert await jobs.run_once(db_session, "extract-worker", kinds=("email.extract",)) is None
    # The dunning job is still queued for the general pool.
    remaining = await db_session.scalar(
        select(Job.status).where(Job.kind == "dunning.run", Job.org_id == org)
    )
    assert remaining == jobmodel.QUEUED


@pytest.mark.asyncio
async def test_lane_exclude_skips_kind(auth_client, db_session):
    """The complementary lane (`exclude`) drains everything BUT the heavy kind."""
    org = await _org(db_session)

    @jobs.handler("email.extract")
    async def _extract(db, payload, job):
        return {}

    @jobs.handler("dunning.run")
    async def _dunning(db, payload, job):
        return {}

    await jobs.enqueue(db_session, "email.extract", {}, org_id=org)
    await jobs.enqueue(db_session, "dunning.run", {}, org_id=org)

    job = await jobs.run_once(db_session, "general", exclude=("email.extract",))
    assert job is not None and job.kind == "dunning.run"
    # email.extract is never claimed by the general lane.
    assert await jobs.run_once(db_session, "general", exclude=("email.extract",)) is None
    left = await db_session.scalar(
        select(Job.status).where(Job.kind == "email.extract", Job.org_id == org)
    )
    assert left == jobmodel.QUEUED


# --- audit 2026-09-05: BE-001 / BE-002 / BE-003 ------------------------------


@pytest.mark.asyncio
async def test_be001_retry_refuses_a_running_job(auth_client, db_session):
    """BE-001: clearing `locked_by` on a job a worker is executing makes it
    claimable by a second worker, so the SAME job runs twice in parallel. The
    route answers 409 and the row is untouched."""
    org = await _org(db_session)

    @jobs.handler("test.slow")
    async def _noop(db, payload, job):
        return {}

    j = await jobs.enqueue(db_session, "test.slow", {}, org_id=org)
    claimed = await jobs.claim(db_session, "w1")
    assert claimed is not None and claimed.status == jobmodel.RUNNING

    r = await auth_client.post(f"/api/v1/jobs/{j.id}/retry")
    assert r.status_code == 409, r.text
    assert "running" in r.json()["detail"]
    await db_session.refresh(claimed)
    assert claimed.status == jobmodel.RUNNING
    assert claimed.locked_by == "w1"
    # And no second worker can take it.
    assert await jobs.claim(db_session, "w2") is None


@pytest.mark.asyncio
async def test_be001_retry_refuses_a_finished_job_but_requeues_a_dead_one(auth_client, db_session):
    org = await _org(db_session)

    @jobs.handler("test.ok")
    async def _ok(db, payload, job):
        return {"done": True}

    done = await jobs.enqueue(db_session, "test.ok", {}, org_id=org)
    await jobs.run_once(db_session, "w1")
    r = await auth_client.post(f"/api/v1/jobs/{done.id}/retry")
    assert r.status_code == 409, r.text

    @jobs.handler("test.boom")
    async def _boom(db, payload, job):
        raise RuntimeError("kaboom")

    dead = await jobs.enqueue(db_session, "test.boom", {}, org_id=org, max_attempts=1)
    await jobs.run_once(db_session, "w1")
    await db_session.refresh(dead)
    assert dead.status == jobmodel.DEAD
    r = await auth_client.post(f"/api/v1/jobs/{dead.id}/retry")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == jobmodel.QUEUED
    assert r.json()["attempts"] == 0


@pytest.mark.asyncio
async def test_be002_a_stale_lease_at_max_attempts_dead_letters_instead_of_looping(
    auth_client, db_session
):
    """BE-002: a job that kills its worker never reaches `_fail`. Before the fix
    `reclaim_stale` requeued it unconditionally and immediately — forever."""
    org = await _org(db_session)

    @jobs.handler("test.poison")
    async def _poison(db, payload, job):
        return {}

    await jobs.enqueue(db_session, "test.poison", {}, org_id=org, max_attempts=1)
    claimed = await jobs.claim(db_session, "dying-worker")
    assert claimed.attempts == 1  # the one and only attempt has been spent
    claimed.locked_at = datetime.now(UTC) - timedelta(seconds=jobs.STALE_LEASE_SECONDS + 60)
    await db_session.commit()

    assert await jobs.reclaim_stale(db_session) == 1
    await db_session.refresh(claimed)
    assert claimed.status == jobmodel.DEAD
    assert claimed.last_error == jobs.LEASE_EXPIRED_ERROR
    assert claimed.locked_by is None
    # Nothing left to claim: the crash-loop is over.
    assert await jobs.claim(db_session, "w2") is None


@pytest.mark.asyncio
async def test_be002_a_stale_lease_with_attempts_left_backs_off_rather_than_hot_looping(
    auth_client, db_session
):
    org = await _org(db_session)

    @jobs.handler("test.flaky")
    async def _flaky(db, payload, job):
        return {}

    await jobs.enqueue(db_session, "test.flaky", {}, org_id=org, max_attempts=3)
    claimed = await jobs.claim(db_session, "dying-worker")
    now = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    claimed.locked_at = now - timedelta(seconds=jobs.STALE_LEASE_SECONDS + 60)
    await db_session.commit()

    assert await jobs.reclaim_stale(db_session, now=now) == 1
    await db_session.refresh(claimed)
    assert claimed.status == jobmodel.QUEUED
    assert claimed.run_after.replace(tzinfo=UTC) > now  # not claimable this instant
    assert claimed.last_error == jobs.LEASE_EXPIRED_ERROR
    # Not due yet → the next worker does not immediately re-run it …
    assert await jobs.claim(db_session, "w2", now=now) is None
    # … but it is still alive once the backoff has passed.
    later = claimed.run_after.replace(tzinfo=UTC) + timedelta(seconds=1)
    assert (await jobs.claim(db_session, "w2", now=later)) is not None


@pytest.mark.asyncio
async def test_be003_a_non_idempotency_integrity_error_is_not_answered_with_someone_elses_job(
    auth_client, db_session
):
    from sqlalchemy.exc import IntegrityError

    org = await _org(db_session)
    with pytest.raises(IntegrityError):
        # A NOT NULL violation is not the idempotency race (SQLite does not
        # enforce FKs in the harness, so `kind` is the violation that reaches
        # the database on every dialect).
        await jobs.enqueue(
            db_session,
            None,  # type: ignore[arg-type]
            {},
            org_id=org,
            idempotency_key=None,
        )
    await db_session.rollback()


@pytest.mark.asyncio
async def test_be004_a_repeated_idempotency_key_answers_200_deduplicated_never_201(
    auth_client, db_session
):
    """BE-004 (audit 2026-09-05): the unique index on (org, kind, key) is
    unconditional while the pre-check looks at LIVE rows only, so an enqueue
    that repeats a finished job's key returns that finished job — and the
    route answered `201 Created` for it, telling the client something had
    been scheduled when nothing had. 201 now means a row was created; every
    dedupe (live or finished) is 200 with `deduplicated: true`."""
    first = await auth_client.post(
        "/api/v1/jobs", json={"kind": "dunning.run", "idempotency_key": "nightly-2026-09"}
    )
    assert first.status_code == 201, first.text
    assert first.json()["deduplicated"] is False
    job_id = first.json()["id"]

    # Live duplicate: same key while the job is queued.
    again = await auth_client.post(
        "/api/v1/jobs", json={"kind": "dunning.run", "idempotency_key": "nightly-2026-09"}
    )
    assert again.status_code == 200, again.text
    assert again.json()["id"] == job_id
    assert again.json()["deduplicated"] is True

    # Finished duplicate: the job ran; the same key still names the same work.
    from sqlalchemy import update

    from app.models import job as jobmodel
    from app.models.job import Job

    await db_session.execute(update(Job).where(Job.id == job_id).values(status=jobmodel.SUCCEEDED))
    await db_session.commit()
    done = await auth_client.post(
        "/api/v1/jobs", json={"kind": "dunning.run", "idempotency_key": "nightly-2026-09"}
    )
    assert done.status_code == 200, done.text
    assert done.json()["id"] == job_id
    assert done.json()["status"] == "succeeded"
    assert done.json()["deduplicated"] is True

    # A new key is a new unit of work: 201, new row, not deduplicated.
    fresh = await auth_client.post(
        "/api/v1/jobs", json={"kind": "dunning.run", "idempotency_key": "nightly-2026-10"}
    )
    assert fresh.status_code == 201, fresh.text
    assert fresh.json()["id"] != job_id
    assert fresh.json()["deduplicated"] is False


# ---------------------------------------------------------------------------
# Reference integration 2026-09-07.
#   STIR-P1-01 (Stirling-PDF)  the worker renews the lease of a job it is still
#                              running, so a slow job is not reclaimed as dead.
#   PAT-004    (Scrapling)     a Retry-After hint lengthens, never shortens, backoff.
#   PAT-030    (Paperless-ngx) handler log lines carry job id + kind; reset after.


@pytest.mark.asyncio
async def test_renew_lease_keeps_owned_job_live(auth_client, db_session, _db, monkeypatch):
    org = await _org(db_session)
    await jobs.enqueue(db_session, "recurring.generate", {}, org_id=org)
    claimed = await jobs.claim(db_session, "live-worker")
    assert claimed is not None

    # Age the lease past the stale cutoff — the exact state a long OCR reaches.
    old = datetime.now(UTC) - timedelta(seconds=jobs.STALE_LEASE_SECONDS + 60)
    claimed.locked_at = old
    await db_session.commit()

    import app.core.database as database

    monkeypatch.setattr(database, "SessionLocal", _db)

    assert await jobs._renew_lease(jobs._Lease.from_job(claimed)) is True
    await db_session.refresh(claimed)
    assert claimed.locked_at is not None
    assert claimed.locked_at.replace(tzinfo=UTC) > old  # SQLite hands back naive UTC
    assert claimed.locked_by == "live-worker"
    assert claimed.status == jobmodel.RUNNING
    # …and the reclaim sweep now leaves it alone.
    assert await jobs.reclaim_stale(db_session, now=datetime.now(UTC)) == 0


@pytest.mark.asyncio
async def test_renew_lease_does_not_retake_a_lease_another_worker_now_holds(
    auth_client, db_session, _db, monkeypatch
):
    """The renewal predicate is id + RUNNING + locked_by == us. Once the sweep
    reclaimed the job and a second worker claimed it, the first worker's
    heartbeat must report False — silently re-taking it would put two
    workers on one job."""
    org = await _org(db_session)
    await jobs.enqueue(db_session, "recurring.generate", {}, org_id=org)
    first = await jobs.claim(db_session, "worker-a")
    assert first is not None
    original_lease = jobs._Lease.from_job(first)
    first.locked_at = datetime.now(UTC) - timedelta(seconds=jobs.STALE_LEASE_SECONDS + 60)
    await db_session.commit()
    assert await jobs.reclaim_stale(db_session) == 1
    # The reclaim backs the job off (30 s); claim it as the next worker would.
    second = await jobs.claim(db_session, "worker-b", now=datetime.now(UTC) + timedelta(seconds=60))
    assert second is not None and second.id == first.id

    import app.core.database as database

    monkeypatch.setattr(database, "SessionLocal", _db)
    assert await jobs._renew_lease(original_lease) is False
    await db_session.refresh(second)
    assert second.locked_by == "worker-b"


@pytest.mark.asyncio
async def test_run_once_heartbeats_while_handler_is_alive(auth_client, db_session, monkeypatch):
    org = await _org(db_session)
    heartbeat_seen = asyncio.Event()
    release = asyncio.Event()

    @jobs.handler("test.long")
    async def _long(db, payload, job):
        await release.wait()
        return {"ok": True}

    async def _fake_renew(lease):
        heartbeat_seen.set()
        return True

    monkeypatch.setattr(jobs, "_renew_lease", _fake_renew)
    monkeypatch.setattr(jobs, "LEASE_HEARTBEAT_SECONDS", 0.01)

    await jobs.enqueue(db_session, "test.long", {}, org_id=org)
    task = asyncio.create_task(jobs.run_once(db_session, "live-worker"))
    await asyncio.wait_for(heartbeat_seen.wait(), timeout=1.0)
    release.set()
    job = await task
    assert job is not None
    assert job.status == jobmodel.SUCCEEDED


@pytest.mark.asyncio
async def test_heartbeat_stops_when_handler_fails_and_a_failed_renewal_does_not_break_the_job(
    auth_client, db_session, monkeypatch
):
    org = await _org(db_session)
    calls = []

    @jobs.handler("test.slow-fail")
    async def _slow_fail(db, payload, job):
        await asyncio.sleep(0.05)
        raise RuntimeError("boom")

    async def _broken_renew(lease):
        calls.append(lease.job_id)
        raise ConnectionError("db went away")

    monkeypatch.setattr(jobs, "_renew_lease", _broken_renew)
    monkeypatch.setattr(jobs, "LEASE_HEARTBEAT_SECONDS", 0.01)
    await jobs.enqueue(db_session, "test.slow-fail", {}, org_id=org, max_attempts=3)
    job = await jobs.run_once(db_session, "w1")
    assert job is not None
    assert job.status == jobmodel.QUEUED  # ordinary retry path, heartbeat errors swallowed
    assert calls  # the heartbeat ran and failed, without affecting the outcome


@pytest.mark.asyncio
async def test_heartbeat_task_inherits_the_job_tenant_and_log_context(
    auth_client, db_session, monkeypatch
):
    from app.core.observability import job_id_ctx, job_kind_ctx
    from app.core.tenant import get_current_org

    org = await _org(db_session)
    seen = {}
    release = asyncio.Event()

    @jobs.handler("test.ctx-hb")
    async def _h(db, payload, job):
        await release.wait()
        return {}

    async def _renew(lease):
        seen.update(org=get_current_org(), job_id=job_id_ctx.get(), kind=job_kind_ctx.get())
        release.set()
        return True

    monkeypatch.setattr(jobs, "_renew_lease", _renew)
    monkeypatch.setattr(jobs, "LEASE_HEARTBEAT_SECONDS", 0.01)
    queued = await jobs.enqueue(db_session, "test.ctx-hb", {}, org_id=org)
    await asyncio.wait_for(jobs.run_once(db_session, "w1"), timeout=2.0)
    assert seen == {"org": org, "job_id": queued.id, "kind": "test.ctx-hb"}


@pytest.mark.asyncio
@pytest.mark.parametrize(("hint", "expected_delay"), [(5.0, 30.0), (120.0, 120.0)])
async def test_retry_after_can_lengthen_but_never_shorten_backoff(
    auth_client, db_session, hint, expected_delay
):
    org = await _org(db_session)

    @jobs.handler("test.retry-after")
    async def _retry_after(db, payload, job):
        raise jobs.RetryAfterError("receiver asked us to wait", retry_after_seconds=hint)

    now = datetime(2026, 9, 6, 16, 0, tzinfo=UTC)
    await jobs.enqueue(
        db_session, "test.retry-after", {}, org_id=org, max_attempts=2, run_after=now
    )
    job = await jobs.run_once(db_session, "w1", now=now)
    assert job is not None
    assert job.status == jobmodel.QUEUED
    assert job.run_after == now + timedelta(seconds=expected_delay)


@pytest.mark.asyncio
async def test_retry_after_on_the_last_attempt_still_dead_letters(auth_client, db_session):
    org = await _org(db_session)

    @jobs.handler("test.retry-after-dead")
    async def _h(db, payload, job):
        raise jobs.RetryAfterError("wait", retry_after_seconds=3600)

    await jobs.enqueue(db_session, "test.retry-after-dead", {}, org_id=org, max_attempts=1)
    job = await jobs.run_once(db_session, "w1")
    assert job is not None and job.status == jobmodel.DEAD


def test_retry_after_error_rejects_a_negative_or_non_finite_hint():
    for bad in (-1, float("inf"), float("nan")):
        with pytest.raises(ValueError):
            jobs.RetryAfterError("x", retry_after_seconds=bad)


@pytest.mark.asyncio
@pytest.mark.parametrize("hint", [1e300, 315_360_000.0, jobs.RETRY_AFTER_CAP_SECONDS + 1])
async def test_retry_after_is_capped_so_a_receiver_cannot_park_a_delivery(
    auth_client, db_session, hint
):
    """Review finding S-5 (reference batch R1): an unbounded hint either overflowed
    the datetime before commit — leaving a phantom RUNNING lease — or parked the
    job for a decade outside every gauge. The cap makes the worst a receiver can
    do one day."""
    org = await _org(db_session)

    @jobs.handler("test.retry-after-huge")
    async def _h(db, payload, job):
        raise jobs.RetryAfterError("wait", retry_after_seconds=hint)

    now = datetime(2026, 9, 6, 16, 0, tzinfo=UTC)
    await jobs.enqueue(
        db_session, "test.retry-after-huge", {}, org_id=org, max_attempts=3, run_after=now
    )
    job = await jobs.run_once(db_session, "w1", now=now)
    assert job is not None and job.status == jobmodel.QUEUED
    assert job.run_after == now + timedelta(seconds=jobs.RETRY_AFTER_CAP_SECONDS)
    assert job.locked_by is None  # no phantom lease


def test_heartbeat_constants_keep_the_lease_alive_and_the_worker_free():
    """The renewal period must sit well inside the stale cutoff (otherwise a live
    job is reclaimed between two renewals) and the join bound well inside the
    period (otherwise a hung renewal holds the sequential worker)."""
    assert jobs.LEASE_HEARTBEAT_SECONDS * 2 <= jobs.STALE_LEASE_SECONDS
    assert jobs.HEARTBEAT_JOIN_SECONDS < jobs.LEASE_HEARTBEAT_SECONDS
    assert jobs.RETRY_AFTER_CAP_SECONDS >= jobs._BACKOFF_CAP_SECONDS


@pytest.mark.asyncio
async def test_a_hung_renewal_does_not_hold_the_worker_after_the_handler_returns(
    auth_client, db_session, monkeypatch
):
    """Review finding A-1: `stop.set()` cannot interrupt a renewal stuck on a
    half-open connection; `run_once` waits at most HEARTBEAT_JOIN_SECONDS, then
    cancels it, and the job outcome is unaffected."""
    org = await _org(db_session)
    started = asyncio.Event()

    @jobs.handler("test.hung-renewal")
    async def _h(db, payload, job):
        await started.wait()  # let the heartbeat get stuck first
        return {"ok": True}

    async def _stuck(lease):
        started.set()
        await asyncio.sleep(3600)  # a connection that never answers
        return True

    monkeypatch.setattr(jobs, "_renew_lease", _stuck)
    monkeypatch.setattr(jobs, "LEASE_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(jobs, "HEARTBEAT_JOIN_SECONDS", 0.05)
    await jobs.enqueue(db_session, "test.hung-renewal", {}, org_id=org)
    job = await asyncio.wait_for(jobs.run_once(db_session, "w1"), timeout=2.0)
    assert job is not None and job.status == jobmodel.SUCCEEDED


@pytest.mark.asyncio
async def test_a_terminal_job_cannot_be_renewed_or_resurrected(
    auth_client, db_session, _db, monkeypatch
):
    org = await _org(db_session)
    await jobs.enqueue(db_session, "recurring.generate", {}, org_id=org)
    claimed = await jobs.claim(db_session, "w1")
    assert claimed is not None
    original_lease = jobs._Lease.from_job(claimed)
    await jobs._complete(db_session, claimed, {})
    import app.core.database as database

    monkeypatch.setattr(database, "SessionLocal", _db)
    assert await jobs._renew_lease(original_lease) is False
    await db_session.refresh(claimed)
    assert claimed.status == jobmodel.SUCCEEDED and claimed.locked_at is None


@pytest.mark.asyncio
async def test_job_log_context_is_reset_after_a_failing_handler_too(auth_client, db_session):
    from app.core.observability import job_id_ctx, job_kind_ctx

    org = await _org(db_session)

    @jobs.handler("test.context-fail")
    async def _h(db, payload, job):
        raise RuntimeError("boom")

    await jobs.enqueue(db_session, "test.context-fail", {}, org_id=org)
    job = await jobs.run_once(db_session, "w1")
    assert job is not None and job.status == jobmodel.QUEUED
    assert job_id_ctx.get() is None and job_kind_ctx.get() is None


@pytest.mark.asyncio
async def test_handler_receives_job_log_context_and_context_is_reset(auth_client, db_session):
    from app.core.observability import job_id_ctx, job_kind_ctx

    org = await _org(db_session)
    seen = {}

    @jobs.handler("test.context")
    async def _context(db, payload, job):
        seen["job_id"] = job_id_ctx.get()
        seen["job_kind"] = job_kind_ctx.get()
        return {}

    queued = await jobs.enqueue(db_session, "test.context", {}, org_id=org)
    await jobs.run_once(db_session, "w1")
    assert seen == {"job_id": queued.id, "job_kind": "test.context"}
    assert job_id_ctx.get() is None
    assert job_kind_ctx.get() is None


@pytest.mark.asyncio
async def test_json_log_line_inside_a_handler_carries_job_id_and_kind(auth_client, db_session):
    import json as _json
    import logging

    from app.core.observability import _JsonFormatter

    org = await _org(db_session)
    lines: list[str] = []

    class _Sink(logging.Handler):
        def emit(self, record):
            lines.append(_JsonFormatter().format(record))

    sink = _Sink()
    target = logging.getLogger("invoiceiq.test-handler")
    target.addHandler(sink)
    try:

        @jobs.handler("test.logline")
        async def _h(db, payload, job):
            target.warning("cargo manifest page 3 unreadable")
            return {}

        queued = await jobs.enqueue(db_session, "test.logline", {}, org_id=org)
        await jobs.run_once(db_session, "w1")
    finally:
        target.removeHandler(sink)
    payload = _json.loads(lines[-1])
    assert payload["job_id"] == queued.id and payload["job_kind"] == "test.logline"
    # Outside a job the keys are absent, not null.
    outside = _json.loads(
        _JsonFormatter().format(logging.LogRecord("x", 20, "f", 1, "m", (), None))
    )
    assert "job_id" not in outside and "job_kind" not in outside


@pytest.mark.asyncio
async def test_permanent_job_error_dead_letters_on_the_first_attempt(auth_client, db_session):
    """BILL-REL-001: a precondition the job encodes no longer holds — retrying a
    certainty five times with backoff only delays the dead-letter signal."""
    org = await _org(db_session)

    @jobs.handler("test.permanent")
    async def _h(db, payload, job):
        raise jobs.PermanentJobError("customer no longer resolves to this tenant")

    await jobs.enqueue(db_session, "test.permanent", {}, org_id=org, max_attempts=5)
    job = await jobs.run_once(db_session, "w1")
    assert job is not None
    assert job.status == jobmodel.DEAD and job.attempts == 1
    assert "PermanentJobError" in (job.last_error or "")
    assert job.locked_by is None
