"""Email delivery/review races on separate nonprivileged PostgreSQL connections."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import time
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.database import get_session
from app.core.tenant import reset_current_org, set_current_org
from app.main import app
from app.models.audit import AuditEvent
from app.models.email_intake import InboundInvoice
from app.models.invoice import Invoice
from app.models.job import Job
from app.services import audit, email_intake, image_ocr, jobs, parser

PG_URL = os.environ.get("RLS_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not PG_URL, reason="requires migrated non-superuser PostgreSQL")
REQUEST_NAME = ContextVar("email_test_request_name", default=None)
SECRET = "synthetic-email-concurrency-secret"
CSV = (
    b"vendor,invoice_number,issue_date,description,quantity,unit_price,amount,tax_rate\n"
    b"Synthetic email,EMAIL-RACE-1,2026-06-01,Synthetic row,1,100,100,21\n"
)


@pytest_asyncio.fixture
async def email_pg(monkeypatch):
    from app.core.config import settings

    engine = create_async_engine(PG_URL, pool_size=8, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    pids = {}

    async def session():
        async with factory() as db:
            label = REQUEST_NAME.get() or asyncio.current_task().get_name()

            def record_backend(session, transaction, connection):
                pids[label] = connection.scalar(text("SELECT pg_backend_pid()"))

            event.listen(db.sync_session, "after_begin", record_backend)
            yield db

    overrides = dict(app.dependency_overrides)
    app.dependency_overrides[get_session] = session
    monkeypatch.setattr(settings, "inbound_email_secret", SECRET)
    monkeypatch.setattr(settings, "mailgun_signing_key", SECRET)
    try:
        async with factory() as db:
            assert not await db.scalar(
                text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user")
            )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            reg = await client.post(
                "/api/v1/auth/register",
                json={
                    "organization_name": "Synthetic email concurrency",
                    "name": "Synthetic owner",
                    "email": f"email-races-{uuid.uuid4().hex}@example.com",
                    "password": "Synthetic-Email-Races-Password-32",
                },
            )
            assert reg.status_code == 201, reg.text
            identity = reg.json()
            client.headers["Authorization"] = "Bearer " + identity["token"]["access_token"]
            assert (
                await client.put("/api/v1/modules/email_intake", json={"enabled": True})
            ).status_code == 200
            address = (await client.get("/api/v1/email/settings")).json()["address"]
            yield client, factory, identity["organization"]["id"], address, pids
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(overrides)
        await engine.dispose()


@asynccontextmanager
async def scoped(factory, org_id):
    token = set_current_org(org_id)
    try:
        async with factory() as db:
            yield db
    finally:
        reset_current_org(token)


async def blocking_or_finished(factory, pids, task):
    """Observe only this test's exact request backend, never any same-user lock."""
    for _ in range(500):
        if task.done():
            return
        pid = pids.get(task.get_name())
        if pid is not None:
            async with factory() as observer:
                if await observer.scalar(
                    text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"), {"pid": pid}
                ):
                    return
        await asyncio.sleep(0.01)
    raise AssertionError("Request neither completed nor waited for its exact PostgreSQL lock")


async def delivery(client, address, message_id, *, provider="generic", attachments=2):
    REQUEST_NAME.set(asyncio.current_task().get_name())
    if provider == "generic":
        return await client.post(
            "/api/v1/email/inbound",
            headers={"X-Inbound-Secret": SECRET},
            json={
                "to": address,
                "message_id": message_id,
                "attachments": [
                    {
                        "filename": f"source-{i}.csv",
                        "content_base64": base64.b64encode(CSV).decode(),
                    }
                    for i in range(attachments)
                ],
            },
        )
    timestamp, token = str(int(time.time())), uuid.uuid4().hex
    signature = hmac.new(SECRET.encode(), (timestamp + token).encode(), hashlib.sha256).hexdigest()
    form = {
        "recipient": address,
        "timestamp": timestamp,
        "token": token,
        "signature": signature,
        "attachment-count": str(attachments),
    }
    if message_id is not None:
        form["Message-Id"] = message_id
    return await client.post(
        "/api/v1/email/inbound/mailgun",
        data=form,
        files={
            f"attachment-{i + 1}": (f"source-{i}.csv", CSV, "text/csv") for i in range(attachments)
        },
    )


