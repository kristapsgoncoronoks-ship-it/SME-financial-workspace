"""Historical161/162 operations run only in a database at actual162."""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import inspect, text, update
from sqlalchemy.engine import make_url

from tests.test_migrations import _run_alembic
from tests.test_optional_parent_migration import _all_rows
from tests.test_optional_parent_migration_pg import _database
from tests.test_partner_webhook_parent_migrations import PARTNER, WEBHOOK, _revision
from tests.test_partner_webhook_parent_tenant import _link
from tests.test_partner_webhook_parent_tenant_pg import (
    _apply,
    _catalog,
    _rows,
    _scope,
    parent_family,
)
from tests.test_pg_migration_schema_contract import PrivateDatabaseURL
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
    name = "mandatory_parent162_" + uuid4().hex

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
        assert WEBHOOK == "a8b0d2f4c6e9"
        upgraded = _run_alembic(["upgrade", WEBHOOK], url)
        assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
        initial_schema, initial_rows = _database(url, schemas), _database(url, _all_rows)

        def exact162(connection):
            assert set(
                connection.execute(text("SELECT version_num FROM alembic_version")).scalars()
            ) == {WEBHOOK}
            assert connection.execute(
                text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user")
            ).one() == (False, False)
            inspector = inspect(connection)
            for revision in (PARTNER, WEBHOOK):
                migration = _revision(revision)
                pairs = [
                    fk
                    for fk in inspector.get_foreign_keys(migration.CHILD_TABLE)
                    if fk["name"] == migration.COMPOSITE_FK
                ]
                assert len(pairs) == 1
                assert pairs[0]["constrained_columns"] == ["org_id", migration.LINK_COLUMN]
                assert pairs[0]["referred_columns"] == ["org_id", "id"]
                assert pairs[0]["options"] == {"ondelete": "CASCADE"}
            for table, name in (
                ("expense_transactions", "fk_expense_transactions_item"),
                ("issued_invoices", "fk_issued_invoices_partner"),
                ("recurring_invoices", "fk_recurring_invoices_partner"),
                ("vendor_change_requests", "fk_vendor_change_requests_source_document"),
            ):
                assert all(fk["name"] != name for fk in inspector.get_foreign_keys(table))
            for parent in ("expense_items", "documents"):
                assert all(
                    index["name"] != f"uq_{parent}_org_id"
                    for index in inspector.get_indexes(parent)
                )
            assert schemas(connection) == initial_schema
            assert _all_rows(connection) == initial_rows

        _database(url, exact162)
        try:
            yield url
        finally:
            _database(url, exact162)
    finally:
        asyncio.run(manage(False))


@pytest_asyncio.fixture(params=["partner", "webhook"])
async def historical_parent_pg(request, historical_parent_url):
    async with parent_family(historical_parent_url, request.param) as family:
        yield family


@pytest.mark.asyncio
async def test_native_populated_roundtrip_preserves_rows_catalog_and_optional_contracts(
    historical_parent_pg,
):
    _engine, db, ids, migration, kind = historical_parent_pg
    parent, child, _column = _link(kind)
    before, schema = await _rows(db, parent, child, ids), await _catalog(db)
    for direction in ("downgrade", "upgrade"):
        await db.run_sync(
            lambda connection, direction=direction: _apply(connection, migration, direction)
        )
        assert await _rows(db, parent, child, ids) == before
    assert await _catalog(db) == schema


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["wrong_tenant", "missing_parent"])
async def test_native_unscoped_preflight_masks_global_history_and_never_repairs(
    historical_parent_pg, fault
):
    _engine, db, ids, migration, kind = historical_parent_pg
    parent, child, column = _link(kind)
    await db.run_sync(lambda connection: _apply(connection, migration, "downgrade"))
    missing = str(uuid4())
    if fault == "missing_parent":
        await db.execute(
            text(f"ALTER TABLE {migration.CHILD_TABLE} DROP CONSTRAINT {migration.OLD_FK}")
        )
    for letter, foreign in (("a", "b"), ("b", "a")):
        await db.execute(
            update(child)
            .where(child.id == ids[f"child_{letter}"])
            .values(**{column: missing if fault == "missing_parent" else ids[f"parent_{foreign}"]})
        )
    before, schema = await _rows(db, parent, child, ids), await _catalog(db)
    expected = "wrong_tenant=2" if fault == "wrong_tenant" else "missing_parent=2"
    with pytest.raises(RuntimeError, match=expected) as refused:
        await db.run_sync(lambda connection: _apply(connection, migration, "upgrade"))
    assert all(value not in str(refused.value) for value in (*ids.values(), missing))
    assert await _rows(db, parent, child, ids) == before and await _catalog(db) == schema


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
async def test_native_postvalidation_failure_rolls_back_ddl_inside_outer_transaction(
    historical_parent_pg, monkeypatch, direction
):
    _engine, db, ids, migration, kind = historical_parent_pg
    parent, child, _column = _link(kind)
    if direction == "upgrade":
        await db.run_sync(lambda connection: _apply(connection, migration, "downgrade"))
    before, schema = await _rows(db, parent, child, ids), await _catalog(db)
    original, calls = migration._check_sqlite_foreign_keys, []

    def fail_after_ddl(connection):
        original(connection)
        calls.append(True)
        if len(calls) == 2:
            raise RuntimeError("Synthetic late validation failure")

    monkeypatch.setattr(migration, "_check_sqlite_foreign_keys", fail_after_ddl)
    with pytest.raises(RuntimeError, match="Synthetic late validation failure"):
        await db.run_sync(lambda connection: _apply(connection, migration, direction))
    assert len(calls) == 2
    assert await _rows(db, parent, child, ids) == before and await _catalog(db) == schema


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
async def test_native_scoped_migration_refuses_before_any_change(historical_parent_pg, direction):
    _engine, db, ids, migration, kind = historical_parent_pg
    parent, child, _column = _link(kind)
    if direction == "upgrade":
        await db.run_sync(lambda connection: _apply(connection, migration, "downgrade"))
    before, schema = await _rows(db, parent, child, ids), await _catalog(db)
    await _scope(db, ids["org_a"])
    with pytest.raises(RuntimeError, match="requires an unscoped connection"):
        await db.run_sync(lambda connection: _apply(connection, migration, direction))
    await _scope(db, "")
    assert await _rows(db, parent, child, ids) == before and await _catalog(db) == schema


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "policy",
    [
        "ON DELETE RESTRICT",
        "ON DELETE CASCADE ON UPDATE CASCADE",
        "ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED",
    ],
)
async def test_native_unexpected_existing_fk_contract_is_refused(historical_parent_pg, policy):
    _engine, db, ids, migration, kind = historical_parent_pg
    parent, child, _column = _link(kind)
    await db.run_sync(lambda connection: _apply(connection, migration, "downgrade"))
    await db.execute(
        text(f"ALTER TABLE {migration.CHILD_TABLE} DROP CONSTRAINT {migration.OLD_FK}")
    )
    await db.execute(
        text(
            f"ALTER TABLE {migration.CHILD_TABLE} ADD CONSTRAINT {migration.OLD_FK} FOREIGN KEY ({migration.LINK_COLUMN}) REFERENCES {migration.PARENT_TABLE}(id) {policy}"
        )
    )
    before, schema = await _rows(db, parent, child, ids), await _catalog(db)
    with pytest.raises(RuntimeError, match="Unexpected .* foreign-key contract"):
        await db.run_sync(lambda connection: _apply(connection, migration, "upgrade"))
    assert await _rows(db, parent, child, ids) == before and await _catalog(db) == schema
