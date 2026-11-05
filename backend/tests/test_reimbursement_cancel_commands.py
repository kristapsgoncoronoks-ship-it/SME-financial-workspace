"""EX19 cancellation receipts through real HTTP and committed database rows.

Synthetic approved reports qualify cancellation, not expense approval or bank
settlement. Native locks are covered in test_reimbursement_pay_concurrency.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import event, select

from app.core.errors import AppError
from app.models.membership import Membership
from app.models.user import UserRole
from app.models.webhook import WebhookEndpoint
from app.services import audit
from tests.test_reimbursement_payment_commands import BASE, _activate, _key, _seed, _state


def _body(batch, reports, *, reason=None):
    return {
        "version": batch.version,
        "report_versions": {r.id: r.version for r in reports},
        "reason": reason,
    }


@pytest.mark.asyncio
async def test_cancel_original_key_keeps_exact_frozen_released_set_after_rebatch(
    auth_client, db_session, _db
):
    await _activate(auth_client)
    owner, batch, reports = await _seed(db_session)
    body, key = _body(batch, reports, reason="Duplicate selection"), str(uuid4())
    response = await auth_client.post(f"{BASE}/{batch.id}/cancel", json=body, headers=_key(key))
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["batch"]["status"] == "cancelled"
    assert result["batch"]["version"] == body["version"] + 1
    assert result["batch"]["reports"] == [] and result["batch"]["report_count"] == 0
    assert result["reason"] == body["reason"]
    assert {r["id"]: r["version"] for r in result["released_reports"]} == {
        r.id: body["report_versions"][r.id] + 1 for r in reports
    }
    assert {r["status"] for r in result["released_reports"]} == {
        "approved",
        "marked_for_reimbursement",
    }
    committed = await _state(_db, owner.org_id)
    assert len(committed["expense_commands"]) == 1
    assert not committed["jobs"] and not committed["webhook_deliveries"]
    assert all(r["payout_batch_id"] is None for r in committed["expense_reports"])
    actions = [r for r in committed["audit_events"] if r["action"] == audit.A.REIMBURSE_CANCEL]
    assert len(actions) == 1
    assert json.loads(actions[0]["meta"])["reason"] == body["reason"]
    for path in (f"{BASE}/commands/{key}", f"/api/v1/expenses/commands/{key}"):
        lookup = await auth_client.get(path)
        assert lookup.status_code == 200, lookup.text
        assert lookup.json()["operation"] == "reimbursement_cancelled"
        assert lookup.json()["result"] == result
    assert (await auth_client.get(f"/api/v1/expenses/commands/{key}/result")).json() == result
    assert await _state(_db, owner.org_id) == committed
    new = await auth_client.post(
        BASE,
        json={
            "report_ids": [r.id for r in reports],
            "report_versions": {r["id"]: r["version"] for r in result["released_reports"]},
        },
        headers=_key(),
    )
    assert new.status_code == 201, new.text
    rebatched = await _state(_db, owner.org_id)
    for method, path in (("POST", f"{BASE}/{batch.id}/cancel"), ("DELETE", f"{BASE}/{batch.id}")):
        replay = await auth_client.request(method, path, json=body, headers=_key(key))
        assert replay.status_code == (200 if method == "POST" else 204), replay.text
        if method == "POST":
            assert replay.json() == result
    assert (await auth_client.get(f"{BASE}/commands/{key}")).json()["result"] == result
    assert await _state(_db, owner.org_id) == rebatched
    changed = await auth_client.post(
        f"{BASE}/{batch.id}/cancel", json={**body, "reason": "Changed reason"}, headers=_key(key)
    )
    assert changed.status_code == 409
    assert changed.json()["code"] == "expense_command_payload_conflict"
    assert await _state(_db, owner.org_id) == rebatched


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", [None, "", "   ", "  Selection changed  "])
async def test_cancel_reason_optional_canonical(auth_client, db_session, reason):
    await _activate(auth_client)
    # Cancellation retains APPROVE policy: the creator may cancel an open
    # unexported batch; payment's maker-checker rule is a different action.
    _, batch, reports = await _seed(db_session, maker_is_owner=True)
    response = await auth_client.post(
        f"{BASE}/{batch.id}/cancel", json=_body(batch, reports, reason=reason), headers=_key()
    )
    assert response.status_code == 200, response.text
    assert response.json()["reason"] == (reason.strip() or None if reason else None)


@pytest.mark.asyncio
async def test_cancel_has_no_external_event_with_a_wildcard_subscriber(
    auth_client, db_session, _db
):
    await _activate(auth_client)
    owner, batch, reports = await _seed(db_session)
    endpoint = await db_session.scalar(
        select(WebhookEndpoint).where(WebhookEndpoint.org_id == owner.org_id)
    )
    endpoint.events = "*"
    await db_session.commit()
    response = await auth_client.post(
        f"{BASE}/{batch.id}/cancel", json=_body(batch, reports), headers=_key()
    )
    assert response.status_code == 200, response.text
    state = await _state(_db, owner.org_id)
    assert not state["webhook_deliveries"] and not state["jobs"]
    assert len(state["expense_commands"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed",
    [
        "batch_version",
        "report_version",
        "missing_report",
        "extra_report",
        "empty_reports",
        "paid",
        "timestamp",
        "count",
        "negative_count",
        "message",
        "empty_message",
        "paid_at",
        "terminal_report",
        "report_reference",
        "report_method",
        "report_paid_at",
        "binned_report",
    ],
)
async def test_cancel_refuses_stale_or_unsafe_disposition_without_writes(
    auth_client, db_session, _db, changed
):
    await _activate(auth_client)
    owner, batch, reports = await _seed(db_session)
    body = _body(batch, reports)
    if changed == "batch_version":
        body["version"] -= 1
    elif changed == "report_version":
        body["report_versions"][reports[0].id] -= 1
    elif changed == "missing_report":
        reports[0].payout_batch_id = None
    elif changed == "extra_report":
        body["report_versions"][str(uuid4())] = 1
    elif changed == "empty_reports":
        body["report_versions"] = {}
    elif changed == "paid":
        batch.status = "paid"
    elif changed == "timestamp":
        batch.exported_at = datetime.now(UTC)
    elif changed in ("count", "negative_count"):
        batch.export_count = 1 if changed == "count" else -1
    elif changed in ("message", "empty_message"):
        batch.last_msg_id = "EX19-SYNTHETIC" if changed == "message" else ""
    elif changed == "paid_at":
        batch.paid_at = datetime.now(UTC)
    elif changed == "terminal_report":
        reports[0].status = "reimbursed"
    elif changed == "report_reference":
        reports[0].payment_reference = "Prior payment"
    elif changed == "report_method":
        reports[0].payment_method = "cash"
    elif changed == "binned_report":
        reports[0].deleted_at = datetime.now(UTC)
    else:
        reports[0].reimbursed_at = datetime.now(UTC)
    await db_session.commit()
    before = await _state(_db, owner.org_id)
    response = await auth_client.post(f"{BASE}/{batch.id}/cancel", json=body, headers=_key())
    assert response.status_code == 409, response.text
    assert await _state(_db, owner.org_id) == before


@pytest.mark.asyncio
async def test_cancel_legacy_adapter_does_not_leave_an_unseen_binned_report_linked(
    auth_client, db_session, _db
):
    await _activate(auth_client)
    owner, batch, reports = await _seed(db_session)
    reports[0].deleted_at = datetime.now(UTC)
    await db_session.commit()
    before = await _state(_db, owner.org_id)
    refused = await auth_client.delete(f"{BASE}/{batch.id}")
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "reimbursement_selection_conflict"
    assert await _state(_db, owner.org_id) == before


@pytest.mark.asyncio
async def test_cancel_exact_empty_set_preserves_existing_empty_batch_cancellation(
    auth_client, db_session
):
    await _activate(auth_client)
    _, batch, reports = await _seed(db_session)
    for report in reports:
        report.payout_batch_id = None
    await db_session.commit()
    response = await auth_client.post(
        f"{BASE}/{batch.id}/cancel",
        json={"version": batch.version, "report_versions": {}, "reason": None},
        headers=_key(),
    )
    assert response.status_code == 200, response.text
    assert response.json()["released_reports"] == []
    assert response.json()["batch"]["status"] == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["audit_none", "audit_raise", "receipt"])
async def test_cancel_strict_audit_receipt_failure_rolls_back_every_row(
    auth_client, db_session, _db, monkeypatch, failure
):
    await _activate(auth_client)
    owner, batch, reports = await _seed(db_session)
    before, engine = await _state(_db, owner.org_id), db_session.bind.sync_engine

    async def failed_audit(*args, **kwargs):
        if failure == "audit_none":
            return None
        raise AppError("Synthetic audit refusal", status=503, code="expense_audit_unavailable")

    def no_receipt(_conn, _cursor, statement, *_rest):
        if statement.lstrip().lower().startswith("insert into expense_commands"):
            raise RuntimeError("synthetic cancellation receipt refusal")

    if failure == "receipt":
        event.listen(engine, "before_cursor_execute", no_receipt)
    else:
        monkeypatch.setattr(audit, "record", failed_audit)
    try:
        response = await auth_client.post(
            f"{BASE}/{batch.id}/cancel", json=_body(batch, reports), headers=_key()
        )
        # audit.record catches its flush failure; the strict receipt helper
        # turns that None into a 503 and the outer transaction rolls back.
        assert response.status_code == 503, response.text
    finally:
        if failure == "receipt":
            event.remove(engine, "before_cursor_execute", no_receipt)
    assert await _state(_db, owner.org_id) == before


@pytest.mark.asyncio
async def test_cancel_legacy_adapter_records_unique_receipt_without_implicit_replay(
    auth_client, db_session, _db
):
    await _activate(auth_client)
    owner, batch, _ = await _seed(db_session)
    response = await auth_client.delete(f"{BASE}/{batch.id}")
    assert response.status_code == 204, response.text
    before = await _state(_db, owner.org_id)
    assert len(before["expense_commands"]) == 1
    assert before["expense_commands"][0]["operation"] == "reimbursement_cancelled"
    assert sorted(r["version"] for r in before["expense_reports"]) == [8, 9]
    assert (await auth_client.delete(f"{BASE}/{batch.id}")).status_code == 409
    assert await _state(_db, owner.org_id) == before


@pytest.mark.asyncio
async def test_cancel_demoted_actor_cannot_replay_or_read_any_alias(auth_client, db_session, _db):
    await _activate(auth_client)
    owner, batch, reports = await _seed(db_session)
    body, key = _body(batch, reports), str(uuid4())
    assert (
        await auth_client.post(f"{BASE}/{batch.id}/cancel", json=body, headers=_key(key))
    ).status_code == 200
    member = await db_session.scalar(
        select(Membership).where(Membership.org_id == owner.org_id, Membership.user_id == owner.id)
    )
    member.role = owner.role = UserRole.user
    member.is_expense_approver = owner.is_expense_approver = False
    await db_session.commit()
    before = await _state(_db, owner.org_id)
    for path in (
        f"{BASE}/commands/{key}",
        f"/api/v1/expenses/commands/{key}",
        f"/api/v1/expenses/commands/{key}/result",
    ):
        denied = await auth_client.get(path)
        assert denied.status_code == 403, denied.text
        assert reports[0].title not in denied.text
    assert (
        await auth_client.post(f"{BASE}/{batch.id}/cancel", json=body, headers=_key(key))
    ).status_code == 403
    assert await _state(_db, owner.org_id) == before


@pytest.mark.asyncio
async def test_cancel_requires_reviewed_versions_and_valid_payload(auth_client, db_session, _db):
    await _activate(auth_client)
    owner, batch, reports = await _seed(db_session)
    before = await _state(_db, owner.org_id)
    for body in (
        {},
        {"version": True, "report_versions": {}},
        {**_body(batch, reports), "reason": "x" * 1001},
        {**_body(batch, reports), "report_versions": {"invalid": 1}},
    ):
        result = await auth_client.post(f"{BASE}/{batch.id}/cancel", json=body, headers=_key())
        assert result.status_code == 422, result.text
    keyed_implicit = await auth_client.delete(f"{BASE}/{batch.id}", headers=_key())
    assert keyed_implicit.status_code == 422, keyed_implicit.text
    assert await _state(_db, owner.org_id) == before


@pytest.mark.asyncio
async def test_cancel_receipt_tenant_actor_module_and_offboarding_guards(
    auth_client, client, db_session, role_client, _db
):
    await _activate(auth_client)
    owner, batch, reports = await _seed(db_session)
    body, key = _body(batch, reports), str(uuid4())
    assert (
        await auth_client.post(f"{BASE}/{batch.id}/cancel", json=body, headers=_key(key))
    ).status_code == 200
    assert (await auth_client.get(f"{BASE}/commands/{uuid4()}")).status_code == 404
    invitation = await auth_client.post(
        "/api/v1/team/invites", json={"email": "ex19-other@example.com", "role": "admin"}
    )
    assert invitation.status_code == 201, invitation.text
    accepted = await client.post(
        "/api/v1/auth/accept-invite",
        json={
            "token": invitation.json()["token"],
            "name": "Other checker",
            "password": "supersecret",
        },
    )
    assert accepted.status_code == 201, accepted.text
    other_headers = {"Authorization": f"Bearer {accepted.json()['token']['access_token']}"}
    assert (await client.get(f"{BASE}/commands/{key}", headers=other_headers)).status_code == 404
    other_tenant = await role_client("owner")
    await _activate(other_tenant)
    assert (await other_tenant.get(f"{BASE}/commands/{key}")).status_code == 404
    assert (
        await other_tenant.post(f"{BASE}/{batch.id}/cancel", json=body, headers=_key(key))
    ).status_code == 404
    # Restore the explicit tenant context after the helper created a second org.
    from app.core.tenant import reset_current_org, set_current_org

    token = set_current_org(owner.org_id)
    try:
        disabled = await auth_client.put("/api/v1/modules/expenses", json={"enabled": False})
        assert disabled.status_code == 200, disabled.text
        before = await _state(_db, owner.org_id)
        for path in (
            f"{BASE}/commands/{key}",
            f"/api/v1/expenses/commands/{key}",
            f"/api/v1/expenses/commands/{key}/result",
        ):
            assert (await auth_client.get(path)).status_code == 403
        assert (
            await auth_client.post(f"{BASE}/{batch.id}/cancel", json=body, headers=_key(key))
        ).status_code == 403
        assert await _state(_db, owner.org_id) == before
        await _activate(auth_client)
        member = await db_session.scalar(
            select(Membership).where(
                Membership.org_id == owner.org_id, Membership.user_id == owner.id
            )
        )
        member.status = "suspended"
        await db_session.commit()
        assert (await auth_client.get(f"{BASE}/commands/{key}")).status_code == 401
    finally:
        reset_current_org(token)


@pytest.mark.asyncio
async def test_cancel_key_is_shared_with_other_expense_operations(auth_client, db_session, _db):
    await _activate(auth_client)
    owner, batch, reports = await _seed(db_session)
    body, key = _body(batch, reports), str(uuid4())
    assert (
        await auth_client.post(f"{BASE}/{batch.id}/cancel", json=body, headers=_key(key))
    ).status_code == 200
    before = await _state(_db, owner.org_id)
    changed = await auth_client.post(
        BASE,
        json={
            "report_ids": [r.id for r in reports],
            "report_versions": {r.id: body["report_versions"][r.id] + 1 for r in reports},
        },
        headers=_key(key),
    )
    assert changed.status_code == 409, changed.text
    assert changed.json()["code"] == "expense_command_payload_conflict"
    assert await _state(_db, owner.org_id) == before
