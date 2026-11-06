"""Portable development bootstrap with the existing tenant-safe PostgreSQL DDL.

This builds metadata for new development tables only. Alembic still owns
production schemas and its separate comparison helper verifies installed FKs.
No existing constraint is altered or inferred from an inspected database.
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.engine import Dialect

from app.core.migration_schema import (
    POSTGRES_COLUMN_SET_NULL,
    project_optional_parent_metadata,
    validate_customer_contact_metadata,
    validate_optional_parent_metadata,
)


def development_metadata(metadata: MetaData, dialect: Dialect) -> MetaData:
    """Copy PostgreSQL DDL after validating original policies; keep SQLite portable."""
    if dialect.name != "postgresql":
        return metadata

    source_keys = {}
    for (table, name), column in POSTGRES_COLUMN_SET_NULL.items():
        key = f"{metadata.schema}.{table}" if metadata.schema else table
        source_table = metadata.tables.get(key)
        if source_table is None:
            raise RuntimeError(f"[DB-PG-BOOTSTRAP-01] {table}.{name} contract missing or ambiguous")
        constraints = [fk for fk in source_table.foreign_key_constraints if fk.name == name]
        if len(constraints) != 1:
            raise RuntimeError(f"[DB-PG-BOOTSTRAP-01] {table}.{name} contract missing or ambiguous")
        constraint = constraints[0]
        parent = "issuer_profiles" if column == "issuer_id" else "projects"
        schema = f"{source_table.schema}." if source_table.schema else ""
        if (
            tuple(constraint.column_keys) != ("org_id", column)
            or tuple(element.target_fullname for element in constraint.elements)
            != (f"{schema}{parent}.org_id", f"{schema}{parent}.id")
            or constraint.columns["org_id"].nullable
            or not constraint.columns[column].nullable
            or constraint.ondelete != "SET NULL"
            or any(
                getattr(element, option) != getattr(constraint, option)
                for element in constraint.elements
                for option in (
                    "ondelete",
                    "onupdate",
                    "deferrable",
                    "initially",
                    "match",
                    "use_alter",
                    "link_to_name",
                )
            )
        ):
            raise RuntimeError(f"[DB-PG-BOOTSTRAP-01] {table}.{name} portable ORM contract changed")
        source_keys[table, name] = key

    validate_optional_parent_metadata(metadata)
    validate_customer_contact_metadata(metadata)
    # Table.to_metadata can normalize inconsistent element options. Validate the
    # originals above first, rather than trusting a normalized clone.
    projected = MetaData(schema=metadata.schema, naming_convention=metadata.naming_convention)
    for model_table in metadata.sorted_tables:
        model_table.to_metadata(projected)
    for (table, name), column in POSTGRES_COLUMN_SET_NULL.items():
        copied = next(
            fk
            for fk in projected.tables[source_keys[table, name]].foreign_key_constraints
            if fk.name == name
        )
        copied.ondelete = f"SET NULL ({dialect.identifier_preparer.quote(column)})"
        for element in copied.elements:
            element.ondelete = copied.ondelete
    project_optional_parent_metadata(projected, dialect)
    return projected
