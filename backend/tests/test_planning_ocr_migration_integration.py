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
from copy import deepcopy
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
JOB_LEASE = "b2e4f6a8c0d3"
IMAGE_READING = "c3f5a7b9d1e4"
EXPENSE_ITEM = "d5e7f9a1b3c6"
EXPENSE_CHILDREN = "e6f8a0b2c4d7"
PARTNER_DOCUMENTS = "f7a9c1e3b5d8"
WEBHOOK_DELIVERIES = "a8b0d2f4c6e9"
READING_OBSERVATIONS = "b9c1e3f5a7d0"
OPTIONAL_PARENTS = "c0d2e4f6a8b1"
RECONCILIATION_COMMANDS = "d1e3f5a7c9b2"
EXPECTED_HEAD = RECONCILIATION_COMMANDS
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
    "expense_transactions",
    "expense_comments",
    "expense_approval_steps",
    "expense_commands",
    "audit_events",
    "partners",
    "partner_documents",
    "issued_invoices",
    "recurring_invoices",
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
        (
            org,
            user,
            project,
            run,
            assignment,
            batch,
            report,
            endpoint,
            delivery,
            session,
            partner,
            partner_document,
            issued_invoice,
            recurring_invoice,
        ) = (str(uuid.uuid4()) for _ in range(14))
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
        item = _insert(
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
            "expense_transactions",
            org_id=org,
            employee_id=user,
            item_id=item,
            txn_date=date(2026, 10, 28),
            description="Synthetic retained statement link",
            amount=Decimal("35.07"),
            currency="EUR",
            source="card",
            status="assigned",
        )
        _insert(
            connection,
            "expense_comments",
            org_id=org,
            report_id=report,
            author_id=user,
            author_name="Synthetic migration employee",
            body="Synthetic retained review evidence",
        )
        _insert(
            connection,
            "expense_approval_steps",
            org_id=org,
            report_id=report,
            seq=0,
            status="approved",
            decided_by=user,
            decided_by_email=email,
            decided_at=STAMP,
            note="Synthetic retained approval evidence",
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
            "partners",
            id=partner,
            org_id=org,
            name="Synthetic retained partner",
            requires_contract=True,
        )
        _insert(
            connection,
            "partner_documents",
            id=partner_document,
            org_id=org,
            partner_id=partner,
            kind="contract",
            title="Synthetic retained signed contract",
            status="signed",
            signed_by="Synthetic migration signer",
            signed_date=STAMP.date(),
        )
        _insert(
            connection,
            "issued_invoices",
            id=issued_invoice,
            org_id=org,
            partner_id=partner,
            buyer_name="Synthetic retained buyer",
            seller_json='{"legal_name":"Synthetic retained seller"}',
            issue_date=STAMP.date(),
            currency="EUR",
            subtotal=Decimal("35.07"),
            tax_total=Decimal("0.00"),
            total=Decimal("35.07"),
        )
        _insert(
            connection,
            "recurring_invoices",
            id=recurring_invoice,
            org_id=org,
            partner_id=partner,
            title="Synthetic retained cadence",
            template_json=json.dumps(
                {
                    "partner_id": partner,
                    "buyer_name": "Synthetic retained buyer",
                    "vat_scheme": "standard",
                    "lines": [
                        {
                            "description": "Synthetic retained service",
                            "quantity": "1",
                            "unit_price": "35.07",
                            "vat_rate": "0",
                        }
                    ],
                },
                sort_keys=True,
            ),
            start_date=STAMP.date(),
            next_run_date=(STAMP + timedelta(days=30)).date(),
            frequency="monthly",
            interval=1,
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
                "partner": partner,
                "partner_document": partner_document,
                "endpoint": endpoint,
                "delivery": delivery,
                "issued_invoice": issued_invoice,
                "recurring_invoice": recurring_invoice,
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


def _records_before_additive_heads(records):
    expected = deepcopy(records)
    for job in expected["jobs"]:
        # This migration adds exactly one nullable column and infers no past
        # lease identities. Keep every other field/value in the comparison.
        assert job.pop("lease_token") is None
    for run in expected["extraction_runs"]:
        # Historical image preparation is deliberately unknown. These tests
        # may remove only the newly added NULL, never measured provenance.
        assert run.pop("reading_preparation_json") is None
        assert run.pop("reading_observation_json") is None
    return expected


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
            "unique_constraints": inspector.get_unique_constraints(name),
            "check_constraints": inspector.get_check_constraints(name),
            "primary_key": inspector.get_pk_constraint(name),
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
                    "SELECT tablename,policyname,cmd,qual,with_check,roles,permissive "
                    "FROM pg_policies "
                    "WHERE schemaname='public' ORDER BY tablename,policyname"
                )
            )
        ]
    # Inspector dialect options can contain equivalent TextClause objects that
    # compare by Python identity. Preserve their complete SQL text rather than
    # treating a fresh reflection object as a physical schema change.
    result = json.loads(json.dumps(result, default=str, sort_keys=True))
    for name, table in result.items():
        if name.startswith("_"):
            continue
        for key in ("indexes", "foreign_keys", "unique_constraints", "check_constraints"):
            # Reflection order is not a constraint attribute. Preserve every
            # field in each entry, including physical names and dialect options.
            table[key].sort(key=lambda entry: json.dumps(entry, sort_keys=True))
    return result


