"""Facts history is additive, tenant-owned and never erased by code rollback."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.core.tenant import TENANT_MODELS
from app.models.capture_reading import CaptureReadingObservation
from tests.test_migrations import BACKEND_DIR, _run_alembic
from tests.test_planning_ocr_migration_integration import _insert, database, migrate, schemas
from tests.test_planning_ocr_migration_integration import migration_url as migration_url

PARENT = "a8b0d2f4c6e9"
HEAD = "b9c1e3f5a7d0"
HISTORY = "capture_reading_observations"
OWNERS = ("extraction_runs", "inbound_invoices")
SUMMARY = "reading_observation_json"
STAMP = datetime(2026, 11, 5, 12, tzinfo=UTC)


def _revision():
    config = Config()
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return ScriptDirectory.from_config(config).get_revision(HEAD).module


def _apply(connection, direction):
    with Operations.context(MigrationContext.configure(connection)):
        getattr(_revision(), direction)()


def _seed(connection):
    identities = []
    for _ in range(2):
        org, run, inbound = (str(uuid4()) for _ in range(3))
        _insert(connection, "organizations", id=org, name="Synthetic reading migration")
        _insert(
            connection,
            "extraction_runs",
            id=run,
            org_id=org,
            method="ocr",
            note="Retain existing extraction evidence",
        )
        _insert(
            connection,
            "inbound_invoices",
            id=inbound,
            org_id=org,
            received_at=STAMP,
            filename="synthetic.pdf",
            error="Retain existing inbound evidence",
        )
        identities.append(dict(org=org, run=run, inbound=inbound))
    return identities


def _history(connection, identity, *, inbound=False, **overrides):
    values = dict(
        id=str(uuid4()),
        org_id=identity["org"],
        created_at=STAMP,
        extraction_run_id=None if inbound else identity["run"],
        inbound_invoice_id=identity["inbound"] if inbound else None,
        source_sha256=None,
        attempt_seq=None if inbound else 0,
        outcome="failure",
        failure_code=None,
        profile_json=None,
        page_facts_json=None,
    )
    values.update(overrides)
    return _insert(connection, HISTORY, **values)


def _rows(connection):
    return {
        table: [
            dict(row) for row in connection.execute(text(f'SELECT * FROM "{table}"')).mappings()
        ]
        for table in sorted(inspect(connection).get_table_names())
        if table != "alembic_version"
    }


def _sqlite_schema(connection):
    if connection.dialect.name != "sqlite":
        return None
    return connection.exec_driver_sql(
        "SELECT name,sql,rootpage FROM sqlite_schema "
        "WHERE name NOT LIKE 'sqlite_%' AND name <> 'alembic_version' ORDER BY name"
    ).all()


def _unscoped(connection):
    if connection.dialect.name == "postgresql":
        connection.execute(text("SET LOCAL app.current_org = ''"))


def test_history_model_is_registered_facts_only_and_has_no_update_timestamp():
    assert CaptureReadingObservation in TENANT_MODELS
    table = CaptureReadingObservation.__table__
    assert set(table.c) == {
        table.c[name]
        for name in (
            "id",
            "org_id",
            "created_at",
            "extraction_run_id",
            "inbound_invoice_id",
            "source_sha256",
            "attempt_seq",
            "outcome",
            "failure_code",
            "profile_json",
            "page_facts_json",
        )
    }
    assert table.c.created_at.onupdate is None
    assert all(column.onupdate is None for column in table.c)


@pytest.mark.slow
def test_additive_upgrade_and_empty_history_exact_roundtrip(migration_url):
    migrate(migration_url, "upgrade", PARENT)
    database(migration_url, _seed)
    before = database(migration_url, schemas)
    rows = database(migration_url, _rows)
    physical = database(migration_url, _sqlite_schema)
    migrate(migration_url, "upgrade", HEAD)
    after = database(migration_url, schemas)
    expected = deepcopy(after)
    del expected[HISTORY]
    for owner in OWNERS:
        assert [c for c in expected[owner]["columns"] if c[0] == SUMMARY] == [
            [SUMMARY, "TEXT", True, None]
        ]
        expected[owner]["columns"] = [c for c in expected[owner]["columns"] if c[0] != SUMMARY]
    index = next(
        entry
        for entry in expected["inbound_invoices"]["indexes"]
        if entry["name"] == "uq_inbound_invoices_org_id"
    )
    assert index["unique"] and index["column_names"] == ["org_id", "id"]
    assert not any("where" in key for key in index.get("dialect_options", {}))
    expected["inbound_invoices"]["indexes"].remove(index)
    for key in ("_rls", "_policies"):
        if key in expected:
            expected[key] = [row for row in expected[key] if row[0] != HISTORY]
    assert expected == before
    added_rows = database(migration_url, _rows)
    assert added_rows.pop(HISTORY) == []
    for owner in OWNERS:
        for row in added_rows[owner]:
            assert row.pop(SUMMARY) is None
    assert added_rows == rows
    migrate(migration_url, "downgrade", PARENT)
    assert database(migration_url, schemas) == before
    assert database(migration_url, _rows) == rows
    assert database(migration_url, _sqlite_schema) == physical
    migrate(migration_url, "upgrade", HEAD)
    assert database(migration_url, schemas) == after

    def scoped_success(connection):
        if connection.dialect.name != "postgresql":
            return
        org = str(rows["organizations"][0]["id"])
        connection.execute(text("SELECT set_config('app.current_org', :org, true)"), {"org": org})
        visible = _rows(connection)
        _apply(connection, "downgrade")
        assert connection.scalar(text("SELECT current_setting('app.current_org', true)")) == org
        assert schemas(connection) == before
        _apply(connection, "upgrade")
        assert connection.scalar(text("SELECT current_setting('app.current_org', true)")) == org
        assert schemas(connection) == after
        assert _rows(connection) == visible

    database(migration_url, scoped_success)


@pytest.mark.slow
@pytest.mark.parametrize("populated", [HISTORY, *OWNERS])
def test_downgrade_refuses_any_history_or_summary_including_hidden_tenants(
    migration_url, populated
):
    migrate(migration_url, "upgrade", HEAD)
    identities = database(migration_url, _seed)

    def seed_provenance(connection):
        if populated == HISTORY:
            _history(connection, identities[0])
        else:
            connection.execute(
                text(f'UPDATE "{populated}" SET {SUMMARY}=:summary WHERE org_id=:org'),
                {"summary": "{}", "org": identities[0]["org"]},
            )

    database(migration_url, seed_provenance)
    before = database(migration_url, schemas)
    retained = database(migration_url, _rows)

    def inherited_scope_refusal(connection):
        if connection.dialect.name == "postgresql":
            assert connection.execute(
                text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user")
            ).one() == (False, False)
            connection.execute(
                text("SELECT set_config('app.current_org', :org, true)"),
                {"org": identities[1]["org"]},
            )
            condition = "" if populated == HISTORY else f" WHERE {SUMMARY} IS NOT NULL"
            assert connection.scalar(text(f'SELECT count(*) FROM "{populated}"{condition}')) == 0
            visible = _rows(connection)
        with pytest.raises(
            RuntimeError, match="Published reading observations or current summaries"
        ):
            _apply(connection, "downgrade")
        if connection.dialect.name == "postgresql":
            assert (
                connection.scalar(text("SELECT current_setting('app.current_org', true)"))
                == identities[1]["org"]
            )
            assert _rows(connection) == visible
            assert schemas(connection) == before
        _unscoped(connection)
        assert schemas(connection) == before
        assert _rows(connection) == retained

    database(migration_url, inherited_scope_refusal)
    refused = _run_alembic(["downgrade", PARENT], migration_url)
    assert refused.returncode != 0
    assert "Published reading observations or current summaries exist" in refused.stderr
    assert database(migration_url, schemas) == before
    assert database(migration_url, _rows) == retained
    assert (
        database(migration_url, lambda c: c.scalar(text("SELECT version_num FROM alembic_version")))
        == HEAD
    )


@pytest.mark.slow
def test_sql_owner_generation_constraints_tenant_isolation_and_lifecycle(migration_url):
    migrate(migration_url, "upgrade", HEAD)
    identities = database(migration_url, _seed)

    def verify(connection):
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            assert connection.scalar(text("PRAGMA foreign_keys")) == 1
        first, other = identities
        invalid = (
            dict(extraction_run_id=None, attempt_seq=None),
            dict(inbound_invoice_id=first["inbound"]),
            dict(attempt_seq=None),
            dict(attempt_seq=-1),
            dict(extraction_run_id=None, inbound_invoice_id=first["inbound"], attempt_seq=0),
            dict(outcome="queued"),
            dict(extraction_run_id=other["run"]),
            dict(extraction_run_id=None, inbound_invoice_id=other["inbound"], attempt_seq=None),
            dict(extraction_run_id=str(uuid4())),
        )
        for values in invalid:
            with pytest.raises(IntegrityError), connection.begin_nested():
                _history(connection, first, **values)
        for identity in identities:
            _history(connection, identity)
            _history(connection, identity, inbound=True)
        inspector = inspect(connection)
        columns = {column["name"] for column in inspector.get_columns(HISTORY)}
        assert columns == set(CaptureReadingObservation.__table__.c.keys())
        assert len(inspector.get_check_constraints(HISTORY)) == 4
        foreign_keys = inspector.get_foreign_keys(HISTORY)
        assert len(foreign_keys) == 3
        for owner, column in zip(OWNERS, ("extraction_run_id", "inbound_invoice_id"), strict=True):
            fk = next(fk for fk in foreign_keys if fk["referred_table"] == owner)
            assert fk["constrained_columns"] == ["org_id", column]
            assert fk["referred_columns"] == ["org_id", "id"]
            assert fk["options"]["ondelete"] == "CASCADE"
        indexes = inspector.get_indexes(HISTORY)
        assert {tuple(index["column_names"]) for index in indexes if not index["unique"]} == {
            ("org_id", "extraction_run_id", "created_at", "id"),
            ("org_id", "inbound_invoice_id", "created_at", "id"),
        }
        if connection.dialect.name == "postgresql":
            assert connection.execute(
                text(
                    "SELECT relrowsecurity,relforcerowsecurity FROM pg_class "
                    "WHERE oid=CAST(:table AS regclass)"
                ),
                {"table": HISTORY},
            ).one() == (True, True)
            connection.execute(
                text("SELECT set_config('app.current_org', :org, true)"), {"org": first["org"]}
            )
            assert connection.scalar(text(f"SELECT count(*) FROM {HISTORY}")) == 2
            with pytest.raises(DBAPIError), connection.begin_nested():
                _history(connection, other)
            _unscoped(connection)
        for owner, key in zip(OWNERS, ("run", "inbound"), strict=True):
            connection.execute(text(f'DELETE FROM "{owner}" WHERE id=:id'), {"id": first[key]})
            remaining = connection.scalar(
                text(f"SELECT count(*) FROM {HISTORY} WHERE org_id=:org"), {"org": first["org"]}
            )
            assert remaining == (1 if owner == "extraction_runs" else 0)
        connection.execute(text("DELETE FROM organizations WHERE id=:id"), {"id": other["org"]})
        assert connection.scalar(text(f"SELECT count(*) FROM {HISTORY}")) == 0
        if connection.dialect.name == "sqlite":
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []

    database(migration_url, verify)


def _minimal_sqlite_parent(connection, fault=None):
    connection.exec_driver_sql("CREATE TABLE organizations (id CHAR(36) NOT NULL PRIMARY KEY)")
    for owner in OWNERS:
        org_type = (
            "VARCHAR(36)" if fault == "inbound_identity_type" and owner == OWNERS[1] else "CHAR(36)"
        )
        org_null = "" if fault == "nullable_run_org" and owner == OWNERS[0] else "NOT NULL"
        pair = (
            ", UNIQUE (org_id,id)" if owner == OWNERS[0] and fault != "missing_run_target" else ""
        )
        summary = f", {SUMMARY} TEXT" if fault == "existing_summary" and owner == OWNERS[1] else ""
        connection.exec_driver_sql(
            f'CREATE TABLE "{owner}" (id CHAR(36) NOT NULL PRIMARY KEY, '
            f"org_id {org_type} {org_null}{summary}, "
            f"FOREIGN KEY (org_id) REFERENCES organizations(id) ON DELETE CASCADE{pair})"
        )
    if fault in ("nonunique_target", "partial_target", "wrong_target_columns"):
        unique = "" if fault == "nonunique_target" else "UNIQUE "
        where = " WHERE org_id IS NOT NULL" if fault == "partial_target" else ""
        columns = "id,org_id" if fault == "wrong_target_columns" else "org_id,id"
        connection.exec_driver_sql(
            f"CREATE {unique}INDEX uq_inbound_invoices_org_id ON inbound_invoices ({columns}){where}"
        )
    if fault == "existing_history":
        connection.exec_driver_sql(f"CREATE TABLE {HISTORY} (unrelated TEXT)")
    connection.commit()


@pytest.mark.parametrize(
    "fault",
    [
        "inbound_identity_type",
        "nullable_run_org",
        "missing_run_target",
        "nonunique_target",
        "partial_target",
        "wrong_target_columns",
        "existing_summary",
        "existing_history",
    ],
)
def test_incompatible_preexisting_schema_refuses_before_any_ddl(tmp_path, monkeypatch, fault):
    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite:///{tmp_path / 'incompatible.sqlite'}")
    try:
        with engine.connect() as connection:
            _minimal_sqlite_parent(connection, fault)
            before, retained = _sqlite_schema(connection), _rows(connection)

            def unexpected_ddl(*args, **kwargs):
                pytest.fail("Incompatible schema must be detected before any mutation")

            for method in ("add_column", "create_index", "create_table"):
                monkeypatch.setattr(Operations, method, unexpected_ddl)
            with pytest.raises(RuntimeError, match="Incompatible|Preexisting"):
                _apply(connection, "upgrade")
            connection.commit()
            assert _sqlite_schema(connection) == before
            assert _rows(connection) == retained
    finally:
        engine.dispose()


def test_sqlite_ddl_failure_rolls_back_native_owner_changes(tmp_path, monkeypatch):
    """A failure after adding owner columns cannot commit half an upgrade."""
    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite:///{tmp_path / 'atomic.sqlite'}")
    try:
        with engine.connect() as connection:
            _minimal_sqlite_parent(connection)
            before = _sqlite_schema(connection)
            original = Operations.create_table

            def fail_after_owner_changes(self, name, *args, **kwargs):
                if name == HISTORY:
                    raise RuntimeError("Synthetic new-table creation failure")
                return original(self, name, *args, **kwargs)

            monkeypatch.setattr(Operations, "create_table", fail_after_owner_changes)
            with pytest.raises(RuntimeError, match="Synthetic new-table creation failure"):
                _apply(connection, "upgrade")
            connection.commit()
            assert _sqlite_schema(connection) == before
    finally:
        engine.dispose()


@pytest.mark.slow
def test_late_downgrade_failure_restores_all_ddl_and_prior_scope(migration_url, monkeypatch):
    migrate(migration_url, "upgrade", HEAD)
    identities = database(migration_url, _seed)
    before = database(migration_url, schemas)
    retained = database(migration_url, _rows)
    physical = database(migration_url, _sqlite_schema)
    original = Operations.drop_column

    def fail_after_first_owner_drop(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError("Synthetic late destructive-downgrade failure")

    monkeypatch.setattr(Operations, "drop_column", fail_after_first_owner_drop)

    def interrupted(connection):
        if connection.dialect.name == "postgresql":
            connection.execute(
                text("SELECT set_config('app.current_org', :org, true)"),
                {"org": identities[1]["org"]},
            )
        visible = _rows(connection)
        with pytest.raises(RuntimeError, match="Synthetic late destructive-downgrade failure"):
            _apply(connection, "downgrade")
        if connection.dialect.name == "postgresql":
            assert (
                connection.scalar(text("SELECT current_setting('app.current_org', true)"))
                == identities[1]["org"]
            )
        assert _rows(connection) == visible
        assert schemas(connection) == before
        _unscoped(connection)
        assert _rows(connection) == retained
        assert _sqlite_schema(connection) == physical

    database(migration_url, interrupted)
