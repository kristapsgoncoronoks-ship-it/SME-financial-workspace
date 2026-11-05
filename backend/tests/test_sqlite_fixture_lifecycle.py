"""Owned SQLite fixture cleanup, without attributing the historical SYS-07 warning."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]

# Run each lifecycle in its own interpreter/loop. The probe always disposes and
# joins its explicitly owned connections in finally, including on the old buggy
# fixture, so a failed regression never leaves a process waiting for its worker.
PROBE = r"""
import asyncio
import json
import sys
import threading

from sqlalchemy import event, inspect, text

from app.services import fx
from tests import conftest

mode = sys.argv[1]
thread_errors = []
threading.excepthook = lambda args: thread_errors.append(
    {'type': args.exc_type.__name__, 'message': str(args.exc_value)}
)


async def run():
    original_factory = conftest.create_async_engine
    original_schema = conftest.Base.metadata.create_all
    original_seed = fx.ensure_seed_rates
    engines, drivers, closed = [], [], []
    seed_entered = asyncio.Event()
    native_entered, close_entered = asyncio.Event(), asyncio.Event()
    native_release = threading.Event()
    loop = asyncio.get_running_loop()
    outcome, task = None, None
    work, teardown = None, None
    normal, disposal = {}, {}

    def factory(*args, **kwargs):
        engine = original_factory(*args, **kwargs)
        engines.append(engine)

        @event.listens_for(engine.sync_engine, 'connect')
        def connected(connection, record):
            drivers.append(connection.driver_connection)

        @event.listens_for(engine.sync_engine, 'close')
        def closing(connection, record):
            closed.append(True)
            close_entered.set()

        return engine

    def schema_failure(*args, **kwargs):
        raise RuntimeError('Synthetic schema initialization failure')

    async def seed_failure(*args, **kwargs):
        raise RuntimeError('Synthetic FX initialization failure')

    async def blocked_seed(*args, **kwargs):
        seed_entered.set()
        await asyncio.Event().wait()

    def native_work():
        loop.call_soon_threadsafe(native_entered.set)
        if not native_release.wait(10):
            raise RuntimeError('Owned SQLite worker barrier timed out')

    conftest.create_async_engine = factory
    if mode == 'schema_failure':
        conftest.Base.metadata.create_all = schema_failure
    elif mode == 'seed_failure':
        fx.ensure_seed_rates = seed_failure
    elif mode == 'cancel_during_seed':
        fx.ensure_seed_rates = blocked_seed
    fixture = conftest._db.__wrapped__()
    try:
        if mode == 'cancel_during_seed':
            task = asyncio.create_task(anext(fixture))
            await asyncio.wait_for(seed_entered.wait(), 5)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                outcome = 'CancelledError'
        elif mode in {'schema_failure', 'seed_failure'}:
            try:
                await anext(fixture)
            except RuntimeError as exc:
                outcome = str(exc)
        else:
            sessions = await anext(fixture)
            if mode == 'normal':
                async with sessions() as db:
                    connection = await db.connection()
                    tables = await connection.run_sync(
                        lambda sync: inspect(sync).get_table_names()
                    )
                    normal = {
                        'foreign_keys': await db.scalar(text('PRAGMA foreign_keys')),
                        'schema_complete': set(tables) == set(conftest.Base.metadata.tables),
                        'seed_rows': await db.scalar(text('SELECT count(*) FROM ecb_rates')),
                        'latest_seed_date': await db.scalar(
                            text('SELECT max(rate_date) FROM ecb_rates')
                        ),
                    }
                try:
                    await anext(fixture)
                except StopAsyncIteration:
                    outcome = 'StopAsyncIteration'
            elif mode.startswith('dispose_'):
                # Hold a real operation on this driver's native queue. The
                # observed SQLAlchemy close then waits behind that operation.
                work = asyncio.create_task(drivers[0]._execute(native_work))
                await asyncio.wait_for(native_entered.wait(), 5)
                teardown = asyncio.create_task(fixture.aclose())
                await asyncio.wait_for(close_entered.wait(), 5)
                teardown.cancel('original fixture disposal cancellation')
                # Cooperative loop turns deliver cancellation, without a timed
                # sleep or releasing the actual native-work barrier.
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                if mode == 'dispose_repeated_cancel':
                    teardown.cancel('repeated disposal cancellation')
                    await asyncio.sleep(0)
                    await asyncio.sleep(0)
                disposal = {
                    'actual_close_event_observed': close_entered.is_set(),
                    'caller_pending_before_native_release': not teardown.done(),
                    'owned_worker_alive_before_native_release': drivers[0]._thread.is_alive(),
                }
                native_release.set()
                await work
                try:
                    await teardown
                except asyncio.CancelledError as exc:
                    outcome = 'CancelledError'
                    disposal['cancellation_args'] = list(exc.args)
                else:
                    outcome = 'returned normally'
        await fixture.aclose()
        # The driver is pinned by requirements; inspect/join only this probe's
        # actual owner thread. Joining does not close an undisposed connection.
        for driver in drivers:
            await asyncio.to_thread(driver._thread.join, 0.25)
        observed = {
            'mode': mode,
            'outcome': outcome,
            'connections': len(drivers),
            'fixture_close_events': len(closed),
            'closed_before_probe_cleanup': all(d._connection is None for d in drivers),
            'workers_joined_before_probe_cleanup': all(
                not d._thread.is_alive() for d in drivers
            ),
            'normal': normal,
            'disposal': disposal,
        }
    finally:
        native_release.set()
        conftest.create_async_engine = original_factory
        conftest.Base.metadata.create_all = original_schema
        fx.ensure_seed_rates = original_seed
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if work is not None:
            await asyncio.gather(work, return_exceptions=True)
        if teardown is not None:
            await asyncio.gather(teardown, return_exceptions=True)
        await fixture.aclose()
        for engine in engines:
            await engine.dispose()
        for driver in drivers:
            await asyncio.to_thread(driver._thread.join, 1)
        assert all(d._connection is None for d in drivers)
        assert all(not d._thread.is_alive() for d in drivers)
    return observed


