"""Historical159 migration checks own a fresh database at exactly that revision.

Current-head tenant tests may run at160 or later. These two rollback tests must
not remove a parent index required by those later, correctly installed FKs.
"""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import inspect, text, update
from sqlalchemy.engine import make_url

from app.models.expense import ExpenseItem
from tests.test_expense_item_parent_tenant_pg import (
    _run_migration,
    _scope,
    _snapshot,
    migration,
    parent_family,
)
from tests.test_migrations import _run_alembic
from tests.test_pg_migration_schema_contract import PrivateDatabaseURL, database
from tests.test_planning_ocr_migration_integration import schemas


@pytest.fixture(scope="module")
def historical_parent_url():
    admin_url = os.environ.get("MIGRATION_TEST_ADMIN_URL")
    app_url = os.environ.get("RLS_TEST_DATABASE_URL")
    if not admin_url or not app_url:
        pytest.skip("requires explicit disposable-database admin and non-superuser app URLs")
    import asyncpg

    admin_url = PrivateDatabaseURL(admin_url)
    app = make_url(app_url)
    assert app.username and app.username.replace("_", "").isalnum()
    name = "expense_parent159_" + uuid4().hex

    async def manage(create):
        admin = await asyncpg.connect(admin_url)
        try:
            if create:
                await admin.execute(f'CREATE DATABASE "{name}" OWNER "{app.username}"')
            else:
                await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        finally:
            await admin.close()

    asyncio.run(manage(True))
    url = PrivateDatabaseURL(app.set(database=name).render_as_string(hide_password=False))
    try:
        upgraded = _run_alembic(["upgrade", migration.revision], url)
        assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr

        def exact_historical_shape(connection):
            assert set(
                connection.execute(text("SELECT version_num FROM alembic_version")).scalars()
            ) == {"d5e7f9a1b3c6"}
            inspector = inspect(connection)
            item_keys = [
                key
                for key in inspector.get_foreign_keys("expense_items")
                if key["referred_table"] == "expense_reports"
            ]
            assert len(item_keys) == 1
            assert item_keys[0]["name"] == migration.COMPOSITE_FK
            assert item_keys[0]["constrained_columns"] == ["org_id", "report_id"]
            assert item_keys[0]["referred_columns"] == ["org_id", "id"]
            assert item_keys[0]["options"] == {"ondelete": "CASCADE"}
            target_indexes = [
                index
                for index in inspector.get_indexes("expense_reports")
                if index["name"] == migration.TARGET_INDEX
            ]
            assert target_indexes == [
                {
                    "name": "uq_expense_reports_org_id",
                    "unique": True,
                    "column_names": ["org_id", "id"],
                    "dialect_options": {"postgresql_include": []},
                }
            ]
            for table in ("expense_comments", "expense_approval_steps"):
                report_keys = [
                    key
                    for key in inspector.get_foreign_keys(table)
                    if key["referred_table"] == "expense_reports"
                ]
                assert len(report_keys) == 1
                key = report_keys[0]
                assert key["name"] == f"{table}_report_id_fkey"
                assert key["constrained_columns"] == ["report_id"]
                assert key["referred_columns"] == ["id"]
                assert key["options"] == {"ondelete": "CASCADE"}
                assert all(
                    candidate["name"] != f"fk_{table}_report"
                    for candidate in inspector.get_foreign_keys(table)
                )

        database(url, exact_historical_shape)
        initial_catalog = database(url, schemas)
        try:
            yield url
        finally:
            database(url, exact_historical_shape)
            assert database(url, schemas) == initial_catalog
    finally:
        asyncio.run(manage(False))


@pytest_asyncio.fixture
async def historical_parent_pg(historical_parent_url):
    async with parent_family(historical_parent_url) as family:
        yield family


async def test_native_migration_roundtrip_preserves_all_existing_rows_and_links(
    historical_parent_pg,
):
    _, db, ids = historical_parent_pg
    await _scope(db, "")
    before = await _snapshot(db, ids)
    before_catalog = await db.run_sync(schemas)
    await db.run_sync(_run_migration, "downgrade")
    await db.run_sync(_run_migration, "upgrade")
    assert await _snapshot(db, ids) == before
    assert await db.run_sync(schemas) == before_catalog


async def test_native_preflight_sees_other_tenants_and_refuses_without_ddl_or_repair(
    historical_parent_pg,
):
    _, db, ids = historical_parent_pg
    await _scope(db, "")
    await db.run_sync(_run_migration, "downgrade")
    await _scope(db, ids["org_b"])
    await db.execute(
        update(ExpenseItem.__table__)
        .where(ExpenseItem.id == ids["item_b"])
        .values(report_id=ids["report_a"])
    )
    await _scope(db, "")
    before = await _snapshot(db, ids)
    before_catalog = await db.run_sync(schemas)
    with pytest.raises(RuntimeError, match="missing_parent=0, wrong_tenant=1") as caught:
        await db.run_sync(_run_migration, "upgrade")
    assert all(value not in str(caught.value) for value in ids.values())
    assert await _snapshot(db, ids) == before
    indexes = await db.run_sync(
        lambda connection: inspect(connection).get_indexes("expense_reports")
    )
    assert all(index["name"] != migration.TARGET_INDEX for index in indexes)
    fks = await db.run_sync(
        lambda connection: inspect(connection).get_foreign_keys("expense_items")
    )
    assert any(fk["constrained_columns"] == ["report_id"] for fk in fks)
    assert await db.run_sync(schemas) == before_catalog
