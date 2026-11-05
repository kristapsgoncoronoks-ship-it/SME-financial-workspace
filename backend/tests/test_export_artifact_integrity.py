"""An export link may consume only the exact admitted artifact, with retry after repair."""

import io
import re
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.core import storage
from app.core.config import settings
from app.models.audit import AuditEvent
from app.models.email_message import EmailMessage
from app.models.email_token import EmailToken
from app.services import audit, documents
from tests.test_local_export_read_errors import _model, _request, _run, _seed
from tests.test_s3_export_read_errors import _S3Files

PRIVATE = "synthetic-private-export-artifact-path"
READ_ERROR = {
    "detail": "Stored document could not be read. Check storage and retry.",
    "code": "storage_read_unavailable",
}
INTEGRITY_ERROR = {
    "detail": "Stored export integrity could not be verified. Restore the original export and retry.",
    "code": "export_integrity_unverified",
}


class _File:
    def __init__(self, source):
        self.source = source
        self.requests = []
        self.consumed = 0
        self.close_calls = 0

    @property
    def closed(self):
        return self.source.closed

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def read(self, amount=None):
        self.requests.append(amount)
        data = self.source.read(amount)
        self.consumed += len(data)
        return data

    def close(self):
        self.close_calls += 1
        self.source.close()


class _Artifact:
    def __init__(self, local, key, original, backend):
        self.local, self.key = local, key
        self.handles, self.calls = [], []
        self.provider = None
        if backend == "memory":
            self.store = storage.MemoryStorage()
            self.store.put(key, original)
        elif backend == "s3":
            self.provider = _S3Files(local)
            self.store = storage.S3Storage.__new__(storage.S3Storage)
            self.store._bucket = "synthetic-export-bucket"
            self.store._prefix = ""
            self.store._client = self.provider
        else:
            self.store = local
        storage.set_storage(self.store)

    def replace(self, data):
        if self.provider is not None:
            self.local.put(self.key, data)
        else:
            self.store.put(self.key, data)

    def observe(self, monkeypatch):
        original_open, original_get = Path.open, self.store.get_file
        target = self.local._path(self.key)

        def opened(path, *args, **kwargs):
            source = original_open(path, *args, **kwargs)
            if path == target and args == ("rb",):
                source = _File(source)
                self.handles.append(source)
            return source

        def get_file(key, destination, *, max_bytes=None):
            self.calls.append((key, max_bytes))
            return original_get(key, destination, max_bytes=max_bytes)

        monkeypatch.setattr(Path, "open", opened)
        monkeypatch.setattr(self.store, "get_file", get_file)

    def assert_closed(self):
        assert all(h.source.closed and h.close_calls == 1 for h in self.handles)
        if self.provider is not None:
            self.provider.assert_closed()


async def _ready(client, db, tmp_path, kind, backend="local"):
    _, _, local = await _seed(client, db, tmp_path)
    export_id = await _request(client, kind)
    assert (await _run(db, kind)).status == "succeeded"
    row = await db.get(_model(kind), export_id)
    await db.refresh(row)
    token = await db.scalar(select(EmailToken).where(EmailToken.purpose == f"{kind}_export"))
    email = await db.scalar(select(EmailMessage).where(EmailMessage.kind == f"{kind}_export"))
    match = re.search(rf"/api/v1/{kind}/export/download/[A-Za-z0-9_-]+", email.body)
    assert match is not None
    key = storage.content_key(documents.EXPORTS, row.org_id, row.sha256)
    original = local.get(key)
    return row, token, match.group(0), original, _Artifact(local, key, original, backend)


def _snapshot(row):
    return {column.key: getattr(row, column.key) for column in row.__table__.columns}


def _action(kind):
    return (
        audit.A.WORKSPACE_EXPORT_DOWNLOADED
        if kind == "workspace"
        else audit.A.ARCHIVE_EXPORT_DOWNLOADED
    )


async def _audit_count(db, kind, row):
    return await db.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(AuditEvent.action == _action(kind), AuditEvent.target_id == row.id)
    )


async def _unchanged(db, kind, row, token, row_before, token_before):
    await db.refresh(row)
    await db.refresh(token)
    assert _snapshot(row) == row_before
    assert _snapshot(token) == token_before
    assert row.status == "ready" and row.downloaded_at is None and token.used_at is None
    assert await _audit_count(db, kind, row) == 0


def _refused(response, artifact, tmp_path, *, read_error=False):
    assert response.status_code == 503
    assert response.json() == (READ_ERROR if read_error else INTEGRITY_ERROR)
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "content-disposition" not in response.headers
    assert all(value not in response.text for value in (PRIVATE, artifact.key, str(tmp_path)))
    artifact.assert_closed()


