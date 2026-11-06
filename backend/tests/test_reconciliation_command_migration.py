"""Actual165 is additive, FK-on safe and refuses destructive history loss."""

from __future__ import annotations

import json
import shutil
from contextlib import contextmanager
from datetime import date
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from tests.test_migrations import BACKEND_DIR, _run_alembic
from tests.test_planning_ocr_migration_integration import _insert, schemas

PARENT = "c0d2e4f6a8b1"
HEAD = "d1e3f5a7c9b2"


def _revision():
    config = Config()
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return ScriptDirectory.from_config(config).get_revision(HEAD).module


def _migrate(path, direction, revision):
    result = _run_alembic([direction, revision], "sqlite+aiosqlite:///" + str(path))
    assert result.returncode == 0, result.stdout + result.stderr


@contextmanager
def _connection(path):
    engine = create_engine("sqlite:///" + str(path))
    try:
        with engine.connect() as connection:
            connection.execute(text("PRAGMA foreign_keys=ON"))
            connection.commit()
            yield connection
    finally:
        engine.dispose()


def _rows(connection):
    result = {
        table: [dict(r) for r in connection.execute(text(f'SELECT * FROM "{table}"')).mappings()]
        for table in sorted(inspect(connection).get_table_names())
        if table != "alembic_version"
    }
    return json.loads(
        json.dumps(
            {
                table: sorted(rows, key=lambda row: json.dumps(row, default=str, sort_keys=True))
                for table, rows in result.items()
            },
            default=str,
            sort_keys=True,
        )
    )


def _seed(connection):
    identities = []
    kinds = ("receipt", "reimbursement", "payment_run", "issued_payment")
    for letter in "ab":
        org, user, statement = (str(uuid4()) for _ in range(3))
        _insert(connection, "organizations", id=org, name="Synthetic bank " + letter)
        _insert(
            connection,
            "users",
            id=user,
            org_id=org,
            email=uuid4().hex + "@example.invalid",
            name="Synthetic actor",
            hashed_password="not-a-login",
        )
        _insert(
            connection,
            "bank_statements",
            id=statement,
            org_id=org,
            filename="retained.csv",
            sha256=letter * 64,
        )
        lines = []
        for position, status in enumerate(("unmatched", "ignored", *("matched",) * 4)):
            line = str(uuid4())
            _insert(
                connection,
                "bank_lines",
                id=line,
                org_id=org,
                statement_id=statement,
                line_date=date(2026, 11, 6),
                amount="-12.34",
                direction="debit",
                balance="98.76",
                description="Original unclassified source",
                currency=None,
                status=status,
                matched_kind=kinds[position - 2] if status == "matched" else None,
                matched_id=str(uuid4()) if status == "matched" else None,
            )
            lines.append(line)
        # Legacy invalid-money source is deliberately untouched by migration.
        connection.execute(
            text("UPDATE bank_lines SET amount='NaN',balance='NaN' WHERE id=:id"), {"id": lines[0]}
        )
        identities.append(dict(org=org, user=user, statement=statement, line=lines[0], lines=lines))
    connection.commit()
    return identities


def _apply(connection, direction, migration=None):
    with Operations.context(MigrationContext.configure(connection)):
        getattr(migration or _revision(), direction)()


def _without_version(rows):
    old = json.loads(json.dumps(rows))
    assert old.pop("reconciliation_commands") == []
    for line in old["bank_lines"]:
        assert line.pop("version") == 1
    return old