def _expense_marker_schema(expected, head, marker, backend, revision):
    """Project only the exact report-parent constraints added after revision158."""
    assert revision in (EXPENSE_ITEM, EXPENSE_CHILDREN)
    index = {
        "name": "uq_expense_reports_org_id",
        "unique": True,
        "column_names": ["org_id", "id"],
        "dialect_options": {"postgresql_include": []} if backend == "postgresql" else {},
    }
    assert [
        entry for entry in head["expense_reports"]["indexes"] if entry["name"] == index["name"]
    ] == [index]
    expected["expense_reports"]["indexes"].remove(index)
    assert all(entry["name"] != index["name"] for entry in marker["expense_reports"]["indexes"])
    links = [("expense_items", "fk_expense_items_report")]
    if revision == EXPENSE_CHILDREN:
        links.extend(
            [
                ("expense_comments", "fk_expense_comments_report"),
                ("expense_approval_steps", "fk_expense_approval_steps_report"),
            ]
        )
    for table, name in links:
        legacy = [
            entry
            for entry in marker[table]["foreign_keys"]
            if entry["referred_table"] == "expense_reports"
        ]
        # SQLite revision159 restores the canonical report FK name, rather
        # than the unnamed original. Keep that recorded physical-name fact.
        legacy_contract = {
            "name": f"{table}_report_id_fkey",
            "constrained_columns": ["report_id"],
            "referred_schema": None,
            "referred_table": "expense_reports",
            "referred_columns": ["id"],
            "options": {"ondelete": "CASCADE"},
        }
        if backend == "postgresql":
            legacy_contract["comment"] = None
        assert legacy == [legacy_contract]
        composite = deepcopy(legacy[0])
        composite.update(
            name=name,
            constrained_columns=["org_id", "report_id"],
            referred_columns=["org_id", "id"],
        )
        assert [
            entry
            for entry in head[table]["foreign_keys"]
            if entry["referred_table"] == "expense_reports"
        ] == [composite]
        expected[table]["foreign_keys"].remove(composite)
        expected[table]["foreign_keys"].append(deepcopy(legacy[0]))
        expected[table]["foreign_keys"].sort(key=lambda entry: json.dumps(entry, sort_keys=True))


