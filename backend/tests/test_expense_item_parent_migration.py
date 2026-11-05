"""Actual SQLite migration preserves every historical row and optional link."""

from __future__ import annotations

import shutil
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from tests.test_migrations import BACKEND_DIR, _run_alembic
from tests.test_planning_ocr_migration_integration import _insert

REVISION = "d5e7f9a1b3c6"
PARENT = "c3f5a7b9d1e4"
TABLES = (
    "organizations",
    "users",
    "expense_reports",
    "expense_items",
    "expense_transactions",
    "expense_comments",
    "expense_approval_steps",
)
STAMP = datetime(2026, 11, 5, 12, tzinfo=UTC)


def _revision():
    config = Config()
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return ScriptDirectory.from_config(config).get_revision(REVISION).module


def _migrate(path, *args):
    result = _run_alembic(list(args), f"sqlite+aiosqlite:///{path}")
    assert result.returncode == 0, result.stdout + result.stderr


@contextmanager
def _connection(path):
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as connection:
            yield connection
    finally:
        engine.dispose()


def _rows(connection):
    return {
        table: [
            dict(row)
            for row in connection.execute(text(f"SELECT * FROM {table} ORDER BY id")).mappings()
        ]
        for table in TABLES
    }


def _schema(connection):
    return connection.exec_driver_sql(
        "SELECT name,sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
    ).all()


def _apply(connection, migration, direction):
    with Operations.context(MigrationContext.configure(connection)):
        getattr(migration, direction)()


def _seed(connection):
    identities = []
    for index in range(2):
        org, user, report, item, transaction, comment, step = [str(uuid4()) for _ in range(7)]
        _insert(connection, "organizations", id=org, name=f"Synthetic parent migration {index}")
        _insert(
            connection,
            "users",
            id=user,
            org_id=org,
            email=f"{user}@example.com",
            name="Synthetic claimant",
            hashed_password="not-a-login-hash",
        )
        _insert(
            connection,
            "expense_reports",
            id=report,
            org_id=org,
            employee_id=user,
            employee_name="Synthetic claimant",
            title="Retained report evidence",
            status="draft",
            total=Decimal("42.00"),
            vat_total=Decimal("8.82"),
            total_eur=Decimal("42.00"),
        )
        _insert(
            connection,
            "expense_items",
            id=item,
            org_id=org,
            report_id=report,
            spend_date=date(2026, 5, 1),
            description="Retained item evidence",
            amount=Decimal("42.00"),
            vat_amount=Decimal("8.82"),
            receipt_sha256="f" * 64,
            receipt_size=32,
            receipt_mime="image/png",
            created_at=STAMP,
            updated_at=STAMP,
        )
        _insert(
            connection,
            "expense_transactions",
            id=transaction,
            org_id=org,
            employee_id=user,
            item_id=item,
            txn_date=date(2026, 5, 1),
            description="Retained card statement link",
            amount=Decimal("42.00"),
            currency="EUR",
            source="card",
            status="assigned",
        )
        _insert(
            connection,
            "expense_comments",
            id=comment,
            org_id=org,
            report_id=report,
            author_id=user,
            author_name="Synthetic claimant",
            body="Retained review evidence",
        )
        _insert(connection, "expense_approval_steps", id=step, org_id=org, report_id=report, seq=0)
        identities.append(
            dict(org=org, user=user, report=report, item=item, transaction=transaction)
        )
    connection.commit()
    return identities


@pytest.fixture(scope="module")
def parent_template(tmp_path_factory):
    path = tmp_path_factory.mktemp("expense-parent-history") / "parent.sqlite"
    _migrate(path, "upgrade", PARENT)
    return path


@pytest.fixture
def historical_database(parent_template, tmp_path):
    path = tmp_path / "history.sqlite"
    shutil.copyfile(parent_template, path)
    with _connection(path) as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 0
        identities = _seed(connection)
    return path, identities


