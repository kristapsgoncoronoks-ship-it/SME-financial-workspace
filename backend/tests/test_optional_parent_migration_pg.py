"""Actual163/164 native migration behavior in freshly owned UUID databases."""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from tests.test_migrations import _run_alembic
from tests.test_optional_parent_migration import (
    HEAD,
    PARENT,
    _all_rows,
    _apply,
    _expected_added_schema,
    _revision,
    _seed,
)
from tests.test_pg_migration_schema_contract import PrivateDatabaseURL
from tests.test_planning_ocr_migration_integration import schemas

# Independent expected business links; source registries are not test expectations.
LINKS = (
    (
        "expense_transactions",
        "item_id",
        "expense_items",
        "item",
        "transaction",
        "fk_expense_transactions_item",
        "expense_transactions_item_id_fkey",
    ),
    (
        "issued_invoices",
        "partner_id",
        "partners",
        "partner",
        "invoice",
        "fk_issued_invoices_partner",
        "fk_issued_partner",
    ),
    (
        "recurring_invoices",
        "partner_id",
        "partners",
        "partner",
        "schedule",
        "fk_recurring_invoices_partner",
        "recurring_invoices_partner_id_fkey",
    ),
    (
        "vendor_change_requests",
        "source_document_id",
        "documents",
        "document",
        "request",
        "fk_vendor_change_requests_source_document",
        "vendor_change_requests_source_document_id_fkey",
    ),
)
SECURED = (
    "expense_transactions",
    "issued_invoices",
    "recurring_invoices",
    "vendor_change_requests",
    "expense_items",
    "partners",
    "documents",
    "expense_reports",
)


def _database(url, action):
    async def run():
        engine = create_async_engine(url)
        try:
            # _seed commits multiple synthetic families. Never wrap it in an
            # engine.begin context whose transaction would then be closed.
            async with engine.connect() as connection:
                return await connection.run_sync(action)
        finally:
            await engine.dispose()

    return asyncio.run(run())


async def _scope(db, org):
    await db.execute(text("SELECT set_config('app.current_org', :org, true)"), {"org": org})


async def _role_and_rls(db):
    assert (
        await db.execute(
            text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user")
        )
    ).one() == (False, False)
    for table in SECURED:
        assert (
            await db.execute(
                text(
                    "SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid=CAST(:table AS regclass)"
                ),
                {"table": table},
            )
        ).one() == (True, True)


@pytest.fixture(scope="module")
def optional_database():
    admin_url = os.environ.get("MIGRATION_TEST_ADMIN_URL")
    app_url = os.environ.get("RLS_TEST_DATABASE_URL")
    if not admin_url or not app_url:
        pytest.skip("requires explicit disposable-database admin and non-superuser app URLs")
    import asyncpg

    admin_url = PrivateDatabaseURL(admin_url)
    app = make_url(app_url)
    assert app.username and app.username.replace("_", "").isalnum()
    name = "optional_parent164_" + uuid4().hex

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
        assert (
            _revision().revision == "c0d2e4f6a8b1" and _revision().down_revision == "b9c1e3f5a7d0"
        )
        upgraded = _run_alembic(["upgrade", PARENT], url)
        assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr

        def seed_historical(connection):
            assert set(
                connection.execute(text("SELECT version_num FROM alembic_version")).scalars()
            ) == {PARENT}
            assert connection.execute(
                text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user")
            ).one() == (False, False)
            return _seed(connection)

        identities = _database(url, seed_historical)
        before_rows, before_schema = _database(url, _all_rows), _database(url, schemas)
        upgraded = _run_alembic(["upgrade", HEAD], url)
        assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
        initial_schema = _expected_added_schema(before_schema, postgres=True)

        def exact164(connection):
            assert set(
                connection.execute(text("SELECT version_num FROM alembic_version")).scalars()
            ) == {HEAD}
            assert _all_rows(connection) == before_rows
            assert schemas(connection) == initial_schema

        _database(url, exact164)
        try:
            yield url, identities
        finally:
            _database(url, exact164)
    finally:
        asyncio.run(manage(False))


@pytest_asyncio.fixture
async def optional_family(optional_database):
    url, identities = optional_database
    engine = create_async_engine(url)
    try:
        async with engine.connect() as db:
            transaction = await db.begin()
            try:
                await _scope(db, "")
                await _role_and_rls(db)
                before = await db.run_sync(_all_rows)
                yield db, identities
            finally:
                await transaction.rollback()
            await _scope(db, "")
            assert await db.run_sync(_all_rows) == before
            await db.rollback()
    finally:
        await engine.dispose()


def _apply_frozen(connection, migration, direction):
    with Operations.context(MigrationContext.configure(connection)):
        getattr(migration, direction)()


async def test_actual_roundtrip_preserves_complete_schema_security_and_history(optional_family):
    db, _ = optional_family
    before, schema = await db.run_sync(_all_rows), await db.run_sync(schemas)
    await db.run_sync(_apply, "downgrade")
    parent_schema = await db.run_sync(schemas)
    assert schema == _expected_added_schema(parent_schema, postgres=True)
    assert await db.run_sync(_all_rows) == before
    await db.run_sync(_apply, "upgrade")
    assert await db.run_sync(schemas) == schema
    assert await db.run_sync(_all_rows) == before
    assert set((await db.execute(text("SELECT version_num FROM alembic_version"))).scalars()) == {
        HEAD
    }


