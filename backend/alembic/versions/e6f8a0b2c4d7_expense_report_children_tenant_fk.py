"""Keep expense comments and approval steps in their report's tenant.

PREFLIGHT: reject missing/wrong-tenant parents with masked counts before DDL.
PostgreSQL excludes parent/child writes in a fixed order and retains FORCE RLS
under the existing unscoped maintenance policy. SQLite requires a dedicated
FK-off migration connection, validates existing FKs and owns a real transaction.
POSTCONDITION: both mandatory report links include org_id and retain CASCADE.
The revision159 parent index and every optional link remain untouched.
ROLLBACK: restore only the two original single-column CASCADE report links.
Both replacements and postvalidation share a savepoint; history is never repaired.
"""

from __future__ import annotations

from collections import Counter

import sqlalchemy as sa

from alembic import op

revision = "e6f8a0b2c4d7"
down_revision = "d5e7f9a1b3c6"
branch_labels = None
depends_on = None

TARGET_INDEX = "uq_expense_reports_org_id"
LINKS = (
    ("expense_comments", "fk_expense_comments_report", "expense_comments_report_id_fkey"),
    (
        "expense_approval_steps",
        "fk_expense_approval_steps_report",
        "expense_approval_steps_report_id_fkey",
    ),
)


def _prepare(connection) -> None:
    if connection.dialect.name == "postgresql":
        current_org = connection.scalar(sa.text("SELECT current_setting('app.current_org', true)"))
        if current_org not in (None, ""):
            raise RuntimeError("Expense child preflight requires an unscoped migration connection")
        connection.execute(
            sa.text(
                "LOCK TABLE expense_reports, expense_comments, expense_approval_steps "
                "IN SHARE ROW EXCLUSIVE MODE"
            )
        )
    elif connection.dialect.name == "sqlite":
        if connection.scalar(sa.text("PRAGMA foreign_keys")):
            raise RuntimeError(
                "Expense child migration requires a dedicated SQLite foreign_keys=OFF connection"
            )
        driver = connection.connection.driver_connection
        in_transaction = getattr(driver, "in_transaction", None)
        if not isinstance(in_transaction, bool):
            raise RuntimeError("Cannot establish SQLite migration transaction ownership")
        if not in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
    else:
        raise RuntimeError("Expense child migration supports PostgreSQL and SQLite only")


def _check_sqlite_foreign_keys(connection) -> None:
    if connection.dialect.name != "sqlite":
        return
    failures = Counter(row[0] for row in connection.execute(sa.text("PRAGMA foreign_key_check")))
    if failures:
        raise RuntimeError(
            "Expense child migration refused existing foreign-key violations: "
            + ", ".join(f"{table}={count}" for table, count in sorted(failures.items()))
        )


def _check_target(connection) -> None:
    for index in sa.inspect(connection).get_indexes("expense_reports"):
        if (
            index["name"] == TARGET_INDEX
            and index["unique"]
            and index["column_names"] == ["org_id", "id"]
            and not any("where" in key for key in index.get("dialect_options", {}))
        ):
            return
    raise RuntimeError("Expected revision159 report pair index is absent or incompatible")


def _preflight(connection) -> None:
    failures = []
    for table, _composite, _original in LINKS:
        counts = connection.execute(
            sa.text(
                "SELECT count(*) FILTER (WHERE r.id IS NULL), "
                "count(*) FILTER (WHERE r.id IS NOT NULL AND c.org_id <> r.org_id) "
                f"FROM {table} c LEFT JOIN expense_reports r ON r.id = c.report_id"
            )
        ).one()
        if counts[0] or counts[1]:
            failures.append(f"{table}: missing_parent={counts[0]}, wrong_tenant={counts[1]}")
    if failures:
        raise RuntimeError(
            "Expense child migration refused inconsistent history: " + "; ".join(failures)
        )


def _fk_name(connection, table, columns, referred_columns, expected_name=None) -> str:
    for fk in sa.inspect(connection).get_foreign_keys(table):
        if (
            fk["constrained_columns"] == columns
            and fk["referred_table"] == "expense_reports"
            and fk["referred_columns"] == referred_columns
        ):
            options = fk.get("options") or {}
            if (
                (options.get("ondelete") or "").upper() != "CASCADE"
                or (options.get("onupdate") or "NO ACTION").upper() != "NO ACTION"
                or options.get("deferrable", False)
                or (options.get("initially") or "IMMEDIATE").upper() != "IMMEDIATE"
                or (options.get("match") or "SIMPLE").upper() != "SIMPLE"
                or (expected_name is not None and fk["name"] != expected_name)
            ):
                raise RuntimeError(f"Unexpected {table} report foreign-key contract")
            return fk["name"] or f"fk_{table}_report_id_expense_reports"
    raise RuntimeError(f"Expected {table} report foreign key is absent")


def _replace_fk(connection, table, old_name, new_name, columns, referred_columns) -> None:
    if connection.dialect.name == "sqlite":
        # Name only the unnamed report FK so Alembic can address it. Applying
        # a whole-table naming convention would also rename unrelated org FKs.
        reflected = sa.Table(table, sa.MetaData(), autoload_with=connection)
        for constraint in reflected.foreign_key_constraints:
            if (
                constraint.name is None
                and "report_id" in constraint.columns
                and constraint.referred_table.name == "expense_reports"
            ):
                constraint.name = old_name
        with op.batch_alter_table(table, copy_from=reflected) as batch:
            batch.drop_constraint(old_name, type_="foreignkey")
            batch.create_foreign_key(
                new_name, "expense_reports", columns, referred_columns, ondelete="CASCADE"
            )
    else:
        op.drop_constraint(old_name, table, type_="foreignkey")
        op.create_foreign_key(
            new_name, table, "expense_reports", columns, referred_columns, ondelete="CASCADE"
        )


def upgrade() -> None:
    connection = op.get_bind()
    _prepare(connection)
    _check_sqlite_foreign_keys(connection)
    _check_target(connection)
    _preflight(connection)
    old_names = {
        table: _fk_name(connection, table, ["report_id"], ["id"])
        for table, _composite, _original in LINKS
    }
    with connection.begin_nested():
        for table, composite, _original in LINKS:
            _replace_fk(
                connection,
                table,
                old_names[table],
                composite,
                ["org_id", "report_id"],
                ["org_id", "id"],
            )
        _check_sqlite_foreign_keys(connection)


def downgrade() -> None:
    connection = op.get_bind()
    _prepare(connection)
    _check_sqlite_foreign_keys(connection)
    _check_target(connection)
    _preflight(connection)
    for table, composite, _original in LINKS:
        _fk_name(connection, table, ["org_id", "report_id"], ["org_id", "id"], composite)
    with connection.begin_nested():
        for table, composite, original in LINKS:
            _replace_fk(connection, table, composite, original, ["report_id"], ["id"])
        _check_sqlite_foreign_keys(connection)
