"""Reconciliation completeness (Phase 18): a bank CREDIT can be matched to a direct
issued-invoice payment (PATCH /issued/payment), not only to a receipt."""

import io

import pytest

from tests.reconciliation_command_client import transition

ISSUER = {
    "legal_name": "InvoiceIQ Demo BV",
    "vat_number": "NL123456789B01",
    "registration_number": "NL-KVK-12345678",
    "address_line1": "Keizersgracht 1",
    "city": "Amsterdam",
    "postal_code": "1015 CJ",
    "country": "NL",
    "iban": "NL91ABNA0417164300",
    "bic": "ABNANL2A",
    "email": "billing@invoiceiq.test",
}


async def _activate(auth_client):
    await auth_client.put("/api/v1/issuer", json=ISSUER)
    await auth_client.put("/api/v1/modules/issuing", json={"enabled": True})


@pytest.mark.asyncio
async def test_credit_matches_direct_issued_payment(auth_client):
    await _activate(auth_client)
    # An issued invoice paid directly (no receipt) → a Payment row, receipt_id NULL.
    inv = (
        await auth_client.post(
            "/api/v1/issued",
            json={
                "buyer_name": "Globex",
                "issue_date": "2026-02-01",
                "due_date": "2026-03-01",
                "lines": [
                    {"description": "S", "quantity": "1", "unit_price": "500", "vat_rate": "0"}
                ],
            },
        )
    ).json()
    pay = await auth_client.patch(
        f"/api/v1/issued/{inv['id']}/payment",
        json={"amount_paid": "500.00", "paid_date": "2026-02-20"},
    )
    assert pay.status_code == 200

    # Import a matching bank credit.
    csv = "date,description,credit,debit\n2026-02-20,Globex payment,500.00,\n"
    files = {"file": ("s.csv", io.BytesIO(csv.encode()), "text/csv")}
    await auth_client.post("/api/v1/reconciliation/import", files=files)
    sid = (await auth_client.get("/api/v1/reconciliation/statements")).json()[0]["id"]
    line = (await auth_client.get(f"/api/v1/reconciliation/statements/{sid}/lines")).json()[0]

    cands = (await auth_client.get(f"/api/v1/reconciliation/lines/{line['id']}/candidates")).json()
    ip = [c for c in cands if c["kind"] == "issued_payment"]
    assert len(ip) == 1 and ip[0]["amount"] == "500.00"

    m = await transition(
        auth_client, line["id"], "match", target={"kind": "issued_payment", "id": ip[0]["id"]}
    )
    assert m.status_code == 200 and m.json()["status"] == "matched"

    # A debit line cannot match an issued payment (money in).
    csv2 = "date,description,credit,debit\n2026-02-21,Out,,500.00\n"
    files2 = {"file": ("s2.csv", io.BytesIO(csv2.encode()), "text/csv")}
    await auth_client.post("/api/v1/reconciliation/import", files=files2)
    stmts = (await auth_client.get("/api/v1/reconciliation/statements")).json()
    sid2 = next(s["id"] for s in stmts if s["id"] != sid)
    debit = (await auth_client.get(f"/api/v1/reconciliation/statements/{sid2}/lines")).json()[0]
    bad = await transition(
        auth_client, debit["id"], "match", target={"kind": "issued_payment", "id": ip[0]["id"]}
    )
    assert bad.status_code == 400 and "debit" in bad.json()["detail"]
