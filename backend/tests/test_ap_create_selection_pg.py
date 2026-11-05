"""Native HTTP races for initial AP run assignment, separate from pay/pay."""

from __future__ import annotations

import asyncio
import json
import os
from contextvars import ContextVar
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.database import get_session
from app.main import app
from app.models.invoice import Invoice, WorkflowState
from app.models.payment_run import PaymentRun
from app.services import audit, payment_run
from tests.test_ap_create_selection import BASE, seed_invoices
from tests.test_payment_run_cancel_export_guard import _state
from tests.test_payment_runs import _h, _member
from tests.test_reimbursement_pay_concurrency import _ex20_observe_wait

PG_URL = os.environ.get("RLS_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not PG_URL, reason="requires migrated non-superuser PostgreSQL")


@pytest_asyncio.fixture
async def selection_pg(monkeypatch):
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
            response = await client.post(
                "/api/v1/auth/register",
                json={
                    "organization_name": "Synthetic AP selection concurrency",
                    "name": "Synthetic maker",
                    "email": f"ap-selection-{uuid4()}@example.com",
                    "password": "Synthetic-AP-Selection-Password-32",
                },
            )
            assert response.status_code == 201, response.text
            identity = response.json()
            client.headers["Authorization"] = "Bearer " + identity["token"]["access_token"]
            org_id = identity["organization"]["id"]
            async with factory() as db:
                invoices = await seed_invoices(db, org_id)
            yield client, factory, org_id, [row.id for row in invoices]
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", ["same_order", "reverse_order", "overlap", "disjoint"])
@pytest.mark.parametrize("different_actor", [False, True])
@pytest.mark.parametrize("round_number", [1, 2])
async def test_concurrent_initial_selection_preserves_one_owner_and_no_orphan_run(
    selection_pg, monkeypatch, record_property, selection, different_actor, round_number
):
    client, factory, org_id, ids = selection_pg
    second_headers = (
        _h(await _member(client, client, f"second-maker-{uuid4()}@example.com"))
        if different_actor
        else {}
    )
    first_ids = ids[:2]
    second_ids = {
        "same_order": first_ids,
        "reverse_order": list(reversed(first_ids)),
        "overlap": [ids[2], ids[1]],
        "disjoint": ids[2:],
    }[selection]
    before = await _state(factory, org_id)
    role = ContextVar("ap_selection_request", default=None)
    first_loaded, second_entered, second_loaded = asyncio.Event(), asyncio.Event(), asyncio.Event()
    release_first, release_second = asyncio.Event(), asyncio.Event()
    original, pids, tasks, probes = payment_run.create_run, {}, [], []

    async def create(db, org, invoice_ids, **kwargs):
        name = role.get()
        if name is not None:
            pids[name] = await db.scalar(text("SELECT pg_backend_pid()"))
        if name == "second":
            second_entered.set()
        run = await original(db, org, invoice_ids, **kwargs)
        if name == "first":
            first_loaded.set()
            await release_first.wait()
        elif name == "second":
            second_loaded.set()
            await release_second.wait()
        return run

    monkeypatch.setattr(payment_run, "create_run", create)

    async def request(name):
        token = role.set(name)
        try:
            return await client.post(
                BASE,
                headers=second_headers if name == "second" else {},
                json={"invoice_ids": second_ids if name == "second" else first_ids},
            )
        finally:
            role.reset(token)

    try:
        tasks.append(asyncio.create_task(request("first")))
        await asyncio.wait_for(first_loaded.wait(), 10)
        tasks.append(asyncio.create_task(request("second")))
        await asyncio.wait_for(second_entered.wait(), 10)
        probes = [
            asyncio.create_task(
                _ex20_observe_wait(factory, pids["first"], pids["second"], finished=tasks[1])
            ),
            asyncio.create_task(second_loaded.wait()),
        ]
        done, _ = await asyncio.wait(probes, timeout=10, return_when=asyncio.FIRST_COMPLETED)
        assert done, (
            "Second create neither reached the old publication seam nor waited on PostgreSQL"
        )
        blocked = probes[0] in done
        observed = probes[0].result() if blocked else 0
        release_first.set()
        first = await asyncio.wait_for(tasks[0], 15)
        assert first.status_code == 201, first.text
        committed = await _state(factory, org_id)
        release_second.set()
        second = await asyncio.wait_for(tasks[1], 15)
        after = await _state(factory, org_id)
        owners = {row["id"]: row["payment_run_id"] for row in after["invoices"]}
        orphans = [
            row["id"] for row in after["payment_runs"] if row["id"] not in set(owners.values())
        ]
        diagnosis = {
            "statuses": [first.status_code, second.status_code],
            "run_count": len(after["payment_runs"]),
            "orphan_runs": orphans,
            "invoice_owners": owners,
            "actual_row_wait": blocked,
        }
        record_property("native_outcome", json.dumps(diagnosis))
        print(json.dumps(diagnosis))
        if selection == "disjoint":
            assert second.status_code == 201, second.text
            assert len(after["payment_runs"]) == len(before["payment_runs"]) + 2
            assert {owners[target] for target in first_ids} == {first.json()["id"]}
            assert {owners[target] for target in second_ids} == {second.json()["id"]}
        else:
            assert second.status_code == 422, second.text
            assert after == committed
            assert len(after["payment_runs"]) == len(before["payment_runs"]) + 1
            assert {owners[target] for target in first_ids} == {first.json()["id"]}
            assert blocked, "Overlap did not wait on the actual first selection transaction"
        assert orphans == []
        expected_increment = 2 if selection == "disjoint" else 1
        assert first.json()["invoice_count"] == len(first_ids)
        for run in after["payment_runs"]:
            linked = [row for row in after["invoices"] if row["payment_run_id"] == run["id"]]
            assert linked
            assert run["total_eur"] == sum((row["total_eur"] for row in linked), Decimal("0"))
        assert (
            sum(row["action"] == audit.A.AP_RUN_CREATE for row in after["audit_events"])
            == sum(row["action"] == audit.A.AP_RUN_CREATE for row in before["audit_events"])
            + expected_increment
        )
        assert after["supplier_payments"] == before["supplier_payments"]
        assert after["jobs"] == before["jobs"]
        assert after["webhook_deliveries"] == before["webhook_deliveries"]
        assert len({pids["first"], pids["second"]}) == 2
        record_property("selection", selection)
        record_property("different_actor", different_actor)
        record_property("round", round_number)
        record_property("observer_queries", observed)
    finally:
        release_first.set()
        release_second.set()
        for task in [*tasks, *probes]:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, *probes, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["workflow", "assigned", "binned", "fx"])