async def seeded(email_pg):
    client, factory, org_id, address, _ = email_pg
    response = await delivery(client, address, uuid.uuid4().hex, attachments=1)
    assert response.status_code == 200 and response.json()["queued"] == 1, response.text
    async with scoped(factory, org_id) as db:
        return (await db.scalar(select(InboundInvoice).where(InboundInvoice.org_id == org_id))).id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "providers", [("generic", "generic"), ("mailgun", "mailgun"), ("generic", "mailgun")]
)
async def test_concurrent_same_message_commits_one_attachment_set(email_pg, monkeypatch, providers):
    client, factory, org_id, address, pids = email_pg
    started, release = asyncio.Event(), asyncio.Event()
    original = email_intake.process_attachment
    first = True

    async def held(*args, **kwargs):
        nonlocal first
        if first:
            first = False
            started.set()
            await asyncio.wait_for(release.wait(), timeout=15)
        return await original(*args, **kwargs)

    monkeypatch.setattr(email_intake, "process_attachment", held)
    key = uuid.uuid4().hex
    one = asyncio.create_task(
        delivery(client, address, key, provider=providers[0]), name="email-first"
    )
    two = None
    try:
        await asyncio.wait_for(started.wait(), timeout=10)
        two = asyncio.create_task(
            delivery(client, address, key, provider=providers[1]), name="email-second"
        )
        await blocking_or_finished(factory, pids, two)
    finally:
        release.set()
        results = await asyncio.gather(one, *([two] if two else []))
    assert all(r.status_code == 200 for r in results), [r.text for r in results]
    assert sorted(r.json()["queued"] for r in results) == [0, 2]
    assert sorted(r.json()["deduplicated"] for r in results) == [False, True]
    async with scoped(factory, org_id) as db:
        assert await db.scalar(select(func.count()).select_from(InboundInvoice)) == 2
        assert (
            await db.scalar(
                select(func.count()).select_from(Job).where(Job.kind == "email.extract")
            )
            == 2
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["confirm", "discard"])
@pytest.mark.parametrize("parse_failure", [False, True])
async def test_late_worker_cannot_replace_review_decision(
    email_pg, monkeypatch, decision, parse_failure
):
    client, factory, org_id, _, _ = email_pg
    row_id = await seeded(email_pg)
    started, release = asyncio.Event(), asyncio.Event()
    original = image_ocr.run_reading

    async def held_parse(*args, **kwargs):
        started.set()
        await asyncio.wait_for(release.wait(), timeout=15)
        if parse_failure:
            raise ValueError("Synthetic extraction failed after the user decided")
        return await original(*args, **kwargs)

    monkeypatch.setattr(image_ocr, "run_reading", held_parse)

    async def work():
        async with scoped(factory, org_id) as db:
            return await jobs.run_once(db, "synthetic-email-worker", kinds=("email.extract",))

    worker = asyncio.create_task(work())
    try:
        await asyncio.wait_for(started.wait(), timeout=10)
        body = (
            {"draft": parser.parse_invoice_file("source.csv", CSV).draft.model_dump(mode="json")}
            if decision == "confirm"
            else None
        )
        response = await client.post(f"/api/v1/email/inbox/{row_id}/{decision}", json=body)
        assert response.status_code == (201 if decision == "confirm" else 200), response.text
    finally:
        release.set()
        await worker
    async with scoped(factory, org_id) as db:
        row = await db.get(InboundInvoice, row_id)
        assert row.status == ("confirmed" if decision == "confirm" else "discarded")
        assert row.draft_json is None
        assert row.failure_seq == 0 and row.failure_code is None and row.error is None
        assert await db.scalar(select(func.count()).select_from(Invoice)) == int(
            decision == "confirm"
        )
        assert await db.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.action == audit.A.INBOUND_CONFIRM)
        ) == int(decision == "confirm")


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["raise", "missing"])
async def test_confirm_failure_rolls_back_invoice_and_link_together(email_pg, monkeypatch, fault):
    from app.api.routes import email as route

    client, factory, org_id, _, _ = email_pg
    row_id = await seeded(email_pg)
    original = route.audit.record

    async def refuse(db, action, **kwargs):
        if action == audit.A.INBOUND_CONFIRM:
            if fault == "raise":
                raise RuntimeError("Synthetic inbound-confirm audit failure")
            return None
        return await original(db, action, **kwargs)

    monkeypatch.setattr(route.audit, "record", refuse)
    body = {"draft": parser.parse_invoice_file("source.csv", CSV).draft.model_dump(mode="json")}
    if fault == "raise":
        with pytest.raises(RuntimeError, match="Synthetic inbound-confirm"):
            await client.post(f"/api/v1/email/inbox/{row_id}/confirm", json=body)
    else:
        refused = await client.post(f"/api/v1/email/inbox/{row_id}/confirm", json=body)
        assert refused.status_code == 503, refused.text
        assert refused.json()["code"] == "inbound_confirmation_unavailable"
    async with scoped(factory, org_id) as db:
        row = await db.get(InboundInvoice, row_id)
        assert row.status == "queued" and row.invoice_id is None
        assert await db.scalar(select(func.count()).select_from(Invoice)) == 0
    monkeypatch.setattr(route.audit, "record", original)
    response = await client.post(f"/api/v1/email/inbox/{row_id}/confirm", json=body)
    assert response.status_code == 201, response.text
    async with scoped(factory, org_id) as db:
        assert await db.scalar(select(func.count()).select_from(Invoice)) == 1


