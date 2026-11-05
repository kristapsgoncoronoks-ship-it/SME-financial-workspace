"""Populated SQLite upgrades/downgrades preserve rows, optional links and names."""

from __future__ import annotations

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
from sqlalchemy.exc import IntegrityError

from tests.test_expense_item_parent_migration import _seed as _expense_seed
from tests.test_migrations import BACKEND_DIR, _run_alembic
from tests.test_planning_ocr_migration_integration import _insert

PARENT = "e6f8a0b2c4d7"
PARTNER = "f7a9c1e3b5d8"
WEBHOOK = "a8b0d2f4c6e9"
TABLES = (
    "organizations",
    "users",
    "expense_reports",
    "expense_items",
    "expense_transactions",
    "expense_comments",
    "expense_approval_steps",
    "partners",
    "partner_documents",
    "webhook_endpoints",
    "webhook_deliveries",
    "issued_invoices",
    "recurring_invoices",
    "documents",
    "vendors",
    "vendor_change_requests",
    "audit_events",
    "jobs",
)


def _revision(revision):
    config = Config()
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return ScriptDirectory.from_config(config).get_revision(revision).module


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


def _apply(connection, migration, direction):
    with Operations.context(MigrationContext.configure(connection)):
        getattr(migration, direction)()


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


def _unrelated_foreign_keys(connection):
    inspector = inspect(connection)
    return {
        table: [
            fk
            for fk in inspector.get_foreign_keys(table)
            if not (
                table in ("partner_documents", "webhook_deliveries")
                and fk["referred_table"] in ("partners", "webhook_endpoints")
            )
        ]
        for table in TABLES
    }


def _unrelated_indexes(connection):
    inspector = inspect(connection)
    return {
        table: [
            (
                index["name"],
                index["unique"],
                tuple(index["column_names"]),
                tuple(
                    sorted(
                        (key, str(value)) for key, value in index.get("dialect_options", {}).items()
                    )
                ),
            )
            for index in inspector.get_indexes(table)
            if index["name"] not in ("uq_partners_org_id", "uq_webhook_endpoints_org_id")
        ]
        for table in TABLES
    }


def _assert_link(connection, migration, composite):
    foreign_keys = [
        fk
        for fk in inspect(connection).get_foreign_keys(migration.CHILD_TABLE)
        if fk["referred_table"] == migration.PARENT_TABLE
    ]
    assert len(foreign_keys) == 1
    assert foreign_keys[0]["constrained_columns"] == (
        ["org_id", migration.LINK_COLUMN] if composite else [migration.LINK_COLUMN]
    )
    assert foreign_keys[0]["referred_columns"] == (["org_id", "id"] if composite else ["id"])
    assert foreign_keys[0]["options"]["ondelete"] == "CASCADE"
    indexes = [
        i
        for i in inspect(connection).get_indexes(migration.PARENT_TABLE)
        if i["name"] == migration.TARGET_INDEX
    ]
    assert bool(indexes) is composite
    if composite:
        assert indexes[0]["unique"] and indexes[0]["column_names"] == ["org_id", "id"]
    assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []


def _seed(connection):
    identities = _expense_seed(connection)
    for index, identity in enumerate(identities):
        org = identity["org"]
        partner, doc, endpoint, delivery, invoice, schedule, document, vendor, request = (
            str(uuid4()) for _ in range(9)
        )
        _insert(
            connection,
            "partners",
            id=partner,
            org_id=org,
            name="Retained partner",
            requires_contract=True,
        )
        _insert(
            connection,
            "partner_documents",
            id=doc,
            org_id=org,
            partner_id=partner,
            kind="contract",
            title="Retained signed contract",
            status="signed",
            signed_by="Synthetic signer",
            signed_date=date(2026, 10, 1),
        )
        _insert(
            connection,
            "webhook_endpoints",
            id=endpoint,
            org_id=org,
            url="https://example.invalid/never-called",
            secret="retained-synthetic-key",
        )
        _insert(
            connection,
            "webhook_deliveries",
            id=delivery,
            org_id=org,
            endpoint_id=endpoint,
            event_type="ping",
            payload_json='{"synthetic":true}',
            idempotency_key=f"retained-intent-{index}",
            status="delivered",
            attempts=2,
            response_code=200,
        )
        _insert(
            connection,
            "issued_invoices",
            id=invoice,
            org_id=org,
            partner_id=partner,
            buyer_name="Retained buyer snapshot",
            issue_date=date(2026, 10, 1),
            seller_json='{"legal_name":"Retained seller snapshot"}',
            total="42.00",
            subtotal="40.00",
            tax_total="2.00",
        )
        _insert(
            connection,
            "recurring_invoices",
            id=schedule,
            org_id=org,
            partner_id=partner,
            title="Retained schedule",
            template_json='{"synthetic":true}',
            start_date=date(2026, 10, 1),
            next_run_date=date(2026, 12, 1),
        )
        _insert(
            connection,
            "documents",
            id=document,
            org_id=org,
            sha256=str(index) * 64,
            size=32,
            kind="receipts",
        )
        _insert(connection, "vendors", id=vendor, org_id=org, name="Retained vendor")
        _insert(
            connection,
            "vendor_change_requests",
            id=request,
            org_id=org,
            vendor_id=vendor,
            source_document_id=document,
            requested_by=identity["user"],
            field="tax_id",
            old_value="Synthetic old",
            new_value="Synthetic new",
        )
        identity.update(
            partner=partner,
            partner_document=doc,
            endpoint=endpoint,
            delivery=delivery,
            invoice=invoice,
            schedule=schedule,
            document=document,
            vendor=vendor,
            request=request,
        )
    connection.commit()
    return identities


