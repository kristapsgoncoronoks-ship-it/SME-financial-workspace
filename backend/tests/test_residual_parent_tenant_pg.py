"""Current-head residual pairs are immediate and retain legal delete behavior."""

from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from app.models import Base
from tests.test_migrations import BACKEND_DIR
from tests.test_planning_ocr_migration_integration import schemas
from tests.test_residual_parent_migration_pg import (
    SINGLES,
    _owned_database,
    _scope,
    _security,
)
from tests.test_residual_parent_tenant_links import LINKS, _rows


@pytest.fixture(scope="module")
def current_database():
    config = Config()
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    head = ScriptDirectory.from_config(config).get_current_head()
    assert head is not None
    with _owned_database(head, historical=False) as family:
        yield family


@pytest_asyncio.fixture
async def current_family(current_database):
    url, identities, _, _ = current_database
    engine = create_async_engine(url)
    try:
        async with engine.connect() as db:
            outer = await db.begin()
            await _scope(db, "")
            await _security(db)
            before, schema = await db.run_sync(_rows), await db.run_sync(schemas)
            try:
                yield db, identities
            finally:
                await outer.rollback()
            await db.invalidate()
            await _scope(db, "")
            assert await db.run_sync(_rows) == before
            assert await db.run_sync(schemas) == schema
            await db.rollback()
    finally:
        await engine.dispose()


def _constraint_name(error):
    original = error.orig
    return (
        getattr(original.__cause__, "constraint_name", None)
        or getattr(original, "constraint_name", None)
        or getattr(getattr(original, "diag", None), "constraint_name", None)
    )


async def _source(db, table, identifier):
    return dict((await db.execute(select(table).where(table.c.id == identifier))).mappings().one())


def _clone(source, *, reference=None, column=None):
    values = dict(source, id=str(uuid4()))
    if "number" in values:
        values["number"] = "N-" + uuid4().hex
    if column is not None:
        values[column] = reference
    return values


@pytest.mark.parametrize("model,column,parent,parent_key,child_key,name,nullable", LINKS)
@pytest.mark.parametrize("operation", ["insert", "reference", "child_org", "parent_org"])
async def test_intended_guard_refuses_wrong_pair_immediately_without_other_guard_substitution(
    current_family, model, column, parent, parent_key, child_key, name, nullable, operation
):
    db, (a, b) = current_family
    before = await db.run_sync(_rows)
    child = model.__table__
    original = await _source(db, child, a[child_key])
    await _scope(db, "" if operation in ("child_org", "parent_org") else a["org"])
    with pytest.raises(IntegrityError) as caught:
        async with db.begin_nested():
            if operation == "insert":
                await db.execute(
                    child.insert().values(
                        **_clone(original, column=column, reference=b[parent_key])
                    )
                )
            elif operation == "reference":
                await db.execute(
                    child.update()
                    .where(child.c.id == a[child_key])
                    .values(**{column: b[parent_key]})
                )
            elif operation == "child_org":
                # A fresh valid child has no incoming payment, attachment or
                # reading-history guard that could mask this intended refusal.
                candidate = _clone(original)
                await db.execute(child.insert().values(**candidate))
                await db.execute(
                    child.update().where(child.c.id == candidate["id"]).values(org_id=b["org"])
                )
            else:
                # Fresh parent + one new child isolate the pair from other
                # seeded incoming references. Received invoices additionally
                # move their vendor link coherently with the parent tenant.
                target = Base.metadata.tables[parent]
                candidate = _clone(await _source(db, target, a[parent_key]))
                await db.execute(target.insert().values(**candidate))
                await db.execute(
                    child.insert().values(
                        **_clone(original, column=column, reference=candidate["id"])
                    )
                )
                values = {"org_id": b["org"]}
                if parent == "invoices":
                    values["vendor_id"] = b["vendor"]
                await db.execute(
                    target.update().where(target.c.id == candidate["id"]).values(**values)
                )
    assert caught.value.orig.sqlstate == "23503"
    assert _constraint_name(caught.value) == name
    await _scope(db, "")
    assert await db.run_sync(_rows) == before


