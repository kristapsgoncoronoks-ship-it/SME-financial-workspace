"""Version bank-line intentions without changing cash or historical evidence.

Existing lines receive version1 only as a concurrency baseline. No old action,
reason or event is inferred. Masked global state/association preflight refuses
ambiguous history; amount, balance, source bytes and legacy invalid-money values
are never normalized. Existing bank_lines is never batch rebuilt or replaced.
PostgreSQL uses the established transaction-local maintenance scope and FORCE
RLS. SQLite ADD COLUMN with an inline check remains safe with FK enforcement ON.
Code rollback keeps additive schema/old writers disabled. Destructive downgrade
refuses any receipt or advanced version globally; only a pristine baseline can
remove the empty table, owned index and column, atomically, without FK toggles.
"""

from __future__ import annotations

from contextlib import contextmanager

import sqlalchemy as sa

from alembic import op
from app.models.base import GUID

revision = "d1e3f5a7c9b2"
down_revision = "c0d2e4f6a8b1"
branch_labels = None
depends_on = None
TENANT_TABLES = ("reconciliation_commands",)
TABLE = "reconciliation_commands"
TARGET_INDEX = "uq_bank_lines_org_id"
VERSION_CHECK = "ck_bank_lines_version"
VERSION_SQL = "version = CAST(version AS INTEGER) AND version >= 1 AND version <= 2147483647"
MAX_VERSION = 2147483647


def _prepare(connection, *, downgrade):
    if connection.dialect.name == "postgresql":
        tables = "bank_statements, bank_lines" + (", reconciliation_commands" if downgrade else "")
        connection.execute(sa.text(f"LOCK TABLE {tables} IN SHARE ROW EXCLUSIVE MODE"))
    elif connection.dialect.name == "sqlite":
        driver = connection.connection.driver_connection
        in_transaction = getattr(driver, "in_transaction", None)
        if not isinstance(in_transaction, bool):
            raise RuntimeError("Cannot establish SQLite reconciliation transaction ownership")
        if not in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        if downgrade and connection.dialect.server_version_info < (3, 35, 0):
            raise RuntimeError("Reconciliation empty rollback requires SQLite direct DROP COLUMN")
    else:
        raise RuntimeError("Reconciliation commands support PostgreSQL and SQLite only")


@contextmanager
def _atomic_scope(connection):
    # Both DDL and temporary scope live in the savepoint. A failed statement
    # rolls back the GUC too; no restoration SQL runs in an aborted transaction.
    with connection.begin_nested():
        pg = connection.dialect.name == "postgresql"
        if pg:
            previous = connection.scalar(sa.text("SELECT current_setting('app.current_org', true)"))
            connection.execute(sa.text("SET LOCAL app.current_org = ''"))
        yield
        if pg:
            connection.execute(
                sa.text("SELECT set_config('app.current_org', :org, true)"),
                {"org": previous if previous is not None else ""},
            )


def _preflight(connection):
    bad_state, bad_association = connection.execute(
        sa.text("""
        SELECT count(*) FILTER (WHERE status IS NULL OR status NOT IN ('unmatched','matched','ignored')),
            count(*) FILTER (WHERE
                (status='matched' AND (matched_kind IS NULL OR matched_kind NOT IN
                    ('receipt','reimbursement','payment_run','issued_payment') OR matched_id IS NULL))
                OR (status IN ('unmatched','ignored') AND (matched_kind IS NOT NULL OR matched_id IS NOT NULL)))
        FROM bank_lines
    """)
    ).one()
    if bad_state or bad_association:
        raise RuntimeError(
            "Reconciliation migration refused inconsistent historical state: "
            f"invalid_state={bad_state}, invalid_association={bad_association}"
        )


def _object_exists(connection, name):
    if connection.dialect.name == "sqlite":
        return bool(
            connection.scalar(
                sa.text("SELECT count(*) FROM sqlite_schema WHERE lower(name)=lower(:name)"),
                {"name": name},
            )
        )
    return connection.scalar(sa.text("SELECT to_regclass(:name) IS NOT NULL"), {"name": name})


