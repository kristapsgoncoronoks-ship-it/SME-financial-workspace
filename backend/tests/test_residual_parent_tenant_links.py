"""Residual parent links reject tenant mismatches without rewriting legal data."""

from __future__ import annotations

import json
import re
import warnings
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, inspect, select, text
from sqlalchemy.exc import IntegrityError, SAWarning
from sqlalchemy.orm import Session, configure_mappers, selectinload

from app.models import Base
from app.models.customer import Customer, CustomerContact
from app.models.email_intake import InboundInvoice
from app.models.issued_invoice import IssuedInvoice
from tests.test_planning_ocr_migration_integration import _insert, schemas

LINKS = (
    (
        CustomerContact,
        "customer_id",
        "customers",
        "customer",
        "contact",
        "fk_customer_contacts_customer",
        False,
    ),
    (
        InboundInvoice,
        "invoice_id",
        "invoices",
        "received",
        "inbound",
        "fk_inbound_invoices_invoice_tenant",
        True,
    ),
    (
        IssuedInvoice,
        "customer_id",
        "customers",
        "customer",
        "billed",
        "fk_issued_invoices_customer_tenant",
        True,
    ),
    (
        IssuedInvoice,
        "corrected_invoice_id",
        "issued_invoices",
        "original",
        "credit",
        "fk_issued_invoices_corrected_tenant",
        True,
    ),
)


def _rows(connection):
    return {
        name: sorted(
            [dict(row) for row in connection.execute(text(f'SELECT * FROM "{name}"')).mappings()],
            key=lambda row: json.dumps(row, default=str, sort_keys=True),
        )
        for name in sorted(inspect(connection).get_table_names())
        if name != "alembic_version"
    }


def _seed(connection):
    identities = []
    for label in ("A", "B"):
        org = _insert(connection, "organizations", name="Synthetic residual " + label)
        customer = _insert(connection, "customers", org_id=org, name="Customer " + label)
        vendor = _insert(connection, "vendors", org_id=org, name="Supplier " + label)
        received = _insert(
            connection,
            "invoices",
            org_id=org,
            vendor_id=vendor,
            invoice_number="RECEIVED-" + label,
            issue_date=date(2026, 11, 6),
            total="91.23",
        )
        original = _insert(
            connection,
            "issued_invoices",
            org_id=org,
            buyer_name="Retained parent buyer " + label,
            seller_json='{"retained":true}',
            issue_date=date(2026, 11, 6),
            number="ORIGINAL-" + label,
            total="137.21",
        )
        contact = _insert(
            connection,
            "customer_contacts",
            org_id=org,
            customer_id=customer,
            name="Retained contact " + label,
            email="contact@example.com",
        )
        inbound = _insert(
            connection,
            "inbound_invoices",
            org_id=org,
            invoice_id=received,
            received_at=datetime(2026, 11, 6, tzinfo=UTC),
            filename="retained.csv",
            size=17,
            status="confirmed",
            sha256="a" * 64,
            draft_json='{"retained":"draft"}',
            reading_preparation_json='{"retained":"preparation"}',
            reading_observation_json='{"retained":"observation"}',
        )
        billed = _insert(
            connection,
            "issued_invoices",
            org_id=org,
            customer_id=customer,
            buyer_name="Frozen buyer " + label,
            seller_json='{"retained":"seller"}',
            issue_date=date(2026, 11, 6),
            number="BILLED-" + label,
            total="137.21",
            subtotal="130.00",
            tax_total="7.21",
        )
        credit = _insert(
            connection,
            "issued_invoices",
            org_id=org,
            corrected_invoice_id=original,
            corrected_invoice_number="ORIGINAL-" + label,
            doc_type="credit_note",
            buyer_name="Frozen correction buyer " + label,
            seller_json='{"retained":"seller"}',
            issue_date=date(2026, 11, 6),
            number="CREDIT-" + label,
            total="7.89",
        )
        line = _insert(
            connection,
            "issued_invoice_lines",
            invoice_id=original,
            description="Retained legal line",
            quantity="2",
            unit_price="20.00",
            net_amount="40.00",
        )
        attachment = _insert(
            connection,
            "issued_invoice_attachments",
            org_id=org,
            invoice_id=original,
            filename="retained-legal.pdf",
            sha256="b" * 64,
            size=32,
        )
        payment = _insert(
            connection,
            "payments",
            org_id=org,
            issued_invoice_id=original,
            amount="12.34",
            paid_on=date(2026, 11, 6),
            reference="Retained payment ledger",
        )
        history = _insert(
            connection,
            "capture_reading_observations",
            org_id=org,
            inbound_invoice_id=inbound,
            source_sha256="a" * 64,
            outcome="failure",
            failure_code="synthetic_retained",
            profile_json='{"retained":"profile"}',
            page_facts_json='[{"retained":"page"}]',
        )
        identities.append(
            dict(
                org=org,
                customer=customer,
                vendor=vendor,
                received=received,
                original=original,
                contact=contact,
                inbound=inbound,
                billed=billed,
                credit=credit,
                line=line,
                attachment=attachment,
                payment=payment,
                history=history,
            )
        )
    connection.commit()
    return identities


