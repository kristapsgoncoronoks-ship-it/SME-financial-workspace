"""Exact AP run selection; scheduled invoice state is declared synthetic setup."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.invoice import Invoice, InvoiceStatus, WorkflowState
from app.models.vendor import Vendor
from tests.test_payment_run_cancel_export_guard import _state

BASE = "/api/v1/payment-runs"


async def seed_invoices(db, org_id, count=4):
    vendor = Vendor(org_id=org_id, name="Synthetic AP selection supplier")
    db.add(vendor)
    await db.flush()
    rows = []
    for index in range(count):
        value = Decimal((index + 1) * 10)
        row = Invoice(
            org_id=org_id,
            vendor_id=vendor.id,
            invoice_number=f"AP-SELECT-{index}-{uuid4().hex[:8]}",
            issue_date=date(2026, 5, 1),
            currency="EUR",
            subtotal=value,
            tax_amount=Decimal("0"),
            total=value,
            total_eur=value,
            status=InvoiceStatus.pending,
            workflow_state=WorkflowState.scheduled_for_payment,
        )
        db.add(row)
        rows.append(row)
    await db.commit()
    return rows


@pytest.mark.asyncio
async def test_duplicate_invoice_selection_is_refused_without_any_write(
    auth_client, db_session, _db
):
    org_id = (await auth_client.get("/api/v1/auth/me")).json()["user"]["org_id"]
    rows = await seed_invoices(db_session, org_id)
    before = await _state(_db, org_id)
    response = await auth_client.post(BASE, json={"invoice_ids": [rows[0].id, rows[0].id]})
    assert response.status_code == 422, response.text
    assert await _state(_db, org_id) == before


@pytest.mark.asyncio
async def test_unique_selection_preserves_exact_count_amount_and_links(auth_client, db_session):
    from app.models.audit import AuditEvent
    from app.services import audit

    org_id = (await auth_client.get("/api/v1/auth/me")).json()["user"]["org_id"]
    rows = await seed_invoices(db_session, org_id)
    chosen = [rows[2].id, rows[0].id]
    response = await auth_client.post(BASE, json={"invoice_ids": chosen})
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["invoice_count"] == 2 and result["total_eur"] == "40.00"
    assert {row["id"] for row in result["invoices"]} == set(chosen)
    event = await db_session.scalar(
        select(AuditEvent).where(
            AuditEvent.org_id == org_id, AuditEvent.action == audit.A.AP_RUN_CREATE
        )
    )
    assert json.loads(event.meta)["invoices"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["missing", "already_assigned", "workflow", "fx"])
async def test_invalid_selection_refuses_the_whole_new_run(auth_client, db_session, _db, invalid):
    org_id = (await auth_client.get("/api/v1/auth/me")).json()["user"]["org_id"]
    rows = await seed_invoices(db_session, org_id)
    selected = [rows[0].id, rows[1].id]
    if invalid == "missing":
        selected.append(str(uuid4()))
    elif invalid == "already_assigned":
        existing = await auth_client.post(BASE, json={"invoice_ids": [rows[0].id]})
        assert existing.status_code == 201, existing.text
    elif invalid == "workflow":
        rows[0].workflow_state = WorkflowState.approved
        await db_session.commit()
    else:
        rows[0].currency, rows[0].total_eur, rows[0].fx_source = "USD", None, "unknown"
        await db_session.commit()
    before = await _state(_db, org_id)
    response = await auth_client.post(BASE, json={"invoice_ids": selected})
    assert response.status_code == 422, response.text
    assert await _state(_db, org_id) == before
