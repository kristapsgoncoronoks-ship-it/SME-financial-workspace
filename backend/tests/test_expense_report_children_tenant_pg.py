"""Native tenant/report pairs, global preflight and two-link atomic migration."""

from __future__ import annotations

import importlib.util
import os
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

from app.models.expense import ExpenseComment, ExpenseItem, ExpenseReport, ExpenseTransaction
from app.models.expense_approval import ExpenseApprovalStep
from app.models.organization import Organization
from app.models.user import User, UserRole

PG_URL = os.environ.get("RLS_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not PG_URL, reason="requires migrated non-superuser PostgreSQL")
PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic/versions/e6f8a0b2c4d7_expense_report_children_tenant_fk.py"
)
spec = importlib.util.spec_from_file_location("expense_children_migration", PATH)
assert spec is not None and spec.loader is not None
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)
MODELS = (
    (Organization, "org"),
    (User, "user"),
    (ExpenseReport, "report"),
    (ExpenseItem, "item"),
    (ExpenseTransaction, "txn"),
    (ExpenseComment, "comment"),
    (ExpenseApprovalStep, "step"),
)
CHILDREN = (
    (ExpenseComment, "comment", "fk_expense_comments_report"),
    (ExpenseApprovalStep, "step", "fk_expense_approval_steps_report"),
)
TABLE_LIST = "'expense_reports','expense_items','expense_transactions','expense_comments','expense_approval_steps'"


async def _scope(db, org):
    await db.execute(text("SELECT set_config('app.current_org', :org, true)"), {"org": org})


def _apply(connection, operation):
    with Operations.context(MigrationContext.configure(connection)):
        getattr(migration, operation)()


async def _rows(db, ids):
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
        for model, kind in MODELS
    }


async def _catalog(db):
    statements = {
        "constraints": f"SELECT c.relname,co.conname,pg_get_constraintdef(co.oid) FROM pg_constraint co JOIN pg_class c ON c.oid=co.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relname IN ({TABLE_LIST}) ORDER BY c.relname,co.conname",
        "indexes": f"SELECT tablename,indexname,indexdef FROM pg_indexes WHERE schemaname='public' AND tablename IN ({TABLE_LIST}) ORDER BY tablename,indexname",
        "security": f"SELECT relname,relrowsecurity,relforcerowsecurity FROM pg_class WHERE relnamespace='public'::regnamespace AND relname IN ({TABLE_LIST}) ORDER BY relname",
        "policies": f"SELECT tablename,policyname,cmd,qual,with_check FROM pg_policies WHERE schemaname='public' AND tablename IN ({TABLE_LIST}) ORDER BY tablename,policyname",
        "columns": f"SELECT table_name,column_name,is_nullable,data_type,column_default FROM information_schema.columns WHERE table_schema='public' AND table_name IN ({TABLE_LIST}) ORDER BY table_name,ordinal_position",
    }
    return {key: (await db.execute(text(statement))).all() for key, statement in statements.items()}


def _constraint_name(error):
    original = error.orig
    return (
        getattr(original.__cause__, "constraint_name", None)
        or getattr(original, "constraint_name", None)
        or getattr(getattr(original, "diag", None), "constraint_name", None)
    )


