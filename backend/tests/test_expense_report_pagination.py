"""Existing expense pages preserve exact populations and deterministic ties."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core import tenant
from app.models import Base
from app.models.expense import ExpenseReport
from tests.test_expenses import _member

BASE = "/api/v1/expenses"
STATE_TABLES = (
    "expense_reports",
    "expense_items",
    "expense_transactions",
    "expense_import_batches",
    "expense_commands",
    "reimbursement_batches",
    "receipts",
    "payments",
    "supplier_payments",
    "documents",
    "document_versions",
    "audit_events",
    "jobs",
)


async def seed_reports(db, org_id, employee_id, count, *, status="draft"):
    identifiers = sorted(str(uuid4()) for _ in range(count))
    rows = [
        ExpenseReport(
            id=identifier,
            org_id=org_id,
            employee_id=employee_id,
            employee_name="Synthetic report employee",
            title=f"Synthetic report {index + 1}",
            status=status,
            currency="USD" if index == count - 1 else "EUR",
            total=Decimal("123.45"),
            vat_total=Decimal("23.45"),
            total_eur=None if index == count - 1 else Decimal("123.45"),
            created_at=datetime(2026, 11, 1, tzinfo=UTC),
            submitted_at=datetime(2026, 11, 1, tzinfo=UTC) if status == "submitted" else None,
            version=1,
        )
        for index, identifier in enumerate(identifiers)
    ]
    # Opposite insertion order proves the tie is not accidental insertion order.
    db.add_all(list(reversed(rows)))
    await db.commit()
    return rows


async def report_state(factory, org_id):
    token = tenant.set_current_org(org_id)
    try:
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
                for name in STATE_TABLES
            }
    finally:
        tenant.reset_current_org(token)


async def assert_report_pages(client, rows, *, params, headers=None):
    expected = [row.id for row in rows]
    for size in (37, 100, 500):
        found = []
        for page in range(1, (len(rows) + size - 1) // size + 1):
            response = await client.get(
                BASE, params={**params, "page": page, "page_size": size}, headers=headers
            )
            assert response.status_code == 200, response.text
            body = response.json()
            assert set(body) == {"items", "total"}
            assert body["total"] == len(rows) and len(body["items"]) <= size
            found.extend(row["id"] for row in body["items"])
            for row in body["items"]:
                original = rows[expected.index(row["id"])]
                assert row["employee_id"] == original.employee_id
                assert row["status"] == original.status and row["version"] == 1
                assert row["total"] == "123.45" and row["vat_total"] == "23.45"
                assert row["currency"] == original.currency
                assert row["total_eur"] == (None if original.currency == "USD" else "123.45")
        assert found == expected and len(found) == len(set(found))
    tail = await client.get(BASE, params={**params, "page": 999, "page_size": 100}, headers=headers)
    assert tail.json() == {"items": [], "total": len(rows)}


async def prepare_populations(owner, anonymous, factory):
    identity = (await owner.get("/api/v1/auth/me")).json()
    org_id, owner_id = identity["organization"]["id"], identity["user"]["id"]
    assert (await owner.put("/api/v1/modules/expenses", json={"enabled": True})).status_code == 200
    member_token = await _member(owner, anonymous, f"expense-pages-{uuid4()}@example.com")
    member_headers = {"Authorization": "Bearer " + member_token}
    member = (await anonymous.get("/api/v1/auth/me", headers=member_headers)).json()["user"]
    token = tenant.set_current_org(org_id)
    try:
        async with factory() as db:
            own = await seed_reports(db, org_id, owner_id, 201)
            pending = await seed_reports(db, org_id, member["id"], 101, status="submitted")
    finally:
        tenant.reset_current_org(token)
    return org_id, owner_id, own, pending, member_headers


@pytest.mark.asyncio
async def test_tied_201_own_and101_approval_pages_preserve_filters_state_and_employee_scope(
    auth_client, client, _db
):
    org_id, owner_id, own, pending, employee = await prepare_populations(auth_client, client, _db)
    before = await report_state(_db, org_id)
    assert len(before["expense_reports"]) == 302
    await assert_report_pages(auth_client, own, params={"mine": "true"})
    await assert_report_pages(auth_client, pending, params={"status": "submitted"})
    await assert_report_pages(client, pending, params={"mine": "true"}, headers=employee)
    # A regular employee cannot expand the read to a requested other employee.
    await assert_report_pages(client, pending, params={"employee_id": owner_id}, headers=employee)
    default = (await auth_client.get(BASE, params={"mine": "true"})).json()
    assert len(default["items"]) == 100 and default["total"] == 201
    assert await report_state(_db, org_id) == before


@pytest.mark.asyncio
async def test_report_pages_keep_foreign_tenant_count_and_ids_opaque(auth_client, role_client, _db):
    own_identity = (await auth_client.get("/api/v1/auth/me")).json()
    foreign = await role_client("owner")
    foreign_identity = (await foreign.get("/api/v1/auth/me")).json()
    for client in [auth_client, foreign]:
        assert (
            await client.put("/api/v1/modules/expenses", json={"enabled": True})
        ).status_code == 200
    async with _db() as db:
        own = await seed_reports(
            db, own_identity["organization"]["id"], own_identity["user"]["id"], 3
        )
        other = await seed_reports(
            db, foreign_identity["organization"]["id"], foreign_identity["user"]["id"], 7
        )
    before = await report_state(_db, own_identity["organization"]["id"])
    result = (await auth_client.get(BASE, params={"page": 1})).json()
    assert result["total"] == 3 and [row["id"] for row in result["items"]] == [
        row.id for row in own
    ]
    response = (await foreign.get(BASE)).json()
    assert response["total"] == 7 and [row["id"] for row in response["items"]] == [
        row.id for row in other
    ]
    assert await report_state(_db, own_identity["organization"]["id"]) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["page=0", "page=-1", "page=bad", "page_size=0", "page_size=501"])
async def test_report_page_existing_bounds_remain_enforced(auth_client, query):
    assert (await auth_client.get(BASE + "?" + query)).status_code == 422
