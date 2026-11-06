"""Development bootstrap DDL on disposable PostgreSQL, separately from migrations.

These model-created schemas intentionally have no migration-only RLS policies.
They prove composite-key and deletion behavior, not tenant-selected visibility.
Only explicit admin/app URLs permit creating and dropping UUID databases.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from sqlalchemy import event, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from app.core.migration_schema import postgresql_comparison_metadata
from app.models import Base
from tests.test_pg_migration_schema_contract import (
    PrivateDatabaseURL,
    database,
    metadata_signature,
    seed_delete_family,
    snapshot,
)

# Independent expectations from the committed DB-017/018 deletion policy.
LINKS = [
    ("expense_items", "project_id", "projects", "fk_expense_items_project"),
    ("invoices", "project_id", "projects", "fk_invoices_project"),
    ("issued_invoices", "project_id", "projects", "fk_issued_invoices_project"),
    ("issued_invoices", "issuer_id", "issuer_profiles", "fk_issued_invoices_issuer"),
]


def _bootstrap_database(*, projected):
    admin_url = os.environ.get("MIGRATION_TEST_ADMIN_URL")
    app_url = os.environ.get("RLS_TEST_DATABASE_URL")
    if not admin_url or not app_url:
        pytest.skip("requires explicit disposable-database admin and non-superuser app URLs")
    import asyncpg

    app = make_url(app_url)
    assert app.username and app.username.replace("_", "").isalnum()
    name = "development_bootstrap_" + uuid.uuid4().hex

    async def manage(create):
        admin = await asyncpg.connect(admin_url)
        try:
            if create:
                assert not await admin.fetchval("SELECT 1 FROM pg_database WHERE datname=$1", name)
                await admin.execute(f'CREATE DATABASE "{name}" OWNER "{app.username}"')
            else:
                await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
                assert not await admin.fetchval("SELECT 1 FROM pg_database WHERE datname=$1", name)
        finally:
            await admin.close()

    asyncio.run(manage(True))
    url = PrivateDatabaseURL(app.set(database=name).render_as_string(hide_password=False))
    try:

        def create(connection):
            assert (
                connection.scalar(
                    text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user")
                )
                is False
            )
            assert inspect(connection).get_table_names() == []
            if projected:
                from app.core.development_schema import development_metadata

                development_metadata(Base.metadata, connection.dialect).create_all(connection)
            else:
                Base.metadata.create_all(connection)
            assert set(inspect(connection).get_table_names()) == set(Base.metadata.tables)
            assert "alembic_version" not in inspect(connection).get_table_names()
            # Bootstrap is not a replacement for migrated RLS/schema ownership.
            assert connection.scalar(text("SELECT count(*) FROM pg_policies")) == 0

        database(url, create)
        yield url
    finally:
        asyncio.run(manage(False))


@pytest.fixture(scope="module")
def bootstrap_url():
    yield from _bootstrap_database(projected=True)


@pytest.fixture(scope="module")
def legacy_bootstrap_url():
    yield from _bootstrap_database(projected=False)


@pytest.mark.parametrize("table,column,parent,name", LINKS)
def test_bootstrap_catalog_has_exact_validated_column_specific_action(
    bootstrap_url, table, column, parent, name
):
    def verify(connection):
        fk = next(fk for fk in inspect(connection).get_foreign_keys(table) if fk["name"] == name)
        assert fk["constrained_columns"] == ["org_id", column]
        assert fk["referred_table"] == parent
        assert fk["referred_columns"] == ["org_id", "id"]
        assert fk["options"]["ondelete"] == f"SET NULL ({column})"
        catalog = connection.execute(
            text(
                "SELECT c.confdeltype::text,c.convalidated,"
                "ARRAY(SELECT a.attname::text FROM unnest(c.confdelsetcols) "
                "WITH ORDINALITY AS n(attnum,pos) JOIN pg_attribute a "
                "ON a.attrelid=c.conrelid AND a.attnum=n.attnum ORDER BY n.pos) "
                "FROM pg_constraint c WHERE c.conrelid=to_regclass(:table) AND c.conname=:name"
            ),
            {"table": table, "name": name},
        ).one()
        assert catalog == ("n", True, [column])
        assert connection.execute(
            text(
                "SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid=to_regclass(:table)"
            ),
            {"table": table},
        ).one() == (False, False)

    database(bootstrap_url, verify)


@pytest.mark.parametrize("table,column,parent,name", LINKS)
def test_bootstrap_parent_deletion_preserves_full_rows_tenant_keys_and_other_references(
    bootstrap_url, table, column, parent, name
):
    before = database(bootstrap_url, snapshot)

    def exercise(connection):
        boundary = connection.begin_nested()
        try:
            child = Base.metadata.tables[table]
            parent_table = Base.metadata.tables[parent]
            a, b = [
                seed_delete_family(connection, str(uuid.uuid4()), label, table)
                for label in ("A", "B")
            ]

            def children():
                return {
                    row["id"]: dict(row) for row in connection.execute(select(child)).mappings()
                }

            original = children()
            assert set(original) == set(a["children"].values()) | set(b["children"].values())
            # Both tenants are visible: this is FK/bootstrap evidence, not RLS.
            assert {row["org_id"] for row in original.values()} == {a["org"], b["org"]}
            assert all(
                row["amount" if table == "expense_items" else "total"] > 0
                for row in original.values()
            )
            with pytest.raises(IntegrityError) as rejected:
                with connection.begin_nested():
                    connection.execute(
                        child.update()
                        .where(child.c.id == a["children"]["target"])
                        .values({column: b[parent]["target"]})
                    )
            assert rejected.value.orig.sqlstate == "23503"
            assert rejected.value.orig.__cause__.constraint_name == name
            assert children() == original
            assert (
                connection.execute(
                    parent_table.delete().where(parent_table.c.id == a[parent]["target"])
                ).rowcount
                == 1
            )
            expected = {key: dict(value) for key, value in original.items()}
            expected[a["children"]["target"]][column] = None
            assert children() == expected
            assert (
                connection.scalar(
                    select(parent_table.c.id).where(parent_table.c.id == b[parent]["target"])
                )
                == b[parent]["target"]
            )
            assert (
                connection.scalar(
                    select(parent_table.c.id).where(parent_table.c.id == a[parent]["keep"])
                )
                == a[parent]["keep"]
            )
        finally:
            boundary.rollback()

    database(bootstrap_url, exercise)
    assert database(bootstrap_url, snapshot) == before


@pytest.mark.parametrize("table,column,parent,name", LINKS)
def test_portable_bootstrap_negative_control_fails_delete_instead_of_nulling_org(
    legacy_bootstrap_url, table, column, parent, name
):
    before = database(legacy_bootstrap_url, snapshot)

    def exercise(connection):
        boundary = connection.begin_nested()
        try:
            family = seed_delete_family(connection, str(uuid.uuid4()), "legacy", table)
            parent_table = Base.metadata.tables[parent]
            seeded = snapshot(connection)
            fk = next(
                fk for fk in inspect(connection).get_foreign_keys(table) if fk["name"] == name
            )
            assert fk["options"]["ondelete"] == "SET NULL"
            with pytest.raises(IntegrityError) as rejected:
                with connection.begin_nested():
                    connection.execute(
                        parent_table.delete().where(parent_table.c.id == family[parent]["target"])
                    )
            assert rejected.value.orig.sqlstate == "23502"
            cause = rejected.value.orig.__cause__
            assert cause.table_name == table and cause.column_name == "org_id"
            assert snapshot(connection) == seeded
        finally:
            boundary.rollback()

    database(legacy_bootstrap_url, exercise)
    assert database(legacy_bootstrap_url, snapshot) == before


@pytest.mark.parametrize("schema_kind", ["fresh", "legacy"])
def test_repeated_bootstrap_preserves_existing_rows_catalog_and_metadata(request, schema_kind):
    from app.core.development_schema import development_metadata

    url = request.getfixturevalue(
        "bootstrap_url" if schema_kind == "fresh" else "legacy_bootstrap_url"
    )
    original_metadata = metadata_signature(Base.metadata)
    before = database(url, snapshot)

    def exercise(connection):
        boundary = connection.begin_nested()
        try:
            seed_delete_family(connection, str(uuid.uuid4()), "preservation", "issued_invoices")
            seeded = snapshot(connection)
            statements = []

            def record(_connection, _cursor, statement, *_args):
                statements.append(statement)

            event.listen(connection, "before_cursor_execute", record)
            try:
                for _ in range(2):
                    development_metadata(Base.metadata, connection.dialect).create_all(connection)
            finally:
                event.remove(connection, "before_cursor_execute", record)
            assert statements and all(s.lstrip().upper().startswith("SELECT") for s in statements)
            assert snapshot(connection) == seeded
            if schema_kind == "legacy":
                with pytest.raises(RuntimeError, match="DB-PG-PARITY-01"):
                    postgresql_comparison_metadata(connection, Base.metadata)
            else:
                assert metadata_signature(postgresql_comparison_metadata(connection, Base.metadata))
            assert snapshot(connection) == seeded
        finally:
            boundary.rollback()

    database(url, exercise)
    assert database(url, snapshot) == before
    assert metadata_signature(Base.metadata) == original_metadata


def test_schema_qualified_bootstrap_compares_without_writes_or_shared_mutation(bootstrap_url):
    from sqlalchemy import MetaData

    from app.core.development_schema import development_metadata

    before = database(bootstrap_url, snapshot)
    original = metadata_signature(Base.metadata)
    # The disposable database is already unique. Keep the schema prefix short
    # so generated index names do not share a randomized truncation namespace.
    schema = "qualified"

    def exercise(connection):
        boundary = connection.begin_nested()
        try:
            assert not connection.scalar(
                text("SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=:schema)"),
                {"schema": schema},
            )
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            metadata = MetaData(schema=schema, naming_convention=Base.metadata.naming_convention)
            for table in Base.metadata.sorted_tables:
                table.to_metadata(metadata, schema=schema)
            source = metadata_signature(metadata)
            projected = development_metadata(metadata, connection.dialect)
            index_names = [
                connection.dialect.identifier_preparer.format_index(index)
                for table in projected.tables.values()
                for index in table.indexes
            ]
            assert len(index_names) == len(set(index_names))
            projected.create_all(connection)
            statements = []

            def record(_connection, _cursor, statement, *_args):
                statements.append(statement)

            event.listen(connection, "before_cursor_execute", record)
            try:
                comparison = postgresql_comparison_metadata(connection, metadata)
            finally:
                event.remove(connection, "before_cursor_execute", record)
            assert statements and all(s.lstrip().upper().startswith("SELECT") for s in statements)
            assert comparison.schema == metadata.schema
            assert comparison.naming_convention == metadata.naming_convention
            assert metadata_signature(comparison) == metadata_signature(projected)
            assert metadata_signature(metadata) == source
        finally:
            boundary.rollback()
        assert not connection.scalar(
            text("SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=:schema)"),
            {"schema": schema},
        )

    database(bootstrap_url, exercise)
    assert database(bootstrap_url, snapshot) == before
    assert metadata_signature(Base.metadata) == original