@pytest_asyncio.fixture
async def children_pg():
    engine = create_async_engine(PG_URL)
    ids = {f"{kind}_{tenant}": str(uuid4()) for _model, kind in MODELS for tenant in ("a", "b")}
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
                security = (
                    await db.execute(
                        text(
                            f"SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE relnamespace='public'::regnamespace AND relname IN ({TABLE_LIST})"
                        )
                    )
                ).all()
                assert len(security) == 5 and all(
                    enabled and forced for enabled, forced in security
                )
                for model, _kind, name in CHILDREN:
                    fks = await db.run_sync(
                        lambda connection, table=model.__tablename__: inspect(
                            connection
                        ).get_foreign_keys(table)
                    )
                    assert any(
                        fk["name"] == name and fk["constrained_columns"] == ["org_id", "report_id"]
                        for fk in fks
                    )
                for tenant in ("a", "b"):
                    org = ids[f"org_{tenant}"]
                    await _scope(db, org)
                    values = (
                        (Organization, "org", {"name": "Synthetic children " + tenant}),
                        (
                            User,
                            "user",
                            {
                                "org_id": org,
                                "name": "Synthetic claimant",
                                "email": uuid4().hex + "@example.invalid",
                                "hashed_password": "synthetic-not-a-login",
                                "role": UserRole.owner,
                            },
                        ),
                        (
                            ExpenseReport,
                            "report",
                            {
                                "org_id": org,
                                "employee_id": ids[f"user_{tenant}"],
                                "employee_name": "Synthetic claimant",
                                "title": "Retained synthetic report",
                            },
                        ),
                        (
                            ExpenseItem,
                            "item",
                            {
                                "org_id": org,
                                "report_id": ids[f"report_{tenant}"],
                                "spend_date": date(2026, 11, 5),
                                "description": "Retained synthetic item",
                                "receipt_sha256": "e" * 64,
                                "receipt_size": 32,
                            },
                        ),
                        (
                            ExpenseTransaction,
                            "txn",
                            {
                                "org_id": org,
                                "employee_id": ids[f"user_{tenant}"],
                                "txn_date": date(2026, 11, 5),
                                "description": "Retained synthetic card link",
                                "item_id": ids[f"item_{tenant}"],
                            },
                        ),
                        (
                            ExpenseComment,
                            "comment",
                            {
                                "org_id": org,
                                "report_id": ids[f"report_{tenant}"],
                                "author_id": ids[f"user_{tenant}"],
                                "author_name": "Synthetic claimant",
                                "body": "Retained synthetic review text",
                            },
                        ),
                        (
                            ExpenseApprovalStep,
                            "step",
                            {
                                "org_id": org,
                                "report_id": ids[f"report_{tenant}"],
                                "seq": 0,
                                "approver_id": ids[f"user_{tenant}"],
                                "approver_email": "synthetic@example.invalid",
                                "note": "Retained synthetic review note",
                            },
                        ),
                    )
                    for model, kind, fields in values:
                        await db.execute(
                            insert(model.__table__).values(id=ids[f"{kind}_{tenant}"], **fields)
                        )
                yield engine, db, ids
            finally:
                await transaction.rollback()
            await _scope(db, "")
            for model, kind in MODELS:
                assert (
                    await db.scalar(
                        select(func.count())
                        .select_from(model.__table__)
                        .where(model.id.in_([ids[f"{kind}_a"], ids[f"{kind}_b"]]))
                    )
                    == 0
                )
            await db.rollback()
    finally:
        await engine.dispose()


@pytest.mark.parametrize("model,kind,constraint", CHILDREN)
@pytest.mark.parametrize("operation", ["insert", "report_update", "org_update"])
async def test_native_pair_refuses_cross_tenant_insert_and_both_key_updates(
    children_pg, model, kind, constraint, operation
):
    _, db, ids = children_pg
    await _scope(db, "" if operation == "org_update" else ids["org_a"])
    with pytest.raises(IntegrityError) as caught:
        async with db.begin_nested():
            if operation == "insert":
                fields = (
                    {
                        "author_id": ids["user_a"],
                        "author_name": "Synthetic",
                        "body": "Refused synthetic",
                    }
                    if kind == "comment"
                    else {"seq": 1}
                )
                await db.execute(
                    insert(model.__table__).values(
                        org_id=ids["org_a"], report_id=ids["report_b"], **fields
                    )
                )
            else:
                values = (
                    {"org_id": ids["org_b"]}
                    if operation == "org_update"
                    else {"report_id": ids["report_b"]}
                )
                await db.execute(
                    update(model.__table__).where(model.id == ids[f"{kind}_a"]).values(**values)
                )
    assert caught.value.orig.sqlstate == "23503"
    assert _constraint_name(caught.value) == constraint


@pytest.mark.parametrize("model,kind,_constraint", CHILDREN)
async def test_native_rls_refuses_foreign_owned_correct_pairs(
    children_pg, model, kind, _constraint
):
    _, db, ids = children_pg
    await _scope(db, ids["org_a"])
    fields = (
        {"author_id": ids["user_b"], "author_name": "Synthetic", "body": "Refused synthetic"}
        if kind == "comment"
        else {"seq": 1}
    )
    with pytest.raises(DBAPIError) as caught:
        async with db.begin_nested():
            await db.execute(
                insert(model.__table__).values(
                    org_id=ids["org_b"], report_id=ids["report_b"], **fields
                )
            )
    assert caught.value.orig.sqlstate == "42501"


