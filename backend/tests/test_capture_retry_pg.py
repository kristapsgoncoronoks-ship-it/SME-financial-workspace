"""Capture attempts publish once, atomically, across real PostgreSQL sessions."""

import asyncio
import json
import os
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.database import get_session
from app.core.tenant import reset_current_org, set_current_org
from app.main import app
from app.models.audit import AuditEvent
from app.models.capture_field_memory import CaptureFieldMemory
from app.models.extraction_field import ExtractionField
from app.models.extraction_run import ExtractionRun
from app.models.job import Job
from app.services import capture_memory, capture_progress, extraction, image_ocr, jobs

PG_URL = os.environ.get("RLS_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not PG_URL, reason="requires migrated non-superuser PostgreSQL")
CSV = (
    b"description,quantity,unit_price,tax_rate,vendor,invoice_number,issue_date\n"
    b"Synthetic attempt,1,100,21,Synthetic Vendor,SYNTHETIC-ATTEMPT,2026-10-18\n"
)


@pytest_asyncio.fixture
async def capture():
    engine = create_async_engine(PG_URL, pool_size=6, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    async def session():
        async with factory() as db:
            yield db

    overrides = dict(app.dependency_overrides)
    app.dependency_overrides[get_session] = session
    try:
        async with factory() as db:
            assert not await db.scalar(
                text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user")
            )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            registered = await client.post(
                "/api/v1/auth/register",
                json={
                    "organization_name": "Synthetic attempt isolation",
                    "name": "Synthetic Owner",
                    "email": f"attempt-{uuid.uuid4().hex}@example.com",
                    "password": "Synthetic-Attempt-PG-Password-32-Only",
                },
            )
            assert registered.status_code == 201
            client.headers["Authorization"] = "Bearer " + registered.json()["token"]["access_token"]
            org_id = (await client.get("/api/v1/auth/me")).json()["organization"]["id"]
            accepted = await client.post(
                "/api/v1/invoices/upload", files={"file": ("attempt.csv", CSV, "text/csv")}
            )
            assert accepted.status_code == 202
            run_id = accepted.json()["extraction_run_id"]
            token = set_current_org(org_id)
            try:
                yield client, factory, org_id, run_id
            finally:
                reset_current_org(token)
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(overrides)
        await engine.dispose()


async def work(factory, name="synthetic-attempt-worker"):
    async with factory() as db:
        return await jobs.run_once(db, name, kinds=(extraction.UPLOAD_EXTRACT_KIND,))


async def snapshot(factory, run_id):
    async with factory() as db:
        run = await db.get(ExtractionRun, run_id)
        count = await db.scalar(
            select(func.count())
            .select_from(ExtractionField)
            .where(ExtractionField.extraction_run_id == run_id)
        )
        return run, count


async def wait_for_capture_lock(factory, pending):
    """Prove the request reached PostgreSQL, without a timing-based assertion."""
    async with asyncio.timeout(8):
        async with factory() as observer:
            while not pending.done():
                waiting = await observer.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname=current_database() AND pid<>pg_backend_pid() "
                        "AND wait_event_type='Lock' AND query LIKE '%extraction_runs%'"
                    )
                )
                if waiting:
                    return
                # pg_stat_activity snapshots are transaction-local; refresh it.
                await observer.rollback()
                await asyncio.sleep(0.01)
    raise AssertionError("Request did not wait for the capture publication/review lock")


