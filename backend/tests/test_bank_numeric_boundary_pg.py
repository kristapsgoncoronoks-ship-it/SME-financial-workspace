"""Actual PG imports reject unsafe emitted money before any source facts commit.

CAMT/MT940 use their real parsers. PDF cases supply pure extracted text and do
not claim decoder/OCR coverage. Existing CSV fixture owns its isolated tenant.
"""

import hashlib
import json
import os
from pathlib import Path

import pytest
from sqlalchemy import select, text

from app.core import tenant
from app.models import Base
from app.services import bank_statement
from tests.test_bank_csv_row_accounting_pg import bank_csv_pg as bank_csv_pg
from tests.test_bank_statement_camt import _entry, _stmt

pytestmark = pytest.mark.skipif(
    not os.environ.get("RLS_TEST_DATABASE_URL"),
    reason="requires migrated non-superuser PostgreSQL",
)
TABLES = (
    "bank_statements",
    "bank_lines",
    "receipts",
    "payments",
    "invoices",
    "issued_invoices",
    "payment_runs",
    "supplier_payments",
    "expense_reports",
    "expense_items",
    "expense_transactions",
    "expense_import_batches",
    "reimbursement_batches",
    "expense_commands",
    "documents",
    "document_versions",
    "jobs",
    "webhook_deliveries",
    "audit_events",
    "email_messages",
)


def mt940(amount, *, reversal=False):
    mark = "RD" if reversal else "D"
    return (
        ":20:SYNTHETIC-NUMERIC\r\n:25:SYNTHETIC-ACCOUNT\r\n:60F:C261106EUR0,00\r\n"
        ":61:261106D12,34NTRFNONREF//VALID-PREFIX\r\n:86:Synthetic valid prefix\r\n"
        f":61:261106{mark}{amount}NTRFNONREF//TEST\r\n:86:Synthetic tested entry\r\n"
    ).encode()


async def state(factory, org_id):
    token = tenant.set_current_org(org_id)
    try:
        async with factory() as db:
            result = {}
            for name in TABLES:
                table = Base.metadata.tables[name]
                rows = [
                    dict(row)
                    for row in (
                        await db.execute(
                            select(table).where(table.c.org_id == org_id).order_by(table.c.id)
                        )
                    ).mappings()
                ]
                # Preserve complete SQL-row equality without disclosing the
                # synthetic verification email token or using NaN self-equality.
                result[name] = {
                    "count": len(rows),
                    "sha256": hashlib.sha256(
                        json.dumps(rows, sort_keys=True, default=str).encode()
                    ).hexdigest(),
                }
            result["raw_bank_money"] = [
                dict(row)
                for row in (
                    await db.execute(
                        text(
                            "SELECT id::text,amount::text AS amount,balance::text AS balance,"
                            "amount::text NOT IN ('NaN','Infinity','-Infinity') AS finite "
                            "FROM bank_lines WHERE org_id=:org ORDER BY id"
                        ),
                        {"org": org_id},
                    )
                ).mappings()
            ]
            return result
    finally:
        tenant.reset_current_org(token)


