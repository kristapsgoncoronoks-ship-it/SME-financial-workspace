"""Complete four residual tenant-parent links without changing legal deletion.

The mandatory contact link remains CASCADE. Three optional references retain
their original single-column SET NULL constraints; their new tenant pairs use
portable NO ACTION and PostgreSQL SET NULL(optional_id). Existing named unique
constraints are reused, without parent/index DDL or historical data repair.

Use an explicit unscoped maintenance connection. SQLite additionally requires
foreign_keys=OFF before entry; this revision never toggles it. Real transaction
ownership, ordered write exclusion, masked global preflight and one savepoint
retain every row and unrelated schema object on a refusal or late DDL failure.
"""

from __future__ import annotations

from collections import Counter
from typing import NamedTuple

import sqlalchemy as sa

from alembic import op

revision = "e2f4a6b8d0c3"
down_revision = "d1e3f5a7c9b2"
branch_labels = None
depends_on = None


class Link(NamedTuple):
    table: str
    column: str
    parent: str
    pair_name: str
    single_name: str
    sqlite_single_name: str | None
    required: bool
    target_name: str


# Frozen contracts, independent of mutable application registries.
LINKS = (
    Link(
        "customer_contacts",
        "customer_id",
        "customers",
        "fk_customer_contacts_customer",
        "customer_contacts_customer_id_fkey",
        None,
        True,
        "uq_customers_org_id",
    ),
    Link(
        "inbound_invoices",
        "invoice_id",
        "invoices",
        "fk_inbound_invoices_invoice_tenant",
        "inbound_invoices_invoice_id_fkey",
        None,
        False,
        "uq_invoices_org_id",
    ),
    Link(
        "issued_invoices",
        "customer_id",
        "customers",
        "fk_issued_invoices_customer_tenant",
        "fk_issued_invoices_customer",
        "fk_issued_invoices_customer",
        False,
        "uq_customers_org_id",
    ),
    Link(
        "issued_invoices",
        "corrected_invoice_id",
        "issued_invoices",
        "fk_issued_invoices_corrected_tenant",
        "fk_issued_corrected",
        "fk_issued_corrected",
        False,
        "uq_issued_invoices_org_id",
    ),
)
TABLES = ("customer_contacts", "customers", "inbound_invoices", "invoices", "issued_invoices")


def _prepare(connection) -> None:
    if connection.dialect.name == "postgresql":
        if connection.scalar(sa.text("SELECT current_setting('app.current_org', true)")) not in (
            None,
            "",
        ):
            raise RuntimeError("Residual parent migration requires an unscoped connection")
        connection.execute(
            sa.text("LOCK TABLE " + ", ".join(TABLES) + " IN SHARE ROW EXCLUSIVE MODE")
        )
        for table in TABLES:
            if connection.execute(
                sa.text(
                    "SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid=to_regclass(:table)"
                ),
                {"table": table},
            ).one_or_none() != (True, True):
                raise RuntimeError("Residual parent migration requires existing FORCE RLS")
    elif connection.dialect.name == "sqlite":
        if connection.scalar(sa.text("PRAGMA foreign_keys")):
            raise RuntimeError(
                "Residual parent migration requires a dedicated SQLite foreign_keys=OFF connection"
            )
        in_transaction = getattr(connection.connection.driver_connection, "in_transaction", None)
        if not isinstance(in_transaction, bool):
            raise RuntimeError("Cannot establish SQLite migration transaction ownership")
        if not in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
    else:
        raise RuntimeError("Residual parent migration supports PostgreSQL and SQLite only")


def _check_sqlite_foreign_keys(connection) -> None:
    if connection.dialect.name != "sqlite":
        return
    failures = Counter(row[0] for row in connection.execute(sa.text("PRAGMA foreign_key_check")))
    if failures:
        raise RuntimeError(
            "Residual parent migration refused existing foreign-key violations: "
            + ", ".join(f"{table}={count}" for table, count in sorted(failures.items()))
        )


def _preflight(connection) -> None:
    failures = []
    for spec in LINKS:
        missing, wrong = connection.execute(
            sa.text(
                "SELECT count(*) FILTER (WHERE p.id IS NULL), "
                "count(*) FILTER (WHERE p.id IS NOT NULL AND c.org_id<>p.org_id) "
                f"FROM {spec.table} c LEFT JOIN {spec.parent} p ON p.id=c.{spec.column} "
                f"WHERE c.{spec.column} IS NOT NULL"
            )
        ).one()
        if missing or wrong:
            failures.append(
                f"{spec.table}.{spec.column}: missing_parent={missing}, wrong_tenant={wrong}"
            )
    if failures:
        raise RuntimeError(
            "Residual parent migration refused inconsistent history: " + "; ".join(failures)
        )


