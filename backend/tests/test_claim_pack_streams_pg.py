"""A prepared filing ZIP is released only for current authority and source facts."""

import asyncio
import os
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core import storage
from app.core.database import get_session
from app.main import app
from app.models.document import Document
from app.models.extraction_run import ExtractionRun
from app.services import document_registry, documents, modules
from tests.test_invoice_delivery_reads_pg import scoped
from tests.transport.test_wo77_admin_routes import (
    _enable_transport,
    _register_org,
    _submitted_claim,
)

PG_URL = os.environ.get("RLS_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not PG_URL, reason="requires migrated non-superuser PostgreSQL")


@pytest_asyncio.fixture
async def claim_pg():
    engine = create_async_engine(PG_URL, pool_size=6, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    async def session():
        async with factory() as db:
            yield db

    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_session] = session
    try:
        async with factory() as db:
            assert not await db.scalar(
                text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user")
            )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            headers, org_id = await _register_org(client)
            async with scoped(factory, org_id) as db:
                await _enable_transport(db, org_id)
                claim, _entity = await _submitted_claim(client, db, headers, org_id)
                run = await db.scalar(
                    select(ExtractionRun).where(ExtractionRun.invoice_id.is_not(None))
                )
                data = storage.get_storage().get(
                    storage.content_key(documents.UPLOADS, org_id, run.source_sha256)
                )
                await document_registry.register(
                    db,
                    org_id,
                    sha256=run.source_sha256,
                    size=len(data),
                    kind=documents.UPLOADS,
                )
                await db.commit()
                run_id = run.id
            yield client, factory, org_id, headers, claim, run_id
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
        await engine.dispose()


@pytest.mark.parametrize("change", ["logout", "module", "source", "admission"])
async def test_claim_evidence_rechecks_after_real_storage_io(claim_pg, monkeypatch, change):
    client, factory, org_id, headers, claim, run_id = claim_pg
    entered, release, created = asyncio.Event(), asyncio.Event(), []
    old_load, old_file, old_temp = documents.load, documents.load_file, tempfile.mkstemp

    async def pause(prefix):
        if prefix == documents.UPLOADS and not entered.is_set():
            entered.set()
            await release.wait()

    async def load(*args, **kwargs):
        content = await old_load(*args, **kwargs)
        await pause(args[0])
        return content

    async def load_file(*args, **kwargs):
        staged = await old_file(*args, **kwargs)
        await pause(args[0])
        return staged

    def temporary(*args, **kwargs):
        result = old_temp(*args, **kwargs)
        created.append(Path(result[1]))
        return result

    monkeypatch.setattr(documents, "load", load)
    monkeypatch.setattr(documents, "load_file", load_file)
    monkeypatch.setattr(tempfile, "mkstemp", temporary)
    task = asyncio.create_task(
        client.get(f"/api/v1/transport/claims/{claim['id']}/evidence", headers=headers)
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        assert not task.done()
        if change == "logout":
            response = await client.post("/api/v1/auth/logout", headers=headers)
            assert response.status_code == 200 and response.json() == {"logged_out": True}
        else:
            async with scoped(factory, org_id) as changed:
                if change == "module":
                    await modules.set_enabled(changed, org_id, "transport", False)
                elif change == "source":
                    await changed.execute(
                        update(ExtractionRun)
                        .where(ExtractionRun.id == run_id)
                        .values(source_sha256="0" * 64)
                    )
                else:
                    await changed.execute(
                        update(Document)
                        .where(Document.kind == documents.UPLOADS)
                        .values(size=Document.size + 1)
                    )
                await changed.commit()
    finally:
        release.set()
    response = await asyncio.wait_for(task, timeout=10)
    assert response.status_code == (
        401 if change == "logout" else 403 if change == "module" else 409
    ), f"Unexpected HTTP {response.status_code} ({response.headers.get('content-type')})"
    assert "content-disposition" not in response.headers and not response.content.startswith(b"PK")
    assert all(not path.exists() for path in created)
