"""Grouped reads retain frozen best-effort report semantics; no time budget gate."""

import ast
import hashlib
import json
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import event, select

from app.models import Base
from app.models.invoice import Invoice
from app.models.issued_invoice import IssuedInvoice
from app.models.organization import Organization
from app.models.payment import Payment
from app.models.receipt import Receipt
from app.models.supplier_payment import SupplierPayment
from app.models.vendor import Vendor
from app.services import integrity

LEGACY = Path(__file__).parent / "fixtures" / "integrity_verify_ledger_legacy.txt"


def legacy_verify():
    """Exact frozen6ed function only; unchanged live models/q2/_ledger_sum namespace."""
    source = LEGACY.read_text()
    assert (
        hashlib.sha256(source.encode()).hexdigest()
        == "0179dc71c397672721745348e86abfbf164ee1b2905282d122cc268422357b09"
    )
    namespace = dict(vars(integrity))
    exec(compile(ast.parse(source), str(LEGACY), "exec"), namespace)
    return namespace["verify_ledger"]


@contextmanager
def commands(db):
    observed = []
    engine = db.get_bind()

    def capture(_conn, _cursor, statement, _parameters, _context, _many):
        observed.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        yield observed
    finally:
        event.remove(engine, "before_cursor_execute", capture)


def business_reads(statements):
    return [s for s in statements if s.lstrip().startswith("SELECT") and "set_config(" not in s]


async def snapshot(db, org_id):
    result = {}
    for table in Base.metadata.sorted_tables:
        if "org_id" not in table.c or table.name == "sessions":
            continue
        rows = (await db.execute(select(table).where(table.c.org_id == org_id))).mappings().all()
        encoded = sorted(json.dumps(dict(row), sort_keys=True, default=str) for row in rows)
        result[table.name] = {
            "count": len(rows),
            "sha256": hashlib.sha256(json.dumps(encoded).encode()).hexdigest(),
        }
    return result


def issued(org_id, name, cache):
    return IssuedInvoice(
        id=str(uuid4()),
        org_id=org_id,
        number=name,
        buyer_name="Synthetic groups",
        issue_date=date(2026, 11, 6),
        currency="EUR",
        seller_json="{}",
        subtotal=Decimal("100"),
        tax_total=Decimal("21"),
        total=Decimal("121"),
        amount_paid=Decimal(cache),
    )


async def seed_rich(db, org_id):
    """Nonempty signed/missing/standalone/reversed/current+binned AP cases."""
    specs = [
        ("zero", "0", []),
        ("missing", "7.77", []),
        ("partial", "10.01", ["30.03", "-20.02"]),
        ("negative", "-5", ["-5"]),
        ("reversed", "0", ["9.99", "-9.99"]),
        ("drift", "99.99", ["30.03"]),
        ("cents", "0.03", ["0.01", "0.02"]),
        ("over", "7", ["7"]),
        ("refund", "-2.50", ["-2.50"]),
        ("mixed", "2", ["5", "-3"]),
    ]
    receipts = {}
    for label, amount in [
        ("empty", "2"),
        ("partial", "15"),
        ("over", "5"),
        ("refund", "1"),
        ("mixed", "2"),
    ]:
        row = Receipt(
            id=str(uuid4()),
            org_id=org_id,
            amount=Decimal(amount),
            received_on=date(2026, 11, 6),
            reference=label,
        )
        receipts[label] = row
        db.add(row)
    await db.flush()
    invoices = []
    for name, cache, values in specs:
        invoice = issued(org_id, name, cache)
        db.add(invoice)
        invoices.append(invoice)
        await db.flush()
        previous = None
        for index, amount in enumerate(values):
            payment = Payment(
                id=str(uuid4()),
                org_id=org_id,
                issued_invoice_id=invoice.id,
                amount=Decimal(amount),
                paid_on=date(2026, 11, 6),
                receipt_id=receipts[name].id if name in receipts else None,
                reversal_of_id=previous.id if name == "reversed" and index == 1 else None,
            )
            db.add(payment)
            previous = payment
    vendor = Vendor(id=str(uuid4()), org_id=org_id, name="Synthetic AP")
    db.add(vendor)
    # Flush parents before child inserts on SQLite, whose deferred self-FK also
    # preserves the reversal pair. This is fixture setup, not service mutation.
    await db.flush()
    for label, cache, amounts, deleted in [
        ("AP-zero", "0", [], False),
        ("AP-drift", "6", ["5"], False),
        ("AP-signed", "2", ["5", "-3"], False),
        ("AP-binned", "8", ["9"], True),
    ]:
        row = Invoice(
            id=str(uuid4()),
            org_id=org_id,
            vendor_id=vendor.id,
            invoice_number=label,
            issue_date=date(2026, 11, 6),
            currency="EUR",
            subtotal=Decimal("10"),
            tax_amount=Decimal("0"),
            total=Decimal("10"),
            amount_paid=Decimal(cache),
            deleted_at=datetime.now(UTC) if deleted else None,
        )
        db.add(row)
        await db.flush()
        for amount in amounts:
            db.add(
                SupplierPayment(
                    org_id=org_id,
                    invoice_id=row.id,
                    amount=Decimal(amount),
                    paid_on=date(2026, 11, 6),
                )
            )
    await db.commit()
    return {"issued_ids": [x.id for x in invoices], "over_receipt": receipts["over"].id}