def _partner_webhook_marker_schema(expected, head, marker, backend):
    """Project only revision161/162's exact two pair indexes and parent FKs."""
    for parent, table, column, index_name, composite_name, legacy_name in (
        (
            "partners",
            "partner_documents",
            "partner_id",
            "uq_partners_org_id",
            "fk_partner_documents_partner",
            "partner_documents_partner_id_fkey",
        ),
        (
            "webhook_endpoints",
            "webhook_deliveries",
            "endpoint_id",
            "uq_webhook_endpoints_org_id",
            "fk_webhook_deliveries_endpoint",
            "webhook_deliveries_endpoint_id_fkey",
        ),
    ):
        index = {
            "name": index_name,
            "unique": True,
            "column_names": ["org_id", "id"],
            "dialect_options": {"postgresql_include": []} if backend == "postgresql" else {},
        }
        assert [entry for entry in head[parent]["indexes"] if entry["name"] == index_name] == [
            index
        ]
        expected[parent]["indexes"].remove(index)
        assert all(entry["name"] != index_name for entry in marker[parent]["indexes"])
        # The SQLite161/162 downgrades restore only these target FKs using
        # their canonical PostgreSQL-style names. Their original unnamed
        # SQLite forms are not normalized away anywhere else in the schema.
        legacy = {
            "name": legacy_name,
            "constrained_columns": [column],
            "referred_schema": None,
            "referred_table": parent,
            "referred_columns": ["id"],
            "options": {"ondelete": "CASCADE"},
        }
        if backend == "postgresql":
            legacy["comment"] = None
        assert [
            entry for entry in marker[table]["foreign_keys"] if entry["referred_table"] == parent
        ] == [legacy]
        composite = deepcopy(legacy)
        composite.update(
            name=composite_name,
            constrained_columns=["org_id", column],
            referred_columns=["org_id", "id"],
        )
        assert [
            entry for entry in head[table]["foreign_keys"] if entry["referred_table"] == parent
        ] == [composite]
        expected[table]["foreign_keys"].remove(composite)
        expected[table]["foreign_keys"].append(deepcopy(legacy))
        expected[table]["foreign_keys"].sort(key=lambda entry: json.dumps(entry, sort_keys=True))

    # Neither target index changes the two optional partner detach contracts.
    for table, name in (
        ("issued_invoices", "fk_issued_partner"),
        (
            "recurring_invoices",
            "recurring_invoices_partner_id_fkey" if backend == "postgresql" else None,
        ),
    ):
        optional = {
            "name": name,
            "constrained_columns": ["partner_id"],
            "referred_schema": None,
            "referred_table": "partners",
            "referred_columns": ["id"],
            "options": {"ondelete": "SET NULL"},
        }
        if backend == "postgresql":
            optional["comment"] = None
        for snapshot_schema in (head, marker):
            assert [
                entry
                for entry in snapshot_schema[table]["foreign_keys"]
                if entry["referred_table"] == "partners"
            ] == [optional]
    idempotency_index = {
        "name": "uq_webhook_deliv_idem",
        "unique": True,
        "column_names": ["endpoint_id", "idempotency_key"],
        "dialect_options": {
            "postgresql_include": [],
            "postgresql_where": "(idempotency_key IS NOT NULL)",
        }
        if backend == "postgresql"
        else {"sqlite_where": "idempotency_key IS NOT NULL"},
    }
    for snapshot_schema in (head, marker):
        assert [
            entry
            for entry in snapshot_schema["webhook_deliveries"]["indexes"]
            if entry["name"] == "uq_webhook_deliv_idem"
        ] == [idempotency_index]


def _reading_marker_schema(expected, head, marker, backend):
    """Project only the reviewed additive163 contract, retaining unrelated drift."""
    table = "capture_reading_observations"
    fixture = Path(__file__).parent / "fixtures" / f"reading_observation_schema_{backend}.json"
    assert head[table] == json.loads(fixture.read_text())
    assert table not in marker
    del expected[table]
    index = {
        "name": "uq_inbound_invoices_org_id",
        "unique": True,
        "column_names": ["org_id", "id"],
        "dialect_options": {"postgresql_include": []} if backend == "postgresql" else {},
    }
    assert [
        entry for entry in head["inbound_invoices"]["indexes"] if entry["name"] == index["name"]
    ] == [index]
    assert all(entry["name"] != index["name"] for entry in marker["inbound_invoices"]["indexes"])
    expected["inbound_invoices"]["indexes"].remove(index)
    for owner in ("extraction_runs", "inbound_invoices"):
        column = ["reading_observation_json", "TEXT", True, None]
        assert [entry for entry in head[owner]["columns"] if entry[0] == column[0]] == [column]
        assert all(entry[0] != column[0] for entry in marker[owner]["columns"])
        expected[owner]["columns"].remove(column)
    if backend == "postgresql":
        assert [row for row in head["_rls"] if row[0] == table] == [[table, True, True]]
        policies = [row for row in head["_policies"] if row[0] == table]
        assert policies == [
            [
                table,
                "tenant_isolation",
                "ALL",
                "((current_setting('app.current_org'::text, true) IS NULL) OR "
                "(current_setting('app.current_org'::text, true) = ''::text) OR "
                "((org_id)::text = current_setting('app.current_org'::text, true)))",
                "((current_setting('app.current_org'::text, true) IS NULL) OR "
                "(current_setting('app.current_org'::text, true) = ''::text) OR "
                "((org_id)::text = current_setting('app.current_org'::text, true)))",
                ["public"],
                "PERMISSIVE",
            ]
        ]


