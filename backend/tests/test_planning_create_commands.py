"""Durable planning creation through real HTTP; no provider/SMTP delivery runs."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models import Base
from app.models.project_assignment import ProjectAssignment
from tests.test_schedule_delivery import assignment_input

BASE = "/api/v1/schedule"


async def _state(factory, org_id):
    names = (
        "project_assignments",
        "planning_commands",
        "email_messages",
        "jobs",
        "audit_events",
        "calendar_event_mappings",
    )
    async with factory() as db:
        return {
            name: [
                dict(r)
                for r in (
                    await db.execute(
                        select(Base.metadata.tables[name])
                        .where(Base.metadata.tables[name].c.org_id == org_id)
                        .order_by(Base.metadata.tables[name].c.id)
                    )
                ).mappings()
            ]
            for name in names
            if name in Base.metadata.tables
        }


@pytest.mark.asyncio
async def test_keyed_create_returns_original_assignment_without_duplicate_intents(
    auth_client, db_session, _db
):
    body, key = await assignment_input(auth_client), str(uuid4())
    headers = {"Idempotency-Key": key}
    identity = (await auth_client.get("/api/v1/auth/me")).json()["user"]
    org_id = identity["org_id"]
    first = await auth_client.post(f"{BASE}/assignments", json=body, headers=headers)
    assert first.status_code == 201, first.text
    result = first.json()
    before = await _state(_db, org_id)
    replay = await auth_client.post(f"{BASE}/assignments", json=body, headers=headers)
    assert replay.status_code == 201, replay.text
    assert replay.json() == result
    assert await _state(_db, org_id) == before
    status = await auth_client.get(f"{BASE}/commands/{key}")
    assert status.status_code == 200, status.text
    assert status.json()["status"] == "committed"
    assert status.json()["operation"] == "assignment_created"
    assert status.json()["org_id"] == org_id
    assert status.json()["actor_user_id"] == identity["id"]
    assert status.json()["result"] == result
    assert status.json()["payload"]["time_zone"] == "UTC"
    assert len(before["planning_commands"]) == 1
    assert (
        json.loads(before["planning_commands"][0]["payload_json"])["starts_at"]
        == status.json()["payload"]["starts_at"]
    )
    assert len(before["project_assignments"]) == 1
    assert sum(r["action"] == "assignment.create" for r in before["audit_events"]) == 1
    current = await auth_client.get(f"{BASE}/assignments/{result['assignment']['id']}")
    assert current.status_code == 200
    assert current.json() == result["assignment"]
    changed = await auth_client.patch(
        f"{BASE}/assignments/{result['assignment']['id']}",
        json={"note": "Later edit", "expected_revision": 1},
    )
    assert changed.status_code == 200, changed.text
    edited = await _state(_db, org_id)
    assert (await auth_client.get(f"{BASE}/commands/{key}")).json()["result"] == result
    assert (
        await auth_client.post(f"{BASE}/assignments", json=body, headers=headers)
    ).json() == result
    assert await _state(_db, org_id) == edited
    assert (
        await db_session.scalar(
            select(ProjectAssignment.id).where(ProjectAssignment.org_id == org_id)
        )
        == result["assignment"]["id"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["note", "notify", "zone", "assignee", "window"])
async def test_same_key_changed_inputs_refused_without_writes(auth_client, _db, changed):
    body, key = await assignment_input(auth_client), str(uuid4())
    org_id = (await auth_client.get("/api/v1/auth/me")).json()["user"]["org_id"]
    headers = {"Idempotency-Key": key}
    assert (
        await auth_client.post(f"{BASE}/assignments", json=body, headers=headers)
    ).status_code == 201
    before = await _state(_db, org_id)
    patch = {
        "note": {"note": "Different note"},
        "notify": {"notify_assignee": False},
        "zone": {"time_zone": "Europe/Riga"},
        "assignee": {"assignee_user_id": str(uuid4())},
        "window": {"ends_at": "2035-01-01T00:00:00Z"},
    }[changed]
    refused = await auth_client.post(f"{BASE}/assignments", json={**body, **patch}, headers=headers)
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "planning_command_payload_conflict"
    assert await _state(_db, org_id) == before


@pytest.mark.asyncio
async def test_legacy_create_remains_fresh_intent_and_overlaps_are_advisory(auth_client, _db):
    body = await assignment_input(auth_client)
    org_id = (await auth_client.get("/api/v1/auth/me")).json()["user"]["org_id"]
    first = await auth_client.post(f"{BASE}/assignments", json=body)
    second = await auth_client.post(f"{BASE}/assignments", json=body)
    assert first.status_code == second.status_code == 201
    assert first.json()["assignment"]["id"] != second.json()["assignment"]["id"]
    assert [r["id"] for r in second.json()["overlaps"]] == [first.json()["assignment"]["id"]]
    assert (await _state(_db, org_id))["planning_commands"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_key", ["", "   ", "bad/key", "x" * 121])
async def test_invalid_key_never_creates_assignment_or_intent(auth_client, _db, bad_key):
    body = await assignment_input(auth_client)
    org_id = (await auth_client.get("/api/v1/auth/me")).json()["user"]["org_id"]
    before = await _state(_db, org_id)
    response = await auth_client.post(
        f"{BASE}/assignments", json=body, headers={"Idempotency-Key": bad_key}
    )
    assert response.status_code == 422, response.text
    assert await _state(_db, org_id) == before


@pytest.mark.asyncio
async def test_normalized_key_payload_and_defaults_replay_original_intent(auth_client, _db):
    from datetime import datetime, timedelta, timezone

    body, key = await assignment_input(auth_client), str(uuid4())
    org_id = (await auth_client.get("/api/v1/auth/me")).json()["user"]["org_id"]
    response = await auth_client.post(
        f"{BASE}/assignments", json=body, headers={"Idempotency-Key": f" {key} "}
    )
    assert response.status_code == 201, response.text
    before = await _state(_db, org_id)
    # UUID spellings, equivalent UTC instants and explicit fixed defaults share
    # identity. Workspace notification/reminder defaults are never resolved.
    normalized = {
        **body,
        "project_id": body["project_id"].upper(),
        "assignee_user_id": body["assignee_user_id"].upper(),
        "starts_at": datetime.fromisoformat(body["starts_at"])
        .astimezone(timezone(timedelta(hours=3)))
        .isoformat(),
        "all_day": False,
        "note": None,
        "time_zone": "UTC",
        "notify_assignee": True,
        "reminders_enabled": True,
        "remind_hours_before": None,
        "client_notice_hours_before": None,
        "client_notice_enabled": None,
    }
    replay = await auth_client.post(
        f"{BASE}/assignments", json=normalized, headers={"Idempotency-Key": key}
    )
    assert replay.status_code == 201 and replay.json() == response.json(), replay.text
    assert await _state(_db, org_id) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["audit_none", "audit_raise", "receipt", "notice", "calendar"])
async def test_create_receipt_audit_and_dispatch_failures_rollback_every_row(
    auth_client, db_session, _db, monkeypatch, failure
):
    from sqlalchemy import event

    from app.services import audit, calendar_sync, scheduling

    body = await assignment_input(auth_client)
    org_id = (await auth_client.get("/api/v1/auth/me")).json()["user"]["org_id"]
    before = await _state(_db, org_id)
    original = audit.record

    async def refuse_audit(db, action, *args, **kwargs):
        if action == "assignment.create":
            if failure == "audit_none":
                return None
            raise RuntimeError("Synthetic planning audit refusal")
        return await original(db, action, *args, **kwargs)

    async def refuse_intent(*args, **kwargs):
        raise RuntimeError("Synthetic planning dispatch refusal")

    def refuse_receipt(_conn, _cursor, statement, *_args):
        if statement.lstrip().lower().startswith("insert into planning_commands"):
            raise RuntimeError("Synthetic planning receipt refusal")

    engine = db_session.bind.sync_engine
    if failure.startswith("audit"):
        monkeypatch.setattr(audit, "record", refuse_audit)
    elif failure == "receipt":
        event.listen(engine, "before_cursor_execute", refuse_receipt)
    elif failure == "notice":
        monkeypatch.setattr(scheduling, "notify_assigned", refuse_intent)
    else:
        monkeypatch.setattr(calendar_sync, "enqueue_assignment_sync", refuse_intent)
    try:
        if failure == "audit_none":
            response = await auth_client.post(
                f"{BASE}/assignments", json=body, headers={"Idempotency-Key": str(uuid4())}
            )
            assert response.status_code == 503, response.text
            assert response.json()["code"] == "planning_command_unavailable"
        else:
            with pytest.raises(RuntimeError, match="Synthetic planning"):
                await auth_client.post(
                    f"{BASE}/assignments", json=body, headers={"Idempotency-Key": str(uuid4())}
                )
    finally:
        if failure == "receipt":
            event.remove(engine, "before_cursor_execute", refuse_receipt)
    assert await _state(_db, org_id) == before


@pytest.mark.asyncio
async def test_receipt_and_current_readback_guard_actor_tenant_and_revocation(
    auth_client, client, db_session, role_client, _db
):
    from app.models.membership import Membership
    from app.models.user import User, UserRole
    from tests.test_scheduling import _h, _invite

    body, key = await assignment_input(auth_client), str(uuid4())
    owner = (await auth_client.get("/api/v1/auth/me")).json()["user"]
    first = await auth_client.post(
        f"{BASE}/assignments", json=body, headers={"Idempotency-Key": key}
    )
    assert first.status_code == 201
    assignment_id = first.json()["assignment"]["id"]
    other, _ = await _invite(auth_client, client, "planning-other@example.com", "admin")
    assert (await client.get(f"{BASE}/commands/{key}", headers=_h(other))).status_code == 404
    other_org = await role_client("owner")
    assert (await other_org.get(f"{BASE}/commands/{key}")).status_code == 404
    assert (await other_org.get(f"{BASE}/assignments/{assignment_id}")).status_code == 404
    from app.core.tenant import reset_current_org, set_current_org

    token = set_current_org(owner["org_id"])
    try:
        user = await db_session.get(User, owner["id"])
        member = await db_session.scalar(
            select(Membership).where(
                Membership.org_id == owner["org_id"], Membership.user_id == owner["id"]
            )
        )
        user.role = member.role = UserRole.user
        await db_session.commit()
        before = await _state(_db, owner["org_id"])
        for path in (f"{BASE}/commands/{key}", f"{BASE}/assignments/{assignment_id}"):
            denied = await auth_client.get(path)
            assert denied.status_code == 403, denied.text
            assert first.json()["assignment"]["assignee_email"] not in denied.text
        assert (
            await auth_client.post(
                f"{BASE}/assignments", json=body, headers={"Idempotency-Key": key}
            )
        ).status_code == 403
        assert await _state(_db, owner["org_id"]) == before
        member.status = "suspended"
        await db_session.commit()
        assert (await auth_client.get(f"{BASE}/commands/{key}")).status_code == 401
    finally:
        reset_current_org(token)


@pytest.mark.asyncio
async def test_frozen_notice_reminder_and_calendar_intents_are_not_duplicated(
    auth_client, db_session, _db, monkeypatch
):
    from app.core.config import settings
    from app.models.calendar_connection import CalendarConnection
    from app.services import mailer

    body, key = await assignment_input(auth_client), str(uuid4())
    me = (await auth_client.get("/api/v1/auth/me")).json()["user"]
    # Synthetic connected state qualifies intent atomicity only. No provider,
    # token exchange, worker or SMTP delivery is exercised or claimed.
    db_session.add(
        CalendarConnection(
            org_id=me["org_id"],
            user_id=me["id"],
            provider="google",
            status="connected",
            generation=1,
            calendar_id="synthetic-calendar",
            sealed_tokens="synthetic-unused-token-envelope",
        )
    )
    await db_session.commit()
    monkeypatch.setattr(settings, "smtp_host", "smtp.synthetic.invalid")
    monkeypatch.setattr(mailer, "_smtp_send", lambda *_args: pytest.fail("SMTP before commit"))
    response = await auth_client.post(
        f"{BASE}/assignments", json=body, headers={"Idempotency-Key": key}
    )
    assert response.status_code == 201, response.text
    before = await _state(_db, me["org_id"])
    assert len(before["project_assignments"]) == len(before["planning_commands"]) == 1
    messages = [r for r in before["email_messages"] if r["kind"] == "assignment"]
    assert len(messages) == 1 and messages[0]["status"] == "queued"
    assert len(before["calendar_event_mappings"]) == 1
    kinds = [r["kind"] for r in before["jobs"]]
    assert kinds.count("schedule.email") == kinds.count("assignment.reminder") == 1
    assert len(kinds) == 3, kinds
    assert (
        await auth_client.post(f"{BASE}/assignments", json=body, headers={"Idempotency-Key": key})
    ).json() == response.json()
    assert await _state(_db, me["org_id"]) == before


@pytest.mark.asyncio
async def test_original_advisory_overlap_snapshot_and_runtime_defaults_stay_frozen(
    auth_client, _db, monkeypatch
):
    from app.services import scheduling

    body = await assignment_input(auth_client)
    legacy = await auth_client.post(f"{BASE}/assignments", json=body)
    assert legacy.status_code == 201, legacy.text
    key = str(uuid4())
    response = await auth_client.post(
        f"{BASE}/assignments", json=body, headers={"Idempotency-Key": key}
    )
    assert response.status_code == 201, response.text
    original = response.json()
    assert [r["id"] for r in original["overlaps"]] == [legacy.json()["assignment"]["id"]]
    changed = await auth_client.post(
        f"{BASE}/assignments/{legacy.json()['assignment']['id']}/transition",
        json={"status": "cancelled", "expected_revision": 1},
    )
    assert changed.status_code == 200, changed.text
    org_id = (await auth_client.get("/api/v1/auth/me")).json()["user"]["org_id"]
    before = await _state(_db, org_id)
    monkeypatch.setattr(scheduling, "DEFAULT_REMIND_HOURS", 48)
    assert (
        await auth_client.post(f"{BASE}/assignments", json=body, headers={"Idempotency-Key": key})
    ).json() == original
    assert (await auth_client.get(f"{BASE}/commands/{key}")).json()["result"] == original
    assert await _state(_db, org_id) == before
