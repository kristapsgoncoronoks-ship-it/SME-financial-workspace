"""Portable HTTP checks for current-line reconciliation state and no-write replays.

Native lock schedules live separately. These checks do not imply client-version
fencing, reconciliation history, or any change to the cash movement itself.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import event, select, update

from app.models import Base
from app.models.bank_import import BankLine
from app.models.expense import ReimbursementBatch
from app.models.payment_run import PaymentRun
from app.models.receipt import Receipt
from tests.test_reconciliation import _enable, _h, _member, _upload

BASE = "/api/v1/reconciliation/lines"
TABLES = (
    "bank_statements",
    "bank_lines",
    "receipts",
    "payments",
    "reimbursement_batches",
    "payment_runs",
    "invoices",
    "supplier_payments",
    "audit_events",
    "jobs",
    "webhook_deliveries",
    "org_modules",
)


async def _state(db, org_ids):
    """Read every stored column, including BankLine's creation/update timestamps."""
    result = {}
    for name in TABLES:
        table = Base.metadata.tables[name]
        result[name] = [
            dict(row)
            for row in (
                await db.execute(
                    select(table).where(table.c.org_id.in_(org_ids)).order_by(table.c.id)
                )
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


async def _without_writes(db, org_ids, request):
    before = await _state(db, org_ids)
    with _dml(db) as writes:
        response = await request()
    assert writes == [], writes
    assert await _state(db, org_ids) == before
    return response


async def _receipt(client, *, amount="1210.00", reference="SYN-RECON-TARGET"):
    response = await client.post(
        "/api/v1/receipts",
        json={"amount": amount, "received_on": "2026-02-05", "reference": reference},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _setup(client):
    await _enable(client)
    org_id = (await client.get("/api/v1/auth/me")).json()["organization"]["id"]
    imported = await _upload(client)
    assert imported.status_code == 201, imported.text
    response = await client.get(
        f"/api/v1/reconciliation/statements/{imported.json()['statement_id']}/lines"
    )
    assert response.status_code == 200, response.text
    lines = {line["direction"]: line for line in response.json()}
    receipt = await _receipt(client)
    return org_id, lines, receipt


async def _match(client, line_id, target_id, kind="receipt", **kwargs):
    return await client.post(
        f"{BASE}/{line_id}/match", json={"kind": kind, "target_id": target_id}, **kwargs
    )


async def _action(client, action, line_id, target_id, **kwargs):
    if action == "match":
        return await _match(client, line_id, target_id, **kwargs)
    if action == "unmatch":
        return await client.delete(f"{BASE}/{line_id}/match", **kwargs)
    return await client.post(f"{BASE}/{line_id}/ignore", **kwargs)


async def test_a_second_target_cannot_replace_a_confirmed_line(auth_client, db_session):
    org_id, lines, first = await _setup(auth_client)
    second = await _receipt(auth_client, reference="SYN-SECOND-TARGET")
    line_id = lines["credit"]["id"]
    original = await _match(auth_client, line_id, first["id"])
    assert original.status_code == 200, original.text

    refused = await _without_writes(
        db_session, [org_id], lambda: _match(auth_client, line_id, second["id"])
    )
    assert refused.status_code == 400
    assert "unmatch" in refused.json()["detail"]
    stored = await db_session.scalar(select(BankLine).where(BankLine.id == line_id))
    assert (stored.status, stored.matched_kind, stored.matched_id) == (
        "matched",
        "receipt",
        first["id"],
    )


async def test_an_ignored_line_requires_restore_before_confirm(auth_client, db_session):
    org_id, lines, receipt = await _setup(auth_client)
    line_id = lines["credit"]["id"]
    ignored = await auth_client.post(f"{BASE}/{line_id}/ignore")
    assert ignored.status_code == 200 and ignored.json()["status"] == "ignored"
    refused = await _without_writes(
        db_session, [org_id], lambda: _match(auth_client, line_id, receipt["id"])
    )
    assert refused.status_code == 400 and "restore" in refused.json()["detail"]

    restored = await auth_client.delete(f"{BASE}/{line_id}/match")
    assert restored.status_code == 200 and restored.json()["status"] == "unmatched"
    assert restored.json()["matched_kind"] is None and restored.json()["matched_id"] is None
    confirmed = await _match(auth_client, line_id, receipt["id"])
    assert confirmed.status_code == 200 and confirmed.json()["matched_id"] == receipt["id"]


async def test_a_matched_line_cannot_be_ignored(auth_client, db_session):
    org_id, lines, receipt = await _setup(auth_client)
    line_id = lines["credit"]["id"]
    confirmed = await _match(auth_client, line_id, receipt["id"])
    assert confirmed.status_code == 200, confirmed.text
    refused = await _without_writes(
        db_session, [org_id], lambda: auth_client.post(f"{BASE}/{line_id}/ignore")
    )
    assert refused.status_code == 400 and "unmatch" in refused.json()["detail"]


async def test_exact_match_replay_returns_same_line_without_dml(auth_client, db_session):
    org_id, lines, receipt = await _setup(auth_client)
    line_id = lines["credit"]["id"]
    confirmed = await _match(auth_client, line_id, receipt["id"])
    assert confirmed.status_code == 200, confirmed.text
    replay = await _without_writes(
        db_session, [org_id], lambda: _match(auth_client, line_id, receipt["id"])
    )
    assert replay.status_code == 200 and replay.json() == confirmed.json()


@pytest.mark.parametrize("action", ["ignore", "unmatch"])
async def test_repeated_ignore_and_already_unmatched_restore_do_not_write(
    auth_client, db_session, action
):
    org_id, lines, receipt = await _setup(auth_client)
    line_id = lines["credit"]["id"]
    first = await _action(auth_client, action, line_id, receipt["id"])
    assert first.status_code == 200, first.text
    replay = await _without_writes(
        db_session, [org_id], lambda: _action(auth_client, action, line_id, receipt["id"])
    )
    assert replay.status_code == 200 and replay.json() == first.json()


async def test_exact_match_replay_still_revalidates_target_amount(auth_client, db_session):
    org_id, lines, receipt = await _setup(auth_client)
    line_id = lines["credit"]["id"]
    assert (await _match(auth_client, line_id, receipt["id"])).status_code == 200
    await db_session.execute(
        update(Receipt).where(Receipt.id == receipt["id"]).values(amount=Decimal("1210.03"))
    )
    await db_session.commit()
    refused = await _without_writes(
        db_session, [org_id], lambda: _match(auth_client, line_id, receipt["id"])
    )
    assert refused.status_code == 400 and "amount mismatch" in refused.json()["detail"]


async def test_exact_match_replay_still_revalidates_source_currency(auth_client, db_session):
    org_id, lines, receipt = await _setup(auth_client)
    line_id = lines["credit"]["id"]
    assert (await _match(auth_client, line_id, receipt["id"])).status_code == 200
    await db_session.execute(update(BankLine).where(BankLine.id == line_id).values(currency="USD"))
    await db_session.commit()
    refused = await _without_writes(
        db_session, [org_id], lambda: _match(auth_client, line_id, receipt["id"])
    )
    assert refused.status_code == 400
    assert "USD" in refused.json()["detail"] and "EUR" in refused.json()["detail"]


@pytest.mark.parametrize(
    "kind,model", [("reimbursement", ReimbursementBatch), ("payment_run", PaymentRun)]
)
async def test_exact_payout_replay_still_requires_a_paid_target(
    auth_client, db_session, kind, model
):
    org_id, lines, _receipt_row = await _setup(auth_client)
    target = model(
        org_id=org_id,
        status="paid",
        total_eur=Decimal("250.00"),
        paid_at=datetime(2026, 2, 10, tzinfo=UTC),
        reference="SYN-PAID-REPLAY",
    )
    db_session.add(target)
    await db_session.commit()
    line_id = lines["debit"]["id"]
    confirmed = await _match(auth_client, line_id, target.id, kind)
    assert confirmed.status_code == 200, confirmed.text
    replay = await _without_writes(
        db_session, [org_id], lambda: _match(auth_client, line_id, target.id, kind)
    )
    assert replay.status_code == 200 and replay.json() == confirmed.json()

    # Explicit stored-state fixture: replay cannot bypass the existing paid gate.
    target.status = "open"
    await db_session.commit()
    refused = await _without_writes(
        db_session, [org_id], lambda: _match(auth_client, line_id, target.id, kind)
    )
    assert refused.status_code == 400 and "only a paid" in refused.json()["detail"]


@pytest.mark.parametrize("action", ["match", "unmatch", "ignore"])
async def test_each_line_mutation_requires_payment_write(auth_client, client, db_session, action):
    org_id, lines, receipt = await _setup(auth_client)
    reader = await _member(auth_client, client, "line-reader@synthetic.example", role="user_free")
    refused = await _without_writes(
        db_session,
        [org_id],
        lambda: _action(
            auth_client, action, lines["credit"]["id"], receipt["id"], headers=_h(reader)
        ),
    )
    assert refused.status_code == 403


@pytest.mark.parametrize("action", ["match", "unmatch", "ignore"])
async def test_each_line_mutation_requires_the_issuing_module(auth_client, db_session, action):
    org_id, lines, receipt = await _setup(auth_client)
    disabled = await auth_client.put("/api/v1/modules/issuing", json={"enabled": False})
    assert disabled.status_code == 200, disabled.text
    refused = await _without_writes(
        db_session,
        [org_id],
        lambda: _action(auth_client, action, lines["credit"]["id"], receipt["id"]),
    )
    assert refused.status_code == 403 and "not activated" in refused.json()["detail"]


@pytest.mark.parametrize("action", ["match", "unmatch", "ignore"])
async def test_each_line_mutation_hides_other_tenants_lines(
    auth_client, role_client, db_session, action
):
    org_id, lines, receipt = await _setup(auth_client)
    other = await role_client("owner")
    await _enable(other)
    other_org = (await other.get("/api/v1/auth/me")).json()["organization"]["id"]
    refused = await _without_writes(
        db_session,
        [org_id, other_org],
        lambda: _action(other, action, lines["credit"]["id"], receipt["id"]),
    )
    assert refused.status_code == 404 and refused.json()["detail"] == "Bank line not found"


async def test_confirm_cannot_select_another_tenants_target(auth_client, role_client, db_session):
    org_id, lines, _receipt_row = await _setup(auth_client)
    other = await role_client("owner")
    await _enable(other)
    other_org = (await other.get("/api/v1/auth/me")).json()["organization"]["id"]
    foreign_receipt = await _receipt(other, reference="SYN-FOREIGN-TARGET")
    refused = await _without_writes(
        db_session,
        [org_id, other_org],
        lambda: _match(auth_client, lines["credit"]["id"], foreign_receipt["id"]),
    )
    assert refused.status_code == 400 and refused.json()["detail"] == "receipt not found"
