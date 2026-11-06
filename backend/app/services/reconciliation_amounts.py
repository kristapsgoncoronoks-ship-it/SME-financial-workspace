"""Read existing bank money without replacing unavailable stored facts with zero."""

from decimal import Decimal, DecimalException

from sqlalchemy import Numeric

from app.models.bank_import import BankLine


def finite_decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (Decimal, str, int)):
        return None
    try:
        parsed = Decimal(value)
    except (DecimalException, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def bank_decimal(value: object) -> Decimal | None:
    parsed = finite_decimal(value)
    column = BankLine.__table__.c.amount.type
    assert isinstance(column, Numeric)
    assert column.precision is not None and column.scale is not None
    maximum = Decimal("9" * (column.precision - column.scale) + "." + "9" * column.scale)
    return parsed if parsed is not None and parsed.copy_abs() <= maximum else None
