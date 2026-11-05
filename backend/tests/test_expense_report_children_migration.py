"""Revision160 preserves both report children atomically on actual SQLite history."""

from __future__ import annotations

import shutil
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from tests.test_expense_item_parent_migration import (
    _assert_parent_fk,
    _connection,
    _migrate,
    _rows,
    _schema,
    _seed,
)
from tests.test_migrations import BACKEND_DIR

REVISION = "e6f8a0b2c4d7"
PARENT = "d5e7f9a1b3c6"
BEFORE_ITEM = "c3f5a7b9d1e4"
CHILDREN = ("expense_comments", "expense_approval_steps")


def _revision():
    config = Config()
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return ScriptDirectory.from_config(config).get_revision(REVISION).module


def _apply(connection, migration, direction):
    with Operations.context(MigrationContext.configure(connection)):
        getattr(migration, direction)()


def _report_fk(connection, table):
    keys = [
        key
        for key in inspect(connection).get_foreign_keys(table)
        if key["referred_table"] == "expense_reports"
    ]
    assert len(keys) == 1
    return keys[0]


def _unrelated_catalogue(connection):
    inspector = inspect(connection)
    return {
        table: {
            "foreign_keys": [
                key
                for key in inspector.get_foreign_keys(table)
                if key["referred_table"] != "expense_reports"
            ],
            "indexes": inspector.get_indexes(table),
            "unique": inspector.get_unique_constraints(table),
            "primary_key": inspector.get_pk_constraint(table),
            "columns": [
                (column["name"], str(column["type"]), column["nullable"], column["default"])
                for column in inspector.get_columns(table)
            ],
        }
        for table in CHILDREN
    }


def _protected_schema(connection):
    return connection.exec_driver_sql(
        "SELECT name,tbl_name,sql FROM sqlite_schema WHERE tbl_name IN "
        "('expense_reports','expense_items','expense_transactions') ORDER BY name"
    ).all()


def _assert_links(connection, composite, *, restored=False):
    for table in CHILDREN:
        key = _report_fk(connection, table)
        assert key["constrained_columns"] == (
            ["org_id", "report_id"] if composite else ["report_id"]
        )
        assert key["referred_columns"] == (["org_id", "id"] if composite else ["id"])
        assert key["options"]["ondelete"] == "CASCADE"
        assert key["name"] == (
            f"fk_{table}_report" if composite else f"{table}_report_id_fkey" if restored else None
        )
    _assert_parent_fk(connection, True)


@pytest.fixture(scope="module")
def children_parent_template(tmp_path_factory):
    path = tmp_path_factory.mktemp("expense-children-history") / "parent.sqlite"
    _migrate(path, "upgrade", BEFORE_ITEM)
    with _connection(path) as connection:
        original_item_name = _report_fk(connection, "expense_items")["name"]
        assert original_item_name is None
    _migrate(path, "upgrade", PARENT)
    return path, original_item_name


@pytest.fixture
def children_database(children_parent_template, tmp_path):
    template, original_item_name = children_parent_template
    path = tmp_path / "history.sqlite"
    shutil.copyfile(template, path)
    with _connection(path) as connection:
        identities = _seed(connection)
        for table in CHILDREN:
            org_key = next(
                key
                for key in inspect(connection).get_foreign_keys(table)
                if key["referred_table"] == "organizations"
            )
            assert org_key["name"] is None
    return path, identities, original_item_name


@pytest.mark.slow
def test_actual_160_cli_roundtrip_preserves_all_rows_and_unrelated_physical_schema(
    children_database,
):
    path, _, _ = children_database
    with _connection(path) as connection:
        before, untouched = _rows(connection), _unrelated_catalogue(connection)
        protected = _protected_schema(connection)
        _assert_links(connection, False)
    for direction, target, composite, restored in (
        ("upgrade", REVISION, True, False),
        ("downgrade", PARENT, False, True),
        ("upgrade", REVISION, True, False),
    ):
        _migrate(path, direction, target)
        with _connection(path) as connection:
            assert _rows(connection) == before
            assert _unrelated_catalogue(connection) == untouched
            assert _protected_schema(connection) == protected
            _assert_links(connection, composite, restored=restored)
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == target
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 0


