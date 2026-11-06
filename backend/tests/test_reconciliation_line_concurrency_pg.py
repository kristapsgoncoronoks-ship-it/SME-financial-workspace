"""Incumbent native line/target schedules now use explicit reviewed commands.

The old unversioned erasure/no-op policy is superseded: stale Unmatch/Restore
loses after a later winner. Actual cash, source, PID/lock and no-write controls
remain; full HTTP Session/role checks live in test_reconciliation_commands_pg.
"""

import asyncio
import json
import os
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, event, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core import tenant
from app.core.errors import AppError
from app.core.identity import principal
from app.models import Base
from app.models.audit import AuditEvent
from app.models.bank_import import BankLine, BankStatement
from app.models.membership import Membership
from app.models.module import OrgModule
from app.models.organization import Organization
from app.models.receipt import Receipt
from app.models.reconciliation_command import ReconciliationCommand
from app.models.user import User, UserRole
from app.schemas.reconciliation_commands import CommandIntent
from app.services import reconciliation as recon
from app.services import reconciliation_commands as commands

PG_URL = os.environ.get("RLS_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not PG_URL, reason="requires migrated non-superuser PostgreSQL")
LEDGER_TABLES = (
    "receipts",
    "payments",
    "reimbursement_batches",
    "payment_runs",
    "supplier_payments",
    "issued_invoices",
    "expense_reports",
    "expense_items",
    "audit_events",
    "jobs",
)


@pytest_asyncio.fixture
async def native_lines():
    engine = create_async_engine(PG_URL, pool_size=4, max_overflow=0)
    actor_ids = [str(uuid4()), str(uuid4())]
    factory = async_sessionmaker(
        engine,
        expire_on_commit=False,
        autoflush=False,
        info={"actor_ids": actor_ids, "actor_id": actor_ids[0]},
    )
    org_id, statement_id = str(uuid4()), str(uuid4())
    line_ids = [str(uuid4()) for _ in range(2)]
    # Ensure the native UUID spelling control always has a letter to uppercase.
    receipt_ids = ["a" + str(uuid4())[1:] for _ in range(2)]
    scope = tenant.set_current_org(org_id)
    try:
        async with factory() as db:
            assert not await db.scalar(
                text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user")
            )
            db.add(Organization(id=org_id, name="Synthetic native current bank line"))
            await db.flush()
            db.add_all(
                User(
                    id=actor_id,
                    org_id=org_id,
                    email=f"native-reviewed-{actor_id}@example.com",
                    name="Synthetic command actor",
                    hashed_password="not-a-login",
                    role=UserRole.accountant,
                    email_verified=True,
                )
                for actor_id in actor_ids
            )
            await db.flush()
            db.add_all(
                Membership(
                    org_id=org_id, user_id=actor_id, role=UserRole.accountant, status="active"
                )
                for actor_id in actor_ids
            )
            db.add(OrgModule(org_id=org_id, key="issuing", enabled=True))
            await db.flush()
            db.add(
                BankStatement(
                    id=statement_id,
                    org_id=org_id,
                    filename="synthetic-serialization.csv",
                    sha256=uuid4().hex * 2,
                    line_count=2,
                )
            )
            await db.flush()
            db.add_all(
                BankLine(
                    id=identifier,
                    org_id=org_id,
                    statement_id=statement_id,
                    line_date=date(2026, 11, 5),
                    description="Synthetic same-line serialization",
                    amount=Decimal("1210.00"),
                    direction="credit",
                    currency="EUR",
                )
                for identifier in line_ids
            )
            db.add_all(
                Receipt(
                    id=identifier,
                    org_id=org_id,
                    amount=Decimal("1210.00"),
                    received_on=date(2026, 11, 5),
                )
                for identifier in receipt_ids
            )
            await db.commit()
        yield engine, factory, org_id, line_ids, receipt_ids
    finally:
        async with factory() as db:
            await db.execute(
                delete(ReconciliationCommand).where(ReconciliationCommand.org_id == org_id)
            )
            await db.execute(delete(AuditEvent).where(AuditEvent.org_id == org_id))
            await db.execute(delete(Organization).where(Organization.id == org_id))
            await db.commit()
        tenant.reset_current_org(scope)
        await engine.dispose()


async def ledger(factory, org_id):
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
            for name in LEDGER_TABLES
        }


async def line_state(factory, org_id, line_id):
    async with factory() as db:
        row = (
            (
                await db.execute(
                    select(BankLine.__table__).where(
                        BankLine.org_id == org_id, BankLine.id == line_id
                    )
                )
            )
            .mappings()
            .one()
        )
        return dict(row)


