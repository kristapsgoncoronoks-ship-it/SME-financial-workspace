"""Pinned166 migration controls use fresh UUID databases, never the current DB."""

from __future__ import annotations

import asyncio
import os
from contextlib import contextmanager
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from tests.test_migrations import BACKEND_DIR, _run_alembic
from tests.test_pg_migration_schema_contract import PrivateDatabaseURL
from tests.test_planning_ocr_migration_integration import schemas
from tests.test_residual_parent_tenant_links import LINKS, _rows, _seed

HEAD = "e2f4a6b8d0c3"
PARENT = "d1e3f5a7c9b2"
SINGLES = {
    ("customer_contacts", "customer_id"): "customer_contacts_customer_id_fkey",
    ("inbound_invoices", "invoice_id"): "inbound_invoices_invoice_id_fkey",
    ("issued_invoices", "customer_id"): "fk_issued_invoices_customer",
    ("issued_invoices", "corrected_invoice_id"): "fk_issued_corrected",
}
SECURED = ("customers", "customer_contacts", "invoices", "inbound_invoices", "issued_invoices")


def _revision():
    config = Config()
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return ScriptDirectory.from_config(config).get_revision(HEAD).module


def _apply(connection, direction, migration=None):
    with Operations.context(MigrationContext.configure(connection)):
        getattr(migration or _revision(), direction)()


def _database(url, action):
    async def run():
        engine = create_async_engine(url)
        try:
            # The reflected synthetic seed commits, only in this UUID sandbox.
            async with engine.connect() as connection:
                return await connection.run_sync(action)
        finally:
            await engine.dispose()

    return asyncio.run(run())


async def _scope(db, org):
    await db.execute(text("SELECT set_config('app.current_org', :org, true)"), {"org": org})


async def _security(db):
    assert (
        await db.execute(
            text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user")
        )
    ).one() == (False, False)
    for table in SECURED:
        assert (
            await db.execute(
                text(
                    "SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid=to_regclass(:table)"
                ),
                {"table": table},
            )
        ).one() == (True, True)


@contextmanager
def _owned_database(revision, *, historical):
    admin_url = os.environ.get("MIGRATION_TEST_ADMIN_URL")
    app_url = os.environ.get("RLS_TEST_DATABASE_URL")
    if not admin_url or not app_url:
        pytest.skip("requires explicit owned-database admin and non-superuser app URLs")
    import asyncpg

    admin_url = PrivateDatabaseURL(admin_url)
    app = make_url(app_url)
    assert app.username and app.username.replace("_", "").isalnum()
    name = ("residual166_" if historical else "residual_current_") + uuid4().hex

    async def manage(create):
        admin = await asyncpg.connect(admin_url)
        try:
            await admin.execute(
                f'CREATE DATABASE "{name}" OWNER "{app.username}"'
                if create
                else f'DROP DATABASE "{name}" WITH (FORCE)'
            )
        finally:
            await admin.close()

    asyncio.run(manage(True))
    url = PrivateDatabaseURL(app.set(database=name).render_as_string(hide_password=False))
    try:

        def identity(connection):
            assert connection.scalar(text("SELECT current_database()")) == name
            assert connection.execute(
                text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user")
            ).one() == (False, False)
            assert connection.scalar(text("SELECT to_regclass('public.alembic_version') IS NULL"))

        _database(url, identity)
        upgraded = _run_alembic(["upgrade", PARENT if historical else revision], url)
        assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
        identities = _database(url, _seed)
        old_rows, old_schema = _database(url, _rows), _database(url, schemas)
        if historical:
            from tests.test_residual_parent_migration import _expected_added_schema

            assert (
                _database(url, lambda c: c.scalar(text("SELECT version_num FROM alembic_version")))
                == PARENT
            )
            upgraded = _run_alembic(["upgrade", revision], url)
            assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
            assert _database(url, _rows) == old_rows
            assert _database(url, schemas) == _expected_added_schema(old_schema, postgres=True)
        initial_rows, initial_schema = _database(url, _rows), _database(url, schemas)

        def final(connection):
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == revision
            assert _rows(connection) == initial_rows
            assert schemas(connection) == initial_schema

        _database(url, final)
        try:
            yield url, identities, old_rows, old_schema
        finally:
            _database(url, final)
    finally:
        asyncio.run(manage(False))


@pytest.fixture(scope="module")
def historical_database():
    assert _revision().revision == HEAD and _revision().down_revision == PARENT
    with _owned_database(HEAD, historical=True) as family:
        yield family