def _assert_parent_fk(connection, composite):
    keys = [
        key
        for key in inspect(connection).get_foreign_keys("expense_items")
        if key["referred_table"] == "expense_reports"
    ]
    assert len(keys) == 1
    assert keys[0]["constrained_columns"] == (
        ["org_id", "report_id"] if composite else ["report_id"]
    )
    assert keys[0]["referred_columns"] == (["org_id", "id"] if composite else ["id"])
    assert keys[0]["options"]["ondelete"] == "CASCADE"
    targets = [
        i
        for i in inspect(connection).get_indexes("expense_reports")
        if i["name"] == "uq_expense_reports_org_id"
    ]
    assert bool(targets) is composite
    if composite:
        assert targets[0]["unique"] and targets[0]["column_names"] == ["org_id", "id"]
    assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []


@pytest.mark.slow
def test_actual_cli_roundtrip_preserves_reports_children_and_optional_links(historical_database):
    path, identities = historical_database
    with _connection(path) as connection:
        before = _rows(connection)
        _assert_parent_fk(connection, False)
    for direction, target, composite in (
        ("upgrade", REVISION, True),
        ("downgrade", PARENT, False),
        ("upgrade", REVISION, True),
    ):
        _migrate(path, direction, target)
        with _connection(path) as connection:
            assert _rows(connection) == before
            _assert_parent_fk(connection, composite)
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 0
    with _connection(path) as connection:
        # Enable enforcement only on this fresh application-like connection,
        # outside a transaction; the migration itself must never toggle it.
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.commit()
        with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
            with connection.begin_nested():
                connection.execute(
                    text("UPDATE expense_items SET report_id=:parent WHERE id=:item"),
                    {"parent": identities[1]["report"], "item": identities[0]["item"]},
                )


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_fk_on_connection_refuses_before_any_schema_or_data_change(historical_database, direction):
    path, _ = historical_database
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
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert _rows(connection) == before and _schema(connection) == schema


@pytest.mark.parametrize("fault", ["wrong_tenant", "missing_parent", "dangling_transaction"])
def test_history_preflight_refuses_without_rewriting_evidence(historical_database, fault):
    path, identities = historical_database
    migration = _revision()
    with _connection(path) as connection:
        if fault == "dangling_transaction":
            missing = str(uuid4())
            connection.execute(
                text("UPDATE expense_transactions SET item_id=:missing WHERE id=:id"),
                {"missing": missing, "id": identities[0]["transaction"]},
            )
        else:
            missing = str(uuid4())
            connection.execute(
                text("UPDATE expense_items SET report_id=:parent WHERE id=:id"),
                {
                    "parent": identities[1]["report"] if fault == "wrong_tenant" else missing,
                    "id": identities[0]["item"],
                },
            )
        connection.commit()
        before, schema = _rows(connection), _schema(connection)
        expected = (
            "wrong_tenant=1" if fault == "wrong_tenant" else "existing foreign-key violations"
        )
        with pytest.raises(RuntimeError, match=expected) as refused:
            _apply(connection, migration, "upgrade")
        assert missing not in str(refused.value)
        assert all(identity["org"] not in str(refused.value) for identity in identities)
        assert _rows(connection) == before and _schema(connection) == schema
        if fault == "missing_parent":
            with pytest.raises(RuntimeError, match="missing_parent=1, wrong_tenant=0"):
                migration._preflight(connection)


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_late_validation_failure_restores_actual_schema_rows_and_links(
    historical_database, monkeypatch, direction
):
    path, _ = historical_database
    migration = _revision()
    with _connection(path) as connection:
        if direction == "downgrade":
            _apply(connection, migration, "upgrade")
            connection.commit()
        before, schema = _rows(connection), _schema(connection)
        original = migration._check_sqlite_foreign_keys
        calls = []

        def fail_after_copy(bound):
            original(bound)
            calls.append(True)
            if len(calls) == 2:
                raise RuntimeError("Synthetic post-copy validation failure")

        monkeypatch.setattr(migration, "_check_sqlite_foreign_keys", fail_after_copy)
        with pytest.raises(RuntimeError, match="Synthetic post-copy validation failure"):
            _apply(connection, migration, direction)
        assert len(calls) == 2
        # Observe rollback inside the still-owned outer transaction, before
        # connection close/rollback could conceal an unprotected DDL change.
        assert _rows(connection) == before and _schema(connection) == schema
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 0
        connection.commit()
    with _connection(path) as reopened:
        assert _rows(reopened) == before and _schema(reopened) == schema
