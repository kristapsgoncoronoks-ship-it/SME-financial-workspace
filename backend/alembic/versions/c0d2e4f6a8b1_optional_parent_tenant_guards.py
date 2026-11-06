"""Guard four optional parents without changing their detach actions.

PREFLIGHT: exclude parent/child writes and refuse globally inconsistent history
with masked counts, retaining FORCE RLS and the existing empty-org scope.
POSTCONDITION: immediate tenant pairs; old single SET NULL FKs are untouched.
PostgreSQL pairs target SET NULL(optional_id); SQLite pairs use NO ACTION and
the retained singles detach only optional_id. Parent unique indexes avoid parent
rebuilds. SQLite requires a dedicated FK-off migration connection and checks all
existing FKs; all DDL/postvalidation share a real transaction/savepoint.
ROLLBACK: drop only these four new guards and two new indexes, preserving every
historical row, old FK name/action and unrelated constraint/index.
"""

from __future__ import annotations

from collections import Counter

import sqlalchemy as sa

from alembic import op

revision = "c0d2e4f6a8b1"
down_revision = "b9c1e3f5a7d0"
branch_labels = None
depends_on = None

# Frozen revision contracts; do not import mutable application registries.
# table, optional column, parent, new pair, original PG name, original SQLite name
LINKS = (
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
)
NEW_TARGETS = ("expense_items", "documents")


def _prepare(connection) -> None:
    if connection.dialect.name == "postgresql":
        if connection.scalar(sa.text("SELECT current_setting('app.current_org', true)")) not in (
            None,
            "",
        ):
            raise RuntimeError("Optional parent migration requires an unscoped connection")
        connection.execute(
            sa.text(
                "LOCK TABLE documents, expense_items, partners, expense_transactions, "
                "issued_invoices, recurring_invoices, vendor_change_requests "
                "IN SHARE ROW EXCLUSIVE MODE"
            )
        )
    elif connection.dialect.name == "sqlite":
        if connection.scalar(sa.text("PRAGMA foreign_keys")):
            raise RuntimeError(
                "Optional parent migration requires a dedicated SQLite foreign_keys=OFF connection"
            )
        in_transaction = getattr(connection.connection.driver_connection, "in_transaction", None)
        if not isinstance(in_transaction, bool):
            raise RuntimeError("Cannot establish SQLite migration transaction ownership")
        if not in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
    else:
        raise RuntimeError("Optional parent migration supports PostgreSQL and SQLite only")


def _check_sqlite_foreign_keys(connection) -> None:
    if connection.dialect.name != "sqlite":
        return
    failures = Counter(row[0] for row in connection.execute(sa.text("PRAGMA foreign_key_check")))
    if failures:
        raise RuntimeError(
            "Optional parent migration refused existing foreign-key violations: "
            + ", ".join(f"{table}={count}" for table, count in sorted(failures.items()))
        )


def _preflight(connection) -> None:
    failures = []
    for table, column, parent, *_ in LINKS:
        missing, wrong = connection.execute(
            sa.text(
                "SELECT count(*) FILTER (WHERE p.id IS NULL), "
                "count(*) FILTER (WHERE p.id IS NOT NULL AND c.org_id<>p.org_id) "
                f"FROM {table} c LEFT JOIN {parent} p ON p.id=c.{column} WHERE c.{column} IS NOT NULL"
            )
        ).one()
        if missing or wrong:
            failures.append(f"{table}: missing_parent={missing}, wrong_tenant={wrong}")
    if failures:
        raise RuntimeError(
            "Optional parent migration refused inconsistent history: " + "; ".join(failures)
        )


def _check_target(connection, parent) -> None:
    name = f"uq_{parent}_org_id"
    indexes = [
        index for index in sa.inspect(connection).get_indexes(parent) if index["name"] == name
    ]
    if len(indexes) != 1 or not (
        indexes[0]["unique"]
        and indexes[0]["column_names"] == ["org_id", "id"]
        and not any(
            "where" in key or "include" in key and value
            for key, value in indexes[0].get("dialect_options", {}).items()
        )
    ):
        raise RuntimeError(f"Expected {parent} immediate full pair index is absent or incompatible")
    if connection.dialect.name == "postgresql":
        valid = connection.scalar(
            sa.text(
                "SELECT i.indisunique AND i.indisvalid AND i.indisready AND i.indimmediate "
                "AND i.indpred IS NULL AND i.indexprs IS NULL AND i.indnkeyatts=2 AND i.indnatts=2 "
                "FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
                "WHERE i.indrelid=CAST(:parent AS regclass) AND c.relname=:name"
            ),
            {"parent": parent, "name": name},
        )
        if not valid:
            raise RuntimeError(
                f"Expected {parent} immediate full pair index is absent or incompatible"
            )