observed = asyncio.run(run())
observed['thread_errors_through_loop_close'] = thread_errors
print(json.dumps(observed))
"""


@pytest.mark.parametrize(
    "mode,outcome",
    [
        ("normal", "StopAsyncIteration"),
        ("schema_failure", "Synthetic schema initialization failure"),
        ("seed_failure", "Synthetic FX initialization failure"),
        ("early_close", None),
        ("cancel_during_seed", "CancelledError"),
        ("dispose_single_cancel", "CancelledError"),
        ("dispose_repeated_cancel", "CancelledError"),
    ],
)
def test_fixture_disposes_actual_sqlite_connection_before_loop_close(mode, outcome):
    result = subprocess.run(
        [sys.executable, "-c", PROBE, mode],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed["outcome"] == outcome, observed
    assert observed["connections"] == 1, observed
    assert observed["fixture_close_events"] == 1, observed
    assert observed["closed_before_probe_cleanup"] is True, observed
    assert observed["workers_joined_before_probe_cleanup"] is True, observed
    assert observed["thread_errors_through_loop_close"] == [], observed
    assert "Event loop is closed" not in result.stderr
    assert "Exception in thread" not in result.stderr
    if mode.startswith("dispose_"):
        assert observed["disposal"]["actual_close_event_observed"] is True
        assert observed["disposal"]["caller_pending_before_native_release"] is True, observed
        assert observed["disposal"]["owned_worker_alive_before_native_release"] is True
        assert observed["disposal"]["cancellation_args"] == [
            "original fixture disposal cancellation"
        ], observed
    if mode == "normal":
        assert observed["normal"]["foreign_keys"] == 1
        assert observed["normal"]["schema_complete"] is True
        assert observed["normal"]["seed_rows"] > 0
        assert observed["normal"]["latest_seed_date"] == "2026-07-01"
