"""Actual HTTP bank commands retain pinned authority across native row waits."""

from __future__ import annotations

import asyncio
import json
import os
from contextvars import ContextVar
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic.script import ScriptDirectory
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core import tenant
from app.core.config import settings
from app.core.database import get_session
from app.main import app
from app.models.audit import AuditEvent
from app.models.bank_import import BankLine, BankStatement
from app.models.organization import Organization
from app.models.receipt import Receipt
from app.models.reconciliation_command import ReconciliationCommand
from app.models.user import User
from app.services import reconciliation, team
from app.services import reconciliation_commands as commands
from tests.reconciliation_command_client import intention, submit

PG_URL = os.environ.get("RLS_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not PG_URL, reason="requires owned migrated non-superuser PostgreSQL"
)
BASE = "/api/v1/reconciliation/lines"
WHO = ContextVar("reconciliation_native_request", default=None)
PASSWORD = "Synthetic-Native-Reconciliation-Password-38"
CASH_TABLES = (
    "bank_statements",
    "receipts",
    "payments",
    "reimbursement_batches",
    "payment_runs",
    "supplier_payments",
    "issued_invoices",
    "expense_reports",
    "expense_items",
    "expense_transactions",
    "jobs",
)


def _headers(identity):
    return {
        "Authorization": "Bearer " + identity["token"]["access_token"],
        "X-Expected-Org-ID": identity["organization"]["id"],
        "X-Expected-User-ID": identity["user"]["id"],
    }