@pytest.mark.slow
def test_159_sqlite_downgrade_restores_semantics_with_documented_named_report_fk(children_database):
    path, _, original_item_name = children_database
    with _connection(path) as connection:
        before = _rows(connection)
    _migrate(path, "downgrade", BEFORE_ITEM)
    with _connection(path) as connection:
        restored = _report_fk(connection, "expense_items")
        assert original_item_name is None
        assert restored["name"] == "expense_items_report_id_fkey"
        assert restored["constrained_columns"] == ["report_id"]
        assert restored["referred_columns"] == ["id"]
        assert restored["options"]["ondelete"] == "CASCADE"
        assert _rows(connection) == before
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    # Revision159 stays byte-for-byte unchanged; record this physical-name
    # difference without claiming that its downgrade restores unnamed DDL.


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_160_fk_on_connection_refuses_before_any_change(children_database, direction):
    path, _, _ = children_database
    migration = _revision()
    with _connection(path) as connection:
        if direction == "downgrade":
            _apply(connection, migration, "upgrade")
            connection.commit()
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.commit()
        before, schema = _rows(connection), _schema(connection)
        with pytest.raises(RuntimeError, match="dedicated SQLite foreign_keys=OFF"):
            _apply(connection, migration, direction)
        assert _rows(connection) == before and _schema(connection) == schema
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1


@pytest.mark.parametrize("table", CHILDREN)
@pytest.mark.parametrize("fault", ["wrong_tenant", "missing_parent"])
def test_160_preflight_masks_and_preserves_inconsistent_child_history(
    children_database, table, fault
):
    path, identities, _ = children_database
    migration = _revision()
    missing = str(uuid4())
    with _connection(path) as connection:
        connection.execute(
            text(f"UPDATE {table} SET report_id=:parent WHERE org_id=:org"),
            {
                "parent": identities[1]["report"] if fault == "wrong_tenant" else missing,
                "org": identities[0]["org"],
            },
        )
        connection.commit()
        before, schema = _rows(connection), _schema(connection)
        with pytest.raises(RuntimeError) as refused:
            _apply(connection, migration, "upgrade")
        message = str(refused.value)
        assert table in message
        assert (
            "wrong_tenant=1" if fault == "wrong_tenant" else "foreign-key violations"
        ) in message
        assert all(identity["org"] not in message for identity in identities)
        assert missing not in message
        assert _rows(connection) == before and _schema(connection) == schema
        if fault == "missing_parent":
            with pytest.raises(RuntimeError, match="missing_parent=1, wrong_tenant=0"):
                migration._preflight(connection)


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
@pytest.mark.parametrize("timing", ["before_second", "after_second"])
def test_both_replacements_share_one_atomic_rollback(
    children_database, monkeypatch, direction, timing
):
    path, _, _ = children_database
    migration = _revision()
    with _connection(path) as connection:
        if direction == "downgrade":
            _apply(connection, migration, "upgrade")
            connection.commit()
        before, schema = _rows(connection), _schema(connection)
        original, completed = migration._replace_fk, []

        def fail_second(bound, table, old_name, new_name, columns, referred_columns):
            if table == "expense_approval_steps":
                assert completed == ["expense_comments"]
                if timing == "before_second":
                    raise RuntimeError("Synthetic second replacement failure")
            original(bound, table, old_name, new_name, columns, referred_columns)
            completed.append(table)
            if table == "expense_approval_steps" and timing == "after_second":
                raise RuntimeError("Synthetic second replacement failure")

        monkeypatch.setattr(migration, "_replace_fk", fail_second)
        with pytest.raises(RuntimeError, match="Synthetic second replacement failure"):
            _apply(connection, migration, direction)
        assert completed == (list(CHILDREN) if timing == "after_second" else ["expense_comments"])
        assert _rows(connection) == before and _schema(connection) == schema
        connection.commit()
    with _connection(path) as reopened:
        assert _rows(reopened) == before and _schema(reopened) == schema


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_160_postvalidation_failure_rolls_back_both_copies(
    children_database, monkeypatch, direction
):
    path, _, _ = children_database
    migration = _revision()
    with _connection(path) as connection:
        if direction == "downgrade":
            _apply(connection, migration, "upgrade")
            connection.commit()
        before, schema = _rows(connection), _schema(connection)
        original, calls = migration._check_sqlite_foreign_keys, []

        def fail_after_copy(bound):
            original(bound)
            calls.append(True)
            if len(calls) == 2:
                raise RuntimeError("Synthetic child postvalidation failure")

        monkeypatch.setattr(migration, "_check_sqlite_foreign_keys", fail_after_copy)
        with pytest.raises(RuntimeError, match="Synthetic child postvalidation failure"):
            _apply(connection, migration, direction)
        assert len(calls) == 2
        assert _rows(connection) == before and _schema(connection) == schema
        connection.commit()
    with _connection(path) as reopened:
        assert _rows(reopened) == before and _schema(reopened) == schema
