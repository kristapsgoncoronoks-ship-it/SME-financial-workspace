"""Combined graph upgrades, record preservation and rollback boundaries.

PostgreSQL uses disposable databases created only when an explicit CI admin URL
is supplied. Application migrations and assertions use the non-superuser role.
No test points a destructive reset at RLS_TEST_DATABASE_URL.
"""

import ast
import asyncio
import hashlib
import importlib.util
import json
import os
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import MetaData, Table, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from tests.test_migrations import BACKEND_DIR, _run_alembic

MERGE = "f0b2d4e6a8c1"
PLANNING_COMMANDS = "8d1f3a5c7e90"
OLD_MERGE = "e5f7091a2345"
PLANNING = "c3d5e7f90112"
OCR = "d4e6f8a01223"
PAYMENT = "a9c3e7f1b5d0"
PARENTS = {OLD_MERGE, PAYMENT}
MAIN = "c129fa806b32"
STARTS = [
    (),
    (MAIN,),
    (PLANNING,),
    (OCR,),
    (PAYMENT,),
    (OLD_MERGE,),
    (PLANNING, PAYMENT),
    (OLD_MERGE, PAYMENT),
]
PRESERVED_TABLES = (
    "organizations",
    "users",
    "sessions",
    "projects",
    "extraction_runs",
    "project_assignments",
    "reimbursement_batches",
    "expense_reports",
    "expense_items",
    "expense_commands",
    "audit_events",
    "webhook_endpoints",
    "webhook_deliveries",
    "jobs",
    "calendar_connections",
    "calendar_oauth_attempts",
    "calendar_event_mappings",
    "capture_region_reads",
)
STAMP = datetime(2026, 10, 28, 8, tzinfo=UTC)


class MigrationURL(str):
    """Connect with the real URL while pytest renders a redacted fixture value."""

    def __repr__(self):
        return repr(make_url(self).render_as_string(hide_password=True))


@pytest.fixture(params=["sqlite", "postgres"])
def migration_url(request, tmp_path):
    if request.param == "sqlite":
        yield MigrationURL(f"sqlite+aiosqlite:///{tmp_path / 'integration.db'}")
        return
    admin_url = os.environ.get("MIGRATION_TEST_ADMIN_URL")
    app_url = os.environ.get("RLS_TEST_DATABASE_URL")
    if not admin_url or not app_url:
        pytest.skip("requires explicit disposable-database CI admin and app URLs")
    import asyncpg

    name = "migration_integration_" + uuid.uuid4().hex
    app = make_url(app_url)
    # Restrict SQL identifiers rather than interpolating arbitrary configuration.
    assert app.username and app.username.replace("_", "").isalnum()

    async def manage(create):
        admin = await asyncpg.connect(admin_url)
        try:
            if create:
                await admin.execute(f'CREATE DATABASE "{name}" OWNER "{app.username}"')
            else:
                await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        finally:
            await admin.close()

    asyncio.run(manage(True))
    try:
        yield MigrationURL(app.set(database=name).render_as_string(hide_password=False))
    finally:
        asyncio.run(manage(False))


def migrate(url, *args):
    result = _run_alembic(list(args), url)
    assert result.returncode == 0, result.stdout + result.stderr


def database(url, action):
    async def run():
        engine = create_async_engine(url)
        try:
            async with engine.begin() as connection:
                return await connection.run_sync(action)
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _insert(connection, table_name, **values):
    """Use the historical reflected shape and current defaults for shared fields.

    These are synthetic migration-preservation fixtures, not application commands
    or external provider outcomes. Reflected tables avoid accidentally inserting
    columns that have not yet been introduced at the selected starting revision.
    """
    from app.models import Base

    table = Table(table_name, MetaData(), autoload_with=connection)
    supplied = {key: value for key, value in values.items() if key in table.c}
    for column in table.c:
        if column.name in supplied or column.nullable or column.server_default is not None:
            continue
        original = Base.metadata.tables[table_name].c[column.name]
        default = original.default
        assert default is not None, f"Fixture needs explicit {table_name}.{column.name}"
        supplied[column.name] = default.arg(None) if default.is_callable else default.arg
    connection.execute(table.insert(), supplied)
    return supplied["id"]