@pytest_asyncio.fixture
async def native_commands(monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", None)
    engine = create_async_engine(PG_URL, pool_size=8, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    previous = dict(app.dependency_overrides)
    owned_orgs, owned_users = [], []

    async def session():
        async with factory() as db:
            yield db

    app.dependency_overrides[get_session] = session
    try:
        async with factory() as db:
            assert (
                await db.execute(
                    text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user")
                )
            ).one() == (False, False)
            assert (
                await db.scalar(text("SELECT version_num FROM alembic_version"))
                == ScriptDirectory(
                    str(Path(__file__).resolve().parents[1] / "alembic")
                ).get_current_head()
            )
            assert (
                await db.execute(
                    text(
                        "SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid='reconciliation_commands'::regclass"
                    )
                )
            ).one() == (True, True)
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
        ) as client:

            async def register(label):
                response = await client.post(
                    "/api/v1/auth/register",
                    json={
                        "organization_name": "Synthetic command " + label,
                        "name": "Synthetic " + label,
                        "email": uuid4().hex + "@example.com",
                        "password": PASSWORD,
                    },
                )
                assert response.status_code == 201, response.text
                value = response.json()
                owned_orgs.append(value["organization"]["id"])
                owned_users.append(value["user"]["id"])
                return value

            control = await register("control")
            other = await register("global-writer")
            org, other_org = control["organization"]["id"], other["organization"]["id"]
            control_headers = _headers(control)
            response = await client.put(
                "/api/v1/modules/issuing", headers=control_headers, json={"enabled": True}
            )
            assert response.status_code == 200, response.text
            writers = []
            for label, email in (
                ("local-writer", uuid4().hex + "@example.com"),
                ("global-writer", other["user"]["email"]),
            ):
                invited = await client.post(
                    "/api/v1/team/invites",
                    headers=control_headers,
                    json={"email": email, "role": "accountant"},
                )
                assert invited.status_code == 201, invited.text
                accepted = await client.post(
                    "/api/v1/auth/accept-invite",
                    json={
                        "token": invited.json()["token"],
                        "name": "Synthetic " + label,
                        "password": PASSWORD,
                    },
                )
                assert accepted.status_code == 201, accepted.text
                identity = accepted.json()
                owned_users.append(identity["user"]["id"])
                writers.append({"id": identity["user"]["id"], "headers": _headers(identity)})
            assert writers[0]["id"] != writers[1]["id"]
            statement = str(uuid4())
            lines, receipts = [str(uuid4()) for _ in range(2)], [str(uuid4()) for _ in range(2)]
            token = tenant.set_current_org(org)
            try:
                async with factory.begin() as db:
                    db.add(
                        BankStatement(
                            id=statement,
                            org_id=org,
                            filename="retained-native.csv",
                            sha256=uuid4().hex * 2,
                            line_count=2,
                        )
                    )
                    await db.flush()
                    for position in range(2):
                        db.add(
                            BankLine(
                                id=lines[position],
                                org_id=org,
                                statement_id=statement,
                                line_date=date(2026, 11, 6),
                                description="Retained bank source " + str(position),
                                amount=Decimal("100.00"),
                                direction="credit",
                                currency="EUR",
                                balance=Decimal("200.00"),
                            )
                        )
                        db.add(
                            Receipt(
                                id=receipts[position],
                                org_id=org,
                                amount=Decimal("100.00"),
                                received_on=date(2026, 11, 6),
                                reference="Retained cash " + str(position),
                            )
                        )
            finally:
                tenant.reset_current_org(token)
            yield {
                "client": client,
                "factory": factory,
                "engine": engine,
                "org": org,
                "other_org": other_org,
                "control": control_headers,
                "writers": writers,
                "statement": statement,
                "lines": lines,
                "receipts": receipts,
            }
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
        token = tenant.set_current_org(None)
        try:
            async with factory.begin() as db:
                await db.execute(text("SELECT set_config('app.current_org','',true)"))
                # Only this fixture's synthetic trail is removed; no retention
                # or foreign-key policy is relaxed for application requests.
                if owned_orgs:
                    await db.execute(
                        delete(ReconciliationCommand).where(
                            ReconciliationCommand.org_id.in_(owned_orgs)
                        )
                    )
                    await db.execute(delete(AuditEvent).where(AuditEvent.org_id.in_(owned_orgs)))
                    await db.execute(delete(Organization).where(Organization.id.in_(owned_orgs)))
                if owned_users:
                    await db.execute(delete(User).where(User.id.in_(owned_users)))
                assert not list(
                    (
                        await db.execute(
                            select(Organization.id).where(Organization.id.in_(owned_orgs))
                        )
                    ).scalars()
                )
                assert not list(
                    (await db.execute(select(User.id).where(User.id.in_(owned_users)))).scalars()
                )
        finally:
            tenant.reset_current_org(token)
            await engine.dispose()


async def _facts(fixture):
    async with fixture["factory"]() as db:
        await db.execute(text("SELECT set_config('app.current_org','',true)"))
        facts = {
            table: await db.scalar(
                text(
                    f"SELECT COALESCE(jsonb_agg(to_jsonb(t) ORDER BY id),'[]'::jsonb)::text FROM {table} t WHERE org_id=:org"
                ),
                {"org": fixture["org"]},
            )
            for table in CASH_TABLES
        }
        facts["bank_line_source"] = await db.scalar(
            text(
                "SELECT COALESCE(jsonb_agg((to_jsonb(t) - ARRAY['status','matched_kind',"
                "'matched_id','version','updated_at']) ORDER BY id),'[]'::jsonb)::text "
                "FROM bank_lines t WHERE org_id=:org"
            ),
            {"org": fixture["org"]},
        )
        return facts


async def _domain(fixture):
    async with fixture["factory"]() as db:
        await db.execute(text("SELECT set_config('app.current_org','',true)"))
        result = {}
        for key, query in {
            "lines": "SELECT id::text,version,status,matched_kind,matched_id::text FROM bank_lines WHERE org_id=:org ORDER BY id",
            "commands": "SELECT id::text,command_key,actor_user_id::text,line_id::text,before_version,after_version,reason,result_hash FROM reconciliation_commands WHERE org_id=:org ORDER BY id",
            "audit": "SELECT id::text,actor_id::text,target_id,meta FROM audit_events WHERE org_id=:org AND action='reconciliation.command_committed' ORDER BY id",
        }.items():
            result[key] = [
                dict(row)
                for row in (await db.execute(text(query), {"org": fixture["org"]})).mappings()
            ]
        return result


async def _review(fixture, line, writer, operation, *, target=None, reason=None):
    response = await fixture["client"].get(BASE + "/" + line, headers=writer["headers"])
    assert response.status_code == 200, response.text
    return intention(response.json(), operation, target=target, reason=reason)


async def _send(fixture, writer, line, body, key):
    return await submit(fixture["client"], line, body, key, headers=writer["headers"])


async def _labeled(label, operation):
    token = WHO.set(label)
    try:
        return await operation()
    finally:
        WHO.reset(token)


async def _blocked(engine, blocked, allowed, relation, task):
    async with engine.connect() as observer:
        async with asyncio.timeout(5):
            while True:
                await observer.execute(text("SELECT pg_stat_clear_snapshot()"))
                row = (
                    (
                        await observer.execute(
                            text(
                                "SELECT pg_blocking_pids(pid) AS blockers,query,wait_event_type FROM pg_stat_activity WHERE pid=:pid"
                            ),
                            {"pid": blocked},
                        )
                    )
                    .mappings()
                    .one()
                )
                if set(row["blockers"]) & set(allowed):
                    assert row["wait_event_type"] == "Lock" and relation in row["query"]
                    assert not task.done()
                    return dict(row)
                assert not task.done(), "request completed before the expected native wait"
                await asyncio.sleep(0.02)


async def _finish(tasks, *releases):
    for release in releases:
        release.set()
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def test_global_user_default_other_company_commits_under_current_membership(native_commands):
    f = native_commands
    before = await _facts(f)
    writer = f["writers"][1]
    async with f["factory"]() as db:
        assert await db.scalar(select(User.org_id).where(User.id == writer["id"])) == f["other_org"]
    body = await _review(f, f["lines"][0], writer, "ignore", reason="Reviewed imported duplicate")
    response = await _send(f, writer, f["lines"][0], body, "global-identity")
    assert response.status_code == 200, response.text
    assert (
        response.json()["actor_user_id"] == writer["id"] and response.json()["org_id"] == f["org"]
    )
    domain = await _domain(f)
    assert len(domain["commands"]) == len(domain["audit"]) == 1
    assert await _facts(f) == before


@pytest.mark.parametrize("wait_on", ["line", "target"])
@pytest.mark.parametrize("credential", ["switch", "logout"])
async def test_actual_credential_change_while_native_waiting_refuses_before_domain_write(
    native_commands, monkeypatch, record_testsuite_property, wait_on, credential
):
    f = native_commands
    writer = f["writers"][1]
    line, receipt = f["lines"][0], f["receipts"][0]
    body = await _review(f, line, writer, "match", target={"kind": "receipt", "id": receipt})
    before, cash = await _domain(f), await _facts(f)
    entered = asyncio.Event()
    pids = {}
    original = commands._line if wait_on == "line" else reconciliation.validate_match
    if wait_on == "line":

        async def observe(db, current, line_id, **kwargs):
            if WHO.get() == "command" and kwargs.get("lock"):
                pids["command"] = await db.scalar(text("SELECT pg_backend_pid()"))
                entered.set()
            return await original(db, current, line_id, **kwargs)

        monkeypatch.setattr(commands, "_line", observe)
    else:

        async def observe(db, *args, **kwargs):
            if WHO.get() == "command":
                pids["command"] = await db.scalar(text("SELECT pg_backend_pid()"))
                entered.set()
            return await original(db, *args, **kwargs)

        monkeypatch.setattr(reconciliation, "validate_match", observe)
    tasks = []
    async with f["factory"]() as holder:
        transaction = await holder.begin()
        table = "bank_lines" if wait_on == "line" else "receipts"
        target = line if wait_on == "line" else receipt
        await holder.execute(
            text(f"SELECT id FROM {table} WHERE id=:id FOR UPDATE"), {"id": target}
        )
        holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
        try:
            request = asyncio.create_task(
                _labeled("command", lambda: _send(f, writer, line, body, "credential-wait"))
            )
            tasks.append(request)
            await asyncio.wait_for(entered.wait(), 5)
            proof = await _blocked(f["engine"], pids["command"], {holder_pid}, table, request)
            record_testsuite_property(
                "credential_wait_" + credential + "_" + wait_on,
                json.dumps(proof, default=str, sort_keys=True),
            )
            changed = await f["client"].post(
                "/api/v1/auth/switch-org/" + f["other_org"]
                if credential == "switch"
                else "/api/v1/auth/logout",
                headers=writer["headers"],
            )
            assert changed.status_code == 200, changed.text
            await transaction.rollback()
            result = await asyncio.wait_for(request, 8)
            # The pinned refresh helper intentionally treats a session moved
            # outside its original RLS scope as the same opaque credential
            # refusal as revocation, on both databases.
            assert result.status_code == 401, result.text
            assert await _domain(f) == before
            assert await _facts(f) == cash
        finally:
            if transaction.is_active:
                await transaction.rollback()
            await _finish(tasks)


async def test_distinct_actors_wait_on_one_line_and_refresh_a_preloaded_identity_map(
    native_commands, monkeypatch, record_testsuite_property
):
    f = native_commands
    first, second = f["writers"]
    line = f["lines"][0]
    before_cash = await _facts(f)
    bodies = [
        await _review(f, line, writer, "ignore", reason="Reviewed native source " + str(position))
        for position, writer in enumerate((first, second))
    ]
    locked, second_entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    pids, seen, retained = {}, {}, []
    original = commands._line
    calls = {}

    async def observe(db, current, line_id, **kwargs):
        label = WHO.get()
        if label not in ("first", "second") or not kwargs.get("lock"):
            return await original(db, current, line_id, **kwargs)
        pids[label] = await db.scalar(text("SELECT pg_backend_pid()"))
        calls[label] = calls.get(label, 0) + 1
        if label == "second" and calls[label] == 1:
            cached = await db.get(BankLine, line_id)
            retained.append(cached)
            seen["preloaded"] = cached.version
            second_entered.set()
        row = await original(db, current, line_id, **kwargs)
        if label == "first" and calls[label] == 1:
            locked.set()
            await release.wait()
        if label == "second":
            seen["after_wait"] = row.version
        return row

    monkeypatch.setattr(commands, "_line", observe)
    tasks = []
    try:
        tasks.append(
            asyncio.create_task(
                _labeled("first", lambda: _send(f, first, line, bodies[0], "line-first"))
            )
        )
        await asyncio.wait_for(locked.wait(), 5)
        tasks.append(
            asyncio.create_task(
                _labeled("second", lambda: _send(f, second, line, bodies[1], "line-second"))
            )
        )
        await asyncio.wait_for(second_entered.wait(), 5)
        proof = await _blocked(f["engine"], pids["second"], {pids["first"]}, "bank_lines", tasks[1])
        record_testsuite_property("line_wait", json.dumps(proof, default=str, sort_keys=True))
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks), 10)
        assert [r.status_code for r in results] == [200, 409], [r.text for r in results]
        assert results[1].json()["code"] == "reconciliation_line_stale"
        assert seen == {"preloaded": 1, "after_wait": 2}
        domain = await _domain(f)
        assert len(domain["commands"]) == len(domain["audit"]) == 1
        assert next(row for row in domain["lines"] if row["id"] == line)["version"] == 2
        assert await _facts(f) == before_cash
    finally:
        await _finish(tasks, release)


