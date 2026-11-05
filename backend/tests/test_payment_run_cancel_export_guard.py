"""FIN-AP-EXPORTED-CANCEL-GUARD: bank-file evidence never releases AP invoices.

Approval is an explicit synthetic fixture. These are real HTTP transactions and
committed rows; PostgreSQL cases observe the original route's lock directly.
"""

from __future__ import annotations

import asyncio
import os
from contextvars import ContextVar
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.routes import payment_runs as routes
from app.core.database import get_session
from app.main import app
from app.models import Base
from app.models.invoice import Invoice, InvoiceStatus, WorkflowState
from app.models.issuer import IssuerProfile
from app.models.payment_run import PaymentRun
from app.models.vendor import Vendor

BASE = "/api/v1/payment-runs"
PG_URL = os.environ.get("RLS_TEST_DATABASE_URL")
pg_only = pytest.mark.skipif(
    not PG_URL, reason="set RLS_TEST_DATABASE_URL for native AP lock proof"
)


async def _seed(db, org_id, *, status="approved", **markers):
    vendor = Vendor(
        org_id=org_id,
        name="Synthetic AP supplier",
        iban="DE89370400440532013000",
        bic="COBADEFFXXX",
    )
    db.add(vendor)
    db.add(
        IssuerProfile(
            org_id=org_id,
            legal_name="Synthetic AP payer",
            is_default=True,
            iban="NL91ABNA0417164300",
            bic="ABNANL2A",
        )
    )
    run = PaymentRun(
        org_id=org_id,
        method="bank_transfer",
        status=status,
        total_eur=Decimal("100.00"),
        created_by="synthetic-maker@example.com",
        approved_by="synthetic-checker@example.com" if status == "approved" else None,
        **markers,
    )
    db.add(run)
    await db.flush()
    invoice = Invoice(
        org_id=org_id,
        vendor_id=vendor.id,
        invoice_number=f"AP-GUARD-{uuid4().hex[:10]}",
        issue_date=date(2026, 5, 1),
        due_date=date(2026, 12, 1),
        currency="EUR",
        subtotal=Decimal("100.00"),
        tax_amount=Decimal("0.00"),
        total=Decimal("100.00"),
        total_eur=Decimal("100.00"),
        status=InvoiceStatus.pending,
        workflow_state=WorkflowState.scheduled_for_payment,
        payment_run_id=run.id,
        version=4,
    )
    db.add(invoice)
    await db.commit()
    return run.id, invoice.id


async def _state(factory, org_id):
    names = (
        "payment_runs",
        "invoices",
        "supplier_payments",
        "audit_events",
        "jobs",
        "webhook_deliveries",
    )
    async with factory() as db:
        return {
            name: [
                dict(row)
                for row in (
                    await db.execute(
                        select(Base.metadata.tables[name])
                        .where(Base.metadata.tables[name].c.org_id == org_id)
                        .order_by(Base.metadata.tables[name].c.id)
                    )
                ).mappings()
            ]
            for name in names
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["open", "approved"])
@pytest.mark.parametrize(
    "markers",
    [
        {"export_count": 1},
        {"export_count": -1},
        {"exported_at": datetime(2026, 11, 5, tzinfo=UTC)},
        {"last_msg_id": "AP-SYNTHETIC-LEGACY"},
        {"last_msg_id": ""},
    ],
    ids=["count", "negative-count", "timestamp", "message", "empty-message"],
)
async def test_recorded_export_refuses_cancel_with_exactly_no_writes(
    auth_client, db_session, _db, status, markers
):
    org_id = (await auth_client.get("/api/v1/auth/me")).json()["user"]["org_id"]
    run_id, _ = await _seed(db_session, org_id, status=status, **markers)
    before = await _state(_db, org_id)
    response = await auth_client.delete(f"{BASE}/{run_id}")
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "run_exported_cannot_cancel"
    assert "bank" in response.json()["detail"] and "accountant" in response.json()["detail"]
    assert await _state(_db, org_id) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["open", "approved"])
async def test_unexported_cancel_preserves_existing_invoice_release(
    auth_client, db_session, _db, status
):
    org_id = (await auth_client.get("/api/v1/auth/me")).json()["user"]["org_id"]
    run_id, invoice_id = await _seed(db_session, org_id, status=status)
    response = await auth_client.delete(f"{BASE}/{run_id}")
    assert response.status_code == 204, response.text
    state = await _state(_db, org_id)
    assert state["payment_runs"][0]["status"] == "cancelled"
    assert state["payment_runs"][0]["version"] == 2
    invoice = next(row for row in state["invoices"] if row["id"] == invoice_id)
    assert invoice["payment_run_id"] is None
    assert invoice["workflow_state"] == WorkflowState.scheduled_for_payment
    assert invoice["version"] == 4 and invoice["amount_paid"] == Decimal("0.00")
    assert sum(row["action"] == "ap.run_cancel" for row in state["audit_events"]) == 1
    assert not state["supplier_payments"] and not state["jobs"] and not state["webhook_deliveries"]


