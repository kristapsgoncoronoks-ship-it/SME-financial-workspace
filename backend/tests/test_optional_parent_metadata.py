"""Exact optional-parent projections preserve the shared portable declarations."""

from __future__ import annotations

import pytest
from sqlalchemy import Column, ForeignKeyConstraint, Integer, MetaData, Table
from sqlalchemy.dialects import postgresql

from app.core.development_schema import development_metadata
from app.models import Base
from tests.test_development_schema import alternate_metadata, ddl_snapshot, named_fk
from tests.test_optional_parent_tenant_links import LINKS


@pytest.mark.parametrize("model,column,parent,parent_key,child_key,name", LINKS)
@pytest.mark.parametrize("part", ["pair", "single", "pair_element", "single_element"])
@pytest.mark.parametrize(
    "attribute,value",
    [
        ("ondelete", "CASCADE"),
        ("onupdate", "CASCADE"),
        ("deferrable", True),
        ("initially", "DEFERRED"),
        ("match", "FULL"),
        ("use_alter", True),
        ("link_to_name", True),
    ],
)
def test_optional_constraint_and_element_policy_drift_is_refused(
    model, column, parent, parent_key, child_key, name, part, attribute, value
):
    metadata = alternate_metadata()
    pair = named_fk(metadata, model.__tablename__, name)
    single = next(
        fk
        for fk in metadata.tables[model.__tablename__].foreign_key_constraints
        if tuple(fk.column_keys) == (column,)
    )
    constraint = pair if part.startswith("pair") else single
    target = constraint.elements[0] if part.endswith("element") else constraint
    setattr(target, attribute, value)
    # Test coherent unwanted policies too: equality alone must not bless them.
    if not part.endswith("element"):
        for element in constraint.elements:
            setattr(element, attribute, value)
    with pytest.raises(RuntimeError, match=rf"{model.__tablename__}\.{name}"):
        development_metadata(metadata, postgresql.dialect())
    assert getattr(target, attribute) == value


@pytest.mark.asyncio
async def test_seed_bootstrap_uses_connection_dialect_projection(monkeypatch, tmp_path):
    from sqlalchemy import inspect
    from sqlalchemy.ext.asyncio import create_async_engine

    from app import seed

    class BootstrapComplete(Exception):
        pass

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'seed-bootstrap.db'}")
    calls = []

    def project(metadata, dialect):
        calls.append(dialect.name)
        return development_metadata(metadata, dialect)

    def stop_before_demo_data():
        raise BootstrapComplete

    monkeypatch.setattr(seed, "engine", engine)
    monkeypatch.setattr(seed, "development_metadata", project)
    monkeypatch.setattr(seed, "SessionLocal", stop_before_demo_data)
    try:
        with pytest.raises(BootstrapComplete):
            await seed.seed()
        assert calls == ["sqlite"]
        async with engine.connect() as connection:
            assert set(await connection.run_sync(lambda c: inspect(c).get_table_names())) == set(
                Base.metadata.tables
            )
    finally:
        await engine.dispose()


@pytest.mark.parametrize("model,column,parent,parent_key,child_key,name", LINKS)
@pytest.mark.parametrize(
    "mutation",
    [
        "missing_pair",
        "missing_single",
        "duplicate_pair",
        "duplicate_single",
        "nullable_org",
        "required_ref",
        "named_single",
        "retargeted_pair",
    ],
)
def test_optional_contract_shape_is_not_inferred_or_normalized(
    model, column, parent, parent_key, child_key, name, mutation
):
    metadata = alternate_metadata()
    table = metadata.tables[model.__tablename__]
    pair = named_fk(metadata, model.__tablename__, name)
    single = next(fk for fk in table.foreign_key_constraints if tuple(fk.column_keys) == (column,))
    if mutation.startswith("missing"):
        removed = pair if mutation == "missing_pair" else single
        table.constraints.remove(removed)
        for element in removed.elements:
            table.foreign_keys.remove(element)
            element.parent.foreign_keys.remove(element)
    elif mutation.startswith("duplicate"):
        columns, targets, action = (
            (["org_id", column], [parent + ".org_id", parent + ".id"], None)
            if mutation == "duplicate_pair"
            else ([column], [parent + ".id"], "SET NULL")
        )
        table.append_constraint(
            ForeignKeyConstraint(columns, targets, name="unexpected_duplicate", ondelete=action)
        )
    elif mutation == "nullable_org":
        table.c.org_id.nullable = True
    elif mutation == "required_ref":
        table.c[column].nullable = False
    elif mutation == "named_single":
        single.name = "unexpected_legacy_name"
    else:
        table.constraints.remove(pair)
        for element in pair.elements:
            table.foreign_keys.remove(element)
            element.parent.foreign_keys.remove(element)
        target = "documents" if parent != "documents" else "partners"
        table.append_constraint(
            ForeignKeyConstraint(
                ["org_id", column], [target + ".org_id", target + ".id"], name=name
            )
        )
    expected_error = rf"{model.__tablename__}\.{name}"
    if mutation == "nullable_org" and model.__tablename__ == "issued_invoices":
        # This shared column also belongs to DB-018's older project pair,
        # whose existing validator runs first and must retain its exact error.
        expected_error = r"issued_invoices\.fk_issued_invoices_project"
    with pytest.raises(RuntimeError, match=expected_error):
        development_metadata(metadata, postgresql.dialect())