@pytest.mark.parametrize("cross_line", [False, True], ids=["same-intention", "same-key-other-line"])
async def test_same_actor_key_queues_on_membership_then_replays_or_refuses_cross_line(
    native_commands, monkeypatch, record_testsuite_property, cross_line
):
    f = native_commands
    writer = f["writers"][0]
    first_line = f["lines"][0]
    second_line = f["lines"][1] if cross_line else first_line
    first_body = await _review(
        f, first_line, writer, "ignore", reason="Reviewed original intention"
    )
    second_body = await _review(
        f, second_line, writer, "ignore", reason="Reviewed original intention"
    )
    cash = await _facts(f)
    held, entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    pids = {}
    original_actor, original_line = commands.lock_actor, commands._line
    second_line_loaded = []

    async def actor(db, current, permission):
        if WHO.get() == "second":
            pids["second"] = await db.scalar(text("SELECT pg_backend_pid()"))
            entered.set()
        return await original_actor(db, current, permission)

    first_calls = []

    async def line(db, current, line_id, **kwargs):
        row = await original_line(db, current, line_id, **kwargs)
        if WHO.get() == "second":
            second_line_loaded.append(line_id)
        if WHO.get() == "first" and kwargs.get("lock") and not first_calls:
            first_calls.append(True)
            pids["first"] = await db.scalar(text("SELECT pg_backend_pid()"))
            held.set()
            await release.wait()
        return row

    monkeypatch.setattr(commands, "lock_actor", actor)
    monkeypatch.setattr(commands, "_line", line)
    tasks = []
    try:
        tasks.append(
            asyncio.create_task(
                _labeled("first", lambda: _send(f, writer, first_line, first_body, "one-owner-key"))
            )
        )
        await asyncio.wait_for(held.wait(), 5)
        tasks.append(
            asyncio.create_task(
                _labeled(
                    "second", lambda: _send(f, writer, second_line, second_body, "one-owner-key")
                )
            )
        )
        await asyncio.wait_for(entered.wait(), 5)
        proof = await _blocked(
            f["engine"], pids["second"], {pids["first"]}, "memberships", tasks[1]
        )
        record_testsuite_property(
            "same_key_membership_" + str(cross_line), json.dumps(proof, default=str, sort_keys=True)
        )
        release.set()
        a, b = await asyncio.wait_for(asyncio.gather(*tasks), 10)
        assert a.status_code == 200, a.text
        if cross_line:
            assert (
                b.status_code == 409
                and b.json()["code"] == "reconciliation_command_payload_conflict"
            ), b.text
        else:
            assert b.status_code == 200 and b.json() == a.json(), b.text
        assert second_line_loaded == []
        domain = await _domain(f)
        assert len(domain["commands"]) == len(domain["audit"]) == 1
        assert next(row for row in domain["lines"] if row["id"] == f["lines"][1])["version"] == 1
        assert await _facts(f) == cash
    finally:
        await _finish(tasks, release)


