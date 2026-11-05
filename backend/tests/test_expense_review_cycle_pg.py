"""Real report-row waits join owner mutations to the approval boundary."""

from __future__ import annotations

import asyncio
import os
from contextvars import ContextVar
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.routes import expenses as route
from app.core.database import get_session
from app.main import app
from app.models.expense import ExpenseTransaction
from app.services import expense_commands
from tests.test_expense_decision_version import _PNG, _h
from tests.test_expense_review_cycle import BASE, financial_state, report_fixture
from tests.test_reimbursement_pay_concurrency import _ex20_observe_wait

PG_URL = os.environ.get("RLS_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not PG_URL, reason="requires migrated non-superuser PostgreSQL")


@pytest_asyncio.fixture
async def review_pg(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "smtp_host", None)
    engine = create_async_engine(PG_URL, pool_size=6, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    async def session():
        async with factory() as db:
            yield db

    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_session] = session
    try:
        async with factory() as db:
            assert (
                await db.scalar(
                    text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user")
                )
                is False
            )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            registered = await client.post(
                "/api/v1/auth/register",
                json={
                    "organization_name": "Synthetic report review concurrency",
                    "name": "Synthetic reviewer",
                    "email": f"review-owner-{uuid4()}@example.com",
                    "password": "Synthetic-Review-Cycle-Password-32",
                },
            )
            assert registered.status_code == 201, registered.text
            identity = registered.json()
            client.headers["Authorization"] = "Bearer " + identity["token"]["access_token"]
            yield client, factory, identity["organization"]["id"]
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "first_action,second_action",
    [
        ("withdraw", "decision"),
        ("decision", "withdraw"),
        ("reassign", "decision"),
        ("decision", "reassign"),
    ],
)
async def test_withdraw_and_decision_serialize_one_review_cycle(
    review_pg, monkeypatch, record_property, first_action, second_action
):
    client, factory, org_id = review_pg
    employee, report = await report_fixture(client, client)
    owner_id = (await client.get("/api/v1/auth/me")).json()["user"]["id"]
    report_id = report["id"]
    role = ContextVar("review_cycle_request", default=None)
    first_loaded, second_entered, second_loaded = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    release_first, release_second = asyncio.Event(), asyncio.Event()
    pids, tasks, probes = {}, [], []
    original, original_actor = route._load, expense_commands.lock_actor

    async def actor(db, current, **kwargs):
        # Same-actor reviews now serialize on live membership before reaching
        # the report. Observe that actual earlier wait rather than waiting for
        # a report-loader hook the protected request cannot reach yet.
        if role.get() == "second":
            pids["second"] = await db.scalar(text("SELECT pg_backend_pid()"))
            second_entered.set()
        return await original_actor(db, current, **kwargs)

    async def load(db, org, target, *, lock=False):
        name = role.get()
        if target == report_id and name == "second":
            pids[name] = await db.scalar(text("SELECT pg_backend_pid()"))
            second_entered.set()
        row = await original(db, org, target, lock=lock)
        if target == report_id and name == "first":
            pids[name] = await db.scalar(text("SELECT pg_backend_pid()"))
            first_loaded.set()
            await release_first.wait()
        elif target == report_id and name == "second":
            second_loaded.set()
            await release_second.wait()
        return row

    monkeypatch.setattr(route, "_load", load)
    monkeypatch.setattr(expense_commands, "lock_actor", actor)

    async def request(name, action):
        token = role.set(name)
        try:
            if action == "withdraw":
                return await client.post(f"{BASE}/{report_id}/withdraw", headers=_h(employee))
            if action == "reassign":
                return await client.post(
                    f"{BASE}/{report_id}/reassign", json={"approver_id": owner_id}
                )
            return await client.post(
                f"{BASE}/{report_id}/decision",
                json={"action": "approve", "version": report["version"]},
            )
        finally:
            role.reset(token)

    try:
        tasks.append(asyncio.create_task(request("first", first_action)))
        await asyncio.wait_for(first_loaded.wait(), 10)
        tasks.append(asyncio.create_task(request("second", second_action)))
        await asyncio.wait_for(second_entered.wait(), 10)
        probes = [
            asyncio.create_task(
                _ex20_observe_wait(factory, pids["first"], pids["second"], finished=tasks[1])
            ),
            asyncio.create_task(second_loaded.wait()),
        ]
        done, _ = await asyncio.wait(probes, timeout=10, return_when=asyncio.FIRST_COMPLETED)
        assert probes[0] in done, "Competing review mutation bypassed protected review admission"
        observed = probes[0].result()
        relation = (
            "memberships"
            if first_action != "withdraw" and second_action != "withdraw"
            else "expense_reports"
        )
        async with factory() as observer:
            query = await observer.scalar(
                text("SELECT query FROM pg_stat_activity WHERE pid = :pid"),
                {"pid": pids["second"]},
            )
        assert f"FROM {relation}" in query, query
        release_first.set()
        first = await asyncio.wait_for(tasks[0], 15)
        assert first.status_code == 200, first.text
        await asyncio.wait_for(second_loaded.wait(), 10)
        committed = await financial_state(factory, org_id)
        release_second.set()
        second = await asyncio.wait_for(tasks[1], 15)
        assert second.status_code == 409, second.text
        assert await financial_state(factory, org_id) == committed
        current = (await client.get(f"{BASE}/{report_id}")).json()
        assert (
            current["status"]
            == {"withdraw": "draft", "decision": "approved", "reassign": "submitted"}[first_action]
        )
        assert current["version"] == report["version"] + 1
        record_property("first_action", first_action)
        record_property("observed_review_transaction_wait", True)
        record_property("observed_lock_relation", relation)
        record_property("observer_queries", observed)
    finally:
        release_first.set()
        release_second.set()
        for task in [*tasks, *probes]:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, *probes, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation", ["report", "item", "add", "receipt", "bin", "assign", "match", "unmatch"]
)
@pytest.mark.parametrize("first_action", ["mutation", "submit"])
async def test_editable_mutations_and_submission_share_actual_report_lock(
    review_pg, monkeypatch, record_property, mutation, first_action
):
    client, factory, org_id = review_pg
    employee, report = await report_fixture(client, client, submitted=False)
    report_id, item_id = report["id"], report["items"][0]["id"]
    transaction_id = None
    if mutation in {"assign", "match", "unmatch"}:
        async with factory() as db:
            transaction = ExpenseTransaction(
                org_id=org_id,
                employee_id=report["employee_id"],
                txn_date=date(2026, 5, 1),
                description="Synthetic owned card transaction",
                amount=Decimal("42.00"),
                currency="EUR",
                source="card",
                status="available",
            )
            db.add(transaction)
            await db.commit()
            transaction_id = transaction.id
        if mutation == "unmatch":
            response = await client.post(
                f"{BASE}/{report_id}/items/{item_id}/match",
                headers=_h(employee),
                json={"transaction_id": transaction_id},
            )
            assert response.status_code == 200, response.text
    role = ContextVar("editable_review_request", default=None)
    first_loaded, second_entered, second_loaded = asyncio.Event(), asyncio.Event(), asyncio.Event()
    release_first, release_second = asyncio.Event(), asyncio.Event()
    original, pids, tasks, probes = route._load, {}, [], []

    async def load(db, org, target, *, lock=False):
        name = role.get()
        if target == report_id and name == "second":
            pids[name] = await db.scalar(text("SELECT pg_backend_pid()"))
            second_entered.set()
        try:
            row = await original(db, org, target, lock=lock)
        except Exception:
            if target == report_id and name == "second":
                second_loaded.set()
                await release_second.wait()
            raise
        if target == report_id and name == "first":
            pids[name] = await db.scalar(text("SELECT pg_backend_pid()"))
            first_loaded.set()
            await release_first.wait()
        elif target == report_id and name == "second":
            second_loaded.set()
            await release_second.wait()
        return row

    monkeypatch.setattr(route, "_load", load)
    item_body = {
        "spend_date": "2026-05-01",
        "category": "meals",
        "description": "Reviewed new contents",
        "amount": "84.00",
        "comment": "Declared synthetic purpose",
        "missing_receipt_declaration": "Synthetic missing original",
    }

    async def request(name, action):
        token = role.set(name)
        path, headers = f"{BASE}/{report_id}", _h(employee)
        try:
            if action == "submit":
                return await client.post(f"{path}/submit", headers=headers)
            if mutation == "report":
                return await client.patch(path, headers=headers, json={"items": [item_body]})
            if mutation == "item":
                return await client.patch(
                    f"{path}/items/{item_id}", headers=headers, json={"amount": "84.00"}
                )
            if mutation == "add":
                return await client.post(f"{path}/items", headers=headers, json=item_body)
            if mutation == "receipt":
                return await client.post(
                    f"{path}/items/{item_id}/receipt",
                    headers=headers,
                    files={"file": ("synthetic.png", _PNG, "image/png")},
                )
            if mutation == "bin":
                return await client.delete(path, headers=headers)
            if mutation == "assign":
                return await client.post(
                    f"{path}/items/from-transaction",
                    headers=headers,
                    json={"transaction_id": transaction_id},
                )
            if mutation == "match":
                return await client.post(
                    f"{path}/items/{item_id}/match",
                    headers=headers,
                    json={"transaction_id": transaction_id},
                )
            return await client.delete(f"{path}/items/{item_id}/match", headers=headers)
        finally:
            role.reset(token)

    try:
        tasks.append(asyncio.create_task(request("first", first_action)))
        await asyncio.wait_for(first_loaded.wait(), 10)
        tasks.append(
            asyncio.create_task(
                request("second", "submit" if first_action == "mutation" else "mutation")
            )
        )
        await asyncio.wait_for(second_entered.wait(), 10)
        probes = [
            asyncio.create_task(
                _ex20_observe_wait(factory, pids["first"], pids["second"], finished=tasks[1])
            ),
            asyncio.create_task(second_loaded.wait()),
        ]
        done, _ = await asyncio.wait(probes, timeout=10, return_when=asyncio.FIRST_COMPLETED)
        assert probes[0] in done, f"{mutation} bypassed the actual report row lock"
        observed = probes[0].result()
        release_first.set()
        first = await asyncio.wait_for(tasks[0], 15)
        expected_first = (
            {"add": 201, "bin": 204}.get(mutation, 200) if first_action == "mutation" else 200
        )
        assert first.status_code == expected_first, first.text
        await asyncio.wait_for(second_loaded.wait(), 10)
        committed = await financial_state(factory, org_id)
        release_second.set()
        second = await asyncio.wait_for(tasks[1], 15)
        expected_second = (
            (404 if mutation == "bin" else 422 if mutation == "assign" else 200)
            if first_action == "mutation"
            else (403 if mutation == "bin" else 409)
        )
        assert second.status_code == expected_second, second.text
        after = await financial_state(factory, org_id)
        if expected_second != 200:
            assert after == committed
        rows = after["expense_reports"]
        assert len(rows) == 1
        if mutation == "bin" and first_action == "mutation":
            assert rows[0]["deleted_at"] is not None and rows[0]["status"] == "draft"
        elif mutation == "assign" and first_action == "mutation":
            assert rows[0]["status"] == "draft" and rows[0]["total"] == Decimal("84.00")
        else:
            assert rows[0]["status"] == "submitted"
            if mutation in {"report", "item", "add"}:
                expected_total = Decimal("42.00")
                if first_action == "mutation":
                    expected_total = Decimal("126.00") if mutation == "add" else Decimal("84.00")
                assert rows[0]["total"] == expected_total
        record_property("mutation", mutation)
        record_property("first_action", first_action)
        record_property("observed_report_row_wait", True)
        record_property("observer_queries", observed)
    finally:
        release_first.set()
        release_second.set()
        for task in [*tasks, *probes]:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, *probes, return_exceptions=True)
