"""Actual non-superuser PostgreSQL registry paging and tenant opacity."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.database import get_session
from app.core.tenant import reset_current_org, set_current_org
from app.main import app
from tests.test_document_pagination import (
    BASE,
    assert_registry_pages,
    registry_state,
    seed_registry,
)

PG_URL = os.environ.get("RLS_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not PG_URL, reason="requires migrated non-superuser PostgreSQL")


async def register(client):
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "organization_name": "Synthetic registry reader",
            "name": "Synthetic owner",
            "email": f"registry-pages-{uuid4()}@example.com",
            "password": "Synthetic-Registry-Password-32",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return body["organization"]["id"], {"Authorization": "Bearer " + body["token"]["access_token"]}


@pytest_asyncio.fixture
async def registry_pg(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "smtp_host", None)
    engine = create_async_engine(PG_URL, pool_size=4, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    async def session():
        async with factory() as db:
            yield db

    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_session] = session
    token = None
    try:
        async with factory() as db:
            assert (
                await db.scalar(
                    text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user")
                )
            ) is False
            assert (
                await db.execute(
                    text(
                        "SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE relname='documents'"
                    )
                )
            ).one() == (True, True)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            org_id, headers = await register(client)
            client.headers.update(headers)
            token = set_current_org(org_id)
            async with factory() as db:
                rows = await seed_registry(db, org_id)
            yield client, factory, org_id, rows
    finally:
        if token is not None:
            reset_current_org(token)
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
        await engine.dispose()


@pytest.mark.asyncio
async def test_native_registry203_complete_pages_facets_and_read_only_legacy(registry_pg):
    client, factory, org_id, rows = registry_pg
    before = await registry_state(factory, org_id)
    await assert_registry_pages(client, rows)
    legacy = await client.get(BASE)
    assert isinstance(legacy.json(), list) and len(legacy.json()) == len(rows)
    filtered = (await client.get(f"{BASE}/page?kind=receipts&limit=1")).json()
    assert filtered["total"] == 101 and filtered["kinds"] == ["logos", "receipts"]
    assert len(filtered["items"]) == 1 and filtered["items"][0]["kind"] == "receipts"
    assert await registry_state(factory, org_id) == before


@pytest.mark.asyncio
async def test_native_registry_rows_total_and_facets_stay_in_tenant(registry_pg):
    client, factory, org_id, _rows = registry_pg
    before = await registry_state(factory, org_id)
    other_id, headers = await register(client)
    assert (await client.get(f"{BASE}/page", headers=headers)).json() == {
        "items": [],
        "total": 0,
        "kinds": [],
    }
    token = set_current_org(other_id)
    try:
        async with factory() as db:
            other = await seed_registry(db, other_id, count=3, kind="foreign-only")
    finally:
        reset_current_org(token)
    foreign = (await client.get(f"{BASE}/page", headers=headers)).json()
    assert foreign["total"] == 3 and foreign["kinds"] == ["foreign-only"]
    assert [row["id"] for row in foreign["items"]] == [row.id for row in other]
    own = (await client.get(f"{BASE}/page")).json()
    assert own["total"] == 203 and own["kinds"] == ["logos", "receipts"]
    async with factory() as db:
        assert (
            await db.scalar(
                text("SELECT count(*) FROM documents WHERE org_id=:org"), {"org": other_id}
            )
            == 0
        )
    assert await registry_state(factory, org_id) == before