async def tagged_post(client, path, **kwargs):
    REQUEST_NAME.set(asyncio.current_task().get_name())
    return await client.post(path, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["generic", "mailgun"])
@pytest.mark.parametrize("identity", ["missing", "different"])
async def test_distinct_or_unidentified_deliveries_keep_both_sets(email_pg, provider, identity):
    client, factory, org_id, address, _ = email_pg
    keys = [None, None] if identity == "missing" else [uuid.uuid4().hex, uuid.uuid4().hex]
    results = await asyncio.gather(
        *(delivery(client, address, key, provider=provider) for key in keys)
    )
    assert [r.status_code for r in results] == [200, 200]
    assert [r.json()["queued"] for r in results] == [2, 2]
    assert not any(r.json()["deduplicated"] for r in results)
    async with scoped(factory, org_id) as db:
        assert await db.scalar(select(func.count()).select_from(InboundInvoice)) == 4
        assert (
            await db.scalar(
                select(func.count()).select_from(Job).where(Job.kind == "email.extract")
            )
            == 4
        )


@pytest.mark.asyncio
async def test_rechecks_same_message_after_health_commit(email_pg, monkeypatch):
    from app.services import inbound_health

    client, factory, org_id, address, _ = email_pg
    started, release = asyncio.Event(), asyncio.Event()
    original = inbound_health.begin_attempt

    # ASGI middleware can create an inner task: request identity is an inherited
    # context value, rather than the middleware's generated task name.
    async def held_health(*args, **kwargs):
        await original(*args, **kwargs)
        if REQUEST_NAME.get() == "email-health-first":
            started.set()
            await asyncio.wait_for(release.wait(), timeout=15)

    monkeypatch.setattr(inbound_health, "begin_attempt", held_health)
    key = uuid.uuid4().hex
    first = asyncio.create_task(delivery(client, address, key), name="email-health-first")
    try:
        await asyncio.wait_for(started.wait(), timeout=10)
        second = await delivery(client, address, key)
        assert second.status_code == 200 and second.json()["queued"] == 2, second.text
    finally:
        release.set()
        result = await first
    assert result.status_code == 200 and result.json()["deduplicated"], result.text
    async with scoped(factory, org_id) as db:
        assert await db.scalar(select(func.count()).select_from(InboundInvoice)) == 2
        assert (
            await db.scalar(
                select(func.count()).select_from(Job).where(Job.kind == "email.extract")
            )
            == 2
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("invalidated", ["recipient", "module"])
async def test_delivery_refreshes_authority_after_workspace_lock_wait(email_pg, invalidated):
    from app.models.email_intake import EmailIntake
    from app.models.module import OrgModule
    from app.models.organization import Organization

    client, factory, org_id, address, pids = email_pg
    async with scoped(factory, org_id) as blocker:
        await blocker.scalar(
            select(Organization.id).where(Organization.id == org_id).with_for_update(key_share=True)
        )
        pending = asyncio.create_task(
            delivery(client, address, uuid.uuid4().hex), name="email-revoked-delivery"
        )
        try:
            await blocking_or_finished(factory, pids, pending)
            assert not pending.done(), "Delivery did not wait for the actual workspace lock"
            async with scoped(factory, org_id) as mutation:
                if invalidated == "recipient":
                    row = await mutation.scalar(
                        select(EmailIntake).where(EmailIntake.org_id == org_id)
                    )
                    row.token = uuid.uuid4().hex
                else:
                    row = await mutation.scalar(
                        select(OrgModule).where(
                            OrgModule.org_id == org_id, OrgModule.key == "email_intake"
                        )
                    )
                    row.enabled = False
                await mutation.commit()
        finally:
            await blocker.rollback()
            result = await pending
    assert result.status_code == (401 if invalidated == "recipient" else 403), result.text
    async with scoped(factory, org_id) as db:
        assert await db.scalar(select(func.count()).select_from(InboundInvoice)) == 0
        assert await db.scalar(select(func.count()).select_from(Job)) == 0


@pytest.mark.asyncio
async def test_two_confirmations_share_lock_until_invoice_link_commit(email_pg, monkeypatch):
    from app.api.routes import email as route

    client, factory, org_id, _, pids = email_pg
    row_id = await seeded(email_pg)
    started, release = asyncio.Event(), asyncio.Event()
    original = route.persist_invoice

    async def held_invoice(*args, **kwargs):
        result = await original(*args, **kwargs)
        if REQUEST_NAME.get() == "email-confirm-first":
            started.set()
            await asyncio.wait_for(release.wait(), timeout=15)
        return result

    monkeypatch.setattr(route, "persist_invoice", held_invoice)
    body = {"draft": parser.parse_invoice_file("source.csv", CSV).draft.model_dump(mode="json")}
    path = f"/api/v1/email/inbox/{row_id}/confirm"
    first = asyncio.create_task(tagged_post(client, path, json=body), name="email-confirm-first")
    second = None
    try:
        await asyncio.wait_for(started.wait(), timeout=10)
        async with scoped(factory, org_id) as observer:
            assert await observer.scalar(select(func.count()).select_from(Invoice)) == 0
        second = asyncio.create_task(
            tagged_post(client, path, json=body), name="email-confirm-second"
        )
        await blocking_or_finished(factory, pids, second)
        assert not second.done(), "A competing confirm bypassed the source row lock"
    finally:
        release.set()
        results = await asyncio.gather(first, *([second] if second else []))
    assert sorted(r.status_code for r in results) == [201, 409], [r.text for r in results]
    async with scoped(factory, org_id) as db:
        row = await db.get(InboundInvoice, row_id)
        assert row.status == "confirmed" and row.invoice_id
        assert await db.scalar(select(func.count()).select_from(Invoice)) == 1
        assert (
            await db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == audit.A.INBOUND_CONFIRM)
            )
            == 1
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("first_decision", ["confirm", "discard"])
async def test_confirm_and_discard_share_the_review_boundary(email_pg, monkeypatch, first_decision):
    from app.api.routes import email as route

    client, factory, org_id, _, pids = email_pg
    row_id = await seeded(email_pg)
    started, release = asyncio.Event(), asyncio.Event()
    original = route._locked_inbound

    async def held_lock(*args, **kwargs):
        result = await original(*args, **kwargs)
        if REQUEST_NAME.get() == "email-decision-first":
            started.set()
            await asyncio.wait_for(release.wait(), timeout=15)
        return result

    monkeypatch.setattr(route, "_locked_inbound", held_lock)
    body = {"draft": parser.parse_invoice_file("source.csv", CSV).draft.model_dump(mode="json")}

    async def decide(action):
        return await tagged_post(
            client,
            f"/api/v1/email/inbox/{row_id}/{action}",
            json=body if action == "confirm" else None,
        )

    first = asyncio.create_task(decide(first_decision), name="email-decision-first")
    second = None
    try:
        await asyncio.wait_for(started.wait(), timeout=10)
        second = asyncio.create_task(
            decide("discard" if first_decision == "confirm" else "confirm"),
            name="email-decision-second",
        )
        await blocking_or_finished(factory, pids, second)
        assert not second.done(), "Competing decisions did not share the row lock"
    finally:
        release.set()
        results = await asyncio.gather(first, *([second] if second else []))
    assert results[0].status_code == (201 if first_decision == "confirm" else 200)
    assert results[1].status_code == 409, results[1].text
    async with scoped(factory, org_id) as db:
        row = await db.get(InboundInvoice, row_id)
        assert row.status == ("confirmed" if first_decision == "confirm" else "discarded")
        assert await db.scalar(select(func.count()).select_from(Invoice)) == int(
            first_decision == "confirm"
        )