def _before_upgrade(connection):
    inspector = sa.inspect(connection)
    if "version" in {column["name"] for column in inspector.get_columns("bank_lines")}:
        raise RuntimeError("Unexpected existing bank-line version; no adoption performed")
    if _object_exists(connection, TARGET_INDEX) or _object_exists(connection, TABLE):
        raise RuntimeError(
            "Unexpected existing reconciliation target or history object; no adoption performed"
        )
    if any(
        check["name"] == VERSION_CHECK for check in inspector.get_check_constraints("bank_lines")
    ):
        raise RuntimeError("Unexpected existing bank-line version check; no adoption performed")


def _check_installed(connection):
    inspector = sa.inspect(connection)
    columns = {column["name"]: column for column in inspector.get_columns("bank_lines")}
    version = columns.get("version")
    if (
        version is None
        or version["nullable"]
        or not isinstance(version["type"], sa.Integer)
        or str(version["default"]).strip("'() ") != "1"
    ):
        raise RuntimeError("Unexpected installed bank-line version contract")
    checks = [
        check
        for check in inspector.get_check_constraints("bank_lines")
        if check["name"] == VERSION_CHECK
    ]
    if len(checks) != 1:
        raise RuntimeError("Expected installed bank-line version check is absent")
    indexes = [
        index for index in inspector.get_indexes("bank_lines") if index["name"] == TARGET_INDEX
    ]
    if len(indexes) != 1 or not (
        indexes[0]["unique"]
        and indexes[0]["column_names"] == ["org_id", "id"]
        and not any(
            "where" in key or "include" in key and value
            for key, value in indexes[0].get("dialect_options", {}).items()
        )
    ):
        raise RuntimeError("Unexpected installed bank-line tenant target")
    if connection.dialect.name == "postgresql":
        valid = connection.scalar(
            sa.text(
                "SELECT indisunique AND indisvalid AND indisready AND indimmediate AND indpred IS NULL AND indexprs IS NULL AND indnkeyatts=2 AND indnatts=2 FROM pg_index WHERE indexrelid=CAST(:name AS regclass)"
            ),
            {"name": TARGET_INDEX},
        )
        if not valid:
            raise RuntimeError("Unexpected installed bank-line tenant target")
    expected_columns = {
        "id",
        "org_id",
        "actor_user_id",
        "line_id",
        "command_key",
        "contract_version",
        "operation",
        "payload_json",
        "payload_hash",
        "result_json",
        "result_hash",
        "before_state_json",
        "after_state_json",
        "expected_version",
        "before_version",
        "after_version",
        "reason",
        "recorded_at",
    }
    if {column["name"] for column in inspector.get_columns(TABLE)} != expected_columns:
        raise RuntimeError("Unexpected installed reconciliation history shape")
    expected_fks = {
        (("org_id",), "organizations", ("id",), "CASCADE"),
        (("actor_user_id",), "users", ("id",), "RESTRICT"),
        (("org_id", "line_id"), "bank_lines", ("org_id", "id"), "CASCADE"),
    }
    actual_fks = {
        (
            tuple(fk["constrained_columns"]),
            fk["referred_table"],
            tuple(fk["referred_columns"]),
            fk.get("options", {}).get("ondelete"),
        )
        for fk in inspector.get_foreign_keys(TABLE)
    }
    if actual_fks != expected_fks:
        raise RuntimeError("Unexpected installed reconciliation history relationships")
    if connection.dialect.name == "postgresql":
        if connection.execute(
            sa.text(
                "SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid=CAST(:table AS regclass)"
            ),
            {"table": TABLE},
        ).one() != (True, True):
            raise RuntimeError("Expected reconciliation FORCE RLS is absent")