def seed(connection):
    from app.services.audit import _hash

    identities = []
    for tenant in range(2):
        org, user, project, run, assignment, batch, report, endpoint, delivery, session = (
            str(uuid.uuid4()) for _ in range(10)
        )
        email = f"migration-{user}@example.invalid"
        _insert(connection, "organizations", id=org, name=f"Synthetic migration tenant {tenant}")
        _insert(
            connection,
            "users",
            id=user,
            org_id=org,
            email=email,
            name="Synthetic migration checker",
            hashed_password="not-a-login-hash",
        )
        _insert(
            connection,
            "sessions",
            id=session,
            org_id=org,
            user_id=user,
            expires_at=STAMP + timedelta(days=1),
        )
        _insert(
            connection,
            "projects",
            id=project,
            org_id=org,
            code=f"SYNTHETIC-{tenant}",
            name="Synthetic migration project",
        )
        _insert(
            connection,
            "extraction_runs",
            id=run,
            org_id=org,
            method="text-layer",
            note="retain original evidence",
        )
        _insert(
            connection,
            "project_assignments",
            id=assignment,
            org_id=org,
            project_id=project,
            assignee_user_id=user,
            assignee_email=email,
            starts_at=STAMP,
            ends_at=STAMP + timedelta(hours=2),
            created_by=email,
        )
        _insert(
            connection,
            "reimbursement_batches",
            id=batch,
            org_id=org,
            status="paid",
            total_eur=Decimal("35.07"),
            paid_at=STAMP,
            created_by_id=user,
            created_by=email,
            version=2,
            export_count=1,
            reference="Synthetic original payment evidence",
            last_msg_id="SYNTHETIC-MIGRATION",
        )
        _insert(
            connection,
            "expense_reports",
            id=report,
            org_id=org,
            employee_id=user,
            employee_name="Synthetic migration employee",
            title="Synthetic retained claim",
            status="reimbursed",
            total=Decimal("35.07"),
            total_eur=Decimal("35.07"),
            vat_total=Decimal("0.00"),
            payout_batch_id=batch,
            reimbursed_at=STAMP,
            payment_method="bank_transfer",
            payment_reference="Synthetic original payment evidence",
            version=3,
        )
        _insert(
            connection,
            "expense_items",
            org_id=org,
            report_id=report,
            spend_date=date(2026, 10, 28),
            description="Synthetic retained expense",
            amount=Decimal("35.07"),
            original_amount=Decimal("35.07"),
            currency="EUR",
            fx_rate=Decimal("1"),
            fx_source="eur",
            project_id=project,
        )
        _insert(
            connection,
            "expense_commands",
            org_id=org,
            employee_id=user,
            command_key=f"migration-payment-{batch}",
            operation="reimbursement_paid",
            payload_hash="a" * 64,
            target_id=batch,
            result_json=json.dumps({"synthetic": True, "batch_id": batch, "total_eur": "35.07"}),
            created_at=STAMP,
        )
        action, meta, at_ms = (
            "expense.reimbursement_paid",
            '{"fixture":"migration only"}',
            1793174400000,
        )
        _insert(
            connection,
            "audit_events",
            org_id=org,
            seq=1,
            actor_id=user,
            actor_email=email,
            action=action,
            target_type="reimbursement_batch",
            target_id=batch,
            at_ms=at_ms,
            meta=meta,
            hash=_hash(None, 1, org, user, action, "reimbursement_batch", batch, at_ms, meta),
        )
        _insert(
            connection,
            "webhook_endpoints",
            id=endpoint,
            org_id=org,
            url="https://synthetic.invalid/migration-only",
            secret="synthetic-migration-only",
            events="expense.reimbursed",
        )
        _insert(
            connection,
            "webhook_deliveries",
            id=delivery,
            org_id=org,
            endpoint_id=endpoint,
            event_type="expense.reimbursed",
            payload_json='{"synthetic":true}',
            idempotency_key=f"reimbursement-paid:{batch}",
            status="pending",
            attempts=0,
        )
        _insert(
            connection,
            "jobs",
            org_id=org,
            kind="webhook.deliver",
            payload_json=json.dumps({"delivery_id": delivery}),
            idempotency_key=f"migration-delivery-{delivery}",
            status="queued",
            attempts=0,
        )
        identities.append(
            {
                "org": org,
                "user": user,
                "project": project,
                "run": run,
                "assignment": assignment,
                "batch": batch,
                "session": session,
            }
        )
    seed_feature_evidence(connection, identities)
    return identities


