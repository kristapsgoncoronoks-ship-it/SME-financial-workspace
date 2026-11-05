"""Real membership/report waits preserve designated expense-review authority."""

from __future__ import annotations

import asyncio
import os
from contextvars import ContextVar

import pytest
from sqlalchemy import select, text

from app.api.routes import expenses as route
from app.models.expense import ExpenseReport
from app.models.membership import Membership
from app.services import expense_commands, team
from tests.test_expense_review_cycle import BASE, financial_state, report_fixture
from tests.test_expense_review_cycle_pg import review_pg as _review_pg
from tests.test_reimbursement_pay_concurrency import _ex20_observe_wait

pytestmark = pytest.mark.skipif(
    not os.environ.get("RLS_TEST_DATABASE_URL"),
    reason="requires migrated non-superuser PostgreSQL",
)
review_authority_pg = _review_pg


async def _setup(client):
    _, report = await report_fixture(client, client)
    reviewer_id = (await client.get("/api/v1/auth/me")).json()["user"]["id"]
    # The actual team route must permit the later revocation: keep another
    # designated approver instead of bypassing its last-approver safeguard.
    second = await client.patch(
        f"/api/v1/team/members/{report['employee_id']}",
        json={"is_expense_approver": True},
    )
    assert second.status_code == 200, second.text
    return reviewer_id, report


async def _review(client, report, reviewer_id, action):
    if action == "reassign":
        return await client.post(
            f"{BASE}/{report['id']}/reassign", json={"approver_id": reviewer_id}
        )
    return await client.post(
        f"{BASE}/{report['id']}/decision",
        json={"action": "approve", "version": report["version"]},
    )


async def _revoked(factory, org_id, reviewer_id):
    async with factory() as db:
        return await db.scalar(
            select(Membership.is_expense_approver).where(
                Membership.org_id == org_id, Membership.user_id == reviewer_id
            )
        )


async def _finish(tasks):
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["decision", "reassign"])
async def test_committed_revocation_wins_review_authority_admission(
    review_authority_pg, monkeypatch, record_property, action
):
    client, factory, org_id = review_authority_pg
    reviewer_id, report = await _setup(client)
    who = ContextVar("expense_authority_revocation_first", default=None)
    revoker_locked, actor_entered, actor_loaded = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    report_entered = asyncio.Event()
    release_revoker, release_actor = asyncio.Event(), asyncio.Event()
    original_member, original_actor, original_report = (
        team.get_member,
        expense_commands.lock_actor,
        route._load,
    )
    pids, tasks = {}, []

    async def member(db, org, user_id, *, lock=False):
        row = await original_member(db, org, user_id, lock=lock)
        if who.get() == "revoke" and user_id == reviewer_id and lock:
            pids["revoke"] = await db.scalar(text("SELECT pg_backend_pid()"))
            revoker_locked.set()
            await release_revoker.wait()
        return row

    async def actor(db, current, **kwargs):
        if who.get() == "review":
            pids["review"] = await db.scalar(text("SELECT pg_backend_pid()"))
            actor_entered.set()
        authority = await original_actor(db, current, **kwargs)
        if who.get() == "review":
            actor_loaded.set()
            await release_actor.wait()
        return authority

    async def load(db, org, report_id, *, lock=False):
        if who.get() == "review" and report_id == report["id"]:
            report_entered.set()
        return await original_report(db, org, report_id, lock=lock)

    monkeypatch.setattr(team, "get_member", member)
    monkeypatch.setattr(expense_commands, "lock_actor", actor)
    monkeypatch.setattr(route, "_load", load)

    async def request(name):
        token = who.set(name)
        try:
            if name == "revoke":
                return await client.patch(
                    f"/api/v1/team/members/{reviewer_id}",
                    json={"is_expense_approver": False},
                )
            return await _review(client, report, reviewer_id, action)
        finally:
            who.reset(token)

    try:
        tasks.append(asyncio.create_task(request("revoke")))
        await asyncio.wait_for(revoker_locked.wait(), 10)
        tasks.append(asyncio.create_task(request("review")))
        actor_probe = asyncio.create_task(actor_entered.wait())
        report_probe = asyncio.create_task(report_entered.wait())
        tasks.extend([actor_probe, report_probe])
        done, _ = await asyncio.wait(
            [actor_probe, report_probe, tasks[1]], timeout=10, return_when=asyncio.FIRST_COMPLETED
        )
        assert actor_probe in done and not report_entered.is_set(), (
            "Review reached report mutation without first admitting live membership authority"
        )
        observed = await asyncio.wait_for(
            _ex20_observe_wait(factory, pids["revoke"], pids["review"], finished=tasks[1]),
            10,
        )
        release_revoker.set()
        revoked = await asyncio.wait_for(tasks[0], 15)
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["is_expense_approver"] is False
        await asyncio.wait_for(actor_loaded.wait(), 10)
        committed = await financial_state(factory, org_id)
        release_actor.set()
        refused = await asyncio.wait_for(tasks[1], 15)
        assert refused.status_code == 403, refused.text
        assert refused.json()["detail"] == (
            "Only a designated expense approver can decide on expenses"
            if action == "decision"
            else "Only an expense approver can reassign"
        )
        assert not report_entered.is_set()
        assert await financial_state(factory, org_id) == committed
        assert await _revoked(factory, org_id, reviewer_id) is False
        record_property("actual_membership_wait_observed", True)
        record_property("observer_queries", observed)
        record_property("winning_operation", "revocation")
    finally:
        release_revoker.set()
        release_actor.set()
        await _finish(tasks)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["decision", "reassign"])