def _reconciliation_marker_schema(head, marker, backend):
    """Validate165 completely before projecting its exact additive164 shape.

    Full table fixtures retain physical FK names, checks, defaults and indexes.
    Only those verified objects are removed; unrelated drift remains visible.
    """
    table = "reconciliation_commands"
    fixture = Path(__file__).parent / "fixtures" / f"reconciliation_command_schema_{backend}.json"
    contract = json.loads(fixture.read_text())
    assert head[table] == contract["table"]
    assert table not in marker
    projected = deepcopy(head)
    del projected[table]
    for kind, name in (
        ("columns", "version"),
        ("indexes", "uq_bank_lines_org_id"),
        ("check_constraints", "ck_bank_lines_version"),
    ):
        entries = contract["bank_lines"][kind]
        assert len(entries) == 1
        identity = (lambda row: row[0]) if kind == "columns" else (lambda row: row["name"])
        assert identity(entries[0]) == name
        assert [row for row in head["bank_lines"][kind] if identity(row) == name] == entries
        assert all(identity(row) != name for row in marker["bank_lines"][kind])
        projected["bank_lines"][kind].remove(entries[0])
    if backend == "postgresql":
        assert contract["rls"] == [[table, True, True]]
        for key, expected in (("_rls", contract["rls"]), ("_policies", contract["policies"])):
            assert [row for row in head[key] if row[0] == table] == expected
            assert all(row[0] != table for row in marker[key])
            projected[key] = [row for row in projected[key] if row[0] != table]
    return projected


def _reconciliation_native_contract(connection):
    assert (
        connection.scalar(
            text(
                "SELECT indisunique AND indisvalid AND indisready AND indimmediate "
                "AND indpred IS NULL AND indexprs IS NULL AND indnkeyatts=2 AND indnatts=2 "
                "FROM pg_index WHERE indexrelid='uq_bank_lines_org_id'::regclass "
                "AND indrelid='bank_lines'::regclass"
            )
        )
        is True
    )
    assert connection.execute(
        text(
            "SELECT convalidated,condeferrable,condeferred,confdeltype::text,confupdtype::text "
            "FROM pg_constraint WHERE conrelid='reconciliation_commands'::regclass "
            "AND conname='fk_reconciliation_commands_line'"
        )
    ).one() == (True, False, False, "c", "a")


