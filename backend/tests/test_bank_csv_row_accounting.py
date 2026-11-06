"""CSV exclusion accounting without changing transaction interpretation or writes."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models.bank_import import BankLine, BankStatement
from app.models.receipt import Receipt
from app.services import bank_statement

DATE_SUMMARY = (
    "Skipped 1 CSV row: 1 with an invalid or missing date; 0 with no nonzero recognized amount."
)
AMOUNT_SUMMARY = (
    "Skipped 1 CSV row: 0 with an invalid or missing date; 1 with no nonzero recognized amount."
)
VALID = bank_statement.Txn(
    date=date(2026, 11, 5),
    description="Synthetic valid transaction",
    amount=Decimal("12.34"),
    direction="debit",
    currency="EUR",
)


def test_csv_invalid_date_reports_safe_row_ordinal_and_keeps_valid_transaction():
    result = bank_statement.parse(
        "statement.csv",
        b"date,description,amount,currency\n"
        b"2026-11-05,Synthetic valid transaction,-12.34,EUR\n"
        b"synthetic-private-invalid-date,synthetic-private-payee,-20,EUR\n",
    )
    assert result.method == "csv" and result.transactions == [VALID]
    assert result.warnings == [DATE_SUMMARY, "CSV row 3 skipped: invalid or missing date."]
    assert "synthetic-private" not in " ".join(result.warnings)


@pytest.mark.parametrize("amount", ["0", "", "not-a-money-value", "1.2.3"])
def test_zero_or_unrecognized_amount_is_described_without_claiming_numeric_validation(amount):
    result = bank_statement.parse(
        "statement.csv",
        (
            "date,description,amount,currency\n"
            f"2026-11-05,synthetic-private-payee,{amount},EUR\n"
            "2026-11-05,Synthetic valid transaction,-12.34,EUR\n"
        ).encode(),
    )
    assert result.transactions == [VALID]
    assert result.warnings == [
        AMOUNT_SUMMARY,
        "CSV row 2 skipped: no nonzero amount was recognized.",
    ]
    assert "synthetic-private" not in " ".join(result.warnings)


@pytest.mark.parametrize(("debit", "credit"), [("0", "not-a-money-value"), ("", "")])
def test_separate_debit_credit_columns_report_a_row_with_no_nonzero_recognized_amount(
    debit,
    credit,
):
    result = bank_statement.parse(
        "statement.csv",
        (
            "date,description,debit,credit,currency\n"
            f"2026-11-05,synthetic-private-payee,{debit},{credit},EUR\n"
            "2026-11-05,Synthetic valid transaction,12.34,,EUR\n"
        ).encode(),
    )
    assert result.transactions == [VALID]
    assert result.warnings == [
        AMOUNT_SUMMARY,
        "CSV row 2 skipped: no nonzero amount was recognized.",
    ]


def test_valid_csv_direction_currency_rounding_and_debit_precedence_remain_unchanged():
    signed = bank_statement.parse(
        "signed.csv",
        b"date,description,amount,currency\n"
        b"2026-11-05,Synthetic debit,-12.346, usd \n"
        b"2026-11-05,Synthetic credit,4.56,\n",
    )
    assert signed.transactions == [
        bank_statement.Txn(
            date(2026, 11, 5), "Synthetic debit", Decimal("12.35"), "debit", currency="USD"
        ),
        bank_statement.Txn(date(2026, 11, 5), "Synthetic credit", Decimal("4.56"), "credit"),
    ]
    split = bank_statement.parse(
        "split.csv",
        b"date,description,debit,credit\n2026-11-05,Synthetic debit,12.34,99\n"
        b"2026-11-05,Synthetic credit,,4.56\n",
    )
    assert [(row.amount, row.direction, row.currency) for row in split.transactions] == [
        (Decimal("12.34"), "debit", None),
        (Decimal("4.56"), "credit", None),
    ]
    assert signed.warnings == split.warnings == []


def test_many_skipped_rows_have_twenty_safe_details_and_exact_reason_and_omitted_counts():
    rows = ["date,description,amount,currency"]
    for i in range(1001):
        rows.append(
            "synthetic-private-date,synthetic-private-payee,-20,EUR"
            if i % 2 == 0
            else "2026-11-05,synthetic-private-payee,not-money,EUR"
        )
    rows.append("2026-11-05,Synthetic valid transaction,-12.34,EUR")
    result = bank_statement.parse("statement.csv", "\n".join(rows).encode())
    assert result.transactions == [VALID]
    assert len(result.warnings) == 21
    assert result.warnings[0] == (
        "Skipped 1001 CSV rows: 501 with an invalid or missing date; "
        "500 with no nonzero recognized amount. Additional skipped rows not listed: 981."
    )
    assert result.warnings[1] == "CSV row 2 skipped: invalid or missing date."
    assert result.warnings[-1] == "CSV row 21 skipped: no nonzero amount was recognized."
    assert "synthetic-private" not in " ".join(result.warnings)
    assert len("\n".join(result.warnings).encode()) < 2000


def test_row_ordinals_count_csv_records_and_missing_date_is_not_double_counted():
    result = bank_statement.parse(
        "statement.csv",
        b'date,description,amount,currency\n2026-11-05,"Synthetic valid\ntransaction",-12.34,EUR\n'
        b",synthetic-private-payee,not-money,EUR\n",
    )
    assert len(result.transactions) == 1
    assert result.transactions[0].description == "Synthetic valid\ntransaction"
    assert result.warnings == [DATE_SUMMARY, "CSV row 3 skipped: invalid or missing date."]


def test_header_only_csv_does_not_invent_rejected_transactions():
    result = bank_statement.parse("empty.csv", b"date,description,amount\n")
    assert result.transactions == [] and result.warnings == []


async def _reconciliation_import(client, content):
    return await client.post(
        "/api/v1/reconciliation/import",
        files={"file": ("synthetic.csv", content, "text/csv")},
    )


@pytest.mark.asyncio
async def test_reconciliation_returns_exclusion_warnings_and_duplicate_bytes_add_no_facts(
    auth_client,
    db_session,
):
    await auth_client.put("/api/v1/modules/issuing", json={"enabled": True})
    receipt = await auth_client.post(
        "/api/v1/receipts",
        json={"amount": "7.89", "received_on": "2026-11-05", "reference": "Synthetic control"},
    )
    assert receipt.status_code == 201, receipt.text
    content = (
        b"date,description,amount,currency\n"
        b"2026-11-05,Synthetic valid transaction,-12.34,EUR\n"
        b"synthetic-private-date,synthetic-private-payee,-20,EUR\n"
    )
    response = await _reconciliation_import(auth_client, content)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["method"] == "csv" and body["imported"] == 1
    assert body["warnings"] == [DATE_SUMMARY, "CSV row 3 skipped: invalid or missing date."]
    lines = await auth_client.get(f"/api/v1/reconciliation/statements/{body['statement_id']}/lines")
    assert lines.status_code == 200 and len(lines.json()) == 1
    line = lines.json()[0]
    assert {
        key: line[key] for key in ("description", "amount", "currency", "status", "matched_id")
    } == {
        "description": VALID.description,
        "amount": "-12.34",
        "currency": "EUR",
        "status": "unmatched",
        "matched_id": None,
    }
    snapshot = list(
        (await db_session.execute(select(BankLine.id, BankLine.amount, BankLine.status))).all()
    )
    duplicate = await _reconciliation_import(auth_client, content)
    assert (
        duplicate.status_code == 409
        and duplicate.json()["detail"] == "this statement was already imported"
    )
    assert await db_session.scalar(select(func.count()).select_from(BankStatement)) == 1
    assert (
        list(
            (await db_session.execute(select(BankLine.id, BankLine.amount, BankLine.status))).all()
        )
        == snapshot
    )
    assert list((await db_session.execute(select(Receipt.id, Receipt.amount))).all()) == [
        (receipt.json()["id"], Decimal("7.89"))
    ]


@pytest.mark.asyncio
async def test_reconciliation_many_invalid_rows_reports_exact_omission_without_source_values(
    auth_client,
):
    await auth_client.put("/api/v1/modules/issuing", json={"enabled": True})
    content = (
        "date,description,amount\n"
        + "synthetic-private-date,synthetic-private-payee,-20\n" * 83
        + "2026-11-05,Synthetic valid transaction,-12.34\n"
    ).encode()
    response = await _reconciliation_import(auth_client, content)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["imported"] == 1 and len(body["warnings"]) == 21
    assert body["warnings"][0] == (
        "Skipped 83 CSV rows: 83 with an invalid or missing date; "
        "0 with no nonzero recognized amount. Additional skipped rows not listed: 63."
    )
    assert "synthetic-private" not in response.text


@pytest.mark.asyncio
async def test_all_rejected_reconciliation_csv_keeps_existing_refusal_and_creates_no_statement(
    auth_client,
    db_session,
):
    await auth_client.put("/api/v1/modules/issuing", json={"enabled": True})
    response = await _reconciliation_import(
        auth_client,
        b"date,description,amount\ninvalid,Synthetic date,-20\n2026-11-05,Synthetic zero,0\n",
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "no transactions recognised in the statement"
    assert await db_session.scalar(select(func.count()).select_from(BankStatement)) == 0
    assert await db_session.scalar(select(func.count()).select_from(BankLine)) == 0


@pytest.mark.asyncio
async def test_shared_expense_import_retains_valid_debit_credit_behavior_and_warnings(auth_client):
    await auth_client.put("/api/v1/modules/expenses", json={"enabled": True})
    response = await auth_client.post(
        "/api/v1/expenses/import/bank-statement",
        files={
            "file": (
                "synthetic.csv",
                b"date,description,amount,currency\n"
                b"2026-11-05,Synthetic valid transaction,-12.34,EUR\n"
                b"synthetic-private-date,synthetic-private-payee,-20,EUR\n"
                b"2026-11-05,Synthetic valid credit,50,EUR\n",
                "text/csv",
            )
        },
        data={"account_reference": "Synthetic card", "statement_currency": "EUR"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["imported"] == 1 and len(body["transactions"]) == 1
    assert body["transactions"][0]["description"] == VALID.description
    assert body["transactions"][0]["amount"] == "12.34"
    assert DATE_SUMMARY in body["warnings"]
    assert "CSV row 3 skipped: invalid or missing date." in body["warnings"]
    assert "synthetic-private" not in " ".join(body["warnings"])


# Values the current Numeric(14,2) cash/expense columns cannot represent.
UNSUPPORTED_AMOUNTS = [
    "NaN",
    "sNaN",
    "Infinity",
    "-Infinity",
    "1e100000",
    "-1e1000000",
    "1000000000000",
    "999999999999.995",
]
NONFINITE_AMOUNTS = ["NaN", "sNaN", "Infinity", "-Infinity"]
NUMERIC_API_CASES = [(value, "signed") for value in UNSUPPORTED_AMOUNTS] + [
    (value, layout)
    for value in NONFINITE_AMOUNTS
    for layout in ["debit", "credit", "unselected-credit"]
]
RANGE_DETAIL = "Invalid amount on CSV row 3; use a finite value within the supported range."


def _numeric_csv(value, layout="signed", *, preceding_valid=True):
    import csv
    import io

    content = io.StringIO()
    writer = csv.writer(content)
    if layout == "signed":
        writer.writerow(["date", "description", "amount", "currency"])
        if preceding_valid:
            writer.writerow(["2026-11-05", "Synthetic valid transaction", "-12.34", "EUR"])
        writer.writerow(["2026-11-05", "synthetic-private-payee", value, "EUR"])
    else:
        writer.writerow(["date", "description", "debit", "credit", "currency"])
        if preceding_valid:
            writer.writerow(["2026-11-05", "Synthetic valid transaction", "12.34", "0", "EUR"])
        writer.writerow(
            [
                "2026-11-05",
                "synthetic-private-payee",
                value if layout == "debit" else "12.34" if layout == "unselected-credit" else "0",
                value if layout in ["credit", "unselected-credit"] else "0",
                "EUR",
            ]
        )
    return content.getvalue().encode()


@pytest.mark.parametrize("value", UNSUPPORTED_AMOUNTS)
@pytest.mark.parametrize("layout", ["signed", "debit", "credit"])
def test_csv_numeric_boundary_refuses_nonfinite_or_unrepresentable_values(value, layout):
    with pytest.raises(ValueError, match="^" + RANGE_DETAIL.replace(".", r"\.") + "$"):
        bank_statement.parse("numeric.csv", _numeric_csv(value, layout))


@pytest.mark.parametrize("value", NONFINITE_AMOUNTS)
def test_csv_unselected_credit_must_still_be_finite(value):
    with pytest.raises(ValueError, match="^" + RANGE_DETAIL.replace(".", r"\.") + "$"):
        bank_statement.parse("numeric.csv", _numeric_csv(value, "unselected-credit"))


def test_csv_accepts_stored_numeric_edges_and_preserves_existing_cent_rounding():
    from decimal import getcontext

    column = BankLine.__table__.c.amount.type
    assert (column.precision, column.scale) == (14, 2)
    maximum = Decimal(10) ** (column.precision - column.scale) - Decimal(10) ** -column.scale
    assert maximum == Decimal("999999999999.99")
    context = getcontext()
    before = (context.prec, context.rounding, context.Emax, context.Emin, dict(context.traps))
    for value, magnitude, direction in [
        (str(maximum), maximum, "credit"),
        (str(-maximum), maximum, "debit"),
        ("999999999999.994", maximum, "credit"),
        ("1e11", Decimal("100000000000.00"), "credit"),
        ("0.004", Decimal("0.00"), "credit"),
    ]:
        parsed = bank_statement.parse("edges.csv", _numeric_csv(value, preceding_valid=False))
        assert len(parsed.transactions) == 1 and parsed.warnings == []
        assert (parsed.transactions[0].amount, parsed.transactions[0].direction) == (
            magnitude,
            direction,
        )
    assert (
        context.prec,
        context.rounding,
        context.Emax,
        context.Emin,
        dict(context.traps),
    ) == before
    # Finite split columns retain the existing debit precedence.
    parsed = bank_statement.parse(
        "split.csv", b"date,description,debit,credit\n2026-11-05,Synthetic debit,12.34,56.78\n"
    )
    assert len(parsed.transactions) == 1 and parsed.transactions[0].amount == Decimal("12.34")


def test_csv_numeric_boundary_does_not_reclassify_unrelated_programming_errors(monkeypatch):
    def broken_numeric_reader(_value):
        raise RuntimeError("Synthetic unrelated reader bug")

    monkeypatch.setattr(bank_statement.pdf_ocr, "_num", broken_numeric_reader)
    with pytest.raises(RuntimeError, match="Synthetic unrelated reader bug"):
        bank_statement.parse("numeric.csv", _numeric_csv("1"))


@pytest.mark.asyncio
@pytest.mark.parametrize(("value", "layout"), NUMERIC_API_CASES)
async def test_actual_reconciliation_api_refuses_invalid_numeric_file_before_bank_commit(
    auth_client,
    db_session,
    value,
    layout,
):
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    await auth_client.put("/api/v1/modules/issuing", json={"enabled": True})
    # Observe the actual error response instead of letting the test transport
    # re-raise an unhandled exception; no API response or handler is mocked.
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
        headers=auth_client.headers,
    ) as actual:
        response = await _reconciliation_import(actual, _numeric_csv(value, layout))
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == RANGE_DETAIL
    assert "synthetic-private" not in response.text and value not in response.text
    assert await db_session.scalar(select(func.count()).select_from(BankStatement)) == 0
    assert await db_session.scalar(select(func.count()).select_from(BankLine)) == 0


@pytest.mark.asyncio
async def test_actual_reconciliation_api_persists_valid_numeric_edges(auth_client):
    await auth_client.put("/api/v1/modules/issuing", json={"enabled": True})
    content = (
        b"date,description,amount,currency\n"
        b"2026-11-05,Synthetic maximum credit,999999999999.99,EUR\n"
        b"2026-11-05,Synthetic maximum debit,-999999999999.99,EUR\n"
        b"2026-11-05,Synthetic rounded within maximum,999999999999.994,EUR\n"
    )
    response = await _reconciliation_import(auth_client, content)
    assert response.status_code == 201, response.text
    assert response.json()["imported"] == 3 and response.json()["warnings"] == []
    listed = await auth_client.get(
        f"/api/v1/reconciliation/statements/{response.json()['statement_id']}/lines"
    )
    assert listed.status_code == 200
    amounts = {row["description"]: row["amount"] for row in listed.json()}
    assert amounts == {
        "Synthetic maximum credit": "999999999999.99",
        "Synthetic maximum debit": "-999999999999.99",
        "Synthetic rounded within maximum": "999999999999.99",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(("value", "layout"), NUMERIC_API_CASES)
async def test_actual_shared_expense_api_refuses_numeric_file_without_partial_import(
    auth_client,
    db_session,
    value,
    layout,
):
    from httpx import ASGITransport, AsyncClient

    from app.main import app
    from app.models.expense import ExpenseImportBatch, ExpenseTransaction

    await auth_client.put("/api/v1/modules/expenses", json={"enabled": True})
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
        headers=auth_client.headers,
    ) as actual:
        response = await actual.post(
            "/api/v1/expenses/import/bank-statement",
            files={"file": ("numeric.csv", _numeric_csv(value, layout), "text/csv")},
            data={"account_reference": "Synthetic card", "statement_currency": "EUR"},
        )
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == RANGE_DETAIL
    assert await db_session.scalar(select(func.count()).select_from(ExpenseImportBatch)) == 0
    assert await db_session.scalar(select(func.count()).select_from(ExpenseTransaction)) == 0
