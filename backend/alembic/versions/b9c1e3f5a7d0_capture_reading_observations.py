"""Add source-owned, facts-only published reading observations.

IMPACT: one tenant table, one full inbound owner-pair index and two nullable
current-summary columns. No backfill, page text or new retention policy.
POSTCONDITION: exactly one same-tenant source owner, cascading with that owner.
ROLLBACK: retain additive schema for code rollback. Destructive downgrade locks
all three tables and refuses any published history/current summary, including
tenants hidden by inherited RLS scope, before changing schema. FORCE RLS stays
enabled throughout. Native column drops preserve the existing owner tables.
"""

from __future__ import annotations

from contextlib import contextmanager

import sqlalchemy as sa

from alembic import op
from app.models.base import GUID

revision = "b9c1e3f5a7d0"
down_revision = "a8b0d2f4c6e9"
branch_labels = None
depends_on = None

TABLE = "capture_reading_observations"
OWNERS = ("extraction_runs", "inbound_invoices")
INBOUND_INDEX = "uq_inbound_invoices_org_id"
SUMMARY_COLUMN = "reading_observation_json"


def _prepare(connection, *, downgrade: bool) -> None:
    if connection.dialect.name == "postgresql":
        tables = ", ".join(
            f'"{table}"' for table in (*OWNERS, TABLE) if downgrade or table != TABLE
        )
        connection.execute(sa.text(f"LOCK TABLE {tables} IN ACCESS EXCLUSIVE MODE"))
    elif connection.dialect.name == "sqlite":
        driver = connection.connection.driver_connection
        in_transaction = getattr(driver, "in_transaction", None)
        if not isinstance(in_transaction, bool):
            raise RuntimeError("Cannot establish SQLite migration transaction ownership")
        if not in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
    else:
        raise RuntimeError("Reading observations migration supports PostgreSQL and SQLite only")


@contextmanager
def _atomic_scope(connection, *, unscoped: bool):
    # The SAVEPOINT owns both DDL and the temporary maintenance scope. A refusal
    # or failed PostgreSQL statement rolls both back before the caller can catch
    # it; never try to restore a GUC inside an already failed SQL transaction.
    with connection.begin_nested():
        scoped = unscoped and connection.dialect.name == "postgresql"
        if scoped:
            previous_org = connection.scalar(
                sa.text("SELECT current_setting('app.current_org', true)")
            )
            connection.execute(sa.text("SET LOCAL app.current_org = ''"))
        yield
        if scoped:
            connection.execute(
                sa.text("SELECT set_config('app.current_org', :org, true)"),
                {"org": previous_org if previous_org is not None else ""},
            )


def _full_pair(index) -> bool:
    return index.get("column_names") == ["org_id", "id"] and not any(
        "where" in key for key in index.get("dialect_options", {})
    )


def _native_target_valid(connection, table: str, name: str) -> bool:
    if connection.dialect.name != "postgresql":
        return True
    return bool(
        connection.scalar(
            sa.text(
                "SELECT i.indisunique AND i.indisvalid AND i.indisready AND i.indimmediate "
                "AND i.indpred IS NULL AND i.indexprs IS NULL AND i.indnkeyatts=2 "
                "FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
                "WHERE i.indrelid=CAST(:table AS regclass) AND c.relname=:name"
            ),
            {"table": table, "name": name},
        )
    )


def _check_owner_schema(connection, *, summary_present: bool) -> None:
    inspector = sa.inspect(connection)
    guid_type = str(GUID().compile(dialect=connection.dialect)).upper()
    for owner in OWNERS:
        columns = {column["name"]: column for column in inspector.get_columns(owner)}
        for name in ("id", "org_id"):
            column = columns.get(name)
            if (
                column is None
                or column["nullable"]
                or str(column["type"].compile(dialect=connection.dialect)).upper() != guid_type
            ):
                raise RuntimeError(f"Incompatible {owner} owner identity schema")
        if inspector.get_pk_constraint(owner)["constrained_columns"] != ["id"]:
            raise RuntimeError(f"Incompatible {owner} owner primary key")
        org_fks = [
            fk
            for fk in inspector.get_foreign_keys(owner)
            if fk["constrained_columns"] == ["org_id"]
        ]
        if (
            len(org_fks) != 1
            or org_fks[0]["referred_table"] != "organizations"
            or org_fks[0]["referred_columns"] != ["id"]
            or (org_fks[0].get("options", {}).get("ondelete") or "").upper() != "CASCADE"
        ):
            raise RuntimeError(f"Incompatible {owner} owner organization foreign key")
        summary = columns.get(SUMMARY_COLUMN)
        if summary_present:
            if (
                summary is None
                or not summary["nullable"]
                or str(summary["type"]).upper() != "TEXT"
                or summary["default"] is not None
            ):
                raise RuntimeError(f"Incompatible {owner} current-summary schema")
        elif summary is not None:
            raise RuntimeError(f"Preexisting {owner} current-summary column; refusing adoption")
    extraction_targets = inspector.get_unique_constraints("extraction_runs") + [
        index for index in inspector.get_indexes("extraction_runs") if index["unique"]
    ]
    if not any(
        _full_pair(target) and _native_target_valid(connection, "extraction_runs", target["name"])
        for target in extraction_targets
    ):
        raise RuntimeError("Incompatible extraction_runs owner tenant target")


