"""FIN-AP-SETTLEMENT-BOUNDARY: real HTTP cumulative-payment conservation.

Native schedules use real independent actors and PostgreSQL transactions. Seeded
approval is an explicit synthetic fixture, not an approval-journey assertion.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.routes import invoices as invoice_routes
from app.core.database import get_session
from app.main import app
from app.models import Base
from app.models.invoice import Invoice, InvoiceStatus, WorkflowState
from app.models.payment_run import PaymentRun
from app.models.user import User
from app.models.vendor import Vendor
from app.services import payment_run

PG_URL = os.environ.get("RLS_TEST_DATABASE_URL")
pg_only = pytest.mark.skipif(not PG_URL, reason="set RLS_TEST_DATABASE_URL for native AP proof")
REQUEST = ContextVar("ap_settlement_request", default=None)


async def _seed(db, org_id, *, linked=False, children=1):
    vendor = Vendor(org_id=org_id, name="Synthetic settlement supplier")
    db.add(vendor)
    run = None
    if linked:
        run = PaymentRun(
            org_id=org_id,
            method="bank_transfer",
            status="approved",
            total_eur=Decimal("100.00") * children,
            created_by="synthetic-maker@example.com",
            approved_by="synthetic-checker@example.com",
        )
        db.add(run)
    await db.flush()
    # UUID order deliberately opposes invoice-number order. Readonly payout-file
    # ordering and the global lock ordering have different responsibilities.
    ids = sorted(str(uuid4()) for _ in range(children))
    for index, invoice_id in enumerate(ids):
        db.add(
            Invoice(
                id=invoice_id,
                org_id=org_id,
                vendor_id=vendor.id,
                invoice_number=f"SYNTH-{children - index:03}",
                issue_date=date(2026, 5, 1),
                due_date=date(2026, 12, 1),
                currency="EUR",
                subtotal=Decimal("100.00"),
                tax_amount=Decimal("0.00"),
                total=Decimal("100.00"),
                total_eur=Decimal("100.00"),
                status=InvoiceStatus.pending,
                workflow_state=WorkflowState.scheduled_for_payment,
                payment_run_id=run.id if run else None,
                version=4,
            )
        )
    await db.commit()
    return ids, run.id if run else None


async def _state(factory, org_id):
    names = (
        "invoices",
        "supplier_payments",
        "payment_runs",
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


def _json(value):
    return json.dumps(value, default=str, sort_keys=True, indent=2)


def _record(case, result):
    directory = os.environ.get("AP_SETTLEMENT_EVIDENCE_DIR")
    if not directory:
        return
    destination = Path(directory)
    destination.mkdir(parents=True, exist_ok=True)
    data = {
        "case": case,
        "first_status": result.first.status_code,
        "second_status": result.second.status_code,
        "distinct_pids": result.pids,
        "actual_pg_blocking_pids_wait": result.blocked,
        "observed_children": result.observed,
        "first_commit": result.first_state,
        "final": result.final,
        "first_commit_sha256": hashlib.sha256(_json(result.first_state).encode()).hexdigest(),
        "final_sha256": hashlib.sha256(_json(result.final).encode()).hexdigest(),
    }
    (destination / f"{case}.json").write_text(_json(data) + "\n")


@pytest_asyncio.fixture
async def native():
    if not PG_URL:
        pytest.skip("set RLS_TEST_DATABASE_URL for native AP proof")
    engine = create_async_engine(PG_URL, pool_size=6, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    async def sessions():
        async with factory() as db:
            yield db

    previous = app.dependency_overrides.get(get_session)
    app.dependency_overrides[get_session] = sessions
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            stamp = uuid4().hex[:12]
            registration = await client.post(
                "/api/v1/auth/register",
                json={
                    "organization_name": f"Synthetic settlement {stamp}",
                    "name": "Synthetic owner",
                    "email": f"settlement-{stamp}@example.com",
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
                json={"email": f"settlement-second-{stamp}@example.com", "role": "admin"},
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
            headers = {"Authorization": f"Bearer {accepted.json()['token']['access_token']}"}
            yield factory, client, org_id, headers
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_session, None)
        else:
            app.dependency_overrides[get_session] = previous
        await engine.dispose()


@dataclass
class RaceResult:
    first: object
    second: object
    first_state: dict
    final: dict
    pids: dict
    blocked: bool
    observed: dict


async def _race(
    monkeypatch, native, invoice_id, run_id, first, second, *, preload=False, override=False
):
    factory, client, org_id, second_headers = native
    original_invoice_load = invoice_routes._load_scoped
    original_run_children = payment_run.run_invoices
    entered = asyncio.Event()
    first_loaded = asyncio.Event()
    second_loaded = asyncio.Event()
    release_first = asyncio.Event()
    release_second = asyncio.Event()
    pids, observed = {}, {}
    seen = set()
    tasks = []
    preloaded = []

    async def before_load(db):
        who = REQUEST.get()
        if who in ("first", "second") and who not in seen:
            pids[who] = await db.scalar(text("SELECT pg_backend_pid()"))
            if who == "second":
                entered.set()
                if preload:
                    # Keep strong references, as a prior route/service read can.
                    # The subsequent lock must refresh these same ORM instances.
                    preloaded.extend(
                        await db.scalars(select(Invoice).where(Invoice.org_id == org_id))
                    )

    async def after_load(rows):
        who = REQUEST.get()
        if who not in ("first", "second") or who in seen:
            return
        seen.add(who)
        observed[who] = [
            {
                "id": row.id,
                "amount_paid": str(row.amount_paid),
                "workflow_state": row.workflow_state.value,
                "version": row.version,
            }
            for row in rows
        ]
        (first_loaded if who == "first" else second_loaded).set()
        await (release_first if who == "first" else release_second).wait()

    async def invoice_load(db, requested_org, requested_id, **kwargs):
        if requested_id == invoice_id:
            await before_load(db)
        row = await original_invoice_load(db, requested_org, requested_id, **kwargs)
        if requested_id == invoice_id:
            await after_load([row])
        return row

    async def run_children(db, requested_org, requested_run, **kwargs):
        if run_id is not None and requested_run == run_id:
            await before_load(db)
        rows = await original_run_children(db, requested_org, requested_run, **kwargs)
        if run_id is not None and requested_run == run_id:
            await after_load(rows)
        return rows

    monkeypatch.setattr(invoice_routes, "_load_scoped", invoice_load)
    monkeypatch.setattr(payment_run, "run_invoices", run_children)

    async def request(operation, who):
        token = REQUEST.set(who)
        try:
            headers = second_headers if who == "second" else {}
            if operation == "run":
                return await client.post(
                    f"/api/v1/payment-runs/{run_id}/pay",
                    headers=headers,
                    json={
                        "version": 1,
                        "reference": f"settlement-{who}",
                        "override_sod": override,
                    },
                )
            return await client.patch(
                f"/api/v1/invoices/{invoice_id}/payment",
                headers=headers,
                json={
                    "amount_paid": operation,
                    "paid_date": "2026-11-05",
                    "reference": f"settlement-{who}",
                },
            )
        finally:
            REQUEST.reset(token)

    try:
        tasks.append(asyncio.create_task(request(first, "first")))
        await asyncio.wait_for(first_loaded.wait(), 10)
        tasks.append(asyncio.create_task(request(second, "second")))
        await asyncio.wait_for(entered.wait(), 10)
        assert pids["first"] != pids["second"]

        async def observe_schedule():
            # On the old code the second unprotected read returns immediately.
            # Let that stale schedule complete so financial assertions expose the
            # defect. Fixed code must exhibit an actual database row-lock wait.
            async with factory() as observer:
                while True:
                    if await observer.scalar(
                        text(
                            "SELECT CAST(:holder AS integer) = ANY(pg_blocking_pids(CAST(:waiter AS integer)))"
                        ),
                        {"holder": pids["first"], "waiter": pids["second"]},
                    ):
                        return True
                    if second_loaded.is_set():
                        return False
                    assert not tasks[1].done(), "second HTTP request ended before its child read"
                    await asyncio.sleep(0.01)

        blocked = await asyncio.wait_for(observe_schedule(), 10)
        release_first.set()
        first_response = await asyncio.wait_for(tasks[0], 15)
        await asyncio.wait_for(second_loaded.wait(), 10)
        first_state = await _state(factory, org_id)
        release_second.set()
        second_response = await asyncio.wait_for(tasks[1], 15)
        return RaceResult(
            first_response,
            second_response,
            first_state,
            await _state(factory, org_id),
            pids,
            blocked,
            observed,
        )
    finally:
        release_first.set()
        release_second.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def _assert_conservation(state):
    for invoice in state["invoices"]:
        ledger = sum(
            (
                Decimal(row["amount"])
                for row in state["supplier_payments"]
                if row["invoice_id"] == invoice["id"]
            ),
            Decimal("0"),
        )
        assert ledger == Decimal(invoice["amount_paid"]), (
            f"ledger {ledger} != cached cumulative {invoice['amount_paid']} for {invoice['id']}"
        )
    # No subscriptions are seeded; this is a no-unexpected-intent assertion, not
    # evidence that invoice.paid was never emitted internally.
    assert not state["jobs"] and not state["webhook_deliveries"]


@pg_only
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "first,second",
    [
        ("40.00", "80.00"),
        ("80.00", "40.00"),
        ("40.00", "40.00"),
        ("40.00", "100.00"),
        ("100.00", "40.00"),
    ],
)
async def test_native_manual_cumulative_requests_conserve_signed_ledger(
    monkeypatch, native, first, second
):
    factory, _, org_id, _ = native
    async with factory() as db:
        ids, _ = await _seed(db, org_id)
    result = await _race(monkeypatch, native, ids[0], None, first, second)
    _record(f"manual-{first}-then-{second}", result)
    assert result.first.status_code == 200, result.first.text
    _assert_conservation(result.final)
    assert result.blocked, "concurrent settlement must wait on the invoice row"
    assert result.observed["second"][0]["amount_paid"] == first
    if first == "100.00":
        assert result.second.status_code == 422, result.second.text
        assert result.final == result.first_state
        return
    assert result.second.status_code == 200, result.second.text
    invoice = result.final["invoices"][0]
    assert invoice["amount_paid"] == Decimal(second)
    expected = [Decimal(first)] + ([Decimal(second) - Decimal(first)] if first != second else [])
    assert sorted(Decimal(row["amount"]) for row in result.final["supplier_payments"]) == sorted(
        expected
    )
    assert sum(row["action"] == "ap.payment" for row in result.final["audit_events"]) == 2
    assert invoice["workflow_state"] == (
        WorkflowState.paid if second == "100.00" else WorkflowState.partially_paid
    )
    assert invoice["version"] == (6 if second == "100.00" else 5)


@pg_only
@pytest.mark.asyncio
@pytest.mark.parametrize("first,second", [("40.00", "run"), ("run", "40.00"), ("100.00", "run")])
async def test_native_run_and_manual_requests_share_fresh_invoice_boundary(
    monkeypatch, native, first, second
):
    factory, _, org_id, _ = native
    async with factory() as db:
        ids, run_id = await _seed(db, org_id, linked=True, children=2)
    result = await _race(monkeypatch, native, ids[-1], run_id, first, second)
    _record(f"{first}-then-{second}", result)
    assert result.first.status_code == 200, result.first.text
    _assert_conservation(result.final)
    assert result.blocked, "run/manual settlement must share an invoice row lock"
    run_children = result.observed["first" if first == "run" else "second"]
    assert [row["id"] for row in run_children] == ids
    target = next(row for row in result.observed["second"] if row["id"] == ids[-1])
    assert target["amount_paid"] == ("100.00" if first == "run" else first)
    if first in ("run", "100.00"):
        assert result.second.status_code == 422, result.second.text
        assert result.final == result.first_state
        return
    assert result.second.status_code == 200, result.second.text
    run = result.final["payment_runs"][0]
    assert run["status"] == "paid" and run["version"] == 2
    assert run["reference"] == "settlement-second"
    assert run["total_eur"] == Decimal("200.00")
    for invoice in result.final["invoices"]:
        assert invoice["workflow_state"] == WorkflowState.paid
        assert invoice["status"] == InvoiceStatus.paid
        assert invoice["amount_paid"] == Decimal("100.00")
        assert invoice["version"] == (6 if invoice["id"] == ids[-1] else 5)
    assert sorted(Decimal(row["amount"]) for row in result.final["supplier_payments"]) == [
        Decimal("40.00"),
        Decimal("60.00"),
        Decimal("100.00"),
    ]
    assert sum(row["action"] == "ap.payment" for row in result.final["audit_events"]) == 1
    assert sum(row["action"] == "ap.run_paid" for row in result.final["audit_events"]) == 1


@pg_only
@pytest.mark.asyncio
@pytest.mark.parametrize("second", ["80.00", "run"])
async def test_native_locked_settlement_refreshes_preloaded_child_instances(
    monkeypatch, native, second
):
    factory, _, org_id, _ = native
    async with factory() as db:
        ids, run_id = await _seed(
            db, org_id, linked=second == "run", children=2 if second == "run" else 1
        )
    result = await _race(monkeypatch, native, ids[-1], run_id, "40.00", second, preload=True)
    _record(f"preloaded-manual-40.00-then-{second}", result)
    assert result.first.status_code == result.second.status_code == 200
    _assert_conservation(result.final)
    assert result.blocked
    target = next(row for row in result.observed["second"] if row["id"] == ids[-1])
    assert target["amount_paid"] == "40.00" and target["version"] == 5
    assert target["workflow_state"] == "partially_paid"


@pg_only
@pytest.mark.asyncio
async def test_native_run_sod_override_waits_for_children_before_tenant_audit(monkeypatch, native):
    factory, client, org_id, second_headers = native
    actor = (await client.get("/api/v1/auth/me", headers=second_headers)).json()["user"]
    async with factory() as db:
        ids, run_id = await _seed(db, org_id, linked=True, children=2)
        operator = await db.get(User, actor["id"])
        operator.is_platform_admin = True
        run = await db.get(PaymentRun, run_id)
        run.created_by = actor["email"]
        run.created_by_id = actor["id"]
        await db.commit()
    result = await _race(monkeypatch, native, ids[-1], run_id, "40.00", "run", override=True)
    _record("manual-then-explicit-platform-run-override", result)
    assert result.first.status_code == result.second.status_code == 200
    _assert_conservation(result.final)
    assert result.blocked
    assert not any(
        row["action"] == "payment_run.sod_override" for row in result.first_state["audit_events"]
    )
    overrides = [
        row for row in result.final["audit_events"] if row["action"] == "payment_run.sod_override"
    ]
    assert len(overrides) == 1 and overrides[0]["target_id"] == run_id
    assert result.final["payment_runs"][0]["status"] == "paid"


@pytest.mark.asyncio
async def test_sequential_negative_correction_and_equal_total_preserve_current_contract(
    auth_client, db_session, _db
):
    org_id = (await auth_client.get("/api/v1/auth/me")).json()["user"]["org_id"]
    ids, _ = await _seed(db_session, org_id)
    for amount in ("80.00", "40.00", "40.00"):
        response = await auth_client.patch(
            f"/api/v1/invoices/{ids[0]}/payment", json={"amount_paid": amount}
        )
        assert response.status_code == 200, response.text
    state = await _state(_db, org_id)
    _assert_conservation(state)
    assert sorted(Decimal(row["amount"]) for row in state["supplier_payments"]) == [
        Decimal("-40.00"),
        Decimal("80.00"),
    ]
    invoice = state["invoices"][0]
    assert invoice["amount_paid"] == Decimal("40.00")
    assert invoice["workflow_state"] == WorkflowState.partially_paid and invoice["version"] == 5
    assert sum(row["action"] == "ap.payment" for row in state["audit_events"]) == 3


@pytest.mark.asyncio
async def test_readonly_run_children_preserve_payout_file_number_order(auth_client, db_session):
    org_id = (await auth_client.get("/api/v1/auth/me")).json()["user"]["org_id"]
    ids, run_id = await _seed(db_session, org_id, linked=True, children=2)
    rows = await payment_run.run_invoices(db_session, org_id, run_id)
    assert [row.invoice_number for row in rows] == ["SYNTH-001", "SYNTH-002"]
    assert [row.id for row in rows] == list(reversed(ids))
