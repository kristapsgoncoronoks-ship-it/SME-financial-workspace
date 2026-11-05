"""Evidence ZIP staging preserves the filing contract with finite source reads."""

import csv
import hashlib
import io
import stat
import tempfile
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core import storage
from app.core.config import settings
from app.core.errors import AppError
from app.main import app
from app.models.invoice import Invoice
from app.services import document_registry, documents
from app.services.transport import claim_pack
from tests.test_issued_attachment_read_bounds import ObservedBody
from tests.transport.test_g2_12_claim_pack import _attach_document, _ready_org, _submitted_claim
from tests.transport.test_wo77_admin_routes import (
    _enable_transport,
    _register_org,
)
from tests.transport.test_wo77_admin_routes import _submitted_claim as submitted_http

CONTENT = b"%PDF-1.4 Synthetic original filing evidence"


async def ready(db, *, content=CONTENT, known=True):
    org, entity = await _ready_org(db)
    claim, invoice, sha = await _submitted_claim(db, org, entity, doc_data=content)
    if known:
        await document_registry.register(
            db, org.id, sha256=sha, size=len(content), kind=documents.UPLOADS
        )
        await db.commit()
    return org, claim, invoice, sha


def verify_manifest(data):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        manifest = next(name for name in names if name.endswith("manifest.csv"))
        rows = list(csv.reader(io.StringIO(archive.read(manifest).decode())))
        assert rows[0] == ["file", "sha256", "size_bytes"]
        assert [row[0] for row in rows[1:]] == [name for name in names if name != manifest]
        for name, sha, size in rows[1:]:
            content = archive.read(name)
            assert hashlib.sha256(content).hexdigest() == sha and len(content) == int(size)
        return {name: archive.read(name) for name in names}


async def test_restored_claim_source_stops_at_recorded_size_and_refuses_corrupt_bundle(
    db_session, tmp_path, monkeypatch
):
    store = storage.LocalStorage(str(tmp_path))
    storage.set_storage(store)
    org, claim, _invoice, sha = await ready(db_session)
    key = storage.content_key(documents.UPLOADS, org.id, sha)
    path = store._path(key)
    with path.open("r+b") as source:
        source.truncate(32 * 1024 * 1024)
    original_open, reads = Path.open, []

    def observed(candidate, *args, **kwargs):
        source = original_open(candidate, *args, **kwargs)
        if candidate == path and args == ("rb",):
            source = ObservedBody(source)
            reads.append(source)
        return source

    monkeypatch.setattr(Path, "open", observed)
    error = None
    try:
        await claim_pack.build_evidence_pack(db_session, org.id, claim.id)
    except AppError as exc:
        error = exc
    assert len(reads) == 1 and reads[0].consumed == len(CONTENT) + 1
    assert reads[0].source.closed and all(0 < n <= 65536 for n in reads[0].requests)
    assert error is not None and error.code == "evidence_document_unavailable"
    assert path.stat().st_size == 32 * 1024 * 1024
    store.put(key, CONTENT)
    recovered = await claim_pack.build_evidence_pack(db_session, org.id, claim.id)
    assert CONTENT in verify_manifest(recovered).values()


@pytest.mark.parametrize("known", [False, True])
@pytest.mark.parametrize("replacement", [b"X" + CONTENT[1:], CONTENT[:4]])
async def test_corrupt_claim_original_never_becomes_a_new_manifest_identity(
    db_session, known, replacement
):
    org, claim, _invoice, sha = await ready(db_session, known=known)
    storage.get_storage().put(storage.content_key(documents.UPLOADS, org.id, sha), replacement)
    with pytest.raises(AppError) as error:
        await claim_pack.build_evidence_pack(db_session, org.id, claim.id)
    assert error.value.code == "evidence_document_unavailable"


@pytest.mark.parametrize("known", [False, True])
async def test_owned_claim_file_keeps_historical_sources_without_workspace_archive_cap(
    db_session, monkeypatch, known
):
    content = b"%PDF-1.4\n" + hashlib.shake_256(b"Synthetic historical claim original").digest(
        2 * 1024 * 1024
    )
    org, claim, _invoice, _sha = await ready(db_session, content=content, known=known)
    monkeypatch.setattr(settings, "max_upload_mb", 1)
    monkeypatch.setattr(settings, "workspace_export_max_bytes", 1024 * 1024)

    async def forbid_bytes(*args, **kwargs):
        raise AssertionError("File builder materialized a complete original through the byte API")

    monkeypatch.setattr(documents, "load", forbid_bytes)
    staged = await claim_pack.build_evidence_pack_file(db_session, org.id, claim.id)
    try:
        assert stat.S_IMODE(staged.path.stat().st_mode) == 0o600
        assert staged.size == staged.path.stat().st_size > settings.workspace_export_max_bytes
        data = staged.path.read_bytes()
        assert staged.sha256 == hashlib.sha256(data).hexdigest()
        assert content in verify_manifest(data).values()
    finally:
        staged.cleanup()
    assert not staged.path.exists()


