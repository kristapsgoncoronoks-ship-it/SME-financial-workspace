"""Real PostgreSQL constraints, FORCE RLS, migration rollback and writer barriers."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import delete, func, insert, inspect, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from app.models.organization import Organization
from tests.test_partner_webhook_parent_migrations import PARTNER, WEBHOOK, _revision
from tests.test_partner_webhook_parent_tenant import _child_values, _link

PG_URL = os.environ.get("RLS_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not PG_URL, reason="requires migrated non-superuser PostgreSQL")
TABLES = "'partners','partner_documents','webhook_endpoints','webhook_deliveries','expense_transactions','issued_invoices','recurring_invoices','vendor_change_requests'"


async def _scope(db, org):
    await db.execute(text("SELECT set_config('app.current_org', :org, true)"), {"org": org})


def _apply(connection, migration, operation):
    with Operations.context(MigrationContext.configure(connection)):
        getattr(migration, operation)()


async def _catalog(db):
    statements = {
        "constraints": f"SELECT c.relname,co.conname,pg_get_constraintdef(co.oid) FROM pg_constraint co JOIN pg_class c ON c.oid=co.conrelid WHERE c.relnamespace='public'::regnamespace AND c.relname IN ({TABLES}) ORDER BY c.relname,co.conname",
        "indexes": f"SELECT tablename,indexname,indexdef FROM pg_indexes WHERE schemaname='public' AND tablename IN ({TABLES}) ORDER BY tablename,indexname",
        "security": f"SELECT relname,relrowsecurity,relforcerowsecurity FROM pg_class WHERE relnamespace='public'::regnamespace AND relname IN ({TABLES}) ORDER BY relname",
        "policies": f"SELECT tablename,policyname,cmd,qual,with_check FROM pg_policies WHERE schemaname='public' AND tablename IN ({TABLES}) ORDER BY tablename,policyname",
        "columns": f"SELECT table_name,column_name,is_nullable,data_type,column_default FROM information_schema.columns WHERE table_schema='public' AND table_name IN ({TABLES}) ORDER BY table_name,ordinal_position",
    }
    return {
        name: (await db.execute(text(statement))).all() for name, statement in statements.items()
    }


async def _rows(db, parent, child, ids):
    return {
        model.__tablename__: [
            dict(row)
            for row in (
                await db.execute(
                    select(model.__table__)
                    .where(model.id.in_([ids[f"{kind}_a"], ids[f"{kind}_b"]]))
                    .order_by(model.id)
                )
            ).mappings()
        ]
        for model, kind in ((parent, "parent"), (child, "child"))
    }


def _error_state(error):
    return error.orig.__cause__.sqlstate, getattr(error.orig.__cause__, "constraint_name", None)


@asynccontextmanager
async def parent_family(database_url, kind):
    parent, child, _column = _link(kind)
    migration = _revision(PARTNER if kind == "partner" else WEBHOOK)
    engine = create_async_engine(database_url)
    ids = {
        f"{prefix}_{letter}": str(uuid4())
        for prefix in ("org", "parent", "child")
        for letter in "ab"
    }
    try:
        async with engine.connect() as db:
            transaction = await db.begin()
            try:
                assert (
                    await db.execute(
                        text(
                            "SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"
                        )
                    )
                ).one() == (False, False)
                security = (await _catalog(db))["security"]
                assert len(security) == 8 and all(
                    enabled and forced for _table, enabled, forced in security
                )
                foreign_keys = await db.run_sync(
                    lambda connection: inspect(connection).get_foreign_keys(child.__tablename__)
                )
                assert any(
                    fk["name"] == migration.COMPOSITE_FK
                    and fk["constrained_columns"] == ["org_id", migration.LINK_COLUMN]
                    for fk in foreign_keys
                )
                for letter in "ab":
                    org = ids[f"org_{letter}"]
                    await _scope(db, org)
                    await db.execute(
                        insert(Organization).values(
                            id=org, name="Synthetic mandatory parent " + letter
                        )
                    )
                    values = (
                        {"name": "Synthetic partner"}
                        if kind == "partner"
                        else {
                            "url": "https://example.invalid/never-called",
                            "secret": "synthetic-never-used",
                        }
                    )
                    await db.execute(
                        insert(parent).values(id=ids[f"parent_{letter}"], org_id=org, **values)
                    )
                    values = _child_values(kind, org, ids[f"parent_{letter}"])
                    values["id"] = ids[f"child_{letter}"]
                    await db.execute(insert(child).values(**values))
                await _scope(db, "")
                yield engine, db, ids, migration, kind
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(params=["partner", "webhook"])
async def parent_pg(request):
    async with parent_family(PG_URL, request.param) as family:
        yield family


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["insert", "parent_link", "child_org", "parent_org"])
async def test_native_pair_refuses_cross_tenant_insert_and_key_updates(parent_pg, operation):
    _engine, db, ids, migration, kind = parent_pg
    parent, child, column = _link(kind)
    await _scope(db, ids["org_a"])
    assert await db.scalar(select(parent.id).where(parent.id == ids["parent_b"])) is None
    if operation == "insert":
        statement = insert(child).values(**_child_values(kind, ids["org_a"], ids["parent_b"]))
    elif operation == "parent_link":
        statement = (
            update(child).where(child.id == ids["child_a"]).values(**{column: ids["parent_b"]})
        )
    elif operation == "child_org":
        await _scope(db, "")
        statement = update(child).where(child.id == ids["child_a"]).values(org_id=ids["org_b"])
    else:
        await _scope(db, "")
        statement = update(parent).where(parent.id == ids["parent_a"]).values(org_id=ids["org_b"])
    with pytest.raises(DBAPIError) as refused:
        async with db.begin_nested():
            await db.execute(statement)
    assert _error_state(refused.value) == ("23503", migration.COMPOSITE_FK)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["insert", "update"])
async def test_native_rls_still_refuses_foreign_owned_correct_pair(parent_pg, operation):
    _engine, db, ids, _migration, kind = parent_pg
    _parent, child, column = _link(kind)
    await _scope(db, ids["org_a"])
    if operation == "insert":
        statement = insert(child).values(**_child_values(kind, ids["org_b"], ids["parent_b"]))
    else:
        statement = (
            update(child)
            .where(child.id == ids["child_a"])
            .values(org_id=ids["org_b"], **{column: ids["parent_b"]})
        )
    with pytest.raises(DBAPIError) as refused:
        async with db.begin_nested():
            await db.execute(statement)
    assert _error_state(refused.value)[0] == "42501"


@pytest.mark.asyncio
async def test_native_parent_delete_cascades_only_own_history(parent_pg):
    _engine, db, ids, _migration, kind = parent_pg
    parent, child, _column = _link(kind)
    before = await _rows(db, parent, child, ids)
    await _scope(db, ids["org_a"])
    await db.execute(delete(parent).where(parent.id == ids["parent_a"]))
    assert await db.scalar(select(func.count()).select_from(child)) == 0
    await _scope(db, ids["org_b"])
    remaining = (
        (await db.execute(select(child.__table__).where(child.id == ids["child_b"])))
        .mappings()
        .one()
    )
    assert dict(remaining) == next(
        row for row in before[child.__tablename__] if row["id"] == ids["child_b"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("table_kind", ["parent", "child"])
async def test_native_migration_locks_actually_block_parent_and_child_writes(parent_pg, table_kind):
    engine, db, _ids, migration, _kind = parent_pg
    table = migration.PARENT_TABLE if table_kind == "parent" else migration.CHILD_TABLE
    locker_pid = await db.scalar(text("SELECT pg_backend_pid()"))
    savepoint = await db.begin_nested()
    await db.run_sync(migration._prepare)
    async with engine.connect() as writer:
        writer_transaction = await writer.begin()
        task = None
        try:
            await writer.execute(text("SET LOCAL lock_timeout = '5s'"))
            writer_pid = await writer.scalar(text("SELECT pg_backend_pid()"))
            task = asyncio.create_task(
                writer.execute(text(f"UPDATE {table} SET id=id WHERE false"))
            )
            async with asyncio.timeout(3):
                while not await db.scalar(
                    text(
                        "SELECT :locker=ANY(pg_blocking_pids(:writer)) AND EXISTS "
                        "(SELECT 1 FROM pg_locks WHERE pid=:writer AND relation=to_regclass(:table) "
                        "AND mode='RowExclusiveLock' AND NOT granted) AND EXISTS "
                        "(SELECT 1 FROM pg_stat_activity WHERE pid=:writer AND wait_event_type='Lock' "
                        "AND query LIKE 'UPDATE %')"
                    ),
                    {"locker": locker_pid, "writer": writer_pid, "table": table},
                ):
                    await asyncio.sleep(0.01)
            assert not task.done()
            await savepoint.rollback()
            result = await asyncio.wait_for(task, 2)
            assert result.rowcount == 0
        finally:
            if savepoint.is_active:
                await savepoint.rollback()
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
            await writer_transaction.rollback()