def _check_target(connection, spec) -> None:
    inspector = sa.inspect(connection)
    targets = [
        u for u in inspector.get_unique_constraints(spec.parent) if u["name"] == spec.target_name
    ]
    columns = {c["name"]: c for c in inspector.get_columns(spec.parent)}
    if not (
        len(targets) == 1
        and targets[0]["column_names"] == ["org_id", "id"]
        and targets[0].get("dialect_options", {})
        == (
            {"postgresql_include": [], "postgresql_nulls_not_distinct": False}
            if connection.dialect.name == "postgresql"
            else {}
        )
        and not columns["org_id"]["nullable"]
        and not columns["id"]["nullable"]
        and inspector.get_pk_constraint(spec.parent)["constrained_columns"] == ["id"]
    ):
        raise RuntimeError(
            f"Expected {spec.parent} named immediate unique constraint is absent or incompatible"
        )
    if connection.dialect.name == "postgresql":
        rows = list(
            connection.execute(
                sa.text("""
            SELECT c.convalidated,c.condeferrable,c.condeferred,
                i.indisunique,i.indisvalid,i.indisready,i.indimmediate,i.indnkeyatts,i.indnatts,i.indnullsnotdistinct,
                i.indpred IS NULL AS unfiltered,i.indexprs IS NULL AS ordinary,
                ARRAY(SELECT a.attname::text FROM unnest(c.conkey) WITH ORDINALITY n(num,pos)
                    JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=n.num ORDER BY n.pos) AS columns,
                ARRAY(SELECT a.attname::text FROM unnest(i.indkey) WITH ORDINALITY n(num,pos)
                    JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=n.num ORDER BY n.pos) AS index_columns
            FROM pg_constraint c JOIN pg_index i ON i.indexrelid=c.conindid
                AND i.indrelid=c.conrelid
            JOIN pg_class backing ON backing.oid=i.indexrelid
            WHERE c.conrelid=to_regclass(:parent) AND c.conname=:name
                AND backing.relname=:name AND c.contype='u'
        """),
                {"parent": spec.parent, "name": spec.target_name},
            ).mappings()
        )
        if len(rows) != 1 or not (
            rows[0]["convalidated"]
            and not rows[0]["condeferrable"]
            and not rows[0]["condeferred"]
            and all(
                rows[0][key]
                for key in (
                    "indisunique",
                    "indisvalid",
                    "indisready",
                    "indimmediate",
                    "unfiltered",
                    "ordinary",
                )
            )
            and rows[0]["indnkeyatts"] == rows[0]["indnatts"] == 2
            and not rows[0]["indnullsnotdistinct"]
            and rows[0]["columns"] == rows[0]["index_columns"] == ["org_id", "id"]
        ):
            raise RuntimeError(
                f"Expected {spec.parent} native unique target is absent or incompatible"
            )


def _native_fk(connection, spec, name, *, pair) -> None:
    if connection.dialect.name != "postgresql":
        return
    rows = list(
        connection.execute(
            sa.text("""
        SELECT c.confrelid=to_regclass(:parent) AS correct_parent,
            c.confdeltype::text AS delete_action,c.confupdtype::text AS update_action,
            c.confmatchtype::text AS match_type,c.convalidated,c.condeferrable,c.condeferred,
            ARRAY(SELECT a.attname::text FROM unnest(c.conkey) WITH ORDINALITY n(num,pos)
                JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=n.num ORDER BY n.pos) AS local_columns,
            ARRAY(SELECT a.attname::text FROM unnest(c.confkey) WITH ORDINALITY n(num,pos)
                JOIN pg_attribute a ON a.attrelid=c.confrelid AND a.attnum=n.num ORDER BY n.pos) AS target_columns,
            c.confdelsetcols::int[] AS set_attnums,
            (SELECT a.attnum FROM pg_attribute a WHERE a.attrelid=c.conrelid AND a.attname=:column AND NOT a.attisdropped) AS optional_attnum
        FROM pg_constraint c WHERE c.conrelid=to_regclass(:table) AND c.conname=:name AND c.contype='f'
    """),
            {"table": spec.table, "parent": spec.parent, "column": spec.column, "name": name},
        ).mappings()
    )
    if len(rows) != 1 or not (
        rows[0]["correct_parent"]
        and rows[0]["delete_action"] == ("c" if spec.required else "n")
        and rows[0]["update_action"] == "a"
        and rows[0]["match_type"] == "s"
        and rows[0]["convalidated"]
        and not rows[0]["condeferrable"]
        and not rows[0]["condeferred"]
        and rows[0]["local_columns"] == (["org_id", spec.column] if pair else [spec.column])
        and rows[0]["target_columns"] == (["org_id", "id"] if pair else ["id"])
        and rows[0]["set_attnums"]
        == ([rows[0]["optional_attnum"]] if pair and not spec.required else None)
    ):
        raise RuntimeError(f"Unexpected {spec.table}.{name} native foreign-key contract")