async def test_distinct_actors_wait_on_one_target_and_only_one_line_uses_it(
    native_commands, monkeypatch, record_testsuite_property
):
    f = native_commands
    bodies = [
        await _review(f, line, writer, "match", target={"kind": "receipt", "id": f["receipts"][0]})
        for line, writer in zip(f["lines"], f["writers"], strict=True)
    ]
    cash = await _facts(f)
    entered = {name: asyncio.Event() for name in ("first", "second")}
    pids = {}
    original = reconciliation.validate_match

    async def target(db, *args, **kwargs):
        label = WHO.get()
        if label in entered:
            pids[label] = await db.scalar(text("SELECT pg_backend_pid()"))
            entered[label].set()
        return await original(db, *args, **kwargs)

    monkeypatch.setattr(reconciliation, "validate_match", target)
    tasks = []
    async with f["factory"]() as holder:
        transaction = await holder.begin()
        await holder.execute(
            text("SELECT id FROM receipts WHERE id=:id FOR UPDATE"), {"id": f["receipts"][0]}
        )
        holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
        try:
            tasks.append(
                asyncio.create_task(
                    _labeled(
                        "first",
                        lambda: _send(f, f["writers"][0], f["lines"][0], bodies[0], "target-first"),
                    )
                )
            )
            await asyncio.wait_for(entered["first"].wait(), 5)
            first_proof = await _blocked(
                f["engine"], pids["first"], {holder_pid}, "receipts", tasks[0]
            )
            tasks.append(
                asyncio.create_task(
                    _labeled(
                        "second",
                        lambda: _send(
                            f, f["writers"][1], f["lines"][1], bodies[1], "target-second"
                        ),
                    )
                )
            )
            await asyncio.wait_for(entered["second"].wait(), 5)
            second_proof = await _blocked(
                f["engine"], pids["second"], {holder_pid, pids["first"]}, "receipts", tasks[1]
            )
            record_testsuite_property(
                "target_wait_first", json.dumps(first_proof, default=str, sort_keys=True)
            )
            record_testsuite_property(
                "target_wait_second", json.dumps(second_proof, default=str, sort_keys=True)
            )
            await transaction.rollback()
            results = await asyncio.wait_for(asyncio.gather(*tasks), 10)
            assert sorted(r.status_code for r in results) == [200, 400], [r.text for r in results]
            loser = next(r for r in results if r.status_code == 400)
            assert loser.json()["code"] == "reconciliation_match_invalid"
            domain = await _domain(f)
            assert len(domain["commands"]) == len(domain["audit"]) == 1
            assert sorted((row["status"], row["version"]) for row in domain["lines"]) == [
                ("matched", 2),
                ("unmatched", 1),
            ]
            assert await _facts(f) == cash
        finally:
            if transaction.is_active:
                await transaction.rollback()
            await _finish(tasks)


