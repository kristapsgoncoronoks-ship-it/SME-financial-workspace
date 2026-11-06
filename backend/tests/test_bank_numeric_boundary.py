"""Public statement numeric boundary, without changing reader money semantics.

PDF text extraction is supplied directly: no OCR/native reader is invoked.
Nonfinite emitted-PDF facts use an explicitly labelled result seam because the
existing PDF money-token regex cannot read literal NaN or Infinity text.
"""

from __future__ import annotations

import io
from contextlib import contextmanager
from datetime import date
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from pypdf import PdfWriter
from sqlalchemy import event, select

from app.main import app
from app.models import Base
from app.services import bank_statement as bs

MAXIMUM = Decimal("999999999999.99")
OVERFLOW = "1000000000000.00"
CONTEXT_OVERFLOW = "12345678901234567890123456789012345.00"
PDF_AMOUNT_OVERFLOW = f"2026-11-06 Synthetic valid 12.34\n2026-11-07 Synthetic bad {OVERFLOW}"
TABLES = (
    "bank_statements",
    "bank_lines",
    "expense_import_batches",
    "expense_transactions",
    "expense_commands",
    "expense_reports",
    "expense_items",
    "receipts",
    "payments",
    "reimbursement_batches",
    "payment_runs",
    "supplier_payments",
    "invoices",
    "issued_invoices",
    "audit_events",
    "jobs",
    "webhook_deliveries",
)


def _entry(amount, direction="CRDT", *, reversal=False, currency="USD"):
    return (
        f'<Ntry><Amt Ccy="{currency}">{amount}</Amt><CdtDbtInd>{direction}</CdtDbtInd>'
        f"<Sts>BOOK</Sts><RvslInd>{str(reversal).lower()}</RvslInd>"
        "<BookgDt><Dt>2026-11-06</Dt></BookgDt>"
        "<NtryDtls><TxDtls><RmtInf><Ustrd>Synthetic private numeric row</Ustrd>"
        "</RmtInf></TxDtls></NtryDtls></Ntry>"
    )


def _camt(amount, direction="CRDT"):
    return (
        '<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02">'
        "<BkToCstmrStmt><Stmt>"
        + _entry("12.34")
        + _entry(amount, direction)
        + "</Stmt></BkToCstmrStmt></Document>"
    ).encode()


def _mt940(amount, direction="D"):
    return (
        ":20:SYNTHETIC\n:60F:C261106USD100,00\n"
        ":61:261106D12,34NTRF\n:86:Synthetic valid prefix\n"
        f":61:261107{direction}{amount.replace('.', ',')}NTRF\n"
        ":86:Synthetic private numeric row\n"
    ).encode()


def _balance_text(*, negative=False):
    start = Decimal(OVERFLOW) * (-1 if negative else 1)
    return "\n".join(
        f"2026-11-{6 + index:02d} Synthetic balance row 1.00 {start - index:.2f}"
        for index in range(3)
    )


def _extract(monkeypatch, text):
    monkeypatch.setattr(bs.pdf_ocr, "extract_text", lambda _content: (text, "text-layer"))


def _emitted_pdf(monkeypatch, field, value):
    """Exercise validation of emitted facts, not recognition of nonnumeric text."""
    _extract(monkeypatch, "2026-11-06 Synthetic supplied text 12.34")
    transaction = bs.Txn(
        date=date(2026, 11, 6),
        description="Synthetic emitted result",
        amount=Decimal("12.34"),
        direction="debit",
        balance=Decimal("100.00"),
        currency="USD",
    )
    setattr(transaction, field, Decimal(value))
    monkeypatch.setattr(bs, "_transactions_from_rows", lambda _rows, _warnings: [transaction])
    return transaction


@pytest.mark.parametrize("direction", ["CRDT", "DBIT"])
def test_camt_rejects_nan_after_a_valid_prefix(direction):
    with pytest.raises(ValueError):
        bs.parse("synthetic.xml", _camt("NaN", direction))


@pytest.mark.parametrize("direction", ["CRDT", "DBIT"])
def test_camt_refuses_whole_result_for_emitted_storage_overflow(direction):
    with pytest.raises(ValueError):
        bs.parse("synthetic.xml", _camt(OVERFLOW, direction))


