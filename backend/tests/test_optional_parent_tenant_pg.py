"""Immediate optional parent pairs under actual164 FORCE RLS and both action orders."""

from __future__ import annotations

import asyncio
import os
from copy import deepcopy
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.models import Base
from tests.test_migrations import BACKEND_DIR, _run_alembic
from tests.test_optional_parent_migration import _all_rows, _seed
from tests.test_optional_parent_migration_pg import LINKS, _database, _scope
from tests.test_optional_parent_migration_pg import optional_family as optional_family
from tests.test_pg_migration_schema_contract import PrivateDatabaseURL
from tests.test_planning_ocr_migration_integration import schemas


@pytest.fixture(scope="module")
def optional_database():
    """Current application behavior follows the graph head, never historical164."""
    admin_url = os.environ.get("MIGRATION_TEST_ADMIN_URL")
    app_url = os.environ.get("RLS_TEST_DATABASE_URL")
    if not admin_url or not app_url:
        pytest.skip("requires explicit disposable-database admin and non-superuser app URLs")
    import asyncpg

    config = Config()
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    current_head = ScriptDirectory.from_config(config).get_current_head()
    assert current_head is not None
    admin_url = PrivateDatabaseURL(admin_url)
    app = make_url(app_url)
    assert app.username and app.username.replace("_", "").isalnum()
    name = "optional_current_" + uuid4().hex

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
        upgraded = _run_alembic(["upgrade", current_head], url)
        assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr

        def seed_current(connection):
            assert connection.execute(
                text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user")
            ).one() == (False, False)
            return _seed(connection)

        identities = _database(url, seed_current)
        before_rows, before_schema = _database(url, _all_rows), _database(url, schemas)

        def unchanged_current_head(connection):
            assert set(
                connection.execute(text("SELECT version_num FROM alembic_version")).scalars()
            ) == {current_head}
            assert _all_rows(connection) == before_rows
            assert schemas(connection) == before_schema

        _database(url, unchanged_current_head)
        try:
            yield url, identities
        finally:
            _database(url, unchanged_current_head)
    finally:
        asyncio.run(manage(False))


def _constraint_name(error):
    original = error.orig
    return (
        getattr(original.__cause__, "constraint_name", None)
        or getattr(original, "constraint_name", None)
        or getattr(getattr(original, "diag", None), "constraint_name", None)
    )


def _sorted_rows(rows):
    import json

    return {
        table: sorted(values, key=lambda row: json.dumps(row, default=str, sort_keys=True))
        for table, values in rows.items()
    }


def _after_parent_delete(before, parent, identifiers):
    expected = deepcopy(before)
    expected[parent] = [row for row in expected[parent] if str(row["id"]) not in identifiers]
    if parent == "partners":
        expected["partner_documents"] = [
            row
            for row in expected["partner_documents"]
            if str(row["partner_id"]) not in identifiers
        ]
    for table, column, target, *_ in LINKS:
        if target == parent:
            for row in expected[table]:
                if str(row[column]) in identifiers:
                    row[column] = None
    return _sorted_rows(expected)


async def _order_constraints(db, table, pair, old_single, pair_first):
    if pair_first:
        definition = await db.scalar(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid=CAST(:table AS regclass) AND conname=:name"
            ),
            {"table": table, "name": old_single},
        )
        await db.execute(
            text(
                f'ALTER TABLE "{table}" DROP CONSTRAINT "{old_single}", ADD CONSTRAINT "{old_single}" {definition}'
            )
        )
    names = list(
        (
            await db.execute(
                text(
                    "SELECT conname FROM pg_constraint WHERE conrelid=CAST(:table AS regclass) AND conname IN (:pair,:single) ORDER BY oid"
                ),
                {"table": table, "pair": pair, "single": old_single},
            )
        ).scalars()
    )
    assert names == ([pair, old_single] if pair_first else [old_single, pair])


@pytest.mark.parametrize("table,column,parent,parent_key,child_key,name,old_name", LINKS)
@pytest.mark.parametrize("operation", ["insert", "reference", "child_org", "parent_org"])
async def test_native_wrong_pair_is_immediately_rejected_by_the_intended_guard(
    optional_family, table, column, parent, parent_key, child_key, name, old_name, operation
):
    db, (a, b) = optional_family
    before = await db.run_sync(_all_rows)
    child = Base.metadata.tables[table]
    original = dict(
        (await db.execute(select(child).where(child.c.id == a[child_key]))).mappings().one()
    )
    await _scope(db, "" if operation in ("child_org", "parent_org") else a["org"])
    with pytest.raises(IntegrityError) as caught:
        async with db.begin_nested():
            if operation == "insert":
                values = dict(original, id=str(uuid4()), **{column: b[parent_key]})
                if table == "vendor_change_requests":
                    values["status"] = "approved"
                await db.execute(child.insert().values(**values))
            elif operation == "reference":
                await db.execute(
                    child.update()
                    .where(child.c.id == a[child_key])
                    .values(**{column: b[parent_key]})
                )
            elif operation == "child_org":
                values = {"org_id": b["org"]}
                if table == "vendor_change_requests":
                    values.update(vendor_id=b["vendor"], status="approved")
                elif table == "issued_invoices":
                    # These existing incoming tenant pairs would reject the
                    # org edit first. Isolate this guard within the same
                    # savepoint; the refusal restores all settlement/evidence.
                    await db.execute(
                        text("DELETE FROM payments WHERE issued_invoice_id=:id"),
                        {"id": a["invoice"]},
                    )
                    await db.execute(
                        text("DELETE FROM issued_invoice_attachments WHERE invoice_id=:id"),
                        {"id": a["invoice"]},
                    )
                await db.execute(child.update().where(child.c.id == a[child_key]).values(**values))
            else:
                values = {"org_id": b["org"]}
                if parent == "expense_items":
                    values["report_id"] = b["report"]
                elif parent == "partners":
                    other = (
                        "recurring_invoices" if table == "issued_invoices" else "issued_invoices"
                    )
                    await db.execute(
                        text(f'UPDATE "{other}" SET partner_id=NULL WHERE partner_id=:id'),
                        {"id": a["partner"]},
                    )
                    await db.execute(
                        text("DELETE FROM partner_documents WHERE partner_id=:id"),
                        {"id": a["partner"]},
                    )
                target = Base.metadata.tables[parent]
                await db.execute(
                    target.update().where(target.c.id == a[parent_key]).values(**values)
                )
    assert caught.value.orig.sqlstate == "23503"
    assert _constraint_name(caught.value) == name
    await _scope(db, "")
    assert await db.run_sync(_all_rows) == before


