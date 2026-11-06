"""The item/report tenant pair is structural under native FORCE RLS."""

from __future__ import annotations

import importlib.util
import os
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import delete, func, insert, inspect, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from app.models.expense import ExpenseItem, ExpenseReport, ExpenseTransaction
from app.models.organization import Organization
from app.models.user import User, UserRole

PG_URL = os.environ.get("RLS_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not PG_URL, reason="requires migrated non-superuser PostgreSQL")
MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic/versions/d5e7f9a1b3c6_expense_item_report_tenant_fk.py"
)
spec = importlib.util.spec_from_file_location("expense_parent_migration", MIGRATION_PATH)
assert spec is not None and spec.loader is not None
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


async def _scope(db, org):
    await db.execute(text("SELECT set_config('app.current_org', :org, true)"), {"org": org})


def _run_migration(connection, operation):
    with Operations.context(MigrationContext.configure(connection)):
        getattr(migration, operation)()


async def _snapshot(db, ids):
    return {
        model.__tablename__: [
            dict(row)
            for row in (
                await db.execute(
                    select(model.__table__).where(model.id.in_(values)).order_by(model.id)
                )
            ).mappings()
        ]
        for model, values in (
            (ExpenseReport, [ids["report_a"], ids["report_b"]]),
            (ExpenseItem, [ids["item_a"], ids["item_b"]]),
            (ExpenseTransaction, [ids["txn_a"], ids["txn_b"]]),
        )
    }


@asynccontextmanager
async def parent_family(database_url):
    """Seed a rollback-only family in the explicitly selected database."""
    engine = create_async_engine(database_url)
    ids = {
        f"{kind}_{tenant}": str(uuid4())
        for tenant in ("a", "b")
        for kind in ("org", "user", "report", "item", "txn")
    }
    try:
        async with engine.connect() as db:
            transaction = await db.begin()
            try:
                assert (
                    await db.execute(
                        text(
                            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user"
                        )
                    )
                ).one() == (False, False)
                security = (
                    await db.execute(
                        text(
                            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid IN ('expense_items'::regclass,'expense_reports'::regclass,'expense_transactions'::regclass)"
                        )
                    )
                ).all()
                assert len(security) == 3 and all(
                    enabled and forced for enabled, forced in security
                )
                fks = await db.run_sync(
                    lambda connection: inspect(connection).get_foreign_keys("expense_items")
                )
                assert any(
                    fk["name"] == migration.COMPOSITE_FK
                    and fk["constrained_columns"] == ["org_id", "report_id"]
                    for fk in fks
                )
                for tenant in ("a", "b"):
                    org = ids[f"org_{tenant}"]
                    await _scope(db, org)
                    await db.execute(
                        insert(Organization.__table__).values(
                            id=org, name="Synthetic expense parent " + tenant
                        )
                    )
                    await db.execute(
                        insert(User.__table__).values(
                            id=ids[f"user_{tenant}"],
                            org_id=org,
                            email=uuid4().hex + "@example.invalid",
                            name="Synthetic parent",
                            hashed_password="synthetic-not-a-login",
                            role=UserRole.owner,
                        )
                    )
                    await db.execute(
                        insert(ExpenseReport.__table__).values(
                            id=ids[f"report_{tenant}"],
                            org_id=org,
                            employee_id=ids[f"user_{tenant}"],
                            employee_name="Synthetic parent",
                            title="Synthetic parent " + tenant,
                        )
                    )
                    await db.execute(
                        insert(ExpenseItem.__table__).values(
                            id=ids[f"item_{tenant}"],
                            org_id=org,
                            report_id=ids[f"report_{tenant}"],
                            spend_date=date(2026, 11, 5),
                            description="Synthetic preserved item " + tenant,
                            receipt_sha256="a" * 64,
                            receipt_size=17,
                        )
                    )
                    await db.execute(
                        insert(ExpenseTransaction.__table__).values(
                            id=ids[f"txn_{tenant}"],
                            org_id=org,
                            employee_id=ids[f"user_{tenant}"],
                            txn_date=date(2026, 11, 5),
                            description="Synthetic preserved transaction " + tenant,
                            item_id=ids[f"item_{tenant}"],
                        )
                    )
                yield engine, db, ids
            finally:
                await transaction.rollback()
            await _scope(db, "")
            for model, names in (
                (Organization, ["org_a", "org_b"]),
                (User, ["user_a", "user_b"]),
                (ExpenseReport, ["report_a", "report_b"]),
                (ExpenseItem, ["item_a", "item_b"]),
                (ExpenseTransaction, ["txn_a", "txn_b"]),
            ):
                assert (
                    await db.scalar(
                        select(func.count())
                        .select_from(model.__table__)
                        .where(model.id.in_([ids[name] for name in names]))
                    )
                    == 0
                )
            await db.rollback()
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def parent_pg():
    async with parent_family(PG_URL) as family:
        yield family