@pytest.mark.asyncio
async def test_confirm_rechecks_membership_after_inbound_lock_wait(email_pg):
    from app.models.membership import Membership

    client, factory, org_id, _, pids = email_pg
    row_id = await seeded(email_pg)
    body = {"draft": parser.parse_invoice_file("source.csv", CSV).draft.model_dump(mode="json")}
    async with scoped(factory, org_id) as blocker:
        await blocker.scalar(
            select(InboundInvoice).where(InboundInvoice.id == row_id).with_for_update()
        )
        pending = asyncio.create_task(
            tagged_post(client, f"/api/v1/email/inbox/{row_id}/confirm", json=body),
            name="email-confirm-revoked",
        )
        try:
            await blocking_or_finished(factory, pids, pending)
            assert not pending.done(), "Confirmation did not wait for source lock"
            async with scoped(factory, org_id) as mutation:
                member = await mutation.scalar(
                    select(Membership).where(Membership.org_id == org_id)
                )
                member.status = "inactive"
                await mutation.commit()
        finally:
            await blocker.rollback()
            result = await pending
    assert result.status_code == 401, result.text
    async with scoped(factory, org_id) as db:
        assert await db.scalar(select(func.count()).select_from(Invoice)) == 0
        row = await db.get(InboundInvoice, row_id)
        assert row.status == "queued" and row.invoice_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("late_failure", [False, True])
