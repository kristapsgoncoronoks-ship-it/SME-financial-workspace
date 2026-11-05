"""Keep each expense item in the tenant of its report.

PREFLIGHT: count missing/wrong-tenant parents globally before DDL; refuse rather
than rewrite historical rows. PostgreSQL retains FORCE RLS and uses the existing
empty-org maintenance scope under write exclusion. SQLite requires a dedicated
foreign_keys=OFF migration connection; no PRAGMA is changed here. Existing FK
violations refuse before DDL, and a post-check plus transactional DDL protects
every existing row and optional transaction-to-item reference.
POSTCONDITION: (org_id, report_id) references expense_reports(org_id, id), CASCADE.
ROLLBACK: restore only the previous single-column CASCADE FK and drop our index.
No tenant identifier, receipt, money, optional link, RLS or retention policy changes.
"""

from __future__ import annotations

from collections import Counter

import sqlalchemy as sa

from alembic import op

revision = "d5e7f9a1b3c6"
down_revision = "c3f5a7b9d1e4"
branch_labels = None
depends_on = None

TARGET_INDEX = "uq_expense_reports_org_id"
COMPOSITE_FK = "fk_expense_items_report"
OLD_FK = "expense_items_report_id_fkey"
NAMING = {"fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"}


def _prepare(connection) -> None:
    if connection.dialect.name == "postgresql":
        current_org = connection.scalar(sa.text("SELECT current_setting('app.current_org', true)"))
        if current_org not in (None, ""):
            raise RuntimeError("Expense parent preflight requires an unscoped migration connection")
        connection.execute(
            sa.text("LOCK TABLE expense_reports, expense_items IN SHARE ROW EXCLUSIVE MODE")
        )
    elif connection.dialect.name == "sqlite":
        if connection.scalar(sa.text("PRAGMA foreign_keys")):
            raise RuntimeError(
                "Expense parent migration requires a dedicated SQLite foreign_keys=OFF connection; "
                "rebuilding with enforcement enabled can detach existing transaction links"
            )
        # sqlite3's legacy transaction control does not BEGIN for DDL. Ensure
        # the savepoint and later Alembic version stamp share a real transaction.
        driver = connection.connection.driver_connection
        in_transaction = getattr(driver, "in_transaction", None)
        if not isinstance(in_transaction, bool):
            raise RuntimeError("Cannot establish SQLite migration transaction ownership")
        if not in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
    else:
        raise RuntimeError("Expense parent migration supports PostgreSQL and SQLite only")


def _check_sqlite_foreign_keys(connection) -> None:
    if connection.dialect.name != "sqlite":
        return
    # Table/count only: never expose row IDs, tenant IDs or financial content.
    failures = Counter(row[0] for row in connection.execute(sa.text("PRAGMA foreign_key_check")))
    if failures:
        raise RuntimeError(
            "Expense parent migration refused existing foreign-key violations: "
            + ", ".join(f"{table}={count}" for table, count in sorted(failures.items()))
        )


def _preflight(connection) -> None:
    counts = connection.execute(
        sa.text(
            "SELECT "
            "count(*) FILTER (WHERE r.id IS NULL) AS missing_parent, "
            "count(*) FILTER (WHERE r.id IS NOT NULL AND i.org_id <> r.org_id) AS wrong_tenant "
            "FROM expense_items i LEFT JOIN expense_reports r ON r.id = i.report_id"
        )
    ).one()
    if counts[0] or counts[1]:
        raise RuntimeError(
            "Expense parent migration refused inconsistent historical items: "
            f"missing_parent={counts[0]}, wrong_tenant={counts[1]}"
        )


def _old_fk_name(connection) -> str:
    for fk in sa.inspect(connection).get_foreign_keys("expense_items"):
        if fk["constrained_columns"] == ["report_id"] and fk["referred_table"] == "expense_reports":
            if (fk.get("options") or {}).get("ondelete", "").upper() != "CASCADE":
                raise RuntimeError("Unexpected expense item report delete contract")
            return fk["name"] or "fk_expense_items_report_id_expense_reports"
    raise RuntimeError("Expected original expense item report foreign key is absent")


def _replace_fk(connection, old_name, new_name, columns, referred_columns) -> None:
    if connection.dialect.name == "sqlite":
        with op.batch_alter_table("expense_items", naming_convention=NAMING) as batch:
            batch.drop_constraint(old_name, type_="foreignkey")
            batch.create_foreign_key(
                new_name, "expense_reports", columns, referred_columns, ondelete="CASCADE"
            )
    else:
        op.drop_constraint(old_name, "expense_items", type_="foreignkey")
        op.create_foreign_key(
            new_name,
            "expense_items",
            "expense_reports",
            columns,
            referred_columns,
            ondelete="CASCADE",
        )


def upgrade() -> None:
    connection = op.get_bind()
    _prepare(connection)
    _check_sqlite_foreign_keys(connection)
    _preflight(connection)
    old_name = _old_fk_name(connection)
    with connection.begin_nested():
        # A unique index is a valid target on both backends and avoids rebuilding
        # the parent table, whose existing children must remain untouched.
        op.create_index(TARGET_INDEX, "expense_reports", ["org_id", "id"], unique=True)
        _replace_fk(connection, old_name, COMPOSITE_FK, ["org_id", "report_id"], ["org_id", "id"])
        _check_sqlite_foreign_keys(connection)


def downgrade() -> None:
    connection = op.get_bind()
    _prepare(connection)
    _check_sqlite_foreign_keys(connection)
    _preflight(connection)
    with connection.begin_nested():
        _replace_fk(connection, COMPOSITE_FK, OLD_FK, ["report_id"], ["id"])
        op.drop_index(TARGET_INDEX, table_name="expense_reports")
        _check_sqlite_foreign_keys(connection)
