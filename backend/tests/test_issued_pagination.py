"""Issued lists retain exact bounded population and deterministic page order."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import event, select

from app.models import Base
from app.models.issued_invoice import IssuedInvoice
from app.models.issuer import IssuerProfile
from app.services import issued_status
from tests.test_issued_lifecycle import _activate

BASE = "/api/v1/issued"
STAMP = datetime(2026, 11, 5, 12, tzinfo=UTC)


async def seed_issued_page(db, org_id, count=105):
    profiles = [
        IssuerProfile(
            org_id=org_id,
            legal_name=f"Synthetic page issuer {index}",
            invoice_prefix=f"PAGE{index}",
            credit_note_prefix=f"PC{index}",
        )
        for index in range(2)
    ]
    db.add_all(profiles)
    await db.flush()
    rows = []
    for index in range(count):
        paid = index % 3 == 0
        row = IssuedInvoice(
            id=str(uuid4()),
            org_id=org_id,
            issuer_id=profiles[index % 2].id,
            number=f"SYN-PAGE-{index:03d}",
            buyer_name=f"Synthetic page buyer {index:03d}",
            seller_json="{}",
            issue_date=date(2026, 5 if index % 2 == 0 else 6, 10),
            due_date=date(2099, 1, 1),
            currency="EUR" if index % 2 == 0 else "USD",
            subtotal=Decimal("37.29"),
            tax_total=Decimal("0.00"),
            total=Decimal("37.29"),
            amount_paid=Decimal("37.29") if paid else Decimal("0.00"),
            created_at=STAMP,
        )
        rows.append(row)
    # Reverse physical insertion makes the old timestamp-only order a concrete
    # wrong page order instead of relying on a lucky backend tie disposition.
    db.add_all(sorted(rows, key=lambda row: row.id, reverse=True))
    await db.commit()
    return sorted(rows, key=lambda row: row.id)


async def issued_state(factory, org_id):
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
                "issued_invoices",
                "issuer_profiles",
                "audit_events",
                "jobs",
                "webhook_deliveries",
            )
        }


async def assert_bounded_pages(client, expected, db_engine):
    expected_ids = [row.id for row in expected]
    for size in (1, 37, 100, 500):
        actual = []
        for page in range(1, (len(expected_ids) + size - 1) // size + 1):
            queries = []

            def observe(_connection, _cursor, statement, *_args, captured=queries):
                if (
                    statement.lstrip().lower().startswith("select")
                    and "from issued_invoices" in statement.lower()
                ):
                    captured.append(statement)

            event.listen(db_engine, "before_cursor_execute", observe)
            try:
                response = await client.get(BASE, params={"page": page, "page_size": size})
            finally:
                event.remove(db_engine, "before_cursor_execute", observe)
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["total"] == len(expected_ids)
            assert len(body["items"]) <= size
            assert len(queries) == 2, queries
            actual.extend(row["id"] for row in body["items"])
            source = {row.id: row for row in expected}
            for row in body["items"]:
                original = source[row["id"]]
                assert row["issuer_id"] == original.issuer_id
                assert row["currency"] == original.currency
                assert row["total"] == "37.29"
                assert row["status"] == issued_status.status_of(original)
        assert actual == expected_ids
        assert len(actual) == len(set(actual))


@pytest.mark.asyncio
async def test_equal_timestamp_pages_reach_every_invoice_once_with_exact_totals(
    auth_client, db_session, _db
):
    await _activate(auth_client)
    org_id = (await auth_client.get("/api/v1/auth/me")).json()["user"]["org_id"]
    rows = await seed_issued_page(db_session, org_id)
    before = await issued_state(_db, org_id)
    await assert_bounded_pages(auth_client, rows, db_session.bind.sync_engine)
    beyond = await auth_client.get(BASE, params={"page": 3, "page_size": 100})
    assert beyond.status_code == 200 and beyond.json() == {"items": [], "total": 105}
    assert await issued_state(_db, org_id) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["page=0", "page=-1", "page_size=0", "page_size=501", "page=bad"])
async def test_existing_page_bounds_are_enforced(auth_client, query):
    assert (await auth_client.get(f"{BASE}?{query}")).status_code == 422


@pytest.mark.asyncio
async def test_tenant_empty_and_primary_chronology_are_preserved(
    auth_client, db_session, role_client
):
    await _activate(auth_client)
    org_id = (await auth_client.get("/api/v1/auth/me")).json()["user"]["org_id"]
    rows = await seed_issued_page(db_session, org_id)
    newest = rows[-1]
    newest.created_at = datetime(2026, 11, 6, 12, tzinfo=UTC)
    await db_session.commit()
    other = await role_client("owner")
    assert (await other.get(BASE)).json() == {"items": [], "total": 0}
    page = (await auth_client.get(f"{BASE}?page=1&page_size=100")).json()
    assert page["total"] == 105 and page["items"][0]["id"] == newest.id
    assert [row["id"] for row in page["items"][1:]] == [row.id for row in rows[:-1]][:99]


@pytest.mark.asyncio
async def test_zip_filters_keep_global_population_after_opening_a_later_list_page(
    auth_client, db_session, monkeypatch
):

    await _activate(auth_client)
    org_id = (await auth_client.get("/api/v1/auth/me")).json()["user"]["org_id"]
    rows = await seed_issued_page(db_session, org_id)
    await assert_global_zip_scope(auth_client, rows, monkeypatch)


async def assert_global_zip_scope(auth_client, rows, monkeypatch):
    from app.api.routes import issued as route

    later = (await auth_client.get(f"{BASE}?page=2&page_size=100")).json()["items"]
    rendered = []

    async def pdf(_db, _org, invoice):
        rendered.append(invoice.id)
        return b"%PDF-1.4\nDeclared synthetic renderer; this test verifies export selection only."

    monkeypatch.setattr(route, "_render_pdf", pdf)
    response = await auth_client.get(f"{BASE}/export.zip?from=2026-05-01&to=2026-05-31&status=paid")
    assert response.status_code == 200, response.text
    expected = {
        row.id
        for row in rows
        if row.issue_date.month == 5 and issued_status.status_of(row) == "paid"
    }
    assert set(rendered) == expected
    assert expected - {row["id"] for row in later}
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["scope"]["status"] == "paid"
        assert len(archive.namelist()) >= len(expected)