async def test_duplicate_worker_cannot_replace_newer_publication(
    email_pg, monkeypatch, late_failure
):
    client, factory, org_id, _, _ = email_pg
    row_id = await seeded(email_pg)
    started, release = asyncio.Event(), asyncio.Event()
    original = image_ocr.run_reading
    first = True

    async def held_parse(*args, **kwargs):
        nonlocal first
        if first:
            first = False
            started.set()
            await asyncio.wait_for(release.wait(), timeout=15)
            if late_failure:
                raise ValueError("Synthetic late obsolete parser failure")
        return await original(*args, **kwargs)

    monkeypatch.setattr(image_ocr, "run_reading", held_parse)

    async def extract():
        async with scoped(factory, org_id) as db:
            return await email_intake.extract_inbound(db, row_id)

    old = asyncio.create_task(extract())
    try:
        await asyncio.wait_for(started.wait(), timeout=10)
        current = await extract()
        assert current["status"] == "pending"
        async with scoped(factory, org_id) as observer:
            row = await observer.get(InboundInvoice, row_id)
            expected = (row.draft_json, row.updated_at)
    finally:
        release.set()
        obsolete = await old
    assert obsolete == {"skipped": "inbound changed"}
    async with scoped(factory, org_id) as db:
        row = await db.get(InboundInvoice, row_id)
        assert row.status == "pending" and row.failure_seq == 0
        assert (row.draft_json, row.updated_at) == expected
        assert await db.scalar(select(func.count()).select_from(Invoice)) == 0


@pytest.mark.asyncio
async def test_delete_during_parse_keeps_attachment_deleted(email_pg, monkeypatch):
    client, factory, org_id, _, _ = email_pg
    row_id = await seeded(email_pg)
    started, release = asyncio.Event(), asyncio.Event()
    original = image_ocr.run_reading

    async def held_parse(*args, **kwargs):
        started.set()
        await asyncio.wait_for(release.wait(), timeout=15)
        return await original(*args, **kwargs)

    monkeypatch.setattr(image_ocr, "run_reading", held_parse)

    async def extract():
        async with scoped(factory, org_id) as db:
            return await email_intake.extract_inbound(db, row_id)

    worker = asyncio.create_task(extract())
    try:
        await asyncio.wait_for(started.wait(), timeout=10)
        result = await client.delete(f"/api/v1/email/inbox/{row_id}")
        assert result.status_code == 204, result.text
    finally:
        release.set()
        publication = await worker
    assert publication == {"skipped": "row gone"}
    async with scoped(factory, org_id) as db:
        assert await db.get(InboundInvoice, row_id) is None
        assert await db.scalar(select(func.count()).select_from(Invoice)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["confirm", "discard"])
