"""Response-only sanitation keeps known, missing and invalid stored money distinct."""

from datetime import date
from decimal import Decimal, getcontext
from types import SimpleNamespace

import pytest

from app.models.bank_import import BankLine
from app.schemas.reconciliation import BankLineOut
from app.services.reconciliation_amounts import bank_decimal, finite_decimal


@pytest.mark.parametrize(
    "value", ["NaN", "sNaN", "Infinity", "-Infinity", "1e100000", "1000000000000.00"]
)
@pytest.mark.parametrize("field", ["amount", "balance"])
def test_unreadable_money_is_null_and_explicit_without_mutating_the_source(value, field):
    values = dict(
        id="line",
        statement_id="statement",
        line_date=date(2026, 11, 6),
        description="Synthetic stored line",
        amount=Decimal("12.34"),
        balance=Decimal("20.00"),
        currency=None,
        direction="credit",
        status="unmatched",
    )
    values[field] = Decimal(value)
    source = SimpleNamespace(**values)
    context = getcontext()
    before = (context.prec, context.rounding, context.Emax, context.Emin, dict(context.traps))
    result = BankLineOut.model_validate(source)
    assert getattr(result, field) is None and getattr(result, field + "_status") == "invalid"
    assert source.__dict__ == values
    assert "NaN" not in result.model_dump_json() and "Infinity" not in result.model_dump_json()
    assert (
        context.prec,
        context.rounding,
        context.Emax,
        context.Emin,
        dict(context.traps),
    ) == before


@pytest.mark.parametrize(
    "value", ["0.00", "-0.00", "12.34", "-12.34", "999999999999.99", "-999999999999.99"]
)
def test_current_finite_money_keeps_its_exact_wire_value_and_missing_balance_is_unprovided(value):
    result = BankLineOut.model_validate(
        dict(
            id="line",
            statement_id="statement",
            line_date=date(2026, 11, 6),
            description="Synthetic finite line",
            amount=Decimal(value),
            balance=None,
            direction="credit",
            status="unmatched",
        )
    )
    body = result.model_dump(mode="json")
    assert body["amount"] == value and body["amount_status"] == "valid"
    assert body["balance"] is None and body["balance_status"] == "unprovided"
    assert body["currency"] is None


def test_bound_comes_from_bank_storage_and_is_not_a_new_target_amount_cap():
    column = BankLine.__table__.c.amount.type
    assert (column.precision, column.scale) == (14, 2)
    maximum = Decimal("999999999999.99")
    assert bank_decimal(maximum) == maximum and bank_decimal(-maximum) == -maximum
    assert bank_decimal(maximum + Decimal("0.01")) is None
    larger_finite_target = Decimal("100000000000000000.00")
    assert finite_decimal(larger_finite_target) == larger_finite_target
    schema = BankLineOut.model_json_schema()
    assert schema["properties"]["amount_status"]["enum"] == ["valid", "invalid"]
    assert schema["properties"]["balance_status"]["enum"] == ["valid", "invalid", "unprovided"]
