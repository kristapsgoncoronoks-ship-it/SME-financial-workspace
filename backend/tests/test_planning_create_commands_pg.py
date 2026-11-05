"""Real planner membership waits and exact keyed HTTP creation transactions."""

from __future__ import annotations

import asyncio
import os
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from app.models.calendar_connection import CalendarConnection
from app.models.membership import Membership
from app.models.planning_command import PlanningCommand
from app.models.user import User, UserRole
from app.services import planning_commands
from tests.test_planning_create_commands import BASE, _state
from tests.test_reimbursement_pay_concurrency import _ex20_observe_wait
from tests.test_schedule_delivery_pg import pg_schedule as _pg_schedule

pg_schedule = _pg_schedule
pytestmark = pytest.mark.skipif(
    not os.environ.get("RLS_TEST_DATABASE_URL"), reason="requires migrated non-superuser PostgreSQL"
)


def _body(initial):
    start = datetime.fromisoformat(initial["starts_at"]).astimezone(UTC) + timedelta(days=1)
    return {
        "project_id": initial["project_id"],
        "assignee_user_id": initial["assignee_user_id"],
        "starts_at": start.isoformat(),
        "ends_at": (start + timedelta(hours=2)).isoformat(),
        "notify_assignee": True,
        "reminders_enabled": True,
        "client_notice_enabled": False,
    }


async def _connection(factory, org_id, user_id):
    async with factory() as db:
        db.add(
            CalendarConnection(
                org_id=org_id,
                user_id=user_id,
                provider="google",
                status="connected",
                generation=1,
                calendar_id="synthetic-calendar",
                sealed_tokens="synthetic-unused-envelope",
            )
        )
        await db.commit()


