"""Metrics diagnostics are bounded, contain no exception data, and preserve work."""

from __future__ import annotations

import asyncio
import signal

import pytest

from app import worker
from app.services import queue_health

SECRET = "METRICS_PRIVATE https://user:password@db.invalid/private SELECT tenant_payload"


def wire_worker(monkeypatch, factory):
    signals, liveness = {}, []
    monkeypatch.setattr(worker, "SessionLocal", factory)
    monkeypatch.setattr(worker, "_touch_liveness", lambda: liveness.append(1))
    monkeypatch.setattr(
        asyncio.get_running_loop(),
        "add_signal_handler",
        lambda sig, fn: signals.setdefault(sig, fn),
    )
    return signals, liveness


async def no_jobs(*args, **kwargs):
    return 0


class Session:
    def __init__(self, index, rollbacks, closes, *, rollback=None):
        self.index, self.rollbacks, self.closes, self.rollback_hook = (
            index,
            rollbacks,
            closes,
            rollback,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closes.append(self.index)

    async def rollback(self):
        self.rollbacks.append(self.index)
        if self.rollback_hook is not None:
            await self.rollback_hook()


async def sequence(monkeypatch, modes, *, rollback_hook=None):
    rollbacks, closes, sessions, dispatch = [], [], [], []

    def factory():
        row = Session(len(sessions) + 1, rollbacks, closes, rollback=rollback_hook)
        sessions.append(row)
        return row

    signals, liveness = wire_worker(monkeypatch, factory)
    monkeypatch.setattr(worker.jobs, "reclaim_stale", no_jobs)
    monkeypatch.setattr(worker.scheduler, "enqueue_daily", no_jobs)
    calls = []

    async def snapshot(db):
        mode = modes[len(calls)]
        calls.append(mode)
        if mode == "failure":
            # A class name is user-controlled too: neither it nor __str__ may
            # become part of an operational diagnostic.
            hostile = type("METRICS_PRIVATE_CLASS_" + "x" * 1000, (RuntimeError,), {})
            raise hostile(SECRET)

    async def run_once(db, worker_id, **kwargs):
        dispatch.append(db.index)
        assert kwargs["kinds"] == ("test.lane",)
        assert kwargs["exclude"] == ("test.other",)
        assert kwargs["on_liveness_tick"] is worker._touch_liveness
        if len(calls) == len(modes):
            signals[signal.SIGTERM]()
        return None

    monkeypatch.setattr(queue_health, "snapshot", snapshot)
    monkeypatch.setattr(worker.jobs, "run_once", run_once)
    await asyncio.wait_for(
        worker.run_forever(0, kinds=("test.lane",), exclude=("test.other",)), timeout=5
    )
    return rollbacks, closes, dispatch, liveness


@pytest.mark.asyncio
async def test_metric_failures_log_once_per_episode_and_recover_without_disrupting_dispatch(
    monkeypatch, caplog
):
    caplog.set_level("INFO", logger="invoiceiq.worker")
    modes = ["failure"] * 3 + ["healthy"] + ["failure"] * 2 + ["healthy"]
    rollbacks, closes, dispatch, liveness = await sequence(monkeypatch, modes)
    assert rollbacks == [1, 2, 3, 5, 6]
    assert closes == dispatch == list(range(1, 8))
    assert len(liveness) == 7
    warnings = [
        r for r in caplog.records if r.name == "invoiceiq.worker" and r.levelname == "WARNING"
    ]
    recovered = [
        r for r in caplog.records if r.getMessage() == "queue health metrics snapshot recovered"
    ]
    assert len(warnings) == len(recovered) == 2
    assert all(
        r.getMessage() == "queue health metrics snapshot failed; recovering transaction"
        and not r.exc_info
        and not r.args
        for r in warnings
    )
    assert "METRICS_PRIVATE" not in caplog.text and "password" not in caplog.text


@pytest.mark.asyncio
async def test_failed_metrics_rollback_discards_that_session_before_dispatch(monkeypatch, caplog):
    async def broken_rollback():
        raise RuntimeError(SECRET)

    rollbacks, closes, dispatch, _liveness = await sequence(
        monkeypatch, ["failure", "healthy"], rollback_hook=broken_rollback
    )
    assert rollbacks == [1] and closes == [1, 2] and dispatch == [2]
    assert "METRICS_PRIVATE" not in caplog.text and "password" not in caplog.text
    assert "worker loop error" not in caplog.text
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1


@pytest.mark.asyncio
async def test_healthy_metrics_do_not_rollback_or_emit_failure_diagnostics(monkeypatch, caplog):
    rollbacks, closes, dispatch, _liveness = await sequence(monkeypatch, ["healthy", "healthy"])
    assert not rollbacks and closes == dispatch == [1, 2]
    assert not [r for r in caplog.records if r.levelname in {"WARNING", "ERROR"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("during", ["snapshot", "rollback"])
async def test_actual_cancellation_propagates_through_metrics_and_cleanup(
    monkeypatch, caplog, during
):
    entered, closes, rollbacks = asyncio.Event(), [], []

    async def rollback():
        entered.set()
        await asyncio.Event().wait()

    row = Session(1, rollbacks, closes, rollback=rollback)
    wire_worker(monkeypatch, lambda: row)
    monkeypatch.setattr(worker.jobs, "reclaim_stale", no_jobs)
    monkeypatch.setattr(worker.scheduler, "enqueue_daily", no_jobs)

    async def snapshot(db):
        if during == "rollback":
            raise RuntimeError(SECRET)
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(queue_health, "snapshot", snapshot)
    task = asyncio.create_task(worker.run_forever(0))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closes == [1]
        assert rollbacks == ([1] if during == "rollback" else [])
        assert "METRICS_PRIVATE" not in caplog.text
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
