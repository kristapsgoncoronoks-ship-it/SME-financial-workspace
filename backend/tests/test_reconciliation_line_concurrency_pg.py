"""Native current-line serialization; no expected-version/history claim."""

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
from app.models import Base
from app.models.bank_import import BankLine, BankStatement
from app.models.organization import Organization
from app.models.receipt import Receipt
from app.services import reconciliation as recon

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
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
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


async def action(db, org_id, line, operation, target_id):
    try:
        if operation == "confirm":
            await recon.confirm_match(db, org_id, line, "receipt", target_id)
        elif operation == "ignore":
            await recon.set_ignored(db, org_id, line)
        else:
            assert operation == "unmatch"
            await recon.unmatch(db, org_id, line)
        return {"result": "accepted"}
    except recon.ReconError as error:
        await db.rollback()
        return {"result": "refused", "detail": str(error)}


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
    assert await ledger(factory, org_id) == before_ledger


@pytest.mark.asyncio
async def test_native_existing_uuid_spelling_replays_without_writing(native_lines):
    """PostgreSQL accepts uppercase UUIDs, then reads the same identity lowercase."""
    engine, factory, org_id, line_ids, receipt_ids = native_lines
    before_ledger = await ledger(factory, org_id)
    requested_id = receipt_ids[0].upper()
    assert requested_id != receipt_ids[0]
    async with factory() as db:
        line = await recon.get_line(db, org_id, line_ids[0])
        assert (await action(db, org_id, line, "confirm", requested_id))["result"] == "accepted"
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
            result = await action(db, org_id, line, "confirm", requested_id)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", observe)
    assert result["result"] == "accepted", result
    assert writes == []
    assert await line_state(factory, org_id, line_ids[0]) == before
    assert await ledger(factory, org_id) == before_ledger


async def blocked_by(engine, blocked, blocker, relation):
    async with engine.connect() as observer:
        async with asyncio.timeout(5):
            while True:
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
    before_ledger = await ledger(factory, org_id)
    first_flushed, release = asyncio.Event(), asyncio.Event()
    async with factory() as db1, factory() as db2:
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
                    db2, org_id, line2, second, receipt_ids[0] if same_target else receipt_ids[1]
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
    # Unmatch/Restore serializes against the current state; it does not identify
    # the client's old intent (the API has no expected-version field).
    assert loser["result"] == ("accepted" if second == "unmatch" else "refused"), evidence
    expected_status = (
        "unmatched" if second == "unmatch" else ("matched" if first == "confirm" else "ignored")
    )
    assert state["status"] == expected_status
    assert state["matched_id"] == (receipt_ids[0] if expected_status == "matched" else None)
    if same_target:
        other = await line_state(factory, org_id, line_ids[1])
        assert other["status"] == "unmatched" and other["matched_id"] is None
    assert await ledger(factory, org_id) == before_ledger