def seed_feature_evidence(connection, identities):
    available = set(inspect(connection).get_table_names())
    for ids in identities:
        org, user = ids["org"], ids["user"]
        if "calendar_connections" in available and not connection.scalar(
            text("SELECT count(*) FROM calendar_connections WHERE org_id=:org"), {"org": org}
        ):
            connection_id = _insert(
                connection,
                "calendar_connections",
                org_id=org,
                user_id=user,
                provider="google",
                status="active",
                calendar_id="synthetic-calendar-only",
                sealed_tokens="synthetic-sealed-marker-not-a-credential",
                generation=2,
            )
            _insert(
                connection,
                "calendar_oauth_attempts",
                org_id=org,
                user_id=user,
                session_id=ids["session"],
                provider="google",
                connection_id=connection_id,
                connection_generation=2,
                state_hash=uuid.uuid4().hex * 2,
                browser_hash="b" * 64,
                sealed_verifier="synthetic-sealed-verifier",
                expires_at=STAMP + timedelta(minutes=5),
            )
            _insert(
                connection,
                "calendar_event_mappings",
                org_id=org,
                connection_id=connection_id,
                assignment_id=ids["assignment"],
                stable_id=str(uuid.uuid4()),
                event_id="synthetic-provider-id",
                desired_revision=3,
                synced_revision=1,
                status="queued",
            )
        if "capture_region_reads" in available and not connection.scalar(
            text("SELECT count(*) FROM capture_region_reads WHERE org_id=:org"), {"org": org}
        ):
            _insert(
                connection,
                "capture_region_reads",
                org_id=org,
                extraction_run_id=ids["run"],
                requested_by=user,
                command_key=str(uuid.uuid4()),
                payload_hash="c" * 64,
                source_sha256="d" * 64,
                attempt_seq=1,
                request_json='{"synthetic":true}',
                before_draft_json='{"supplier_name":"retained synthetic supplier"}',
            )


def snapshot(connection):
    available = set(inspect(connection).get_table_names())
    return {
        name: [
            dict(row)
            for row in connection.execute(text(f"SELECT * FROM {name} ORDER BY id")).mappings()
        ]
        for name in PRESERVED_TABLES
        if name in available
    }


def schemas(connection):
    inspector = inspect(connection)
    result = {
        name: {
            "columns": [
                (c["name"], str(c["type"]), c["nullable"], c["default"])
                for c in inspector.get_columns(name)
            ],
            "indexes": inspector.get_indexes(name),
            "foreign_keys": inspector.get_foreign_keys(name),
        }
        for name in inspector.get_table_names()
        if name != "alembic_version"
    }
    if connection.dialect.name == "postgresql":
        result["_rls"] = [
            tuple(row)
            for row in connection.execute(
                text(
                    "SELECT c.relname,c.relrowsecurity,c.relforcerowsecurity FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' "
                    "AND c.relkind='r' AND c.relname<>'alembic_version' ORDER BY c.relname"
                )
            )
        ]
        result["_policies"] = [
            tuple(row)
            for row in connection.execute(
                text(
                    "SELECT tablename,policyname,cmd,qual,with_check FROM pg_policies "
                    "WHERE schemaname='public' ORDER BY tablename,policyname"
                )
            )
        ]
    # Inspector dialect options can contain equivalent TextClause objects that
    # compare by Python identity. Preserve their complete SQL text rather than
    # treating a fresh reflection object as a physical schema change.
    return json.loads(json.dumps(result, default=str, sort_keys=True))


def test_complete_graph_has_one_merge_head_and_no_missing_dependencies():
    config = Config()
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    graph = ScriptDirectory.from_config(config)
    assert graph.get_heads() == [PLANNING_COMMANDS]
    assert graph.get_revision(PLANNING_COMMANDS).down_revision == MERGE
    assert set(graph.get_revision(MERGE).down_revision) == PARENTS
    assert set(graph.get_revision(OLD_MERGE).down_revision) == {PLANNING, OCR}
    assert graph.get_revision(PAYMENT).down_revision == OCR
    revisions = list(graph.walk_revisions())
    assert len(revisions) == 156
    assert len({r.revision for r in revisions}) == len(revisions)
    assert graph.get_revision(MAIN)
    # walk_revisions resolves all down_revision and depends_on edges, not just
    # the three feature scripts named in the report.
    assert all(graph.get_revision(r.revision) for r in revisions)
    old_marker = BACKEND_DIR / "alembic/versions/e5f7091a2345_merge_planning_and_region_ocr.py"
    assert hashlib.sha256(old_marker.read_bytes()).hexdigest() == (
        "bc8b784a0df8a244d7b23ddfea5d7c14a5d10850ae20191adc691cbb29dcd4a0"
    )
    marker = next(path for path in (BACKEND_DIR / "alembic/versions").glob(f"{MERGE}_*.py"))
    functions = {
        node.name: node
        for node in ast.parse(marker.read_text()).body
        if isinstance(node, ast.FunctionDef)
    }
    assert set(functions) == {"upgrade", "downgrade"}
    assert all(len(fn.body) == 1 and isinstance(fn.body[0], ast.Pass) for fn in functions.values())