async def test_selection_refreshes_preloaded_eligibility_after_actual_row_wait(
    selection_pg, monkeypatch, record_property, changed
):
    client, factory, org_id, ids = selection_pg
    original = payment_run.create_run
    entered, pids, task = asyncio.Event(), {}, None

    async def create(db, org, invoice_ids, **kwargs):
        # A preloaded identity-map copy must not survive a later locking read.
        stale = await db.get(Invoice, invoice_ids[0])
        assert stale.workflow_state == WorkflowState.scheduled_for_payment
        pids["waiter"] = await db.scalar(text("SELECT pg_backend_pid()"))
        entered.set()
        return await original(db, org, invoice_ids, **kwargs)

    monkeypatch.setattr(payment_run, "create_run", create)
    try:
        async with factory() as blocker:
            pids["holder"] = await blocker.scalar(text("SELECT pg_backend_pid()"))
            row = await blocker.scalar(
                select(Invoice)
                .where(Invoice.org_id == org_id, Invoice.id == ids[0])
                .with_for_update()
            )
            task = asyncio.create_task(client.post(BASE, json={"invoice_ids": ids[:2]}))
            await asyncio.wait_for(entered.wait(), 10)
            observed = await asyncio.wait_for(
                _ex20_observe_wait(factory, pids["holder"], pids["waiter"], finished=task), 10
            )
            if changed == "workflow":
                row.workflow_state = WorkflowState.approved
            elif changed == "assigned":
                run = PaymentRun(
                    org_id=org_id, method="bank_transfer", status="open", total_eur=row.total_eur
                )
                blocker.add(run)
                await blocker.flush()
                row.payment_run_id = run.id
            elif changed == "binned":
                row.deleted_at = datetime.now(UTC)
                row.deleted_by = "synthetic-test@example.com"
            else:
                row.currency, row.total_eur, row.fx_source = "USD", None, "unknown"
            await blocker.commit()
            committed = await _state(factory, org_id)
            response = await asyncio.wait_for(task, 15)
        assert response.status_code == 422, response.text
        assert await _state(factory, org_id) == committed
        record_property("changed", changed)
        record_property("observed_row_wait", True)
        record_property("observer_queries", observed)
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