async def _other_planner(client):
    invite = await client.post(
        "/api/v1/team/invites",
        json={"email": f"planning-other-{uuid4()}@example.com", "role": "admin"},
    )
    assert invite.status_code == 201, invite.text
    accepted = await client.post(
        "/api/v1/auth/accept-invite",
        json={
            "token": invite.json()["token"],
            "name": "Second planner",
            "password": "Synthetic-Planning-Password-32",
        },
    )
    assert accepted.status_code == 201, accepted.text
    return {"Authorization": f"Bearer {accepted.json()['token']['access_token']}"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "schedule", ["same_actor_same_key", "same_actor_new_key", "different_actor_same_key"]
)
@pytest.mark.parametrize("round_number", [1, 2])
async def test_planning_keyed_http_transactions_serialize_at_actual_memberships(
    pg_schedule, monkeypatch, record_property, schedule, round_number
):
    from time import perf_counter

    from app.core.config import settings

    client, factory, org_id, initial = pg_schedule
    body = _body(initial)
    await _connection(factory, org_id, initial["assignee_user_id"])
    # The fixture's SMTP trap belongs to the scheduling subject. Bootstrap
    # another identity with the same offline configuration as registration.
    with monkeypatch.context() as bootstrap:
        bootstrap.setattr(settings, "smtp_host", None)
        second_headers = (
            await _other_planner(client) if schedule == "different_actor_same_key" else {}
        )
    before = await _state(factory, org_id)
    first_key, second_key = str(uuid4()), str(uuid4())
    if schedule != "same_actor_new_key":
        second_key = first_key
    role = ContextVar("planning_create_request", default=None)
    locked, entered, second_locked = asyncio.Event(), asyncio.Event(), asyncio.Event()
    release_first, release_second = asyncio.Event(), asyncio.Event()
    original, pids, tasks = planning_commands.lock_memberships, {}, []

    async def memberships(db, current, assignee_id=None):
        name = role.get()
        if name == "second":
            pids[name] = await db.scalar(text("SELECT pg_backend_pid()"))
            entered.set()
        await original(db, current, assignee_id)
        if name == "first":
            pids[name] = await db.scalar(text("SELECT pg_backend_pid()"))
            locked.set()
            await release_first.wait()
        elif name == "second":
            second_locked.set()
            await release_second.wait()

    monkeypatch.setattr(planning_commands, "lock_memberships", memberships)

    async def request(name=None):
        token = role.set(name)
        try:
            key = first_key if name == "first" else second_key
            headers = {} if name == "first" else second_headers
            return await client.post(
                f"{BASE}/assignments", json=body, headers={**headers, "Idempotency-Key": key}
            )
        finally:
            role.reset(token)

    started = perf_counter()
    try:
        tasks.append(asyncio.create_task(request("first")))
        await asyncio.wait_for(locked.wait(), 10)
        tasks.append(asyncio.create_task(request("second")))
        await asyncio.wait_for(entered.wait(), 10)
        observations = await asyncio.wait_for(
            _ex20_observe_wait(factory, pids["first"], pids["second"], finished=tasks[1]), 10
        )
        release_first.set()
        first = await asyncio.wait_for(tasks[0], 15)
        assert first.status_code == 201, first.text
        await asyncio.wait_for(second_locked.wait(), 10)
        committed = await _state(factory, org_id)
        release_second.set()
        second = await asyncio.wait_for(tasks[1], 15)
        assert second.status_code == 201, second.text
        after = await _state(factory, org_id)
        new_commands = [
            r
            for r in after["planning_commands"]
            if r["id"] not in {x["id"] for x in before["planning_commands"]}
        ]
        increment = 1 if schedule == "same_actor_same_key" else 2
        if increment == 1:
            assert second.json() == first.json()
            assert after == committed
        else:
            assert second.json()["assignment"]["id"] != first.json()["assignment"]["id"]
            assert [r["id"] for r in second.json()["overlaps"]] == [
                first.json()["assignment"]["id"]
            ]
        assert len(new_commands) == increment
        assert len(after["project_assignments"]) == len(before["project_assignments"]) + increment
        assert len(after["email_messages"]) == len(before["email_messages"]) + increment
        assert (
            len(after["calendar_event_mappings"])
            == len(before["calendar_event_mappings"]) + increment
        )
        assert len(after["jobs"]) == len(before["jobs"]) + 3 * increment
        assert (
            sum(r["action"] == "assignment.create" for r in after["audit_events"])
            == sum(r["action"] == "assignment.create" for r in before["audit_events"]) + increment
        )
        replay = await request()
        assert replay.status_code == 201 and replay.json() == second.json(), replay.text
        assert await _state(factory, org_id) == after
        record_property("schedule", schedule)
        record_property("round", round_number)
        record_property("observed_real_membership_block", True)
        record_property("observer_queries", observations)
        record_property("elapsed_seconds", round(perf_counter() - started, 6))
    finally:
        release_first.set()
        release_second.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_reciprocal_planners_lock_memberships_in_global_order(
    pg_schedule, monkeypatch, record_property
):
    """A blocked high-id row proves both requests acquire the low-id row first."""
    from app.core.config import settings

    client, factory, org_id, initial = pg_schedule
    with monkeypatch.context() as bootstrap:
        bootstrap.setattr(settings, "smtp_host", None)
        other_headers = await _other_planner(client)
    other_id = (await client.get("/api/v1/auth/me", headers=other_headers)).json()["user"]["id"]
    owner_id = initial["assignee_user_id"]
    low_id, high_id = sorted((owner_id, other_id))
    headers = {owner_id: {}, other_id: other_headers}
    await _connection(factory, org_id, owner_id)
    await _connection(factory, org_id, other_id)
    body = _body(initial)
    requests = (
        ({**body, "assignee_user_id": low_id}, headers[high_id], str(uuid4())),
        ({**body, "assignee_user_id": high_id}, headers[low_id], str(uuid4())),
    )
    before = await _state(factory, org_id)
    role = ContextVar("reciprocal_planning_request", default=None)
    entered = {name: asyncio.Event() for name in ("first", "second")}
    original, pids, tasks = planning_commands.lock_memberships, {}, []

    async def memberships(db, current, assignee_id=None):
        name = role.get()
        if name is not None:
            pids[name] = await db.scalar(text("SELECT pg_backend_pid()"))
            entered[name].set()
        await original(db, current, assignee_id)

    monkeypatch.setattr(planning_commands, "lock_memberships", memberships)

    async def request(name, index):
        payload, actor_headers, key = requests[index]
        token = role.set(name)
        try:
            return await client.post(
                f"{BASE}/assignments",
                json=payload,
                headers={**actor_headers, "Idempotency-Key": key},
            )
        finally:
            role.reset(token)

    try:
        async with factory() as blocker:
            pids["blocker"] = await blocker.scalar(text("SELECT pg_backend_pid()"))
            await blocker.scalar(
                select(Membership)
                .where(Membership.org_id == org_id, Membership.user_id == high_id)
                .with_for_update()
            )
            tasks.append(asyncio.create_task(request("first", 0)))
            await asyncio.wait_for(entered["first"].wait(), 10)
            await asyncio.wait_for(
                _ex20_observe_wait(factory, pids["blocker"], pids["first"], finished=tasks[0]),
                10,
            )
            tasks.append(asyncio.create_task(request("second", 1)))
            await asyncio.wait_for(entered["second"].wait(), 10)
            # The first planner is blocked on high-id but holds low-id. A
            # reversed actor/assignee order would make this wait on blocker.
            observed = await asyncio.wait_for(
                _ex20_observe_wait(factory, pids["first"], pids["second"], finished=tasks[1]),
                10,
            )
            await blocker.commit()
        responses = await asyncio.wait_for(asyncio.gather(*tasks), 15)
        assert [response.status_code for response in responses] == [201, 201]
        assert all(response.json()["overlaps"] == [] for response in responses)
        after = await _state(factory, org_id)
        for table, increment in (
            ("planning_commands", 2),
            ("project_assignments", 2),
            ("email_messages", 2),
            ("jobs", 6),
            ("calendar_event_mappings", 2),
        ):
            assert len(after[table]) == len(before[table]) + increment
        assert (
            sum(row["action"] == "assignment.create" for row in after["audit_events"])
            == sum(row["action"] == "assignment.create" for row in before["audit_events"]) + 2
        )
        for index, expected in enumerate(responses):
            replay = await request(None, index)
            assert replay.status_code == 201 and replay.json() == expected.json()
        assert await _state(factory, org_id) == after
        record_property("observed_global_membership_lock_order", True)
        record_property("observer_queries", observed)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["planner_role", "assignee_status"])