async def _once(client, db, kind, row, token, url, original, artifact):
    identity = row.sha256, row.size
    response = await client.get(url)
    assert response.status_code == 200 and response.content == original
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-disposition"].startswith("attachment;")
    await db.refresh(row)
    await db.refresh(token)
    assert row.status == "downloaded" and row.downloaded_at is not None
    assert token.used_at is not None and (row.sha256, row.size) == identity
    assert await _audit_count(db, kind, row) == 1
    before = len(artifact.calls)
    assert (await client.get(url)).status_code == 404
    assert len(artifact.calls) == before
    artifact.assert_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "backend", "replacement"),
    [
        ("workspace", "memory", "same"),
        ("archive", "memory", "same"),
        ("workspace", "local", "larger"),
        ("archive", "s3", "larger"),
        ("workspace", "s3", "truncated"),
        ("archive", "local", "truncated"),
        ("workspace", "local", "empty"),
        ("archive", "memory", "empty"),
    ],
)
async def test_corrupt_export_refuses_without_consumption_then_exact_restore_recovers(
    auth_client, db_session, tmp_path, monkeypatch, kind, backend, replacement
):
    row, token, url, original, artifact = await _ready(
        auth_client, db_session, tmp_path, kind, backend
    )
    if replacement == "same":
        corrupt = bytes([original[0] ^ 1]) + original[1:]
    elif replacement == "larger":
        corrupt = original + b"x" * (1024 * 1024)
    elif replacement == "truncated":
        corrupt = original[:-1]
    else:
        corrupt = b""
    row_before, token_before = _snapshot(row), _snapshot(token)
    artifact.replace(corrupt)
    artifact.observe(monkeypatch)
    _refused(await auth_client.get(url), artifact, tmp_path)
    await _unchanged(db_session, kind, row, token, row_before, token_before)
    assert artifact.calls == [(artifact.key, row.size)]
    if backend != "memory":
        assert len(artifact.handles) == 1
        handle = artifact.handles[0]
        assert handle.consumed == min(len(corrupt), row.size + 1)
        assert all(0 < amount <= 65536 for amount in handle.requests)
    artifact.replace(original)
    await _once(auth_client, db_session, kind, row, token, url, original, artifact)


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "zero_size"), [("workspace", False), ("archive", True)])
async def test_recorded_size_is_verified_even_when_sha_matches(
    auth_client, db_session, tmp_path, kind, zero_size
):
    row, token, url, original, artifact = await _ready(
        auth_client, db_session, tmp_path, kind, "memory"
    )
    actual_size = row.size
    row.size = 0 if zero_size else row.size + 3
    await db_session.commit()
    await db_session.refresh(row)
    await db_session.refresh(token)
    row_before, token_before = _snapshot(row), _snapshot(token)
    _refused(await auth_client.get(url), artifact, tmp_path)
    await _unchanged(db_session, kind, row, token, row_before, token_before)
    row.size = actual_size
    await db_session.commit()
    await _once(auth_client, db_session, kind, row, token, url, original, artifact)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "backend", "known_size"),
    [("workspace", "local", False), ("archive", "s3", False), ("workspace", "memory", True)],
)
async def test_historical_artifact_ignores_current_upload_cap_but_always_verifies_sha(
    auth_client, db_session, tmp_path, monkeypatch, kind, backend, known_size
):
    row, token, url, _, artifact = await _ready(auth_client, db_session, tmp_path, kind, backend)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("historical.bin", b"x" * (1536 * 1024))
    original = output.getvalue()
    row.sha256 = storage.sha256_hex(original)
    row.size = len(original) if known_size else None
    artifact.key = storage.content_key(documents.EXPORTS, row.org_id, row.sha256)
    artifact.replace(original)
    await db_session.commit()
    await db_session.refresh(row)
    await db_session.refresh(token)
    monkeypatch.setattr(settings, "max_upload_mb", 1)
    assert len(original) > 1024 * 1024
    row_before, token_before = _snapshot(row), _snapshot(token)
    artifact.replace(bytes([original[0] ^ 1]) + original[1:])
    artifact.observe(monkeypatch)
    _refused(await auth_client.get(url), artifact, tmp_path)
    await _unchanged(db_session, kind, row, token, row_before, token_before)
    assert artifact.calls == [(artifact.key, row.size)]
    artifact.replace(original)
    await _once(auth_client, db_session, kind, row, token, url, original, artifact)


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "backend"), [("workspace", "memory"), ("archive", "local")])
async def test_recorded_zero_and_matching_empty_hash_are_not_reinterpreted_as_zip_parser_policy(
    auth_client, db_session, tmp_path, kind, backend
):
    row, token, url, _, artifact = await _ready(auth_client, db_session, tmp_path, kind, backend)
    row.sha256, row.size = storage.sha256_hex(b""), 0
    artifact.key = storage.content_key(documents.EXPORTS, row.org_id, row.sha256)
    artifact.replace(b"")
    await db_session.commit()
    await _once(auth_client, db_session, kind, row, token, url, b"", artifact)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "backend", "fault"),
    [
        ("workspace", "memory", "missing"),
        ("archive", "s3", "missing"),
        ("workspace", "local", "read"),
        ("archive", "s3", "read"),
    ],
)
async def test_missing_and_unavailable_stay_distinct_without_consuming_link(
    auth_client, db_session, tmp_path, kind, backend, fault
):
    row, token, url, original, artifact = await _ready(
        auth_client, db_session, tmp_path, kind, backend
    )
    row_before, token_before = _snapshot(row), _snapshot(token)
    if artifact.provider is not None:
        if fault == "missing":
            artifact.local.delete(artifact.key)
        else:
            artifact.provider.faults[artifact.key] = "read"
    elif fault == "missing":
        artifact.store.delete(artifact.key)
    else:
        path = artifact.local._path(artifact.key)
        path.unlink()
        path.mkdir()
    response = await auth_client.get(url)
    if fault == "missing":
        assert response.status_code == 404 and "content-disposition" not in response.headers
    else:
        _refused(response, artifact, tmp_path, read_error=True)
    await _unchanged(db_session, kind, row, token, row_before, token_before)
    if artifact.provider is not None:
        artifact.provider.faults.clear()
    elif fault == "read":
        artifact.local._path(artifact.key).rmdir()
    artifact.replace(original)
    await _once(auth_client, db_session, kind, row, token, url, original, artifact)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["workspace", "archive"])
