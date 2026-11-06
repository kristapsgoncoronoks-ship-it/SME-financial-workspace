"""Strict native-currency Explore cuts and the explicit offline contract."""

import csv
import hashlib
import io
import json

import openpyxl
import pytest
from sqlalchemy import select

from app.models import Base
from app.models.invoice import Invoice


async def native_invoice(client, number, currency, price, rate=None):
    body = {
        "vendor_name": "Synthetic currency basis supplier",
        "invoice_number": number,
        "issue_date": "2026-11-01",
        "currency": currency,
        "line_items": [
            {
                "description": "Synthetic native-priced service",
                "category": "Synthetic currency basis",
                "quantity": "1",
                "unit_price": price,
                "tax_rate": "10",
            }
        ],
    }
    if rate is not None:
        body["fx_rate"] = rate
    response = await client.post("/api/v1/invoices", json=body)
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["currency"] == currency
    assert result["line_items"][0]["amount"] == price
    return result


async def seed_native_controls(client):
    invoices = [
        await native_invoice(client, "SYNTHETIC-USD-RATE2", "USD", "100.00", "2"),
        await native_invoice(client, "SYNTHETIC-EUR-CONTROL", "EUR", "50.00"),
        await native_invoice(client, "SYNTHETIC-USD-RATE4", "USD", "200.00", "4"),
    ]
    assert [i["total_eur"] for i in invoices] == ["55.00", "55.00", "55.00"]
    assert [i["fx_source"] for i in invoices] == ["stated", "eur", "stated"]
    return invoices


async def financial_state(db, org_id=None):
    result = {}
    for name in (
        "invoices",
        "line_items",
        "vendors",
        "audit_events",
        "ecb_rates",
        "payments",
        "bank_lines",
        "bank_statements",
        "payment_runs",
        "supplier_payments",
        "expense_reports",
        "expense_items",
        "expense_transactions",
        "issued_invoices",
        "receipts",
    ):
        table = Base.metadata.tables[name]
        statement = select(table).order_by(table.c.id)
        if org_id is not None:
            if name == "line_items":
                statement = statement.join(Invoice, Invoice.id == table.c.invoice_id).where(
                    Invoice.org_id == org_id
                )
            elif "org_id" in table.c:
                statement = statement.where(table.c.org_id == org_id)
        rows = [dict(row) for row in (await db.execute(statement)).mappings()]
        result[name] = {
            "count": len(rows),
            "sha256": hashlib.sha256(
                json.dumps(rows, sort_keys=True, default=str).encode()
            ).hexdigest(),
        }
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("measure", ["net", "tax", "gross"])
async def test_money_requires_explicit_currency(auth_client, measure):
    response = await auth_client.get("/api/v1/analytics/explore", params={"measure": measure})
    assert response.status_code == 422
    assert response.json()["code"] == "explore_currency_required"
    assert "currency" in response.json()["detail"].lower()
    assert "x-explore-currency" not in response.headers


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "currency", ["", "US", "USDD", "U1D", "ÜSD", " USD", "ßSD", "ＵＳＤ", "USD\r\nX-Bad:yes"]
)
async def test_invalid_unit_never_reaches_a_response_header(auth_client, currency):
    response = await auth_client.get("/api/v1/analytics/explore", params={"currency": currency})
    assert response.status_code == 422
    assert response.json()["code"] == "explore_currency_invalid"
    assert "x-explore-currency" not in response.headers
    assert "x-bad" not in response.headers


@pytest.mark.asyncio
async def test_exact_native_fixture_keeps_units_and_source_snapshots(auth_client, db_session):
    await seed_native_controls(auth_client)
    before = await financial_state(db_session)
    for currency, values in [
        ("usd", ("300.00", "30.00", "330.00")),
        ("eur", ("50.00", "5.00", "55.00")),
    ]:
        for measure, value in zip(("net", "tax", "gross"), values, strict=True):
            response = await auth_client.get(
                "/api/v1/analytics/explore",
                params={"measure": measure, "dim": "currency", "currency": currency},
            )
            assert response.status_code == 200, response.text
            result = response.json()
            assert result["rows"] == [{"currency": currency.upper(), "value": value}]
            assert result["currency"] == currency.upper()
            assert result["currency_basis"] == "native"
            assert response.headers["x-explore-currency"] == currency.upper()
            assert response.headers["x-explore-currency-basis"] == "native"
            assert (
                result["measure"]["label"]
                == {"net": "Net spend", "tax": "Tax", "gross": "Gross spend"}[measure]
            )
    assert await financial_state(db_session) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("format", ["csv", "xlsx", "pdf"])