@pytest.mark.parametrize("model,column,parent,parent_key,child_key,name,nullable", LINKS)
@pytest.mark.parametrize("operation", ["insert", "update"])
async def test_actual_force_rls_hides_parent_and_refuses_foreign_child_even_with_valid_pair(
    current_family, model, column, parent, parent_key, child_key, name, nullable, operation
):
    db, (a, b) = current_family
    before = await db.run_sync(_rows)
    child, target = model.__table__, Base.metadata.tables[parent]
    foreign = _clone(await _source(db, child, b[child_key]))
    await _scope(db, a["org"])
    assert await db.scalar(select(target.c.id).where(target.c.id == b[parent_key])) is None
    with pytest.raises(DBAPIError) as caught:
        async with db.begin_nested():
            if operation == "insert":
                await db.execute(child.insert().values(**foreign))
            else:
                await db.execute(
                    child.update()
                    .where(child.c.id == a[child_key])
                    .values(org_id=b["org"], **{column: b[parent_key]})
                )
    assert caught.value.orig.sqlstate == "42501"
    await _scope(db, "")
    assert await db.run_sync(_rows) == before


@pytest.mark.parametrize("model,column,parent,parent_key,child_key,name,nullable", LINKS)
async def test_own_parent_and_original_nullability_preserve_all_child_fields(
    current_family, model, column, parent, parent_key, child_key, name, nullable
):
    db, (a, _b) = current_family
    before = await db.run_sync(_rows)
    child = model.__table__
    original = await _source(db, child, a[child_key])
    await _scope(db, a["org"])
    for value in (a[parent_key], None):
        candidate = _clone(original, column=column, reference=value)
        if value is None and not nullable:
            with pytest.raises(IntegrityError) as caught:
                async with db.begin_nested():
                    await db.execute(child.insert().values(**candidate))
            assert caught.value.orig.sqlstate == "23502"
            continue
        boundary = await db.begin_nested()
        try:
            await db.execute(child.insert().values(**candidate))
            assert await _source(db, child, candidate["id"]) == candidate
        finally:
            await boundary.rollback()
    await _scope(db, "")
    assert await db.run_sync(_rows) == before


def _sorted(rows):
    import json

    return {
        table: sorted(values, key=lambda value: json.dumps(value, default=str, sort_keys=True))
        for table, values in rows.items()
    }


def _after_delete(before, parent, identifiers):
    expected = deepcopy(before)
    expected[parent] = [row for row in expected[parent] if str(row["id"]) not in identifiers]
    if parent == "customers":
        expected["customer_contacts"] = [
            row
            for row in expected["customer_contacts"]
            if str(row["customer_id"]) not in identifiers
        ]
    elif parent == "invoices":
        expected["line_items"] = [
            row for row in expected["line_items"] if str(row["invoice_id"]) not in identifiers
        ]
    elif parent == "issued_invoices":
        for table, reference in (
            ("issued_invoice_lines", "invoice_id"),
            ("issued_invoice_attachments", "invoice_id"),
            ("payments", "issued_invoice_id"),
        ):
            expected[table] = [
                row for row in expected[table] if str(row[reference]) not in identifiers
            ]
    for model, column, target, *_ in LINKS:
        if target == parent and model.__tablename__ != "customer_contacts":
            for row in expected[model.__tablename__]:
                if str(row[column]) in identifiers:
                    row[column] = None
    return _sorted(expected)


async def _order_constraints(db, table, pair, single, pair_first):
    definitions = {}
    for name in (pair, single):
        definitions[name] = await db.scalar(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid=to_regclass(:table) AND conname=:name"
            ),
            {"table": table, "name": name},
        )
        assert definitions[name] is not None
    # Recreate both explicitly, so both tested orders remain real even if a
    # future migration happened to recreate the retained single first/last.
    await db.execute(
        text(f'ALTER TABLE "{table}" DROP CONSTRAINT "{pair}", DROP CONSTRAINT "{single}"')
    )
    order = (pair, single) if pair_first else (single, pair)
    for name in order:
        await db.execute(text(f'ALTER TABLE "{table}" ADD CONSTRAINT "{name}" {definitions[name]}'))
    installed = list(
        (
            await db.execute(
                text(
                    "SELECT conname FROM pg_constraint WHERE conrelid=to_regclass(:table) AND conname IN (:pair,:single) ORDER BY oid"
                ),
                {"table": table, "pair": pair, "single": single},
            )
        ).scalars()
    )
    assert installed == list(order)


