"""Exact native optional-pair validation on owned rollback-only catalogues.

These tiny schemas test installed catalogue contracts, not migrated RLS or
application row behavior. No existing public table or migration marker changes.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import MetaData, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.migration_schema import validate_installed_optional_parents

PG_URL = os.environ.get("RLS_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not PG_URL, reason="requires owned non-superuser PostgreSQL")
LINKS = (
    (
        "expense_transactions",
        "item_id",
        "expense_items",
        "fk_expense_transactions_item",
        "expense_transactions_item_id_fkey",
    ),
    (
        "issued_invoices",
        "partner_id",
        "partners",
        "fk_issued_invoices_partner",
        "fk_issued_partner",
    ),
    (
        "recurring_invoices",
        "partner_id",
        "partners",
        "fk_recurring_invoices_partner",
        "recurring_invoices_partner_id_fkey",
    ),
    (
        "vendor_change_requests",
        "source_document_id",
        "documents",
        "fk_vendor_change_requests_source_document",
        "vendor_change_requests_source_document_id_fkey",
    ),
)
VARIANTS = (
    "pair_no_action",
    "pair_generic_set_null",
    "pair_org_only_set_null",
    "pair_onupdate_cascade",
    "pair_deferred",
    "pair_deferrable_immediate",
    "pair_not_valid",
    "pair_match_full",
    "pair_reversed_columns",
    "pair_wrong_target",
    "pair_missing",
    "pair_renamed",
    "pair_duplicate",
    "single_missing",
    "single_renamed",
    "single_cascade",
    "single_no_action",
    "single_targeted_set_null",
    "single_deferred",
)


@pytest_asyncio.fixture
async def catalog_schema(request):
    options = getattr(request, "param", {})
    deferred_parent = options.get("deferred_parent")
    pair_first = options.get("pair_first", False)
    schema = "optional_catalog_" + uuid4().hex
    engine = create_async_engine(PG_URL)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                assert (
                    await connection.execute(
                        text(
                            "SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"
                        )
                    )
                ).one() == (False, False)
                await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
                for parent in ("expense_items", "partners", "documents"):
                    target = f'"{schema}".{parent}'
                    await connection.execute(
                        text(
                            f"CREATE TABLE {target}(org_id integer NOT NULL,id integer PRIMARY KEY)"
                        )
                    )
                    index = f"uq_{parent}_org_id"
                    if parent == deferred_parent:
                        # The pair FK can use this immediate index while the
                        # exact expected named index is incorrectly deferred.
                        await connection.execute(
                            text(
                                f"CREATE UNIQUE INDEX alternate_{parent}_pair ON {target}(org_id,id)"
                            )
                        )
                        await connection.execute(
                            text(
                                f"ALTER TABLE {target} ADD CONSTRAINT {index} UNIQUE(org_id,id) DEFERRABLE INITIALLY DEFERRED"
                            )
                        )
                    else:
                        await connection.execute(
                            text(f"CREATE UNIQUE INDEX {index} ON {target}(org_id,id)")
                        )
                for child, column, parent, pair, single in LINKS:
                    table, target = f'"{schema}".{child}', f'"{schema}".{parent}'
                    clauses = [
                        f"CONSTRAINT {pair} FOREIGN KEY(org_id,{column}) REFERENCES {target}(org_id,id) ON DELETE SET NULL ({column})",
                        f"CONSTRAINT {single} FOREIGN KEY({column}) REFERENCES {target}(id) ON DELETE SET NULL",
                    ]
                    if not pair_first:
                        clauses.reverse()
                    await connection.execute(
                        text(
                            f"CREATE TABLE {table}(id integer PRIMARY KEY,org_id integer NOT NULL,{column} integer,"
                            + ",".join(clauses)
                            + ")"
                        )
                    )
                yield connection, MetaData(schema=schema), schema
            finally:
                await transaction.rollback()
            assert not await connection.scalar(
                text("SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=:schema)"),
                {"schema": schema},
            )
            await connection.rollback()
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    "catalog_schema", [{"pair_first": False}, {"pair_first": True}], indirect=True
)
async def test_exact_native_pair_and_single_catalogue_passes_both_orders(catalog_schema):
    connection, metadata, _schema = catalog_schema
    await connection.run_sync(validate_installed_optional_parents, metadata)


@pytest.mark.parametrize(
    "catalog_schema",
    [{"deferred_parent": parent} for parent in ("expense_items", "partners", "documents")],
    indirect=True,
)
async def test_expected_pair_index_must_itself_be_immediate(catalog_schema):
    connection, metadata, schema = catalog_schema
    indexes = list(
        (
            await connection.execute(
                text(
                    "SELECT c.relname,i.indimmediate FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid WHERE c.relnamespace=to_regnamespace(:schema) AND c.relname LIKE 'uq_%_org_id'"
                ),
                {"schema": schema},
            )
        ).all()
    )
    assert sum(not immediate for _, immediate in indexes) == 1
    # All four fully validated pair FKs exist. The changed expected index is
    # not their support index: a different immediate pair index supports them.
    keys = list(
        (
            await connection.execute(
                text(
                    "SELECT c.conname,c.convalidated,i.indimmediate FROM pg_constraint c JOIN pg_index i ON i.indexrelid=c.conindid WHERE c.connamespace=to_regnamespace(:schema) AND c.contype='f' AND cardinality(c.conkey)=2"
                ),
                {"schema": schema},
            )
        ).all()
    )
    assert len(keys) == 4 and all(valid and immediate for _, valid, immediate in keys)
    with pytest.raises(RuntimeError, match="DB-PG-OPTIONAL-01"):
        await connection.run_sync(validate_installed_optional_parents, metadata)


@pytest.mark.parametrize("child,column,parent,pair,single", LINKS)
@pytest.mark.parametrize("variant", VARIANTS)
async def test_installed_pair_or_single_variant_is_refused(
    catalog_schema, child, column, parent, pair, single, variant
):
    connection, metadata, schema = catalog_schema
    table, target = f'"{schema}".{child}', f'"{schema}".{parent}'
    await connection.run_sync(validate_installed_optional_parents, metadata)
    if variant.startswith("pair_"):
        if variant != "pair_duplicate":
            await connection.execute(text(f"ALTER TABLE {table} DROP CONSTRAINT {pair}"))
        if variant != "pair_missing":
            name = pair + ("_mutant" if variant in ("pair_duplicate", "pair_renamed") else "")
            local, remote = f"org_id,{column}", "org_id,id"
            action, tail, match = f"ON DELETE SET NULL ({column})", "NOT DEFERRABLE", ""
            if variant == "pair_no_action":
                action = "ON DELETE NO ACTION"
            if variant == "pair_generic_set_null":
                action = "ON DELETE SET NULL"
            if variant == "pair_org_only_set_null":
                action = "ON DELETE SET NULL (org_id)"
            if variant == "pair_onupdate_cascade":
                action += " ON UPDATE CASCADE"
            if variant == "pair_deferred":
                tail = "DEFERRABLE INITIALLY DEFERRED"
            if variant == "pair_deferrable_immediate":
                tail = "DEFERRABLE INITIALLY IMMEDIATE"
            if variant == "pair_not_valid":
                tail += " NOT VALID"
            if variant == "pair_match_full":
                match = "MATCH FULL"
            if variant == "pair_reversed_columns":
                local, remote = f"{column},org_id", "id,org_id"
            if variant == "pair_wrong_target":
                target = f'"{schema}".' + ("documents" if parent != "documents" else "partners")
            await connection.execute(
                text(
                    f"ALTER TABLE {table} ADD CONSTRAINT {name} FOREIGN KEY({local}) REFERENCES {target}({remote}) {match} {action} {tail}"
                )
            )
    else:
        await connection.execute(text(f"ALTER TABLE {table} DROP CONSTRAINT {single}"))
        if variant != "single_missing":
            name = single + ("_mutant" if variant == "single_renamed" else "")
            action = "SET NULL"
            if variant == "single_cascade":
                action = "CASCADE"
            if variant == "single_no_action":
                action = "NO ACTION"
            if variant == "single_targeted_set_null":
                action = f"SET NULL ({column})"
            tail = (
                "DEFERRABLE INITIALLY DEFERRED"
                if variant == "single_deferred"
                else "NOT DEFERRABLE"
            )
            await connection.execute(
                text(
                    f"ALTER TABLE {table} ADD CONSTRAINT {name} FOREIGN KEY({column}) REFERENCES {target}(id) ON DELETE {action} {tail}"
                )
            )
    # Each mutant DDL must have succeeded, so a SQL setup failure cannot stand
    # in for the exact installed-contract validator refusing the intended link.
    with pytest.raises(RuntimeError, match=rf"{child}\.{pair}"):
        await connection.run_sync(validate_installed_optional_parents, metadata)
