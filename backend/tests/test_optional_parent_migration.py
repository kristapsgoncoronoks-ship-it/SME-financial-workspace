"""Actual SQLite164 keeps old physical names, facts, money and incoming links."""

from __future__ import annotations

import json
import shutil
from contextlib import contextmanager
from copy import deepcopy
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from tests.test_capture_reading_migration import _history
from tests.test_capture_reading_migration import _seed as _reading_seed
from tests.test_migrations import BACKEND_DIR, _run_alembic
from tests.test_partner_webhook_parent_migrations import _seed as _parent_seed
from tests.test_planning_ocr_migration_integration import _insert, schemas

PARENT = "b9c1e3f5a7d0"
HEAD = "c0d2e4f6a8b1"


def _revision():
    config = Config()
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return ScriptDirectory.from_config(config).get_revision(HEAD).module


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


def _apply(connection, direction):
    with Operations.context(MigrationContext.configure(connection)):
        getattr(_revision(), direction)()


def _all_rows(connection):
    return {
        table: sorted(
            [dict(row) for row in connection.execute(text(f'SELECT * FROM "{table}"')).mappings()],
            key=lambda row: json.dumps(row, default=str, sort_keys=True),
        )
        for table in sorted(inspect(connection).get_table_names())
        if table != "alembic_version"
    }