async def seed_growth(db, org_id, count):
    vendor = Vendor(id=str(uuid4()), org_id=org_id, name="Synthetic growth")
    db.add(vendor)
    await db.flush()
    for i in range(count):
        invoice = issued(org_id, f"G-{i}", "1")
        receipt = Receipt(
            id=str(uuid4()), org_id=org_id, amount=Decimal("1"), received_on=date(2026, 11, 6)
        )
        db.add_all([invoice, receipt])
        await db.flush()
        db.add(
            Payment(
                org_id=org_id,
                issued_invoice_id=invoice.id,
                receipt_id=receipt.id,
                amount=Decimal("1"),
                paid_on=date(2026, 11, 6),
            )
        )
        db.add(
            Invoice(
                org_id=org_id,
                vendor_id=vendor.id,
                invoice_number=f"AP-G-{i}",
                issue_date=date(2026, 11, 6),
                currency="EUR",
                subtotal=Decimal("1"),
                tax_amount=Decimal("0"),
                total=Decimal("1"),
                amount_paid=Decimal("0"),
            )
        )
    await db.commit()


@pytest.mark.asyncio
async def test_signed_reports_match_frozen_function_in_original_order_without_writes(
    auth_client, db_session
):
    org = (await auth_client.get("/api/v1/auth/me")).json()["organization"]["id"]
    own = await seed_rich(db_session, org)
    foreign = Organization(name="Synthetic foreign groups")
    db_session.add(foreign)
    await db_session.commit()
    other = await seed_rich(db_session, foreign.id)
    before = await snapshot(db_session, org)
    reference = await legacy_verify()(db_session, org)
    caller_transaction = db_session.sync_session.get_transaction()
    assert caller_transaction is not None
    with commands(db_session) as observed:
        report = await integrity.verify_ledger(db_session, org)
    assert db_session.sync_session.get_transaction() is caller_transaction
    assert asdict(report) == asdict(reference)
    assert report.checked == 18 and report.ok == 14
    assert [i.kind for i in report.issues] == [
        "invoice_ledger",
        "invoice_ledger",
        "supplier_invoice_ledger",
        "receipt_allocation",
    ]
    assert [i.detail for i in report.issues] == [
        "amount_paid 7.77 != ledger sum 0.00",
        "amount_paid 99.99 != ledger sum 30.03",
        "amount_paid 6.00 != supplier ledger sum 5.00",
        "allocated 7.00 > received 5.00",
    ]
    assert report.issues[-1].entity_id == own["over_receipt"]
    assert not {i.entity_id for i in report.issues}.intersection(other["issued_ids"])
    assert len(business_reads(observed)) <= 6
    assert all(s.lstrip().startswith("SELECT") for s in observed)
    assert await snapshot(db_session, org) == before
    assert db_session.in_transaction()  # Caller still owns the transaction.


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 1, 64])
async def test_populated_query_count_does_not_grow_with_parent_count(
    auth_client, db_session, count
):
    org = (await auth_client.get("/api/v1/auth/me")).json()["organization"]["id"]
    await seed_growth(db_session, org, count)
    with commands(db_session) as observed:
        report = await integrity.verify_ledger(db_session, org)
    assert report.healthy and report.checked == 3 * count
    assert len(business_reads(observed)) <= 6
    assert not any(s.lstrip().startswith(("UPDATE", "INSERT", "DELETE")) for s in observed)


@pytest.mark.asyncio
@pytest.mark.parametrize("amount", ["0", "-1"])
async def test_missing_allocation_keeps_legacy_zero_precision(auth_client, db_session, amount):
    org = (await auth_client.get("/api/v1/auth/me")).json()["organization"]["id"]
    db_session.add(Receipt(org_id=org, amount=Decimal(amount), received_on=date(2026, 11, 6)))
    await db_session.commit()
    reference = await legacy_verify()(db_session, org)
    report = await integrity.verify_ledger(db_session, org)
    assert asdict(report) == asdict(reference)
    if amount == "-1":
        assert report.issues[0].detail == "allocated 0.00 > received -1.00"