def _expected_fk(connection, spec, *, pair):
    pg = connection.dialect.name == "postgresql"
    name = spec.pair_name if pair else spec.single_name if pg else spec.sqlite_single_name
    action = (
        "CASCADE" if spec.required else f"SET NULL ({spec.column})" if pg and pair else "SET NULL"
    )
    expected = {
        "name": name,
        "constrained_columns": ["org_id", spec.column] if pair else [spec.column],
        "referred_schema": None,
        "referred_table": spec.parent,
        "referred_columns": ["org_id", "id"] if pair else ["id"],
        "options": {} if pair and not spec.required and not pg else {"ondelete": action},
    }
    if pg:
        expected["comment"] = None
    return expected


def _check_installed(connection, *, upgrade) -> None:
    inspector = sa.inspect(connection)
    for spec in LINKS:
        _check_target(connection, spec)
        columns = {c["name"]: c for c in inspector.get_columns(spec.table)}
        if columns["org_id"]["nullable"] or columns[spec.column]["nullable"] == spec.required:
            raise RuntimeError(f"Unexpected {spec.table}.{spec.column} nullability")
        keys = inspector.get_foreign_keys(spec.table)
        singles = [fk for fk in keys if fk["constrained_columns"] == [spec.column]]
        pairs = [
            fk
            for fk in keys
            if fk["name"] == spec.pair_name
            or set(fk["constrained_columns"]) == {"org_id", spec.column}
        ]
        if not upgrade or not spec.required:
            if singles != [_expected_fk(connection, spec, pair=False)]:
                raise RuntimeError(
                    f"Unexpected {spec.table}.{spec.column} retained single foreign-key contract"
                )
            _native_fk(connection, spec, spec.single_name, pair=False)
        elif singles:
            raise RuntimeError(
                f"Unexpected {spec.table}.{spec.column} redundant mandatory single foreign key"
            )
        if upgrade:
            if pairs != [_expected_fk(connection, spec, pair=True)]:
                raise RuntimeError(f"Unexpected {spec.table}.{spec.pair_name} tenant pair contract")
            _native_fk(connection, spec, spec.pair_name, pair=True)
        elif pairs:
            raise RuntimeError(
                f"Unexpected existing {spec.table}.{spec.pair_name} guard; no adoption performed"
            )


def _replace_fk(connection, spec, *, upgrade) -> None:
    if connection.dialect.name == "sqlite":
        reflected = sa.Table(spec.table, sa.MetaData(), autoload_with=connection)
        if spec.required and not upgrade:
            # Rebuild with the actual original unnamed single, rather than
            # inventing a physical name during rollback. Keep every other
            # reflected constraint and all column/default/index attributes.
            pair = next(fk for fk in reflected.foreign_key_constraints if fk.name == spec.pair_name)
            reflected.constraints.remove(pair)
            for element in pair.elements:
                reflected.foreign_keys.remove(element)
                element.parent.foreign_keys.remove(element)
            reflected.append_constraint(
                sa.ForeignKeyConstraint(
                    [spec.column],
                    [f"{spec.parent}.id"],
                    name=spec.sqlite_single_name,
                    ondelete="CASCADE",
                )
            )
            with op.batch_alter_table(spec.table, copy_from=reflected, recreate="always"):
                pass
        else:
            if spec.required:
                single = next(
                    fk
                    for fk in reflected.foreign_key_constraints
                    if tuple(fk.column_keys) == (spec.column,)
                )
                single.name = spec.single_name
            with op.batch_alter_table(spec.table, copy_from=reflected) as batch:
                if upgrade:
                    if spec.required:
                        batch.drop_constraint(spec.single_name, type_="foreignkey")
                    batch.create_foreign_key(
                        spec.pair_name,
                        spec.parent,
                        ["org_id", spec.column],
                        ["org_id", "id"],
                        ondelete="CASCADE" if spec.required else None,
                    )
                else:
                    batch.drop_constraint(spec.pair_name, type_="foreignkey")
    elif upgrade:
        if spec.required:
            op.drop_constraint(spec.single_name, spec.table, type_="foreignkey")
        op.create_foreign_key(
            spec.pair_name,
            spec.table,
            spec.parent,
            ["org_id", spec.column],
            ["org_id", "id"],
            ondelete="CASCADE" if spec.required else f"SET NULL ({spec.column})",
        )
    else:
        op.drop_constraint(spec.pair_name, spec.table, type_="foreignkey")
        if spec.required:
            op.create_foreign_key(
                spec.single_name, spec.table, spec.parent, [spec.column], ["id"], ondelete="CASCADE"
            )


def _migrate(*, upgrade) -> None:
    connection = op.get_bind()
    _prepare(connection)
    # No GUC is set or cleared. A scoped caller is refused before locks/DDL;
    # None and empty maintenance scopes remain exact on success and failure.
    with connection.begin_nested():
        _check_sqlite_foreign_keys(connection)
        _preflight(connection)
        _check_installed(connection, upgrade=not upgrade)
        for spec in LINKS:
            _replace_fk(connection, spec, upgrade=upgrade)
        _check_installed(connection, upgrade=upgrade)
        _check_sqlite_foreign_keys(connection)


def upgrade() -> None:
    _migrate(upgrade=True)


def downgrade() -> None:
    _migrate(upgrade=False)
