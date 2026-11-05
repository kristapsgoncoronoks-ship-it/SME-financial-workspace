"""S3 read failures preserve complete export builds and one-time download recovery.

The S3 adapter and HTTP/job/database paths are real. Only the provider client is
synthetic; returned botocore bodies wrap actual isolated local file descriptors.
"""

from __future__ import annotations

import re

import pytest
from botocore.exceptions import ClientError
from botocore.response import StreamingBody
from sqlalchemy import func, select

from app.core import storage
from app.models.audit import AuditEvent
from app.models.document import Document
from app.models.email_message import EmailMessage
from app.models.email_token import EmailToken
from app.services import audit, documents, jobs
from tests.test_local_export_read_errors import PAYLOAD, _assert_zip, _model, _request, _run, _seed

PRIVATE_DIAGNOSTIC = "synthetic-private-s3-provider-diagnostic"


class _FileSource:
    def __init__(self, source, fault):
        self.source = source
        self.fault = fault
        self.consumed = 0
        self.close_calls = 0

    def read(self, amount=None):
        if self.fault == "read":
            self.consumed += len(self.source.read(8))
            raise OSError(PRIVATE_DIAGNOSTIC)
        data = self.source.read(amount)
        self.consumed += len(data)
        return data

    def close(self):
        self.close_calls += 1
        self.source.close()


class _S3Files:
    """An owned file-backed provider seam; S3Storage performs error classification."""

    def __init__(self, local):
        self.local = local
        self.faults = {}
        self.handles = []
        self.lookups = []
        self.uploads = {}

    def get_object(self, *, Bucket, Key):
        assert Bucket == "synthetic-export-bucket"
        self.lookups.append(Key)
        fault = self.faults.get(Key)
        if fault == "denied":
            raise ClientError(
                {"Error": {"Code": "AccessDenied", "Message": PRIVATE_DIAGNOSTIC}}, "GetObject"
            )
        try:
            path = self.local._path(Key)
            source = path.open("rb")
        except FileNotFoundError as exc:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": PRIVATE_DIAGNOSTIC}}, "GetObject"
            ) from exc
        body = _FileSource(source, fault)
        self.handles.append(body)
        expected = path.stat().st_size + (1 if fault == "truncated" else 0)
        return {"Body": StreamingBody(body, expected), "ContentLength": expected}

    def put_object(self, *, Bucket, Key, Body, **kwargs):
        assert Bucket == "synthetic-export-bucket"
        self.local.put(Key, Body, kwargs.get("ContentType"))

    def create_multipart_upload(self, *, Bucket, Key, **kwargs):
        assert Bucket == "synthetic-export-bucket"
        upload_id = f"synthetic-{len(self.uploads)}"
        self.uploads[upload_id] = (Key, {})
        return {"UploadId": upload_id}

    def upload_part(self, *, Bucket, Key, UploadId, PartNumber, Body):
        assert Bucket == "synthetic-export-bucket"
        assert self.uploads[UploadId][0] == Key
        self.uploads[UploadId][1][PartNumber] = Body
        return {"ETag": f"part-{PartNumber}"}

    def complete_multipart_upload(self, *, Bucket, Key, UploadId, MultipartUpload):
        assert Bucket == "synthetic-export-bucket"
        key, parts = self.uploads.pop(UploadId)
        assert key == Key
        self.local.put(Key, b"".join(parts[n] for n in sorted(parts)))

    def abort_multipart_upload(self, *, Bucket, Key, UploadId):
        assert Bucket == "synthetic-export-bucket"
        self.uploads.pop(UploadId, None)

    def assert_closed(self):
        assert all(handle.source.closed and handle.close_calls == 1 for handle in self.handles)
        assert not self.uploads