def assert_ledger(before, after, committed):
    assert {key: value for key, value in before.items() if key != "audit_events"} == {
        key: value for key, value in after.items() if key != "audit_events"
    }
    old = {row["id"]: row for row in before["audit_events"]}
    current = {row["id"]: row for row in after["audit_events"]}
    assert all(current.get(key) == value for key, value in old.items())
    appended = [value for key, value in current.items() if key not in old]
    assert len(appended) == committed and all(
        row["action"] == "reconciliation.command_committed" for row in appended
    )


async def action(db, org_id, line, operation, target_id, *, key=None, intent_override=None):
    line_id = line.id
    operation = "match" if operation == "confirm" else operation
    body = intent_override or CommandIntent.model_validate(
        {
            "operation": operation,
            "expected_version": line.version,
            "expected": {
                "status": "matched"
                if operation == "unmatch"
                else "ignored"
                if operation == "restore"
                else "unmatched",
                "matched_kind": line.matched_kind if operation == "unmatch" else None,
                "matched_id": line.matched_id if operation == "unmatch" else None,
            },
            "target": {"kind": "receipt", "id": target_id} if operation == "match" else None,
            "reason": "Reviewed synthetic fee" if operation == "ignore" else None,
        }
    )
    actor_id = db.info["actor_id"]

    async def refresh():
        assert not (db.new or db.dirty or db.deleted)
        db.expire_all()
        user = await db.get(User, actor_id)
        member = await db.scalar(
            select(Membership)
            .where(Membership.org_id == org_id, Membership.user_id == actor_id)
            .execution_options(populate_existing=True)
        )
        return principal(user, member)

    try:
        current = await refresh()
        outcome = await commands.execute(db, current, line_id, key or uuid4().hex, body, refresh)
        return {"result": "accepted", "receipt": outcome.model_dump(mode="json")}
    except AppError as error:
        await db.rollback()
        return {"result": "refused", "detail": str(error), "code": error.code}


@pytest.mark.asyncio
@pytest.mark.parametrize("first,second", [("confirm", "confirm"), ("ignore", "confirm")])
async def test_native_sequential_line_requires_explicit_restore(native_lines, first, second):
    _engine, factory, org_id, line_ids, receipt_ids = native_lines
    before_ledger = await ledger(factory, org_id)
    async with factory() as db:
        line = await recon.get_line(db, org_id, line_ids[0])
        assert (await action(db, org_id, line, first, receipt_ids[0]))["result"] == "accepted"
        before = await line_state(factory, org_id, line_ids[0])
        result = await action(db, org_id, line, second, receipt_ids[1])
    after = await line_state(factory, org_id, line_ids[0])
    assert result["result"] == "refused", {"second": result, "before": before, "after": after}
    assert after == before
    assert_ledger(before_ledger, await ledger(factory, org_id), 1)


