"""Native non-superuser history paging over the actual API and legacy route."""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.database import get_session
from app.main import app
from tests.test_payment_run_pagination import (
    BASE,
    assert_run_pages,
    run_history_state,
    seed_run_history,
)

PG_URL = os.environ.get("RLS_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not PG_URL, reason="requires migrated non-superuser PostgreSQL")


async def register(client):
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "organization_name": "Synthetic native AP history",
            "name": "Synthetic history reader",
            "email": f"ap-history-{uuid4()}@example.com",
            "password": "Synthetic-History-Password-32",
        },
    )
    assert response.status_code == 201, response.text
    identity = response.json()
    return identity["organization"]["id"], {
        "Authorization": "Bearer " + identity["token"]["access_token"]
    }


@pytest_asyncio.fixture
async def run_page_pg(monkeypatch):
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
            ) is False
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            org_id, headers = await register(client)
            client.headers.update(headers)
            async with factory() as db:
                rows = await seed_run_history(db, org_id)
            yield client, factory, org_id, rows
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
        await engine.dispose()


@pytest.mark.asyncio
async def test_native_run201_exact_pages_legacy_array_and_zero_financial_writes(run_page_pg):
    client, factory, org_id, rows = run_page_pg
    before = await run_history_state(factory, org_id)
    await assert_run_pages(client, rows)
    legacy = await client.get(BASE)
    assert isinstance(legacy.json(), list) and len(legacy.json()) == 200
    last = await client.get(f"{BASE}/page?limit=200&offset=200")
    assert len(last.json()["items"]) == 1 and last.json()["total"] == 201
    assert last.json()["items"][0]["invoice_count"] == 2
    after = await run_history_state(factory, org_id)
    assert after == before
    artifact = os.environ.get("AP_HISTORY_ARTIFACT_DIR")
    if artifact:
        Path(artifact).mkdir(parents=True, exist_ok=True)
        Path(artifact, "native-history-outcome.json").write_text(
            json.dumps(
                {
                    "org_id": org_id,
                    "ordered_ids": [row.id for row in rows],
                    "last_page": last.json(),
                    "before": before,
                    "after": after,
                },
                indent=2,
                default=str,
            )
            + "\n"
        )


@pytest.mark.asyncio
async def test_native_run_history_total_and_rows_remain_opaque_across_tenants(run_page_pg):
    client, factory, org_id, _rows = run_page_pg
    before = await run_history_state(factory, org_id)
    other_id, other_headers = await register(client)
    empty = await client.get(f"{BASE}/page", headers=other_headers)
    assert empty.status_code == 200 and empty.json() == {"items": [], "total": 0}
    async with factory() as db:
        other = await seed_run_history(db, other_id, count=3)
    response = await client.get(f"{BASE}/page", headers=other_headers)
    assert response.json()["total"] == 3
    assert [r["id"] for r in response.json()["items"]] == [r.id for r in other]
    assert (await client.get(f"{BASE}/page", headers={"Authorization": ""})).status_code == 401
    assert await run_history_state(factory, org_id) == before
