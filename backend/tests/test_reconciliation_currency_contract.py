"""Source units must survive import and the public reconciliation contract."""

import pytest

from app.services import bank_statement
from tests.reconciliation_command_client import transition


@pytest.mark.parametrize("column", ["currency", "Ccy", "Transaction Currency"])
def test_csv_retains_explicit_currency_and_missing_stays_unknown(column):
    result = bank_statement.parse(
        "units.csv",
        f"date,description,amount,{column}\n2026-10-14,USD credit,100, usd \n"
        "2026-10-14,EUR debit,-25,EUR\n2026-10-14,Unknown,50,\n".encode(),
    )
    assert [t.currency for t in result.transactions] == ["USD", "EUR", None]
    assert [t.direction for t in result.transactions] == ["credit", "debit", "credit"]


@pytest.mark.parametrize("unit", ["EURO", "$", "12", "USD private source"])
def test_invalid_stated_csv_currency_refuses_without_echoing_source(unit):
    with pytest.raises(ValueError) as exc:
        bank_statement.parse(
            "units.csv", f"date,description,amount,currency\n2026-10-14,Debit,-25,{unit}\n".encode()
        )
    assert str(exc.value) == "Invalid currency code on CSV row 2; use three letters."


@pytest.mark.asyncio
@pytest.mark.parametrize("source_format", ["csv", "camt"])
async def test_import_currency_reaches_api_and_foreign_line_cannot_settle_eur(
    auth_client, source_format
):
    assert (
        await auth_client.put("/api/v1/modules/issuing", json={"enabled": True})
    ).status_code == 200
    receipt = await auth_client.post(
        "/api/v1/receipts",
        json={"amount": "100", "received_on": "2026-10-14", "reference": "Synthetic"},
    )
    assert receipt.status_code == 201, receipt.text
    content = b"date,description,amount,currency\n2026-10-14,Foreign,100,USD\n2026-10-14,Base,100,EUR\n2026-10-14,Unknown,100,\n"
    filename, content_type = "units.csv", "text/csv"
    if source_format == "camt":
        filename, content_type = "units.xml", "application/xml"
        entries = "".join(
            f"<Ntry><Amt{ccy}>100.00</Amt><CdtDbtInd>CRDT</CdtDbtInd>"
            "<BookgDt><Dt>2026-10-14</Dt></BookgDt></Ntry>"
            for ccy in (' Ccy="USD"', ' Ccy="EUR"', "")
        )
        content = (
            f"<Document><BkToCstmrStmt><Stmt>{entries}</Stmt></BkToCstmrStmt></Document>".encode()
        )
    imported = await auth_client.post(
        "/api/v1/reconciliation/import",
        files={
            "file": (
                filename,
                content,
                content_type,
            )
        },
    )
    assert imported.status_code == 201, imported.text
    lines = (
        await auth_client.get(
            f"/api/v1/reconciliation/statements/{imported.json()['statement_id']}/lines"
        )
    ).json()
    by_unit = {line["currency"]: line for line in lines}
    assert set(by_unit) == {"USD", "EUR", None}
    foreign = by_unit["USD"]
    assert (
        await auth_client.get(f"/api/v1/reconciliation/lines/{foreign['id']}/candidates")
    ).json() == []
    refused = await transition(
        auth_client, foreign["id"], "match", target={"kind": "receipt", "id": receipt.json()["id"]}
    )
    assert refused.status_code == 400
    assert "USD" in refused.json()["detail"]
    base = by_unit["EUR"]
    candidates = (
        await auth_client.get(f"/api/v1/reconciliation/lines/{base['id']}/candidates")
    ).json()
    assert candidates[0]["currency"] == "EUR"
    matched = await transition(
        auth_client, base["id"], "match", target={"kind": "receipt", "id": receipt.json()["id"]}
    )
    assert matched.status_code == 200, matched.text
    assert matched.json()["currency"] == "EUR"
    # A status mutation must also retain an unknown unit, never default to EUR.
    ignored = await transition(auth_client, by_unit[None]["id"], "ignore")
    assert ignored.status_code == 200 and ignored.json()["currency"] is None


@pytest.mark.asyncio
async def test_invalid_currency_import_does_not_persist_partial_statement(auth_client):
    await auth_client.put("/api/v1/modules/issuing", json={"enabled": True})
    imported = await auth_client.post(
        "/api/v1/reconciliation/import",
        files={
            "file": (
                "invalid.csv",
                b"date,description,amount,currency\n2026-10-14,Valid,100,EUR\n2026-10-14,Invalid,100,EURO\n",
                "text/csv",
            )
        },
    )
    assert imported.status_code == 422, imported.text
    assert (await auth_client.get("/api/v1/reconciliation/statements")).json() == []