async def test_late_storage_failure_cannot_replace_review_decision(email_pg, monkeypatch, decision):
    from app.services import capture_failures, documents

    client, factory, org_id, _, _ = email_pg
    row_id = await seeded(email_pg)
    started, release = asyncio.Event(), asyncio.Event()

    async def held_read(*args, **kwargs):
        started.set()
        await asyncio.wait_for(release.wait(), timeout=15)
        raise capture_failures.CaptureError(
            capture_failures.STORED_FILE_CHANGED, "Synthetic late changed source"
        )

    monkeypatch.setattr(documents, "load_capture", held_read)

    async def work():
        async with scoped(factory, org_id) as db:
            return await email_intake.extract_inbound(db, row_id)

    worker = asyncio.create_task(work())
    try:
        await asyncio.wait_for(started.wait(), timeout=10)
        body = (
            {"draft": parser.parse_invoice_file("source.csv", CSV).draft.model_dump(mode="json")}
            if decision == "confirm"
            else None
        )
        response = await client.post(f"/api/v1/email/inbox/{row_id}/{decision}", json=body)
        assert response.status_code == (201 if decision == "confirm" else 200), response.text
    finally:
        release.set()
        result = await worker
    assert result == {"skipped": "inbound changed"}
    async with scoped(factory, org_id) as db:
        row = await db.get(InboundInvoice, row_id)
        assert row.status == ("confirmed" if decision == "confirm" else "discarded")
        assert row.failure_seq == 0 and row.failure_code is None and row.error is None


@pytest.mark.asyncio
async def test_admission_failure_rolls_back_entire_attachment_set_before_retry(
    email_pg, monkeypatch
):
    client, factory, org_id, address, _ = email_pg
    original = jobs.enqueue
    queued = 0

    async def fail_second(*args, **kwargs):
        nonlocal queued
        queued += 1
        result = await original(*args, **kwargs)
        if queued == 2:
            raise RuntimeError("Synthetic second attachment queue failure")
        return result

    monkeypatch.setattr(jobs, "enqueue", fail_second)
    key = uuid.uuid4().hex
    with pytest.raises(RuntimeError, match="Synthetic second attachment"):
        await delivery(client, address, key)
    async with scoped(factory, org_id) as db:
        assert await db.scalar(select(func.count()).select_from(InboundInvoice)) == 0
        assert await db.scalar(select(func.count()).select_from(Job)) == 0
    monkeypatch.setattr(jobs, "enqueue", original)
    result = await delivery(client, address, key)
    assert result.status_code == 200 and result.json()["queued"] == 2, result.text
    async with scoped(factory, org_id) as db:
        assert await db.scalar(select(func.count()).select_from(InboundInvoice)) == 2
        assert await db.scalar(select(func.count()).select_from(Job)) == 2


@pytest.mark.asyncio
async def test_delivery_mutex_does_not_block_an_existing_child_writer(email_pg):
    _, factory, org_id, _, _ = email_pg
    async with scoped(factory, org_id) as admission:
        assert await email_intake.lock_delivery_workspace(admission, org_id)

        async def child_writer():
            async with scoped(factory, org_id) as db:
                row = await email_intake.process_attachment(
                    db,
                    org_id,
                    from_addr=None,
                    subject=None,
                    filename="source.csv",
                    content_type="text/csv",
                    content=CSV,
                    message_id=uuid.uuid4().hex,
                )
                await db.commit()
                return row.id

        child = asyncio.create_task(child_writer())
        try:
            # The writer obtains real PostgreSQL FK KEY SHARE locks while the
            # admission mutex is still held. FOR UPDATE would block it here.
            row_id = await asyncio.wait_for(asyncio.shield(child), timeout=5)
        finally:
            await admission.rollback()
            await child
    async with scoped(factory, org_id) as db:
        assert (await db.get(InboundInvoice, row_id)).status == "queued"