async def test_native_cascade_preserves_other_tenant_and_optional_transaction_identity(children_pg):
    _, db, ids = children_pg
    await _scope(db, "")
    before = await _rows(db, ids)
    await _scope(db, ids["org_a"])
    await db.execute(delete(ExpenseReport.__table__).where(ExpenseReport.id == ids["report_a"]))
    for model, kind in (
        (ExpenseItem, "item"),
        (ExpenseComment, "comment"),
        (ExpenseApprovalStep, "step"),
    ):
        assert (
            await db.scalar(
                select(func.count())
                .select_from(model.__table__)
                .where(model.id == ids[f"{kind}_a"])
            )
            == 0
        )
    assert (
        await db.execute(
            select(ExpenseTransaction.org_id, ExpenseTransaction.item_id).where(
                ExpenseTransaction.id == ids["txn_a"]
            )
        )
    ).one() == (ids["org_a"], None)
    # Compare stored rows under the same maintenance scope as the baseline;
    # User visibility additionally requires a membership, outside this fixture.
    await _scope(db, "")
    after = await _rows(db, ids)
    for model, kind in MODELS:
        assert [row for row in after[model.__tablename__] if row["id"] == ids[f"{kind}_b"]] == [
            row for row in before[model.__tablename__] if row["id"] == ids[f"{kind}_b"]
        ]


async def test_native_roundtrip_preserves_exact_rows_and_catalog_including_159(children_pg):
    _, db, ids = children_pg
    await _scope(db, "")
    before_rows, before_catalog = await _rows(db, ids), await _catalog(db)
    await db.run_sync(_apply, "downgrade")
    await db.run_sync(_apply, "upgrade")
    assert await _rows(db, ids) == before_rows
    assert await _catalog(db) == before_catalog


async def test_native_global_preflight_counts_both_other_tenant_children_without_repair(
    children_pg,
):
    _, db, ids = children_pg
    await _scope(db, "")
    await db.run_sync(_apply, "downgrade")
    await _scope(db, ids["org_b"])
    for model, kind, _constraint in CHILDREN:
        await db.execute(
            update(model.__table__)
            .where(model.id == ids[f"{kind}_b"])
            .values(report_id=ids["report_a"])
        )
    await _scope(db, "")
    before_rows, before_catalog = await _rows(db, ids), await _catalog(db)
    with pytest.raises(RuntimeError, match="inconsistent history") as caught:
        await db.run_sync(_apply, "upgrade")
    for table, _composite, _original in migration.LINKS:
        assert f"{table}: missing_parent=0, wrong_tenant=1" in str(caught.value)
    assert all(value not in str(caught.value) for value in ids.values())
    assert await _rows(db, ids) == before_rows
    assert await _catalog(db) == before_catalog


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
@pytest.mark.parametrize("phase", ["second_replacement", "postcheck"])
async def test_native_two_replacements_rollback_atomically_on_late_failure(
    children_pg, monkeypatch, direction, phase
):
    _, db, ids = children_pg
    await _scope(db, "")
    if direction == "upgrade":
        await db.run_sync(_apply, "downgrade")
    before_rows, before_catalog = await _rows(db, ids), await _catalog(db)
    original_replace, original_check = migration._replace_fk, migration._check_sqlite_foreign_keys
    replaced, checks = [], []

    def replace(connection, table, *args):
        if phase == "second_replacement" and table == "expense_approval_steps":
            assert replaced == ["expense_comments"]
            raise RuntimeError("Synthetic second replacement failure")
        original_replace(connection, table, *args)
        replaced.append(table)

    def check(connection):
        original_check(connection)
        checks.append(True)
        if phase == "postcheck" and len(checks) == 2:
            assert replaced == ["expense_comments", "expense_approval_steps"]
            raise RuntimeError("Synthetic postcheck failure")

    monkeypatch.setattr(migration, "_replace_fk", replace)
    monkeypatch.setattr(migration, "_check_sqlite_foreign_keys", check)
    with pytest.raises(RuntimeError, match="Synthetic"):
        await db.run_sync(_apply, direction)
    assert await _rows(db, ids) == before_rows
    assert await _catalog(db) == before_catalog


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
async def test_native_migration_refuses_scoped_connection_before_changes(children_pg, direction):
    _, db, ids = children_pg
    await _scope(db, ids["org_a"])
    before = await _catalog(db)
    with pytest.raises(RuntimeError, match="unscoped migration connection"):
        await db.run_sync(_apply, direction)
    assert await _catalog(db) == before


@pytest.mark.parametrize("table", ["expense_reports", "expense_comments", "expense_approval_steps"])
async def test_native_preflight_excludes_writes_to_all_three_tables(children_pg, table):
    engine, db, _ids = children_pg
    await _scope(db, "")
    await db.run_sync(migration._prepare)
    async with engine.connect() as writer:
        with pytest.raises(DBAPIError) as caught:
            async with writer.begin():
                await writer.execute(text("SET LOCAL lock_timeout = '250ms'"))
                await writer.execute(text(f"UPDATE {table} SET id=id WHERE false"))
        assert caught.value.orig.sqlstate == "55P03"
