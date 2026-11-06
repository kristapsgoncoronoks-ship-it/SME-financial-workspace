"""Shared native admission, safe refusal and inherited process ownership."""

import asyncio
import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from app.models.extraction_run import ExtractionRun
from app.schemas.image_reading import ImageReadingPreparation
from app.services import capture_failures, documents, extraction, image_ocr


@pytest.fixture(autouse=True)
def isolated_slot(monkeypatch, tmp_path):
    monkeypatch.setattr(image_ocr, "_SLOT_DIRECTORY", tmp_path / "shared-admission")


def reading():
    return image_ocr.ImageReading(
        "Observed receipt",
        ImageReadingPreparation(source_width=1, source_height=1, raster_width=1, raster_height=1),
    )


def png():
    output = io.BytesIO()
    with Image.new("RGB", (1, 1), "white") as image:
        image.save(output, format="PNG")
    return output.getvalue()


def assert_busy():
    with pytest.raises(image_ocr.ImageOcrError) as error:
        image_ocr.read(png())
    assert error.value.code == capture_failures.IMAGE_READING_BUSY
    assert error.value.reading_preparation is None
    assert "then try again" in str(error.value)


def test_same_process_contention_is_refused_before_reading_and_keeps_stable_inode(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def hold(content):
        calls.append(content)
        entered.set()
        assert release.wait(5)
        return reading()

    monkeypatch.setattr(image_ocr, "_read", hold)
    result = []
    first = threading.Thread(target=lambda: result.append(image_ocr.read(png())))
    first.start()
    try:
        assert entered.wait(5)
        before = (image_ocr._SLOT_DIRECTORY / "native.lock").stat()
        assert_busy()
        assert len(calls) == 1
    finally:
        release.set()
        first.join(5)
    assert not first.is_alive() and result == [reading()]
    assert image_ocr.read(png()) == reading()
    after = (image_ocr._SLOT_DIRECTORY / "native.lock").stat()
    assert (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)


def test_cross_process_contention_and_release(monkeypatch):
    program = (
        "import sys\nfrom pathlib import Path\nfrom app.services import image_ocr\n"
        "image_ocr._SLOT_DIRECTORY=Path(sys.argv[1])\n"
        "with image_ocr._native_slot():\n print('held',flush=True)\n sys.stdin.readline()\n"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", program, str(image_ocr._SLOT_DIRECTORY)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        monkeypatch.setattr(image_ocr, "_read", lambda content: reading())
        assert_busy()
        holder.communicate("release\n", timeout=10)
        assert holder.returncode == 0
        assert image_ocr.read(png()) == reading()
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(10)


@pytest.mark.parametrize("failure", [ValueError("unreadable"), OSError("private-path")])
def test_read_errors_release_admission_without_unlinking(monkeypatch, failure):
    def refuse(content):
        raise failure

    monkeypatch.setattr(image_ocr, "_read", refuse)
    with pytest.raises((ValueError, image_ocr.ImageOcrError)):
        image_ocr.read(png())
    before = (image_ocr._SLOT_DIRECTORY / "native.lock").stat()
    monkeypatch.setattr(image_ocr, "_read", lambda content: reading())
    assert image_ocr.read(png()) == reading()
    after = (image_ocr._SLOT_DIRECTORY / "native.lock").stat()
    assert after.st_ino == before.st_ino


def test_independent_namespace_and_original_byte_refusal(monkeypatch, tmp_path):
    monkeypatch.setattr(image_ocr, "_read", lambda content: reading())
    with image_ocr._native_slot():
        assert_busy()
        monkeypatch.setattr("app.services.filesec.max_bytes", lambda: 1)
        with pytest.raises(ValueError, match="large"):
            image_ocr.read(b"oversize")
        monkeypatch.setattr("app.services.filesec.max_bytes", lambda: 1024)
        monkeypatch.setattr(image_ocr, "_SLOT_DIRECTORY", tmp_path / "independent")
        assert image_ocr.read(png()) == reading()


@pytest.mark.parametrize(
    "changed", [None, [], {"pass_fds": (123,)}, {"close_fds": False}, {"close_fds": None}]
)
def test_changed_engine_spawn_adapter_fails_closed_without_broadening_fds(changed):
    def original():
        return changed

    engine = SimpleNamespace(subprocess_args=original)
    with image_ocr._native_slot(), pytest.raises(image_ocr.ImageOcrError) as error:
        with image_ocr._engine_slot(SimpleNamespace(pytesseract=engine)):
            engine.subprocess_args()
    assert error.value.code == capture_failures.CAPTURE_UNAVAILABLE
    assert engine.subprocess_args is original


@pytest.mark.parametrize(
    "unsafe",
    [
        "directory-symlink",
        "directory-mode",
        "file-symlink",
        "file-mode",
        "file-hardlink",
        "file-fifo",
        "foreign-owner",
        "inode-replaced",
    ],
)
def test_unsafe_namespace_is_sanitized_fail_closed_without_removing_entries(
    monkeypatch, tmp_path, unsafe
):
    root = image_ocr._SLOT_DIRECTORY
    foreign = tmp_path / "private-original"
    foreign.write_text("unchanged")
    foreign.chmod(0o600)
    if unsafe == "directory-symlink":
        target = tmp_path / "foreign-directory"
        target.mkdir(mode=0o700)
        root.symlink_to(target, target_is_directory=True)
    else:
        root.mkdir(mode=0o700)
        lock = root / "native.lock"
        if unsafe == "directory-mode":
            root.chmod(0o755)
        elif unsafe == "file-symlink":
            lock.symlink_to(foreign)
        elif unsafe == "file-hardlink":
            os.link(foreign, lock)
        elif unsafe == "file-fifo":
            os.mkfifo(lock, 0o600)
        else:
            lock.write_text("")
            lock.chmod(0o644 if unsafe == "file-mode" else 0o600)
        if unsafe == "foreign-owner":
            monkeypatch.setattr(image_ocr.os, "geteuid", lambda: root.stat().st_uid + 1)
        if unsafe == "inode-replaced":
            original_stat = os.stat

            def replaced(path, *args, **kwargs):
                if path == "native.lock" and "dir_fd" in kwargs:
                    return original_stat(foreign)
                return original_stat(path, *args, **kwargs)

            monkeypatch.setattr(image_ocr.os, "stat", replaced)

    def forbidden(content):
        raise AssertionError("Unsafe admission reached decoder")

    monkeypatch.setattr(image_ocr, "_read", forbidden)
    with pytest.raises(image_ocr.ImageOcrError) as error:
        image_ocr.read(png())
    assert error.value.code == capture_failures.CAPTURE_UNAVAILABLE
    assert "private-original" not in str(error.value)
    assert root.exists() and foreign.read_text() == "unchanged"


def test_native_helper_inherits_only_lease_descriptor(monkeypatch, tmp_path):
    real = subprocess.Popen
    unrelated = tmp_path / "unrelated-private-file"
    unrelated.write_text("unrelated")
    with unrelated.open() as foreign:
        os.set_inheritable(foreign.fileno(), True)

        def spawn(command, **kwargs):
            descriptor = int(kwargs["env"][image_ocr._SLOT_FD_ENV])
            assert kwargs["pass_fds"] == (descriptor,)
            assert kwargs.get("close_fds", True)
            program = (
                "import json,os\nfrom pathlib import Path\n"
                f"assert os.readlink('/proc/self/fd/{descriptor}').endswith('/native.lock')\n"
                f"assert not any(p.exists() and p.resolve()==Path({str(unrelated)!r}) for p in Path('/proc/self/fd').iterdir())\n"
                "print(json.dumps({'preparation':{'source_width':1,'source_height':1,'raster_width':1,'raster_height':1}}))\n"
                "print(json.dumps({'text':'Observed receipt'}))\n"
            )
            return real([sys.executable, "-c", program], **kwargs)

        monkeypatch.setattr(image_ocr.subprocess, "Popen", spawn)
        assert image_ocr.read(png()) == reading()


def test_worker_and_helper_death_do_not_release_a_surviving_engine_lease(monkeypatch, tmp_path):
    marker, release = tmp_path / "engine.json", tmp_path / "release-engine"
    engine = (
        "import json,os,sys,time\nfrom pathlib import Path\n"
        "fd=int(sys.argv[1])\n"
        "Path(sys.argv[2]).write_text(json.dumps({'engine':os.getpid(),'helper':os.getppid(),'lease':os.readlink('/proc/self/fd/'+str(fd))}))\n"
        "while not Path(sys.argv[3]).exists():time.sleep(.01)\n"
    )
    helper = (
        "import subprocess,sys,time\nimport pytesseract\nfrom app.services import image_ocr\n"
        "fd=int(sys.argv[1]);image_ocr._slot_fd.set(fd)\n"
        "with image_ocr._engine_slot(pytesseract):\n"
        f" child=subprocess.Popen([sys.executable,'-c',{engine!r},str(fd),sys.argv[2],sys.argv[3]],**pytesseract.pytesseract.subprocess_args())\n"
        " child.wait()\n"
    )
    controller = (
        "import subprocess,sys\nfrom pathlib import Path\nfrom app.services import image_ocr\n"
        "image_ocr._SLOT_DIRECTORY=Path(sys.argv[1])\n"
        "with image_ocr._native_slot() as fd:\n"
        f" child=subprocess.Popen([sys.executable,'-c',{helper!r},str(fd),sys.argv[2],sys.argv[3]],pass_fds=(fd,))\n"
        " sys.stdin.readline()\n"
    )
    owner = subprocess.Popen(
        [
            sys.executable,
            "-c",
            controller,
            str(image_ocr._SLOT_DIRECTORY),
            str(marker),
            str(release),
        ],
        stdin=subprocess.PIPE,
    )
    identity = None
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and owner.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists()
        identity = json.loads(marker.read_text())
        assert identity["lease"].endswith("/native.lock")
        owner.kill()
        owner.wait(10)
        monkeypatch.setattr(image_ocr, "_read", lambda content: reading())
        assert_busy()
        os.kill(identity["helper"], signal.SIGKILL)
        assert_busy()
        release.touch()
        deadline = time.monotonic() + 5
        while True:
            try:
                assert image_ocr.read(png()) == reading()
                break
            except image_ocr.ImageOcrError as error:
                assert (
                    error.code == capture_failures.IMAGE_READING_BUSY
                    and time.monotonic() < deadline
                )
                time.sleep(0.01)
    finally:
        release.touch()
        if owner.poll() is None:
            owner.kill()
            owner.wait(10)
        if identity:
            for pid in [identity["helper"], identity["engine"]]:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


@pytest.mark.asyncio
async def test_repeated_cancellation_keeps_slot_through_owned_cleanup(monkeypatch, tmp_path):
    real, stop = subprocess.Popen, image_ocr._stop
    entered, stopped, release = threading.Event(), threading.Event(), threading.Event()
    children = []

    def spawn(command, **kwargs):
        process = real([sys.executable, "-c", "import time;time.sleep(10)"], **kwargs)
        children.append((process, Path(kwargs["cwd"])))
        entered.set()
        return process

    def cleanup(process):
        stop(process)
        stopped.set()
        assert release.wait(5)

    monkeypatch.setattr(image_ocr.subprocess, "Popen", spawn)
    monkeypatch.setattr(image_ocr, "_stop", cleanup)
    task = asyncio.create_task(image_ocr.run_owned(image_ocr.read, png()))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        assert await asyncio.to_thread(stopped.wait, 5)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
        assert not task.done()
        assert_busy()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert all(
        process.poll() is not None and not directory.exists() for process, directory in children
    )
    monkeypatch.setattr(image_ocr, "_read", lambda content: reading())
    assert image_ocr.read(png()) == reading()


@pytest.mark.asyncio
async def test_busy_receipt_is_typed_recoverable_without_suggestions_or_geometry(
    auth_client, monkeypatch
):
    assert (
        await auth_client.put("/api/v1/modules/expenses", json={"enabled": True})
    ).status_code == 200

    def busy(filename, content):
        raise image_ocr.ImageOcrError(capture_failures.IMAGE_READING_BUSY)

    monkeypatch.setattr("app.services.receipt_ocr.suggest", busy)
    response = await auth_client.post(
        "/api/v1/expenses/receipt-scan", files={"file": ("receipt.png", png(), "image/png")}
    )
    assert response.status_code == 503
    assert response.json()["code"] == capture_failures.IMAGE_READING_BUSY
    assert "then try again" in response.json()["detail"]
    assert not {"reading_preparation", "amount", "method", "warnings"} & response.json().keys()
    assert response.headers["cache-control"] == "private, no-store"
    error = image_ocr.ImageOcrError(capture_failures.IMAGE_READING_BUSY)
    assert capture_failures.code_for(error) == capture_failures.IMAGE_READING_BUSY
    kind = capture_failures.kind_for(error.code)
    assert kind.retry_helps and kind.user_fixable and "original is still stored" in kind.remediation


@pytest.mark.asyncio
async def test_queued_image_busy_retains_original_and_readable_retryable_failure_without_draft(
    auth_client, db_session, monkeypatch
):
    content = png()
    admitted = await auth_client.post(
        "/api/v1/invoices/upload", files={"file": ("busy.png", content, "image/png")}
    )
    assert admitted.status_code == 202
    run_id = admitted.json()["extraction_run_id"]

    def forbidden(content):
        raise AssertionError("Contended capture must not start native reading")

    monkeypatch.setattr(image_ocr, "_read", forbidden)
    with image_ocr._native_slot():
        await extraction.extract_upload(db_session, run_id)
    row = await db_session.get(ExtractionRun, run_id)
    assert row.failure_code == capture_failures.IMAGE_READING_BUSY
    assert (
        row.status == "failed" and row.draft_json is None and row.reading_preparation_json is None
    )
    result = await auth_client.get(f"/api/v1/invoices/upload/{run_id}")
    assert result.status_code == 200
    assert result.json()["status"] == "failed"
    assert result.json()["draft"] is None and result.json()["reading_preparation"] is None
    failures = await auth_client.get("/api/v1/invoices/captures/failures")
    assert failures.status_code == 200
    item = next(item for item in failures.json()["items"] if item["ref_id"] == run_id)
    assert item["code"] == capture_failures.IMAGE_READING_BUSY
    assert item["retry_helps"] and item["document_retained"]
    assert await documents.load(documents.UPLOADS, row.org_id, row.source_sha256) == content