@pytest.mark.parametrize("start", STARTS, ids=lambda revisions: "+".join(revisions) or "empty")
def test_upgrade_and_marker_roundtrip_preserve_records(migration_url, start):
    before = None
    identities = None
    for revision in start:
        migrate(migration_url, "upgrade", revision)
    if start:
        identities = database(migration_url, seed)
        before = database(migration_url, snapshot)
    migrate(migration_url, "upgrade", "head")
    if identities is None:
        identities = database(migration_url, seed)
    after = database(migration_url, snapshot)
    if before:
        for table, rows in before.items():
            for old, new in zip(rows, after[table], strict=True):
                assert all(new[key] == value for key, value in old.items())
    for assignment in after["project_assignments"]:
        assert assignment["time_zone"] == "UTC" and assignment["revision"] == 1
        assert assignment["notify_assignee"] and assignment["reminders_enabled"]
        assert assignment["client_notice_enabled"] is None
    database(migration_url, lambda c: seed_feature_evidence(c, identities))
    after = database(migration_url, snapshot)
    assert set(after) == set(PRESERVED_TABLES)
    assert all(len(rows) == 2 for rows in after.values())
    migrate(migration_url, "check")

    def index_contract(connection):
        indexes = inspect(connection).get_indexes("expense_commands")
        index = next(i for i in indexes if i["name"] == "ix_expense_commands_org_operation_target")
        assert index["column_names"] == ["org_id", "operation", "target_id"]
        assert not index["unique"]
        if connection.dialect.name == "postgresql":
            assert connection.scalar(
                text(
                    "SELECT indisvalid AND indisready FROM pg_index WHERE indexrelid="
                    "'ix_expense_commands_org_operation_target'::regclass"
                )
            )

    database(migration_url, index_contract)

    head_schema = database(migration_url, schemas)
    # The additive planning-command migration is now the linear head. Its
    # empty receipt table can be removed; test merge ambiguity AT the merge,
    # while the separate planning migration tests refuse populated downgrade.
    migrate(migration_url, "downgrade", MERGE)
    schema_before = database(migration_url, schemas)
    assert set(head_schema) - set(schema_before) == {"planning_commands"}
    assert {
        key: value for key, value in head_schema.items() if key != "planning_commands"
    } == schema_before
    assert database(migration_url, snapshot) == after
    # An unqualified relative downgrade is ambiguous at a merge point.
    refused = _run_alembic(["downgrade", "-1"], migration_url)
    assert refused.returncode != 0 and "Ambiguous walk" in refused.stdout + refused.stderr
    # An explicit parent removes only the marker and exposes BOTH parents.
    migrate(migration_url, "downgrade", OLD_MERGE)
    versions = database(
        migration_url,
        lambda c: set(c.execute(text("SELECT version_num FROM alembic_version")).scalars()),
    )
    assert versions == PARENTS
    assert database(migration_url, schemas) == schema_before
    assert database(migration_url, snapshot) == after
    migrate(migration_url, "upgrade", "head")
    assert database(migration_url, snapshot) == after
    assert database(migration_url, schemas) == head_schema