@pytest.fixture
def residual_family():
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


@pytest.mark.parametrize("model,column,parent,parent_key,child_key,name,nullable", LINKS)
def test_cross_tenant_insert_is_refused_by_actual_foreign_keys(
    residual_family, model, column, parent, parent_key, child_key, name, nullable
):
    connection, (a, b) = residual_family
    before = _rows(connection)
    table = model.__table__
    source = dict(
        connection.execute(select(table).where(table.c.id == a[child_key])).mappings().one()
    )
    source.update(id=str(uuid4()), **{column: b[parent_key]})
    if table.name == "issued_invoices":
        source["number"] = "CROSS-" + uuid4().hex
    with (
        pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"),
        connection.begin_nested(),
    ):
        connection.execute(table.insert().values(**source))
    assert _rows(connection) == before


@pytest.mark.parametrize("model,column,parent,parent_key,child_key,name,nullable", LINKS)
def test_exact_pair_preserves_mandatory_and_optional_reference_contracts(
    model, column, parent, parent_key, child_key, name, nullable
):
    table = model.__table__
    pair = [fk for fk in table.foreign_key_constraints if fk.name == name]
    assert len(pair) == 1
    pair = pair[0]
    assert tuple(pair.column_keys) == ("org_id", column)
    assert tuple(element.target_fullname for element in pair.elements) == (
        parent + ".org_id",
        parent + ".id",
    )
    assert pair.ondelete == (None if nullable else "CASCADE")
    assert pair.deferrable is None and pair.initially is None
    assert not table.c.org_id.nullable and table.c[column].nullable is nullable
    singles = [fk for fk in table.foreign_key_constraints if tuple(fk.column_keys) == (column,)]
    if nullable:
        assert len(singles) == 1 and singles[0].name is None
        assert singles[0].ondelete == "SET NULL"
        assert tuple(e.target_fullname for e in singles[0].elements) == (parent + ".id",)
    else:
        assert singles == []


@pytest.mark.parametrize("parent", ["customers", "invoices", "issued_invoices"])
def test_existing_named_unique_constraint_is_the_exact_target(parent):
    from sqlalchemy import UniqueConstraint

    table = Base.metadata.tables[parent]
    candidates = [
        constraint
        for constraint in table.constraints
        if constraint.name == "uq_" + parent + "_org_id"
    ]
    assert len(candidates) == 1 and isinstance(candidates[0], UniqueConstraint)
    assert tuple(candidates[0].columns) == (table.c.org_id, table.c.id)
    assert candidates[0].deferrable is None and candidates[0].initially is None
    assert not candidates[0].dialect_kwargs
    assert not any(index.name == candidates[0].name for index in table.indexes)