@pytest.fixture(scope="module")
def parent_template(tmp_path_factory):
    path = tmp_path_factory.mktemp("partner-webhook-history") / "parent.sqlite"
    _migrate(path, "upgrade", PARENT)
    with _connection(path) as connection:
        identities = _seed(connection)
    return path, identities


@pytest.fixture
def historical_database(parent_template, tmp_path):
    source, identities = parent_template
    path = tmp_path / "history.sqlite"
    shutil.copyfile(source, path)
    return path, identities


@pytest.fixture(params=[PARTNER, WEBHOOK])
def selected_migration(request, historical_database):
    path, identities = historical_database
    if request.param == WEBHOOK:
        with _connection(path) as connection:
            _apply(connection, _revision(PARTNER), "upgrade")
            connection.commit()
    return path, identities, _revision(request.param)


@pytest.mark.slow
def test_actual_ordered_cli_roundtrip_keeps_all_populated_evidence(historical_database):
    path, identities = historical_database
    with _connection(path) as connection:
        before, unrelated = _rows(connection), _unrelated_foreign_keys(connection)
        indexes = _unrelated_indexes(connection)
    for direction, target, partner_pair, webhook_pair in (
        ("upgrade", PARTNER, True, False),
        ("upgrade", WEBHOOK, True, True),
        ("downgrade", PARENT, False, False),
        ("upgrade", WEBHOOK, True, True),
    ):
        _migrate(path, direction, target)
        with _connection(path) as connection:
            assert _rows(connection) == before
            assert _unrelated_foreign_keys(connection) == unrelated
            assert _unrelated_indexes(connection) == indexes
            _assert_link(connection, _revision(PARTNER), partner_pair)
            _assert_link(connection, _revision(WEBHOOK), webhook_pair)
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 0
    with _connection(path) as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.commit()
        for migration, first, second in (
            (_revision(PARTNER), identities[0]["partner_document"], identities[1]["partner"]),
            (_revision(WEBHOOK), identities[0]["delivery"], identities[1]["endpoint"]),
        ):
            with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
                with connection.begin_nested():
                    connection.execute(
                        text(
                            f"UPDATE {migration.CHILD_TABLE} SET {migration.LINK_COLUMN}=:parent WHERE id=:child"
                        ),
                        {"parent": second, "child": first},
                    )


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_fk_on_connection_refuses_before_any_mutation(selected_migration, direction):
    path, _, migration = selected_migration
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


@pytest.mark.parametrize("fault", ["wrong_tenant", "missing_parent", "unrelated_optional_orphan"])
def test_global_history_refuses_without_repair_or_identifier_leak(selected_migration, fault):
    path, identities, migration = selected_migration
    with _connection(path) as connection:
        missing = str(uuid4())
        if fault == "unrelated_optional_orphan":
            connection.execute(
                text("UPDATE vendor_change_requests SET source_document_id=:id"), {"id": missing}
            )
        else:
            key = "partner" if migration.revision == PARTNER else "endpoint"
            child = "partner_document" if key == "partner" else "delivery"
            connection.execute(
                text(
                    f"UPDATE {migration.CHILD_TABLE} SET {migration.LINK_COLUMN}=:parent WHERE id=:child"
                ),
                {
                    "parent": identities[1][key] if fault == "wrong_tenant" else missing,
                    "child": identities[0][child],
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
def test_postvalidation_failure_rolls_back_entire_ddl_before_outer_commit(
    selected_migration, monkeypatch, direction
):
    path, _, migration = selected_migration
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
                raise RuntimeError("Synthetic late validation failure")

        monkeypatch.setattr(migration, "_check_sqlite_foreign_keys", fail_after_copy)
        with pytest.raises(RuntimeError, match="Synthetic late validation failure"):
            _apply(connection, migration, direction)
        assert len(calls) == 2
        assert _rows(connection) == before and _schema(connection) == schema
        connection.commit()
    with _connection(path) as connection:
        assert _rows(connection) == before and _schema(connection) == schema


@pytest.mark.parametrize("revision", [PARTNER, WEBHOOK])
@pytest.mark.parametrize(
    "policy", ["ON DELETE RESTRICT", "ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED"]
)
def test_unexpected_real_parent_fk_policy_is_refused(tmp_path, revision, policy):
    migration = _revision(revision)
    with _connection(tmp_path / "unexpected.sqlite") as connection:
        connection.exec_driver_sql(
            f"CREATE TABLE {migration.PARENT_TABLE} (id TEXT PRIMARY KEY, org_id TEXT NOT NULL)"
        )
        connection.exec_driver_sql(
            f"CREATE TABLE {migration.CHILD_TABLE} (id TEXT PRIMARY KEY, org_id TEXT NOT NULL, {migration.LINK_COLUMN} TEXT NOT NULL, CONSTRAINT original FOREIGN KEY ({migration.LINK_COLUMN}) REFERENCES {migration.PARENT_TABLE}(id) {policy})"
        )
        connection.commit()
        schema = _schema(connection)
        with pytest.raises(RuntimeError, match="Unexpected .* foreign-key contract"):
            _apply(connection, migration, "upgrade")
        assert _schema(connection) == schema