@pytest.mark.asyncio
async def test_same_message_retains_per_attachment_rejections(email_pg):
    client, factory, org_id, address, _ = email_pg
    body = {
        "to": address,
        "message_id": uuid.uuid4().hex,
        "attachments": [
            {"filename": "source.csv", "content_base64": base64.b64encode(CSV).decode()},
            {
                "filename": "rejected.txt",
                "content_base64": base64.b64encode(b"not an invoice").decode(),
            },
        ],
    }
    accepted = await client.post(
        "/api/v1/email/inbound", headers={"X-Inbound-Secret": SECRET}, json=body
    )
    assert accepted.status_code == 200 and accepted.json() == {
        "received": 2,
        "queued": 1,
        "rejected": 1,
        "deduplicated": False,
    }
    replay = await client.post(
        "/api/v1/email/inbound", headers={"X-Inbound-Secret": SECRET}, json=body
    )
    assert replay.status_code == 200 and replay.json()["deduplicated"], replay.text
    async with scoped(factory, org_id) as db:
        assert await db.scalar(select(func.count()).select_from(InboundInvoice)) == 2
        assert await db.scalar(select(func.count()).select_from(Job)) == 1


@pytest.mark.asyncio
async def test_worker_is_tenant_scoped(email_pg):
    _, factory, org_id, _, _ = email_pg
    row_id = await seeded(email_pg)
    async with scoped(factory, str(uuid.uuid4())) as outsider:
        assert await email_intake.extract_inbound(outsider, row_id) == {"skipped": "row gone"}
    async with scoped(factory, org_id) as db:
        row = await db.get(InboundInvoice, row_id)
        assert row.status == "queued" and row.draft_json is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "user",
        "membership",
        "session",
        "organization",
        "module",
        "deleted",
        "source",
        "size",
        "rejected",
    ],
)
async def test_email_original_rechecks_access_and_source_after_storage(
    email_pg, monkeypatch, change
):
    from datetime import UTC, datetime

    from sqlalchemy import delete, update

    from app.models.membership import Membership
    from app.models.module import OrgModule
    from app.models.organization import Organization
    from app.models.session import Session
    from app.models.user import User
    from app.services import documents

    client, factory, org_id, _, _ = email_pg
    row_id = await seeded(email_pg)
    uid = (await client.get("/api/v1/auth/me")).json()["user"]["id"]
    original = documents.load

    async def change_after_read(*args, **kwargs):
        data = await original(*args, **kwargs)
        async with scoped(factory, org_id) as mutation:
            if change == "deleted":
                await mutation.execute(delete(InboundInvoice).where(InboundInvoice.id == row_id))
            else:
                model, condition, values = {
                    "user": (User, User.id == uid, {"is_active": False}),
                    "membership": (Membership, Membership.user_id == uid, {"status": "suspended"}),
                    "session": (Session, Session.user_id == uid, {"revoked_at": datetime.now(UTC)}),
                    "organization": (
                        Organization,
                        Organization.id == org_id,
                        {"status": "suspended"},
                    ),
                    "module": (OrgModule, OrgModule.key == "email_intake", {"enabled": False}),
                    "source": (InboundInvoice, InboundInvoice.id == row_id, {"sha256": "0" * 64}),
                    "size": (InboundInvoice, InboundInvoice.id == row_id, {"size": len(CSV) + 1}),
                    "rejected": (
                        InboundInvoice,
                        InboundInvoice.id == row_id,
                        {"status": "rejected"},
                    ),
                }[change]
                await mutation.execute(update(model).where(condition).values(**values))
            await mutation.commit()
        return data

    monkeypatch.setattr(documents, "load", change_after_read)
    response = await client.get(f"/api/v1/email/inbox/{row_id}/file")
    expected = (
        401
        if change in {"user", "membership", "session", "organization"}
        else 403
        if change == "module"
        else 404
    )
    assert response.status_code == expected, response.text
    assert "content-disposition" not in response.headers
    async with scoped(factory, org_id) as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == audit.A.DOC_DOWNLOAD)
            )
            == 0
        )