def _native_fk(connection, table, column, parent, name, *, pair) -> None:
    if connection.dialect.name != "postgresql":
        return
    rows = list(
        connection.execute(
            sa.text("""
        SELECT c.confrelid=CAST(:parent AS regclass) AS correct_parent,
            c.confdeltype::text AS delete_action,c.confupdtype::text AS update_action,
            c.confmatchtype::text AS match_type,c.convalidated,c.condeferrable,c.condeferred,
            ARRAY(SELECT a.attname::text FROM unnest(c.conkey) WITH ORDINALITY n(num,pos)
                JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=n.num ORDER BY n.pos) AS local_columns,
            ARRAY(SELECT a.attname::text FROM unnest(c.confkey) WITH ORDINALITY n(num,pos)
                JOIN pg_attribute a ON a.attrelid=c.confrelid AND a.attnum=n.num ORDER BY n.pos) AS target_columns,
            c.confdelsetcols::int[] AS set_attnums,
            (SELECT a.attnum FROM pg_attribute a WHERE a.attrelid=c.conrelid AND a.attname=:column AND NOT a.attisdropped) AS optional_attnum
        FROM pg_constraint c WHERE c.conrelid=CAST(:table AS regclass) AND c.conname=:name AND c.contype='f'
    """),
            {"table": table, "parent": parent, "column": column, "name": name},
        ).mappings()
    )
    if len(rows) != 1:
        raise RuntimeError(f"Unexpected {table}.{name} native foreign-key contract")
    row = rows[0]
    if not (
        row["correct_parent"]
        and row["delete_action"] == "n"
        and row["update_action"] == "a"
        and row["match_type"] == "s"
        and row["convalidated"]
        and not row["condeferrable"]
        and not row["condeferred"]
        and row["local_columns"] == (["org_id", column] if pair else [column])
        and row["target_columns"] == (["org_id", "id"] if pair else ["id"])
        and row["set_attnums"] == ([row["optional_attnum"]] if pair else None)
    ):
        raise RuntimeError(f"Unexpected {table}.{name} native foreign-key contract")


def _singles(connection):
    result = {}
    for table, column, parent, _guard, pg_name, sqlite_name in LINKS:
        matching = [
            fk
            for fk in sa.inspect(connection).get_foreign_keys(table)
            if fk["constrained_columns"] == [column]
        ]
        expected_name = pg_name if connection.dialect.name == "postgresql" else sqlite_name
        if len(matching) != 1 or not (
            matching[0]["name"] == expected_name
            and matching[0]["referred_table"] == parent
            and matching[0]["referred_columns"] == ["id"]
            and matching[0]["referred_schema"] is None
            and matching[0].get("options", {}) == {"ondelete": "SET NULL"}
        ):
            raise RuntimeError(f"Unexpected {table} retained single foreign-key contract")
        _native_fk(connection, table, column, parent, pg_name, pair=False)
        result[table] = matching[0]
    return result


def _pairs(connection, *, present) -> None:
    for table, column, parent, name, *_ in LINKS:
        matches = [
            fk
            for fk in sa.inspect(connection).get_foreign_keys(table)
            if fk["name"] == name or set(fk["constrained_columns"]) == {"org_id", column}
        ]
        if not present:
            if matches:
                raise RuntimeError(f"Unexpected existing {table}.{name} guard")
            continue
        action = (
            {"ondelete": f"SET NULL ({column})"} if connection.dialect.name == "postgresql" else {}
        )
        if len(matches) != 1 or not (
            matches[0]["name"] == name
            and matches[0]["constrained_columns"] == ["org_id", column]
            and matches[0]["referred_table"] == parent
            and matches[0]["referred_columns"] == ["org_id", "id"]
            and matches[0]["referred_schema"] is None
            and matches[0].get("options", {}) == action
        ):
            raise RuntimeError(f"Unexpected {table}.{name} pair foreign-key contract")
        _native_fk(connection, table, column, parent, name, pair=True)


def _change_pair(connection, link, *, create) -> None:
    table, column, parent, name, *_ = link
    if connection.dialect.name == "sqlite":
        # Preserve every original named/unnamed constraint; only append/drop the
        # new named pair. FK-OFF prevents rebuilds cascading into incoming rows.
        reflected = sa.Table(table, sa.MetaData(), autoload_with=connection)
        with op.batch_alter_table(table, copy_from=reflected) as batch:
            if create:
                batch.create_foreign_key(name, parent, ["org_id", column], ["org_id", "id"])
            else:
                batch.drop_constraint(name, type_="foreignkey")
    elif create:
        op.create_foreign_key(
            name,
            table,
            parent,
            ["org_id", column],
            ["org_id", "id"],
            ondelete=f"SET NULL ({column})",
        )
    else:
        op.drop_constraint(name, table, type_="foreignkey")


def upgrade() -> None:
    connection = op.get_bind()
    _prepare(connection)
    _check_sqlite_foreign_keys(connection)
    _preflight(connection)
    originals = _singles(connection)
    _pairs(connection, present=False)
    _check_target(connection, "partners")
    for parent in NEW_TARGETS:
        if any(
            index["name"] == f"uq_{parent}_org_id"
            for index in sa.inspect(connection).get_indexes(parent)
        ):
            raise RuntimeError(f"Unexpected existing {parent} pair index; no adoption performed")
    with connection.begin_nested():
        for parent in NEW_TARGETS:
            op.create_index(f"uq_{parent}_org_id", parent, ["org_id", "id"], unique=True)
        for link in LINKS:
            _change_pair(connection, link, create=True)
        for parent in (*NEW_TARGETS, "partners"):
            _check_target(connection, parent)
        _pairs(connection, present=True)
        if _singles(connection) != originals:
            raise RuntimeError("Optional parent migration changed retained single foreign keys")
        _check_sqlite_foreign_keys(connection)


def downgrade() -> None:
    connection = op.get_bind()
    _prepare(connection)
    _check_sqlite_foreign_keys(connection)
    _preflight(connection)
    originals = _singles(connection)
    _pairs(connection, present=True)
    for parent in (*NEW_TARGETS, "partners"):
        _check_target(connection, parent)
    with connection.begin_nested():
        for link in LINKS:
            _change_pair(connection, link, create=False)
        for parent in reversed(NEW_TARGETS):
            op.drop_index(f"uq_{parent}_org_id", table_name=parent)
        _pairs(connection, present=False)
        _check_target(connection, "partners")
        if _singles(connection) != originals:
            raise RuntimeError("Optional parent downgrade changed retained single foreign keys")
        _check_sqlite_foreign_keys(connection)
