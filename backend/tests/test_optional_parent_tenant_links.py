"""Optional parent references preserve tenant keys and existing detach behavior."""

from __future__ import annotations

import warnings
from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, inspect, select, text
from sqlalchemy.exc import IntegrityError, SAWarning
from sqlalchemy.orm import configure_mappers

from app.models import Base
from app.models.expense import ExpenseTransaction
from app.models.issued_invoice import IssuedInvoice
from app.models.recurring_invoice import RecurringInvoice
from app.models.vendor_change_request import VendorChangeRequest
from tests.test_partner_webhook_parent_migrations import _rows, _seed

# Independent expected contracts; not read from the production registry.
LINKS = (
    (
        ExpenseTransaction,
        "item_id",
        "expense_items",
        "item",
        "transaction",
        "fk_expense_transactions_item",
    ),
    (IssuedInvoice, "partner_id", "partners", "partner", "invoice", "fk_issued_invoices_partner"),
    (
        RecurringInvoice,
        "partner_id",
        "partners",
        "partner",
        "schedule",
        "fk_recurring_invoices_partner",
    ),
    (
        VendorChangeRequest,
        "source_document_id",
        "documents",
        "document",
        "request",
        "fk_vendor_change_requests_source_document",
    ),
)


@pytest.fixture
def optional_family():
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def enable_fk(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    try:
        Base.metadata.create_all(engine)
        with engine.connect() as connection:
            identities = _seed(connection)
            assert connection.scalar(text("PRAGMA foreign_keys")) == 1
            yield connection, identities
    finally:
        engine.dispose()


@pytest.mark.parametrize("model,column,parent,parent_key,child_key,name", LINKS)
def test_exact_pair_and_unchanged_single(model, column, parent, parent_key, child_key, name):
    table = model.__table__
    pair = [fk for fk in table.foreign_key_constraints if fk.name == name]
    assert len(pair) == 1
    assert tuple(pair[0].column_keys) == ("org_id", column)
    assert tuple(e.target_fullname for e in pair[0].elements) == (
        parent + ".org_id",
        parent + ".id",
    )
    assert pair[0].ondelete is None and pair[0].deferrable is None
    assert not table.c.org_id.nullable and table.c[column].nullable
    singles = [fk for fk in table.foreign_key_constraints if tuple(fk.column_keys) == (column,)]
    assert len(singles) == 1 and singles[0].name is None
    assert singles[0].ondelete == "SET NULL"
    assert tuple(e.target_fullname for e in singles[0].elements) == (parent + ".id",)


@pytest.mark.parametrize("parent", ["expense_items", "documents", "partners"])
def test_target_pair_index_is_exact(parent):
    indexes = [
        index
        for index in Base.metadata.tables[parent].indexes
        if index.name == f"uq_{parent}_org_id"
    ]
    assert len(indexes) == 1 and indexes[0].unique
    assert tuple(column.name for column in indexes[0].columns) == ("org_id", "id")
    assert not indexes[0].dialect_kwargs


@pytest.mark.parametrize("model,column,parent,parent_key,child_key,name", LINKS)
@pytest.mark.parametrize("operation", ["insert", "reference", "child_org", "parent_org"])
def test_sqlite_refuses_wrong_pair_immediately(
    optional_family, model, column, parent, parent_key, child_key, name, operation
):
    connection, (a, b) = optional_family
    before = _rows(connection)
    child = model.__table__
    with (
        pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"),
        connection.begin_nested(),
    ):
        if operation == "insert":
            values = dict(
                connection.execute(select(child).where(child.c.id == a[child_key])).mappings().one()
            )
            values.update(id=str(uuid4()), **{column: b[parent_key]})
            if child.name == "vendor_change_requests":
                values["status"] = "approved"
            connection.execute(child.insert().values(**values))
        elif operation == "reference":
            connection.execute(
                child.update().where(child.c.id == a[child_key]).values(**{column: b[parent_key]})
            )
        elif operation == "child_org":
            values = {"org_id": b["org"]}
            if child.name == "vendor_change_requests":
                values.update(vendor_id=b["vendor"], status="approved")
            connection.execute(child.update().where(child.c.id == a[child_key]).values(**values))
        else:
            values = {"org_id": b["org"]}
            if parent == "expense_items":
                values["report_id"] = b["report"]
            if parent == "partners":
                other = (
                    "recurring_invoices" if child.name == "issued_invoices" else "issued_invoices"
                )
                connection.execute(
                    text(f"UPDATE {other} SET partner_id=NULL WHERE partner_id=:id"),
                    {"id": a["partner"]},
                )
                connection.execute(
                    text("DELETE FROM partner_documents WHERE partner_id=:id"), {"id": a["partner"]}
                )
            target = Base.metadata.tables[parent]
            connection.execute(target.update().where(target.c.id == a[parent_key]).values(**values))
    assert _rows(connection) == before


@pytest.mark.parametrize("model,column,parent,parent_key,child_key,name", LINKS)
def test_nullable_reference_and_own_pair_remain_valid(
    optional_family, model, column, parent, parent_key, child_key, name
):
    connection, (a, _b) = optional_family
    table = model.__table__
    original = dict(
        connection.execute(select(table).where(table.c.id == a[child_key])).mappings().one()
    )
    for value in (a[parent_key], None):
        with connection.begin_nested():
            candidate = dict(original, id=str(uuid4()), **{column: value})
            if table.name == "vendor_change_requests":
                candidate["status"] = "approved"
            connection.execute(table.insert().values(**candidate))
            assert (
                connection.scalar(select(table.c[column]).where(table.c.id == candidate["id"]))
                == value
            )


@pytest.mark.parametrize("model,column,parent,parent_key,child_key,name", LINKS)
def test_scalar_id_assignment_does_not_synchronize_org(
    model, column, parent, parent_key, child_key, name
):
    with warnings.catch_warnings():
        warnings.simplefilter("error", SAWarning)
        configure_mappers()
        assert not [
            rel for rel in inspect(model).relationships if rel.mapper.class_.__tablename__ == parent
        ]
        row = model(org_id="retained-org")
        setattr(row, column, "other-parent")
        assert row.org_id == "retained-org" and getattr(row, column) == "other-parent"


@pytest.mark.parametrize(
    "parent,key",
    [
        ("expense_items", "item"),
        ("partners", "partner"),
        ("documents", "document"),
        ("expense_reports", "report"),
    ],
)
@pytest.mark.parametrize("count", [1, 2])
def test_delete_detaches_only_optional_ids_and_rollback_restores_every_row(
    optional_family, parent, key, count
):
    connection, ids = optional_family
    before = _rows(connection)
    targets = {identity[key] for identity in ids[:count]}
    savepoint = connection.begin_nested()
    try:
        table = Base.metadata.tables[parent]
        connection.execute(table.delete().where(table.c.id.in_(targets)))
        expected = deepcopy(before)
        expected[parent] = [row for row in expected[parent] if row["id"] not in targets]
        references = targets
        if parent == "expense_reports":
            references = {
                row["id"] for row in before["expense_items"] if row["report_id"] in targets
            }
            for child in ("expense_items", "expense_comments", "expense_approval_steps"):
                expected[child] = [
                    row for row in expected[child] if row["report_id"] not in targets
                ]
        if parent == "partners":
            expected["partner_documents"] = [
                row for row in expected["partner_documents"] if row["partner_id"] not in targets
            ]
        for model, column, target, _, _, _ in LINKS:
            if target == parent or (parent == "expense_reports" and target == "expense_items"):
                for row in expected[model.__tablename__]:
                    if row[column] in references:
                        row[column] = None
        assert _rows(connection) == expected
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    finally:
        savepoint.rollback()
    assert _rows(connection) == before
