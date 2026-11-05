"""Bound metadata pages, complete facets, stable ties and unchanged legacy clients."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models import Base
from app.models.document import Document
from app.services import document_registry

BASE = "/api/v1/documents"


async def seed_registry(db, org_id, count=203, *, kind=None):
    ids = sorted((str(uuid4()) for _ in range(count)), reverse=True)
    rows = [
        Document(
            id=identity,
            org_id=org_id,
            sha256=f"{index:064x}",
            size=index + 1,
            kind=kind or ("receipts" if index % 2 else "logos"),
            filename=f"synthetic-{index:03d}.png",
            mime="image/png",
            created_at=datetime(2026, 11, 5, tzinfo=UTC),
        )
        for index, identity in enumerate(ids)
    ]
    db.add_all(list(reversed(rows)))
    await db.commit()
    return rows


async def registry_state(factory, org_id):
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
            for name in ("documents", "invoices", "expense_items", "jobs", "audit_events")
        }


async def assert_registry_pages(client, rows):
    expected = [row.id for row in rows]
    for size in (1, 37, 100, 200):
        found = []
        for offset in range(0, len(rows), size):
            response = await client.get(f"{BASE}/page", params={"limit": size, "offset": offset})
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["total"] == len(rows)
            assert body["kinds"] == ["logos", "receipts"]
            assert len(body["items"]) <= size
            found.extend(item["id"] for item in body["items"])
            for item in body["items"]:
                original = rows[expected.index(item["id"])]
                assert item["filename"] == original.filename
                assert item["sha256"] == original.sha256
                assert item["size"] == original.size
        assert found == expected and len(set(found)) == len(rows)
    beyond = await client.get(f"{BASE}/page", params={"offset": len(rows) + 1})
    assert beyond.json() == {"items": [], "total": len(rows), "kinds": ["logos", "receipts"]}


@pytest.mark.asyncio
async def test_complete_stable_registry_pages_are_read_only(auth_client, db_session, _db):
    org_id = (await auth_client.get("/api/v1/auth/me")).json()["organization"]["id"]
    rows = await seed_registry(db_session, org_id)
    before = await registry_state(_db, org_id)
    await assert_registry_pages(auth_client, rows)
    legacy = await auth_client.get(BASE)
    assert isinstance(legacy.json(), list)
    assert {row["id"] for row in legacy.json()} == {row.id for row in rows}
    assert await registry_state(_db, org_id) == before


@pytest.mark.asyncio
async def test_filtered_page_keeps_all_kind_choices_and_exact_filtered_total(
    auth_client, db_session
):
    org_id = (await auth_client.get("/api/v1/auth/me")).json()["organization"]["id"]
    rows = await seed_registry(db_session, org_id)
    response = await auth_client.get(f"{BASE}/page", params={"kind": "receipts", "limit": 37})
    body = response.json()
    assert body["total"] == sum(row.kind == "receipts" for row in rows)
    assert body["kinds"] == ["logos", "receipts"]
    assert [item["id"] for item in body["items"]] == [
        row.id for row in rows if row.kind == "receipts"
    ][:37]
    assert (await auth_client.get(f"{BASE}/page?kind=unknown")).json() == {
        "items": [],
        "total": 0,
        "kinds": ["logos", "receipts"],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query", ["limit=0", "limit=201", "offset=-1", "limit=bad", "kind=" + "x" * 25]
)
async def test_document_page_rejects_invalid_bounds(auth_client, query):
    assert (await auth_client.get(f"{BASE}/page?{query}")).status_code == 422


@pytest.mark.asyncio
async def test_document_page_permission_and_empty_scope(client, role_client):
    assert (await client.get(f"{BASE}/page")).status_code == 401
    employee = await role_client("user")
    assert (await employee.get(f"{BASE}/page")).status_code == 403
    owner = await role_client("owner")
    assert (await owner.get(f"{BASE}/page")).json() == {"items": [], "total": 0, "kinds": []}


@pytest.mark.asyncio
async def test_registry_page_rows_counts_and_facets_never_include_foreign_tenant(
    auth_client, role_client, db_session
):
    org_id = (await auth_client.get("/api/v1/auth/me")).json()["organization"]["id"]
    rows = await seed_registry(db_session, org_id, count=3)
    foreign = await role_client("owner")
    other = (await foreign.get("/api/v1/auth/me")).json()["organization"]["id"]
    await seed_registry(db_session, other, count=17, kind="foreign-only")
    body = (await auth_client.get(f"{BASE}/page")).json()
    assert body["total"] == 3 and body["kinds"] == ["logos", "receipts"]
    assert [item["id"] for item in body["items"]] == [row.id for row in rows]


@pytest.mark.asyncio
async def test_registry_page_materializes_only_requested_rows_with_flat_queries(
    auth_client, db_session
):
    org_id = (await auth_client.get("/api/v1/auth/me")).json()["organization"]["id"]
    await seed_registry(db_session, org_id)
    for size in (1, 37, 100):
        statements = []

        def observe(_connection, _cursor, statement, *_args, captured=statements):
            if statement.lstrip().upper().startswith("SELECT") and "FROM documents" in statement:
                captured.append(statement)

        event.listen(db_session.bind.sync_engine, "before_cursor_execute", observe)
        try:
            response = await auth_client.get(f"{BASE}/page", params={"limit": size})
        finally:
            event.remove(db_session.bind.sync_engine, "before_cursor_execute", observe)
        assert response.status_code == 200 and len(response.json()["items"]) == size
        assert response.json()["total"] == 203
        assert len(statements) == 3, statements
        assert sum("LIMIT" in statement for statement in statements) == 1
        assert sum("SELECT DISTINCT" in statement for statement in statements) == 1


@pytest.mark.asyncio
async def test_registry_page_never_flushes_its_callers_pending_work(auth_client, db_session):
    org_id = (await auth_client.get("/api/v1/auth/me")).json()["organization"]["id"]
    await seed_registry(db_session, org_id, count=3)
    factory = async_sessionmaker(db_session.bind, autoflush=True)
    async with factory() as db:
        invalid_pending = Document(org_id=org_id, sha256=None, size=1, kind="receipts")
        db.add(invalid_pending)
        rows, total, kinds = await document_registry.page_for_org(
            db, org_id, kind=None, limit=1, offset=0
        )
        assert len(rows) == 1 and total == 3 and kinds == ["logos", "receipts"]
        assert invalid_pending in db.new
        await db.rollback()
