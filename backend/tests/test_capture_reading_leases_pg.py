"""Real claims/row waits fence CAP state, history and separate progress writes."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text

from app.api.routes import email as email_routes
from app.models.capture_reading import CaptureReadingObservation
from app.models.email_intake import InboundInvoice
from app.models.extraction_run import ExtractionRun
from app.models.job import Job
from app.services import (
    capture_failures,
    capture_progress,
    email_intake,
    extraction,
    image_ocr,
    jobs,
)
from tests.test_image_reading_metadata_pg import reading_pg as reading_pg
from tests.test_image_reading_metadata_pg import scoped

pytestmark = pytest.mark.asyncio
CSV = b"description,amount\nSynthetic lease row,10.00\n"


async def seed(reading_pg, channel):
    client, factory, org_id = reading_pg
    if channel == "upload":
        response = await client.post(
            "/api/v1/invoices/upload", files={"file": ("lease.csv", CSV, "text/csv")}
        )
        assert response.status_code == 202, response.text
        return response.json()["extraction_run_id"], extraction.UPLOAD_EXTRACT_KIND, ExtractionRun
    async with scoped(factory, org_id) as db:
        row = await email_intake.process_attachment(
            db,
            org_id,
            from_addr="synthetic@example.com",
            subject="Synthetic lease proof",
            filename="lease.csv",
            content_type="text/csv",
            content=CSV,
        )
        await db.commit()
        assert row.status == "queued"
        return row.id, email_intake.EXTRACT_KIND, InboundInvoice


async def successor(db, now, kind, reuse):
    later = now + timedelta(seconds=jobs.STALE_LEASE_SECONDS + 1)
    assert await jobs.reclaim_stale(db, now=later) == 1
    worker = "old-capture-worker" if reuse else "successor-worker"
    if reuse:
        row = await db.scalar(select(Job).where(Job.kind == kind))
        await jobs.retry(db, row, now=later)
    claimed = await jobs.claim(db, worker, kinds=(kind,), now=later + timedelta(minutes=10))
    assert claimed is not None
    return claimed


@pytest.mark.parametrize("channel", ["upload", "inbound"])
@pytest.mark.parametrize("failure", [False, True])
@pytest.mark.parametrize("reuse", [False, True])
async def test_old_claim_cannot_commit_owner_or_history(
    reading_pg, monkeypatch, channel, failure, reuse
):
    _, factory, org_id = reading_pg
    owner_id, kind, model = await seed(reading_pg, channel)
    reached, release = asyncio.Event(), asyncio.Event()
    original = image_ocr.run_reading

    async def held(*args):
        draft = await original(*args)
        reached.set()
        await asyncio.wait_for(release.wait(), 15)
        if failure:
            raise capture_failures.CaptureError(
                capture_failures.MALFORMED_DOCUMENT, "Synthetic failed reading"
            )
        return draft

    monkeypatch.setattr(image_ocr, "run_reading", held)
    now = datetime.now(UTC)

    async def work():
        async with scoped(factory, org_id) as db:
            return await jobs.run_once(db, "old-capture-worker", kinds=(kind,), now=now)

    task = asyncio.create_task(work())
    try:
        await asyncio.wait_for(reached.wait(), 10)
        async with scoped(factory, org_id) as db:
            old = await db.scalar(select(Job).where(Job.kind == kind, Job.org_id == org_id))
            job_id, old_token = old.id, old.lease_token
            old_attempts = old.attempts
            new = await successor(db, now, kind, reuse)
            new_token = new.lease_token
            assert new.id == job_id and old_token != new_token
            if reuse:
                assert new.attempts == old_attempts and new.locked_by == "old-capture-worker"
        release.set()
        await asyncio.wait_for(task, 10)
        async with scoped(factory, org_id) as db:
            owner = await db.get(model, owner_id)
            job = await db.get(Job, job_id)
            assert job.status == "running" and job.lease_token == new_token
            assert owner.status == "queued" and owner.draft_json is None
            assert owner.failure_seq == 0 and owner.reading_observation_json is None
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(CaptureReadingObservation)
                    .where(CaptureReadingObservation.org_id == org_id)
                )
                == 0
            )
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_old_claim_cannot_overwrite_successor_progress(reading_pg, monkeypatch):
    _, factory, org_id = reading_pg
    owner_id, kind, model = await seed(reading_pg, "upload")
    reached, release, wrote, finish = (asyncio.Event() for _ in range(4))
    original = image_ocr.run_reading

    async def held(*args):
        draft = await original(*args)
        reached.set()
        await asyncio.wait_for(release.wait(), 15)
        await asyncio.to_thread(capture_progress.report, capture_progress.INTERPRETING)
        wrote.set()
        await asyncio.wait_for(finish.wait(), 15)
        return draft

    monkeypatch.setattr(image_ocr, "run_reading", held)
    now = datetime.now(UTC)

    async def work():
        async with scoped(factory, org_id) as db:
            return await jobs.run_once(db, "old-capture-worker", kinds=(kind,), now=now)

    task = asyncio.create_task(work())
    try:
        await asyncio.wait_for(reached.wait(), 10)
        async with scoped(factory, org_id) as db:
            await successor(db, now, kind, False)
            owner = await db.get(model, owner_id)
            owner.stage, owner.pages_done, owner.pages_total = capture_progress.OCR, 1, 3
            await db.commit()
        release.set()
        await asyncio.wait_for(wrote.wait(), 10)
        async with scoped(factory, org_id) as db:
            owner = await db.get(model, owner_id)
            assert (owner.stage, owner.pages_done, owner.pages_total) == (
                capture_progress.OCR,
                1,
                3,
            )
    finally:
        release.set()
        finish.set()
        await asyncio.gather(task, return_exceptions=True)


async def observed_lock(factory, pid):
    for _ in range(250):
        async with factory() as probe:
            if await probe.scalar(
                text("SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid=:pid"),
                {"pid": pid},
            ):
                return
        await asyncio.sleep(0.02)
    raise AssertionError("Expected a real PostgreSQL row-lock wait")


async def test_guard_rechecks_matching_claim_after_actual_job_row_wait(reading_pg, monkeypatch):
    _, factory, org_id = reading_pg
    owner_id, kind, model = await seed(reading_pg, "upload")
    parsed, release, guarding = (asyncio.Event() for _ in range(3))
    pids = []
    original_read, original_guard = image_ocr.run_reading, jobs.lock_publication_claim

    async def held(*args):
        draft = await original_read(*args)
        parsed.set()
        await asyncio.wait_for(release.wait(), 15)
        return draft

    async def guard(db, claim):
        pids.append(await db.scalar(text("SELECT pg_backend_pid()")))
        guarding.set()
        return await original_guard(db, claim)

    monkeypatch.setattr(image_ocr, "run_reading", held)
    monkeypatch.setattr(jobs, "lock_publication_claim", guard)
    now = datetime.now(UTC)

    async def work():
        async with scoped(factory, org_id) as db:
            return await jobs.run_once(db, "old-capture-worker", kinds=(kind,), now=now)

    task = asyncio.create_task(work())
    try:
        await asyncio.wait_for(parsed.wait(), 10)
        async with scoped(factory, org_id) as blocker:
            await blocker.scalar(
                select(Job).where(Job.org_id == org_id, Job.kind == kind).with_for_update()
            )
            release.set()
            await asyncio.wait_for(guarding.wait(), 10)
            await observed_lock(factory, pids[-1])
            await successor(blocker, now, kind, False)
        await asyncio.wait_for(task, 10)
        async with scoped(factory, org_id) as db:
            owner = await db.get(model, owner_id)
            assert owner.status == "queued" and owner.reading_observation_json is None
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("action", ["discard", "delete", "confirm"])
async def test_job_then_owner_wait_preserves_real_inbound_action(reading_pg, monkeypatch, action):
    client, factory, org_id = reading_pg
    owner_id, kind, model = await seed(reading_pg, "inbound")
    parsed, parse_release, owner_locked, owner_release, guarding = (
        asyncio.Event() for _ in range(5)
    )
    pids, drafts = [], []
    original_read = image_ocr.run_reading
    original_owner, original_guard = email_routes._locked_inbound, jobs.lock_publication_claim

    async def held_read(*args):
        draft = await original_read(*args)
        drafts.append(draft)
        parsed.set()
        await asyncio.wait_for(parse_release.wait(), 15)
        return draft

    async def held_owner(*args):
        row = await original_owner(*args)
        owner_locked.set()
        await asyncio.wait_for(owner_release.wait(), 15)
        return row

    async def guard(db, claim):
        owned = await original_guard(db, claim)
        pids.append(await db.scalar(text("SELECT pg_backend_pid()")))
        guarding.set()
        return owned

    monkeypatch.setattr(image_ocr, "run_reading", held_read)
    monkeypatch.setattr(email_routes, "_locked_inbound", held_owner)
    monkeypatch.setattr(jobs, "lock_publication_claim", guard)

    async def worker():
        async with scoped(factory, org_id) as db:
            return await jobs.run_once(db, "owner-wait-worker", kinds=(kind,))

    work = asyncio.create_task(worker())
    request = None
    try:
        await asyncio.wait_for(parsed.wait(), 10)
        path = f"/api/v1/email/inbox/{owner_id}"
        if action == "delete":
            request = asyncio.create_task(client.delete(path))
        elif action == "confirm":
            reviewed = drafts[0].draft.model_dump(mode="json")
            reviewed["vendor_name"] = "Synthetic reviewed supplier"
            request = asyncio.create_task(client.post(path + "/confirm", json={"draft": reviewed}))
        else:
            request = asyncio.create_task(client.post(path + "/discard"))
        await asyncio.wait_for(owner_locked.wait(), 10)
        parse_release.set()
        await asyncio.wait_for(guarding.wait(), 10)
        await observed_lock(factory, pids[-1])
        owner_release.set()
        response = await asyncio.wait_for(request, 10)
        assert response.status_code == {"delete": 204, "discard": 200, "confirm": 201}[action], (
            response.text
        )
        await asyncio.wait_for(work, 10)
        async with scoped(factory, org_id) as db:
            owner = await db.get(model, owner_id)
            if action == "delete":
                assert owner is None
            else:
                assert owner.status == {"discard": "discarded", "confirm": "confirmed"}[action]
                assert owner.reading_observation_json is None
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(CaptureReadingObservation)
                    .where(CaptureReadingObservation.org_id == org_id)
                )
                == 0
            )
    finally:
        parse_release.set()
        owner_release.set()
        await asyncio.gather(
            work, *([request] if request is not None else []), return_exceptions=True
        )


async def test_real_capture_retry_does_not_reverse_lock_the_publishing_job(reading_pg):
    client, factory, org_id = reading_pg
    owner_id, kind, model = await seed(reading_pg, "upload")
    async with scoped(factory, org_id) as db:
        job = await jobs.claim(db, "publishing-worker", kinds=(kind,))
        claim = jobs.capture_publication_claim(job)
        assert (await extraction.extract_upload(db, owner_id, publication_claim=claim))[
            "status"
        ] == "parsed"
        job_id = job.id
    # Actual domain publication has committed, while the real queue claim still
    # awaits generic completion. Retry must not take this older job lock.
    async with scoped(factory, org_id) as blocker:
        await blocker.scalar(select(Job).where(Job.id == job_id).with_for_update())
        response = await asyncio.wait_for(
            client.post(f"/api/v1/invoices/upload/{owner_id}/retry"), 5
        )
        assert response.status_code == 202, response.text
        await blocker.rollback()
    async with scoped(factory, org_id) as db:
        owner = await db.get(model, owner_id)
        assert owner.status == "queued" and owner.attempt_seq == 1
        assert owner.reading_observation_json is None
        assert (
            await db.scalar(
                select(func.count())
                .select_from(CaptureReadingObservation)
                .where(CaptureReadingObservation.org_id == org_id)
            )
            == 1
        )