@pytest_asyncio.fixture
async def historical_family(historical_database):
    url, identities, _, _ = historical_database
    engine = create_async_engine(url)
    try:
        async with engine.connect() as db:
            outer = await db.begin()
            await _scope(db, "")
            await _security(db)
            before, schema = await db.run_sync(_rows), await db.run_sync(schemas)
            try:
                yield db, identities
            finally:
                await outer.rollback()
            # DDL rollback can invalidate asyncpg's cached row descriptors.
            await db.invalidate()
            await _scope(db, "")
            assert await db.run_sync(_rows) == before
            assert await db.run_sync(schemas) == schema
            await db.rollback()
    finally:
        await engine.dispose()


def test_actual166_cli_roundtrip_preserves_all_rows_catalog_security_and_marker(
    historical_database,
):
    url, _, old_rows, old_schema = historical_database
    initial = _database(url, schemas)
    for direction, target in (("downgrade", PARENT), ("upgrade", HEAD)):
        result = _run_alembic([direction, target], url)
        assert result.returncode == 0, result.stdout + result.stderr
        assert (
            _database(url, lambda c: c.scalar(text("SELECT version_num FROM alembic_version")))
            == target
        )
        assert _database(url, _rows) == old_rows
        assert _database(url, schemas) == (old_schema if target == PARENT else initial)


@pytest.mark.parametrize("model,column,parent,parent_key,child_key,name,nullable", LINKS)
async def test_hidden_bad_history_global_preflight_is_masked_atomic_and_preserves_unscoped_state(
    historical_family, model, column, parent, parent_key, child_key, name, nullable
):
    db, (a, b) = historical_family
    await db.run_sync(_apply, "downgrade")
    await _scope(db, b["org"])
    await db.execute(
        text(f'UPDATE "{model.__tablename__}" SET "{column}"=:parent WHERE id=:id'),
        {"parent": a[parent_key], "id": b[child_key]},
    )
    await _scope(db, "")
    before, schema = await db.run_sync(_rows), await db.run_sync(schemas)
    assert await db.scalar(text("SELECT current_setting('app.current_org',true)")) == ""
    with pytest.raises(RuntimeError, match="wrong_tenant=1") as caught:
        await db.run_sync(_apply, "upgrade")
    message = str(caught.value)
    assert model.__tablename__ in message
    assert all(value not in message for identity in (a, b) for value in identity.values())
    assert await db.scalar(text("SELECT current_setting('app.current_org',true)")) == ""
    await _scope(db, "")
    assert await db.run_sync(_rows) == before and await db.run_sync(schemas) == schema


async def test_clean_unscoped_roundtrip_preserves_scope_without_changing_force_rls(
    historical_family,
):
    db, _ = historical_family
    before, schema = await db.run_sync(_rows), await db.run_sync(schemas)
    for direction in ("downgrade", "upgrade"):
        await db.run_sync(_apply, direction)
        assert await db.scalar(text("SELECT current_setting('app.current_org',true)")) == ""
    await _security(db)
    assert await db.run_sync(_rows) == before and await db.run_sync(schemas) == schema


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
async def test_scoped_connection_is_refused_before_table_locks_or_ddl(historical_family, direction):
    from sqlalchemy import event

    db, (a, _b) = historical_family
    if direction == "upgrade":
        await db.run_sync(_apply, "downgrade")
    before, schema = await db.run_sync(_rows), await db.run_sync(schemas)
    await _scope(db, a["org"])
    statements = []

    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(db.sync_connection, "before_cursor_execute", observe)
    try:
        with pytest.raises(RuntimeError, match="unscoped"):
            await db.run_sync(_apply, direction)
    finally:
        event.remove(db.sync_connection, "before_cursor_execute", observe)
    assert not any(
        statement.lstrip().upper().startswith(("LOCK TABLE", "ALTER ", "CREATE ", "DROP "))
        for statement in statements
    )
    assert await db.scalar(text("SELECT current_setting('app.current_org',true)")) == a["org"]
    await _scope(db, "")
    assert await db.run_sync(_rows) == before and await db.run_sync(schemas) == schema


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
@pytest.mark.parametrize("fault", ["fourth_replacement", "postvalidation"])
async def test_late_failure_restores_full_catalog_data_and_original_scope(
    historical_family, monkeypatch, direction, fault
):
    db, _ = historical_family
    if direction == "upgrade":
        await db.run_sync(_apply, "downgrade")
    migration = _revision()
    calls = []
    if fault == "fourth_replacement":
        original = migration._replace_fk

        def fail(connection, spec, *, upgrade):
            original(connection, spec, upgrade=upgrade)
            calls.append(spec.column)
            if spec.column == "corrected_invoice_id":
                raise RuntimeError("Synthetic fourth residual replacement failure")

        monkeypatch.setattr(migration, "_replace_fk", fail)
    else:
        original = migration._check_installed

        def fail(connection, *, upgrade):
            original(connection, upgrade=upgrade)
            calls.append(upgrade)
            if len(calls) == 2:
                raise RuntimeError("Synthetic residual postvalidation failure")

        monkeypatch.setattr(migration, "_check_installed", fail)
    before, schema = await db.run_sync(_rows), await db.run_sync(schemas)
    await _scope(db, "")
    with pytest.raises(RuntimeError, match="Synthetic"):
        await db.run_sync(_apply, direction, migration)
    assert len(calls) == (4 if fault == "fourth_replacement" else 2)
    assert await db.scalar(text("SELECT current_setting('app.current_org',true)")) == ""
    await _scope(db, "")
    assert await db.run_sync(_rows) == before and await db.run_sync(schemas) == schema


