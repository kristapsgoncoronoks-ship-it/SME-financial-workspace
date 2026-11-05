"""Worker liveness (`app.worker_probe`): the loop touches a heartbeat file, the
probe fails when it goes stale, and the two are wired as the container's
healthcheck / livenessProbe. Deliberately NOT the queue SLO — a backlog is a
fleet condition `/health/queue` pages on, not a reason to restart a worker."""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app import worker_probe
from app.models import job as jobmodel
from app.services import jobs

BACKEND = Path(__file__).resolve().parents[1]


# --- the probe itself --------------------------------------------------------


def test_probe_is_alive_only_while_the_heartbeat_is_fresh(tmp_path):
    hb = tmp_path / "hb"
    alive, reason = worker_probe.check(str(hb), 180, now=1000.0)
    assert alive is False and "no heartbeat file" in reason

    worker_probe.touch(str(hb))
    os.utime(hb, (1000.0, 1000.0))
    assert worker_probe.check(str(hb), 180, now=1100.0)[0] is True
    assert worker_probe.check(str(hb), 180, now=1180.0)[0] is True  # at the limit
    alive, reason = worker_probe.check(str(hb), 180, now=1181.0)
    assert alive is False and "wedged" in reason
    # An NTP step that puts the file in the future is age 0, not a failure.
    assert worker_probe.check(str(hb), 180, now=900.0)[0] is True


def test_touch_never_truncates_and_bumps_the_mtime(tmp_path):
    hb = tmp_path / "hb"
    hb.write_text("kept")
    os.utime(hb, (1.0, 1.0))
    worker_probe.touch(str(hb))
    assert hb.read_text() == "kept"
    assert hb.stat().st_mtime > 1.0


def test_probe_process_exit_codes_follow_the_env(tmp_path):
    """The container runs `python -m app.worker_probe`; exit 0 = alive."""
    hb = tmp_path / "hb"
    env = {**os.environ, "WORKER_LIVENESS_PATH": str(hb), "WORKER_LIVENESS_MAX_AGE_SECONDS": "5"}
    absent = subprocess.run(
        [sys.executable, "-m", "app.worker_probe"],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
    )
    assert absent.returncode == 1 and "no heartbeat file" in absent.stderr
    worker_probe.touch(str(hb))
    fresh = subprocess.run(
        [sys.executable, "-m", "app.worker_probe"],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
    )
    assert fresh.returncode == 0, fresh.stderr
    os.utime(hb, (1.0, 1.0))
    stale = subprocess.run(
        [sys.executable, "-m", "app.worker_probe"],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
    )
    assert stale.returncode == 1 and "wedged" in stale.stderr


def test_the_probe_imports_nothing_from_the_application():
    """It runs every minute in a second process inside the worker container:
    importing the app (models, handlers, the engine) for one stat() call would
    cost ~1 s and ~150 MB per probe. Guarded at the source level."""
    src = (BACKEND / "app" / "worker_probe.py").read_text()
    offending = [
        line
        for line in src.splitlines()
        if line.startswith(("from app", "import app", "from sqlalchemy", "import sqlalchemy"))
    ]
    assert offending == [], offending
    assert worker_probe.DEFAULT_PATH == "/tmp/invoiceiq-worker-liveness"  # noqa: S108


def test_defaults_agree_with_settings():
    from app.core.config import Settings

    fields = Settings.model_fields
    assert fields["worker_liveness_path"].default == worker_probe.DEFAULT_PATH
    assert fields["worker_liveness_max_age_seconds"].default == worker_probe.DEFAULT_MAX_AGE_SECONDS
    # A live worker's file is never older than one lease heartbeat plus a poll;
    # the limit must clear that with room for a slow tick, or a healthy worker
    # would be restarted mid-job.
    assert worker_probe.DEFAULT_MAX_AGE_SECONDS > 2 * jobs.LEASE_HEARTBEAT_SECONDS


# --- the worker touches it ---------------------------------------------------


