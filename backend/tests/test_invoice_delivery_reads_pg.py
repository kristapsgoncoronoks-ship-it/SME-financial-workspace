"""Real PostgreSQL delivery fences around the native attachment read boundary."""

import asyncio
import os
import uuid
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.database import get_session
from app.core.tenant import reset_current_org, set_current_org
from app.main import app
from app.models.document import Document
from app.models.email_message import EmailMessage
from app.models.job import Job
from app.services import document_registry, documents, invoice_delivery, jobs, mailer
from tests.test_r011_invoice_commands import queue

PG_URL = os.environ.get("RLS_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not PG_URL, reason="requires migrated non-superuser PostgreSQL")


@asynccontextmanager
async def scoped(factory, org_id):
    token = set_current_org(org_id)
    try:
        async with factory() as db:
            yield db
    finally:
        reset_current_org(token)


@pytest_asyncio.fixture
async def delivery_pg(monkeypatch):
    engine = create_async_engine(PG_URL, pool_size=6, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    async def session():
        async with factory() as db:
            yield db

    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_session] = session
    calls = []
    org_id = None
    message = None
    monkeypatch.setattr(mailer, "_smtp_send", lambda *args: calls.append(args))
    try:
        async with factory() as db:
            assert not await db.scalar(
                text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user")
            )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/v1/auth/register",
                json={
                    "organization_name": "Synthetic delivery boundary",
                    "name": "Synthetic owner",
                    "email": f"delivery-boundary-{uuid.uuid4().hex}@example.com",
                    "password": "Synthetic-Delivery-Boundary-32-Password",
                },
            )
            assert response.status_code == 201, response.text
            identity = response.json()
            org_id = identity["organization"]["id"]
            client.headers["Authorization"] = "Bearer " + identity["token"]["access_token"]
            invoice, message = await queue(client, monkeypatch)
            yield client, factory, identity["organization"]["id"], invoice, message, calls
    finally:
        if org_id is not None and message is not None:
            # These tests call the delivery service directly. Remove only its
            # own unclaimed durable intent so a later global worker fixture
            # cannot consume this test's job instead of its own new message.
            async with scoped(factory, org_id) as db:
                await db.execute(
                    delete(Job).where(
                        Job.org_id == org_id,
                        Job.kind == invoice_delivery.KIND,
                        Job.idempotency_key == message["id"],
                    )
                )
                await db.commit()
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
        await engine.dispose()


async def wait_exact_lock(factory, pid, task):
    for _ in range(500):
        assert not task.done(), "Worker finished before its held message lock was released"
        async with factory() as observer:
            blocked = await observer.scalar(
                text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"), {"pid": pid}
            )
        if blocked:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("Worker never waited on its exact PostgreSQL connection")


async def test_admitted_size_ignores_cached_metadata_without_flushing(delivery_pg):
    _client, factory, org_id, _invoice, message, calls = delivery_pg
    async with scoped(factory, org_id) as reader:
        cached = await reader.scalar(select(Document).where(Document.kind == "invoice-delivery"))
        old_size = cached.size
        async with scoped(factory, org_id) as writer:
            await writer.execute(
                update(Document).where(Document.id == cached.id).values(size=old_size + 1)
            )
            await writer.commit()
        cached.filename = "Synthetic pending metadata, never flushed"
        reader.sync_session.autoflush = True
        assert (
            await document_registry.admitted_size(
                reader,
                org_id,
                sha256=cached.sha256,
                kind="invoice-delivery",
            )
            == old_size + 1
        )
        assert cached.size == old_size and cached in reader.dirty
        assert (
            await document_registry.admitted_size(
                reader, org_id, sha256=cached.sha256, kind="uploads"
            )
            is None
        )
        assert cached in reader.dirty and calls == []


@pytest.mark.parametrize("committed", ["sent", "sending", "unknown"])
async def test_delivery_lock_wait_refreshes_cached_outbox_state(delivery_pg, committed):
    _client, factory, org_id, _invoice, message, calls = delivery_pg
    async with scoped(factory, org_id) as worker, scoped(factory, org_id) as blocker:
        cached = await worker.get(EmailMessage, message["id"])
        assert cached.status == "queued"
        pid = await worker.scalar(text("SELECT pg_backend_pid()"))
        current = await blocker.scalar(
            select(EmailMessage).where(EmailMessage.id == cached.id).with_for_update()
        )
        current.status = committed
        task = asyncio.create_task(invoice_delivery.deliver(worker, org_id, cached.id))
        try:
            await wait_exact_lock(factory, pid, task)
        finally:
            await blocker.commit()
        result = await asyncio.wait_for(task, timeout=10)
        assert result == {"status": "unknown" if committed == "sending" else committed}
        assert calls == []
    async with scoped(factory, org_id) as observer:
        final = await observer.get(EmailMessage, message["id"])
        assert final.status == result["status"]


@pytest.mark.parametrize("change", ["void_invoice", "admission_size"])
async def test_delivery_rechecks_committed_evidence_after_actual_storage_read(
    delivery_pg, monkeypatch, change
):
    client, factory, org_id, invoice, message, calls = delivery_pg
    entered, release = asyncio.Event(), asyncio.Event()
    original_load = documents.load

    async def paused(*args, **kwargs):
        content = await original_load(*args, **kwargs)
        if args[0] == "invoice-delivery":
            entered.set()
            await release.wait()
        return content

    monkeypatch.setattr(documents, "load", paused)
    async with scoped(factory, org_id) as worker:
        task = asyncio.create_task(invoice_delivery.deliver(worker, org_id, message["id"]))
        try:
            await asyncio.wait_for(entered.wait(), timeout=10)
            assert not task.done() and calls == []
            if change == "void_invoice":
                response = await client.post(
                    f"/api/v1/issued/{invoice['id']}/void", json={"reason": "Synthetic void"}
                )
                assert response.status_code == 200, response.text
            else:
                async with scoped(factory, org_id) as changed:
                    await changed.execute(
                        update(Document)
                        .where(Document.kind == "invoice-delivery")
                        .values(size=Document.size + 1)
                    )
                    await changed.commit()
        finally:
            release.set()
        if change == "void_invoice":
            assert await asyncio.wait_for(task, timeout=10) == {"status": "blocked"}
        else:
            with pytest.raises(jobs.PermanentJobError):
                await asyncio.wait_for(task, timeout=10)
        assert calls == []
    async with scoped(factory, org_id) as observer:
        final = await observer.get(EmailMessage, message["id"])
        assert final.status == ("blocked" if change == "void_invoice" else "failed")
        assert final.sent_at is None