@pytest.mark.asyncio
@pytest.mark.parametrize("abort", [False, True])
async def test_result_and_provenance_publish_together(capture, monkeypatch, abort):
    client, factory, _, run_id = capture
    reached, release = asyncio.Event(), asyncio.Event()
    original = extraction.record_fields

    async def held_fields(*args, **kwargs):
        reached.set()
        await asyncio.wait_for(release.wait(), timeout=10)
        await original(*args, **kwargs)
        if abort:
            raise RuntimeError("Synthetic provenance transaction failure")

    monkeypatch.setattr(extraction, "record_fields", held_fields)
    worker = asyncio.create_task(work(factory))
    try:
        await asyncio.wait_for(reached.wait(), timeout=10)
        run, count = await snapshot(factory, run_id)
        assert run.status == "queued", "A reviewable draft escaped before its evidence committed"
        assert run.draft_json is None and count == 0
    finally:
        release.set()
        job = await worker
    monkeypatch.setattr(extraction, "record_fields", original)
    run, count = await snapshot(factory, run_id)
    if abort:
        assert run.status == "queued" and run.draft_json is None and count == 0
        assert job.status == "queued"
        async with factory() as db:
            job = await db.get(Job, job.id)
            job.run_after = jobs._now()
            await db.commit()
        assert (await work(factory)).status == "succeeded"
    else:
        assert job.status == "succeeded"
    run, count = await snapshot(factory, run_id)
    assert run.status == "parsed" and count == len(json.loads(run.draft_json)["fields"])
    assert (await client.get(f"/api/v1/invoices/upload/{run_id}")).json()["draft"]


@pytest.mark.asyncio
async def test_simultaneous_retry_clicks_share_one_pending_attempt(capture):
    client, factory, _, run_id = capture
    assert (await work(factory)).status == "succeeded"
    responses = await asyncio.gather(
        client.post(f"/api/v1/invoices/upload/{run_id}/retry"),
        client.post(f"/api/v1/invoices/upload/{run_id}/retry"),
    )
    assert [r.status_code for r in responses] == [202, 202]
    async with factory() as db:
        pending = list(await db.scalars(select(Job).where(Job.status == "queued")))
        assert len(pending) == 1, "Double click created competing extraction intents"
        assert pending[0].idempotency_key is not None
    assert (await snapshot(factory, run_id))[1] == 0
    assert (await work(factory)).status == "succeeded"
    run, count = await snapshot(factory, run_id)
    assert run.status == "parsed" and count == len(json.loads(run.draft_json)["fields"])


@pytest.mark.asyncio
@pytest.mark.parametrize("successor", ["queued", "reviewed", "saved", "failed"])
@pytest.mark.parametrize("old_failure", [False, True])
async def test_old_worker_cannot_publish_after_new_attempt(
    capture, monkeypatch, successor, old_failure
):
    """Two persisted jobs mimic a duplicate delivery/reclaimed execution.

    Pause the original parser, let its duplicate finish, then retry. The old
    worker must not change ANY newer state, including a correction or saved link.
    Only the persistence interleaving is injected; parsing and jobs are native.
    """
    client, factory, org_id, run_id = capture
    reached, release = asyncio.Event(), asyncio.Event()
    original = image_ocr.run_reading
    calls = 0

    async def held_parse(*args, **kwargs):
        nonlocal calls
        calls += 1
        call = calls
        draft = await original(*args, **kwargs)
        if call == 1:
            reached.set()
            await asyncio.wait_for(release.wait(), timeout=15)
            await asyncio.to_thread(
                capture_progress.report, capture_progress.OCR, pages_done=99, pages_total=100
            )
            if old_failure:
                raise ValueError("Synthetic old parser failure")
            draft.draft.vendor_name = "Synthetic stale vendor"
            draft.warnings = ["Synthetic stale warning"]
        elif call == 3 and successor == "failed":
            raise ValueError("Synthetic current parser failure")
        return draft

    monkeypatch.setattr(image_ocr, "run_reading", held_parse)
    old = asyncio.create_task(work(factory, "synthetic-old-execution"))
    try:
        await asyncio.wait_for(reached.wait(), timeout=10)
        async with factory() as db:
            await jobs.enqueue(
                db, extraction.UPLOAD_EXTRACT_KIND, {"run_id": run_id}, org_id=org_id
            )
        assert (await work(factory, "synthetic-duplicate-execution")).status == "succeeded"
        assert (await client.post(f"/api/v1/invoices/upload/{run_id}/retry")).status_code == 202
        if successor != "queued":
            assert (await work(factory, "synthetic-current-execution")).status == "succeeded"
        if successor == "reviewed":
            response = await client.post(
                f"/api/v1/invoices/captures/{run_id}/review",
                json={"fields": [{"field": "vendor_name", "reviewed_value": "Synthetic reviewed"}]},
            )
            assert response.status_code == 200
        if successor == "saved":
            body = (await client.get(f"/api/v1/invoices/upload/{run_id}")).json()["draft"]["draft"]
            assert (await client.post("/api/v1/invoices", json=body)).status_code == 201
        before, before_count = await snapshot(factory, run_id)
        async with factory() as db:
            observed_before = list(
                await db.scalars(
                    select(CaptureFieldMemory.observed_count).order_by(CaptureFieldMemory.field)
                )
            )
        release.set()
        result = await old
        assert json.loads(result.result_json)["skipped"] == "attempt superseded or complete"
        after, after_count = await snapshot(factory, run_id)
        for attr in (
            "status",
            "attempt_seq",
            "draft_json",
            "preview_json",
            "method",
            "failure_code",
            "failure_seq",
            "note",
            "invoice_id",
            "stage",
            "pages_done",
            "pages_total",
            "field_count",
            "warning_count",
        ):
            assert getattr(after, attr) == getattr(before, attr), attr
        assert after_count == before_count
        async with factory() as db:
            assert observed_before == list(
                await db.scalars(
                    select(CaptureFieldMemory.observed_count).order_by(CaptureFieldMemory.field)
                )
            )
            if successor == "reviewed":
                assert (
                    await db.scalar(
                        select(ExtractionField.reviewed_value).where(
                            ExtractionField.field == "vendor_name"
                        )
                    )
                    == "Synthetic reviewed"
                )
    finally:
        release.set()
        await old