async def test_invalid_expired_wrong_purpose_tokens_are_rejected_before_storage(
    auth_client, db_session, tmp_path, monkeypatch, kind
):
    row, token, url, original, artifact = await _ready(
        auth_client, db_session, tmp_path, kind, "memory"
    )
    original_expiry, original_purpose = token.expires_at, token.purpose
    for fault in ("invalid", "expired", "wrong-purpose"):
        token.expires_at = (
            datetime.now(UTC) - timedelta(seconds=1) if fault == "expired" else original_expiry
        )
        token.purpose = "password_reset" if fault == "wrong-purpose" else original_purpose
        await db_session.commit()
        await db_session.refresh(row)
        await db_session.refresh(token)
        row_before, token_before = _snapshot(row), _snapshot(token)
        address = url.rsplit("/", 1)[0] + "/synthetic-invalid-token" if fault == "invalid" else url
        with monkeypatch.context() as patch:
            patch.setattr(
                artifact.store,
                "get",
                lambda *args, **kwargs: pytest.fail("invalid token read storage"),
            )
            patch.setattr(
                artifact.store,
                "digest",
                lambda *args, **kwargs: pytest.fail("invalid token hashed storage"),
            )
            response = await auth_client.get(address)
        assert response.status_code == 404 and "content-disposition" not in response.headers
        await _unchanged(db_session, kind, row, token, row_before, token_before)
    token.expires_at, token.purpose = original_expiry, original_purpose
    await db_session.commit()
    await _once(auth_client, db_session, kind, row, token, url, original, artifact)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["workspace", "archive"])
async def test_negative_recorded_size_refuses_before_any_storage_access(
    auth_client, db_session, tmp_path, monkeypatch, kind
):
    row, token, url, original, artifact = await _ready(
        auth_client, db_session, tmp_path, kind, "memory"
    )
    actual_size = row.size
    row.size = -1
    await db_session.commit()
    await db_session.refresh(row)
    await db_session.refresh(token)
    row_before, token_before = _snapshot(row), _snapshot(token)
    with monkeypatch.context() as patch:
        patch.setattr(
            artifact.store,
            "get",
            lambda *args, **kwargs: pytest.fail("negative size must not read storage"),
        )
        patch.setattr(
            artifact.store,
            "digest",
            lambda *args, **kwargs: pytest.fail("negative size must not hash storage"),
        )
        _refused(await auth_client.get(url), artifact, tmp_path)
    await _unchanged(db_session, kind, row, token, row_before, token_before)
    row.size = actual_size
    await db_session.commit()
    await _once(auth_client, db_session, kind, row, token, url, original, artifact)