def test_downgrade_refuses_changed_assignments_and_applied_ocr(migration_url):
    migrate(migration_url, "upgrade", MAIN)
    database(migration_url, seed)
    migrate(migration_url, "upgrade", "head")

    def guards(connection):
        connection.execute(text("UPDATE project_assignments SET revision=2, notify_assignee=false"))
        identities = [
            {"org": row[0], "user": row[1], "run": row[2], "assignment": row[3], "session": row[4]}
            for row in connection.execute(
                text(
                    "SELECT r.org_id,u.id,r.id,a.id,s.id FROM extraction_runs r "
                    "JOIN users u ON u.org_id=r.org_id JOIN project_assignments a "
                    "ON a.org_id=r.org_id JOIN sessions s ON s.user_id=u.id"
                )
            )
        ]
        seed_feature_evidence(connection, identities)
        region_reads = Table("capture_region_reads", MetaData(), autoload_with=connection)
        connection.execute(region_reads.update().values(applied_at=STAMP))
        before = snapshot(connection)
        schema_before = schemas(connection)
        for filename, message in (
            ("b2c4d6e8f010_assignment_delivery_profiles.py", "retain the additive schema"),
            ("d4e6f8a01223_capture_region_reads.py", "Applied region evidence exists"),
        ):
            path = Path(BACKEND_DIR) / "alembic/versions" / filename
            spec = importlib.util.spec_from_file_location("guard", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            with Operations.context(MigrationContext.configure(connection)):
                with pytest.raises(RuntimeError, match=message):
                    module.downgrade()
        assert connection.scalar(text("SELECT revision FROM project_assignments")) == 2
        assert snapshot(connection) == before
        assert schemas(connection) == schema_before

    database(migration_url, guards)


def test_postgres_new_and_financial_tables_keep_force_rls(migration_url):
    if make_url(migration_url).get_backend_name() != "postgresql":
        pytest.skip("PostgreSQL RLS contract")
    migrate(migration_url, "upgrade", "head")
    identities = database(migration_url, seed)

    def isolation(connection):
        assert connection.execute(
            text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user")
        ).one() == (False, False)
        guarded = (
            "expense_commands",
            "capture_region_reads",
            "calendar_connections",
            "calendar_oauth_attempts",
            "calendar_event_mappings",
        )
        for table in guarded:
            flags = connection.execute(
                text(
                    "SELECT relrowsecurity,relforcerowsecurity FROM pg_class "
                    "WHERE oid=CAST(:table AS regclass)"
                ),
                {"table": table},
            ).one()
            assert flags == (True, True)
        before = snapshot(connection)
        connection.execute(
            text("SELECT set_config('app.current_org', :org, true)"), {"org": identities[0]["org"]}
        )
        for table in guarded:
            assert connection.scalar(text(f"SELECT count(*) FROM {table}")) == 1
        with pytest.raises(DBAPIError, match="row-level security"):
            with connection.begin_nested():
                _insert(
                    connection,
                    "expense_commands",
                    org_id=identities[1]["org"],
                    employee_id=identities[1]["user"],
                    command_key="cross-tenant-refused",
                    operation="reimbursement_paid",
                    payload_hash="e" * 64,
                    target_id=identities[1]["batch"],
                    result_json='{"synthetic":true}',
                )
        connection.execute(text("SELECT set_config('app.current_org', '', true)"))
        assert snapshot(connection) == before

    database(migration_url, isolation)


@pytest.mark.parametrize("interruption", ["invalid", "valid_unstamped"])
def test_payment_index_interruption_refuses_without_guessing_and_recovers(
    migration_url, interruption
):
    if make_url(migration_url).get_backend_name() != "postgresql":
        pytest.skip("Native concurrent-index interruption contract")
    migrate(migration_url, "upgrade", OLD_MERGE)
    database(migration_url, seed)
    before = database(migration_url, snapshot)
    index = "ix_expense_commands_org_operation_target"

    async def native_index(create):
        import asyncpg

        url = make_url(migration_url).set(drivername="postgresql")
        connection = await asyncpg.connect(url.render_as_string(hide_password=False))
        try:
            if not create:
                await connection.execute(f"DROP INDEX CONCURRENTLY {index}")
            elif interruption == "invalid":
                # Failure after catalog registration leaves an invalid native
                # index. A row-dependent zero avoids plan-time constant folding.
                with pytest.raises(asyncpg.DivisionByZeroError):
                    await connection.execute(
                        f"CREATE INDEX CONCURRENTLY {index} ON expense_commands "
                        "(((length(result_json)+1)/(length(result_json)-length(result_json))))"
                    )
            else:
                await connection.execute(
                    f"CREATE INDEX CONCURRENTLY {index} ON expense_commands "
                    "(org_id,operation,target_id)"
                )
        finally:
            await connection.close()

    asyncio.run(native_index(True))

    def index_state(connection):
        return connection.execute(
            text(
                "SELECT i.indisvalid,i.indisready,pg_get_indexdef(i.indexrelid) "
                "FROM pg_index i WHERE indexrelid=CAST(:name AS regclass)"
            ),
            {"name": index},
        ).one()

    state = database(migration_url, index_state)
    assert bool(state[0]) is (interruption == "valid_unstamped")
    failed = _run_alembic(["upgrade", "head"], migration_url)
    assert failed.returncode != 0 and f"{index} already exists" in failed.stdout + failed.stderr
    assert database(migration_url, snapshot) == before
    assert database(migration_url, index_state) == state
    versions = database(
        migration_url,
        lambda c: set(c.execute(text("SELECT version_num FROM alembic_version")).scalars()),
    )
    assert versions == {OLD_MERGE}
    # This cleanup is deliberately limited to the known synthetic interrupted
    # index in this disposable DB; production recovery remains an operator action.
    asyncio.run(native_index(False))
    migrate(migration_url, "upgrade", "head")
    assert database(migration_url, snapshot) == before
    rebuilt = database(migration_url, index_state)
    assert rebuilt[0] and rebuilt[1]
    assert "(org_id, operation, target_id)" in rebuilt[2]
    migrate(migration_url, "check")