def evidence(name, data):
    folder = os.environ.get("BANK_NUMERIC_ARTIFACT_DIR")
    if folder:
        Path(folder).mkdir(parents=True, exist_ok=True)
        Path(folder, name + ".json").write_text(json.dumps(data, indent=2, default=str) + "\n")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,filename,content,kind",
    [
        (
            "camt-nan-credit",
            "synthetic.xml",
            _stmt(_entry("12.34") + _entry("NaN")),
            "application/xml",
        ),
        (
            "camt-nan-debit",
            "synthetic.xml",
            _stmt(_entry("12.34") + _entry("NaN", ind="DBIT")),
            "application/xml",
        ),
        (
            "camt-storage-overflow",
            "synthetic.xml",
            _stmt(_entry("12.34") + _entry("1000000000000.00")),
            "application/xml",
        ),
        (
            "camt-rounded-overflow",
            "synthetic.xml",
            _stmt(_entry("12.34") + _entry("999999999999.995")),
            "application/xml",
        ),
        ("mt940-storage-overflow", "synthetic.sta", mt940("1000000000000,00"), "text/plain"),
        (
            "mt940-context-overflow",
            "synthetic.sta",
            mt940("12345678901234567890123456789012345,00"),
            "text/plain",
        ),
    ],
    ids=[
        "camt-nan-credit",
        "camt-nan-debit",
        "camt-storage-overflow",
        "camt-rounded-overflow",
        "mt940-storage-overflow",
        "mt940-context-overflow",
    ],
)
async def test_native_all_result_money_rejected_before_valid_prefix_commits(
    bank_csv_pg, name, filename, content, kind
):
    client, factory, org_id = bank_csv_pg
    before = await state(factory, org_id)
    response = await client.post(
        "/api/v1/reconciliation/import", files={"file": (filename, content, kind)}
    )
    after = await state(factory, org_id)
    evidence(
        name, {"org_id": org_id, "status": response.status_code, "before": before, "after": after}
    )
    assert response.status_code == 422, response.text
    assert after == before


@pytest.mark.asyncio
@pytest.mark.parametrize("consumer", ["reconciliation", "expenses"])
@pytest.mark.parametrize(
    "name,source_text",
    [
        (
            "PDF-pure-text-amount",
            "2026-11-06 Synthetic valid 12.34\n2026-11-07 Synthetic large 1000000000000.00",
        ),
        (
            "PDF-pure-text-balance",
            "2026-11-06 Synthetic 1.00 1000000000000.00\n"
            "2026-11-07 Synthetic 1.00 999999999999.00\n"
            "2026-11-08 Synthetic 1.00 999999999998.00",
        ),
    ],
    ids=["amount", "balance"],
)
async def test_native_both_PDF_consumers_reject_unsafe_pure_text_without_writes(
    bank_csv_pg, monkeypatch, consumer, name, source_text
):
    client, factory, org_id = bank_csv_pg
    monkeypatch.setattr(
        bank_statement.pdf_ocr, "extract_text", lambda _content: (source_text, "text-layer")
    )
    before = await state(factory, org_id)
    path = (
        "/api/v1/reconciliation/import"
        if consumer == "reconciliation"
        else "/api/v1/expenses/import/bank-statement"
    )
    response = await client.post(
        path,
        files={
            "file": (
                "synthetic.pdf",
                b"%PDF-1.4\n% Synthetic boundary only\n%%EOF\n",
                "application/pdf",
            )
        },
        data={"statement_currency": "EUR", "account_reference": "Synthetic own account"},
    )
    after = await state(factory, org_id)
    evidence(
        name + "-" + consumer,
        {
            "org_id": org_id,
            "status": response.status_code,
            "before": before,
            "after": after,
            "scope": "Extracted text supplied; no decoder/OCR claim",
        },
    )
    assert response.status_code == 422, response.text
    assert after == before


@pytest.mark.asyncio
async def test_native_maximum_source_unit_direction_and_reversal_preserved(bank_csv_pg):
    client, factory, org_id = bank_csv_pg
    content = _stmt(
        _entry("999999999999.99", ind="DBIT")
        + _entry("999999999999.994")
        + _entry("12.34", ccy="USD", reversal=True)
    )
    response = await client.post(
        "/api/v1/reconciliation/import", files={"file": ("valid.xml", content, "application/xml")}
    )
    assert response.status_code == 201, response.text
    lines = await client.get(
        f"/api/v1/reconciliation/statements/{response.json()['statement_id']}/lines"
    )
    assert lines.status_code == 200, lines.text
    assert sorted(
        (row["amount"], row["currency"], row["direction"]) for row in lines.json()
    ) == sorted(
        [
            ("-999999999999.99", "EUR", "debit"),
            ("999999999999.99", "EUR", "credit"),
            ("-12.34", "USD", "debit"),
        ]
    )
    final = await state(factory, org_id)
    assert all(row["finite"] for row in final["raw_bank_money"])
    evidence("valid-camt-max-source-unit-reversal", {"org_id": org_id, "state": final})
