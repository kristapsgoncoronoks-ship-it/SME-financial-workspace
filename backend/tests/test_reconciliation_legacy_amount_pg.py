"""Existing PostgreSQL NaN facts stay intact while reads and refusals stay usable."""

from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, event, text

from app.core import tenant
from app.models.bank_import import BankLine, BankStatement
from app.models.expense import ReimbursementBatch
from app.models.issued_invoice import IssuedInvoice
from app.models.organization import Organization
from app.models.payment import Payment
from app.models.payment_run import PaymentRun
from app.models.receipt import Receipt
from tests.test_bank_csv_row_accounting_pg import PG_URL
from tests.test_bank_csv_row_accounting_pg import bank_csv_pg as bank_csv_pg

pytestmark = pytest.mark.skipif(not PG_URL, reason="requires migrated non-superuser PostgreSQL")
TABLES = (
    "bank_statements",
    "bank_lines",
    "receipts",
    "payments",
    "reimbursement_batches",
    "payment_runs",
    "issued_invoices",
    "supplier_payments",
    "expense_reports",
    "expense_items",
    "expense_transactions",
    "expense_import_batches",
    "audit_events",
    "jobs",
)
LINE_DETAIL = "The stored bank amount is unavailable. Check the original statement with your accountant before matching."
TARGET_DETAIL = "The selected cash record has an unavailable amount. Check that record with your accountant before matching."


@pytest_asyncio.fixture
async def legacy(bank_csv_pg):
    client, factory, org = bank_csv_pg
    sid = str(uuid4())
    ids = {
        name: str(uuid4())
        for name in [
            "invalid",
            "balance",
            "valid",
            "foreign",
            "receipt",
            "bad_receipt",
            "issued_payment",
            "reimbursement",
            "payment_run",
        ]
    }
    token = tenant.set_current_org(org)
    try:
        async with factory.begin() as db:
            policies = (
                await db.execute(
                    text(
                        "SELECT relname,relrowsecurity,relforcerowsecurity FROM pg_class WHERE relname IN ('bank_lines','bank_statements','receipts','payments','reimbursement_batches','payment_runs') ORDER BY relname"
                    )
                )
            ).all()
            assert len(policies) == 6 and all(row[1] and row[2] for row in policies)
            db.add(
                BankStatement(
                    id=sid,
                    org_id=org,
                    filename="synthetic-legacy.csv",
                    sha256=uuid4().hex * 2,
                    line_count=4,
                )
            )
            await db.flush()
            for name, amount, balance, currency in [
                ("invalid", "NaN", "20.00", "EUR"),
                ("balance", "10.00", "NaN", "EUR"),
                ("valid", "-10.00", None, None),
                ("foreign", "10.00", "30.00", "USD"),
            ]:
                db.add(
                    BankLine(
                        id=ids[name],
                        org_id=org,
                        statement_id=sid,
                        line_date=date(2026, 11, 6),
                        description="Synthetic " + name,
                        amount=Decimal(amount),
                        balance=Decimal(balance) if balance else None,
                        direction="debit" if name == "valid" else "credit",
                        currency=currency,
                    )
                )
            db.add_all(
                [
                    Receipt(
                        id=ids["receipt"],
                        org_id=org,
                        amount=Decimal("10.00"),
                        received_on=date(2026, 11, 6),
                    ),
                    Receipt(
                        id=ids["bad_receipt"],
                        org_id=org,
                        amount=Decimal("NaN"),
                        received_on=date(2026, 11, 6),
                    ),
                    ReimbursementBatch(
                        id=ids["reimbursement"],
                        org_id=org,
                        total_eur=Decimal("NaN"),
                        status="paid",
                        paid_at=datetime.now(UTC),
                    ),
                    PaymentRun(
                        id=ids["payment_run"],
                        org_id=org,
                        total_eur=Decimal("NaN"),
                        status="paid",
                        paid_at=datetime.now(UTC),
                    ),
                ]
            )
            invoice = IssuedInvoice(
                org_id=org,
                issue_date=date(2026, 11, 6),
                buyer_name="Synthetic target",
                seller_json="{}",
            )
            db.add(invoice)
            await db.flush()
            db.add(
                Payment(
                    id=ids["issued_payment"],
                    org_id=org,
                    issued_invoice_id=invoice.id,
                    amount=Decimal("NaN"),
                    paid_on=date(2026, 11, 6),
                )
            )
        yield client, factory, org, sid, ids
    finally:
        tenant.reset_current_org(token)


async def facts(factory, org):
    async with factory() as db:
        return {
            name: await db.scalar(
                text(
                    f"SELECT COALESCE(jsonb_agg(to_jsonb(t) ORDER BY id),'[]'::jsonb)::text FROM {name} t WHERE org_id=:org"
                ),
                {"org": org},
            )
            for name in TABLES
        }


