"""Fresh authority admission preserves the existing stricter expense flag."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import update

from app.models.user import User
from tests.test_expense_decision_version import _h
from tests.test_expense_review_authority_pg import _review
from tests.test_expense_review_cycle import financial_state, report_fixture


async def _reviewer(auth_client, client, role):
    invited = await auth_client.post(
        "/api/v1/team/invites",
        json={"email": f"review-authority-{uuid4()}@example.com", "role": role},
    )
    assert invited.status_code == 201, invited.text
    accepted = await client.post(
        "/api/v1/auth/accept-invite",
        json={
            "token": invited.json()["token"],
            "name": "Synthetic reviewer",
            "password": "Synthetic-Review-Authority-Password-32",
        },
    )
    assert accepted.status_code == 201, accepted.text
    return accepted.json()["user"]["id"], _h(accepted.json()["token"]["access_token"])


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["decision", "reassign"])
@pytest.mark.parametrize("role", ["owner", "admin", "finance_manager", "platform"])
async def test_generic_approval_permission_cannot_replace_designated_flag(
    auth_client, client, db_session, _db, action, role
):
    _, report = await report_fixture(auth_client, client)
    owner = (await auth_client.get("/api/v1/auth/me")).json()["user"]
    if role == "owner":
        second = await auth_client.patch(
            f"/api/v1/team/members/{report['employee_id']}",
            json={"is_expense_approver": True},
        )
        assert second.status_code == 200, second.text
        removed = await auth_client.patch(
            f"/api/v1/team/members/{owner['id']}", json={"is_expense_approver": False}
        )
        assert removed.status_code == 200, removed.text
        reviewer_id, headers = owner["id"], dict(auth_client.headers)
    else:
        reviewer_id, headers = await _reviewer(
            auth_client, client, "user" if role == "platform" else role
        )
        if role == "platform":
            await db_session.execute(
                update(User).where(User.id == reviewer_id).values(is_platform_admin=True)
            )
            await db_session.commit()
    identity = await client.get("/api/v1/auth/me", headers=headers)
    assert identity.status_code == 200, identity.text
    assert "expense.approve" in identity.json()["permissions"]
    assert identity.json()["user"]["is_expense_approver"] is False
    before = await financial_state(_db, owner["org_id"])
    # The generic permission is demonstrably granted; only the established
    # designated-approver rule should refuse either financial review action.
    if action == "decision":
        refused = await client.post(
            f"/api/v1/expenses/{report['id']}/decision",
            headers=headers,
            json={"action": "approve", "version": report["version"]},
        )
    else:
        refused = await client.post(
            f"/api/v1/expenses/{report['id']}/reassign",
            headers=headers,
            json={"approver_id": reviewer_id},
        )
    assert refused.status_code == 403, refused.text
    assert refused.json()["detail"] == (
        "Only a designated expense approver can decide on expenses"
        if action == "decision"
        else "Only an expense approver can reassign"
    )
    assert await financial_state(_db, owner["org_id"]) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["decision", "reassign"])
async def test_designated_employee_keeps_existing_approval_permission_bridge(
    auth_client, client, action
):
    _, report = await report_fixture(auth_client, client)
    reviewer_id, headers = await _reviewer(auth_client, client, "user")
    initial = await client.get("/api/v1/auth/me", headers=headers)
    assert "expense.approve" not in initial.json()["permissions"]
    appointed = await auth_client.patch(
        f"/api/v1/team/members/{reviewer_id}", json={"is_expense_approver": True}
    )
    assert appointed.status_code == 200, appointed.text
    identity = await client.get("/api/v1/auth/me", headers=headers)
    assert identity.json()["user"]["role"] == "user"
    assert identity.json()["user"]["is_expense_approver"] is True
    assert "expense.approve" in identity.json()["permissions"]
    # Use a separate client carrying this ordinary employee's token so the
    # review helper exercises its existing request without any identity override.
    previous = dict(client.headers)
    client.headers.update(headers)
    try:
        reviewed = await _review(client, report, reviewer_id, action)
    finally:
        client.headers.clear()
        client.headers.update(previous)
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["version"] == report["version"] + 1
    assert reviewed.json()["status"] == ("approved" if action == "decision" else "submitted")