@pytest.mark.parametrize("change", ["role", "offboard"])
@pytest.mark.parametrize("winner", ["revocation", "command"])
async def test_real_team_change_and_command_follow_both_membership_lock_orders(
    native_commands, monkeypatch, record_testsuite_property, change, winner
):
    f = native_commands
    writer = f["writers"][1]
    line = f["lines"][0]
    body = await _review(f, line, writer, "ignore", reason="Reviewed before authority change")
    cash = await _facts(f)
    held, entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    pids = {}
    original_actor, original_member = commands.lock_actor, team.get_member
    calls = []

    async def actor(db, current, permission):
        if WHO.get() == "command":
            pids["command"] = await db.scalar(text("SELECT pg_backend_pid()"))
            if winner == "revocation":
                entered.set()
        row = await original_actor(db, current, permission)
        if WHO.get() == "command" and winner == "command" and not calls:
            calls.append(True)
            held.set()
            await release.wait()
        return row

    async def member(db, org, user_id, *, lock=False):
        if WHO.get() == "revoke" and lock:
            pids["revoke"] = await db.scalar(text("SELECT pg_backend_pid()"))
            if winner == "command":
                entered.set()
        row = await original_member(db, org, user_id, lock=lock)
        if WHO.get() == "revoke" and lock and winner == "revocation":
            held.set()
            await release.wait()
        return row

    monkeypatch.setattr(commands, "lock_actor", actor)
    monkeypatch.setattr(team, "get_member", member)
    tasks = []

    async def revoke():
        return await f["client"].patch(
            "/api/v1/team/members/" + writer["id"],
            headers=f["control"],
            json={"role": "auditor"} if change == "role" else {"is_active": False},
        )

    try:
        if winner == "revocation":
            tasks.append(asyncio.create_task(_labeled("revoke", revoke)))
            await asyncio.wait_for(held.wait(), 5)
            tasks.append(
                asyncio.create_task(
                    _labeled("command", lambda: _send(f, writer, line, body, "authority-order"))
                )
            )
            waiting, blocker = tasks[1], "revoke"
        else:
            tasks.append(
                asyncio.create_task(
                    _labeled("command", lambda: _send(f, writer, line, body, "authority-order"))
                )
            )
            await asyncio.wait_for(held.wait(), 5)
            tasks.append(asyncio.create_task(_labeled("revoke", revoke)))
            waiting, blocker = tasks[1], "command"
        await asyncio.wait_for(entered.wait(), 5)
        proof = await _blocked(
            f["engine"],
            pids["command" if winner == "revocation" else "revoke"],
            {pids[blocker]},
            "memberships",
            waiting,
        )
        record_testsuite_property(
            "authority_" + change + "_" + winner, json.dumps(proof, default=str, sort_keys=True)
        )
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks), 10)
        command_result = results[1] if winner == "revocation" else results[0]
        revoke_result = results[0] if winner == "revocation" else results[1]
        assert revoke_result.status_code == 200, revoke_result.text
        assert command_result.status_code == (403 if winner == "revocation" else 200), (
            command_result.text
        )
        domain = await _domain(f)
        expected = 0 if winner == "revocation" else 1
        assert len(domain["commands"]) == len(domain["audit"]) == expected
        assert next(row for row in domain["lines"] if row["id"] == line)["version"] == 1 + expected
        async with f["factory"]() as db:
            global_user = (
                await db.execute(select(User.org_id, User.is_active).where(User.id == writer["id"]))
            ).one()
            assert global_user == (f["other_org"], True)
        assert await _facts(f) == cash
    finally:
        await _finish(tasks, release)


