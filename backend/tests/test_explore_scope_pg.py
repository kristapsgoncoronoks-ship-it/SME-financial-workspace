"""Real PostgreSQL bounded Explore cut, tenant opacity, and read-only downloads."""

import csv
import hashlib
import io
import json
import os
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic.script import ScriptDirectory
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, event, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core import tenant
from app.core.config import settings
from app.core.database import get_session
from app.main import app
from app.models import Base
from app.models.audit import AuditEvent
from app.models.invoice import Invoice, LineItem
from app.models.organization import Organization
from tests.test_explore import _mk
from tests.test_explore_scope import seed_groups

PG_URL = os.environ.get("RLS_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not PG_URL, reason="requires migrated non-superuser PostgreSQL")


@pytest_asyncio.fixture
async def explore_pg(monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", None)
    engine = create_async_engine(PG_URL, pool_size=2, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    previous = dict(app.dependency_overrides)
    owned = []

    async def session():
        async with factory() as db:
            yield db

    app.dependency_overrides[get_session] = session
    try:
        async with factory() as db:
            assert (
                await db.execute(
                    text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user")
                )
            ).one() == (False, False)
            assert (
                await db.scalar(text("SELECT version_num FROM alembic_version"))
                == ScriptDirectory(
                    str(Path(__file__).resolve().parents[1] / "alembic")
                ).get_current_head()
            )
            assert (
                await db.execute(
                    text(
                        "SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE relname='invoices'"
                    )
                )
            ).one() == (True, True)
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
        ) as client:

            async def register():
                response = await client.post(
                    "/api/v1/auth/register",
                    json={
                        "organization_name": "Synthetic Explore native",
                        "name": "Synthetic analyst",
                        "email": f"explore-{uuid4()}@example.com",
                        "password": "Synthetic-Explore-Test-Password-42",
                    },
                )
                assert response.status_code == 201, response.text
                body = response.json()
                owned.append(body["organization"]["id"])
                client.headers["Authorization"] = "Bearer " + body["token"]["access_token"]
                return owned[-1]

            yield client, factory, engine, register
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
        try:
            for org_id in owned:
                token = tenant.set_current_org(org_id)
                try:
                    async with factory.begin() as db:
                        await db.execute(delete(AuditEvent).where(AuditEvent.org_id == org_id))
                        await db.execute(delete(Organization).where(Organization.id == org_id))
                        assert (
                            await db.scalar(
                                select(Organization.id).where(Organization.id == org_id)
                            )
                            is None
                        )
                finally:
                    tenant.reset_current_org(token)
        finally:
            await engine.dispose()


async def state(factory, org_id):
    token = tenant.set_current_org(org_id)
    try:
        async with factory() as db:
            result = {}
            for name in (
                "invoices",
                "vendors",
                "audit_events",
                "payments",
                "bank_lines",
                "bank_statements",
                "payment_runs",
                "supplier_payments",
                "expense_reports",
                "expense_items",
                "issued_invoices",
                "receipts",
                "jobs",
            ):
                table = Base.metadata.tables[name]
                rows = [
                    dict(row)
                    for row in (
                        await db.execute(
                            select(table).where(table.c.org_id == org_id).order_by(table.c.id)
                        )
                    ).mappings()
                ]
                result[name] = {
                    "count": len(rows),
                    "sha256": hashlib.sha256(
                        json.dumps(rows, sort_keys=True, default=str).encode()
                    ).hexdigest(),
                }
            rows = [
                dict(row)
                for row in (
                    await db.execute(
                        select(LineItem.__table__)
                        .join(Invoice, Invoice.id == LineItem.invoice_id)
                        .where(Invoice.org_id == org_id)
                        .order_by(LineItem.id)
                    )
                ).mappings()
            ]
            result["line_items"] = {
                "count": len(rows),
                "sha256": hashlib.sha256(
                    json.dumps(rows, sort_keys=True, default=str).encode()
                ).hexdigest(),
            }
            return result
    finally:
        tenant.reset_current_org(token)


@pytest.mark.asyncio
async def test_native_bounded_groups_filters_download_and_no_writes(explore_pg):
    client, factory, engine, register = explore_pg
    foreign = await register()
    await _mk(client, "Foreign synthetic", "FOREIGN-EXPLORE", "2026-01-10", "foreign-only", "99999")
    own = await register()
    token = tenant.set_current_org(own)
    try:
        async with factory() as db:
            await seed_groups(client, db, 1001)
    finally:
        tenant.reset_current_org(token)
    before = {org: await state(factory, org) for org in (own, foreign)}
    grouped = []

    def record(_conn, _cursor, statement, parameters, _context, _many):
        if "GROUP BY" in statement and "line_items" in statement:
            grouped.append((statement, parameters))

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    started = perf_counter()
    try:
        for limit in (100, 1000):
            response = await client.get(
                f"/api/v1/analytics/explore?dim=category&limit={limit}&currency=EUR"
            )
            assert response.status_code == 200, response.text
            result = response.json()
            assert result["truncated"] is True and result["returned_rows"] == limit
            assert result["rows"] == [
                {"category": f"group-{i - 1:04}", "value": f"{i}.00"}
                for i in range(1001, 1001 - limit, -1)
            ]
            assert response.headers["x-explore-limit"] == str(limit)
            assert response.headers["x-explore-truncated"] == "true"
            csv_response = await client.get(
                f"/api/v1/analytics/explore?dim=category&limit={limit}&currency=EUR&format=csv"
            )
            assert csv_response.status_code == 200
            assert list(csv.reader(io.StringIO(csv_response.text))) == [
                ["Category", "Net spend"]
            ] + [[row["category"], row["value"]] for row in result["rows"]]
        for filters in (
            "category=group-0000",
            "currency=USD",
            "status=pending",
            "start=2027-01-01",
            "category=foreign-only",
        ):
            response = await client.get(f"/api/v1/analytics/explore?dim=category&{filters}")
            assert response.status_code == 200, response.text
            result = response.json()
            assert result["truncated"] is False
            assert result["rows"] == (
                [{"category": "group-0000", "value": "1.00"}]
                if filters == "category=group-0000"
                else []
            )
    finally:
        elapsed = perf_counter() - started
        event.remove(engine.sync_engine, "before_cursor_execute", record)
    assert len(grouped) == 9
    assert all(
        "LIMIT" in statement and (101 in parameters or 1001 in parameters)
        for statement, parameters in grouped
    )
    assert {org: await state(factory, org) for org in (own, foreign)} == before
    folder = os.environ.get("EXPLORE_ARTIFACT_DIR")
    if folder:
        Path(folder).mkdir(parents=True, exist_ok=True)
        Path(folder, "native-scope.json").write_text(
            json.dumps(
                {
                    "postgres_grouped_queries": len(grouped),
                    "max_fetch": 1001,
                    "fixture_own_groups": 1001,
                    "fixture_foreign_groups": 1,
                    "measured_request_seconds": elapsed,
                    "snapshots_unchanged": before,
                    "role_super": False,
                    "role_bypass_rls": False,
                    "invoices_force_rls": True,
                },
                indent=2,
            )
            + "\n"
        )