@pytest.mark.parametrize("table,column,parent,parent_key,child_key,name,old_name", LINKS)
async def test_global_preflight_refuses_other_tenant_history_without_ddl_or_repair(
    optional_family, table, column, parent, parent_key, child_key, name, old_name
):
    db, (a, b) = optional_family
    await db.run_sync(_apply, "downgrade")
    await _scope(db, b["org"])
    await db.execute(
        text(f'UPDATE "{table}" SET "{column}"=:parent WHERE id=:id'),
        {"parent": a[parent_key], "id": b[child_key]},
    )
    await _scope(db, "")
    before, schema = await db.run_sync(_all_rows), await db.run_sync(schemas)
    with pytest.raises(RuntimeError, match=f"{table}: missing_parent=0, wrong_tenant=1") as caught:
        await db.run_sync(_apply, "upgrade")
    assert all(value not in str(caught.value) for identity in (a, b) for value in identity.values())
    assert await db.run_sync(_all_rows) == before
    assert await db.run_sync(schemas) == schema


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
async def test_scoped_connection_refuses_before_ddl(optional_family, direction):
    db, (a, _b) = optional_family
    if direction == "upgrade":
        await db.run_sync(_apply, "downgrade")
    await _scope(db, "")
    before, schema = await db.run_sync(_all_rows), await db.run_sync(schemas)
    await _scope(db, a["org"])
    with pytest.raises(RuntimeError, match="requires an unscoped connection"):
        await db.run_sync(_apply, direction)
    await _scope(db, "")
    assert await db.run_sync(_all_rows) == before
    assert await db.run_sync(schemas) == schema


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
@pytest.mark.parametrize("fault", ["fourth_pair", "postvalidation"])
async def test_late_ddl_failure_is_atomic_before_outer_rollback(
    optional_family, monkeypatch, direction, fault
):
    db, _ = optional_family
    if direction == "upgrade":
        await db.run_sync(_apply, "downgrade")
    migration = _revision()
    calls = []
    if fault == "fourth_pair":
        original = migration._change_pair

        def fail(connection, link, *, create):
            original(connection, link, create=create)
            calls.append(True)
            if len(calls) == 4:
                raise RuntimeError("Synthetic fourth pair failure")

        monkeypatch.setattr(migration, "_change_pair", fail)
    else:
        original = migration._check_sqlite_foreign_keys

        def fail(connection):
            original(connection)
            calls.append(True)
            if len(calls) == 2:
                raise RuntimeError("Synthetic native postvalidation failure")

        monkeypatch.setattr(migration, "_check_sqlite_foreign_keys", fail)
    before, schema = await db.run_sync(_all_rows), await db.run_sync(schemas)
    with pytest.raises(RuntimeError, match="Synthetic"):
        await db.run_sync(_apply_frozen, migration, direction)
    assert len(calls) == (4 if fault == "fourth_pair" else 2)
    assert await db.run_sync(_all_rows) == before
    assert await db.run_sync(schemas) == schema


@pytest.mark.parametrize("table,column,parent,parent_key,child_key,name,old_name", LINKS)
@pytest.mark.parametrize("drift", ["cascade", "deferred", "renamed"])
async def test_frozen_migration_refuses_retained_single_drift_before_ddl(
    optional_family, table, column, parent, parent_key, child_key, name, old_name, drift
):
    db, _ = optional_family
    original_schema = await db.run_sync(schemas)
    original_rows = await db.run_sync(_all_rows)
    boundary = await db.begin_nested()
    try:
        definition = await db.scalar(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid=CAST(:table AS regclass) AND conname=:name"
            ),
            {"table": table, "name": old_name},
        )
        if drift == "renamed":
            await db.execute(
                text(
                    f'ALTER TABLE "{table}" RENAME CONSTRAINT "{old_name}" TO "synthetic_renamed_single"'
                )
            )
        else:
            changed = (
                definition.replace("SET NULL", "CASCADE")
                if drift == "cascade"
                else definition + " DEFERRABLE INITIALLY DEFERRED"
            )
            await db.execute(
                text(
                    f'ALTER TABLE "{table}" DROP CONSTRAINT "{old_name}", ADD CONSTRAINT "{old_name}" {changed}'
                )
            )
        before, changed_schema = await db.run_sync(_all_rows), await db.run_sync(schemas)
        with pytest.raises(
            RuntimeError, match="retained single foreign-key contract|native foreign-key contract"
        ):
            await db.run_sync(_apply, "downgrade")
        assert await db.run_sync(_all_rows) == before
        assert await db.run_sync(schemas) == changed_schema
    finally:
        await boundary.rollback()
    assert await db.run_sync(schemas) == original_schema
    assert await db.run_sync(_all_rows) == original_rows


@pytest.mark.parametrize(
    "table",
    [
        "documents",
        "expense_items",
        "partners",
        "expense_transactions",
        "issued_invoices",
        "recurring_invoices",
        "vendor_change_requests",
    ],
)
async def test_native_preflight_excludes_writes_to_every_parent_and_child(
    optional_family, optional_database, table
):
    from sqlalchemy.exc import DBAPIError

    db, _ = optional_family
    await db.run_sync(_revision()._prepare)
    url, _ = optional_database
    writer_engine = create_async_engine(url)
    try:
        async with writer_engine.connect() as writer:
            with pytest.raises(DBAPIError) as caught:
                async with writer.begin():
                    await writer.execute(text("SET LOCAL lock_timeout = '250ms'"))
                    await writer.execute(text(f'UPDATE "{table}" SET id=id WHERE false'))
            assert caught.value.orig.sqlstate == "55P03"
    finally:
        await writer_engine.dispose()