def _optional_parent_marker_schema(head, marker, backend):
    """Validate exactly164's dual guards, then project an explicit163 head.

    Older helper expectations see the proven prior shape. Unknown foreign keys,
    indexes or other schema changes remain in the final whole-schema comparison.
    """
    projected = deepcopy(head)
    for parent in ("expense_items", "documents"):
        index = {
            "name": f"uq_{parent}_org_id",
            "unique": True,
            "column_names": ["org_id", "id"],
            "dialect_options": {"postgresql_include": []} if backend == "postgresql" else {},
        }
        assert [entry for entry in head[parent]["indexes"] if entry["name"] == index["name"]] == [
            index
        ]
        assert all(entry["name"] != index["name"] for entry in marker[parent]["indexes"])
        projected[parent]["indexes"].remove(index)
    for table, column, parent, name, postgres_single, sqlite_single in (
        (
            "expense_transactions",
            "item_id",
            "expense_items",
            "fk_expense_transactions_item",
            "expense_transactions_item_id_fkey",
            "fk_expense_transactions_item_id_expense_items",
        ),
        (
            "issued_invoices",
            "partner_id",
            "partners",
            "fk_issued_invoices_partner",
            "fk_issued_partner",
            "fk_issued_partner",
        ),
        (
            "recurring_invoices",
            "partner_id",
            "partners",
            "fk_recurring_invoices_partner",
            "recurring_invoices_partner_id_fkey",
            None,
        ),
        (
            "vendor_change_requests",
            "source_document_id",
            "documents",
            "fk_vendor_change_requests_source_document",
            "vendor_change_requests_source_document_id_fkey",
            None,
        ),
    ):
        single = {
            "name": postgres_single if backend == "postgresql" else sqlite_single,
            "constrained_columns": [column],
            "referred_schema": None,
            "referred_table": parent,
            "referred_columns": ["id"],
            "options": {"ondelete": "SET NULL"},
        }
        pair = {
            "name": name,
            "constrained_columns": ["org_id", column],
            "referred_schema": None,
            "referred_table": parent,
            "referred_columns": ["org_id", "id"],
            # PostgreSQL targets only the optional ID. SQLite's immediate
            # NO ACTION pair coexists with its unchanged single SET NULL FK.
            "options": {"ondelete": f"SET NULL ({column})"} if backend == "postgresql" else {},
        }
        if backend == "postgresql":
            single["comment"] = pair["comment"] = None
        for snapshot_schema in (head, marker):
            assert [
                entry
                for entry in snapshot_schema[table]["foreign_keys"]
                if entry["constrained_columns"] == [column]
            ] == [single]
        assert [
            entry
            for entry in head[table]["foreign_keys"]
            if entry["name"] == name or entry["constrained_columns"] == ["org_id", column]
        ] == [pair]
        assert all(
            entry["name"] != name and entry["constrained_columns"] != ["org_id", column]
            for entry in marker[table]["foreign_keys"]
        )
        projected[table]["foreign_keys"].remove(pair)
    return projected


def _optional_parent_native_contract(connection):
    """Inspect targeted SET NULL and retained physical singles independently."""
    for parent in ("expense_items", "documents"):
        assert (
            connection.scalar(
                text(
                    "SELECT i.indisunique AND i.indisvalid AND i.indisready AND i.indimmediate "
                    "AND i.indpred IS NULL AND i.indexprs IS NULL AND i.indnkeyatts=2 AND i.indnatts=2 "
                    "FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
                    "WHERE i.indrelid=CAST(:parent AS regclass) AND c.relname=:name"
                ),
                {"parent": parent, "name": f"uq_{parent}_org_id"},
            )
            is True
        )
    for table, column, parent, pair, single in (
        (
            "expense_transactions",
            "item_id",
            "expense_items",
            "fk_expense_transactions_item",
            "expense_transactions_item_id_fkey",
        ),
        (
            "issued_invoices",
            "partner_id",
            "partners",
            "fk_issued_invoices_partner",
            "fk_issued_partner",
        ),
        (
            "recurring_invoices",
            "partner_id",
            "partners",
            "fk_recurring_invoices_partner",
            "recurring_invoices_partner_id_fkey",
        ),
        (
            "vendor_change_requests",
            "source_document_id",
            "documents",
            "fk_vendor_change_requests_source_document",
            "vendor_change_requests_source_document_id_fkey",
        ),
    ):
        for name, is_pair in ((pair, True), (single, False)):
            row = (
                connection.execute(
                    text(
                        "SELECT c.confrelid::regclass::text AS parent, c.confdeltype::text AS delete_action, "
                        "c.confupdtype::text AS update_action, c.confmatchtype::text AS match_type, "
                        "c.convalidated AS validated, c.condeferrable AS deferrable, c.condeferred AS deferred, "
                        "ARRAY(SELECT a.attname::text FROM unnest(c.conkey) WITH ORDINALITY n(num,pos) "
                        "JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=n.num ORDER BY n.pos) AS local_columns, "
                        "ARRAY(SELECT a.attname::text FROM unnest(c.confkey) WITH ORDINALITY n(num,pos) "
                        "JOIN pg_attribute a ON a.attrelid=c.confrelid AND a.attnum=n.num ORDER BY n.pos) AS target_columns, "
                        "c.confdelsetcols::int[] AS set_columns, "
                        "(SELECT a.attnum FROM pg_attribute a WHERE a.attrelid=c.conrelid "
                        "AND a.attname=:column AND NOT a.attisdropped) AS optional_column "
                        "FROM pg_constraint c WHERE c.conrelid=CAST(:table AS regclass) "
                        "AND c.conname=:name AND c.contype='f'"
                    ),
                    {"table": table, "column": column, "name": name},
                )
                .mappings()
                .one()
            )
            assert dict(row) == {
                "parent": parent,
                "delete_action": "n",
                "update_action": "a",
                "match_type": "s",
                "validated": True,
                "deferrable": False,
                "deferred": False,
                "local_columns": ["org_id", column] if is_pair else [column],
                "target_columns": ["org_id", "id"] if is_pair else ["id"],
                "set_columns": [row["optional_column"]] if is_pair else None,
                "optional_column": row["optional_column"],
            }