@pytest.mark.asyncio
async def test_native_existing_uuid_spelling_replays_without_writing(native_lines):
    """PostgreSQL accepts uppercase UUIDs, then reads the same identity lowercase."""
    engine, factory, org_id, line_ids, receipt_ids = native_lines
    before_ledger = await ledger(factory, org_id)
    requested_id = receipt_ids[0].upper()
    assert requested_id != receipt_ids[0]
    async with factory() as db:
        line = await recon.get_line(db, org_id, line_ids[0])
        assert (await action(db, org_id, line, "confirm", requested_id, key="uuid-key"))[
            "result"
        ] == "accepted"
    before = await line_state(factory, org_id, line_ids[0])
    assert before["matched_id"] == receipt_ids[0]
    writes = []

    def observe(_connection, _cursor, statement, *_args):
        if statement.lstrip().split(None, 1)[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
            writes.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", observe)
    try:
        async with factory() as db:
            line = await recon.get_line(db, org_id, line_ids[0])
            result = await action(
                db,
                org_id,
                line,
                "confirm",
                requested_id,
                key="uuid-key",
                intent_override=CommandIntent.model_validate(
                    {
                        "operation": "match",
                        "expected_version": 1,
                        "expected": {"status": "unmatched"},
                        "target": {"kind": "receipt", "id": receipt_ids[0]},
                    }
                ),
            )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", observe)
    assert result["result"] == "accepted", result
    assert writes == []
    assert await line_state(factory, org_id, line_ids[0]) == before
    assert_ledger(before_ledger, await ledger(factory, org_id), 1)


async def blocked_by(engine, blocked, blocker, relation):
    async with engine.connect() as observer:
        async with asyncio.timeout(5):
            while True:
                # pg_blocking_pids is live, but pg_stat_activity can retain a
                # transaction snapshot from an earlier admission query. Observe
                # both from the current statistics snapshot on every poll.
                await observer.execute(text("SELECT pg_stat_clear_snapshot()"))
                row = (
                    (
                        await observer.execute(
                            text(
                                "SELECT pg_blocking_pids(pid) AS blockers, query, wait_event_type "
                                "FROM pg_stat_activity WHERE pid=:pid"
                            ),
                            {"pid": blocked},
                        )
                    )
                    .mappings()
                    .one()
                )
                if blocker in row["blockers"]:
                    assert row["wait_event_type"] == "Lock" and relation in row["query"]
                    return dict(row)
                await asyncio.sleep(0.02)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "first,second,same_target",
    [
        ("confirm", "confirm", False),
        ("confirm", "ignore", False),
        ("ignore", "confirm", False),
        ("confirm", "unmatch", False),
        ("ignore", "unmatch", False),
        ("confirm", "confirm", True),
    ],
)
async def test_native_two_sessions_preserve_winner_and_target_guard(
    native_lines, monkeypatch, first, second, same_target
):
    engine, factory, org_id, line_ids, receipt_ids = native_lines
    stale_intent = None
    if second == "unmatch":
        async with factory() as seed:
            old = await recon.get_line(seed, org_id, line_ids[0])
            old.status = "matched" if first == "confirm" else "ignored"
            old.matched_kind = "receipt" if first == "confirm" else None
            old.matched_id = receipt_ids[1] if first == "confirm" else None
            await seed.commit()
            stale_intent = CommandIntent.model_validate(
                {
                    "operation": "unmatch" if first == "confirm" else "restore",
                    "expected_version": 1,
                    "expected": {
                        "status": old.status,
                        "matched_kind": old.matched_kind,
                        "matched_id": old.matched_id,
                    },
                }
            )
            assert (
                await action(
                    seed,
                    org_id,
                    old,
                    stale_intent.operation,
                    receipt_ids[1],
                    intent_override=stale_intent,
                )
            )["result"] == "accepted"
    before_ledger = await ledger(factory, org_id)
    first_flushed, release = asyncio.Event(), asyncio.Event()
    async with factory() as db1, factory() as db2:
        db2.info["actor_id"] = db2.info["actor_ids"][1]
        line1 = await recon.get_line(db1, org_id, line_ids[0])
        line2 = await recon.get_line(db2, org_id, line_ids[1] if same_target else line_ids[0])
        assert line1.status == line2.status == "unmatched"
        pid1 = await db1.scalar(text("SELECT pg_backend_pid()"))
        pid2 = await db2.scalar(text("SELECT pg_backend_pid()"))
        assert pid1 != pid2
        original_commit = db1.commit

        async def hold_commit():
            await db1.flush()
            first_flushed.set()
            await release.wait()
            await original_commit()

        monkeypatch.setattr(db1, "commit", hold_commit)
        task1 = asyncio.create_task(action(db1, org_id, line1, first, receipt_ids[0]))
        task2 = None
        try:
            async with asyncio.timeout(5):
                await first_flushed.wait()
            task2 = asyncio.create_task(
                action(
                    db2,
                    org_id,
                    line2,
                    second,
                    receipt_ids[0] if same_target else receipt_ids[1],
                    intent_override=stale_intent,
                )
            )
            waiting = await blocked_by(
                engine, pid2, pid1, "receipts" if same_target else "bank_lines"
            )
            release.set()
            async with asyncio.timeout(5):
                winner, loser = await asyncio.gather(task1, task2)
        finally:
            release.set()
            for task in (task1, task2):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(task for task in (task1, task2) if task), return_exceptions=True)
    state = await line_state(factory, org_id, line_ids[0])
    evidence = {
        "first": first,
        "second": second,
        "same_target": same_target,
        "first_pid": pid1,
        "second_pid": pid2,
        "observed_wait": waiting,
        "winner": winner,
        "loser": loser,
        "line": state,
    }
    folder = os.environ.get("RECON_LINE_ARTIFACT_DIR")
    if folder:
        Path(folder).mkdir(parents=True, exist_ok=True)
        Path(folder, f"{first}-{second}-same-target-{same_target}.json").write_text(
            json.dumps(evidence, indent=2, default=str) + "\n"
        )
    assert winner["result"] == "accepted"
    # The losing reviewed intent must never erase a newer association/disposition.
    assert loser["result"] == "refused", evidence
    if not same_target:
        assert loser["code"] == "reconciliation_line_stale", evidence
    expected_status = "matched" if first == "confirm" else "ignored"
    assert state["status"] == expected_status
    assert state["matched_id"] == (receipt_ids[0] if expected_status == "matched" else None)
    if same_target:
        other = await line_state(factory, org_id, line_ids[1])
        assert other["status"] == "unmatched" and other["matched_id"] is None
    assert_ledger(before_ledger, await ledger(factory, org_id), 1)
