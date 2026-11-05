"""Stored email downloads retain admitted bytes within a finite metadata bound."""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.core import storage
from app.models.audit import AuditEvent
from app.services import audit, documents, email_intake, filesec
from tests.test_issued_attachment_read_bounds import ObservedBody

ORIGINAL = b"Synthetic retained email original"


async def attachment(client, db, content=ORIGINAL):
    assert (
        await client.put("/api/v1/modules/email_intake", json={"enabled": True})
    ).status_code == 200
    org_id = (await client.get("/api/v1/auth/me")).json()["organization"]["id"]
    row = await email_intake.process_attachment(
        db,
        org_id,
        from_addr=None,
        subject=None,
        filename="source.csv",
        content_type="text/csv",
        content=b"description,quantity,unit_price\nSynthetic document,1,100\n",
    )
    sha, size = await documents.store(
        documents.EMAIL_ATTACHMENTS, org_id, content, "text/plain", db=db
    )
    row.sha256, row.size, row.filename, row.content_type = sha, size, "retained.csv", "text/plain"
    await db.commit()
    return (
        row,
        f"/api/v1/email/inbox/{row.id}/file",
        storage.content_key(documents.EMAIL_ATTACHMENTS, org_id, sha),
    )


async def downloads(db):
    return await db.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(AuditEvent.action == audit.A.DOC_DOWNLOAD)
    )


def refused(response):
    assert response.status_code == 503, response.text
    assert "integrity" in response.json()["detail"].lower()
    assert "restore" in response.json()["detail"].lower()
    assert "content-disposition" not in response.headers


@pytest.mark.asyncio
async def test_sparse_email_growth_reads_only_admitted_size_plus_one_and_recovers(
    auth_client, db_session, tmp_path, monkeypatch
):
    store = storage.LocalStorage(str(tmp_path))
    storage.set_storage(store)
    row, url, key = await attachment(auth_client, db_session)
    initial = (row.sha256, row.size, row.status, row.invoice_id)
    path = store._path(key)
    with path.open("r+b") as source:
        source.truncate(32 * 1024 * 1024)
    original_open = Path.open
    observed = []

    def observe(candidate, *args, **kwargs):
        source = original_open(candidate, *args, **kwargs)
        if candidate == path and args == ("rb",):
            source = ObservedBody(source)
            observed.append(source)
        return source

    monkeypatch.setattr(Path, "open", observe)
    response = await auth_client.get(url)
    refused(response)
    assert len(observed) == 1 and observed[0].consumed == len(ORIGINAL) + 1
    assert all(0 < size <= 65536 for size in observed[0].requests)
    assert observed[0].source.closed
    assert key not in response.text and str(tmp_path) not in response.text
    assert path.stat().st_size == 32 * 1024 * 1024
    assert await downloads(db_session) == 0
    await db_session.refresh(row)
    assert (row.sha256, row.size, row.status, row.invoice_id) == initial
    store.put(key, ORIGINAL)
    recovered = await auth_client.get(url)
    assert recovered.status_code == 200 and recovered.content == ORIGINAL
    assert observed[-1].source.closed
    assert recovered.headers["cache-control"] == "private, no-store"
    assert recovered.headers["content-disposition"].startswith("attachment;")
    assert recovered.headers["x-content-type-options"] == "nosniff"
    assert await downloads(db_session) == 1


@pytest.mark.asyncio
async def test_remote_email_short_reads_ignore_false_length_and_close(auth_client, db_session):
    row, url, key = await attachment(auth_client, db_session)
    body = ObservedBody(io.BytesIO(ORIGINAL + b"extra bytes"), short_reads=True)
    calls = []

    def get_object(**kwargs):
        calls.append(kwargs)
        return {"Body": body, "ContentLength": 1}

    store = object.__new__(storage.S3Storage)
    store._bucket, store._prefix = "synthetic-bucket", ""
    store._client = SimpleNamespace(get_object=get_object)
    storage.set_storage(store)
    response = await auth_client.get(url)
    refused(response)
    assert calls == [{"Bucket": "synthetic-bucket", "Key": key}]
    assert body.consumed == row.size + 1 and body.source.closed
    assert all(0 < size <= 65536 for size in body.requests)
    assert await downloads(db_session) == 0
    store._client = SimpleNamespace(
        get_object=lambda **kwargs: {"Body": io.BytesIO(ORIGINAL), "ContentLength": 99999999}
    )
    recovered = await auth_client.get(url)
    assert recovered.status_code == 200 and recovered.content == ORIGINAL


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", [b"X" + ORIGINAL[1:], b"short", None])
async def test_changed_truncated_or_missing_email_does_not_log_download(
    auth_client, db_session, replacement
):
    _, url, key = await attachment(auth_client, db_session)
    store = storage.get_storage()
    if replacement is None:
        store.delete(key)
    else:
        store.put(key, replacement)
    response = await auth_client.get(url)
    if replacement is None:
        assert response.status_code == 404, response.text
    else:
        refused(response)
    assert await downloads(db_session) == 0
    assert "content-disposition" not in response.headers
    store.put(key, ORIGINAL)
    recovered = await auth_client.get(url)
    assert recovered.status_code == 200 and recovered.content == ORIGINAL


