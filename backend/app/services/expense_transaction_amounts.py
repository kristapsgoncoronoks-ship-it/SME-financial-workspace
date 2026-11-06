"""Read historical expense money without changing stored financial facts."""

from decimal import Decimal, DecimalException

from sqlalchemy import Numeric

from app.core import money
from app.core.errors import ConflictError
from app.models.expense import ExpenseTransaction

_column = ExpenseTransaction.__table__.c.amount.type
assert isinstance(_column, Numeric)
assert _column.precision is not None and _column.scale is not None
_MAXIMUM = Decimal(
    "9" * (_column.precision - _column.scale) + ("." + "9" * _column.scale if _column.scale else "")
)
_EXPONENT = Decimal(1).scaleb(-_column.scale)


def read_amount(value: object) -> Decimal | None:
    """Return the original finite value if its existing storage rounding fits."""
    if isinstance(value, bool) or not isinstance(value, (Decimal, str, int, float)):
        return None
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
        if not amount.is_finite() or money.q(amount, _EXPONENT).copy_abs() > _MAXIMUM:
            return None
    except (DecimalException, ValueError):
        return None
    return amount


def require_amount(row: ExpenseTransaction) -> Decimal:
    amount = read_amount(row.amount)
    if amount is None:
        raise ConflictError(
            "The stored expense amount is unavailable. Review the original statement "
            "before using this transaction in a report.",
            code="expense_transaction_amount_invalid",
        )
    return amount