@contextmanager
def domain_writes(factory):
    writes = []
    engine = factory.kw["bind"].sync_engine

    def observe(_connection, _cursor, statement, *_args):
        if statement.lstrip().split(None, 1)[0].upper() in {"INSERT", "UPDATE", "DELETE"} and any(
            name in statement for name in TABLES
        ):
            writes.append(statement)

    event.listen(engine, "before_cursor_execute", observe)
    try:
        yield writes
    finally:
        event.remove(engine, "before_cursor_execute", observe)


async def no_change(factory, org, request):
    before = await facts(factory, org)
    with domain_writes(factory) as writes:
        response = await request()
    assert writes == []
    assert await facts(factory, org) == before
    return response


async def test_native_legacy_reads_keep_all_lines_and_raw_money_without_writes(legacy):
    client, factory, org, sid, ids = legacy
    response = await no_change(
        factory, org, lambda: client.get(f"/api/v1/reconciliation/statements/{sid}/lines")
    )
    assert response.status_code == 200, response.text
    rows = {row["id"]: row for row in response.json()}
    assert len(rows) == 4 and "NaN" not in response.text
    assert (rows[ids["invalid"]]["amount"], rows[ids["invalid"]]["amount_status"]) == (
        None,
        "invalid",
    )
    assert (rows[ids["invalid"]]["balance"], rows[ids["invalid"]]["balance_status"]) == (
        "20.00",
        "valid",
    )
    assert (rows[ids["balance"]]["amount"], rows[ids["balance"]]["amount_status"]) == (
        "10.00",
        "valid",
    )
    assert (rows[ids["balance"]]["balance"], rows[ids["balance"]]["balance_status"]) == (
        None,
        "invalid",
    )
    assert (
        rows[ids["valid"]]["amount"],
        rows[ids["valid"]]["balance"],
        rows[ids["valid"]]["balance_status"],
        rows[ids["valid"]]["currency"],
    ) == ("-10.00", None, "unprovided", None)
    assert rows[ids["foreign"]]["currency"] == "USD"
    statements = await no_change(
        factory, org, lambda: client.get("/api/v1/reconciliation/statements")
    )
    assert statements.json()[0]["line_count"] == 4 and statements.json()[0]["unmatched"] == 4


@pytest.mark.parametrize("action", ["candidates", "match"])
async def test_native_invalid_line_returns_typed_recoverable_refusal_without_writes(legacy, action):
    client, factory, org, _sid, ids = legacy
    url = f"/api/v1/reconciliation/lines/{ids['invalid']}/{action}"
    response = await no_change(
        factory,
        org,
        lambda: (
            client.get(url)
            if action == "candidates"
            else client.post(url, json={"kind": "receipt", "target_id": ids["receipt"]})
        ),
    )
    assert response.status_code == 409, response.text
    assert response.json() == {"code": "bank_line_amount_invalid", "detail": LINE_DETAIL}


@pytest.mark.parametrize("kind", ["receipt", "issued_payment", "reimbursement", "payment_run"])
async def test_native_invalid_cash_target_is_refused_without_financial_or_line_writes(legacy, kind):
    client, factory, org, _sid, ids = legacy
    line = ids["balance"] if kind in ["receipt", "issued_payment"] else ids["valid"]
    target = ids["bad_receipt"] if kind == "receipt" else ids[kind]
    response = await no_change(
        factory,
        org,
        lambda: client.post(
            f"/api/v1/reconciliation/lines/{line}/match", json={"kind": kind, "target_id": target}
        ),
    )
    assert response.status_code == 409, response.text
    assert response.json() == {
        "code": "reconciliation_target_amount_invalid",
        "detail": TARGET_DETAIL,
    }


async def test_native_valid_amount_with_invalid_balance_keeps_candidates_and_match_rules(legacy):
    client, factory, org, _sid, ids = legacy
    cands = await no_change(
        factory,
        org,
        lambda: client.get(f"/api/v1/reconciliation/lines/{ids['balance']}/candidates"),
    )
    assert cands.status_code == 200, cands.text
    assert [(c["kind"], c["id"], c["amount"]) for c in cands.json()] == [
        ("receipt", ids["receipt"], "10.00")
    ]
    foreign = await no_change(
        factory,
        org,
        lambda: client.get(f"/api/v1/reconciliation/lines/{ids['foreign']}/candidates"),
    )
    assert foreign.status_code == 200 and foreign.json() == []
    before = await facts(factory, org)
    matched = await client.post(
        f"/api/v1/reconciliation/lines/{ids['balance']}/match",
        json={"kind": "receipt", "target_id": ids["receipt"]},
    )
    assert matched.status_code == 200, matched.text
    assert matched.json()["status"] == "matched" and matched.json()["balance_status"] == "invalid"
    after = await facts(factory, org)
    assert {k: v for k, v in after.items() if k != "bank_lines"} == {
        k: v for k, v in before.items() if k != "bank_lines"
    }


