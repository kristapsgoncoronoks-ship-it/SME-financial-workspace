"""Export workers and HTTP delivery must not materialize the complete ZIP."""

from __future__ import annotations

import builtins
import io
import os
import zipfile
from pathlib import Path

import pytest

from app.core import storage
from app.main import app
from app.services import documents
from tests.test_export_artifact_integrity import _ready
from tests.test_local_export_read_errors import _request, _run, _seed
from tests.test_storage_file_streams import GuardedSource


@pytest.mark.asyncio
async def test_workspace_worker_uses_only_finite_file_reads(
    auth_client, db_session, tmp_path, monkeypatch
):
    await _seed(auth_client, db_session, tmp_path)
    readers = []
    original_open, original_path_open = builtins.open, Path.open

    def watched(path, mode, result):
        if mode == "rb" and os.path.basename(str(path)).startswith("workspace-export-"):
            result = GuardedSource(result)
            readers.append(result)
        return result

    def opened(path, mode="r", *args, **kwargs):
        return watched(path, mode, original_open(path, mode, *args, **kwargs))

    def path_opened(path, mode="r", *args, **kwargs):
        return watched(path, mode, original_path_open(path, mode, *args, **kwargs))

    monkeypatch.setattr(builtins, "open", opened)
    monkeypatch.setattr(Path, "open", path_opened)
    await _request(auth_client, "workspace")
    completed = await _run(db_session, "workspace")
    assert completed.status == "succeeded", completed.last_error
    assert readers and all(reader.requests and reader.source.closed for reader in readers)
    assert all(not Path(reader.source.name).exists() for reader in readers)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["workspace", "archive"])
async def test_export_http_sends_finite_chunks_and_preserves_exact_zip(
    auth_client, db_session, tmp_path, kind
):
    row, _, url, _, artifact = await _ready(auth_client, db_session, tmp_path, kind)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("synthetic.bin", bytes(range(256)) * 20000)
    original = output.getvalue()
    row.sha256, row.size = storage.sha256_hex(original), len(original)
    artifact.key = storage.content_key(documents.EXPORTS, row.org_id, row.sha256)
    artifact.replace(original)
    await db_session.commit()
    chunks = []
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        import anyio

        await anyio.sleep_forever()

    async def send(message):
        if message["type"] == "http.response.start":
            assert message["status"] == 200
        if message["type"] == "http.response.body":
            chunk = message.get("body", b"")
            assert len(chunk) <= 65536, "Export HTTP response allocated one complete ZIP body"
            chunks.append(chunk)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": url,
        "raw_path": url.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("127.0.0.1", 1234),
        "server": ("test", 80),
    }
    await app(scope, receive, send)
    assert len(chunks) > 1 and b"".join(chunks) == original


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "failure", "cancelled"])
async def test_owned_response_cleans_staging_on_every_transfer_exit(tmp_path, outcome):
    import asyncio

    from app.api.file_response import TemporaryFileResponse

    path = tmp_path / "verified.zip"
    path.write_bytes(b"x" * (3 * 65536))
    staged = documents.StoredFile(path, storage.sha256_hex(path.read_bytes()), path.stat().st_size)
    response = TemporaryFileResponse(staged, media_type="application/zip")
    bodies = []

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body":
            bodies.append(message["body"])
            if outcome == "failure":
                raise RuntimeError("Synthetic client transfer failed")
            if outcome == "cancelled":
                raise asyncio.CancelledError()

    scope = {
        "type": "http",
        "method": "GET",
        "headers": [(b"range", b"bytes=0-9")],
        "extensions": {"http.response.pathsend": {}},
        "asgi": {"spec_version": "2.4"},
    }
    if outcome == "success":
        await response(scope, receive, send)
        assert sum(len(body) for body in bodies) == staged.size
        assert "accept-ranges" not in response.headers and "content-range" not in response.headers
    elif outcome == "failure":
        with pytest.raises(RuntimeError, match="Synthetic client"):
            await response(scope, receive, send)
    else:
        with pytest.raises(asyncio.CancelledError):
            await response(scope, receive, send)
    assert bodies and not path.exists()
