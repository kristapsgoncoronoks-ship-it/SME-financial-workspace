"""Historical expense money reads and refusals on ordinary PostgreSQL.

NaN is seeded only in the owned fixture after a real finite import. Every
refused/read operation compares complete tenant-row hashes, including source,
receipt, audit and financial tables. No stored fact is repaired by the API.
"""

import asyncio
import hashlib
import json
import os
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core import tenant
from app.models import Base
from app.models.expense import ExpenseCommand, ExpenseTransaction
from tests.test_bank_csv_row_accounting_pg import bank_csv_pg as bank_csv_pg

pytestmark = pytest.mark.skipif(
    not os.environ.get("RLS_TEST_DATABASE_URL"), reason="requires migrated non-superuser PostgreSQL"
)
CSV = (
    b"date,description,amount,currency\n"
    b"2026-11-06,Synthetic historical unavailable,-12.34,EUR\n"
    b"2026-11-06,Synthetic finite control,-12.34,EUR\n"
)


async def snapshot(factory, org_id):
    scope = tenant.set_current_org(org_id)
    try:
        with tenant.include_deleted():
            async with factory() as db:
                result = {}
                for table in sorted(Base.metadata.tables.values(), key=lambda t: t.name):
                    if "org_id" not in table.c:
                        continue
                    rows = [
                        dict(row)
                        for row in (
                            await db.execute(
                                select(table)
                                .where(table.c.org_id == org_id)
                                .order_by(*table.primary_key.columns)
                            )
                        ).mappings()
                    ]
                    result[table.name] = {
                        "count": len(rows),
                        "sha256": hashlib.sha256(
                            json.dumps(rows, sort_keys=True, default=str).encode()
                        ).hexdigest(),
                    }
                return result
    finally:
        tenant.reset_current_org(scope)


async def no_change(fixture, operation):
    before = await snapshot(fixture["factory"], fixture["org"])
    response = await operation()
    after = await snapshot(fixture["factory"], fixture["org"])
    evidence = os.environ.get("EXPENSE_MONEY_EVIDENCE_DIR")
    if evidence:
        target = Path(evidence)
        target.mkdir(parents=True, exist_ok=True)
        (target / f"{uuid4().hex}.json").write_text(
            json.dumps(
                {
                    "test": os.environ.get("PYTEST_CURRENT_TEST"),
                    "org_id": fixture["org"],
                    "method": response.request.method,
                    "path": response.request.url.path,
                    "status": response.status_code,
                    "before": before,
                    "after": after,
                },
                indent=2,
            )
            + "\n"
        )
    assert after == before
    return response


async def import_copy(client, key):
    return await client.post(
        "/api/v1/expenses/import/bank-statement",
        files={"file": ("synthetic.csv", CSV, "text/csv")},
        data={"account_reference": "Synthetic expense money", "statement_currency": "EUR"},
        headers={"Idempotency-Key": key},
    )


@pytest_asyncio.fixture
async def expense_money_pg(bank_csv_pg):
    client, factory, org = bank_csv_pg
    key = str(uuid4())
    imported = await import_copy(client, key)
    assert imported.status_code == 200, imported.text
    bad, good = imported.json()["transactions"]
    report = await client.post(
        "/api/v1/expenses",
        headers={"Idempotency-Key": str(uuid4())},
        json={
            "title": "Synthetic historical money review",
            "currency": "EUR",
            "items": [
                {
                    "spend_date": "2026-11-06",
                    "description": "Synthetic finite item",
                    "amount": "12.34",
                }
            ],
        },
    )
    assert report.status_code == 201, report.text
    async with factory.begin() as db:
        original_receipt = await db.scalar(
            select(ExpenseCommand.result_json).where(
                ExpenseCommand.org_id == org, ExpenseCommand.command_key == key
            )
        )
        await db.execute(
            update(ExpenseTransaction)
            .where(ExpenseTransaction.org_id == org, ExpenseTransaction.id == bad["id"])
            .values(amount=Decimal("NaN"))
        )
    yield {
        "client": client,
        "factory": factory,
        "org": org,
        "bad": bad,
        "good": good,
        "report": report.json(),
        "key": key,
        "receipt": original_receipt,
    }


