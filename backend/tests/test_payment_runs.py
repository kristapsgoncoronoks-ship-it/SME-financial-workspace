"""Supplier payment runs (Phase 14): group scheduled invoices into a run, approve
it (WO-9: a second user), pay it (a third — maker ≠ checker ≠ payer; writes the AP
ledger + advances each invoice to paid), cancel unlinks, a paid run reconciles a
bank debit, and the PAYMENT_* authz gate. Invoices are driven to
`scheduled_for_payment` via the real submit→approve→schedule path."""

import csv
import io

import pytest

from tests.reconciliation_command_client import transition


def _h(token):
    return {"Authorization": f"Bearer {token}"}


async def _member(auth_client, client, email, role="admin"):
    inv = await auth_client.post("/api/v1/team/invites", json={"email": email, "role": role})
    acc = await client.post(
        "/api/v1/auth/accept-invite",
        json={"token": inv.json()["token"], "name": "M", "password": "supersecret"},
    )
    return acc.json()["token"]["access_token"]


async def _approve_and_pay(auth_client, client, run, approver, *, reference="SEPA-9"):
    """WO-9 flow: the run's creator (the owner/auth_client) can no longer approve or
    pay their own run — a second user approves and a third pays."""
    appr = await client.post(
        f"/api/v1/payment-runs/{run['id']}/approve",
        headers=_h(approver),
        json={"version": run["version"]},
    )
    assert appr.status_code == 200, appr.text
    payer = await _member(auth_client, client, "payer@acme.io", role="admin")
    paid = await client.post(
        f"/api/v1/payment-runs/{run['id']}/pay",
        headers=_h(payer),
        json={"version": appr.json()["version"], "reference": reference},
    )
    return paid