@pytest.mark.parametrize("contract", [None, "1"])
async def test_legacy_money_download_refuses_even_with_selected_unit(
    auth_client, db_session, format, contract
):
    await seed_native_controls(auth_client)
    before = await financial_state(db_session)
    params = {"currency": "USD", "format": format, "dim": "category"}
    if contract is not None:
        params["export_contract"] = contract
    response = await auth_client.get("/api/v1/analytics/explore", params=params)
    assert response.status_code == 422
    assert response.json()["code"] == "explore_export_contract_required"
    assert "2" in response.json()["detail"]
    assert "content-disposition" not in response.headers
    assert await financial_state(db_session) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("format", ["csv", "xlsx", "pdf"])
@pytest.mark.parametrize("currency,value", [("usd", "300.00"), ("eur", "50.00")])
async def test_money_contract2_files_declare_native_unit_without_changing_cells(
    auth_client, db_session, format, currency, value
):
    await seed_native_controls(auth_client)
    before = await financial_state(db_session)
    response = await auth_client.get(
        "/api/v1/analytics/explore",
        params={"currency": currency, "format": format, "dim": "category", "export_contract": 2},
    )
    assert response.status_code == 200, response.text
    code = currency.upper()
    assert response.headers["x-explore-currency"] == code
    assert response.headers["x-explore-currency-basis"] == "native"
    assert response.headers["x-explore-export-contract"] == "2"
    assert f"explore-{code}-up-to-100-groups.{format}" in response.headers["content-disposition"]
    expected = [["Category", f"Net spend ({code})"], ["Synthetic currency basis", value]]
    if format == "csv":
        assert list(csv.reader(io.StringIO(response.text))) == expected
    elif format == "xlsx":
        workbook = openpyxl.load_workbook(io.BytesIO(response.content))
        assert [list(row) for row in workbook.active.values] == expected
    else:
        from pypdf import PdfReader

        rendered = "\n".join(
            page.extract_text() for page in PdfReader(io.BytesIO(response.content)).pages
        )
        assert expected[0][1] in rendered and value in rendered
    assert await financial_state(db_session) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "measure,value", [("quantity", "3.000"), ("lines", "3"), ("invoices", "3")]
)
async def test_nonmoney_mixed_native_units_and_legacy_files_stay_compatible(
    auth_client, measure, value
):
    await seed_native_controls(auth_client)
    params = {"measure": measure, "dim": "category"}
    response = await auth_client.get("/api/v1/analytics/explore", params=params)
    assert response.status_code == 200
    result = response.json()
    assert result["rows"] == [{"category": "Synthetic currency basis", "value": value}]
    assert result["currency"] is None and result["currency_basis"] == "not_applicable"
    assert response.headers["x-explore-currency-basis"] == "not_applicable"
    assert "x-explore-currency" not in response.headers
    for format in ("csv", "xlsx", "pdf"):
        response = await auth_client.get(
            "/api/v1/analytics/explore", params={**params, "format": format}
        )
        assert response.status_code == 200
        assert response.headers["x-explore-export-contract"] == "1"
        assert f"explore-up-to-100-groups.{format}" in response.headers["content-disposition"]
        assert "x-explore-currency" not in response.headers


@pytest.mark.asyncio
@pytest.mark.parametrize("format", ["json", "csv", "xlsx", "pdf"])
async def test_currency_contract_stays_report_read_without_export_run(role_client, format):
    reader = await role_client("user_free")
    permissions = (await reader.get("/api/v1/auth/permissions")).json()["permissions"]
    assert "report.read" in permissions and "export.run" not in permissions
    response = await reader.get(
        "/api/v1/analytics/explore",
        params={"format": format, "currency": "USD", "export_contract": 2},
    )
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("contract", [0, 3])
async def test_unknown_export_contract_refuses(auth_client, contract):
    response = await auth_client.get(
        "/api/v1/analytics/explore",
        params={"currency": "USD", "format": "csv", "export_contract": contract},
    )
    assert response.status_code == 422