@pytest.mark.parametrize(
    "model,column,parent,parent_key,child_key,name,nullable", [x for x in LINKS if x[-1]]
)
@pytest.mark.parametrize("drift", ["renamed", "cascade", "deferred"])
async def test_retained_single_name_action_and_timing_drift_refuse_before_ddl(
    historical_family, model, column, parent, parent_key, child_key, name, nullable, drift
):
    db, _ = historical_family
    before, schema = await db.run_sync(_rows), await db.run_sync(schemas)
    boundary = await db.begin_nested()
    table, single = model.__tablename__, SINGLES[model.__tablename__, column]
    try:
        definition = await db.scalar(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid=to_regclass(:table) AND conname=:name"
            ),
            {"table": table, "name": single},
        )
        assert definition is not None
        if drift == "renamed":
            await db.execute(
                text(
                    f'ALTER TABLE "{table}" RENAME CONSTRAINT "{single}" TO synthetic_renamed_single'
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
                    f'ALTER TABLE "{table}" DROP CONSTRAINT "{single}", ADD CONSTRAINT "{single}" {changed}'
                )
            )
        changed_rows, changed_schema = await db.run_sync(_rows), await db.run_sync(schemas)
        with pytest.raises(RuntimeError, match="contract"):
            await db.run_sync(_apply, "downgrade")
        assert await db.run_sync(_rows) == changed_rows
        assert await db.run_sync(schemas) == changed_schema
    finally:
        await boundary.rollback()
    assert await db.run_sync(_rows) == before and await db.run_sync(schemas) == schema


@pytest.mark.parametrize("parent", ["customers", "invoices", "issued_invoices"])
async def test_exact_existing_unique_constraint_target_is_not_adopted_by_shape(
    historical_family, parent
):
    db, _ = historical_family
    before, schema = await db.run_sync(_rows), await db.run_sync(schemas)
    boundary = await db.begin_nested()
    try:
        await db.execute(
            text(
                f'ALTER TABLE "{parent}" RENAME CONSTRAINT "uq_{parent}_org_id" TO synthetic_renamed_target'
            )
        )
        changed_schema = await db.run_sync(schemas)
        with pytest.raises(
            RuntimeError,
            match=rf"Expected {parent} named immediate unique constraint is absent or incompatible",
        ):
            await db.run_sync(_apply, "downgrade")
        assert await db.run_sync(_rows) == before
        assert await db.run_sync(schemas) == changed_schema
    finally:
        await boundary.rollback()
    assert await db.run_sync(_rows) == before and await db.run_sync(schemas) == schema


@pytest.mark.parametrize("table", SECURED)
async def test_ordered_preflight_excludes_concurrent_parent_and_child_writes(
    historical_family, historical_database, table
):
    db, _ = historical_family
    await db.run_sync(_revision()._prepare)
    url, *_ = historical_database
    engine = create_async_engine(url)
    try:
        async with engine.connect() as writer:
            with pytest.raises(DBAPIError) as caught:
                async with writer.begin():
                    await writer.execute(text("SET LOCAL lock_timeout='250ms'"))
                    await writer.execute(text(f'UPDATE "{table}" SET id=id WHERE false'))
            assert caught.value.orig.sqlstate == "55P03"
    finally:
        await engine.dispose()