@pytest.mark.parametrize("wait_on", ["line", "target"])
async def test_actual_tenant_audit_advisory_wait_begins_only_after_source_wait_finishes(
    native_commands, monkeypatch, record_testsuite_property, wait_on
):
    f = native_commands
    writer = f["writers"][0]
    line, receipt = f["lines"][0], f["receipts"][0]
    body = await _review(f, line, writer, "match", target={"kind": "receipt", "id": receipt})
    before, cash = await _domain(f), await _facts(f)
    entered = asyncio.Event()
    pids = {}
    original = commands._line if wait_on == "line" else reconciliation.validate_match
    if wait_on == "line":

        async def observe(db, current, line_id, **kwargs):
            if WHO.get() == "command" and kwargs.get("lock"):
                pids["command"] = await db.scalar(text("SELECT pg_backend_pid()"))
                entered.set()
            return await original(db, current, line_id, **kwargs)

        monkeypatch.setattr(commands, "_line", observe)
    else:

        async def observe(db, *args, **kwargs):
            if WHO.get() == "command":
                pids["command"] = await db.scalar(text("SELECT pg_backend_pid()"))
                entered.set()
            return await original(db, *args, **kwargs)

        monkeypatch.setattr(reconciliation, "validate_match", observe)
    tasks = []
    async with f["factory"]() as source_holder, f["factory"]() as audit_holder:
        source_tx, audit_tx = await source_holder.begin(), await audit_holder.begin()
        table = "bank_lines" if wait_on == "line" else "receipts"
        await source_holder.execute(
            text(f"SELECT id FROM {table} WHERE id=:id FOR UPDATE"),
            {"id": line if wait_on == "line" else receipt},
        )
        source_pid = await source_holder.scalar(text("SELECT pg_backend_pid()"))
        # This is the exact source-defined audit.record tenant key and native
        # transaction-level advisory lock, not an application stub.
        await audit_holder.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": "audit:" + f["org"]}
        )
        audit_pid = await audit_holder.scalar(text("SELECT pg_backend_pid()"))
        try:
            tasks.append(
                asyncio.create_task(
                    _labeled("command", lambda: _send(f, writer, line, body, "audit-order"))
                )
            )
            await asyncio.wait_for(entered.wait(), 5)
            source_proof = await _blocked(
                f["engine"], pids["command"], {source_pid}, table, tasks[0]
            )
            async with f["engine"].connect() as observer:
                assert (
                    await observer.scalar(
                        text(
                            "SELECT count(*) FROM pg_locks WHERE pid=:pid AND locktype='advisory'"
                        ),
                        {"pid": pids["command"]},
                    )
                    == 0
                )
            await source_tx.rollback()
            audit_proof = await _blocked(
                f["engine"], pids["command"], {audit_pid}, "pg_advisory", tasks[0]
            )
            async with f["engine"].connect() as observer:
                assert (
                    await observer.scalar(
                        text(
                            "SELECT count(*) FROM pg_locks WHERE pid=:pid AND locktype='advisory' AND NOT granted"
                        ),
                        {"pid": pids["command"]},
                    )
                    == 1
                )
            assert await _domain(f) == before
            await audit_tx.rollback()
            response = await asyncio.wait_for(tasks[0], 8)
            assert response.status_code == 200, response.text
            record_testsuite_property(
                "source_then_audit_" + wait_on,
                json.dumps(
                    {"source": source_proof, "audit": audit_proof}, default=str, sort_keys=True
                ),
            )
            domain = await _domain(f)
            assert len(domain["commands"]) == len(domain["audit"]) == 1
            assert await _facts(f) == cash
        finally:
            if source_tx.is_active:
                await source_tx.rollback()
            if audit_tx.is_active:
                await audit_tx.rollback()
            await _finish(tasks)