async def test_claim_zip_write_failure_cleans_only_owned_files(db_session, monkeypatch):
    org, claim, _invoice, _sha = await ready(db_session)
    created, original = [], tempfile.mkstemp

    def track(*args, **kwargs):
        result = original(*args, **kwargs)
        created.append(Path(result[1]))
        return result

    original_write = zipfile.ZipFile.write

    def refuse(archive, filename, arcname=None, *args, **kwargs):
        if arcname and str(arcname).endswith("q8-inv-0001.pdf"):
            raise RuntimeError("Synthetic ZIP write refusal")
        return original_write(archive, filename, arcname, *args, **kwargs)

    monkeypatch.setattr(tempfile, "mkstemp", track)

    monkeypatch.setattr(zipfile.ZipFile, "write", refuse)
    with pytest.raises(RuntimeError, match="Synthetic ZIP write refusal"):
        await claim_pack.build_evidence_pack_file(db_session, org.id, claim.id)
    assert len(created) >= 2 and all(not path.exists() for path in created)
    assert (
        storage.get_storage().get(storage.content_key(documents.UPLOADS, org.id, _sha)) == CONTENT
    )


async def test_claim_http_streams_chunks_and_releases_exact_complete_zip(
    client, db_session, monkeypatch
):
    from types import SimpleNamespace

    headers, org_id = await _register_org(client)
    await _enable_transport(db_session, org_id)
    claim, _entity = await submitted_http(client, db_session, headers, org_id)
    invoice = await db_session.scalar(
        select(Invoice).where(Invoice.org_id == org_id, Invoice.invoice_number == "INV-0001")
    )
    content = b"%PDF-1.4\n" + hashlib.shake_256(b"Synthetic HTTP evidence original").digest(
        256 * 1024
    )
    await _attach_document(
        db_session, SimpleNamespace(id=org_id), invoice, data=content, filename="extra.pdf"
    )
    chunks, started, created = [], {}, []
    real_mkstemp = tempfile.mkstemp

    def track(*args, **kwargs):
        result = real_mkstemp(*args, **kwargs)
        created.append(Path(result[1]))
        return result

    monkeypatch.setattr(tempfile, "mkstemp", track)

    async def forbid_compatibility(*args, **kwargs):
        raise AssertionError("HTTP used the complete-byte compatibility adapter")

    monkeypatch.setattr(claim_pack, "build_evidence_pack", forbid_compatibility)
    first = True

    async def receive():
        nonlocal first
        if first:
            first = False
            return {"type": "http.request", "body": b"", "more_body": False}
        import anyio

        await anyio.sleep_forever()

    async def send(message):
        if message["type"] == "http.response.start":
            started.update(message)
        elif message["type"] == "http.response.body":
            chunk = message.get("body", b"")
            assert len(chunk) <= 65536, "Evidence response allocated a complete ZIP body"
            chunks.append(chunk)

    path = f"/api/v1/transport/claims/{claim['id']}/evidence"
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"authorization", headers["Authorization"].encode()),
            (b"range", b"bytes=0-9"),
        ],
        "client": ("127.0.0.1", 1234),
        "server": ("test", 80),
    }
    await app(scope, receive, send)
    assert started["status"] == 200 and len(chunks) > 1
    response_headers = dict(started["headers"])
    assert response_headers[b"cache-control"] == b"private, no-store"
    assert b"content-range" not in response_headers and b"accept-ranges" not in response_headers
    assert content in verify_manifest(b"".join(chunks)).values()
    assert created and all(not path.exists() for path in created)


@pytest.mark.parametrize("repeated", [False, True])
async def test_cancelled_claim_build_joins_native_reader_before_all_owned_cleanup(
    db_session, tmp_path, monkeypatch, repeated
):
    import asyncio
    import threading

    store = storage.LocalStorage(str(tmp_path / "objects"))
    storage.set_storage(store)
    org, claim, _invoice, sha = await ready(db_session)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    created, real_temp, real_get = [], tempfile.mkstemp, store.get_file

    def track(*args, **kwargs):
        result = real_temp(*args, **kwargs)
        created.append(Path(result[1]))
        return result

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=10)
        try:
            return real_get(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(tempfile, "mkstemp", track)
    monkeypatch.setattr(store, "get_file", delayed)
    task = asyncio.create_task(claim_pack.build_evidence_pack_file(db_session, org.id, claim.id))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        for _ in range(5):
            await asyncio.sleep(0)
        assert not task.done() and not finished.is_set()
        if repeated:
            task.cancel()
            for _ in range(5):
                await asyncio.sleep(0)
            assert not task.done() and not finished.is_set()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert finished.is_set() and len(created) >= 2
    assert all(not path.exists() for path in created)
    assert store.get(storage.content_key(documents.UPLOADS, org.id, sha)) == CONTENT