async def test_fresh_unscoped_connection_retains_exact_none_or_empty_scope(
    historical_database, record_testsuite_property
):
    url, *_ = historical_database
    engine = create_async_engine(url)
    try:
        async with engine.connect() as db:
            outer = await db.begin()
            initial_scope = await db.scalar(text("SELECT current_setting('app.current_org',true)"))
            assert initial_scope in (None, "")
            record_testsuite_property("residual_fresh_native_scope", repr(initial_scope))
            before, schema = await db.run_sync(_rows), await db.run_sync(schemas)
            try:
                for direction in ("downgrade", "upgrade"):
                    await db.run_sync(_apply, direction)
                    assert (
                        await db.scalar(text("SELECT current_setting('app.current_org',true)"))
                        == initial_scope
                    )
                assert await db.run_sync(_rows) == before and await db.run_sync(schemas) == schema
            finally:
                await outer.rollback()
    finally:
        await engine.dispose()


@pytest.mark.parametrize("model,column,parent,parent_key,child_key,name,nullable", LINKS)
async def test_actual_preexisting_pair_is_refused_without_guard_adoption_or_ddl(
    historical_family, model, column, parent, parent_key, child_key, name, nullable
):
    db, _ = historical_family
    await db.run_sync(_apply, "downgrade")
    before, schema = await db.run_sync(_rows), await db.run_sync(schemas)
    boundary = await db.begin_nested()
    try:
        action = f"SET NULL ({column})" if nullable else "CASCADE"
        await db.execute(
            text(
                f'ALTER TABLE "{model.__tablename__}" ADD CONSTRAINT "{name}" '
                f'FOREIGN KEY(org_id,"{column}") REFERENCES "{parent}"(org_id,id) '
                f"ON DELETE {action}"
            )
        )
        changed_schema = await db.run_sync(schemas)
        with pytest.raises(RuntimeError, match="no adoption performed"):
            await db.run_sync(_apply, "upgrade")
        assert await db.run_sync(_rows) == before
        assert await db.run_sync(schemas) == changed_schema
    finally:
        await boundary.rollback()
    assert await db.run_sync(_rows) == before and await db.run_sync(schemas) == schema


@pytest.mark.parametrize("model,column,parent,parent_key,child_key,name,nullable", LINKS)
async def test_actual_retained_single_retargeting_is_refused_before_any_replacement(
    historical_family, model, column, parent, parent_key, child_key, name, nullable
):
    from tests.test_planning_ocr_migration_integration import _insert

    db, _ = historical_family
    await db.run_sync(_apply, "downgrade")
    before, schema = await db.run_sync(_rows), await db.run_sync(schemas)
    boundary = await db.begin_nested()
    table, single = model.__tablename__, SINGLES[model.__tablename__, column]
    try:
        # Matching IDs in the wrong target table keep this synthetic FK fully
        # validated. The refusal must identify target drift, not bad history or
        # a NOT VALID/deferred shortcut.
        references = list(
            (
                await db.execute(
                    text(
                        f'SELECT DISTINCT "{column}"::text FROM "{table}" WHERE "{column}" IS NOT NULL'
                    )
                )
            ).scalars()
        )
        assert references
        for identifier in references:
            assert (
                await db.scalar(
                    text("SELECT count(*) FROM organizations WHERE id=:id"), {"id": identifier}
                )
                == 0
            )
            await db.run_sync(
                lambda connection, identifier=identifier: _insert(
                    connection, "organizations", id=identifier, name="Synthetic wrong FK target"
                )
            )
        action = "SET NULL" if nullable else "CASCADE"
        await db.execute(
            text(
                f'ALTER TABLE "{table}" DROP CONSTRAINT "{single}", '
                f'ADD CONSTRAINT "{single}" FOREIGN KEY("{column}") '
                f"REFERENCES organizations(id) ON DELETE {action}"
            )
        )
        assert (
            await db.scalar(
                text(
                    "SELECT convalidated AND NOT condeferrable AND NOT condeferred "
                    "FROM pg_constraint WHERE conrelid=to_regclass(:table) AND conname=:name"
                ),
                {"table": table, "name": single},
            )
            is True
        )
        changed_rows, changed_schema = await db.run_sync(_rows), await db.run_sync(schemas)
        with pytest.raises(RuntimeError, match="retained single foreign-key contract"):
            await db.run_sync(_apply, "upgrade")
        assert await db.run_sync(_rows) == changed_rows
        assert await db.run_sync(schemas) == changed_schema
    finally:
        await boundary.rollback()
    assert await db.run_sync(_rows) == before and await db.run_sync(schemas) == schema
