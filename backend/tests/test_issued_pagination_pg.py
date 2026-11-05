"""Real non-superuser PostgreSQL/API issued page and independent export scope."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.database import get_session
from app.main import app
from tests.test_issued_lifecycle import _activate
from tests.test_issued_pagination import (
    BASE,
    assert_bounded_pages,
    assert_global_zip_scope,
    issued_state,
    seed_issued_page,
)

PG_URL = os.environ.get("RLS_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not PG_URL, reason="requires migrated non-superuser PostgreSQL")


@pytest_asyncio.fixture
async def issued_page_pg(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "smtp_host", None)
    engine = create_async_engine(PG_URL, pool_size=4, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    async def session():
        async with factory() as db:
            yield db

    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_session] = session
    try:
        async with factory() as db:
            assert (
                await db.scalar(
                    text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user")
                )
                is False
            )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/v1/auth/register",
                json={
                    "organization_name": "Synthetic issued page qualification",
                    "name": "Synthetic page operator",
                    "email": f"issued-page-{uuid4()}@example.com",
                    "password": "Synthetic-Issued-Page-Password-32",
                },
            )
            assert response.status_code == 201, response.text
            identity = response.json()
            client.headers["Authorization"] = "Bearer " + identity["token"]["access_token"]
            await _activate(client)
            org_id = identity["organization"]["id"]
            async with factory() as db:
                rows = await seed_issued_page(db, org_id)
            yield client, factory, engine, org_id, rows
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
        await engine.dispose()


@pytest.mark.asyncio
async def test_native_all_pages_are_bounded_exact_and_read_only(issued_page_pg, record_property):
    client, factory, engine, org_id, rows = issued_page_pg
    before = await issued_state(factory, org_id)
    await assert_bounded_pages(client, rows, engine.sync_engine)
    last = await client.get(BASE, params={"page": 2, "page_size": 100})
    assert len(last.json()["items"]) == 5 and last.json()["total"] == 105
    beyond = await client.get(BASE, params={"page": 3, "page_size": 100})
    assert beyond.json() == {"items": [], "total": 105}
    assert await issued_state(factory, org_id) == before
    record_property("synthetic_invoices", 105)
    record_property("issuer_count", 2)
    record_property("tested_page_sizes", "1,37,100,500")
    record_property("invoice_queries_per_page", 2)
    record_property("no_financial_state_change", True)


@pytest.mark.asyncio
async def test_native_later_page_does_not_narrow_independent_zip_filters(
    issued_page_pg, monkeypatch, record_property
):
    client, factory, _engine, org_id, rows = issued_page_pg
    before = await issued_state(factory, org_id)
    await assert_global_zip_scope(client, rows, monkeypatch)
    assert await issued_state(factory, org_id) == before
    record_property(
        "scope", "Actual ZIP selection after page2, declared synthetic PDF renderer only"
    )


@pytest.mark.asyncio
async def test_native_other_tenant_and_unauthenticated_page_reads_are_opaque(issued_page_pg):
    client, factory, _engine, org_id, _rows = issued_page_pg
    before = await issued_state(factory, org_id)
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "organization_name": "Other synthetic issued page tenant",
            "name": "Other synthetic reader",
            "email": f"other-issued-page-{uuid4()}@example.com",
            "password": "Synthetic-Other-Page-Password-32",
        },
    )
    assert response.status_code == 201, response.text
    headers = {"Authorization": "Bearer " + response.json()["token"]["access_token"]}
    other = await client.get(f"{BASE}?page=2&page_size=100", headers=headers)
    assert other.status_code == 200 and other.json() == {"items": [], "total": 0}
    denied = await client.get(BASE, headers={"Authorization": ""})
    assert denied.status_code == 401, denied.text
    assert await issued_state(factory, org_id) == before