@pytest.mark.parametrize("table,column,parent,parent_key,child_key,name,old_name", LINKS)
async def test_native_force_rls_hides_foreign_parent_and_refuses_foreign_owned_valid_pair(
    optional_family, table, column, parent, parent_key, child_key, name, old_name
):
    db, (a, b) = optional_family
    before = await db.run_sync(_all_rows)
    child = Base.metadata.tables[table]
    values = dict(
        (await db.execute(select(child).where(child.c.id == b[child_key]))).mappings().one()
    )
    values["id"] = str(uuid4())
    if table == "vendor_change_requests":
        values["status"] = "approved"
    await _scope(db, a["org"])
    target = Base.metadata.tables[parent]
    assert await db.scalar(select(target.c.id).where(target.c.id == b[parent_key])) is None
    with pytest.raises(DBAPIError) as caught:
        async with db.begin_nested():
            await db.execute(child.insert().values(**values))
    assert caught.value.orig.sqlstate == "42501"
    await _scope(db, "")
    assert await db.run_sync(_all_rows) == before


@pytest.mark.parametrize("table,column,parent,parent_key,child_key,name,old_name", LINKS)
async def test_native_own_pair_and_null_preserve_the_child_org_and_all_fields(
    optional_family, table, column, parent, parent_key, child_key, name, old_name
):
    db, (a, _b) = optional_family
    before = await db.run_sync(_all_rows)
    child = Base.metadata.tables[table]
    original = dict(
        (await db.execute(select(child).where(child.c.id == a[child_key]))).mappings().one()
    )
    await _scope(db, a["org"])
    for value in (a[parent_key], None):
        boundary = await db.begin_nested()
        try:
            candidate = dict(original, id=str(uuid4()), **{column: value})
            if table == "vendor_change_requests":
                candidate["status"] = "approved"
            await db.execute(child.insert().values(**candidate))
            actual = dict(
                (await db.execute(select(child).where(child.c.id == candidate["id"])))
                .mappings()
                .one()
            )
            assert actual == candidate
        finally:
            await boundary.rollback()
    await _scope(db, "")
    assert await db.run_sync(_all_rows) == before


@pytest.mark.parametrize("table,column,parent,parent_key,child_key,name,old_name", LINKS)
@pytest.mark.parametrize(
    "pair_first", [False, True], ids=["single-created-first", "pair-created-first"]
)
async def test_native_both_creation_orders_detach_only_ids_for_direct_and_multirow_deletes(
    optional_family, table, column, parent, parent_key, child_key, name, old_name, pair_first
):
    db, (a, b) = optional_family
    before, schema = await db.run_sync(_all_rows), await db.run_sync(schemas)
    await _order_constraints(db, table, name, old_name, pair_first)
    assert await db.run_sync(_all_rows) == before
    assert await db.run_sync(schemas) == schema
    for identifiers in ([a[parent_key]], [a[parent_key], b[parent_key]]):
        boundary = await db.begin_nested()
        try:
            target = Base.metadata.tables[parent]
            await db.execute(target.delete().where(target.c.id.in_(identifiers)))
            assert await db.run_sync(_all_rows) == _after_parent_delete(before, parent, identifiers)
        finally:
            await boundary.rollback()
        assert await db.run_sync(_all_rows) == before
        assert await db.run_sync(schemas) == schema


@pytest.mark.parametrize(
    "pair_first", [False, True], ids=["single-created-first", "pair-created-first"]
)
async def test_native_report_cascade_detaches_transactions_and_preserves_published_history(
    optional_family, pair_first
):
    db, (a, _b) = optional_family
    await _order_constraints(
        db,
        "expense_transactions",
        "fk_expense_transactions_item",
        "expense_transactions_item_id_fkey",
        pair_first,
    )
    before, schema = await db.run_sync(_all_rows), await db.run_sync(schemas)
    expected = deepcopy(before)
    expected["expense_reports"] = [
        row for row in expected["expense_reports"] if str(row["id"]) != a["report"]
    ]
    for table in ("expense_items", "expense_comments", "expense_approval_steps"):
        expected[table] = [row for row in expected[table] if str(row["report_id"]) != a["report"]]
    for row in expected["expense_transactions"]:
        if str(row["item_id"]) == a["item"]:
            row["item_id"] = None
    boundary = await db.begin_nested()
    try:
        await db.execute(text("DELETE FROM expense_reports WHERE id=:id"), {"id": a["report"]})
        assert await db.run_sync(_all_rows) == _sorted_rows(expected)
    finally:
        await boundary.rollback()
    assert await db.run_sync(_all_rows) == before
    assert await db.run_sync(schemas) == schema
