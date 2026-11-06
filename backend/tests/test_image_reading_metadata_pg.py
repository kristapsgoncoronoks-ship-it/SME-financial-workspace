"""Native publication guards preserve preparation with the exact reading attempt."""

import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.database import get_session
from app.core.tenant import reset_current_org, set_current_org
from app.main import app
from app.models.email_intake import InboundInvoice
from app.models.extraction_run import ExtractionRun
from app.models.job import Job
from app.services import capture_failures, documents, email_intake, extraction, image_ocr
from tests.test_image_ocr_process import image_bytes
from tests.test_image_reading_metadata import TEXT

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
async def reading_pg(monkeypatch):
    # Imported fixtures do not inherit this module's skip marker. Keep every
    # importing native cohort opt-in, including ordinary SQLite collection.
    if not PG_URL:
        pytest.skip("requires migrated non-superuser PostgreSQL")
    monkeypatch.setattr(settings, "smtp_host", None)
    engine = create_async_engine(PG_URL, pool_size=6, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    async def session():
        async with factory() as db:
            yield db

    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_session] = session
    org_id = None
    try:
        async with factory() as db:
            assert not await db.scalar(
                text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user")
            )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/v1/auth/register",
                json={
                    "organization_name": "Synthetic measured image reading",
                    "name": "Synthetic owner",
                    "email": f"image-reading-{uuid.uuid4().hex}@example.com",
                    "password": "Synthetic-Reading-PG-Password-32-Only",
                },
            )
            assert response.status_code == 201, response.text
            org_id = response.json()["organization"]["id"]
            client.headers["Authorization"] = "Bearer " + response.json()["token"]["access_token"]
            assert (
                await client.put("/api/v1/modules/email_intake", json={"enabled": True})
            ).status_code == 200
            yield client, factory, org_id
    finally:
        if org_id is not None:
            # Only these synthetic direct-worker intents: no global queue reset.
            async with scoped(factory, org_id) as db:
                await db.execute(
                    delete(Job).where(
                        Job.org_id == org_id,
                        Job.kind.in_((extraction.UPLOAD_EXTRACT_KIND, email_intake.EXTRACT_KIND)),
                    )
                )
                await db.commit()
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
        await engine.dispose()


async def seed(reading_pg, kind, monkeypatch):
    client, factory, org_id = reading_pg
    content = image_bytes()
    monkeypatch.setattr(image_ocr, "_emit", lambda message: None)
    raster, preparation = image_ocr._prepare(content)
    raster.close()
    if kind == "upload":
        accepted = await client.post(
            "/api/v1/invoices/upload", files={"file": ("native-reading.png", content, "image/png")}
        )
        assert accepted.status_code == 202, accepted.text
        row_id = accepted.json()["extraction_run_id"]
    else:
        # Images remain excluded by current email admission. This row exercises
        # the worker's historical image compatibility without changing policy.
        async with scoped(factory, org_id) as db:
            sha, _ = await documents.store(
                documents.EMAIL_ATTACHMENTS, org_id, content, "image/png"
            )
            row = InboundInvoice(
                org_id=org_id,
                received_at=datetime.now(UTC),
                filename="native-reading.png",
                content_type="image/png",
                size=len(content),
                sha256=sha,
                status="queued",
            )
            db.add(row)
            await db.commit()
            row_id = row.id
    return row_id, image_ocr.ImageReading(TEXT, preparation)


@pytest.mark.parametrize("kind", ["upload", "email"])
@pytest.mark.parametrize("failure", [False, True])
async def test_native_published_preparation_survives_fresh_connection(
    reading_pg, monkeypatch, kind, failure
):
    client, factory, org_id = reading_pg
    row_id, reading = await seed(reading_pg, kind, monkeypatch)

    def controlled(content):
        if failure:
            raise image_ocr.ImageOcrError(
                capture_failures.IMAGE_PROCESSING_LIMIT, reading.preparation
            )
        return reading

    monkeypatch.setattr(image_ocr, "read", controlled)
    async with scoped(factory, org_id) as db:
        operation = extraction.extract_upload if kind == "upload" else email_intake.extract_inbound
        await operation(db, row_id)
    async with scoped(factory, org_id) as db:
        row = await db.get(ExtractionRun if kind == "upload" else InboundInvoice, row_id)
        assert json.loads(row.reading_preparation_json) == reading.preparation.model_dump()
        assert (row.draft_json is None) is failure
        assert row.status == ("failed" if failure else "parsed" if kind == "upload" else "pending")
    path = (
        f"/api/v1/invoices/upload/{row_id}" if kind == "upload" else f"/api/v1/email/inbox/{row_id}"
    )
    response = await client.get(path)
    assert response.status_code == 200, response.text
    assert response.json()["reading_preparation"] == reading.preparation.model_dump()


@pytest.mark.parametrize("kind", ["upload", "email"])
@pytest.mark.parametrize("failure", [False, True])
async def test_native_late_reading_cannot_publish_preparation_over_new_state(
    reading_pg, monkeypatch, kind, failure
):
    _, factory, org_id = reading_pg
    row_id, reading = await seed(reading_pg, kind, monkeypatch)
    reached, release = asyncio.Event(), asyncio.Event()
    original = image_ocr.run_reading
    monkeypatch.setattr(image_ocr, "read", lambda content: reading)

    async def held(*args):
        value = await original(*args)
        reached.set()
        await asyncio.wait_for(release.wait(), 10)
        if failure:
            raise image_ocr.ImageOcrError(
                capture_failures.IMAGE_PROCESSING_LIMIT, reading.preparation
            )
        return value

    monkeypatch.setattr(image_ocr, "run_reading", held)

    async def work():
        async with scoped(factory, org_id) as db:
            operation = (
                extraction.extract_upload if kind == "upload" else email_intake.extract_inbound
            )
            return await operation(db, row_id)

    pending = asyncio.create_task(work())
    try:
        await asyncio.wait_for(reached.wait(), 10)
        async with scoped(factory, org_id) as db:
            row = await db.scalar(
                select(ExtractionRun if kind == "upload" else InboundInvoice)
                .where((ExtractionRun if kind == "upload" else InboundInvoice).id == row_id)
                .with_for_update()
            )
            if kind == "upload":
                row.attempt_seq += 1
                row.status = "queued"
            else:
                row.status = "discarded"
            row.reading_preparation_json = None
            row.draft_json = None
            await db.commit()
    finally:
        release.set()
        result = await pending
    assert "skipped" in result
    async with scoped(factory, org_id) as db:
        row = await db.get(ExtractionRun if kind == "upload" else InboundInvoice, row_id)
        assert row.reading_preparation_json is None and row.draft_json is None
        assert row.status == ("queued" if kind == "upload" else "discarded")
