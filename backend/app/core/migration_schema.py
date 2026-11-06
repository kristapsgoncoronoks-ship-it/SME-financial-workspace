"""Exact PostgreSQL comparison metadata for retained deletion contracts.

The original four allocation pairs use portable SET NULL; optional-parent
pairs use portable NO ACTION plus retained single-column SET NULL. PostgreSQL
targets only each nullable link, preserving the tenant key. Validate the exact
contracts and build a comparison copy; every other schema attribute remains
subject to ordinary Alembic comparison. No diff is filtered and neither shared
application metadata nor the database is modified.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import ForeignKeyConstraint, MetaData, Table, UniqueConstraint, inspect, text
from sqlalchemy.engine import Connection, Dialect

# These exact names and columns are the committed DB-017/018 migration policy.
POSTGRES_COLUMN_SET_NULL = {
    ("expense_items", "fk_expense_items_project"): "project_id",
    ("invoices", "fk_invoices_project"): "project_id",
    ("issued_invoices", "fk_issued_invoices_project"): "project_id",
    ("issued_invoices", "fk_issued_invoices_issuer"): "issuer_id",
}


@dataclass(frozen=True)
class OptionalParentLink:
    table: str
    column: str
    parent: str
    pair_name: str
    single_name: str
    parent_index: str
    target_kind: str = "index"


OPTIONAL_PARENT_LINKS = (
    OptionalParentLink(
        "expense_transactions",
        "item_id",
        "expense_items",
        "fk_expense_transactions_item",
        "expense_transactions_item_id_fkey",
        "uq_expense_items_org_id",
    ),
    OptionalParentLink(
        "issued_invoices",
        "partner_id",
        "partners",
        "fk_issued_invoices_partner",
        "fk_issued_partner",
        "uq_partners_org_id",
    ),
    OptionalParentLink(
        "recurring_invoices",
        "partner_id",
        "partners",
        "fk_recurring_invoices_partner",
        "recurring_invoices_partner_id_fkey",
        "uq_partners_org_id",
    ),
    OptionalParentLink(
        "vendor_change_requests",
        "source_document_id",
        "documents",
        "fk_vendor_change_requests_source_document",
        "vendor_change_requests_source_document_id_fkey",
        "uq_documents_org_id",
    ),
    OptionalParentLink(
        "inbound_invoices",
        "invoice_id",
        "invoices",
        "fk_inbound_invoices_invoice_tenant",
        "inbound_invoices_invoice_id_fkey",
        "uq_invoices_org_id",
        target_kind="unique_constraint",
    ),
    OptionalParentLink(
        "issued_invoices",
        "customer_id",
        "customers",
        "fk_issued_invoices_customer_tenant",
        "fk_issued_invoices_customer",
        "uq_customers_org_id",
        target_kind="unique_constraint",
    ),
    OptionalParentLink(
        "issued_invoices",
        "corrected_invoice_id",
        "issued_invoices",
        "fk_issued_invoices_corrected_tenant",
        "fk_issued_corrected",
        "uq_issued_invoices_org_id",
        target_kind="unique_constraint",
    ),
)


def metadata_table(metadata: MetaData, name: str) -> Table:
    key = f"{metadata.schema}.{name}" if metadata.schema else name
    return metadata.tables[key]


def _optional_error(spec: OptionalParentLink) -> RuntimeError:
    return RuntimeError(f"[DB-PG-OPTIONAL-01] {spec.table}.{spec.pair_name} contract changed")


def _source_policy(constraint: ForeignKeyConstraint, action: str | None) -> bool:
    expected = {
        "ondelete": action,
        "onupdate": None,
        "deferrable": None,
        "initially": None,
        "match": None,
        "use_alter": False,
        "link_to_name": False,
    }
    return not constraint.dialect_kwargs and all(
        getattr(obj, option) == value
        for obj in (constraint, *constraint.elements)
        for option, value in expected.items()
    )


def _optional_target(parent: Table, spec: OptionalParentLink) -> bool:
    """Validate the declared target kind without accepting a lookalike object."""
    indexes = [index for index in parent.indexes if index.name == spec.parent_index]
    constraints = [
        constraint
        for constraint in parent.constraints
        if isinstance(constraint, UniqueConstraint) and constraint.name == spec.parent_index
    ]
    columns = (parent.c.org_id, parent.c.id)

    def exact_columns(actual) -> bool:
        return len(actual) == 2 and all(
            found is expected for found, expected in zip(actual, columns, strict=True)
        )

    if spec.target_kind == "index":
        return (
            len(indexes) == 1
            and not constraints
            and indexes[0].table is parent
            and indexes[0].unique is True
            and exact_columns(tuple(indexes[0].columns))
            and exact_columns(tuple(indexes[0].expressions))
            and not indexes[0].dialect_kwargs
        )
    if spec.target_kind == "unique_constraint":
        return (
            len(constraints) == 1
            and not indexes
            and constraints[0].table is parent
            and exact_columns(tuple(constraints[0].columns))
            and constraints[0].deferrable is None
            and constraints[0].initially is None
            and not constraints[0].dialect_kwargs
        )
    return False


def validate_optional_parent_metadata(metadata: MetaData) -> None:
    """Validate original pairs and singles before cloning can normalize options."""
    for spec in OPTIONAL_PARENT_LINKS:
        try:
            table = metadata_table(metadata, spec.table)
            parent = metadata_table(metadata, spec.parent)
            pairs = [
                fk
                for fk in table.foreign_key_constraints
                if set(fk.column_keys) == {"org_id", spec.column} or fk.name == spec.pair_name
            ]
            singles = [
                fk
                for fk in table.foreign_key_constraints
                if tuple(fk.column_keys) == (spec.column,)
            ]
            prefix = f"{parent.schema}." if parent.schema else ""
            valid = (
                len(pairs) == len(singles) == 1
                and pairs[0] in table.constraints
                and singles[0] in table.constraints
                and pairs[0].name == spec.pair_name
                and tuple(pairs[0].column_keys) == ("org_id", spec.column)
                and tuple(e.target_fullname for e in pairs[0].elements)
                == (f"{prefix}{spec.parent}.org_id", f"{prefix}{spec.parent}.id")
                and singles[0].name is None
                and tuple(e.target_fullname for e in singles[0].elements)
                == (f"{prefix}{spec.parent}.id",)
                and all(
                    element.column is parent.c[target] and element.parent is table.c[local]
                    for element, local, target in zip(
                        pairs[0].elements, ("org_id", spec.column), ("org_id", "id"), strict=True
                    )
                )
                and singles[0].elements[0].column is parent.c.id
                and singles[0].elements[0].parent is table.c[spec.column]
                and not table.c.org_id.nullable
                and table.c[spec.column].nullable
                and not parent.c.org_id.nullable
                and not parent.c.id.nullable
                and parent.c.id.primary_key
                and len(parent.primary_key.columns) == 1
                and next(iter(parent.primary_key.columns)) is parent.c.id
                and _optional_target(parent, spec)
                and _source_policy(pairs[0], None)
                and _source_policy(singles[0], "SET NULL")
            )
        except (KeyError, AttributeError):
            raise _optional_error(spec) from None
        if not valid:
            raise _optional_error(spec)


def _contact_error() -> RuntimeError:
    return RuntimeError(
        "[DB-PG-RESIDUAL-01] customer_contacts.fk_customer_contacts_customer contract changed"
    )


def validate_customer_contact_metadata(metadata: MetaData) -> None:
    """Pin the new mandatory pair when present; keep partial scratch metadata supported."""
    key = f"{metadata.schema}.customer_contacts" if metadata.schema else "customer_contacts"
    if key not in metadata.tables:
        return
    try:
        table, parent = metadata.tables[key], metadata_table(metadata, "customers")
        pairs = [
            fk
            for fk in table.foreign_key_constraints
            if fk.name == "fk_customer_contacts_customer"
            or set(fk.column_keys) == {"org_id", "customer_id"}
        ]
        singles = [
            fk for fk in table.foreign_key_constraints if tuple(fk.column_keys) == ("customer_id",)
        ]
        targets = [
            constraint
            for constraint in parent.constraints
            if isinstance(constraint, UniqueConstraint) and constraint.name == "uq_customers_org_id"
        ]
        prefix = f"{parent.schema}." if parent.schema else ""
        valid = (
            len(pairs) == len(targets) == 1
            and not singles
            and pairs[0] in table.constraints
            and pairs[0].name == "fk_customer_contacts_customer"
            and tuple(pairs[0].column_keys) == ("org_id", "customer_id")
            and tuple(element.target_fullname for element in pairs[0].elements)
            == (f"{prefix}customers.org_id", f"{prefix}customers.id")
            and all(
                element.parent is table.c[local] and element.column is parent.c[target]
                for element, local, target in zip(
                    pairs[0].elements, ("org_id", "customer_id"), ("org_id", "id"), strict=True
                )
            )
            and not table.c.org_id.nullable
            and not table.c.customer_id.nullable
            and not parent.c.org_id.nullable
            and not parent.c.id.nullable
            and parent.c.id.primary_key
            and len(parent.primary_key.columns) == 1
            and next(iter(parent.primary_key.columns)) is parent.c.id
            and targets[0].table is parent
            and len(targets[0].columns) == 2
            and all(
                actual is expected
                for actual, expected in zip(
                    targets[0].columns, (parent.c.org_id, parent.c.id), strict=True
                )
            )
            and targets[0].deferrable is None
            and targets[0].initially is None
            and not targets[0].dialect_kwargs
            and _source_policy(pairs[0], "CASCADE")
        )
    except (KeyError, AttributeError):
        raise _contact_error() from None
    if not valid:
        raise _contact_error()


def validate_installed_customer_contact(connection: Connection, metadata: MetaData) -> None:
    """Read validation flags that ordinary FK reflection does not expose."""
    key = f"{metadata.schema}.customer_contacts" if metadata.schema else "customer_contacts"
    if key not in metadata.tables:
        return
    schema = metadata.schema or inspect(connection).default_schema_name
    if schema is None:
        raise _contact_error()
    quote = connection.dialect.identifier_preparer
    child = f"{quote.quote_schema(schema)}.{quote.quote('customer_contacts')}"
    parent = f"{quote.quote_schema(schema)}.{quote.quote('customers')}"
    keys = list(
        connection.execute(
            text("""
            SELECT c.conname,c.confrelid=to_regclass(:parent) AS correct_parent,
                c.confdeltype::text AS delete_action,c.confupdtype::text AS update_action,
                c.confmatchtype::text AS match_type,c.convalidated,c.condeferrable,c.condeferred,
                c.confdelsetcols::int[] AS set_attnums,
                ARRAY(SELECT a.attname::text FROM unnest(c.conkey) WITH ORDINALITY n(num,pos)
                    JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=n.num ORDER BY n.pos) AS local_columns,
                ARRAY(SELECT a.attname::text FROM unnest(c.confkey) WITH ORDINALITY n(num,pos)
                    JOIN pg_attribute a ON a.attrelid=c.confrelid AND a.attnum=n.num ORDER BY n.pos) AS target_columns
            FROM pg_constraint c WHERE c.conrelid=to_regclass(:child) AND c.contype='f'
            """),
            {"child": child, "parent": parent},
        ).mappings()
    )
    pairs = [
        row
        for row in keys
        if row["conname"] == "fk_customer_contacts_customer"
        or set(row["local_columns"]) == {"org_id", "customer_id"}
    ]
    if len(pairs) != 1 or any(row["local_columns"] == ["customer_id"] for row in keys):
        raise _contact_error()
    pair = pairs[0]
    if not (
        pair["conname"] == "fk_customer_contacts_customer"
        and pair["correct_parent"]
        and pair["local_columns"] == ["org_id", "customer_id"]
        and pair["target_columns"] == ["org_id", "id"]
        and pair["delete_action"] == "c"
        and pair["update_action"] == "a"
        and pair["match_type"] == "s"
        and pair["convalidated"]
        and not pair["condeferrable"]
        and not pair["condeferred"]
        and pair["set_attnums"] is None
    ):
        raise _contact_error()


def project_optional_parent_metadata(metadata: MetaData, dialect: Dialect) -> None:
    """Modify an already validated PG clone only; portable objects stay intact."""
    for spec in OPTIONAL_PARENT_LINKS:
        table = metadata_table(metadata, spec.table)
        pair = next(fk for fk in table.foreign_key_constraints if fk.name == spec.pair_name)
        pair.ondelete = f"SET NULL ({dialect.identifier_preparer.quote(spec.column)})"
        for element in pair.elements:
            element.ondelete = pair.ondelete
        if spec.table == "issued_invoices":
            # The historical issuing migrations use explicit physical names. Align
            # the development/comparison clone, without renaming Base/SQLite.
            single = next(
                fk
                for fk in table.foreign_key_constraints
                if tuple(fk.column_keys) == (spec.column,)
            )
            single.name = spec.single_name


def validate_installed_optional_parents(connection: Connection, metadata: MetaData) -> None:
    """Pin exact native pair/single contracts, not just inspector action text."""
    schema = metadata.schema or inspect(connection).default_schema_name
    if schema is None:
        raise RuntimeError("[DB-PG-OPTIONAL-01] Cannot resolve the comparison schema")
    quote = connection.dialect.identifier_preparer
    for spec in OPTIONAL_PARENT_LINKS:
        child = f"{quote.quote_schema(schema)}.{quote.quote(spec.table)}"
        parent = f"{quote.quote_schema(schema)}.{quote.quote(spec.parent)}"
        child_oid, parent_oid = connection.execute(
            text("SELECT to_regclass(:child)::oid::bigint,to_regclass(:parent)::oid::bigint"),
            {"child": child, "parent": parent},
        ).one()
        if child_oid is None or parent_oid is None:
            raise _optional_error(spec)
        keys = list(
            connection.execute(
                text("""
            SELECT c.conname,c.conrelid::bigint AS child_oid,c.confrelid::bigint AS parent_oid,
                c.confdeltype::text AS delete_action,c.confupdtype::text AS update_action,
                c.confmatchtype::text AS match_type,c.convalidated,c.condeferrable,c.condeferred,
                ARRAY(SELECT a.attname::text FROM unnest(c.conkey) WITH ORDINALITY n(num,pos)
                    JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=n.num ORDER BY n.pos) AS local_columns,
                ARRAY(SELECT a.attname::text FROM unnest(c.confkey) WITH ORDINALITY n(num,pos)
                    JOIN pg_attribute a ON a.attrelid=c.confrelid AND a.attnum=n.num ORDER BY n.pos) AS target_columns,
                c.confdelsetcols::int[] AS set_attnums,
                (SELECT a.attnum FROM pg_attribute a WHERE a.attrelid=c.conrelid AND a.attname=:column AND NOT a.attisdropped) AS optional_attnum
            FROM pg_constraint c WHERE c.conrelid=to_regclass(:child) AND c.contype='f'
        """),
                {"child": child, "column": spec.column},
            ).mappings()
        )
        pairs = [
            key
            for key in keys
            if key["conname"] == spec.pair_name
            or set(key["local_columns"]) == {"org_id", spec.column}
        ]
        singles = [key for key in keys if key["local_columns"] == [spec.column]]
        if len(pairs) != 1 or len(singles) != 1:
            raise _optional_error(spec)
        for key, name, local, target, set_columns in (
            (
                pairs[0],
                spec.pair_name,
                ["org_id", spec.column],
                ["org_id", "id"],
                [pairs[0]["optional_attnum"]],
            ),
            (singles[0], spec.single_name, [spec.column], ["id"], None),
        ):
            if not (
                key["conname"] == name
                and key["child_oid"] == child_oid
                and key["parent_oid"] == parent_oid
                and key["local_columns"] == local
                and key["target_columns"] == target
                and key["delete_action"] == "n"
                and key["update_action"] == "a"
                and key["match_type"] == "s"
                and key["convalidated"]
                and not key["condeferrable"]
                and not key["condeferred"]
                and key["set_attnums"] == set_columns
            ):
                raise _optional_error(spec)
        indexes = list(
            connection.execute(
                text("""
            SELECT i.indisunique,i.indisvalid,i.indisready,i.indimmediate,i.indnkeyatts,i.indnatts,i.indnullsnotdistinct,
                i.indpred IS NULL AS unfiltered,i.indexprs IS NULL AS ordinary,
                owner.conname AS target_constraint_name,owner.contype::text AS target_constraint_type,
                owner.convalidated AS target_validated,owner.condeferrable AS target_deferrable,
                owner.condeferred AS target_deferred,
                (SELECT a.attnotnull FROM pg_attribute a WHERE a.attrelid=i.indrelid
                    AND a.attname='org_id' AND NOT a.attisdropped) AS parent_org_not_null,
                (SELECT a.attnotnull FROM pg_attribute a WHERE a.attrelid=i.indrelid
                    AND a.attname='id' AND NOT a.attisdropped) AS parent_id_not_null,
                ARRAY(SELECT a.attname::text FROM pg_constraint pk
                    CROSS JOIN LATERAL unnest(pk.conkey) WITH ORDINALITY n(num,pos)
                    JOIN pg_attribute a ON a.attrelid=pk.conrelid AND a.attnum=n.num
                    WHERE pk.conrelid=i.indrelid AND pk.contype='p' ORDER BY n.pos) AS parent_primary_key,
                ARRAY(SELECT a.attname::text FROM unnest(owner.conkey) WITH ORDINALITY n(num,pos)
                    JOIN pg_attribute a ON a.attrelid=owner.conrelid AND a.attnum=n.num ORDER BY n.pos) AS constraint_columns,
                ARRAY(SELECT a.attname::text FROM unnest(i.indkey) WITH ORDINALITY n(num,pos)
                    JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=n.num ORDER BY n.pos) AS columns
            FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid
            LEFT JOIN pg_constraint owner ON owner.conindid=i.indexrelid
                AND owner.conrelid=i.indrelid AND owner.contype IN ('p','u')
            WHERE i.indrelid=to_regclass(:parent) AND c.relname=:name
        """),
                {"parent": parent, "name": spec.parent_index},
            ).mappings()
        )
        if len(indexes) != 1 or not (
            indexes[0]["indisunique"]
            and indexes[0]["indisvalid"]
            and indexes[0]["indisready"]
            and indexes[0]["indimmediate"]
            and indexes[0]["indnkeyatts"] == indexes[0]["indnatts"] == 2
            and indexes[0]["unfiltered"]
            and indexes[0]["ordinary"]
            and not indexes[0]["indnullsnotdistinct"]
            and indexes[0]["parent_org_not_null"]
            and indexes[0]["parent_id_not_null"]
            and indexes[0]["parent_primary_key"] == ["id"]
            and indexes[0]["columns"] == ["org_id", "id"]
            and (
                spec.target_kind == "index"
                and indexes[0]["target_constraint_name"] is None
                or spec.target_kind == "unique_constraint"
                and indexes[0]["target_constraint_name"] == spec.parent_index
                and indexes[0]["target_constraint_type"] == "u"
                and indexes[0]["target_validated"]
                and not indexes[0]["target_deferrable"]
                and not indexes[0]["target_deferred"]
                and indexes[0]["constraint_columns"] == ["org_id", "id"]
            )
        ):
            raise _optional_error(spec)


def postgresql_comparison_metadata(connection: Connection, metadata: MetaData) -> MetaData:
    """Return a checked comparison-only copy; refuse any actual policy drift."""
    if connection.dialect.name != "postgresql":
        raise ValueError("Column-specific SET NULL comparison requires PostgreSQL")
    inspector = inspect(connection)
    for (table, name), column in POSTGRES_COLUMN_SET_NULL.items():
        expected = f"SET NULL ({column})"
        reflected = [
            fk
            for fk in inspector.get_foreign_keys(table, schema=metadata.schema)
            if fk["name"] == name
        ]
        if len(reflected) != 1 or reflected[0].get("options", {}).get("ondelete") != expected:
            raise RuntimeError(f"[DB-PG-PARITY-01] {table}.{name} must use {expected}")
        constraints = [
            fk for fk in metadata_table(metadata, table).foreign_key_constraints if fk.name == name
        ]
        if (
            len(constraints) != 1
            or constraints[0].ondelete != "SET NULL"
            or any(element.ondelete != "SET NULL" for element in constraints[0].elements)
        ):
            raise RuntimeError(f"[DB-PG-PARITY-01] {table}.{name} portable ORM contract changed")
    # Validate the original objects before cloning: Table.to_metadata can
    # normalize inconsistent ForeignKey element options from their constraint.
    validate_optional_parent_metadata(metadata)
    validate_customer_contact_metadata(metadata)
    validate_installed_optional_parents(connection, metadata)
    validate_installed_customer_contact(connection, metadata)
    comparison = MetaData(schema=metadata.schema, naming_convention=metadata.naming_convention)
    for model_table in metadata.sorted_tables:
        model_table.to_metadata(comparison)
    for (table, name), column in POSTGRES_COLUMN_SET_NULL.items():
        copied_constraints = [
            fk
            for fk in metadata_table(comparison, table).foreign_key_constraints
            if fk.name == name
        ]
        if len(copied_constraints) != 1:
            raise RuntimeError(f"[DB-PG-PARITY-01] {table}.{name} comparison clone changed")
        constraint = copied_constraints[0]
        expected = f"SET NULL ({column})"
        constraint.ondelete = expected
        for foreign_key in constraint.elements:
            foreign_key.ondelete = expected
    project_optional_parent_metadata(comparison, connection.dialect)
    return comparison