@pytest.mark.asyncio
async def test_review_and_retry_serialize_before_discard_check(capture, monkeypatch):
    client, factory, _, run_id = capture
    await work(factory)
    reached, release = asyncio.Event(), asyncio.Event()
    original = capture_memory.record_correction

    async def held_correction(*args, **kwargs):
        await original(*args, **kwargs)
        reached.set()
        await asyncio.wait_for(release.wait(), timeout=10)

    monkeypatch.setattr(capture_memory, "record_correction", held_correction)
    review = asyncio.create_task(
        client.post(
            f"/api/v1/invoices/captures/{run_id}/review",
            json={"fields": [{"field": "vendor_name", "reviewed_value": "Synthetic correction"}]},
        )
    )
    retry = None
    try:
        await asyncio.wait_for(reached.wait(), timeout=10)
        retry = asyncio.create_task(client.post(f"/api/v1/invoices/upload/{run_id}/retry"))
        await wait_for_capture_lock(factory, retry)
    finally:
        release.set()
        assert (await review).status_code == 200
    response = await retry
    assert response.status_code == 409 and response.json()["code"] == "capture_has_review"
    async with factory() as db:
        assert (
            await db.scalar(
                select(ExtractionField.reviewed_value).where(ExtractionField.field == "vendor_name")
            )
            == "Synthetic correction"
        )
        assert (
            await db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "capture.review_discarded")
            )
            == 0
        )


@pytest.mark.asyncio
async def test_retry_enqueue_failure_rolls_back_reset_and_explicit_discard(capture, monkeypatch):
    client, factory, _, run_id = capture
    await work(factory)
    await client.post(
        f"/api/v1/invoices/captures/{run_id}/review",
        json={"fields": [{"field": "vendor_name", "reviewed_value": "Synthetic correction"}]},
    )
    before, count = await snapshot(factory, run_id)

    async def refuse_enqueue(*args, **kwargs):
        raise RuntimeError("Synthetic job insert failure")

    monkeypatch.setattr(jobs, "enqueue", refuse_enqueue)
    with pytest.raises(RuntimeError, match="Synthetic job insert"):
        await client.post(f"/api/v1/invoices/upload/{run_id}/retry?discard_review=true")
    after, after_count = await snapshot(factory, run_id)
    assert after_count == count and after.draft_json == before.draft_json
    assert after.status == "parsed" and after.attempt_seq == before.attempt_seq
    async with factory() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "capture.review_discarded")
            )
            == 0
        )


