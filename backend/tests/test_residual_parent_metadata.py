"""Named UNIQUE targets are explicit contracts, not interchangeable indexes."""

from __future__ import annotations

import pytest
from sqlalchemy import Index, UniqueConstraint
from sqlalchemy.dialects import postgresql, sqlite

from app.core.development_schema import development_metadata
from tests.test_development_schema import alternate_metadata, ddl_snapshot, named_fk

LINKS = (
    (
        "inbound_invoices",
        "invoice_id",
        "invoices",
        "uq_invoices_org_id",
        "fk_inbound_invoices_invoice_tenant",
    ),
    (
        "issued_invoices",
        "customer_id",
        "customers",
        "uq_customers_org_id",
        "fk_issued_invoices_customer_tenant",
    ),
    (
        "issued_invoices",
        "corrected_invoice_id",
        "issued_invoices",
        "uq_issued_invoices_org_id",
        "fk_issued_invoices_corrected_tenant",
    ),
)


@pytest.mark.parametrize("table,column,parent,target_name,pair_name", LINKS)
@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "renamed",
        "reversed",
        "lookalike_index",
        "duplicate_name",
        "deferrable",
        "initially",
        "nulls_not_distinct",
        "include",
    ],
)
def test_residual_named_unique_target_drift_is_refused_without_normalization(
    table, column, parent, target_name, pair_name, mutation
):
    metadata = alternate_metadata()
    target_table = metadata.tables[parent]
    target = next(c for c in target_table.constraints if c.name == target_name)
    if mutation == "missing":
        target_table.constraints.remove(target)
    elif mutation == "renamed":
        target.name = "unexpected_unique_target"
    elif mutation == "reversed":
        target_table.constraints.remove(target)
        target_table.append_constraint(UniqueConstraint("id", "org_id", name=target_name))
    elif mutation == "lookalike_index":
        target_table.constraints.remove(target)
        Index(target_name, target_table.c.org_id, target_table.c.id, unique=True)
    elif mutation == "duplicate_name":
        target_table.append_constraint(UniqueConstraint("org_id", "id", name=target_name))
    elif mutation == "deferrable":
        target.deferrable = True
    elif mutation == "initially":
        target.initially = "DEFERRED"
    elif mutation == "nulls_not_distinct":
        target.dialect_kwargs["postgresql_nulls_not_distinct"] = True
    else:
        target.dialect_kwargs["postgresql_include"] = ["created_at"]
    before = ddl_snapshot(metadata, postgresql.dialect())
    with pytest.raises(RuntimeError, match=rf"{table}\.{pair_name} contract changed"):
        development_metadata(metadata, postgresql.dialect())
    assert ddl_snapshot(metadata, postgresql.dialect()) == before


@pytest.mark.parametrize("table,column,parent,target_name,pair_name", LINKS)
@pytest.mark.parametrize("part", ["pair", "single"])
def test_residual_source_not_valid_option_is_refused_before_clone(
    table, column, parent, target_name, pair_name, part
):
    metadata = alternate_metadata()
    target = (
        named_fk(metadata, table, pair_name)
        if part == "pair"
        else next(
            fk
            for fk in metadata.tables[table].foreign_key_constraints
            if tuple(fk.column_keys) == (column,)
        )
    )
    target.dialect_kwargs["postgresql_not_valid"] = True
    before = ddl_snapshot(metadata, postgresql.dialect())
    with pytest.raises(RuntimeError, match=rf"{table}\.{pair_name} contract changed"):
        development_metadata(metadata, postgresql.dialect())
    assert ddl_snapshot(metadata, postgresql.dialect()) == before


