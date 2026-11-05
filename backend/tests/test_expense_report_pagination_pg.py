"""Native non-superuser expense page reads; no financial lifecycle mutation."""

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core import tenant
from app.core.database import get_session
from app.main import app
from tests.test_expense_report_pagination import (
    BASE,
    assert_report_pages,
    prepare_populations,
    report_state,
    seed_reports,
)

PG_URL = os.environ.get("RLS_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not PG_URL, reason="requires migrated non-superuser PostgreSQL")


async def register(client):
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "organization_name": "Synthetic native expense pages",
            "name": "Synthetic native report owner",
            "email": f"native-expense-pages-{uuid4()}@example.com",
            "password": "Synthetic-Expense-Pages-Password-32",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return body, {"Authorization": "Bearer " + body["token"]["access_token"]}


@pytest_asyncio.fixture
async def expense_pages_pg(monkeypatch):
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
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as owner:
            _, headers = await register(owner)
            owner.headers.update(headers)
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as anonymous:
                populations = await prepare_populations(owner, anonymous, factory)
                yield owner, anonymous, factory, populations
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
        await engine.dispose()


@pytest.mark.asyncio
async def test_native_tied201_own101_approval_and_employee_pages_have_no_financial_writes(
    expense_pages_pg,
):
    owner, anonymous, factory, (org_id, owner_id, own, pending, employee) = expense_pages_pg
    before = await report_state(factory, org_id)
    assert len(before["expense_reports"]) == 302
    await assert_report_pages(owner, own, params={"mine": "true"})
    await assert_report_pages(owner, pending, params={"status": "submitted"})
    await assert_report_pages(
        anonymous, pending, params={"employee_id": owner_id}, headers=employee
    )
    after = await report_state(factory, org_id)
    assert after == before
    folder = os.environ.get("EXPENSE_PAGE_ARTIFACT_DIR")
    if folder:
        Path(folder).mkdir(parents=True, exist_ok=True)
        Path(folder, "native-report-pages.json").write_text(
            json.dumps(
                {
                    "scope": "Declared synthetic registration/invite/module/population fixtures; measured API journey contains only read requests.",
                    "org_id": org_id,
                    "own_ordered_ids": [row.id for row in own],
                    "approval_ordered_ids": [row.id for row in pending],
                    "before": before,
                    "after": after,
                },
                indent=2,
                default=str,
            )
            + "\n"
        )


@pytest.mark.asyncio
async def test_native_foreign_tenant_is_opaque_and_unauthenticated_read_refused(expense_pages_pg):
    owner, anonymous, factory, (org_id, _owner_id, _own, _pending, _employee) = expense_pages_pg
    before = await report_state(factory, org_id)
    identity, headers = await register(anonymous)
    assert (
        await anonymous.put("/api/v1/modules/expenses", headers=headers, json={"enabled": True})
    ).status_code == 200
    foreign = identity["organization"]["id"]
    token = tenant.set_current_org(foreign)
    try:
        async with factory() as db:
            rows = await seed_reports(db, foreign, identity["user"]["id"], 3)
    finally:
        tenant.reset_current_org(token)
    result = await anonymous.get(BASE, headers=headers)
    assert result.status_code == 200 and result.json()["total"] == 3
    assert [row["id"] for row in result.json()["items"]] == [row.id for row in rows]
    assert (await anonymous.get(BASE)).status_code == 401
    assert await report_state(factory, org_id) == before