async def _seed_s3(auth_client, db_session, tmp_path):
    org_id, sha, local = await _seed(auth_client, db_session, tmp_path)
    client = _S3Files(local)
    store = storage.S3Storage.__new__(storage.S3Storage)
    store._bucket = "synthetic-export-bucket"
    store._prefix = ""
    store._client = client
    storage.set_storage(store)
    return org_id, sha, client


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["workspace", "archive"])
@pytest.mark.parametrize("fault", ["denied", "read", "truncated"])
async def test_s3_read_failure_cannot_publish_partial_export_and_worker_retry_recovers(
    auth_client, db_session, tmp_path, kind, fault
):
    org_id, sha, client = await _seed_s3(auth_client, db_session, tmp_path)
    source_key = storage.content_key(documents.UPLOADS, org_id, sha)
    client.faults[source_key] = fault
    export_id = await _request(auth_client, kind)
    failed = await _run(db_session, kind)
    assert failed.status == "queued" and failed.attempts == 1, {
        "job_status": failed.status,
        "result": failed.result_json,
    }
    assert "StorageReadError" in failed.last_error
    assert PRIVATE_DIAGNOSTIC not in failed.last_error and source_key not in failed.last_error
    row = await db_session.get(_model(kind), export_id)
    assert row.status == "queued" and row.sha256 is None and row.ready_at is None
    assert not row.missing_documents
    for model, condition in (
        (EmailMessage, EmailMessage.kind == f"{kind}_export"),
        (EmailToken, EmailToken.purpose == f"{kind}_export"),
        (Document, Document.kind == documents.EXPORTS),
    ):
        assert (
            await db_session.scalar(select(func.count()).select_from(model).where(condition)) == 0
        )
    assert client.lookups == [source_key]
    client.assert_closed()
    if fault != "denied":
        assert client.handles and client.handles[0].consumed > 0
    assert client.local.get(source_key) == PAYLOAD

    client.faults.clear()
    await jobs.retry(db_session, failed)
    completed = await _run(db_session, kind)
    assert completed.status == "succeeded" and completed.id == failed.id, completed.last_error
    await db_session.refresh(row)
    assert row.status == "ready" and row.missing_documents == 0 and row.sha256 is not None
    data = await documents.load(documents.EXPORTS, org_id, row.sha256)
    _assert_zip(data, kind, missing=False)
    for model, condition in (
        (EmailMessage, EmailMessage.kind == f"{kind}_export"),
        (EmailToken, EmailToken.purpose == f"{kind}_export"),
        (Document, Document.kind == documents.EXPORTS),
    ):
        assert (
            await db_session.scalar(select(func.count()).select_from(model).where(condition)) == 1
        )
    client.assert_closed()
    assert client.local.get(source_key) == PAYLOAD


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["workspace", "archive"])
async def test_real_absence_at_s3_lookup_still_produces_explicit_missing_manifest(
    auth_client, db_session, tmp_path, kind
):
    org_id, sha, client = await _seed_s3(auth_client, db_session, tmp_path)
    source_key = storage.content_key(documents.UPLOADS, org_id, sha)
    client.local.delete(source_key)
    export_id = await _request(auth_client, kind)
    completed = await _run(db_session, kind)
    assert completed.status == "succeeded", completed.last_error
    row = await db_session.get(_model(kind), export_id)
    assert row.status == "ready" and row.missing_documents == 1
    data = await documents.load(documents.EXPORTS, org_id, row.sha256)
    _assert_zip(data, kind, missing=True)
    client.assert_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["workspace", "archive"])
@pytest.mark.parametrize("fault", ["denied", "read", "truncated"])
async def test_s3_zip_read_failure_preserves_one_time_link_and_audit_until_same_link_recovers(
    auth_client, db_session, tmp_path, kind, fault
):
    org_id, original_sha, client = await _seed_s3(auth_client, db_session, tmp_path)
    export_id = await _request(auth_client, kind)
    assert (await _run(db_session, kind)).status == "succeeded"
    row = await db_session.get(_model(kind), export_id)
    export_sha, export_size = row.sha256, row.size
    key = storage.content_key(documents.EXPORTS, org_id, export_sha)
    original_zip = client.local.get(key)
    email = await db_session.scalar(
        select(EmailMessage).where(EmailMessage.kind == f"{kind}_export")
    )
    match = re.search(rf"/api/v1/{kind}/export/download/([A-Za-z0-9_-]+)", email.body)
    assert match is not None
    url = match.group(0)
    token = await db_session.scalar(
        select(EmailToken).where(EmailToken.purpose == f"{kind}_export")
    )
    action = (
        audit.A.WORKSPACE_EXPORT_DOWNLOADED
        if kind == "workspace"
        else audit.A.ARCHIVE_EXPORT_DOWNLOADED
    )
    client.faults[key] = fault
    refused = await auth_client.get(url)
    assert refused.status_code == 503, refused.text
    assert refused.json() == {
        "detail": "Stored document could not be read. Check storage and retry.",
        "code": "storage_read_unavailable",
    }
    assert refused.headers["cache-control"] == "private, no-store"
    assert "content-disposition" not in refused.headers
    assert all(value not in refused.text for value in (PRIVATE_DIAGNOSTIC, key, str(tmp_path)))
    await db_session.refresh(row)
    await db_session.refresh(token)
    assert row.status == "ready" and row.downloaded_at is None
    assert (row.sha256, row.size) == (export_sha, export_size) and token.used_at is None
    assert (
        await db_session.scalar(
            select(func.count()).select_from(AuditEvent).where(AuditEvent.action == action)
        )
        == 0
    )
    client.assert_closed()
    assert client.local.get(key) == original_zip

    client.faults.clear()
    recovered = await auth_client.get(url)
    assert recovered.status_code == 200 and recovered.content == original_zip
    assert recovered.headers["content-type"] == "application/zip"
    assert recovered.headers["content-disposition"].startswith("attachment;")
    assert recovered.headers["x-content-type-options"] == "nosniff"
    _assert_zip(recovered.content, kind, missing=False)
    await db_session.refresh(row)
    await db_session.refresh(token)
    assert row.status == "downloaded" and row.downloaded_at is not None
    assert token.used_at is not None and row.sha256 == export_sha
    assert (
        await db_session.scalar(
            select(func.count()).select_from(AuditEvent).where(AuditEvent.action == action)
        )
        == 1
    )
    before = len(client.lookups)
    assert (await auth_client.get(url)).status_code == 404
    assert len(client.lookups) == before
    client.assert_closed()
    assert client.local.get(storage.content_key(documents.UPLOADS, org_id, original_sha)) == PAYLOAD
