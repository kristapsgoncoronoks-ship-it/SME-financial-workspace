"""Keep each webhook_deliveries row in its parent's tenant, retaining CASCADE.

PREFLIGHT: lock parent then child; refuse missing/wrong-tenant history with
table/count-only diagnostics. PostgreSQL retains FORCE RLS and requires the
existing unscoped maintenance connection. SQLite requires a dedicated FK-off
connection, validates all existing FKs and owns a real transaction.
POSTCONDITION: mandatory (org_id, endpoint_id) -> webhook_endpoints(org_id, id).
ROLLBACK: restore only the original single-column CASCADE FK and drop our
pair index. DDL and postvalidation share one savepoint; history is never repaired.
"""

from __future__ import annotations

from collections import Counter

import sqlalchemy as sa

from alembic import op

revision = "a8b0d2f4c6e9"
down_revision = "f7a9c1e3b5d8"
branch_labels = None
depends_on = None

PARENT_TABLE = "webhook_endpoints"
CHILD_TABLE = "webhook_deliveries"
LINK_COLUMN = "endpoint_id"
TARGET_INDEX = "uq_webhook_endpoints_org_id"
COMPOSITE_FK = "fk_webhook_deliveries_endpoint"
OLD_FK = "webhook_deliveries_endpoint_id_fkey"


def _prepare(connection) -> None:
    if connection.dialect.name == "postgresql":
        current_org = connection.scalar(sa.text("SELECT current_setting('app.current_org', true)"))
        if current_org not in (None, ""):
            raise RuntimeError(f"{CHILD_TABLE} migration requires an unscoped connection")
        connection.execute(
            sa.text(f"LOCK TABLE {PARENT_TABLE}, {CHILD_TABLE} IN SHARE ROW EXCLUSIVE MODE")
        )
    elif connection.dialect.name == "sqlite":
        if connection.scalar(sa.text("PRAGMA foreign_keys")):
            raise RuntimeError(
                f"{CHILD_TABLE} migration requires a dedicated SQLite foreign_keys=OFF connection"
            )
        driver = connection.connection.driver_connection
        in_transaction = getattr(driver, "in_transaction", None)
        if not isinstance(in_transaction, bool):
            raise RuntimeError("Cannot establish SQLite migration transaction ownership")
        if not in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
    else:
        raise RuntimeError(f"{CHILD_TABLE} migration supports PostgreSQL and SQLite only")


def _check_sqlite_foreign_keys(connection) -> None:
    if connection.dialect.name != "sqlite":
        return
    failures = Counter(row[0] for row in connection.execute(sa.text("PRAGMA foreign_key_check")))
    if failures:
        raise RuntimeError(
            f"{CHILD_TABLE} migration refused existing foreign-key violations: "
            + ", ".join(f"{table}={count}" for table, count in sorted(failures.items()))
        )


def _preflight(connection) -> None:
    counts = connection.execute(
        sa.text(
            "SELECT count(*) FILTER (WHERE p.id IS NULL), "
            "count(*) FILTER (WHERE p.id IS NOT NULL AND c.org_id <> p.org_id) "
            f"FROM {CHILD_TABLE} c LEFT JOIN {PARENT_TABLE} p ON p.id = c.{LINK_COLUMN}"
        )
    ).one()
    if counts[0] or counts[1]:
        raise RuntimeError(
            f"{CHILD_TABLE} migration refused inconsistent history: "
            f"missing_parent={counts[0]}, wrong_tenant={counts[1]}"
        )


def _check_target(connection) -> None:
    for index in sa.inspect(connection).get_indexes(PARENT_TABLE):
        if (
            index["name"] == TARGET_INDEX
            and index["unique"]
            and index["column_names"] == ["org_id", "id"]
            and not any("where" in key for key in index.get("dialect_options", {}))
        ):
            return
    raise RuntimeError(f"Expected {PARENT_TABLE} pair index is absent or incompatible")


def _fk_name(connection, columns, referred_columns, expected_name=None) -> str:
    matching = [
        fk
        for fk in sa.inspect(connection).get_foreign_keys(CHILD_TABLE)
        if fk["constrained_columns"] == columns
        and fk["referred_table"] == PARENT_TABLE
        and fk["referred_columns"] == referred_columns
    ]
    if len(matching) != 1:
        raise RuntimeError(f"Expected {CHILD_TABLE} parent foreign key is absent or ambiguous")
    fk = matching[0]
    options = fk.get("options") or {}
    if (
        (options.get("ondelete") or "").upper() != "CASCADE"
        or (options.get("onupdate") or "NO ACTION").upper() != "NO ACTION"
        or options.get("deferrable", False)
        or (options.get("initially") or "IMMEDIATE").upper() != "IMMEDIATE"
        or (options.get("match") or "SIMPLE").upper() != "SIMPLE"
        or (expected_name is not None and fk["name"] != expected_name)
    ):
        raise RuntimeError(f"Unexpected {CHILD_TABLE} parent foreign-key contract")
    return fk["name"] or f"fk_{CHILD_TABLE}_{LINK_COLUMN}_{PARENT_TABLE}"


def _replace_fk(connection, old_name, new_name, columns, referred_columns) -> None:
    if connection.dialect.name == "sqlite":
        reflected = sa.Table(CHILD_TABLE, sa.MetaData(), autoload_with=connection)
        # Name only this unnamed parent FK; preserve every unrelated name.
        for constraint in reflected.foreign_key_constraints:
            if (
                constraint.name is None
                and LINK_COLUMN in constraint.columns
                and constraint.referred_table.name == PARENT_TABLE
            ):
                constraint.name = old_name
        with op.batch_alter_table(CHILD_TABLE, copy_from=reflected) as batch:
            batch.drop_constraint(old_name, type_="foreignkey")
            batch.create_foreign_key(
                new_name, PARENT_TABLE, columns, referred_columns, ondelete="CASCADE"
            )
    else:
        op.drop_constraint(old_name, CHILD_TABLE, type_="foreignkey")
        op.create_foreign_key(
            new_name, CHILD_TABLE, PARENT_TABLE, columns, referred_columns, ondelete="CASCADE"
        )


def upgrade() -> None:
    connection = op.get_bind()
    _prepare(connection)
    _check_sqlite_foreign_keys(connection)
    _preflight(connection)
    old_name = _fk_name(connection, [LINK_COLUMN], ["id"])
    with connection.begin_nested():
        op.create_index(TARGET_INDEX, PARENT_TABLE, ["org_id", "id"], unique=True)
        _replace_fk(connection, old_name, COMPOSITE_FK, ["org_id", LINK_COLUMN], ["org_id", "id"])
        _check_target(connection)
        _fk_name(connection, ["org_id", LINK_COLUMN], ["org_id", "id"], COMPOSITE_FK)
        _check_sqlite_foreign_keys(connection)


def downgrade() -> None:
    connection = op.get_bind()
    _prepare(connection)
    _check_sqlite_foreign_keys(connection)
    _preflight(connection)
    _check_target(connection)
    _fk_name(connection, ["org_id", LINK_COLUMN], ["org_id", "id"], COMPOSITE_FK)
    with connection.begin_nested():
        _replace_fk(connection, COMPOSITE_FK, OLD_FK, [LINK_COLUMN], ["id"])
        op.drop_index(TARGET_INDEX, table_name=PARENT_TABLE)
        _fk_name(connection, [LINK_COLUMN], ["id"], OLD_FK)
        _check_sqlite_foreign_keys(connection)
