"""Payment-run history pages preserve legacy lists and exact scoped counts."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import event, select

from app.models import Base
from app.models.invoice import Invoice
from app.models.payment_run import PaymentRun
from app.models.vendor import Vendor

BASE = "/api/v1/payment-runs"


async def seed_run_history(db, org_id, count=201):
    ids = sorted(str(uuid4()) for _ in range(count))
    rows = [
        PaymentRun(
            id=ids[index - 1],
            org_id=org_id,
            reference=f"SYN-RUN-{index:03d}",
            total_eur=Decimal("37.29"),
            status=("open", "approved", "paid", "cancelled")[index % 4],
            created_at=datetime(2026, 5, 1, tzinfo=UTC),
        )
        for index in range(1, count + 1)
    ]
    db.add_all(list(reversed(rows)))
    vendor = Vendor(org_id=org_id, name="Synthetic history supplier")
    db.add(vendor)
    await db.flush()
    for index, run in enumerate((rows[0], rows[-1], rows[-1])):
        db.add(
            Invoice(
                org_id=org_id,
                vendor_id=vendor.id,
                invoice_number=f"SYN-COUNT-{index}",
                issue_date=datetime(2026, 5, 1).date(),
                total=Decimal("37.29"),
                payment_run_id=run.id,
            )
        )
    await db.commit()
    return rows


async def run_history_state(factory, org_id):
    async with factory() as db:
        return {
            name: [
                dict(row)
                for row in (
                    await db.execute(
                        select(Base.metadata.tables[name])
                        .where(Base.metadata.tables[name].c.org_id == org_id)
                        .order_by(Base.metadata.tables[name].c.id)
                    )
                ).mappings()
            ]
            for name in (
                "payment_runs",
                "invoices",
                "supplier_payments",
                "audit_events",
                "jobs",
                "webhook_deliveries",
            )
        }


async def assert_run_pages(client, rows):
    expected = [row.id for row in rows]
    for size in (37, 200, 500):
        found = []
        for offset in range(0, len(rows), size):
            response = await client.get(f"{BASE}/page", params={"limit": size, "offset": offset})
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["total"] == len(rows) and len(body["items"]) <= size
            found.extend(row["id"] for row in body["items"])
            for row in body["items"]:
                assert row["total_eur"] == "37.29"
                assert row["status"] == rows[expected.index(row["id"])].status
                assert row["invoice_count"] == (
                    1 if row["id"] == expected[0] else 2 if row["id"] == expected[-1] else 0
                )
        assert found == expected and len(found) == len(set(found))
    beyond = await client.get(f"{BASE}/page", params={"offset": len(rows) + 10})
    assert beyond.json() == {"items": [], "total": len(rows)}


@pytest.mark.asyncio
async def test_201st_run_is_reachable_with_exact_count_and_no_writes(auth_client, db_session, _db):
    org_id = (await auth_client.get("/api/v1/auth/me")).json()["organization"]["id"]
    rows = await seed_run_history(db_session, org_id)
    before = await run_history_state(_db, org_id)
    await assert_run_pages(auth_client, rows)
    # No legacy client changes shape or loses its existing limit/offset behavior.
    legacy = await auth_client.get(BASE)
    assert isinstance(legacy.json(), list) and len(legacy.json()) == 200
    tail = await auth_client.get(BASE, params={"limit": 200, "offset": 200})
    assert [row["id"] for row in tail.json()] == [rows[-1].id]
    assert await run_history_state(_db, org_id) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["limit=0", "limit=501", "offset=-1", "limit=bad"])
async def test_run_page_bounds_remain_enforced(auth_client, query):
    assert (await auth_client.get(f"{BASE}/page?{query}")).status_code == 422


@pytest.mark.asyncio
async def test_run_page_empty_scope_and_permission_fences(client, role_client):
    reader = await role_client("user_free")
    assert (await reader.get(f"{BASE}/page")).json() == {"items": [], "total": 0}
    employee = await role_client("user")
    assert (await employee.get(f"{BASE}/page")).status_code == 403
    assert (await client.get(f"{BASE}/page")).status_code == 401


@pytest.mark.asyncio
async def test_run_page_count_and_rows_never_include_other_tenant(
    auth_client, role_client, db_session
):
    own = (await auth_client.get("/api/v1/auth/me")).json()["organization"]["id"]
    own_rows = await seed_run_history(db_session, own, count=3)
    other = await role_client("owner")
    foreign = (await other.get("/api/v1/auth/me")).json()["organization"]["id"]
    await seed_run_history(db_session, foreign, count=7)
    response = await auth_client.get(f"{BASE}/page")
    assert response.json()["total"] == 3
    assert [row["id"] for row in response.json()["items"]] == [row.id for row in own_rows]


@pytest.mark.asyncio
async def test_run_page_financial_queries_are_flat_and_bounded(auth_client, db_session):
    org_id = (await auth_client.get("/api/v1/auth/me")).json()["organization"]["id"]
    await seed_run_history(db_session, org_id)
    for size in (1, 37, 200):
        statements = []

        def observe(_connection, _cursor, statement, *_args, captured=statements):
            if statement.lstrip().upper().startswith("SELECT") and any(
                "FROM " + table in statement for table in ("payment_runs", "invoices")
            ):
                captured.append(statement)

        event.listen(db_session.bind.sync_engine, "before_cursor_execute", observe)
        try:
            response = await auth_client.get(f"{BASE}/page", params={"limit": size})
        finally:
            event.remove(db_session.bind.sync_engine, "before_cursor_execute", observe)
        assert response.status_code == 200 and len(response.json()["items"]) == size
        assert response.json()["total"] == 201
        assert len(statements) == 3, statements
        assert sum("LIMIT" in statement for statement in statements) == 1
        assert sum("GROUP BY" in statement for statement in statements) == 1