async def test_old_key_after_later_transitions_returns_immutable_outcome_without_target_execution(
    native_commands, monkeypatch
):
    f = native_commands
    writer = f["writers"][0]
    line = f["lines"][0]
    cash = await _facts(f)
    original_body = await _review(f, line, writer, "ignore", reason="Original reviewed duplicate")
    original = await _send(f, writer, line, original_body, "original-receipt")
    assert original.status_code == 200, original.text
    restore = await _review(f, line, writer, "restore")
    assert (await _send(f, writer, line, restore, "restore-cycle")).status_code == 200
    match = await _review(
        f, line, writer, "match", target={"kind": "receipt", "id": f["receipts"][0]}
    )
    assert (await _send(f, writer, line, match, "match-later")).status_code == 200
    before = await _domain(f)

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("Historical replay must not execute a current target")

    monkeypatch.setattr(reconciliation, "validate_match", forbidden)
    replay = await _send(f, writer, line, original_body, "original-receipt")
    assert replay.status_code == 200 and replay.json() == original.json(), replay.text
    checked = await f["client"].get(
        BASE + "/" + line + "/commands/original-receipt", headers=writer["headers"]
    )
    assert checked.status_code == 200 and checked.json() == original.json(), checked.text
    current = await f["client"].get(BASE + "/" + line, headers=writer["headers"])
    assert (
        current.status_code == 200
        and current.json()["version"] == 4
        and current.json()["status"] == "matched"
    )
    assert await _domain(f) == before
    assert len(before["commands"]) == len(before["audit"]) == 3
    assert await _facts(f) == cash


async def test_native_uppercase_line_and_target_uuid_replay_one_canonical_command(native_commands):
    f = native_commands
    writer = f["writers"][1]
    line, receipt = f["lines"][0], f["receipts"][0]
    cash = await _facts(f)
    body = await _review(
        f, line, writer, "match", target={"kind": "receipt", "id": receipt.upper()}
    )
    first = await _send(f, writer, line.upper(), body, "canonical-target")
    assert first.status_code == 200, first.text
    body["target"]["id"] = receipt
    second = await _send(f, writer, line, body, "canonical-target")
    assert second.status_code == 200 and second.json() == first.json(), second.text
    domain = await _domain(f)
    assert len(domain["commands"]) == len(domain["audit"]) == 1
    assert next(row for row in domain["lines"] if row["id"] == line)["matched_id"] == receipt
    assert await _facts(f) == cash