async def test_valid_review_admission_protects_membership_until_commit(
    review_authority_pg, monkeypatch, record_property, action
):
    client, factory, org_id = review_authority_pg
    reviewer_id, report = await _setup(client)
    who = ContextVar("expense_authority_review_first", default=None)
    actor_locked, report_entered, revoker_entered, revoker_locked = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    release_revoker = asyncio.Event()
    original_member, original_actor, original_report = (
        team.get_member,
        expense_commands.lock_actor,
        route._load,
    )
    pids, tasks = {}, []

    async def member(db, org, user_id, *, lock=False):
        if who.get() == "revoke" and user_id == reviewer_id and lock:
            pids["revoke"] = await db.scalar(text("SELECT pg_backend_pid()"))
            revoker_entered.set()
        row = await original_member(db, org, user_id, lock=lock)
        if who.get() == "revoke" and user_id == reviewer_id and lock:
            revoker_locked.set()
            await release_revoker.wait()
        return row

    async def actor(db, current, **kwargs):
        authority = await original_actor(db, current, **kwargs)
        if who.get() == "review":
            pids["review"] = await db.scalar(text("SELECT pg_backend_pid()"))
            actor_locked.set()
        return authority

    async def load(db, org, report_id, *, lock=False):
        if who.get() == "review" and report_id == report["id"]:
            report_entered.set()
        return await original_report(db, org, report_id, lock=lock)

    monkeypatch.setattr(team, "get_member", member)
    monkeypatch.setattr(expense_commands, "lock_actor", actor)
    monkeypatch.setattr(route, "_load", load)

    async def request(name):
        token = who.set(name)
        try:
            if name == "revoke":
                return await client.patch(
                    f"/api/v1/team/members/{reviewer_id}",
                    json={"is_expense_approver": False},
                )
            return await _review(client, report, reviewer_id, action)
        finally:
            who.reset(token)

    try:
        async with factory() as holder:
            await holder.scalar(
                select(ExpenseReport)
                .where(ExpenseReport.org_id == org_id, ExpenseReport.id == report["id"])
                .with_for_update()
            )
            pids["holder"] = await holder.scalar(text("SELECT pg_backend_pid()"))
            tasks.append(asyncio.create_task(request("review")))
            await asyncio.wait_for(report_entered.wait(), 10)
            assert actor_locked.is_set(), (
                "Review locked the report before live membership authority"
            )
            report_wait = await asyncio.wait_for(
                _ex20_observe_wait(factory, pids["holder"], pids["review"], finished=tasks[0]),
                10,
            )
            tasks.append(asyncio.create_task(request("revoke")))
            await asyncio.wait_for(revoker_entered.wait(), 10)
            membership_wait = await asyncio.wait_for(
                _ex20_observe_wait(factory, pids["review"], pids["revoke"], finished=tasks[1]),
                10,
            )
            assert not revoker_locked.is_set()
            await holder.rollback()
        reviewed = await asyncio.wait_for(tasks[0], 15)
        assert reviewed.status_code == 200, reviewed.text
        assert reviewed.json()["version"] == report["version"] + 1
        assert reviewed.json()["status"] == ("approved" if action == "decision" else "submitted")
        await asyncio.wait_for(revoker_locked.wait(), 10)
        committed = await financial_state(factory, org_id)
        review_audit = "expense.approve" if action == "decision" else "expense.reassign_approver"
        assert sum(row["action"] == review_audit for row in committed["audit_events"]) == 1
        assert await _revoked(factory, org_id, reviewer_id) is True
        release_revoker.set()
        revoked = await asyncio.wait_for(tasks[1], 15)
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["is_expense_approver"] is False
        after_revocation = await financial_state(factory, org_id)
        assert {k: v for k, v in after_revocation.items() if k != "audit_events"} == {
            k: v for k, v in committed.items() if k != "audit_events"
        }
        denied = await _review(client, report, reviewer_id, action)
        assert denied.status_code == 403, denied.text
        assert await financial_state(factory, org_id) == after_revocation
        assert await _revoked(factory, org_id, reviewer_id) is False
        record_property("actual_report_wait_observed", True)
        record_property("actual_membership_wait_observed", True)
        record_property("report_observer_queries", report_wait)
        record_property("membership_observer_queries", membership_wait)
        record_property("winning_operation", "authorized_review")
    finally:
        release_revoker.set()
        await _finish(tasks)
