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
