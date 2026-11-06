"""Actual SQLite166 preserves four link contracts and all retained source data."""

from __future__ import annotations

import json
import shutil
import sys
from contextlib import contextmanager
from copy import deepcopy
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import IntegrityError

from alembic import op
from tests.test_migrations import BACKEND_DIR, _run_alembic
from tests.test_planning_ocr_migration_integration import schemas
from tests.test_residual_parent_tenant_links import LINKS
from tests.test_residual_parent_tenant_links import _rows as _all_rows
from tests.test_residual_parent_tenant_links import _seed as _seed

PARENT = "d1e3f5a7c9b2"
HEAD = "e2f4a6b8d0c3"


def _revision():
    config = Config()
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return ScriptDirectory.from_config(config).get_revision(HEAD).module


def _apply(connection, direction):
    with Operations.context(MigrationContext.configure(connection)):
        getattr(_revision(), direction)()


def _migrate(path, direction, revision):
    result = _run_alembic([direction, revision], f"sqlite+aiosqlite:///{path}")
    assert result.returncode == 0, result.stdout + result.stderr


@contextmanager
def _connection(path):
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as connection:
            yield connection
    finally:
        engine.dispose()


def _physical(connection):
    return connection.execute(
        text(
            "SELECT type,name,tbl_name,sql,rootpage FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        )
    ).all()


def _expected_added_schema(before, *, postgres=False):
    expected = deepcopy(before)
    for model, column, parent, _parent_key, _child_key, name, nullable in LINKS:
        table = model.__tablename__
        if not nullable:
            old = [
                fk
                for fk in expected[table]["foreign_keys"]
                if fk["constrained_columns"] == [column]
            ]
            assert len(old) == 1
            assert old[0]["name"] == ("customer_contacts_customer_id_fkey" if postgres else None)
            assert old[0]["referred_table"] == parent
            assert old[0]["referred_columns"] == ["id"]
            assert old[0]["options"] == {"ondelete": "CASCADE"}
            pair = deepcopy(old[0])
            pair.update(
                name=name, constrained_columns=["org_id", column], referred_columns=["org_id", "id"]
            )
            expected[table]["foreign_keys"].remove(old[0])
        else:
            pair = dict(
                name=name,
                constrained_columns=["org_id", column],
                referred_schema=None,
                referred_table=parent,
                referred_columns=["org_id", "id"],
                options={"ondelete": f"SET NULL ({column})"} if postgres else {},
            )
            if postgres:
                pair["comment"] = None
        expected[table]["foreign_keys"].append(pair)
    for table, value in expected.items():
        if table.startswith("_"):
            continue
        for key in ("indexes", "foreign_keys", "unique_constraints", "check_constraints"):
            value[key].sort(key=lambda row: json.dumps(row, sort_keys=True))
    return expected


@pytest.fixture(scope="module")
def parent_template(tmp_path_factory):
    path = tmp_path_factory.mktemp("residual-parent165") / "parent.sqlite"
    _migrate(path, "upgrade", PARENT)
    with _connection(path) as connection:
        identities = _seed(connection)
    return path, identities


@pytest.fixture
def historical_database(parent_template, tmp_path):
    source, identities = parent_template
    path = tmp_path / "residual-history.sqlite"
    shutil.copyfile(source, path)
    return path, identities


def test_actual166_cli_roundtrip_preserves_complete_schema_history_and_original_names(
    historical_database,
):
    path, ids = historical_database
    with _connection(path) as connection:
        before, schema, physical = _all_rows(connection), schemas(connection), _physical(connection)
        old_singles = {
            (model.__tablename__, column): next(
                fk
                for fk in schema[model.__tablename__]["foreign_keys"]
                if fk["constrained_columns"] == [column]
            )["name"]
            for model, column, *_rest in LINKS
        }
        assert old_singles == {
            ("customer_contacts", "customer_id"): None,
            ("inbound_invoices", "invoice_id"): None,
            ("issued_invoices", "customer_id"): "fk_issued_invoices_customer",
            ("issued_invoices", "corrected_invoice_id"): "fk_issued_corrected",
        }
    untouched = {
        row
        for row in physical
        if row[2] not in {"customer_contacts", "inbound_invoices", "issued_invoices"}
    }
    for direction, target in (("upgrade", HEAD), ("downgrade", PARENT), ("upgrade", HEAD)):
        _migrate(path, direction, target)
        with _connection(path) as connection:
            assert _all_rows(connection) == before
            assert schemas(connection) == (
                _expected_added_schema(schema) if target == HEAD else schema
            )
            assert {
                row
                for row in _physical(connection)
                if row[2] not in {"customer_contacts", "inbound_invoices", "issued_invoices"}
            } == untouched
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == target
            assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    with _connection(path) as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.commit()
        for model, column, _parent, parent_key, child_key, _name, _nullable in LINKS:
            with (
                pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"),
                connection.begin_nested(),
            ):
                connection.execute(
                    text(f"UPDATE {model.__tablename__} SET {column}=:parent WHERE id=:id"),
                    {"parent": ids[1][parent_key], "id": ids[0][child_key]},
                )


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_enforced_sqlite_connection_refuses_before_any_ddl(historical_database, direction):
    path, _ = historical_database
    if direction == "downgrade":
        _migrate(path, "upgrade", HEAD)
    with _connection(path) as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.commit()
        before, physical = _all_rows(connection), _physical(connection)
        statements = []

        def observed(_c, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)

        event.listen(connection, "before_cursor_execute", observed)
        try:
            with pytest.raises(RuntimeError, match="dedicated SQLite foreign_keys=OFF"):
                _apply(connection, direction)
        finally:
            event.remove(connection, "before_cursor_execute", observed)
        assert not any(
            s.lstrip().upper().startswith(("CREATE", "DROP", "ALTER")) for s in statements
        )
        assert not any("FOREIGN_KEYS=OFF" in s.replace(" ", "").upper() for s in statements)
        assert _all_rows(connection) == before and _physical(connection) == physical
        assert connection.scalar(text("PRAGMA foreign_keys")) == 1


@pytest.mark.parametrize("model,column,parent,parent_key,child_key,name,nullable", LINKS)
@pytest.mark.parametrize("fault", ["wrong_tenant", "missing_parent"])
def test_each_masked_bad_history_is_refused_without_repair(
    historical_database, model, column, parent, parent_key, child_key, name, nullable, fault
):
    path, ids = historical_database
    missing = str(uuid4())
    with _connection(path) as connection:
        connection.execute(
            text(f"UPDATE {model.__tablename__} SET {column}=:parent WHERE id=:id"),
            {
                "parent": ids[1][parent_key] if fault == "wrong_tenant" else missing,
                "id": ids[0][child_key],
            },
        )
        connection.commit()
        before, physical = _all_rows(connection), _physical(connection)
        with pytest.raises(
            RuntimeError,
            match="wrong_tenant=1"
            if fault == "wrong_tenant"
            else "existing foreign-key violations",
        ) as caught:
            _apply(connection, "upgrade")
        assert all(
            str(value) not in str(caught.value) for identity in ids for value in identity.values()
        )
        assert missing not in str(caught.value)
        assert _all_rows(connection) == before and _physical(connection) == physical
        if fault == "missing_parent":
            with pytest.raises(RuntimeError, match="missing_parent=1, wrong_tenant=0"):
                _revision()._preflight(connection)


def test_all_four_inconsistent_links_are_reported_together_without_identifiers(historical_database):
    path, ids = historical_database
    with _connection(path) as connection:
        for model, column, _parent, parent_key, child_key, _name, _nullable in LINKS:
            connection.execute(
                text(f"UPDATE {model.__tablename__} SET {column}=:parent WHERE id=:id"),
                {"parent": ids[1][parent_key], "id": ids[0][child_key]},
            )
        connection.commit()
        before, physical = _all_rows(connection), _physical(connection)
        with pytest.raises(RuntimeError, match="inconsistent history") as caught:
            _apply(connection, "upgrade")
        assert str(caught.value).count("missing_parent=0, wrong_tenant=1") == 4
        assert all(
            str(value) not in str(caught.value) for identity in ids for value in identity.values()
        )
        assert _all_rows(connection) == before and _physical(connection) == physical


def test_unrelated_incoming_orphan_is_refused_without_cleanup(historical_database):
    path, _ = historical_database
    missing = str(uuid4())
    with _connection(path) as connection:
        connection.execute(
            text("UPDATE issued_invoice_attachments SET invoice_id=:id"), {"id": missing}
        )
        connection.commit()
        before, physical = _all_rows(connection), _physical(connection)
        with pytest.raises(RuntimeError, match="existing foreign-key violations") as caught:
            _apply(connection, "upgrade")
        assert missing not in str(caught.value)
        assert _all_rows(connection) == before and _physical(connection) == physical


@pytest.mark.parametrize("model,column,parent,parent_key,child_key,name,nullable", LINKS)
def test_preexisting_named_pair_is_not_adopted(
    historical_database, model, column, parent, parent_key, child_key, name, nullable
):
    path, _ = historical_database
    with _connection(path) as connection:
        with Operations.context(MigrationContext.configure(connection)):
            with op.batch_alter_table(model.__tablename__, recreate="always") as batch:
                batch.create_foreign_key(
                    name,
                    parent,
                    ["org_id", column],
                    ["org_id", "id"],
                    ondelete=None if nullable else "CASCADE",
                )
        connection.commit()
        before, physical = _all_rows(connection), _physical(connection)
        with pytest.raises(RuntimeError, match="no adoption"):
            _apply(connection, "upgrade")
        assert _all_rows(connection) == before and _physical(connection) == physical


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
@pytest.mark.parametrize("fault", ["fourth_replacement", "postvalidation"])
def test_late_failure_restores_every_schema_object_and_row_before_outer_cleanup(
    historical_database, monkeypatch, direction, fault
):
    path, _ = historical_database
    if direction == "downgrade":
        _migrate(path, "upgrade", HEAD)
    migration = _revision()
    monkeypatch.setattr(sys.modules[__name__], "_revision", lambda: migration)
    calls = []
    if fault == "fourth_replacement":
        original = migration._replace_fk

        def fail(connection, spec, *, upgrade):
            original(connection, spec, upgrade=upgrade)
            calls.append(spec.column)
            if len(calls) == 4:
                raise RuntimeError("Synthetic fourth replacement failure")

        monkeypatch.setattr(migration, "_replace_fk", fail)
    else:
        original = migration._check_sqlite_foreign_keys

        def fail(connection):
            original(connection)
            calls.append(True)
            if len(calls) == 2:
                raise RuntimeError("Synthetic postvalidation failure")

        monkeypatch.setattr(migration, "_check_sqlite_foreign_keys", fail)
    with _connection(path) as connection:
        before, schema, physical = _all_rows(connection), schemas(connection), _physical(connection)
        with pytest.raises(RuntimeError, match="Synthetic"):
            _apply(connection, direction)
        assert len(calls) == (4 if fault == "fourth_replacement" else 2)
        assert (
            _all_rows(connection) == before
            and schemas(connection) == schema
            and _physical(connection) == physical
        )
        connection.commit()
    with _connection(path) as connection:
        assert (
            _all_rows(connection) == before
            and schemas(connection) == schema
            and _physical(connection) == physical
        )