@pytest.mark.parametrize("model,column,parent,parent_key,child_key,name", LINKS)
@pytest.mark.parametrize("part", ["pair", "single"])
def test_constraint_collection_disagreement_is_refused_before_copy(
    model, column, parent, parent_key, child_key, name, part
):
    metadata = alternate_metadata()
    table = metadata.tables[model.__tablename__]
    constraint = (
        named_fk(metadata, model.__tablename__, name)
        if part == "pair"
        else next(fk for fk in table.foreign_key_constraints if tuple(fk.column_keys) == (column,))
    )
    table.constraints.remove(constraint)
    # Elements are deliberately still registered on the table. An inconsistent
    # source object must not become acceptable when to_metadata normalizes it.
    assert constraint in table.foreign_key_constraints
    with pytest.raises(RuntimeError, match=rf"{model.__tablename__}\.{name}"):
        development_metadata(metadata, postgresql.dialect())


def test_schema_qualified_optional_targets_and_one_name_survive_projection():
    metadata = MetaData(schema="qualified_optional")
    for table in Base.metadata.sorted_tables:
        table.to_metadata(metadata, schema=metadata.schema)
    before = ddl_snapshot(metadata, postgresql.dialect())
    projected = development_metadata(metadata, postgresql.dialect())
    assert projected.schema == metadata.schema
    assert set(projected.tables) == set(metadata.tables)
    for model, column, parent, _, _, name in LINKS:
        fk = named_fk(projected, "qualified_optional." + model.__tablename__, name)
        assert tuple(e.target_fullname for e in fk.elements) == (
            f"qualified_optional.{parent}.org_id",
            f"qualified_optional.{parent}.id",
        )
        assert fk.ondelete == f"SET NULL ({column})"
    assert ddl_snapshot(metadata, postgresql.dialect()) == before


@pytest.mark.parametrize("model,column,parent,parent_key,child_key,name", LINKS)
@pytest.mark.parametrize("element_kind", ["pair_org", "pair_id", "single_id"])
def test_resolved_parent_identity_cannot_be_normalized_by_clone(
    model, column, parent, parent_key, child_key, name, element_kind
):
    metadata = alternate_metadata()
    pair = named_fk(metadata, model.__tablename__, name)
    single = next(
        fk
        for fk in metadata.tables[model.__tablename__].foreign_key_constraints
        if tuple(fk.column_keys) == (column,)
    )
    foreign_parent = Table(parent, MetaData(), Column("org_id", Integer), Column("id", Integer))
    element = (
        pair.elements[0 if element_kind == "pair_org" else 1]
        if element_kind.startswith("pair")
        else single.elements[0]
    )
    target = "org_id" if element_kind == "pair_org" else "id"
    assert element.column is metadata.tables[parent].c[target]
    element.column = foreign_parent.c[target]
    # Textual targets still match, but the resolved column belongs to another
    # metadata graph. Table.to_metadata would otherwise conceal this mismatch.
    assert element.target_fullname == parent + "." + target
    with pytest.raises(RuntimeError, match=rf"{model.__tablename__}\.{name}"):
        development_metadata(metadata, postgresql.dialect())