@pg_only
@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["csv", "sepa"])
@pytest.mark.parametrize("first", ["export", "cancel"])
async def test_native_export_cancel_route_orders_preserve_exact_winner_state(
    monkeypatch, kind, first
):
    engine = create_async_engine(PG_URL, pool_size=4, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    async def sessions():
        async with factory() as db:
            yield db

    original_override = app.dependency_overrides.get(get_session)
    app.dependency_overrides[get_session] = sessions
    loaded = asyncio.Event()
    entered = asyncio.Event()
    second_loaded = asyncio.Event()
    release = asyncio.Event()
    release_second = asyncio.Event()
    actor = ContextVar("ap_export_cancel_request", default=None)
    pids = {}
    tasks = []
    original_load = routes._load
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            stamp = uuid4().hex[:12]
            registration = await client.post(
                "/api/v1/auth/register",
                json={
                    "organization_name": f"Synthetic AP guard {stamp}",
                    "name": "Synthetic operator",
                    "email": f"ap-guard-{stamp}@example.com",
                    "password": "synthetic-test-password",
                },
            )
            assert registration.status_code == 201, registration.text
            org_id = registration.json()["organization"]["id"]
            client.headers["Authorization"] = (
                f"Bearer {registration.json()['token']['access_token']}"
            )
            invitation = await client.post(
                "/api/v1/team/invites",
                json={"email": f"ap-second-{stamp}@example.com", "role": "admin"},
            )
            assert invitation.status_code == 201, invitation.text
            accepted = await client.post(
                "/api/v1/auth/accept-invite",
                json={
                    "token": invitation.json()["token"],
                    "name": "Synthetic second operator",
                    "password": "synthetic-test-password",
                },
            )
            assert accepted.status_code == 201, accepted.text
            second_headers = {"Authorization": f"Bearer {accepted.json()['token']['access_token']}"}
            async with factory() as db:
                run_id, invoice_id = await _seed(db, org_id)

            async def observe_load(db, requested_org, requested_run, *, lock=False):
                who = actor.get()
                if requested_run == run_id and who == "second":
                    pids["second"] = await db.scalar(text("SELECT pg_backend_pid()"))
                    entered.set()
                row = await original_load(db, requested_org, requested_run, lock=lock)
                if requested_run == run_id and who == "first":
                    pids["first"] = await db.scalar(text("SELECT pg_backend_pid()"))
                    loaded.set()
                    await release.wait()
                elif requested_run == run_id and who == "second":
                    second_loaded.set()
                    await release_second.wait()
                return row

            monkeypatch.setattr(routes, "_load", observe_load)

            async def request(operation, who):
                token = actor.set(who)
                try:
                    if operation == "cancel":
                        return await client.delete(
                            f"{BASE}/{run_id}", headers=second_headers if who == "second" else {}
                        )
                    path = "export" if kind == "csv" else "sepa"
                    return await client.post(
                        f"{BASE}/{run_id}/{path}", headers=second_headers if who == "second" else {}
                    )
                finally:
                    actor.reset(token)

            tasks.append(asyncio.create_task(request(first, "first")))
            await asyncio.wait_for(loaded.wait(), 10)
            tasks.append(
                asyncio.create_task(request("cancel" if first == "export" else "export", "second"))
            )
            await asyncio.wait_for(entered.wait(), 10)
            assert pids["first"] != pids["second"]

            async def blocked():
                async with factory() as observer:
                    while not await observer.scalar(
                        text(
                            "SELECT CAST(:holder AS integer) = ANY(pg_blocking_pids(CAST(:waiter AS integer)))"
                        ),
                        {"holder": pids["first"], "waiter": pids["second"]},
                    ):
                        assert not tasks[1].done(), (
                            "losing HTTP route did not wait for the run lock"
                        )
                        await asyncio.sleep(0.01)

            await asyncio.wait_for(blocked(), 10)
            release.set()
            winner = await asyncio.wait_for(tasks[0], 15)
            assert winner.status_code == (200 if first == "export" else 204), winner.text
            await asyncio.wait_for(second_loaded.wait(), 10)
            committed = await _state(factory, org_id)
            release_second.set()
            loser = await asyncio.wait_for(tasks[1], 15)
            assert loser.status_code == 409, loser.text
            assert loser.json()["code"] == (
                "run_exported_cannot_cancel" if first == "export" else "run_not_exportable"
            )
            assert await _state(factory, org_id) == committed
            run = committed["payment_runs"][0]
            invoice = next(row for row in committed["invoices"] if row["id"] == invoice_id)
            assert invoice["version"] == 4 and invoice["amount_paid"] == Decimal("0.00")
            assert (
                not committed["supplier_payments"]
                and not committed["jobs"]
                and not committed["webhook_deliveries"]
            )
            if first == "export":
                assert run["status"] == "approved" and run["version"] == 1
                assert run["export_count"] == 1 and run["exported_at"] is not None
                assert bool(run["last_msg_id"]) == (kind == "sepa")
                assert invoice["payment_run_id"] == run_id
                assert (
                    sum(
                        row["action"] == "payment_run.exported" for row in committed["audit_events"]
                    )
                    == 1
                )
                assert not any(
                    row["action"] == "ap.run_cancel" for row in committed["audit_events"]
                )
            else:
                assert run["status"] == "cancelled" and run["version"] == 2
                assert (
                    run["export_count"] == 0
                    and run["exported_at"] is None
                    and run["last_msg_id"] is None
                )
                assert invoice["payment_run_id"] is None
                assert (
                    sum(row["action"] == "ap.run_cancel" for row in committed["audit_events"]) == 1
                )
                assert not any(
                    row["action"] == "payment_run.exported" for row in committed["audit_events"]
                )
    finally:
        release.set()
        release_second.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if original_override is None:
            app.dependency_overrides.pop(get_session, None)
        else:
            app.dependency_overrides[get_session] = original_override
        await engine.dispose()