@pytest.mark.parametrize("direction", ["C", "D"])
def test_mt940_context_failure_is_a_public_value_error(direction):
    with pytest.raises(ValueError):
        bs.parse("synthetic.mt940", _mt940(CONTEXT_OVERFLOW, direction))


def test_pdf_amount_overflow_after_a_valid_row_is_refused(monkeypatch):
    _extract(monkeypatch, PDF_AMOUNT_OVERFLOW)
    with pytest.raises(ValueError):
        bs.parse("synthetic.pdf", b"supplied extraction")


def test_pdf_amount_context_failure_is_a_public_value_error(monkeypatch):
    _extract(monkeypatch, PDF_AMOUNT_OVERFLOW.replace(OVERFLOW, CONTEXT_OVERFLOW))
    with pytest.raises(ValueError):
        bs.parse("synthetic.pdf", b"supplied extraction")


@pytest.mark.parametrize("negative", [False, True], ids=["positive", "negative"])
def test_pdf_recognized_running_balance_must_fit_storage(monkeypatch, negative):
    _extract(monkeypatch, _balance_text(negative=negative))
    with pytest.raises(ValueError):
        bs.parse("synthetic.pdf", b"supplied extraction")


@pytest.mark.parametrize("field", ["amount", "balance"])
@pytest.mark.parametrize("value", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_nonfinite_emitted_pdf_result_is_refused(monkeypatch, field, value):
    _emitted_pdf(monkeypatch, field, value)
    with pytest.raises(ValueError):
        bs.parse("synthetic.pdf", b"supplied extraction")


@pytest.mark.parametrize("field,sign", [("amount", ""), ("balance", "-")])
def test_storage_rounding_is_validated_without_rewriting_emitted_values(monkeypatch, field, sign):
    value = sign + "999999999999.994"
    transaction = _emitted_pdf(monkeypatch, field, value)
    result = bs.parse("synthetic.pdf", b"supplied extraction")
    assert result.transactions == [transaction]
    assert getattr(result.transactions[0], field) == Decimal(value)
    assert getattr(result.transactions[0], field).as_tuple() == Decimal(value).as_tuple()
    assert result.transactions[0].direction == "debit"
    assert result.transactions[0].currency == "USD"


@pytest.mark.parametrize("field,sign", [("amount", ""), ("balance", "-")])
def test_storage_rounding_can_overflow_an_emitted_amount_or_balance(monkeypatch, field, sign):
    _emitted_pdf(monkeypatch, field, sign + "999999999999.995")
    with pytest.raises(ValueError):
        bs.parse("synthetic.pdf", b"supplied extraction")


def test_camt_maximum_reversal_and_source_currency_are_preserved():
    content = (
        '<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02">'
        "<BkToCstmrStmt><Stmt>"
        + _entry(str(MAXIMUM))
        + _entry(str(MAXIMUM), reversal=True)
        + "</Stmt></BkToCstmrStmt></Document>"
    ).encode()
    result = bs.parse("synthetic.xml", content)
    assert [row.amount for row in result.transactions] == [MAXIMUM, MAXIMUM]
    assert [row.direction for row in result.transactions] == ["credit", "debit"]
    assert [row.currency for row in result.transactions] == ["USD", "USD"]


@pytest.mark.parametrize("value", ["0", "-0.00", "-999999999999.99"])
def test_existing_camt_zero_and_signed_values_are_not_reclassified(value):
    row = bs.parse("synthetic.xml", _camt(value, "DBIT")).transactions[-1]
    assert row.amount.as_tuple() == Decimal(value).quantize(Decimal("0.01")).as_tuple()
    assert (row.direction, row.currency) == ("debit", "USD")


def test_mt940_maximum_reversed_debit_keeps_source_currency():
    result = bs.parse("synthetic.sta", _mt940(str(MAXIMUM), "RD"))
    row = result.transactions[-1]
    assert (row.amount, row.direction, row.currency) == (MAXIMUM, "credit", "USD")
    assert row.description.startswith("[reversal]")


def test_current_csv_and_camt_cent_rounding_are_not_replaced_with_storage_rounding():
    csv = (
        b"date,description,amount,currency\n"
        b"2026-11-06,Synthetic rounding,-0.025,usd\n"
        b"2026-11-07,Synthetic maximum,999999999999.994,\n"
    )
    result = bs.parse("synthetic.csv", csv)
    assert [row.amount for row in result.transactions] == [Decimal("0.02"), MAXIMUM]
    assert [row.direction for row in result.transactions] == ["debit", "credit"]
    assert [row.currency for row in result.transactions] == ["USD", None]
    assert bs.parse("synthetic.xml", _camt("1.005")).transactions[-1].amount == Decimal("1.00")


def test_public_numeric_guard_does_not_reclassify_unrelated_reader_errors(monkeypatch):
    def broken(_content):
        raise RuntimeError("Synthetic reader programming error")

    monkeypatch.setattr(bs.pdf_ocr, "extract_text", broken)
    with pytest.raises(RuntimeError, match="Synthetic reader programming error"):
        bs.parse("synthetic.pdf", b"supplied extraction")


def _pdf_bytes():
    output = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(output)
    return output.getvalue()


async def _state(db, org_id):
    result = {}
    for name in TABLES:
        table = Base.metadata.tables[name]
        result[name] = [
            dict(row)
            for row in (
                await db.execute(select(table).where(table.c.org_id == org_id).order_by(table.c.id))
            ).mappings()
        ]
    return result


@contextmanager
def _dml(db):
    statements = []

    def observe(_connection, _cursor, statement, *_args):
        if statement.lstrip().split(None, 1)[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
            statements.append(statement)

    engine = db.bind.sync_engine
    event.listen(engine, "before_cursor_execute", observe)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", observe)


API_CASES = [
    ("reconciliation", "csv"),
    ("reconciliation", "camt"),
    ("reconciliation", "mt940"),
    ("reconciliation", "pdf-amount"),
    ("reconciliation", "pdf-balance"),
    ("expenses", "csv"),
    ("expenses", "pdf-amount"),
    ("expenses", "pdf-balance"),
]


@pytest.mark.parametrize("caller,source", API_CASES)
async def test_actual_import_callers_refuse_before_any_financial_write(
    auth_client, db_session, monkeypatch, caller, source
):
    module = "issuing" if caller == "reconciliation" else "expenses"
    enabled = await auth_client.put(f"/api/v1/modules/{module}", json={"enabled": True})
    assert enabled.status_code == 200, enabled.text
    org_id = (await auth_client.get("/api/v1/auth/me")).json()["organization"]["id"]
    if source == "csv":
        name, media_type = "synthetic.csv", "text/csv"
        content = (
            b"date,description,amount,currency\n"
            b"2026-11-06,Synthetic valid prefix,-12.34,EUR\n"
            b"2026-11-07,Synthetic private numeric row,-NaN,EUR\n"
        )
    elif source == "camt":
        name, media_type, content = "synthetic.xml", "application/xml", _camt("NaN", "DBIT")
    elif source == "mt940":
        name, media_type, content = "synthetic.mt940", "text/plain", _mt940(CONTEXT_OVERFLOW)
    else:
        name, media_type, content = "synthetic.pdf", "application/pdf", _pdf_bytes()
        _extract(monkeypatch, PDF_AMOUNT_OVERFLOW if source == "pdf-amount" else _balance_text())
    path = (
        "/api/v1/reconciliation/import"
        if caller == "reconciliation"
        else "/api/v1/expenses/import/bank-statement"
    )
    before = await _state(db_session, org_id)
    # Real route exceptions become HTTP500 on baseline; do not replace the parser
    # or a persistence helper to manufacture the expected client response.
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
        headers=auth_client.headers,
    ) as client:
        with _dml(db_session) as writes:
            response = await client.post(
                path,
                files={"file": (name, content, media_type)},
                data={"account_reference": "Synthetic numeric guard", "statement_currency": "EUR"},
                headers={"Idempotency-Key": f"synthetic-numeric-{caller}-{source}"},
            )
    assert response.status_code == 422, response.text
    assert isinstance(response.json()["detail"], str)
    assert "Synthetic private" not in response.text
    if source == "csv":
        assert response.json()["detail"] == (
            "Invalid amount on CSV row 3; use a finite value within the supported range."
        )
    assert writes == [], writes
    assert await _state(db_session, org_id) == before