async def test_native_invalid_amount_can_be_ignored_and_restored_without_rewriting_raw_money(
    legacy,
):
    client, factory, org, sid, ids = legacy
    before = await facts(factory, org)
    ignored = await client.post(f"/api/v1/reconciliation/lines/{ids['invalid']}/ignore")
    assert ignored.status_code == 200 and ignored.json()["status"] == "ignored", ignored.text
    assert ignored.json()["amount"] is None and ignored.json()["amount_status"] == "invalid"
    restored = await client.delete(f"/api/v1/reconciliation/lines/{ids['invalid']}/match")
    assert restored.status_code == 200 and restored.json()["status"] == "unmatched", restored.text
    after = await facts(factory, org)
    assert {k: v for k, v in after.items() if k != "bank_lines"} == {
        k: v for k, v in before.items() if k != "bank_lines"
    }
    async with factory() as db:
        raw = (
            await db.execute(
                text(
                    "SELECT amount::text,balance::text FROM bank_lines WHERE org_id=:org AND id=:id"
                ),
                {"org": org, "id": ids["invalid"]},
            )
        ).one()
        assert raw == ("NaN", "20.00")
    readable = await no_change(
        factory, org, lambda: client.get(f"/api/v1/reconciliation/statements/{sid}/lines")
    )
    assert readable.status_code == 200 and len(readable.json()) == 4


async def test_native_reader_can_read_unavailable_facts_but_cannot_mutate_them(legacy):
    client, factory, org, sid, ids = legacy
    invitation = await client.post(
        "/api/v1/team/invites",
        json={"email": f"legacy-auditor-{uuid4()}@example.com", "role": "auditor"},
    )
    assert invitation.status_code == 201, invitation.text
    accepted = await client.post(
        "/api/v1/auth/accept-invite",
        json={
            "token": invitation.json()["token"],
            "name": "Synthetic legacy auditor",
            "password": "Synthetic-Legacy-Auditor-Password32",
        },
    )
    assert accepted.status_code == 201
    headers = {"Authorization": "Bearer " + accepted.json()["token"]["access_token"]}
    read = await no_change(
        factory,
        org,
        lambda: client.get(f"/api/v1/reconciliation/statements/{sid}/lines", headers=headers),
    )
    assert read.status_code == 200 and len(read.json()) == 4, read.text
    assert (
        next(row for row in read.json() if row["id"] == ids["invalid"])["amount_status"]
        == "invalid"
    )
    for action in ["match", "ignore", "restore"]:
        url = f"/api/v1/reconciliation/lines/{ids['invalid']}/{'match' if action == 'restore' else action}"
        response = await no_change(
            factory,
            org,
            lambda url=url, action=action: (
                client.delete(url, headers=headers)
                if action == "restore"
                else client.post(
                    url, headers=headers, json={"kind": "receipt", "target_id": ids["receipt"]}
                )
            ),
        )
        assert response.status_code == 403, response.text


async def test_native_foreign_legacy_rows_remain_opaque_on_every_read_or_match(legacy):
    client, factory, org, _sid, ids = legacy
    foreign, statement, line = str(uuid4()), str(uuid4()), str(uuid4())
    token = tenant.set_current_org(foreign)
    try:
        async with factory.begin() as db:
            db.add(Organization(id=foreign, name="Synthetic foreign legacy fixture"))
            await db.flush()
            db.add(
                BankStatement(
                    id=statement,
                    org_id=foreign,
                    filename="synthetic-foreign.csv",
                    sha256=uuid4().hex * 2,
                    line_count=1,
                )
            )
            await db.flush()
            db.add(
                BankLine(
                    id=line,
                    statement_id=statement,
                    org_id=foreign,
                    line_date=date(2026, 11, 6),
                    description="Synthetic foreign unavailable",
                    amount=Decimal("NaN"),
                    direction="credit",
                    currency="EUR",
                )
            )
        tenant.reset_current_org(token)
        token = tenant.set_current_org(org)
        for path in [f"/statements/{statement}/lines", f"/lines/{line}/candidates"]:
            response = await no_change(
                factory, org, lambda path=path: client.get("/api/v1/reconciliation" + path)
            )
            assert response.status_code == 404, response.text
            assert "unavailable" not in response.text and "NaN" not in response.text
        response = await no_change(
            factory,
            org,
            lambda: client.post(
                f"/api/v1/reconciliation/lines/{line}/match",
                json={"kind": "receipt", "target_id": ids["receipt"]},
            ),
        )
        assert response.status_code == 404, response.text
    finally:
        tenant.reset_current_org(token)
        token = tenant.set_current_org(foreign)
        try:
            async with factory.begin() as db:
                await db.execute(delete(Organization).where(Organization.id == foreign))
        finally:
            tenant.reset_current_org(token)