def _create_history():
    # Frozen whitespace/source checks; do not import mutable application models.
    whitespace = " \t\n\r\v\f\x1c\x1d\x1e\x1f\u0085\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000"
    nonblank = "reason"
    for space in whitespace:
        nonblank = f"replace({nonblank}, '{space}', '')"
    op.create_table(
        TABLE,
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "org_id", GUID(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "actor_user_id", GUID(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("line_id", GUID(), nullable=False),
        sa.Column("command_key", sa.String(120), nullable=False),
        sa.Column("contract_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("operation", sa.String(16), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=False),
        sa.Column("result_hash", sa.String(64), nullable=False),
        sa.Column("before_state_json", sa.Text(), nullable=False),
        sa.Column("after_state_json", sa.Text(), nullable=False),
        sa.Column("expected_version", sa.Integer(), nullable=False),
        sa.Column("before_version", sa.Integer(), nullable=False),
        sa.Column("after_version", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "recorded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "line_id"],
            ["bank_lines.org_id", "bank_lines.id"],
            name="fk_reconciliation_commands_line",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "org_id", "actor_user_id", "command_key", name="uq_reconciliation_command_identity"
        ),
        sa.UniqueConstraint(
            "org_id", "line_id", "after_version", name="uq_reconciliation_command_line_version"
        ),
        sa.CheckConstraint("contract_version = 1", name="ck_reconciliation_commands_contract"),
        sa.CheckConstraint(
            "operation IN ('match', 'ignore', 'unmatch', 'restore')",
            name="ck_reconciliation_commands_operation",
        ),
        sa.CheckConstraint(
            "length(command_key) >= 1 AND length(command_key) <= 120 AND length(trim(command_key)) > 0",
            name="ck_reconciliation_commands_key",
        ),
        sa.CheckConstraint(
            "length(payload_hash) = 64 AND length(result_hash) = 64",
            name="ck_reconciliation_commands_hashes",
        ),
        sa.CheckConstraint(
            "expected_version = CAST(expected_version AS INTEGER) AND expected_version >= 1 AND expected_version <= 2147483647",
            name="ck_reconciliation_commands_expected_version",
        ),
        sa.CheckConstraint(
            "before_version = CAST(before_version AS INTEGER) AND before_version >= 1 AND before_version <= 2147483647",
            name="ck_reconciliation_commands_before_version",
        ),
        sa.CheckConstraint(
            "after_version = CAST(after_version AS INTEGER) AND after_version >= 1 AND after_version <= 2147483647",
            name="ck_reconciliation_commands_after_version",
        ),
        sa.CheckConstraint(
            "expected_version = before_version AND after_version = CAST(before_version AS BIGINT) + 1",
            name="ck_reconciliation_commands_transition",
        ),
        sa.CheckConstraint(
            "reason IS NULL OR length(reason) <= 1000", name="ck_reconciliation_commands_reason"
        ),
        sa.CheckConstraint(
            f"operation <> 'ignore' OR (reason IS NOT NULL AND length({nonblank}) > 0)",
            name="ck_reconciliation_commands_ignore_reason",
        ),
    )
    op.create_index(
        "ix_reconciliation_commands_org_line_version", TABLE, ["org_id", "line_id", "after_version"]
    )


def upgrade():
    connection = op.get_bind()
    _prepare(connection, downgrade=False)
    with _atomic_scope(connection):
        _before_upgrade(connection)
        _preflight(connection)
        # Column-local CHECK is intentional: direct SQLite DROP COLUMN can
        # remove this pristine-baseline column without a parent table rebuild.
        op.add_column(
            "bank_lines",
            sa.Column(
                "version",
                sa.Integer(),
                sa.CheckConstraint(VERSION_SQL, name=VERSION_CHECK),
                server_default="1",
                nullable=False,
            ),
        )
        op.create_index(TARGET_INDEX, "bank_lines", ["org_id", "id"], unique=True)
        _create_history()
        if connection.dialect.name == "postgresql":
            op.execute(sa.text(f'ALTER TABLE "{TABLE}" ENABLE ROW LEVEL SECURITY'))
            op.execute(sa.text(f'ALTER TABLE "{TABLE}" FORCE ROW LEVEL SECURITY'))
            predicate = "current_setting('app.current_org', true) IS NULL OR current_setting('app.current_org', true) = '' OR org_id::text = current_setting('app.current_org', true)"
            op.execute(
                sa.text(
                    f'CREATE POLICY tenant_isolation ON "{TABLE}" USING ({predicate}) WITH CHECK ({predicate})'
                )
            )
        _check_installed(connection)


def downgrade():
    connection = op.get_bind()
    _prepare(connection, downgrade=True)
    with _atomic_scope(connection):
        _check_installed(connection)
        _preflight(connection)
        commands = connection.scalar(sa.text(f'SELECT count(*) FROM "{TABLE}"'))
        advanced = connection.scalar(sa.text("SELECT count(*) FROM bank_lines WHERE version<>1"))
        if commands or advanced:
            raise RuntimeError(
                "Reconciliation history/version exists. Keep additive schema for code rollback; "
                f"commands={commands}, advanced_lines={advanced}. No history or version reset performed"
            )
        op.drop_table(TABLE)
        op.drop_index(TARGET_INDEX, table_name="bank_lines")
        op.drop_column("bank_lines", "version")