@pytest.mark.asyncio
async def test_historical_email_survives_smaller_current_upload_policy(
    auth_client, db_session, monkeypatch
):
    content = b"x" * (2 * 1024 * 1024)
    row, url, _ = await attachment(auth_client, db_session, content)
    monkeypatch.setattr(filesec.settings, "max_upload_mb", 1)
    result = await auth_client.get(url)
    assert row.size > filesec.max_bytes()
    assert result.status_code == 200 and result.content == content
    assert storage.sha256_hex(result.content) == row.sha256


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [0, -1])
async def test_legacy_empty_and_invalid_email_metadata(auth_client, db_session, monkeypatch, size):
    row, url, _ = await attachment(auth_client, db_session, b"")
    row.size = size
    await db_session.commit()
    original = documents.load
    calls = []

    async def observe(*args, **kwargs):
        calls.append(kwargs)
        return await original(*args, **kwargs)

    monkeypatch.setattr(documents, "load", observe)
    response = await auth_client.get(url)
    if size == 0:
        assert response.status_code == 200 and response.content == b""
        assert calls == [{"max_bytes": 1}]
    else:
        refused(response)
        assert calls == []
        assert await downloads(db_session) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("hidden", ["unknown", "rejected", "no_reference", "other_tenant"])
async def test_unavailable_email_original_never_touches_storage(
    auth_client, db_session, role_client, monkeypatch, hidden
):
    from uuid import uuid4

    row, url, _ = await attachment(auth_client, db_session)
    if hidden == "unknown":
        url = url.replace(row.id, str(uuid4()))
    elif hidden == "rejected":
        row.status = "rejected"
        await db_session.commit()
    elif hidden == "no_reference":
        row.sha256 = None
        await db_session.commit()
    else:
        auth_client = await role_client("owner")
        assert (
            await auth_client.put("/api/v1/modules/email_intake", json={"enabled": True})
        ).status_code == 200
    calls = []

    async def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("Invisible email original reached object storage")

    monkeypatch.setattr(documents, "load", forbidden)
    response = await auth_client.get(url)
    assert response.status_code == 404 and calls == [], response.text
    assert await downloads(db_session) == 0


@pytest.mark.asyncio
async def test_email_read_failure_is_sanitized_and_not_recorded_as_download(
    auth_client, db_session, monkeypatch
):
    _, url, _ = await attachment(auth_client, db_session)
    original = documents.load

    async def unavailable(*args, **kwargs):
        raise storage.StorageReadError("Synthetic private bucket/path/credential diagnostic")

    monkeypatch.setattr(documents, "load", unavailable)
    response = await auth_client.get(url)
    assert response.status_code == 503 and response.json()["code"] == "storage_read_unavailable"
    assert "bucket/path/credential" not in response.text
    assert "content-disposition" not in response.headers
    assert await downloads(db_session) == 0
    monkeypatch.setattr(documents, "load", original)
    recovered = await auth_client.get(url)
    assert recovered.status_code == 200 and recovered.content == ORIGINAL


@pytest.mark.asyncio
async def test_existing_discarded_original_remains_downloadable(auth_client, db_session):
    row, url, _ = await attachment(auth_client, db_session)
    row.status = "discarded"
    await db_session.commit()
    result = await auth_client.get(url)
    assert result.status_code == 200 and result.content == ORIGINAL
    assert result.headers["cache-control"] == "private, no-store"
