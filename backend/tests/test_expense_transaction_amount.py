"""The expense read adapter preserves finite facts and labels unavailable money."""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.models.audit import AuditEvent
from app.models.expense import ExpenseCommand, ExpenseItem, ExpenseReport
from app.schemas.expense import ExpenseTransactionOut
from tests.test_r010_expense_commands import activate, report_payload, seed_transactions


def transaction(amount):
    return {
        "id": str(uuid4()),
        "txn_date": date(2026, 11, 6),
        "description": "Synthetic historical expense",
        "merchant": None,
        "amount": amount,
        "currency": "USD",
        "direction": "debit",
        "source": "card",
        "status": "available",
        "version": 3,
    }


@pytest.mark.parametrize(
    "value",
    [
        Decimal("12.34"),
        Decimal("-12.34"),
        Decimal("0.00"),
        Decimal("-0.00"),
        Decimal("999999999999.99"),
        Decimal("-999999999999.99"),
        Decimal("999999999999.994"),
        Decimal("-999999999999.994"),
        "12.3400",
        "1E+3",
        12,
        12.34,
    ],
)
def test_read_preserves_supported_finite_value_sign_and_currency(value):
    original = transaction(value)
    result = ExpenseTransactionOut.model_validate(original)
    assert result.amount == Decimal(str(value))
    assert result.amount.as_tuple() == Decimal(str(value)).as_tuple()
    assert result.amount_status == "valid"
    assert result.currency == "USD"
    assert original["amount"] is value
    assert "amount_status" not in original


@pytest.mark.parametrize(
    "value",
    [
        Decimal("NaN"),
        Decimal("sNaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
        float("nan"),
        float("inf"),
        float("-inf"),
        Decimal("1000000000000.00"),
        Decimal("-1000000000000.00"),
        Decimal("999999999999.995"),
        Decimal("-999999999999.995"),
        Decimal("1E+100000"),
        None,
        True,
        False,
        "unavailable",
        "",
        b"12.34",
        {},
        [],
    ],
)
def test_unavailable_money_becomes_explicit_null_without_mutating_source(value):
    original = transaction(value)
    original["amount_status"] = "valid"
    result = ExpenseTransactionOut.model_validate(original)
    assert result.amount is None
    assert result.amount_status == "invalid"
    assert result.currency == "USD"
    assert original["amount"] is value
    assert original["amount_status"] == "valid"


def test_attribute_read_does_not_assign_to_historical_orm_like_source():
    source = SimpleNamespace(**transaction(Decimal("NaN")))
    before = vars(source).copy()
    result = ExpenseTransactionOut.model_validate(source)
    assert result.amount is None and result.amount_status == "invalid"
    assert vars(source).keys() == before.keys()
    assert source.amount is before["amount"]
    assert not hasattr(source, "amount_status")


async def test_sqlite_historical_oversize_is_visible_but_not_consumed(auth_client, db_session):
    # SQLite can retain an old out-of-typmod value that constrained PostgreSQL
    # refuses to store. Exercise the supported portable API, not a PG claim.
    await activate(auth_client)
    rows = await seed_transactions(db_session)
    row = rows[0]
    row.amount = Decimal("1000000000000.00")
    await db_session.commit()

    async def counts():
        return [
            await db_session.scalar(select(func.count()).select_from(model))
            for model in (ExpenseReport, ExpenseItem, ExpenseCommand, AuditEvent)
        ]

    before = await counts()
    read = await auth_client.get("/api/v1/expenses/transactions")
    assert read.status_code == 200, read.text
    current = next(item for item in read.json() if item["id"] == row.id)
    assert current["amount"] is None and current["amount_status"] == "invalid"
    assert current["currency"] == "EUR"
    refused = await auth_client.post(
        "/api/v1/expenses",
        headers={"Idempotency-Key": str(uuid4())},
        json=report_payload(rows),
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "expense_transaction_amount_invalid"
    assert await counts() == before
    await db_session.refresh(row)
    assert row.amount == Decimal("1000000000000.00")
    assert row.status == "available" and row.item_id is None and row.version == 1
