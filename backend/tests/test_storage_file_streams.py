"""Bounded file APIs must publish only complete content-addressed originals."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest

from app.core import storage
from app.services import documents

BLOCK = bytes(range(256)) * 257
PAYLOAD = BLOCK * 33 + b"synthetic final tail"


class GuardedSource:
    def __init__(self, source, *, fault_after=None):
        self.source, self.fault_after = source, fault_after
        self.requests = []
        self.consumed = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.source.close()

    def read(self, size=-1):
        self.requests.append(size)
        assert 0 < size <= 65536, "File streaming issued an unbounded/oversized source read"
        if self.fault_after is not None and self.consumed >= self.fault_after:
            raise OSError("Synthetic private source read failure")
        data = self.source.read(size)
        self.consumed += len(data)
        return data

    def close(self):
        self.source.close()


class MultipartStore:
    def __init__(self):
        self.objects, self.uploads = {}, {}
        self.parts, self.aborted = [], []
        self.fail = None
        self.completed = []

    def create_multipart_upload(self, **kwargs):
        ident = str(len(self.uploads) + len(self.aborted) + len(self.completed) + 1)
        self.uploads[ident] = (kwargs["Key"], {})
        return {"UploadId": ident}

    def upload_part(self, **kwargs):
        if self.fail == "part":
            raise OSError("Synthetic private provider write failure")
        body = kwargs["Body"]
        assert isinstance(body, bytes) and len(body) <= 8 * 1024 * 1024
        self.parts.append(len(body))
        self.uploads[kwargs["UploadId"]][1][kwargs["PartNumber"]] = body
        return {"ETag": f"part-{kwargs['PartNumber']}"}

    def complete_multipart_upload(self, **kwargs):
        if self.fail == "complete":
            raise OSError("Synthetic private completion failure")
        key, parts = self.uploads.pop(kwargs["UploadId"])
        self.objects[key] = b"".join(parts[i] for i in sorted(parts))
        self.completed.append(kwargs["UploadId"])

    def abort_multipart_upload(self, **kwargs):
        self.aborted.append(kwargs["UploadId"])
        self.uploads.pop(kwargs["UploadId"], None)

    def put_object(self, **kwargs):
        self.objects[kwargs["Key"]] = kwargs["Body"]

    def get_object(self, **kwargs):
        return {"Body": io.BytesIO(self.objects[kwargs["Key"]]), "ContentLength": 1}


def backend(name, tmp_path):
    if name == "memory":
        return storage.MemoryStorage(), None
    if name == "local":
        return storage.LocalStorage(str(tmp_path / "objects")), None
    provider = MultipartStore()
    store = storage.S3Storage.__new__(storage.S3Storage)
    store._bucket, store._prefix, store._client = "synthetic-bucket", "scope", provider
    return store, provider


@pytest.mark.parametrize("kind", ["memory", "local", "s3"])
def test_file_upload_and_copy_roundtrip_bounded_chunks(kind, tmp_path, monkeypatch):
    store, provider = backend(kind, tmp_path)
    source = tmp_path / "source.bin"
    source.write_bytes(PAYLOAD)
    expected = hashlib.sha256(PAYLOAD).hexdigest()
    original_open, readers = Path.open, []

    def observed(path, *args, **kwargs):
        result = original_open(path, *args, **kwargs)
        if path == source and args == ("rb",):
            result = GuardedSource(result)
            readers.append(result)
        return result

    monkeypatch.setattr(Path, "open", observed)
    store.put_file(
        "original", source, "application/octet-stream", sha256=expected, size=len(PAYLOAD)
    )
    destination = tmp_path / "download.bin"
    actual = store.get_file("original", destination, max_bytes=len(PAYLOAD))
    assert actual == (expected, len(PAYLOAD))
    assert destination.read_bytes() == PAYLOAD
    assert readers and all(r.source.closed and r.requests for r in readers)
    if provider:
        assert provider.parts and not provider.uploads
        assert provider.objects["scope/original"] == PAYLOAD


@pytest.mark.parametrize("kind", ["memory", "local", "s3"])
@pytest.mark.parametrize("fault", ["sha", "size", "read"])
def test_failed_file_upload_does_not_replace_existing_object(kind, fault, tmp_path, monkeypatch):
    store, provider = backend(kind, tmp_path)
    previous = b"Synthetic previously published original"
    store.put("same-key", previous)
    source = tmp_path / "candidate.bin"
    content = BLOCK * 270 if fault == "read" else PAYLOAD
    source.write_bytes(content)
    expected = hashlib.sha256(content).hexdigest() if fault != "sha" else "0" * 64
    size = len(content) if fault != "size" else len(content) + 1
    original_open = Path.open

    def observed(path, *args, **kwargs):
        result = original_open(path, *args, **kwargs)
        return (
            GuardedSource(result, fault_after=8 * 1024 * 1024 + 65536)
            if path == source and args == ("rb",) and fault == "read"
            else result
        )

    monkeypatch.setattr(Path, "open", observed)
    with pytest.raises(storage.StorageError):
        store.put_file("same-key", source, sha256=expected, size=size)
    assert store.get("same-key") == previous
    if provider:
        assert provider.aborted and not provider.uploads and not provider.completed
    if kind == "local":
        assert not list((tmp_path / "objects").rglob(".upload-*"))


@pytest.mark.parametrize("fault", ["part", "complete"])
def test_failed_multipart_upload_aborts_before_retry(fault, tmp_path):
    store, provider = backend("s3", tmp_path)
    source = tmp_path / "multipart.bin"
    source.write_bytes(BLOCK * 270)
    expected = hashlib.sha256(source.read_bytes()).hexdigest()
    provider.fail = fault
    with pytest.raises(storage.StorageError):
        store.put_file("multipart", source, sha256=expected, size=source.stat().st_size)
    assert provider.aborted and not provider.uploads and not provider.objects
    provider.fail = None
    store.put_file("multipart", source, sha256=expected, size=source.stat().st_size)
    assert not provider.uploads and len(provider.completed) == 1
    assert all(0 < n <= 8 * 1024 * 1024 for n in provider.parts)
    assert len(provider.objects["scope/multipart"]) == source.stat().st_size


@pytest.mark.parametrize("kind", ["memory", "local", "s3"])
def test_file_copy_rejects_growth_at_limit_plus_one(kind, tmp_path):
    store, _ = backend(kind, tmp_path)
    store.put("grown", PAYLOAD)
    destination = tmp_path / "partial.bin"
    with pytest.raises(storage.StorageReadTooLarge):
        store.get_file("grown", destination, max_bytes=39)
    assert not destination.exists() or destination.stat().st_size <= 39


@pytest.mark.parametrize("kind", ["memory", "local", "s3"])
def test_empty_file_roundtrip(kind, tmp_path):
    store, provider = backend(kind, tmp_path)
    source, destination = tmp_path / "empty", tmp_path / "empty-copy"
    source.write_bytes(b"")
    sha = hashlib.sha256(b"").hexdigest()
    store.put_file("empty", source, sha256=sha, size=0)
    assert store.get_file("empty", destination, max_bytes=1) == (sha, 0)
    assert destination.read_bytes() == b""
    if provider:
        assert not provider.uploads


@pytest.mark.asyncio
async def test_documents_store_file_registers_exact_streamed_identity(
    auth_client, db_session, tmp_path
):
    from sqlalchemy import select

    from app.models.document import Document

    org_id = (await auth_client.get("/api/v1/auth/me")).json()["organization"]["id"]
    source = tmp_path / "fixture.zip"
    source.write_bytes(PAYLOAD)
    sha, size = await documents.store_file(
        documents.EXPORTS, org_id, source, "application/zip", db=db_session, filename="fixture.zip"
    )
    await db_session.commit()
    row = await db_session.scalar(select(Document).where(Document.kind == documents.EXPORTS))
    assert (row.sha256, row.size, row.filename) == (sha, size, "fixture.zip")
    assert sha == hashlib.sha256(PAYLOAD).hexdigest() and size == len(PAYLOAD)
    assert await documents.load(documents.EXPORTS, org_id, sha) == PAYLOAD


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["growth", "missing", "read"])
async def test_staging_failure_removes_only_owned_temporary_file(tmp_path, monkeypatch, failure):
    store = storage.LocalStorage(str(tmp_path / "objects"))
    storage.set_storage(store)
    sha = hashlib.sha256(PAYLOAD).hexdigest()
    key = storage.content_key(documents.EXPORTS, "synthetic-org", sha)
    store.put(key, PAYLOAD)
    if failure == "missing":
        store.delete(key)
    elif failure == "read":
        path = store._path(key)
        path.unlink()
        path.mkdir()
    paths = []
    original = documents.tempfile.mkstemp

    def tracked(*args, **kwargs):
        result = original(*args, **kwargs)
        paths.append(Path(result[1]))
        return result

    monkeypatch.setattr(documents.tempfile, "mkstemp", tracked)
    with pytest.raises(storage.StorageError):
        await documents.load_file(documents.EXPORTS, "synthetic-org", sha, max_bytes=41)
    assert paths and all(not path.exists() for path in paths)
    if failure == "growth":
        assert store.get(key) == PAYLOAD


class UploadInterrupted(BaseException):
    pass


def test_multipart_interruption_aborts_and_preserves_primary_error(tmp_path, monkeypatch):
    store, provider = backend("s3", tmp_path)
    source = tmp_path / "source.bin"
    content = BLOCK * 270
    source.write_bytes(content)
    original_open = Path.open
    primary = UploadInterrupted()

    class Interrupted(GuardedSource):
        def read(self, size=-1):
            if self.consumed >= 8 * 1024 * 1024:
                raise primary
            return super().read(size)

        def close(self):
            self.source.close()
            raise OSError("Synthetic secondary close failure")

    def observed(path, *args, **kwargs):
        result = original_open(path, *args, **kwargs)
        return Interrupted(result) if path == source and args == ("rb",) else result

    monkeypatch.setattr(Path, "open", observed)
    with pytest.raises(UploadInterrupted) as caught:
        store.put_file(
            "interrupted", source, sha256=hashlib.sha256(content).hexdigest(), size=len(content)
        )
    assert caught.value is primary
    assert provider.aborted and not provider.uploads and not provider.completed


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["none", "missing", "read", "hash"])
@pytest.mark.parametrize("repeated", [False, True])
async def test_cancelled_staging_joins_delayed_backend_before_cleanup(
    tmp_path, monkeypatch, failure, repeated
):
    import asyncio
    import threading

    store = storage.LocalStorage(str(tmp_path / "objects"))
    storage.set_storage(store)
    sha = hashlib.sha256(PAYLOAD).hexdigest()
    key = storage.content_key(documents.EXPORTS, "synthetic-org", sha)
    store.put(key, PAYLOAD)
    if failure == "missing":
        store.delete(key)
    elif failure == "read":
        source = store._path(key)
        source.unlink()
        source.mkdir()
    elif failure == "hash":
        store.put(key, b"X" + PAYLOAD[1:])
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    paths = []
    original_get = store.get_file

    def delayed(key, destination, *, max_bytes=None):
        paths.append(destination)
        entered.set()
        assert release.wait(timeout=10), "Synthetic delayed native reader was not released"
        try:
            return original_get(key, destination, max_bytes=max_bytes)
        finally:
            finished.set()

    monkeypatch.setattr(store, "get_file", delayed)
    pending = asyncio.create_task(
        documents.load_verified_file(documents.EXPORTS, "synthetic-org", sha, size=len(PAYLOAD))
    )
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        pending.cancel()
        for _ in range(5):
            await asyncio.sleep(0)
        assert not pending.done() and not finished.is_set()
        if repeated:
            pending.cancel()
            for _ in range(5):
                await asyncio.sleep(0)
            assert not pending.done() and not finished.is_set()
    finally:
        release.set()
        try:
            with pytest.raises(asyncio.CancelledError):
                await pending
        finally:
            assert await asyncio.to_thread(finished.wait, 5)
            leftovers = [path for path in paths if path.exists()]
            # Even a deliberately unsafe control must clean its own test files.
            for path in leftovers:
                path.unlink()
    assert finished.is_set() and paths
    assert leftovers == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mutate", [False, True])
@pytest.mark.parametrize("repeated", [False, True])
async def test_cancelled_file_upload_joins_verification_before_source_cleanup(
    tmp_path, monkeypatch, mutate, repeated
):
    import asyncio
    import threading

    store = storage.LocalStorage(str(tmp_path / "objects"))
    storage.set_storage(store)
    source = tmp_path / "owned-source.zip"
    source.write_bytes(PAYLOAD)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original_put = store.put_file

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=10)
        try:
            return original_put(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(store, "put_file", delayed)

    async def upload():
        try:
            return await documents.store_file(documents.EXPORTS, "synthetic-org", source)
        finally:
            source.unlink(missing_ok=True)

    pending = asyncio.create_task(upload())
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        pending.cancel()
        for _ in range(5):
            await asyncio.sleep(0)
        assert not pending.done() and source.exists() and not finished.is_set()
        if mutate:
            source.write_bytes(b"X" + PAYLOAD[1:])
        if repeated:
            pending.cancel()
            for _ in range(5):
                await asyncio.sleep(0)
            assert not pending.done() and source.exists()
    finally:
        release.set()
        try:
            with pytest.raises(asyncio.CancelledError):
                await pending
        finally:
            assert await asyncio.to_thread(finished.wait, 5)
            source.unlink(missing_ok=True)
    assert finished.is_set() and not source.exists()
    expected_key = storage.content_key(
        documents.EXPORTS, "synthetic-org", hashlib.sha256(PAYLOAD).hexdigest()
    )
    assert store.exists(expected_key) is (not mutate)
    if not mutate:
        assert store.get(expected_key) == PAYLOAD