def _physical(connection):
    return connection.execute(
        text(
            "SELECT type,name,tbl_name,sql,rootpage FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        )
    ).all()


def _seed(connection):
    identities = _parent_seed(connection)
    for identity in identities:
        org, invoice = identity["org"], identity["invoice"]
        _insert(
            connection,
            "issued_invoice_lines",
            invoice_id=invoice,
            description="Retained issued line",
            quantity=Decimal("2"),
            unit_price=Decimal("20"),
            net_amount=Decimal("40"),
        )
        _insert(
            connection,
            "issued_invoice_attachments",
            org_id=org,
            invoice_id=invoice,
            filename="synthetic-retained.pdf",
            sha256="e" * 64,
            size=32,
        )
        _insert(
            connection,
            "payments",
            org_id=org,
            issued_invoice_id=invoice,
            amount=Decimal("12.34"),
            paid_on=date(2026, 11, 5),
            reference="Retained settlement",
        )
        _insert(
            connection,
            "issued_invoices",
            org_id=org,
            corrected_invoice_id=invoice,
            corrected_invoice_number="SYNTHETIC-ORIGINAL",
            doc_type="credit_note",
            buyer_name="Retained correction snapshot",
            seller_json='{"retained":true}',
            issue_date=date(2026, 11, 5),
            total=Decimal("2.34"),
        )
    for identity in _reading_seed(connection):
        for inbound in (False, True):
            _history(connection, identity, inbound=inbound)
        for owner, key in (("extraction_runs", "run"), ("inbound_invoices", "inbound")):
            connection.execute(
                text(f"UPDATE {owner} SET reading_observation_json=:facts WHERE id=:id"),
                {"facts": '{"outcome":"failure","synthetic":true}', "id": identity[key]},
            )
    connection.commit()
    return identities


def _expected_added_schema(before, *, postgres=False):
    expected = deepcopy(before)
    for parent in ("expense_items", "documents"):
        expected[parent]["indexes"].append(
            {
                "name": f"uq_{parent}_org_id",
                "unique": True,
                "column_names": ["org_id", "id"],
                "dialect_options": {"postgresql_include": []} if postgres else {},
            }
        )
    for child, column, parent, name in (
        ("expense_transactions", "item_id", "expense_items", "fk_expense_transactions_item"),
        ("issued_invoices", "partner_id", "partners", "fk_issued_invoices_partner"),
        ("recurring_invoices", "partner_id", "partners", "fk_recurring_invoices_partner"),
        (
            "vendor_change_requests",
            "source_document_id",
            "documents",
            "fk_vendor_change_requests_source_document",
        ),
    ):
        entry = {
            "name": name,
            "constrained_columns": ["org_id", column],
            "referred_schema": None,
            "referred_table": parent,
            "referred_columns": ["org_id", "id"],
            "options": {"ondelete": f"SET NULL ({column})"} if postgres else {},
        }
        if postgres:
            entry["comment"] = None
        expected[child]["foreign_keys"].append(entry)
    for name, table in expected.items():
        if name.startswith("_"):
            continue
        for field in ("indexes", "foreign_keys", "unique_constraints", "check_constraints"):
            table[field].sort(key=lambda entry: json.dumps(entry, sort_keys=True))
    return expected


@pytest.fixture(scope="module")
def parent_template(tmp_path_factory):
    path = tmp_path_factory.mktemp("optional-parent163") / "parent.sqlite"
    _migrate(path, "upgrade", PARENT)
    with _connection(path) as connection:
        identities = _seed(connection)
    return path, identities


@pytest.fixture
def historical_database(parent_template, tmp_path):
    source, identities = parent_template
    path = tmp_path / "optional-history.sqlite"
    shutil.copyfile(source, path)
    return path, identities


def test_actual164_cli_roundtrip_preserves_complete_history_and_exact_single_names(
    historical_database,
):
    path, identities = historical_database
    with _connection(path) as connection:
        before, schema = _all_rows(connection), schemas(connection)
        old = {
            table: [
                fk for fk in schema[table]["foreign_keys"] if fk["constrained_columns"] == [column]
            ][0]
            for table, column in (
                ("expense_transactions", "item_id"),
                ("issued_invoices", "partner_id"),
                ("recurring_invoices", "partner_id"),
                ("vendor_change_requests", "source_document_id"),
            )
        }
        assert {table: fk["name"] for table, fk in old.items()} == {
            "expense_transactions": "fk_expense_transactions_item_id_expense_items",
            "issued_invoices": "fk_issued_partner",
            "recurring_invoices": None,
            "vendor_change_requests": None,
        }
    for direction, target in (("upgrade", HEAD), ("downgrade", PARENT), ("upgrade", HEAD)):
        _migrate(path, direction, target)
        with _connection(path) as connection:
            assert _all_rows(connection) == before
            assert schemas(connection) == (
                _expected_added_schema(schema) if target == HEAD else schema
            )
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == target
            assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    with _connection(path) as connection:
        connection.execute(text("PRAGMA foreign_keys=ON"))
        connection.commit()
        for table, column, parent_key, child_key in (
            ("expense_transactions", "item_id", "item", "transaction"),
            ("issued_invoices", "partner_id", "partner", "invoice"),
            ("recurring_invoices", "partner_id", "partner", "schedule"),
            ("vendor_change_requests", "source_document_id", "document", "request"),
        ):
            with (
                pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"),
                connection.begin_nested(),
            ):
                connection.execute(
                    text(f"UPDATE {table} SET {column}=:parent WHERE id=:id"),
                    {"parent": identities[1][parent_key], "id": identities[0][child_key]},
                )


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_fk_on_connection_refuses_before_any_ddl(historical_database, direction):
    path, _ = historical_database
    if direction == "downgrade":
        _migrate(path, "upgrade", HEAD)
    with _connection(path) as connection:
        connection.execute(text("PRAGMA foreign_keys=ON"))
        connection.commit()
        before, physical = _all_rows(connection), _physical(connection)
        with pytest.raises(RuntimeError, match="dedicated SQLite foreign_keys=OFF"):
            _apply(connection, direction)
        assert _all_rows(connection) == before and _physical(connection) == physical
        assert connection.scalar(text("PRAGMA foreign_keys")) == 1


@pytest.mark.parametrize(
    "table,column,key,parent_key",
    [
        ("expense_transactions", "item_id", "transaction", "item"),
        ("issued_invoices", "partner_id", "invoice", "partner"),
        ("recurring_invoices", "partner_id", "schedule", "partner"),
        ("vendor_change_requests", "source_document_id", "request", "document"),
    ],
)
@pytest.mark.parametrize("fault", ["wrong_tenant", "missing_parent"])
def test_masked_preflight_refuses_each_bad_history_without_repair(
    historical_database, table, column, key, parent_key, fault
):
    path, ids = historical_database
    missing = str(uuid4())
    with _connection(path) as connection:
        connection.execute(
            text(f"UPDATE {table} SET {column}=:parent WHERE id=:id"),
            {
                "parent": ids[1][parent_key] if fault == "wrong_tenant" else missing,
                "id": ids[0][key],
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
            value not in str(caught.value) for identity in ids for value in identity.values()
        )
        assert missing not in str(caught.value)
        assert _all_rows(connection) == before and _physical(connection) == physical
        if fault == "missing_parent":
            with pytest.raises(RuntimeError, match="missing_parent=1, wrong_tenant=0"):
                _revision()._preflight(connection)


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
@pytest.mark.parametrize("fault", ["fourth_rebuild", "postvalidation"])
def test_late_failure_rolls_back_all_four_guards_and_both_indexes(
    historical_database, monkeypatch, direction, fault
):
    path, _ = historical_database
    if direction == "downgrade":
        _migrate(path, "upgrade", HEAD)
    migration = _revision()
    # _apply resolves a new revision module, so bind this exact frozen instance.
    monkeypatch.setattr(
        __import__(__name__, fromlist=["_revision"]), "_revision", lambda: migration
    )
    calls = []
    if fault == "fourth_rebuild":
        original = migration._change_pair

        def fail(connection, link, *, create):
            original(connection, link, create=create)
            calls.append(True)
            if len(calls) == 4:
                raise RuntimeError("Synthetic fourth rebuild failure")

        monkeypatch.setattr(migration, "_change_pair", fail)
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
        assert len(calls) == (4 if fault == "fourth_rebuild" else 2)
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


def test_all_bad_optional_tables_are_reported_together_without_identifiers(historical_database):
    path, ids = historical_database
    with _connection(path) as connection:
        for table, column, parent_key, child_key in (
            ("expense_transactions", "item_id", "item", "transaction"),
            ("issued_invoices", "partner_id", "partner", "invoice"),
            ("recurring_invoices", "partner_id", "partner", "schedule"),
            ("vendor_change_requests", "source_document_id", "document", "request"),
        ):
            connection.execute(
                text(f"UPDATE {table} SET {column}=:parent WHERE id=:child"),
                {"parent": ids[1][parent_key], "child": ids[0][child_key]},
            )
        connection.commit()
        before, physical = _all_rows(connection), _physical(connection)
        with pytest.raises(RuntimeError, match="inconsistent history") as caught:
            _apply(connection, "upgrade")
        assert str(caught.value).count("missing_parent=0, wrong_tenant=1") == 4
        assert all(
            value not in str(caught.value) for identity in ids for value in identity.values()
        )
        assert _all_rows(connection) == before and _physical(connection) == physical


def test_unrelated_incoming_fk_violation_is_refused_without_repair(historical_database):
    path, _ = historical_database
    missing = str(uuid4())
    with _connection(path) as connection:
        connection.execute(
            text("UPDATE issued_invoice_attachments SET invoice_id=:id"), {"id": missing}
        )
        connection.commit()
        before, physical = _all_rows(connection), _physical(connection)
        with pytest.raises(
            RuntimeError, match="existing foreign-key violations: issued_invoice_attachments=2"
        ) as caught:
            _apply(connection, "upgrade")
        assert missing not in str(caught.value)
        assert _all_rows(connection) == before and _physical(connection) == physical


@pytest.mark.parametrize("parent", ["expense_items", "documents"])
def test_existing_pair_index_is_not_implicitly_adopted(historical_database, parent):
    path, _ = historical_database
    with _connection(path) as connection:
        connection.execute(text(f"CREATE UNIQUE INDEX uq_{parent}_org_id ON {parent}(org_id,id)"))
        connection.commit()
        before, physical = _all_rows(connection), _physical(connection)
        with pytest.raises(
            RuntimeError, match=f"Unexpected existing {parent} pair index; no adoption"
        ):
            _apply(connection, "upgrade")
        assert _all_rows(connection) == before and _physical(connection) == physical