@pytest.mark.asyncio
async def test_legacy_job_does_not_adopt_current_retry(capture):
    from datetime import timedelta

    client, factory, org_id, run_id = capture
    await work(factory)
    await client.post(f"/api/v1/invoices/upload/{run_id}/retry")
    async with factory() as db:
        await jobs.enqueue(
            db,
            extraction.UPLOAD_EXTRACT_KIND,
            {"run_id": run_id},
            org_id=org_id,
            run_after=jobs._now() - timedelta(days=1),
        )
    old = await work(factory)
    assert "skipped" in json.loads(old.result_json)
    run, count = await snapshot(factory, run_id)
    assert run.status == "queued" and count == 0 and run.attempt_seq == 1
    assert (await work(factory)).status == "succeeded"


@pytest.mark.asyncio
async def test_dead_native_job_can_be_retried_without_reupload(capture):
    client, factory, _, run_id = capture
    async with factory() as db:
        job = await db.scalar(select(Job))
        job.status = "dead"
        await db.commit()
    assert (await client.post(f"/api/v1/invoices/upload/{run_id}/retry")).status_code == 202
    assert (await work(factory)).status == "succeeded"
    run, count = await snapshot(factory, run_id)
    assert run.status == "parsed" and run.attempt_seq == 1 and count > 0


@pytest.mark.asyncio
async def test_queued_review_and_save_refuse_without_creating_invoice(capture):
    from app.models.invoice import Invoice

    client, factory, _, run_id = capture
    await work(factory)
    draft = (await client.get(f"/api/v1/invoices/upload/{run_id}")).json()["draft"]["draft"]
    await client.post(f"/api/v1/invoices/upload/{run_id}/retry")
    review = await client.post(
        f"/api/v1/invoices/captures/{run_id}/review",
        json={"fields": [{"field": "vendor_name", "reviewed_value": "Synthetic stale review"}]},
    )
    assert review.status_code == 409 and review.json()["code"] == "capture_not_ready"
    saved = await client.post("/api/v1/invoices", json=draft)
    assert saved.status_code == 409 and saved.json()["code"] == "capture_not_ready"
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(Invoice)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["retry", "review"])
async def test_membership_revoked_while_capture_lock_was_waiting(capture, monkeypatch, action):
    from sqlalchemy import delete

    from app.models.membership import Membership

    client, factory, _, run_id = capture
    await work(factory)
    reached, release = asyncio.Event(), asyncio.Event()
    original = extraction.get_capture

    async def held_lock(*args, **kwargs):
        result = await original(*args, **kwargs)
        if kwargs.get("for_update"):
            reached.set()
            await asyncio.wait_for(release.wait(), timeout=10)
        return result

    monkeypatch.setattr(extraction, "get_capture", held_lock)
    before, count = await snapshot(factory, run_id)
    if action == "retry":
        request = asyncio.create_task(client.post(f"/api/v1/invoices/upload/{run_id}/retry"))
    else:
        request = asyncio.create_task(
            client.post(
                f"/api/v1/invoices/captures/{run_id}/review",
                json={"fields": [{"field": "vendor_name", "reviewed_value": "Synthetic revoked"}]},
            )
        )
    try:
        await asyncio.wait_for(reached.wait(), timeout=10)
        async with factory() as db:
            await db.execute(delete(Membership))
            await db.commit()
    finally:
        release.set()
    assert (await request).status_code == 401
    after, after_count = await snapshot(factory, run_id)
    assert after.status == before.status and after.attempt_seq == before.attempt_seq
    assert after_count == count and after.draft_json == before.draft_json