async def test_planning_create_rechecks_authority_after_real_membership_wait(
    pg_schedule, monkeypatch, record_property, changed
):
    client, factory, org_id, initial = pg_schedule
    body = _body(initial)
    if changed == "assignee_status":
        async with factory() as db:
            user = User(
                org_id=org_id,
                email=f"planning-assignee-{uuid4()}@example.com",
                name="Synthetic assignee",
                hashed_password="fixture-only",
                role=UserRole.user,
            )
            db.add(user)
            await db.flush()
            db.add(
                Membership(
                    org_id=org_id,
                    user_id=user.id,
                    role=UserRole.user,
                    status="active",
                    email=user.email,
                    name=user.name,
                )
            )
            await db.commit()
            body["assignee_user_id"] = user.id
    before = await _state(factory, org_id)
    entered, pids, task = asyncio.Event(), {}, None
    original = planning_commands.lock_memberships

    async def memberships(db, current, assignee_id=None):
        pids["waiter"] = await db.scalar(text("SELECT pg_backend_pid()"))
        entered.set()
        return await original(db, current, assignee_id)

    monkeypatch.setattr(planning_commands, "lock_memberships", memberships)
    try:
        async with factory() as blocker:
            pids["holder"] = await blocker.scalar(text("SELECT pg_backend_pid()"))
            target = (
                initial["assignee_user_id"]
                if changed == "planner_role"
                else body["assignee_user_id"]
            )
            member = await blocker.scalar(
                select(Membership)
                .where(Membership.org_id == org_id, Membership.user_id == target)
                .with_for_update()
            )
            task = asyncio.create_task(
                client.post(
                    f"{BASE}/assignments", json=body, headers={"Idempotency-Key": str(uuid4())}
                )
            )
            await asyncio.wait_for(entered.wait(), 10)
            count = await asyncio.wait_for(
                _ex20_observe_wait(factory, pids["holder"], pids["waiter"], finished=task), 10
            )
            if changed == "planner_role":
                member.role = UserRole.user
            else:
                member.status = "suspended"
            await blocker.commit()
            response = await asyncio.wait_for(task, 15)
        assert response.status_code == (403 if changed == "planner_role" else 400), response.text
        assert await _state(factory, org_id) == before
        record_property("changed", changed)
        record_property("observed_real_membership_block", True)
        record_property("observer_queries", count)
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_planning_command_table_enforces_tenant_raw_query_isolation(pg_schedule):
    from sqlalchemy.exc import DBAPIError

    client, factory, org_id, initial = pg_schedule
    key = str(uuid4())
    assert (
        await client.post(
            f"{BASE}/assignments", json=_body(initial), headers={"Idempotency-Key": key}
        )
    ).status_code == 201
    async with factory() as db:
        await db.execute(
            text("SELECT set_config('app.current_org', :org, true)"), {"org": str(uuid4())}
        )
        assert (
            await db.scalar(
                text("SELECT count(*) FROM planning_commands WHERE org_id=:org"), {"org": org_id}
            )
            == 0
        )
        with pytest.raises(DBAPIError):
            await db.execute(
                text(
                    "INSERT INTO planning_commands (id,org_id,actor_user_id,command_key,payload_hash,payload_json,assignment_id,result_json) VALUES (:id,:org,:actor,:key,:hash,'{}',:target,'{}')"
                ),
                {
                    "id": str(uuid4()),
                    "org": org_id,
                    "actor": initial["assignee_user_id"],
                    "key": str(uuid4()),
                    "hash": "0" * 64,
                    "target": str(uuid4()),
                },
            )
        await db.rollback()
    async with factory() as db:
        row = await db.scalar(
            select(PlanningCommand).where(
                PlanningCommand.org_id == org_id, PlanningCommand.command_key == key
            )
        )
        assert row is not None
