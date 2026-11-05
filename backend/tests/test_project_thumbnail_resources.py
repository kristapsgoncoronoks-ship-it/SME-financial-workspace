"""An abandoned active preview must retain its resource slot until reaped."""

import asyncio
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest
from fastapi.concurrency import run_in_threadpool

from app.services import documents, filesec, project_profit, project_thumbnails


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["source", "render"])
@pytest.mark.parametrize("cancellations", [1, 3])
async def test_cancelling_active_decoder_does_not_admit_another_source(
    monkeypatch, phase, cancellations
):
    source_reads = []
    entered, release = threading.Event(), threading.Event()
    row = SimpleNamespace(kind="photo", sha256="1" * 64)

    async def get_document(*args):
        return row

    async def load_document(*args, **kwargs):
        source_reads.append(True)
        if phase == "source":
            await run_in_threadpool(hold)
        return row, b"synthetic verified source"

    def hold():
        entered.set()
        assert release.wait(5), "test did not release active decoder"

    def render(content):
        if phase == "render":
            hold()
        return b"synthetic derivative"

    class Database:
        async def scalar(self, query):
            return 25

    monkeypatch.setattr(project_profit, "get_document", get_document)
    monkeypatch.setattr(project_profit, "load_document", load_document)
    monkeypatch.setattr(project_thumbnails, "render", render)
    first = asyncio.create_task(project_thumbnails.prepare(Database(), "org", "project", "photo"))
    second = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        for _ in range(cancellations):
            first.cancel()
            await asyncio.sleep(0)
        # Give the cancellation callback a turn before requesting a second
        # photo. The first native decoder remains held by the real worker.
        await asyncio.sleep(0.02)
        second = asyncio.create_task(
            project_thumbnails.prepare(Database(), "org", "project", "photo")
        )
        queued = asyncio.create_task(
            project_thumbnails.prepare(Database(), "org", "project", "photo")
        )
        await asyncio.sleep(0)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        await asyncio.sleep(0.05)
        assert not first.done(), "Cancellation returned before the admitted worker was reaped"
        assert len(source_reads) == 1, (
            "A cancelled active decoder released source admission while still running"
        )
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        if second is not None:
            await second
    assert not project_thumbnails._locks


@pytest.mark.parametrize("outcome", ["malformed", "oversized", "failed"])
def test_child_output_refusal_closes_its_private_temporary_file(monkeypatch, outcome):
    handles = []
    create = project_thumbnails.tempfile.TemporaryFile

    def temporary(*args, **kwargs):
        handle = create(*args, **kwargs)
        handles.append(handle)
        return handle

    def child(arguments, **kwargs):
        if outcome == "failed":
            raise subprocess.CalledProcessError(1, arguments)
        data = (
            b"not a JPEG"
            if outcome == "malformed"
            else b"\xff\xd8\xff" + b"x" * filesec.RESOURCE_BYTES["project_thumbnail"]
        )
        kwargs["stdout"].write(data)

    monkeypatch.setattr(project_thumbnails.tempfile, "TemporaryFile", temporary)
    monkeypatch.setattr(project_thumbnails.subprocess, "run", child)
    with pytest.raises(project_thumbnails.AppError) as error:
        project_thumbnails.render(b"synthetic child-input fixture")
    assert error.value.status == 422
    assert len(handles) == 1 and handles[0].closed


def test_wall_timeout_reaps_actual_child_and_closes_output(monkeypatch):
    handles, children = [], []
    create = project_thumbnails.tempfile.TemporaryFile
    run, popen = subprocess.run, subprocess.Popen

    def temporary(*args, **kwargs):
        handle = create(*args, **kwargs)
        handles.append(handle)
        return handle

    def process(*args, **kwargs):
        child = popen(*args, **kwargs)
        children.append(child)
        return child

    def slow_child(arguments, **kwargs):
        return run([sys.executable, "-c", "import time; time.sleep(5)"], **kwargs)

    monkeypatch.setattr(project_thumbnails, "RENDER_SECONDS", 0.05)
    monkeypatch.setattr(project_thumbnails.tempfile, "TemporaryFile", temporary)
    monkeypatch.setattr(project_thumbnails.subprocess, "Popen", process)
    monkeypatch.setattr(project_thumbnails.subprocess, "run", slow_child)
    with pytest.raises(project_thumbnails.AppError):
        project_thumbnails.render(b"synthetic timeout fixture")
    assert len(children) == 1 and children[0].poll() is not None
    assert len(handles) == 1 and handles[0].closed


@pytest.mark.asyncio
async def test_registry_change_cannot_widen_preview_source_read(monkeypatch):
    row = SimpleNamespace(kind="photo", sha256="1" * 64)

    async def get_document(*args):
        return row

    async def forbidden(*args, **kwargs):
        pytest.fail("Changed registry metadata widened preview input before storage I/O")

    class Database:
        def __init__(self):
            self.calls = 0

        async def scalar(self, query):
            self.calls += 1
            return 25 if self.calls == 1 else filesec.RESOURCE_BYTES["project_thumbnail_input"] + 1

    monkeypatch.setattr(project_profit, "get_document", get_document)
    monkeypatch.setattr(documents, "load_verified_original", forbidden)
    with pytest.raises(documents.OriginalIntegrityError):
        await project_thumbnails.prepare(Database(), "org", "project", "photo")
    assert not project_thumbnails._locks