@pytest.mark.parametrize(
    "model,column,parent,parent_key,child_key,name,nullable", [x for x in LINKS if x[-1]]
)
@pytest.mark.parametrize(
    "pair_first", [False, True], ids=["single-created-first", "pair-created-first"]
)
async def test_optional_both_creation_orders_preserve_exact_direct_and_multirow_delete_contracts(
    current_family, model, column, parent, parent_key, child_key, name, nullable, pair_first
):
    db, (a, b) = current_family
    before, schema = await db.run_sync(_rows), await db.run_sync(schemas)
    await _order_constraints(
        db, model.__tablename__, name, SINGLES[model.__tablename__, column], pair_first
    )
    assert await db.run_sync(_rows) == before and await db.run_sync(schemas) == schema
    for identifiers in ([a[parent_key]], [a[parent_key], b[parent_key]]):
        boundary = await db.begin_nested()
        try:
            target = Base.metadata.tables[parent]
            await db.execute(target.delete().where(target.c.id.in_(identifiers)))
            assert await db.run_sync(_rows) == _after_delete(before, parent, identifiers)
        finally:
            await boundary.rollback()
        assert await db.run_sync(_rows) == before and await db.run_sync(schemas) == schema


async def test_mandatory_contact_cascade_preserves_unrelated_tenants_and_legal_snapshots(
    current_family,
):
    db, (a, b) = current_family
    before, schema = await db.run_sync(_rows), await db.run_sync(schemas)
    boundary = await db.begin_nested()
    try:
        await _scope(db, a["org"])
        await db.execute(text("DELETE FROM customers WHERE id=:id"), {"id": a["customer"]})
        await _scope(db, "")
        after = await db.run_sync(_rows)
        assert after == _after_delete(before, "customers", [a["customer"]])
        assert any(str(row["id"]) == b["contact"] for row in after["customer_contacts"])
    finally:
        await boundary.rollback()
    assert await db.run_sync(_rows) == before and await db.run_sync(schemas) == schema


@pytest.mark.parametrize(
    "pair_first", [False, True], ids=["single-created-first", "pair-created-first"]
)
async def test_self_references_and_cycles_keep_original_statement_timing_and_delete_actions(
    current_family, pair_first
):
    db, (a, _b) = current_family
    table = Base.metadata.tables["issued_invoices"]
    await _order_constraints(
        db, table.name, "fk_issued_invoices_corrected_tenant", "fk_issued_corrected", pair_first
    )
    source = await _source(db, table, a["credit"])
    boundary = await db.begin_nested()
    try:
        first, second = _clone(source), _clone(source)
        first["corrected_invoice_id"] = first["id"]
        await db.execute(table.insert().values(**first))
        second["corrected_invoice_id"] = first["id"]
        await db.execute(table.insert().values(**second))
        await db.execute(
            text("UPDATE issued_invoices SET corrected_invoice_id=:second WHERE id=:first"),
            {"first": first["id"], "second": second["id"]},
        )
        before = await db.run_sync(_rows)
        deletion = await db.begin_nested()
        try:
            await db.execute(table.delete().where(table.c.id == first["id"]))
            assert await db.run_sync(_rows) == _after_delete(
                before, "issued_invoices", [first["id"]]
            )
            survivor = await _source(db, table, second["id"])
            assert survivor["corrected_invoice_id"] is None
            assert survivor["corrected_invoice_number"] == source["corrected_invoice_number"]
        finally:
            await deletion.rollback()
        assert await db.run_sync(_rows) == before
        await db.execute(table.delete().where(table.c.id.in_([first["id"], second["id"]])))
        assert await db.run_sync(_rows) == _after_delete(
            before, "issued_invoices", [first["id"], second["id"]]
        )
    finally:
        await boundary.rollback()