async def test_native_transactions_retain_unavailable_row_without_writes(expense_money_pg):
    f = expense_money_pg
    response = await no_change(f, lambda: f["client"].get("/api/v1/expenses/transactions"))
    assert response.status_code == 200, response.text
    rows = {row["id"]: row for row in response.json()}
    assert set(rows) == {f["bad"]["id"], f["good"]["id"]}
    assert rows[f["bad"]["id"]]["amount"] is None
    assert rows[f["bad"]["id"]]["amount_status"] == "invalid"
    assert rows[f["good"]["id"]]["amount"] == "12.34"
    assert rows[f["good"]["id"]]["amount_status"] == "valid"
    assert rows[f["bad"]["id"]]["currency"] == "EUR"


async def test_native_candidates_keep_finite_match_and_skip_unavailable(expense_money_pg):
    f = expense_money_pg
    report, item = f["report"]["id"], f["report"]["items"][0]["id"]
    response = await no_change(
        f, lambda: f["client"].get(f"/api/v1/expenses/{report}/items/{item}/match-candidates")
    )
    assert response.status_code == 200, response.text
    assert [row["id"] for row in response.json()] == [f["good"]["id"]]
    assert response.json()[0]["amount"] == "12.34"


@pytest.mark.parametrize("action", ["report", "add", "match"])
async def test_native_unavailable_selection_refused_without_any_fact_write(
    expense_money_pg, action
):
    f = expense_money_pg
    report, item = f["report"]["id"], f["report"]["items"][0]["id"]
    if action == "report":
        path = "/api/v1/expenses"
        payload = {
            "title": "Synthetic refused unavailable amount",
            "currency": "EUR",
            "transaction_ids": [f["bad"]["id"]],
            "transaction_versions": {f["bad"]["id"]: f["bad"]["version"]},
        }
    else:
        path = (
            f"/api/v1/expenses/{report}/items/from-transaction"
            if action == "add"
            else f"/api/v1/expenses/{report}/items/{item}/match"
        )
        payload = {"transaction_id": f["bad"]["id"]}
    response = await no_change(
        f,
        lambda: f["client"].post(path, json=payload, headers={"Idempotency-Key": str(uuid4())}),
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "expense_transaction_amount_invalid"
    assert isinstance(response.json()["detail"], str)


async def test_native_import_replay_reads_current_money_but_preserves_asof_receipt(
    expense_money_pg,
):
    f = expense_money_pg
    asof = json.loads(f["receipt"])
    before = await snapshot(f["factory"], f["org"])
    replay = await import_copy(f["client"], f["key"])
    assert await snapshot(f["factory"], f["org"]) == before
    assert replay.status_code == 200, replay.text
    assert replay.json()["replayed"] is True
    rows = {row["id"]: row for row in replay.json()["transactions"]}
    assert rows[f["bad"]["id"]]["amount"] is None
    assert rows[f["bad"]["id"]]["amount_status"] == "invalid"
    result = await no_change(
        f, lambda: f["client"].get(f"/api/v1/expenses/commands/{f['key']}/result")
    )
    assert result.status_code == 200, result.text
    assert result.json() == asof
    assert asof["transactions"][0]["amount"] == "12.34"
    async with f["factory"]() as db:
        assert (
            await db.scalar(
                select(ExpenseCommand.result_json).where(
                    ExpenseCommand.org_id == f["org"], ExpenseCommand.command_key == f["key"]
                )
            )
            == f["receipt"]
        )


@pytest.mark.parametrize("conflict", ["version", "currency"])
async def test_native_existing_selection_refusal_precedes_bad_money(expense_money_pg, conflict):
    f = expense_money_pg
    response = await no_change(
        f,
        lambda: f["client"].post(
            "/api/v1/expenses",
            headers={"Idempotency-Key": str(uuid4())},
            json={
                "title": "Synthetic existing selection boundary",
                "currency": "USD" if conflict == "currency" else "EUR",
                "transaction_ids": [f["bad"]["id"]],
                "transaction_versions": {
                    f["bad"]["id"]: f["bad"]["version"] + (conflict == "version")
                },
            },
        ),
    )
    assert response.status_code == (409 if conflict == "version" else 422), response.text
    assert response.json()["code"] == (
        "expense_selection_conflict" if conflict == "version" else "expense_currency_mismatch"
    )


async def test_native_safe_bin_preserves_bad_amount_and_financial_records(expense_money_pg):
    f = expense_money_pg
    before = await snapshot(f["factory"], f["org"])
    response = await f["client"].delete(
        f"/api/v1/expenses/transactions/{f['bad']['id']}?version={f['bad']['version']}",
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 204, response.text
    after = await snapshot(f["factory"], f["org"])
    for table in before:
        if table not in {"expense_transactions", "expense_commands", "audit_events"}:
            assert after[table] == before[table], table
    assert after["expense_commands"]["count"] == before["expense_commands"]["count"] + 1
    assert after["audit_events"]["count"] == before["audit_events"]["count"] + 1
    with tenant.include_deleted():
        async with f["factory"]() as db:
            row = await db.scalar(
                select(ExpenseTransaction).where(
                    ExpenseTransaction.org_id == f["org"],
                    ExpenseTransaction.id == f["bad"]["id"],
                )
            )
            assert row.amount.is_nan()
            assert row.currency == "EUR" and row.item_id is None
            assert row.deleted_at is not None and row.version == f["bad"]["version"] + 1


async def test_native_report_receipt_replay_is_asof_after_assigned_source_changes(expense_money_pg):
    f = expense_money_pg
    key = str(uuid4())
    body = {
        "title": "Synthetic committed finite report",
        "currency": "EUR",
        "transaction_ids": [f["good"]["id"]],
        "transaction_versions": {f["good"]["id"]: f["good"]["version"]},
    }
    first = await f["client"].post("/api/v1/expenses", json=body, headers={"Idempotency-Key": key})
    assert first.status_code == 201, first.text
    assert first.json()["total"] == "12.34"
    assert "amount_status" not in first.json()["items"][0]
    async with f["factory"].begin() as db:
        await db.execute(
            update(ExpenseTransaction)
            .where(ExpenseTransaction.org_id == f["org"], ExpenseTransaction.id == f["good"]["id"])
            .values(amount=Decimal("NaN"))
        )
    replay = await no_change(
        f,
        lambda: f["client"].post("/api/v1/expenses", json=body, headers={"Idempotency-Key": key}),
    )
    assert replay.status_code == 201, replay.text
    assert replay.json() == first.json()
    result = await no_change(f, lambda: f["client"].get(f"/api/v1/expenses/commands/{key}/result"))
    assert result.status_code == 200 and result.json() == first.json()


async def test_native_old_import_receipt_without_amount_status_stays_exact(expense_money_pg):
    """Explicit old-wire fixture, not a rewrite by any application operation."""
    f = expense_money_pg
    legacy = json.loads(f["receipt"])
    for row in legacy["transactions"]:
        row.pop("amount_status", None)
    key = "historical-amount:" + uuid4().hex
    stored = json.dumps(legacy, sort_keys=True)
    async with f["factory"].begin() as db:
        original = await db.scalar(
            select(ExpenseCommand).where(
                ExpenseCommand.org_id == f["org"], ExpenseCommand.command_key == f["key"]
            )
        )
        db.add(
            ExpenseCommand(
                org_id=f["org"],
                employee_id=original.employee_id,
                command_key=key,
                operation="statement_imported",
                payload_hash=original.payload_hash,
                target_id=original.target_id,
                result_json=stored,
            )
        )
    for suffix in ["", "/result"]:
        response = await no_change(
            f,
            lambda suffix=suffix: f["client"].get(f"/api/v1/expenses/commands/{key}{suffix}"),
        )
        assert response.status_code == 200, response.text
        assert (response.json() if suffix else response.json()["result"]) == legacy
    async with f["factory"]() as db:
        assert (
            await db.scalar(
                select(ExpenseCommand.result_json).where(
                    ExpenseCommand.org_id == f["org"], ExpenseCommand.command_key == key
                )
            )
            == stored
        )


async def transaction_rows(factory, org):
    table = ExpenseTransaction.__table__
    async with factory() as db:
        rows = (
            await db.execute(select(table).where(table.c.org_id == org).order_by(table.c.id))
        ).mappings()
        return json.loads(json.dumps([dict(row) for row in rows], default=str))


@pytest_asyncio.fixture
async def expense_money_blocker():
    # The HTTP fixture has two pooled slots used by its request dependencies.
    # A separate connection proves a database waiter rather than starving that
    # test-only pool before the request reaches its transaction lock.
    engine = create_async_engine(os.environ["RLS_TEST_DATABASE_URL"], pool_size=1, max_overflow=0)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    finally:
        await engine.dispose()


async def test_native_waiting_report_rechecks_money_after_real_transaction_lock(
    expense_money_pg, expense_money_blocker
):
    f = expense_money_pg
    before = await snapshot(f["factory"], f["org"])
    expected_rows = await transaction_rows(f["factory"], f["org"])
    next(row for row in expected_rows if row["id"] == f["good"]["id"])["amount"] = "NaN"
    async with expense_money_blocker() as blocker:
        locked = await blocker.scalar(
            select(ExpenseTransaction)
            .where(ExpenseTransaction.org_id == f["org"], ExpenseTransaction.id == f["good"]["id"])
            .with_for_update()
        )
        assert locked is not None
        blocker_pid = await blocker.scalar(text("SELECT pg_backend_pid()"))
        request = asyncio.create_task(
            f["client"].post(
                "/api/v1/expenses",
                headers={"Idempotency-Key": str(uuid4())},
                json={
                    "title": "Synthetic amount changed during lock wait",
                    "currency": "EUR",
                    "transaction_ids": [f["good"]["id"]],
                    "transaction_versions": {f["good"]["id"]: f["good"]["version"]},
                },
            )
        )
        try:
            for _ in range(200):
                await blocker.execute(text("SELECT pg_stat_clear_snapshot()"))
                blocked = await blocker.scalar(
                    text(
                        "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=current_database() "
                        "AND wait_event_type='Lock' AND query LIKE '%expense_transactions%' "
                        "AND :blocker=ANY(pg_blocking_pids(pid)))"
                    ),
                    {"blocker": blocker_pid},
                )
                if blocked:
                    break
                await asyncio.sleep(0.025)
            assert blocked, "Did not observe the independent native transaction-lock waiter"
            await blocker.execute(
                text(
                    "UPDATE expense_transactions SET amount='NaN'::numeric "
                    "WHERE org_id=:org AND id=:id"
                ),
                {"org": f["org"], "id": f["good"]["id"]},
            )
            await blocker.commit()
            response = await asyncio.wait_for(request, timeout=10)
        finally:
            await blocker.rollback()
            if not request.done():
                try:
                    await asyncio.wait_for(asyncio.shield(request), timeout=10)
                except TimeoutError:
                    request.cancel()
            await asyncio.gather(request, return_exceptions=True)
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "expense_transaction_amount_invalid"
    after = await snapshot(f["factory"], f["org"])
    assert {k: v for k, v in after.items() if k != "expense_transactions"} == {
        k: v for k, v in before.items() if k != "expense_transactions"
    }
    assert await transaction_rows(f["factory"], f["org"]) == expected_rows