def _isolate_other_guards(connection, table, column, a, b, *, parent_change=False):
    if parent_change:
        if table == "customer_contacts":
            connection.execute(
                text("UPDATE issued_invoices SET customer_id=NULL WHERE customer_id=:id"),
                {"id": a["customer"]},
            )
        elif column == "customer_id":
            connection.execute(
                text("DELETE FROM customer_contacts WHERE customer_id=:id"), {"id": a["customer"]}
            )
        elif column == "corrected_invoice_id":
            connection.execute(
                text("DELETE FROM payments WHERE issued_invoice_id=:id"), {"id": a["original"]}
            )
            connection.execute(
                text("DELETE FROM issued_invoice_attachments WHERE invoice_id=:id"),
                {"id": a["original"]},
            )
    elif table == "inbound_invoices":
        connection.execute(
            text("DELETE FROM capture_reading_observations WHERE inbound_invoice_id=:id"),
            {"id": a["inbound"]},
        )


@pytest.mark.parametrize("model,column,parent,parent_key,child_key,name,nullable", LINKS)
@pytest.mark.parametrize("operation", ["reference", "child_org", "parent_org"])
def test_each_reference_and_tenant_update_is_refused_immediately(
    residual_family, model, column, parent, parent_key, child_key, name, nullable, operation
):
    connection, (a, b) = residual_family
    before = _rows(connection)
    table = model.__table__
    with (
        pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"),
        connection.begin_nested(),
    ):
        if operation == "reference":
            connection.execute(
                table.update().where(table.c.id == a[child_key]).values(**{column: b[parent_key]})
            )
        elif operation == "child_org":
            _isolate_other_guards(connection, table.name, column, a, b)
            connection.execute(
                table.update().where(table.c.id == a[child_key]).values(org_id=b["org"])
            )
        else:
            _isolate_other_guards(connection, table.name, column, a, b, parent_change=True)
            values = {"org_id": b["org"]}
            if parent == "invoices":
                values["vendor_id"] = b["vendor"]
            target = Base.metadata.tables[parent]
            connection.execute(target.update().where(target.c.id == a[parent_key]).values(**values))
    assert _rows(connection) == before


@pytest.mark.parametrize("model,column,parent,parent_key,child_key,name,nullable", LINKS)
def test_own_reference_and_original_nullability_remain_valid(
    residual_family, model, column, parent, parent_key, child_key, name, nullable
):
    connection, (a, _b) = residual_family
    before = _rows(connection)
    table = model.__table__
    original = dict(
        connection.execute(select(table).where(table.c.id == a[child_key])).mappings().one()
    )
    sp = connection.begin_nested()
    try:
        values = dict(original, id=str(uuid4()), **{column: a[parent_key]})
        if table.name == "issued_invoices":
            values["number"] = "OWN-" + uuid4().hex
        connection.execute(table.insert().values(**values))
        assert (
            connection.scalar(select(table.c[column]).where(table.c.id == values["id"]))
            == a[parent_key]
        )
        if nullable:
            connection.execute(
                table.update().where(table.c.id == values["id"]).values(**{column: None})
            )
            assert (
                connection.scalar(select(table.c[column]).where(table.c.id == values["id"])) is None
            )
        else:
            with (
                pytest.raises(IntegrityError, match="NOT NULL constraint failed"),
                connection.begin_nested(),
            ):
                connection.execute(
                    table.update().where(table.c.id == values["id"]).values(**{column: None})
                )
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    finally:
        sp.rollback()
    assert _rows(connection) == before


@pytest.mark.parametrize("model,column,parent,parent_key,child_key,name,nullable", LINKS[1:])
def test_optional_scalar_assignment_does_not_invent_an_organization(
    model, column, parent, parent_key, child_key, name, nullable
):
    with warnings.catch_warnings():
        warnings.simplefilter("error", SAWarning)
        configure_mappers()
        assert not [
            rel for rel in inspect(model).relationships if rel.mapper.class_.__tablename__ == parent
        ]
        candidate = model(org_id="retained-org")
        setattr(candidate, column, "untrusted-reference")
        assert (
            candidate.org_id == "retained-org"
            and getattr(candidate, column) == "untrusted-reference"
        )


