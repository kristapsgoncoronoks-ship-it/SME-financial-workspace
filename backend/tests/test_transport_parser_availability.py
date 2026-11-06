"""Pure parser offload owns one CPU slot through completion and cancellation."""

import asyncio
import inspect
import json
import threading
from contextlib import suppress
from contextvars import ContextVar
from decimal import ROUND_DOWN, Decimal, localcontext

import pytest
from sqlalchemy import select

from app.core import storage
from app.models import Base
from app.services import documents, modules
from app.services.transport import fuel_card_parser
from tests.factories.transport import synthetic_eurowag_statement
from tests.transport.conftest import make_entity


@pytest.mark.asyncio
async def test_parser_runs_off_loop_and_busy_has_no_queue(monkeypatch):
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    calls = []
    expected = fuel_card_parser.ParsedStatement(network="Synthetic")

    def parse(filename, content):
        calls.append((filename, content, threading.get_ident()))
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5), "test must release its owned worker"
        return expected

    monkeypatch.setattr(fuel_card_parser, "run", parse)
    task = asyncio.create_task(fuel_card_parser.run_async("synthetic.csv", b"synthetic"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert calls[0][:2] == ("synthetic.csv", b"synthetic")
        assert calls[0][2] != threading.get_ident()
        progress = asyncio.Event()
        loop.call_soon(progress.set)
        await asyncio.wait_for(progress.wait(), 5)
        with pytest.raises(fuel_card_parser.StatementReadingBusy) as busy:
            await fuel_card_parser.run_async("second.csv", b"not queued")
        assert (busy.value.status, busy.value.code, busy.value.extra()) == (
            503,
            "statement_reading_busy",
            {"retryable": True},
        )
        assert len(calls) == 1
    finally:
        release.set()
        assert await task is expected
    assert await fuel_card_parser.run_async("after.csv", b"after") is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("worker_fails", [False, True])
async def test_repeated_cancellation_holds_slot_until_worker_finishes(monkeypatch, worker_fails):
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    calls = []

    def parse(filename, content):
        calls.append(filename)
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5), "test must release its owned worker"
        if worker_fails:
            raise ValueError("Synthetic worker failure during cancellation")
        return fuel_card_parser.ParsedStatement(network="Synthetic")

    monkeypatch.setattr(fuel_card_parser, "run", parse)
    task = asyncio.create_task(fuel_card_parser.run_async("cancelled.csv", b"synthetic"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)  # dispatch cancellation, not an elapsed-time assertion
            assert not task.done()
            with pytest.raises(fuel_card_parser.StatementReadingBusy):
                await fuel_card_parser.run_async("blocked.csv", b"not queued")
        assert calls == ["cancelled.csv"]
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    monkeypatch.setattr(
        fuel_card_parser, "run", lambda *_: fuel_card_parser.ParsedStatement(network="Recovered")
    )
    assert (await fuel_card_parser.run_async("recovered.csv", b"recovered")).network == "Recovered"


@pytest.mark.asyncio
@pytest.mark.parametrize("worker_fails", [False, True])
async def test_inner_task_cancellation_cannot_release_a_running_native_parse(
    monkeypatch, worker_fails
):
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    inner = []
    calls = []
    create_task = asyncio.create_task

    def capture(coroutine):
        task = create_task(coroutine)
        inner.append(task)
        return task

    def parse(filename, _content):
        calls.append(filename)
        if filename == "owned.csv":
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5), "test must release its owned worker"
            if worker_fails:
                raise ValueError("Synthetic native parse failure after task cancellation")
        return fuel_card_parser.ParsedStatement(network="Synthetic")

    monkeypatch.setattr(fuel_card_parser, "run", parse)
    monkeypatch.setattr(fuel_card_parser.asyncio, "create_task", capture)
    task = create_task(fuel_card_parser.run_async("owned.csv", b"owned"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert len(inner) == 1
        inner[0].cancel()  # AnyIO's wrapper may end while its callable still runs.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        with pytest.raises(fuel_card_parser.StatementReadingBusy):
            await fuel_card_parser.run_async("must-not-start.csv", b"blocked")
        assert calls == ["owned.csv"]
        assert not task.done()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert (await fuel_card_parser.run_async("after.csv", b"after")).network == "Synthetic"


@pytest.mark.asyncio
async def test_original_exception_context_and_decimal_behavior_are_preserved(monkeypatch):
    error = ValueError("Synthetic late malformed row")
    context = ContextVar("parser_availability_test", default="missing")

    def parse(_filename, _content):
        assert context.get() == "caller"
        assert Decimal("1.239").quantize(Decimal("0.01")) == Decimal("1.23")
        raise error

    monkeypatch.setattr(fuel_card_parser, "run", parse)
    token = context.set("caller")
    try:
        with localcontext() as decimal_context:
            decimal_context.rounding = ROUND_DOWN
            with pytest.raises(ValueError) as raised:
                await fuel_card_parser.run_async("malformed.csv", b"synthetic")
            assert raised.value is error
    finally:
        context.reset(token)
    monkeypatch.setattr(
        fuel_card_parser, "run", lambda *_: fuel_card_parser.ParsedStatement(network="Recovered")
    )
    assert (await fuel_card_parser.run_async("after.csv", b"after")).network == "Recovered"


@pytest.mark.asyncio
async def test_cancelled_before_entry_and_failed_submission_do_not_leak_slot(monkeypatch):
    calls = []
    monkeypatch.setattr(
        fuel_card_parser,
        "run",
        lambda *_: calls.append("parsed") or fuel_card_parser.ParsedStatement(network="Synthetic"),
    )
    cancelled = asyncio.create_task(fuel_card_parser.run_async("cancelled.csv", b"cancelled"))
    cancelled.cancel()
    with suppress(asyncio.CancelledError):
        await cancelled
    assert calls == []
    rejected = []

    def failed_submission(coroutine):
        rejected.append(coroutine)
        raise RuntimeError("Synthetic task submission failure")

    with monkeypatch.context() as patch:
        patch.setattr(fuel_card_parser.asyncio, "create_task", failed_submission)
        with pytest.raises(RuntimeError, match="Synthetic task submission failure"):
            await fuel_card_parser.run_async("failed.csv", b"failed")
    assert inspect.getcoroutinestate(rejected[0]) == inspect.CORO_CLOSED
    assert calls == []
    assert (await fuel_card_parser.run_async("after.csv", b"after")).network == "Synthetic"
    assert calls == ["parsed"]


@pytest.mark.asyncio
async def test_cancelled_thread_submission_recovers_admission(monkeypatch):
    async def cancelled_submission(*_args):
        raise asyncio.CancelledError()

    with monkeypatch.context() as patch:
        patch.setattr(fuel_card_parser, "run_in_threadpool", cancelled_submission)
        with pytest.raises(asyncio.CancelledError):
            await fuel_card_parser.run_async("cancelled.csv", b"cancelled")
    monkeypatch.setattr(
        fuel_card_parser, "run", lambda *_: fuel_card_parser.ParsedStatement(network="Recovered")
    )
    assert (await fuel_card_parser.run_async("after.csv", b"after")).network == "Recovered"


@pytest.mark.asyncio
async def test_cancelled_queued_callable_cannot_parse_or_release_a_successor(monkeypatch):
    loop = asyncio.get_running_loop()
    submitted = asyncio.Event()
    entered = asyncio.Event()
    release = threading.Event()
    delayed = []
    calls = []
    submit = fuel_card_parser.run_in_threadpool

    async def delay_first(function, *args):
        if not delayed:
            delayed.append((function, args))
            submitted.set()
            await asyncio.Event().wait()
        return await submit(function, *args)

    def parse(filename, _content):
        calls.append(filename)
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5), "test must release its owned worker"
        return fuel_card_parser.ParsedStatement(network="Synthetic")

    monkeypatch.setattr(fuel_card_parser, "run_in_threadpool", delay_first)
    monkeypatch.setattr(fuel_card_parser, "run", parse)
    queued = asyncio.create_task(fuel_card_parser.run_async("cancelled.csv", b"cancelled"))
    await asyncio.wait_for(submitted.wait(), 5)
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    assert calls == []
    successor = asyncio.create_task(fuel_card_parser.run_async("successor.csv", b"successor"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        function, args = delayed[0]
        with pytest.raises(asyncio.CancelledError):
            function(*args)  # A stale native submission arrives after cancellation.
        assert calls == ["successor.csv"]
        with pytest.raises(fuel_card_parser.StatementReadingBusy):
            await fuel_card_parser.run_async("third.csv", b"not queued")
    finally:
        release.set()
        assert (await successor).network == "Synthetic"


async def _domain_state(db, org_id):
    result = {}
    for table in sorted(Base.metadata.tables.values(), key=lambda table: table.name):
        if "org_id" in table.c:
            result[table.name] = [
                dict(row)
                for row in (
                    await db.execute(
                        select(table)
                        .where(table.c.org_id == org_id)
                        .order_by(*table.primary_key.columns)
                    )
                ).mappings()
            ]
    return json.dumps(result, sort_keys=True, default=str)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["estimate", "statements"])
async def test_busy_http_fact_storage_outcomes_and_explicit_healthy_retry(
    auth_client, db_session, monkeypatch, kind
):
    identity = (await auth_client.get("/api/v1/auth/me")).json()
    org_id = identity["organization"]["id"]
    await modules.set_enabled(db_session, org_id, "transport", True)
    entity = await make_entity(db_session, org_id, documents=False, seed=108)
    await db_session.commit()
    content = synthetic_eurowag_statement(seed=108).encode()
    data = {"period": "2026-Q2" if kind == "estimate" else "2026-06"}
    if kind == "statements":
        data["entity_id"] = entity.id
    backend = storage.get_storage()
    assert isinstance(backend, storage.MemoryStorage)
    objects_before = dict(backend._data)
    state_before = await _domain_state(db_session, org_id)
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    original = fuel_card_parser.run
    worker_threads = []

    def parse(filename, source):
        worker_threads.append(threading.get_ident())
        if filename == "occupied.csv":
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5), "test must release its owned worker"
            return fuel_card_parser.ParsedStatement(network="Occupied")
        return original(filename, source)

    monkeypatch.setattr(fuel_card_parser, "run", parse)
    occupied = asyncio.create_task(fuel_card_parser.run_async("occupied.csv", b"occupied"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        response = await auth_client.post(
            f"/api/v1/transport/{kind}",
            data=data,
            files={"file": ("synthetic.csv", content, "text/csv")},
        )
        assert response.status_code == 503, response.text
        body = response.json()
        assert body["code"] == "statement_reading_busy" and body["retryable"] is True
        assert isinstance(body["detail"], str)
        assert "try again" in body["detail"] and "finish" in body["detail"]
        assert "retry-after" not in response.headers
        assert await _domain_state(db_session, org_id) == state_before
        if kind == "estimate":
            assert "original_stored" not in body
            assert backend._data == objects_before
        else:
            assert body["original_stored"] is True
            assert (
                "retained" in body["detail"] and "did not register transactions" in body["detail"]
            )
            key = storage.content_key(documents.STATEMENTS, org_id, storage.sha256_hex(content))
            assert backend._data == {**objects_before, key: content}
        assert len(worker_threads) == 1  # busy upload was never queued/parsed
    finally:
        release.set()
        await occupied
    response = await auth_client.post(
        f"/api/v1/transport/{kind}",
        data=data,
        files={"file": ("synthetic.csv", content, "text/csv")},
    )
    assert response.status_code == 200, response.text
    assert all(thread != threading.get_ident() for thread in worker_threads)
    if kind == "estimate":
        assert response.json()["lines"] == 1
        assert await _domain_state(db_session, org_id) == state_before
        assert backend._data == objects_before
    else:
        assert response.json()["lines_registered"] == 1
        # The retry reuses the exact retained object; normal registry/import/
        # audit work completes before the synchronous200 response is returned.
        assert backend._data == {**objects_before, key: content}
        fuel_before_replay = json.loads(await _domain_state(db_session, org_id))[
            "fuel_transactions"
        ]
        repeated = await auth_client.post(
            f"/api/v1/transport/{kind}",
            data=data,
            files={"file": ("synthetic.csv", content, "text/csv")},
        )
        # The existing response counts the registered source lines even on a
        # natural-key replay; pin that contract and prove no duplicated facts.
        assert repeated.status_code == 200 and repeated.json()["lines_registered"] == 1
        assert (
            json.loads(await _domain_state(db_session, org_id))["fuel_transactions"]
            == fuel_before_replay
        )