def _physical(connection):
    return connection.execute(
        text(
            "SELECT type,name,tbl_name,sql,rootpage FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        )
    ).all()


@pytest.fixture(scope="module")
def parent_template(tmp_path_factory):
    path = tmp_path_factory.mktemp("reconciliation164") / "parent.sqlite"
    _migrate(path, "upgrade", PARENT)
    with _connection(path) as connection:
        ids = _seed(connection)
    return path, ids


@pytest.fixture
def legacy_database(parent_template, tmp_path):
    source, ids = parent_template
    path = tmp_path / "history.sqlite"
    shutil.copyfile(source, path)
    return path, ids


def test_actual165_upgrade_empty_rollback_and_model_parity_preserve_all_legacy_source(
    legacy_database,
):
    path, _ = legacy_database
    with _connection(path) as connection:
        before, schema = _rows(connection), schemas(connection)
        original_root = connection.scalar(
            text("SELECT rootpage FROM sqlite_schema WHERE name='bank_lines'")
        )
    for direction, target in [("upgrade", HEAD), ("downgrade", PARENT), ("upgrade", HEAD)]:
        _migrate(path, direction, target)
        with _connection(path) as connection:
            assert connection.scalar(text("PRAGMA foreign_keys")) == 1
            assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
            assert (
                connection.scalar(
                    text("SELECT rootpage FROM sqlite_schema WHERE name='bank_lines'")
                )
                == original_root
            )
            assert (
                _without_version(_rows(connection)) if target == HEAD else _rows(connection)
            ) == before
            if target == PARENT:
                assert schemas(connection) == schema
            else:
                _revision()._check_installed(connection)
                assert connection.scalar(text("SELECT count(*) FROM reconciliation_commands")) == 0
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == target


def test_fk_on_direct_upgrade_and_empty_downgrade_are_atomic_without_parent_rebuild(
    legacy_database,
):
    path, _ = legacy_database
    with _connection(path) as connection:
        before, schema = _rows(connection), schemas(connection)
        root = connection.scalar(text("SELECT rootpage FROM sqlite_schema WHERE name='bank_lines'"))
        _apply(connection, "upgrade")
        assert connection.scalar(text("PRAGMA foreign_keys")) == 1
        assert _without_version(_rows(connection)) == before
        assert (
            connection.scalar(text("SELECT rootpage FROM sqlite_schema WHERE name='bank_lines'"))
            == root
        )
        _apply(connection, "downgrade")
        assert connection.scalar(text("PRAGMA foreign_keys")) == 1
        assert _rows(connection) == before and schemas(connection) == schema
        connection.commit()
    with _connection(path) as connection:
        assert _rows(connection) == before and schemas(connection) == schema


@pytest.mark.parametrize(
    "fault", ["state", "unmatched_association", "ignored_association", "matched_kind", "matched_id"]
)
def test_masked_global_state_association_preflight_never_repairs_history(legacy_database, fault):
    path, (a, b) = legacy_database
    values = {
        "state": "status='lost'",
        "unmatched_association": "matched_kind='receipt'",
        "ignored_association": "status='ignored',matched_id='" + str(uuid4()) + "'",
        "matched_kind": "status='matched',matched_kind='invented',matched_id='"
        + str(uuid4())
        + "'",
        "matched_id": "status='matched',matched_kind='receipt',matched_id=NULL",
    }
    with _connection(path) as connection:
        connection.execute(
            text("UPDATE bank_lines SET " + values[fault] + " WHERE id=:id"), {"id": b["line"]}
        )
        connection.commit()
        before, physical = _rows(connection), _physical(connection)
        with pytest.raises(RuntimeError, match="invalid_state=|invalid_association=") as caught:
            _apply(connection, "upgrade")
        assert all(
            value not in str(caught.value)
            for ids in (a, b)
            for value in (ids["org"], ids["user"], ids["statement"], *ids["lines"])
        )
        assert _rows(connection) == before and _physical(connection) == physical


@pytest.mark.parametrize("object_kind", ["index", "table"])
def test_existing_named_object_is_refused_without_adoption_or_ddl(legacy_database, object_kind):
    path, _ = legacy_database
    with _connection(path) as connection:
        connection.execute(
            text(
                "CREATE INDEX uq_bank_lines_org_id ON bank_lines(id)"
                if object_kind == "index"
                else "CREATE TABLE reconciliation_commands(retained TEXT)"
            )
        )
        connection.commit()
        before, physical = _rows(connection), _physical(connection)
        with pytest.raises(RuntimeError, match="no adoption"):
            _apply(connection, "upgrade")
        assert _rows(connection) == before and _physical(connection) == physical


@pytest.mark.parametrize("advanced", ["version", "history"])
def test_destructive_downgrade_refuses_any_advanced_version_or_receipt(legacy_database, advanced):
    path, ids = legacy_database
    _migrate(path, "upgrade", HEAD)
    with _connection(path) as connection:
        if advanced == "version":
            connection.execute(
                text("UPDATE bank_lines SET version=2 WHERE id=:id"), {"id": ids[1]["line"]}
            )
        else:
            from tests.test_reconciliation_command_schema import _insert_command

            _insert_command(connection, ids[1], actor=ids[0]["user"])
        connection.commit()
        before, physical = _rows(connection), _physical(connection)
        with pytest.raises(RuntimeError, match="Keep additive schema"):
            _apply(connection, "downgrade")
        assert _rows(connection) == before and _physical(connection) == physical


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_late_ddl_failure_restores_every_schema_row_and_name(
    legacy_database, monkeypatch, direction
):
    path, _ = legacy_database
    if direction == "downgrade":
        _migrate(path, "upgrade", HEAD)
    migration = _revision()
    target = migration._check_installed if direction == "upgrade" else migration.op.drop_column

    def fail(*args, **kwargs):
        target(*args, **kwargs)
        raise RuntimeError("Synthetic final reconciliation DDL failure")

    monkeypatch.setattr(
        migration if direction == "upgrade" else migration.op,
        "_check_installed" if direction == "upgrade" else "drop_column",
        fail,
    )
    with _connection(path) as connection:
        before, physical = _rows(connection), _physical(connection)
        with pytest.raises(RuntimeError, match="Synthetic final"):
            _apply(connection, direction, migration)
        assert _rows(connection) == before and _physical(connection) == physical
        connection.commit()
    with _connection(path) as connection:
        assert _rows(connection) == before and _physical(connection) == physical