def _after_delete(before, parent, targets):
    expected = deepcopy(before)
    expected[parent] = [row for row in expected[parent] if row["id"] not in targets]
    if parent == "customers":
        expected["customer_contacts"] = [
            row for row in expected["customer_contacts"] if row["customer_id"] not in targets
        ]
    if parent == "invoices":
        expected["line_items"] = [
            row for row in expected["line_items"] if row["invoice_id"] not in targets
        ]
    if parent == "issued_invoices":
        for table, column in (
            ("issued_invoice_lines", "invoice_id"),
            ("issued_invoice_attachments", "invoice_id"),
            ("payments", "issued_invoice_id"),
        ):
            expected[table] = [row for row in expected[table] if row[column] not in targets]
    for model, column, target, _parent_key, _child_key, _name, nullable in LINKS:
        if target == parent and nullable:
            for row in expected[model.__tablename__]:
                if row[column] in targets:
                    row[column] = None
    return expected


@pytest.mark.parametrize(
    "parent,key",
    [("customers", "customer"), ("invoices", "received"), ("issued_invoices", "original")],
)
@pytest.mark.parametrize("count", [1, 2])
def test_direct_and_multirow_parent_deletes_retain_legal_rows_and_rollback(
    residual_family, parent, key, count
):
    connection, ids = residual_family
    before = _rows(connection)
    targets = {identity[key] for identity in ids[:count]}
    sp = connection.begin_nested()
    try:
        table = Base.metadata.tables[parent]
        connection.execute(table.delete().where(table.c.id.in_(targets)))
        assert _rows(connection) == _after_delete(before, parent, targets)
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
        # Credit-number and financial snapshots survive severed row links.
        for credit in connection.execute(
            text(
                "SELECT corrected_invoice_number,total,org_id FROM issued_invoices WHERE doc_type='credit_note'"
            )
        ).all():
            assert credit[0].startswith("ORIGINAL-") and str(credit[1]) == "7.89"
    finally:
        sp.rollback()
    assert _rows(connection) == before


def test_contact_relationship_replacement_and_soft_delete_preserve_invoice_reference(
    residual_family,
):
    connection, (a, _b) = residual_family
    before = _rows(connection)
    sp = connection.begin_nested()
    try:
        with Session(
            connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        ) as db:
            customer = db.scalar(
                select(Customer)
                .where(Customer.id == a["customer"])
                .options(selectinload(Customer.contacts))
            )
            assert [contact.id for contact in customer.contacts] == [a["contact"]]
            customer.contacts = [CustomerContact(org_id=a["org"], name="Replacement contact")]
            customer.is_active = False
            db.flush()
            assert customer.contacts[0].org_id == a["org"]
            assert db.get(CustomerContact, a["contact"]) is None
            assert db.get(IssuedInvoice, a["billed"]).customer_id == a["customer"]
            assert customer.is_active is False
            db.commit()
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    finally:
        sp.rollback()
    assert _rows(connection) == before


@pytest.mark.parametrize("cycle", [False, True], ids=["self-reference", "same-tenant-cycle"])
def test_original_same_tenant_self_reference_and_cycle_delete_contract_is_preserved(
    residual_family, cycle
):
    connection, (a, _b) = residual_family
    before = _rows(connection)
    sp = connection.begin_nested()
    try:
        # Tenant integrity does not introduce a new document-kind/cycle policy.
        connection.execute(
            text("UPDATE issued_invoices SET corrected_invoice_id=:target WHERE id=:id"),
            {"target": a["billed"] if cycle else a["original"], "id": a["original"]},
        )
        if cycle:
            connection.execute(
                text("UPDATE issued_invoices SET corrected_invoice_id=:target WHERE id=:id"),
                {"target": a["original"], "id": a["billed"]},
            )
        arranged = _rows(connection)
        targets = {a["original"], a["billed"]} if cycle else {a["original"]}
        table = IssuedInvoice.__table__
        connection.execute(table.delete().where(table.c.id.in_(targets)))
        assert _rows(connection) == _after_delete(arranged, "issued_invoices", targets)
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    finally:
        sp.rollback()
    assert _rows(connection) == before