@pytest.mark.parametrize(
    "model,column,parent,parent_key,child_key,name,nullable", [x for x in LINKS if x[-1]]
)
@pytest.mark.parametrize("drift", ["cascade", "not_valid", "deferred", "generic_set_null"])
async def test_current_comparator_refuses_new_pair_policy_drift_without_writing_or_normalizing(
    current_family, model, column, parent, parent_key, child_key, name, nullable, drift
):
    from sqlalchemy import event

    from app.core.migration_schema import postgresql_comparison_metadata
    from tests.test_pg_migration_schema_contract import metadata_signature

    db, _ = current_family
    before, schema = await db.run_sync(_rows), await db.run_sync(schemas)
    source_signature = metadata_signature(Base.metadata)
    boundary = await db.begin_nested()
    table = model.__tablename__
    try:
        definition = await db.scalar(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid=to_regclass(:table) AND conname=:name"
            ),
            {"table": table, "name": name},
        )
        assert definition is not None and f"SET NULL ({column})" in definition
        if drift == "cascade":
            changed = definition.replace(f"SET NULL ({column})", "CASCADE")
        elif drift == "generic_set_null":
            changed = definition.replace(f"SET NULL ({column})", "SET NULL")
        elif drift == "deferred":
            changed = definition + " DEFERRABLE INITIALLY DEFERRED"
        else:
            changed = definition + " NOT VALID"
        assert changed != definition
        await db.execute(
            text(
                f'ALTER TABLE "{table}" DROP CONSTRAINT "{name}", ADD CONSTRAINT "{name}" {changed}'
            )
        )
        changed_schema = await db.run_sync(schemas)
        statements = []

        def observe(_connection, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)

        event.listen(db.sync_connection, "before_cursor_execute", observe)
        try:
            with pytest.raises(RuntimeError, match=table + r"\." + name):
                await db.run_sync(postgresql_comparison_metadata, Base.metadata)
        finally:
            event.remove(db.sync_connection, "before_cursor_execute", observe)
        assert statements and all(
            statement.lstrip().upper().startswith("SELECT") for statement in statements
        )
        assert metadata_signature(Base.metadata) == source_signature
        assert await db.run_sync(_rows) == before
        assert await db.run_sync(schemas) == changed_schema
    finally:
        await boundary.rollback()
    assert metadata_signature(Base.metadata) == source_signature
    assert await db.run_sync(_rows) == before and await db.run_sync(schemas) == schema


async def test_current_comparator_refuses_identical_unvalidated_contact_pair_without_catalog_mutation(
    current_family,
):
    from sqlalchemy import event

    from app.core.migration_schema import postgresql_comparison_metadata
    from tests.test_pg_migration_schema_contract import metadata_signature

    db, _ = current_family
    before, schema = await db.run_sync(_rows), await db.run_sync(schemas)
    source_signature = metadata_signature(Base.metadata)
    native_query = text(
        "SELECT r.relname,c.conname,c.contype::text,c.convalidated,c.condeferrable,c.condeferred,"
        "c.confdeltype::text,c.confupdtype::text,c.confmatchtype::text,c.confdelsetcols::int[],"
        "pg_get_constraintdef(c.oid) FROM pg_constraint c JOIN pg_class r ON r.oid=c.conrelid "
        "JOIN pg_namespace n ON n.oid=r.relnamespace WHERE n.nspname='public' "
        "ORDER BY r.relname,c.conname"
    )
    native_before = (await db.execute(native_query)).all()
    pair = "fk_customer_contacts_customer"
    definition = await db.scalar(
        text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid='customer_contacts'::regclass AND conname=:name"
        ),
        {"name": pair},
    )
    assert definition is not None and definition.endswith("ON DELETE CASCADE")
    boundary = await db.begin_nested()
    try:
        await db.execute(
            text(
                f'ALTER TABLE customer_contacts DROP CONSTRAINT "{pair}", '
                f'ADD CONSTRAINT "{pair}" {definition} NOT VALID'
            )
        )
        assert (
            await db.scalar(
                text(
                    "SELECT convalidated FROM pg_constraint "
                    "WHERE conrelid='customer_contacts'::regclass AND conname=:name"
                ),
                {"name": pair},
            )
            is False
        )
        changed_native = (await db.execute(native_query)).all()
        assert changed_native != native_before
        # The inspector omits validation status. Native state must therefore
        # be pinned separately even when the ordinary reflection is identical.
        assert await db.run_sync(schemas) == schema
        statements = []

        def observe(_connection, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)

        event.listen(db.sync_connection, "before_cursor_execute", observe)
        try:
            with pytest.raises(
                RuntimeError, match=r"customer_contacts\.fk_customer_contacts_customer"
            ):
                await db.run_sync(postgresql_comparison_metadata, Base.metadata)
        finally:
            event.remove(db.sync_connection, "before_cursor_execute", observe)
        assert statements and all(
            statement.lstrip().upper().startswith("SELECT") for statement in statements
        )
        assert (await db.execute(native_query)).all() == changed_native
        assert await db.run_sync(_rows) == before and await db.run_sync(schemas) == schema
        assert metadata_signature(Base.metadata) == source_signature
    finally:
        await boundary.rollback()
    assert (await db.execute(native_query)).all() == native_before
    assert await db.run_sync(_rows) == before and await db.run_sync(schemas) == schema