def test_complete_graph_has_one_merge_head_and_no_missing_dependencies():
    config = Config()
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    graph = ScriptDirectory.from_config(config)
    assert EXPECTED_HEAD == RECONCILIATION_COMMANDS
    assert graph.get_revision(RECONCILIATION_COMMANDS).down_revision == OPTIONAL_PARENTS
    assert graph.get_revision(OPTIONAL_PARENTS).down_revision == READING_OBSERVATIONS
    assert graph.get_revision(READING_OBSERVATIONS).down_revision == WEBHOOK_DELIVERIES
    assert graph.get_heads() == [EXPECTED_HEAD]
    assert graph.get_revision(WEBHOOK_DELIVERIES).down_revision == PARTNER_DOCUMENTS
    assert graph.get_revision(PARTNER_DOCUMENTS).down_revision == EXPENSE_CHILDREN
    assert graph.get_revision(EXPENSE_CHILDREN).down_revision == EXPENSE_ITEM
    assert graph.get_revision(EXPENSE_ITEM).down_revision == IMAGE_READING
    assert graph.get_revision(IMAGE_READING).down_revision == JOB_LEASE
    assert graph.get_revision(JOB_LEASE).down_revision == PLANNING_COMMANDS
    assert graph.get_revision(PLANNING_COMMANDS).down_revision == MERGE
    assert set(graph.get_revision(MERGE).down_revision) == PARENTS
    assert set(graph.get_revision(OLD_MERGE).down_revision) == {PLANNING, OCR}
    assert graph.get_revision(PAYMENT).down_revision == OCR
    revisions = list(graph.walk_revisions())
    assert len(revisions) == 165
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
            _optional_parent_native_contract(connection)
            _reconciliation_native_contract(connection)
            assert connection.scalar(
                text(
                    "SELECT indisvalid AND indisready FROM pg_index WHERE indexrelid="
                    "'ix_expense_commands_org_operation_target'::regclass"
                )
            )
            for target in ("uq_partners_org_id", "uq_webhook_endpoints_org_id"):
                assert connection.scalar(
                    text(
                        "SELECT indisvalid AND indisready FROM pg_index WHERE indexrelid=CAST(:target AS regclass)"
                    ),
                    {"target": target},
                )

    database(migration_url, index_contract)

    head_schema = database(migration_url, schemas)

    # Later additive heads introduce an empty planning receipt table, the
    # unused nullable job token, two unknown image-preparation fields, and
    # report/partner/webhook-parent constraints, empty reading facts and four
    # optional-parent guards and unused bank-line command/version objects.
    # Populated reading-facts downgrade refusal remains
    # independently qualified; this marker roundtrip creates no such evidence.
    # Compare only these exact deltas, then test
    # ambiguity AT the merge.
    # Separate tests guard destructive removal of populated evidence.
    def unused_reading_contract(connection):
        assert connection.scalar(text("SELECT count(*) FROM capture_reading_observations")) == 0
        assert connection.scalar(text("SELECT count(*) FROM reconciliation_commands")) == 0
        assert connection.scalar(text("SELECT count(*) FROM bank_lines WHERE version <> 1")) == 0
        for owner in ("extraction_runs", "inbound_invoices"):
            assert (
                connection.scalar(
                    text(f"SELECT count(*) FROM {owner} WHERE reading_observation_json IS NOT NULL")
                )
                == 0
            )

    database(migration_url, unused_reading_contract)
    migrate(migration_url, "downgrade", MERGE)
    schema_before = database(migration_url, schemas)
    assert set(head_schema) - set(schema_before) == {
        "planning_commands",
        "capture_reading_observations",
        "reconciliation_commands",
    }
    projected_164_head = _reconciliation_marker_schema(
        head_schema, schema_before, make_url(migration_url).get_backend_name()
    )
    projected_163_head = _optional_parent_marker_schema(
        projected_164_head, schema_before, make_url(migration_url).get_backend_name()
    )
    expected_marker_schema = deepcopy(projected_163_head)
    _reading_marker_schema(
        expected_marker_schema,
        projected_163_head,
        schema_before,
        make_url(migration_url).get_backend_name(),
    )
    _expense_marker_schema(
        expected_marker_schema,
        projected_163_head,
        schema_before,
        make_url(migration_url).get_backend_name(),
        EXPENSE_CHILDREN,
    )
    _partner_webhook_marker_schema(
        expected_marker_schema,
        projected_163_head,
        schema_before,
        make_url(migration_url).get_backend_name(),
    )
    del expected_marker_schema["planning_commands"]
    lease_columns = [c for c in head_schema["jobs"]["columns"] if c[0] == "lease_token"]
    lease_type = (
        "UUID" if make_url(migration_url).get_backend_name() == "postgresql" else "CHAR(36)"
    )
    assert lease_columns == [["lease_token", lease_type, True, None]]
    expected_marker_schema["jobs"]["columns"].remove(lease_columns[0])
    assert all(c[0] != "lease_token" for c in schema_before["jobs"]["columns"])
    for table in ("extraction_runs", "inbound_invoices"):
        preparation_columns = [
            c for c in head_schema[table]["columns"] if c[0] == "reading_preparation_json"
        ]
        assert preparation_columns == [["reading_preparation_json", "TEXT", True, None]]
        expected_marker_schema[table]["columns"].remove(preparation_columns[0])
        assert all(c[0] != "reading_preparation_json" for c in schema_before[table]["columns"])
        assert schema_before[table] == expected_marker_schema[table]
    # This includes every remaining column, index and FK; the full comparison
    # below also retains all job RLS flags, policies, roles and permissiveness.
    assert schema_before["jobs"] == expected_marker_schema["jobs"]
    if "_rls" in head_schema:
        assert [row for row in head_schema["_rls"] if row[0] == "planning_commands"] == [
            ["planning_commands", True, True]
        ]
        policies = [row for row in head_schema["_policies"] if row[0] == "planning_commands"]
        assert len(policies) == 1 and policies[0][1] == "tenant_isolation"
        for key in ("_rls", "_policies"):
            expected_marker_schema[key] = [
                row
                for row in projected_164_head[key]
                if row[0] not in ("planning_commands", "capture_reading_observations")
            ]
    assert expected_marker_schema == schema_before
    marker_records = _records_before_additive_heads(after)
    assert database(migration_url, snapshot) == marker_records
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
    assert database(migration_url, snapshot) == marker_records
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
            "expense_reports",
            "expense_items",
            "expense_transactions",
            "expense_comments",
            "expense_approval_steps",
            "partners",
            "partner_documents",
            "issued_invoices",
            "recurring_invoices",
            "webhook_endpoints",
            "webhook_deliveries",
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
    assert _records_before_additive_heads(database(migration_url, snapshot)) == before
    rebuilt = database(migration_url, index_state)
    assert rebuilt[0] and rebuilt[1]
    assert "(org_id, operation, target_id)" in rebuilt[2]
    migrate(migration_url, "check")