def test_real_organization_cascade_preserves_other_tenant_and_restores_every_row(residual_family):
    connection, (a, _b) = residual_family
    before = _rows(connection)
    expected = {
        table: [row for row in values if row.get("org_id") != a["org"]]
        for table, values in deepcopy(before).items()
    }
    expected["organizations"] = [row for row in expected["organizations"] if row["id"] != a["org"]]
    removed_issued = {row["id"] for row in before["issued_invoices"] if row["org_id"] == a["org"]}
    expected["issued_invoice_lines"] = [
        row for row in expected["issued_invoice_lines"] if row["invoice_id"] not in removed_issued
    ]
    sp = connection.begin_nested()
    try:
        connection.execute(text("DELETE FROM organizations WHERE id=:id"), {"id": a["org"]})
        assert _rows(connection) == expected
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    finally:
        sp.rollback()
    assert _rows(connection) == before


@contextmanager
def _ordered_optional_family(link, pair_first):
    """Reorder only two clauses while the private schema is still empty.

    Foreign-key enforcement is enabled before seeding any data and remains on
    for every admissibility/delete statement. This is not a migration toggle.
    """
    model, column, _parent, _parent_key, _child_key, name, _nullable = link
    table = model.__tablename__
    engine = create_engine("sqlite:///:memory:")
    try:
        Base.metadata.create_all(engine)
        with engine.connect() as connection:
            assert connection.scalar(text("PRAGMA foreign_keys")) == 0
            assert all(not values for values in _rows(connection).values())
            before_schema = schemas(connection)
            original = connection.scalar(
                text("SELECT sql FROM sqlite_schema WHERE type='table' AND name=:name"),
                {"name": table},
            )
            lines = original.splitlines()
            pair = next(i for i, line in enumerate(lines) if "CONSTRAINT " + name + " " in line)
            single = next(
                i
                for i, line in enumerate(lines)
                if re.search(rf"FOREIGN KEY\({column}\) REFERENCES", line)
            )
            lower, higher = sorted((pair, single))
            pair_line, single_line = lines[pair], lines[single]
            desired = (pair_line, single_line) if pair_first else (single_line, pair_line)
            rewritten = list(lines)
            for position, clause in zip((lower, higher), desired, strict=True):
                rewritten[position] = clause.rstrip().removesuffix(",") + (
                    "," if lines[position].rstrip().endswith(",") else ""
                )
            assert sorted(line.strip().rstrip(",") for line in lines) == sorted(
                line.strip().rstrip(",") for line in rewritten
            )
            indexes = list(
                connection.execute(
                    text(
                        "SELECT sql FROM sqlite_schema WHERE type='index' AND tbl_name=:name AND sql IS NOT NULL"
                    ),
                    {"name": table},
                ).scalars()
            )
            connection.exec_driver_sql(f'DROP TABLE "{table}"')
            connection.exec_driver_sql("\n".join(rewritten))
            for sql in indexes:
                connection.exec_driver_sql(sql)
            assert schemas(connection) == before_schema
            connection.commit()
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.commit()
            assert connection.scalar(text("PRAGMA foreign_keys")) == 1
            actual = connection.scalar(
                text("SELECT sql FROM sqlite_schema WHERE type='table' AND name=:name"),
                {"name": table},
            )
            assert (
                actual.index(pair_line.strip().rstrip(","))
                < actual.index(single_line.strip().rstrip(","))
            ) is pair_first
            identities = _seed(connection)
            yield connection, identities
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "link", LINKS[1:], ids=["inbound-invoice", "issued-customer", "issued-correction"]
)
@pytest.mark.parametrize("pair_first", [False, True], ids=["single-first", "pair-first"])
def test_each_optional_delete_works_in_both_actual_sqlite_clause_orders(link, pair_first):
    _model, _column, parent, parent_key, _child_key, _name, _nullable = link
    with _ordered_optional_family(link, pair_first) as (connection, ids):
        before = _rows(connection)
        targets = {identity[parent_key] for identity in ids}
        sp = connection.begin_nested()
        try:
            table = Base.metadata.tables[parent]
            connection.execute(table.delete().where(table.c.id.in_(targets)))
            assert _rows(connection) == _after_delete(before, parent, targets)
            assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
            assert connection.scalar(text("PRAGMA foreign_keys")) == 1
        finally:
            sp.rollback()
        assert _rows(connection) == before