@pytest.mark.asyncio
async def test_retry_waits_for_inflight_provenance_then_clears_one_complete_result(
    capture, monkeypatch
):
    client, factory, _, run_id = capture
    reached, release = asyncio.Event(), asyncio.Event()
    original = extraction.record_fields

    async def held_fields(*args, **kwargs):
        reached.set()
        await asyncio.wait_for(release.wait(), timeout=10)
        await original(*args, **kwargs)

    monkeypatch.setattr(extraction, "record_fields", held_fields)
    old = asyncio.create_task(work(factory))
    retry = None
    try:
        await asyncio.wait_for(reached.wait(), timeout=10)
        retry = asyncio.create_task(client.post(f"/api/v1/invoices/upload/{run_id}/retry"))
        await wait_for_capture_lock(factory, retry)
    finally:
        release.set()
        assert (await old).status == "succeeded"
    assert (await retry).status_code == 202
    run, count = await snapshot(factory, run_id)
    assert run.status == "queued" and count == 0 and run.attempt_seq == 1
    monkeypatch.setattr(extraction, "record_fields", original)
    assert (await work(factory)).status == "succeeded"
    run, count = await snapshot(factory, run_id)
    assert count == len(json.loads(run.draft_json)["fields"])


@pytest.mark.asyncio
async def test_reused_worker_session_reads_the_new_generation(capture):
    client, factory, _, run_id = capture
    async with factory() as db:
        pinned = await db.get(ExtractionRun, run_id)
        assert (await jobs.run_once(db, "synthetic-reused-worker")).status == "succeeded"
        response = await client.post(f"/api/v1/invoices/upload/{run_id}/retry")
        assert response.status_code == 202
        # Keep the earlier ORM row strongly referenced in this worker's map.
        assert pinned.attempt_seq == 0
        assert (await jobs.run_once(db, "synthetic-reused-worker")).status == "succeeded"
        await db.refresh(pinned)
        assert pinned.status == "parsed" and pinned.attempt_seq == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_code", ["stored_file_too_large", "stored_file_changed"])
async def test_delayed_stored_read_failure_cannot_replace_a_newer_attempt(
    capture, monkeypatch, failure_code
):
    from app.services import capture_failures, documents

    client, factory, org_id, run_id = capture
    reached, release = asyncio.Event(), asyncio.Event()
    original = documents.load_capture
    calls = 0

    async def held_read(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            reached.set()
            await asyncio.wait_for(release.wait(), timeout=15)
            raise capture_failures.CaptureError(failure_code, "Synthetic old read failure")
        return await original(*args, **kwargs)

    monkeypatch.setattr(documents, "load_capture", held_read)
    old = asyncio.create_task(work(factory, "synthetic-old-read"))
    try:
        await asyncio.wait_for(reached.wait(), timeout=10)
        async with factory() as db:
            await jobs.enqueue(
                db, extraction.UPLOAD_EXTRACT_KIND, {"run_id": run_id}, org_id=org_id
            )
        assert (await work(factory, "synthetic-duplicate-read")).status == "succeeded"
        assert (await client.post(f"/api/v1/invoices/upload/{run_id}/retry")).status_code == 202
        assert (await work(factory, "synthetic-current-read")).status == "succeeded"
        before, before_fields = await snapshot(factory, run_id)
        assert before.status == "parsed" and before.attempt_seq == 1
        release.set()
        result = await old
        assert json.loads(result.result_json)["skipped"] == "attempt superseded or complete"
        after, after_fields = await snapshot(factory, run_id)
        for attr in (
            "status",
            "attempt_seq",
            "draft_json",
            "failure_code",
            "failure_seq",
            "note",
            "stage",
        ):
            assert getattr(after, attr) == getattr(before, attr), attr
        assert after_fields == before_fields
    finally:
        release.set()
        await old