@pytest.mark.asyncio
async def test_the_loop_touches_the_heartbeat_every_tick_even_when_the_database_is_down(
    tmp_path, monkeypatch
):
    """Liveness is 'the loop turns', not 'the database answers': a DB outage is
    the readiness signal's job, and restarting the worker would not fix it."""
    from app import worker
    from app.core.config import settings

    hb = tmp_path / "hb"
    monkeypatch.setattr(settings, "worker_liveness_path", str(hb))

    class _Down:
        async def __aenter__(self):
            raise ConnectionError("db down")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(worker, "SessionLocal", lambda: _Down())
    task = asyncio.create_task(worker.run_forever(0.01))
    try:
        for _ in range(200):
            if hb.exists():
                break
            await asyncio.sleep(0.01)
        assert hb.exists(), "the loop never touched its heartbeat"
        first = hb.stat().st_mtime_ns
        for _ in range(200):
            await asyncio.sleep(0.01)
            if hb.stat().st_mtime_ns > first:
                break
        assert hb.stat().st_mtime_ns > first, "the heartbeat was not touched again on a later tick"
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_run_once_reports_liveness_on_every_lease_heartbeat_during_a_long_job(
    auth_client, db_session, monkeypatch
):
    """A multi-page OCR runs longer than the probe's limit; the lease heartbeat
    (every 60 s) is the loop turning, so it also carries the liveness tick."""
    from sqlalchemy import select

    from app.models.organization import Organization

    org = await db_session.scalar(select(Organization.id).limit(1))
    ticks: list[int] = []
    release = asyncio.Event()

    @jobs.handler("test.long.live")
    async def _long(db, payload, job):
        await release.wait()
        return {"ok": True}

    async def _fake_renew(lease):
        return True

    monkeypatch.setattr(jobs, "_renew_lease", _fake_renew)
    monkeypatch.setattr(jobs, "LEASE_HEARTBEAT_SECONDS", 0.01)
    saved = dict(jobs._HANDLERS)
    try:
        await jobs.enqueue(db_session, "test.long.live", {}, org_id=org)
        task = asyncio.create_task(
            jobs.run_once(db_session, "live-worker", on_liveness_tick=lambda: ticks.append(1))
        )
        for _ in range(200):
            if len(ticks) >= 3:
                break
            await asyncio.sleep(0.01)
        release.set()
        job = await task
        assert job is not None and job.status == jobmodel.SUCCEEDED
        assert len(ticks) >= 3
    finally:
        jobs._HANDLERS.clear()
        jobs._HANDLERS.update(saved)


@pytest.mark.asyncio
async def test_a_failing_liveness_tick_never_fails_the_job(auth_client, db_session, monkeypatch):
    from sqlalchemy import select

    from app.models.organization import Organization

    org = await db_session.scalar(select(Organization.id).limit(1))
    release = asyncio.Event()
    calls: list[int] = []

    @jobs.handler("test.long.badtick")
    async def _long(db, payload, job):
        await release.wait()
        return {"ok": True}

    async def _fake_renew(lease):
        return True

    def _boom():
        calls.append(1)
        raise OSError("read-only file system")

    monkeypatch.setattr(jobs, "_renew_lease", _fake_renew)
    monkeypatch.setattr(jobs, "LEASE_HEARTBEAT_SECONDS", 0.01)
    saved = dict(jobs._HANDLERS)
    try:
        await jobs.enqueue(db_session, "test.long.badtick", {}, org_id=org)
        task = asyncio.create_task(jobs.run_once(db_session, "w", on_liveness_tick=_boom))
        for _ in range(200):
            if len(calls) >= 2:
                break
            await asyncio.sleep(0.01)
        release.set()
        job = await task
        assert job is not None and job.status == jobmodel.SUCCEEDED
        assert len(calls) >= 2  # the loop kept ticking past the failure
    finally:
        jobs._HANDLERS.clear()
        jobs._HANDLERS.update(saved)


@pytest.mark.asyncio
async def test_a_handler_that_awaits_forever_keeps_the_probe_green_by_design(
    auth_client, db_session, monkeypatch, tmp_path
):
    """The honest boundary (R6 review QA-2/A-5): the lease heartbeat keeps
    ticking on a live handler's behalf — a slow job must not be reclaimed —
    so a handler that awaits forever is NOT a liveness failure, and this test
    pins that it stays that way. What catches it instead is the per-job
    deadline (BE-024): the probe answers "is the loop alive", the deadline
    answers "is this job still worth waiting for", and they are different
    questions. This handler is released well inside the deadline, so what is
    asserted here remains purely the probe's behaviour."""
    from sqlalchemy import select

    from app.models.organization import Organization

    org = await db_session.scalar(select(Organization.id).limit(1))
    hb = tmp_path / "hb"
    release = asyncio.Event()

    @jobs.handler("test.forever")
    async def _forever(db, payload, job):
        await release.wait()
        return {"ok": True}

    async def _fake_renew(lease):
        return True

    monkeypatch.setattr(jobs, "_renew_lease", _fake_renew)
    monkeypatch.setattr(jobs, "LEASE_HEARTBEAT_SECONDS", 0.01)
    saved = dict(jobs._HANDLERS)
    try:
        await jobs.enqueue(db_session, "test.forever", {}, org_id=org)
        task = asyncio.create_task(
            jobs.run_once(db_session, "w", on_liveness_tick=lambda: worker_probe.touch(str(hb)))
        )
        await asyncio.sleep(0.2)
        assert worker_probe.check(str(hb), 0.15)[0] is True, "the file must stay fresh"
        release.set()
        job = await task
        assert job is not None and job.status == jobmodel.SUCCEEDED
    finally:
        jobs._HANDLERS.clear()
        jobs._HANDLERS.update(saved)