async def _make_invoice(auth_client, number, unit_price="100"):
    r = await auth_client.post(
        "/api/v1/invoices",
        json={
            "vendor_name": "Acme Supplies",
            "invoice_number": number,
            "issue_date": "2026-05-01",
            "due_date": "2026-12-01",
            "currency": "EUR",
            "line_items": [
                {
                    "description": "Widgets",
                    "quantity": "1",
                    "unit_price": unit_price,
                    "tax_rate": "0",
                }
            ],
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _schedule(auth_client, approver, iid):
    sub = await auth_client.post(f"/api/v1/invoices/{iid}/submit", json={"version": 1})
    ver = sub.json()["version"]
    appr = await auth_client.post(  # owner is also an approver in the default policy
        f"/api/v1/invoices/{iid}/approve",
        headers=_h(approver),
        json={"version": ver},
    )
    body = appr.json()
    sch = await auth_client.post(
        f"/api/v1/invoices/{iid}/transition",
        json={"version": body["version"], "target": "scheduled_for_payment"},
    )
    assert sch.status_code == 200, sch.text


@pytest.mark.asyncio
async def test_create_pay_and_ledger(auth_client, client):
    approver = await _member(auth_client, client, "appr@acme.io", role="admin")
    a = await _make_invoice(auth_client, "INV-1", "100")  # total 100
    b = await _make_invoice(auth_client, "INV-2", "50")  # total 50
    await _schedule(auth_client, approver, a)
    await _schedule(auth_client, approver, b)

    # Both are payable.
    payable = (await auth_client.get("/api/v1/payment-runs/payable")).json()
    assert {i["invoice_number"] for i in payable} == {"INV-1", "INV-2"}

    run = await auth_client.post(
        "/api/v1/payment-runs", json={"invoice_ids": [a, b], "method": "bank_transfer"}
    )
    assert run.status_code == 201, run.text
    rb = run.json()
    assert rb["status"] == "open" and rb["invoice_count"] == 2 and rb["total_eur"] == "150.00"
    rid = rb["id"]

    # Once queued, they leave the payable pool.
    assert (await auth_client.get("/api/v1/payment-runs/payable")).json() == []

    # Approve (second user) then pay (third user) → both invoices paid, ledger written.
    paid = await _approve_and_pay(auth_client, client, rb, approver)
    assert paid.status_code == 200, paid.text
    assert paid.json()["status"] == "paid"
    assert all(i["workflow_state"] == "paid" for i in paid.json()["invoices"])

    inv_a = (await auth_client.get(f"/api/v1/invoices/{a}")).json()
    assert inv_a["payment_status"] == "paid" and inv_a["outstanding"] == "0.00"
    ledger = (await auth_client.get(f"/api/v1/invoices/{a}/payments")).json()
    assert [e["amount"] for e in ledger] == ["100.00"]

    # CSV export works.
    exp = await auth_client.post(f"/api/v1/payment-runs/{rid}/export")
    assert exp.status_code == 200 and "INV-1" in exp.text and "INV-2" in exp.text


@pytest.mark.asyncio
async def test_cancel_unlinks(auth_client, client):
    approver = await _member(auth_client, client, "appr@acme.io", role="admin")
    a = await _make_invoice(auth_client, "INV-1")
    await _schedule(auth_client, approver, a)
    run = (await auth_client.post("/api/v1/payment-runs", json={"invoice_ids": [a]})).json()
    # Cancel → invoice returns to the payable pool.
    assert (await auth_client.delete(f"/api/v1/payment-runs/{run['id']}")).status_code == 204
    payable = (await auth_client.get("/api/v1/payment-runs/payable")).json()
    assert [i["invoice_number"] for i in payable] == ["INV-1"]


@pytest.mark.asyncio
async def test_cannot_run_unscheduled_invoice(auth_client):
    a = await _make_invoice(auth_client, "INV-9")  # still draft
    r = await auth_client.post("/api/v1/payment-runs", json={"invoice_ids": [a]})
    assert r.status_code == 422 and "scheduled-for-payment" in r.json()["detail"]


@pytest.mark.asyncio
async def test_paid_run_reconciles_bank_debit(auth_client, client):
    approver = await _member(auth_client, client, "appr@acme.io", role="admin")
    a = await _make_invoice(auth_client, "INV-1", "100")
    await _schedule(auth_client, approver, a)
    run = (await auth_client.post("/api/v1/payment-runs", json={"invoice_ids": [a]})).json()
    paid = await _approve_and_pay(auth_client, client, run, approver)
    assert paid.status_code == 200, paid.text

    # Import a bank statement with a matching debit (money out 100).
    await auth_client.put("/api/v1/modules/issuing", json={"enabled": True})
    csv = "date,description,credit,debit\n2026-05-20,SEPA-9 supplier payout,,100.00\n"
    files = {"file": ("s.csv", io.BytesIO(csv.encode()), "text/csv")}
    await auth_client.post("/api/v1/reconciliation/import", files=files)
    sid = (await auth_client.get("/api/v1/reconciliation/statements")).json()[0]["id"]
    line = (await auth_client.get(f"/api/v1/reconciliation/statements/{sid}/lines")).json()[0]

    cands = (await auth_client.get(f"/api/v1/reconciliation/lines/{line['id']}/candidates")).json()
    assert any(c["kind"] == "payment_run" and c["id"] == run["id"] for c in cands)
    m = await transition(
        auth_client, line["id"], "match", target={"kind": "payment_run", "id": run["id"]}
    )
    assert m.status_code == 200 and m.json()["status"] == "matched"


@pytest.mark.asyncio
async def test_export_csv_is_formula_injection_safe(auth_client, client):
    """Board R1: `payment_run.export_csv` writes `inv.invoice_number` and
    `run.reference` — both attacker-influenceable free text (an invoice
    number can originate from a vendor-supplied PDF a human transcribes; a
    run reference is operator-typed) — into the bank-payment CSV a finance
    user opens straight in Excel. Neither may reach the file un-neutralised
    (CWE-1236), and the numeric amount column must never be touched."""
    approver = await _member(auth_client, client, "appr@acme.io", role="admin")
    a = await _make_invoice(auth_client, "=cmd|'/c calc'!A1", "100")
    await _schedule(auth_client, approver, a)
    run = (await auth_client.post("/api/v1/payment-runs", json={"invoice_ids": [a]})).json()
    paid = await _approve_and_pay(
        auth_client, client, run, approver, reference="=1+1+cmd|'/c calc'!A1"
    )
    assert paid.status_code == 200, paid.text

    exp = await auth_client.post(f"/api/v1/payment-runs/{run['id']}/export")
    assert exp.status_code == 200
    rows = list(csv.reader(io.StringIO(exp.text)))
    header, data = rows[0], rows[1]
    assert header == [
        "run_reference",
        "invoice_number",
        "amount",
        "currency",
        "amount_eur",
        "method",
    ]
    # Both attacker-influenceable free-text cells are neutralised with a
    # leading quote so Excel/Sheets renders them as inert text, not formulas.
    assert data[0].startswith("'="), data[0]
    assert data[1].startswith("'="), data[1]
    # The numeric amount column is never sanitised (it must stay a real
    # number a spreadsheet can sum, not text).
    assert data[2] == "100.00"


@pytest.mark.asyncio
async def test_authz_split(auth_client, client):
    emp = await _member(auth_client, client, "emp@corp.io", role="user")
    assert (await client.get("/api/v1/payment-runs", headers=_h(emp))).status_code == 403
    ro = await _member(auth_client, client, "ro@corp.io", role="user_free")
    assert (await client.get("/api/v1/payment-runs", headers=_h(ro))).status_code == 200
    assert (
        await client.post("/api/v1/payment-runs", headers=_h(ro), json={"invoice_ids": ["x"]})
    ).status_code == 403