def _preflight(connection, *, downgrade: bool) -> None:
    _check_owner_schema(connection, summary_present=downgrade)
    inspector = sa.inspect(connection)
    existing_indexes = [
        (table, index)
        for table in inspector.get_table_names()
        for index in inspector.get_indexes(table)
        if index["name"] == INBOUND_INDEX
    ]
    if downgrade:
        if (
            len(existing_indexes) != 1
            or existing_indexes[0][0] != "inbound_invoices"
            or not existing_indexes[0][1]["unique"]
            or not _full_pair(existing_indexes[0][1])
            or not _native_target_valid(connection, "inbound_invoices", INBOUND_INDEX)
        ):
            raise RuntimeError("Incompatible inbound owner tenant index; refusing downgrade")
    else:
        if existing_indexes:
            raise RuntimeError("Preexisting inbound owner tenant index; refusing adoption")
        if inspector.has_table(TABLE):
            raise RuntimeError("Preexisting reading observations table; refusing adoption")


def upgrade() -> None:
    connection = op.get_bind()
    _prepare(connection, downgrade=False)
    with _atomic_scope(connection, unscoped=False):
        _preflight(connection, downgrade=False)
        for owner in OWNERS:
            op.add_column(owner, sa.Column(SUMMARY_COLUMN, sa.Text(), nullable=True))
        op.create_index(INBOUND_INDEX, "inbound_invoices", ["org_id", "id"], unique=True)
        op.create_table(
            TABLE,
            sa.Column("id", GUID(), nullable=False),
            sa.Column("org_id", GUID(), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column("extraction_run_id", GUID(), nullable=True),
            sa.Column("inbound_invoice_id", GUID(), nullable=True),
            sa.Column("source_sha256", sa.String(64), nullable=True),
            sa.Column("attempt_seq", sa.Integer(), nullable=True),
            sa.Column("outcome", sa.String(8), nullable=False),
            sa.Column("failure_code", sa.String(40), nullable=True),
            sa.Column("profile_json", sa.Text(), nullable=True),
            sa.Column("page_facts_json", sa.Text(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(
                ["org_id", "extraction_run_id"],
                ["extraction_runs.org_id", "extraction_runs.id"],
                name="fk_capture_reading_run",
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["org_id", "inbound_invoice_id"],
                ["inbound_invoices.org_id", "inbound_invoices.id"],
                name="fk_capture_reading_inbound",
                ondelete="CASCADE",
            ),
            sa.UniqueConstraint("org_id", "id", name="uq_capture_reading_org_id"),
            sa.CheckConstraint(
                "(extraction_run_id IS NOT NULL AND inbound_invoice_id IS NULL) OR "
                "(extraction_run_id IS NULL AND inbound_invoice_id IS NOT NULL)",
                name="ck_capture_reading_one_owner",
            ),
            sa.CheckConstraint(
                "attempt_seq IS NULL OR attempt_seq >= 0",
                name="ck_capture_reading_attempt_nonnegative",
            ),
            sa.CheckConstraint(
                "(extraction_run_id IS NOT NULL AND attempt_seq IS NOT NULL) OR "
                "(inbound_invoice_id IS NOT NULL AND attempt_seq IS NULL)",
                name="ck_capture_reading_owner_generation",
            ),
            sa.CheckConstraint(
                "outcome IN ('success', 'failure')", name="ck_capture_reading_outcome"
            ),
        )
        op.create_index(
            "ix_capture_reading_org_run_history",
            TABLE,
            ["org_id", "extraction_run_id", "created_at", "id"],
        )
        op.create_index(
            "ix_capture_reading_org_inbound_history",
            TABLE,
            ["org_id", "inbound_invoice_id", "created_at", "id"],
        )
        if connection.dialect.name == "postgresql":
            op.execute(sa.text(f'ALTER TABLE "{TABLE}" ENABLE ROW LEVEL SECURITY'))
            op.execute(sa.text(f'ALTER TABLE "{TABLE}" FORCE ROW LEVEL SECURITY'))
            predicate = (
                "current_setting('app.current_org', true) IS NULL OR "
                "current_setting('app.current_org', true) = '' OR "
                "org_id::text = current_setting('app.current_org', true)"
            )
            op.execute(
                sa.text(
                    f'CREATE POLICY tenant_isolation ON "{TABLE}" '
                    f"USING ({predicate}) WITH CHECK ({predicate})"
                )
            )


def downgrade() -> None:
    connection = op.get_bind()
    _prepare(connection, downgrade=True)
    with _atomic_scope(connection, unscoped=True):
        _preflight(connection, downgrade=True)
        populated = bool(connection.scalar(sa.text(f'SELECT count(*) FROM "{TABLE}"')))
        for owner in OWNERS:
            populated |= bool(
                connection.scalar(
                    sa.text(f'SELECT count(*) FROM "{owner}" WHERE {SUMMARY_COLUMN} IS NOT NULL')
                )
            )
        if populated:
            raise RuntimeError(
                "Published reading observations or current summaries exist. Keep the additive "
                "schema for code rollback; do not erase recorded reading provenance."
            )
        # Remove the referencing child before the newly added inbound FK target.
        op.drop_table(TABLE)
        op.drop_index(INBOUND_INDEX, table_name="inbound_invoices")
        for owner in reversed(OWNERS):
            op.drop_column(owner, SUMMARY_COLUMN)