def test_three_residual_projections_keep_exact_target_objects_and_portable_metadata():
    metadata = alternate_metadata()
    before_pg = ddl_snapshot(metadata, postgresql.dialect())
    before_sqlite = ddl_snapshot(metadata, sqlite.dialect())
    projected = development_metadata(metadata, postgresql.dialect())
    assert projected is not metadata
    for table, column, parent, target_name, pair_name in LINKS:
        for candidate in (metadata, projected):
            target = next(c for c in candidate.tables[parent].constraints if c.name == target_name)
            assert isinstance(target, UniqueConstraint)
            assert tuple(target.columns) == (
                candidate.tables[parent].c.org_id,
                candidate.tables[parent].c.id,
            )
            pair = named_fk(candidate, table, pair_name)
            assert pair.elements[0].column is candidate.tables[parent].c.org_id
            assert pair.elements[1].column is candidate.tables[parent].c.id
            assert pair.elements[0].parent is candidate.tables[table].c.org_id
            assert pair.elements[1].parent is candidate.tables[table].c[column]
        assert named_fk(metadata, table, pair_name).ondelete is None
        assert named_fk(projected, table, pair_name).ondelete == f"SET NULL ({column})"
    assert ddl_snapshot(metadata, postgresql.dialect()) == before_pg
    assert ddl_snapshot(metadata, sqlite.dialect()) == before_sqlite
    assert development_metadata(metadata, sqlite.dialect()) is metadata


@pytest.mark.parametrize(
    "mutation",
    [
        "not_valid",
        "delete_action",
        "update_action",
        "deferrable",
        "reversed",
        "wrong_parent",
        "missing_pair",
        "redundant_single",
        "nullable_org",
        "nullable_reference",
        "element_policy",
        "collection_disagreement",
    ],
)
def test_mandatory_contact_source_contract_is_validated_before_projection(mutation):
    from sqlalchemy import ForeignKeyConstraint

    metadata = alternate_metadata()
    table = metadata.tables["customer_contacts"]
    pair = named_fk(metadata, "customer_contacts", "fk_customer_contacts_customer")

    def remove_pair():
        table.constraints.remove(pair)
        for element in pair.elements:
            table.foreign_keys.remove(element)
            element.parent.foreign_keys.remove(element)

    if mutation == "not_valid":
        pair.dialect_kwargs["postgresql_not_valid"] = True
    elif mutation in ("delete_action", "update_action", "deferrable"):
        attribute, value = {
            "delete_action": ("ondelete", "SET NULL"),
            "update_action": ("onupdate", "CASCADE"),
            "deferrable": ("deferrable", True),
        }[mutation]
        setattr(pair, attribute, value)
        for element in pair.elements:
            setattr(element, attribute, value)
    elif mutation in ("reversed", "wrong_parent"):
        remove_pair()
        table.append_constraint(
            ForeignKeyConstraint(
                ["customer_id", "org_id"] if mutation == "reversed" else ["org_id", "customer_id"],
                ["customers.id", "customers.org_id"]
                if mutation == "reversed"
                else ["vendors.org_id", "vendors.id"],
                name="fk_customer_contacts_customer",
                ondelete="CASCADE",
            )
        )
    elif mutation == "missing_pair":
        remove_pair()
    elif mutation == "redundant_single":
        table.append_constraint(
            ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="CASCADE")
        )
    elif mutation == "nullable_org":
        table.c.org_id.nullable = True
    elif mutation == "nullable_reference":
        table.c.customer_id.nullable = True
    elif mutation == "element_policy":
        pair.elements[0].ondelete = "SET NULL"
    else:
        table.constraints.remove(pair)
        assert pair in table.foreign_key_constraints
    before = ddl_snapshot(metadata, postgresql.dialect())
    with pytest.raises(
        RuntimeError, match=r"DB-PG-RESIDUAL-01.*customer_contacts\.fk_customer_contacts_customer"
    ):
        development_metadata(metadata, postgresql.dialect())
    assert ddl_snapshot(metadata, postgresql.dialect()) == before


def test_absent_contact_table_keeps_partial_metadata_supported_without_suppression():
    from tests.test_development_schema import declaration_metadata

    metadata = declaration_metadata(schema="partial_declared_contracts")
    assert "partial_declared_contracts.customer_contacts" not in metadata.tables
    before = ddl_snapshot(metadata, postgresql.dialect())
    projected = development_metadata(metadata, postgresql.dialect())
    assert set(projected.tables) == set(metadata.tables)
    assert projected.schema == metadata.schema
    assert projected.naming_convention == metadata.naming_convention
    assert ddl_snapshot(metadata, postgresql.dialect()) == before
