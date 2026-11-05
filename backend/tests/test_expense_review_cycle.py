"""A reviewed submission cannot authorize changed contents in a later cycle."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models import Base
from tests.test_expense_decision_version import _activate, _h, _member

BASE = "/api/v1/expenses"


async def report_fixture(owner, client, *, submitted=True):
    await _activate(owner)
    employee = await _member(owner, client, f"review-cycle-{uuid4()}@example.com")
    response = await client.post(
        BASE,
        headers=_h(employee),
        json={
            "title": "Synthetic reviewed report",
            "items": [
                {
                    "spend_date": "2026-05-01",
                    "category": "meals",
                    "description": "Synthetic client meal",
                    "amount": "42.00",
                    "comment": "Declared synthetic business purpose",
                    "missing_receipt_declaration": "Synthetic missing original",
                }
            ],
        },
    )
    assert response.status_code == 201, response.text
    report = response.json()
    if submitted:
        response = await client.post(f"{BASE}/{report['id']}/submit", headers=_h(employee))
        assert response.status_code == 200, response.text
        report = response.json()
    return employee, report


async def financial_state(factory, org_id):
    names = (
        "expense_reports",
        "expense_items",
        "expense_approval_steps",
        "expense_transactions",
        "expense_commands",
        "audit_events",
        "webhook_deliveries",
        "jobs",
        "documents",
        "document_versions",
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
async def test_old_review_cannot_approve_changed_resubmission(auth_client, client, _db):
    employee, original = await report_fixture(auth_client, client)
    org_id = (await auth_client.get("/api/v1/auth/me")).json()["user"]["org_id"]
    report_id, item_id = original["id"], original["items"][0]["id"]
    withdrawn = await client.post(f"{BASE}/{report_id}/withdraw", headers=_h(employee))
    assert withdrawn.status_code == 200, withdrawn.text
    edited = await client.patch(
        f"{BASE}/{report_id}/items/{item_id}",
        headers=_h(employee),
        json={"amount": "84.00"},
    )
    assert edited.status_code == 200, edited.text
    submitted = await client.post(f"{BASE}/{report_id}/submit", headers=_h(employee))
    assert submitted.status_code == 200, submitted.text
    before = await financial_state(_db, org_id)
    stale = await auth_client.post(
        f"{BASE}/{report_id}/decision",
        json={"action": "approve", "version": original["version"]},
    )
    assert stale.status_code == 409, stale.text
    assert await financial_state(_db, org_id) == before
    assert submitted.json()["version"] > original["version"]
    assert submitted.json()["total"] == "84.00"
    reviewed = await auth_client.post(
        f"{BASE}/{report_id}/decision",
        json={"action": "approve", "version": submitted.json()["version"]},
    )
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["status"] == "approved"
    assert reviewed.json()["total"] == "84.00"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["submit", "withdraw", "decision", "reassign", "bin"])
@pytest.mark.parametrize("fault", ["missing", "raise"])
async def test_review_change_and_mandatory_audit_rollback_together(
    auth_client, client, _db, monkeypatch, operation, fault
):
    from app.api.routes import expenses as route

    employee, report = await report_fixture(
        auth_client, client, submitted=operation not in {"submit", "bin"}
    )
    owner = (await auth_client.get("/api/v1/auth/me")).json()["user"]
    before = await financial_state(_db, owner["org_id"])
    original = route.audit.record
    actions = {
        "submit": "expense.submit",
        "withdraw": "expense.transition",
        "decision": "expense.approve",
        "reassign": "expense.reassign_approver",
        "bin": "expense.report_binned",
    }

    async def refuse(db, action, **kwargs):
        if action == actions[operation]:
            if fault == "raise":
                raise RuntimeError("Synthetic report review audit failure")
            return None
        return await original(db, action, **kwargs)

    monkeypatch.setattr(route.audit, "record", refuse)

    async def request():
        path = f"{BASE}/{report['id']}"
        if operation == "bin":
            return await client.delete(path, headers=_h(employee))
        if operation == "decision":
            return await auth_client.post(
                f"{path}/decision", json={"action": "approve", "version": report["version"]}
            )
        if operation == "reassign":
            return await auth_client.post(f"{path}/reassign", json={"approver_id": owner["id"]})
        return await client.post(f"{path}/{operation}", headers=_h(employee))

    if fault == "raise":
        with pytest.raises(RuntimeError, match="Synthetic report review audit"):
            await request()
    else:
        response = await request()
        assert response.status_code == 503, response.text
        assert response.json()["code"] == "expense_review_unavailable"
    assert await financial_state(_db, owner["org_id"]) == before


@pytest.mark.asyncio
async def test_changed_pending_assignee_invalidates_original_review(auth_client, client, _db):
    _, report = await report_fixture(auth_client, client)
    owner = (await auth_client.get("/api/v1/auth/me")).json()["user"]
    reassigned = await auth_client.post(
        f"{BASE}/{report['id']}/reassign", json={"approver_id": owner["id"]}
    )
    assert reassigned.status_code == 200, reassigned.text
    assert reassigned.json()["version"] == report["version"] + 1
    before = await financial_state(_db, owner["org_id"])
    refused = await auth_client.post(
        f"{BASE}/{report['id']}/decision",
        json={"action": "approve", "version": report["version"]},
    )
    assert refused.status_code == 409, refused.text
    assert await financial_state(_db, owner["org_id"]) == before