@pytest.mark.parametrize("operation", ["insert", "report_update", "org_update"])
async def test_native_constraint_refuses_both_directions_of_wrong_tenant_pair(parent_pg, operation):
    _, db, ids = parent_pg
    await _scope(db, "" if operation == "org_update" else ids["org_a"])
    before = await _snapshot(db, ids)
    with pytest.raises(IntegrityError) as caught:
        async with db.begin_nested():
            if operation == "org_update":
                # Isolate this outgoing report pair from the newer incoming
                # optional transaction pair. Rollback restores the full link.
                await db.execute(
                    update(ExpenseTransaction.__table__)
                    .where(ExpenseTransaction.item_id == ids["item_a"])
                    .values(item_id=None)
                )
            if operation == "insert":
                await db.execute(
                    insert(ExpenseItem.__table__).values(
                        org_id=ids["org_a"],
                        report_id=ids["report_b"],
                        spend_date=date(2026, 11, 5),
                        description="Synthetic refused pair",
                    )
                )
            else:
                values = (
                    {"report_id": ids["report_b"]}
                    if operation == "report_update"
                    else {"org_id": ids["org_b"]}
                )
                await db.execute(
                    update(ExpenseItem.__table__)
                    .where(ExpenseItem.id == ids["item_a"])
                    .values(**values)
                )
    assert caught.value.orig.sqlstate == "23503"
    original = caught.value.orig
    constraint_name = (
        getattr(original.__cause__, "constraint_name", None)
        or getattr(original, "constraint_name", None)
        or getattr(getattr(original, "diag", None), "constraint_name", None)
    )
    assert constraint_name == migration.COMPOSITE_FK
    assert await _snapshot(db, ids) == before


async def test_native_rls_still_refuses_a_foreign_owned_correct_pair(parent_pg):
    _, db, ids = parent_pg
    await _scope(db, ids["org_a"])
    with pytest.raises(DBAPIError) as caught:
        async with db.begin_nested():
            await db.execute(
                insert(ExpenseItem.__table__).values(
                    org_id=ids["org_b"],
                    report_id=ids["report_b"],
                    spend_date=date(2026, 11, 5),
                    description="Synthetic RLS refusal",
                )
            )
    assert caught.value.orig.sqlstate == "42501"


async def test_native_report_cascade_detaches_only_optional_item_id(parent_pg):
    _, db, ids = parent_pg
    await _scope(db, ids["org_a"])
    await db.execute(delete(ExpenseReport.__table__).where(ExpenseReport.id == ids["report_a"]))
    assert (
        await db.scalar(
            select(func.count())
            .select_from(ExpenseItem.__table__)
            .where(ExpenseItem.id == ids["item_a"])
        )
        == 0
    )
    own = (
        await db.execute(
            select(ExpenseTransaction.org_id, ExpenseTransaction.item_id).where(
                ExpenseTransaction.id == ids["txn_a"]
            )
        )
    ).one()
    assert own == (ids["org_a"], None)
    await _scope(db, ids["org_b"])
    other = (
        await db.execute(
            select(ExpenseTransaction.org_id, ExpenseTransaction.item_id).where(
                ExpenseTransaction.id == ids["txn_b"]
            )
        )
    ).one()
    assert other == (ids["org_b"], ids["item_b"])


@pytest.mark.parametrize("operation", ["upgrade", "downgrade"])
async def test_native_migration_refuses_a_scoped_connection_before_schema_changes(
    parent_pg, operation
):
    _, db, ids = parent_pg
    await _scope(db, ids["org_a"])
    with pytest.raises(RuntimeError, match="unscoped migration connection"):
        await db.run_sync(_run_migration, operation)


@pytest.mark.parametrize("table", ["expense_reports", "expense_items"])
async def test_native_preflight_excludes_writes_to_both_pair_tables(parent_pg, table):
    engine, db, _ = parent_pg
    await _scope(db, "")
    await db.run_sync(migration._prepare)
    async with engine.connect() as writer:
        with pytest.raises(DBAPIError) as caught:
            async with writer.begin():
                await writer.execute(text("SET LOCAL lock_timeout = '250ms'"))
                await writer.execute(text(f"UPDATE {table} SET id=id WHERE false"))
        assert caught.value.orig.sqlstate == "55P03"
