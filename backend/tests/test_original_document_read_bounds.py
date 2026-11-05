"""Stored originals use admitted metadata, never a later upload-policy ceiling."""

import io
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select

from app.core import storage
from app.models.archived_invoice import ArchivedInvoice
from app.models.audit import AuditEvent
from app.models.document import Document
from app.models.expense import ExpenseItem, ExpenseReport
from app.models.extraction_run import ExtractionRun
from app.models.invoice import Invoice
from app.models.invoice_collab import InvoiceAttachment
from app.models.vendor import Vendor
from app.services import audit, documents, filesec, issuer, modules
from tests.test_issued_attachment_read_bounds import ObservedBody

SURFACES = ["capture", "attachment", "logo", "receipt", "archive", "statement"]
ORIGINAL = b"Synthetic admitted original document.\n"


async def stored_original(client, db, surface, data=ORIGINAL):
    identity = (await client.get("/api/v1/auth/me")).json()
    org, user = identity["organization"]["id"], identity["user"]["id"]
    prefix = {
        "capture": documents.UPLOADS,
        "attachment": documents.INVOICE_ATTACHMENTS,
        "logo": documents.LOGOS,
        "receipt": documents.RECEIPTS,
        "archive": documents.UPLOADS,
        "statement": documents.STATEMENTS,
    }[surface]
    sha, size = await documents.store(
        prefix, org, data, "application/octet-stream", db=db, filename="synthetic.txt"
    )
    parent_id = None
    if surface == "capture":
        row = ExtractionRun(
            org_id=org, source_sha256=sha, source_filename="synthetic.txt", method="csv"
        )
        db.add(row)
        await db.flush()
        url = f"/api/v1/invoices/captures/{row.id}/source"
    elif surface == "attachment":
        vendor = Vendor(org_id=org, name="Synthetic original vendor")
        db.add(vendor)
        await db.flush()
        invoice = Invoice(
            org_id=org,
            vendor_id=vendor.id,
            invoice_number="SYN-ORIGINAL",
            issue_date=date(2026, 1, 1),
        )
        db.add(invoice)
        await db.flush()
        row = InvoiceAttachment(
            org_id=org,
            invoice_id=invoice.id,
            filename="synthetic.txt",
            mime="text/plain",
            size=size,
            sha256=sha,
        )
        db.add(row)
        await db.flush()
        parent_id = invoice.id
        url = f"/api/v1/invoices/{invoice.id}/attachments/{row.id}/download"
    elif surface == "logo":
        row = await issuer.get_or_create(db, org)
        row.logo_sha256, row.logo_size, row.logo_mime = sha, size, "image/png"
        url = "/api/v1/issuer/logo"
    elif surface == "receipt":
        await modules.set_enabled(db, org, "expenses", True)
        report = ExpenseReport(
            org_id=org,
            employee_id=user,
            employee_name="Synthetic Owner",
            title="Synthetic original report",
        )
        db.add(report)
        await db.flush()
        row = ExpenseItem(
            org_id=org,
            report_id=report.id,
            spend_date=date(2026, 1, 1),
            description="Synthetic receipt",
            receipt_sha256=sha,
            receipt_size=size,
        )
        db.add(row)
        await db.flush()
        parent_id = report.id
        url = f"/api/v1/expenses/{report.id}/items/{row.id}/receipt"
    elif surface == "archive":
        row = ArchivedInvoice(
            org_id=org,
            original_invoice_id=str(uuid4()),
            source_sha256=sha,
            source_filename="synthetic.txt",
            archived_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(days=365),
        )
        db.add(row)
        await db.flush()
        url = f"/api/v1/archive/{row.id}/document"
    else:
        row = await db.scalar(
            select(Document).where(
                Document.org_id == org, Document.kind == prefix, Document.sha256 == sha
            )
        )
        url = f"/api/v1/transport/statements/{sha}/file"
    await db.commit()
    return SimpleNamespace(
        surface=surface,
        org=org,
        user=user,
        row_id=row.id,
        model=type(row),
        parent_id=parent_id,
        prefix=prefix,
        sha=sha,
        size=size,
        key=storage.content_key(prefix, org, sha),
        url=url,
        data=data,
    )


def integrity_refused(response):
    assert response.status_code == 503, response.text
    assert "integrity" in response.json()["detail"].lower()
    assert "restore" in response.json()["detail"].lower()
    assert "content-disposition" not in response.headers


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", SURFACES)
async def test_original_growth_stops_after_admitted_size_plus_one(
    auth_client, db_session, tmp_path, monkeypatch, surface
):
    store = storage.LocalStorage(str(tmp_path))
    storage.set_storage(store)
    original = await stored_original(auth_client, db_session, surface)
    filename = store._path(original.key)
    with filename.open("r+b") as stream:
        stream.truncate(8 * 1024 * 1024)
    real_open = Path.open
    observed = []

    def observe(candidate, *args, **kwargs):
        stream = real_open(candidate, *args, **kwargs)
        if candidate == filename and args == ("rb",):
            stream = ObservedBody(stream)
            observed.append(stream)
        return stream

    monkeypatch.setattr(Path, "open", observe)
    response = await auth_client.get(original.url)
    integrity_refused(response)
    assert len(observed) == 1
    assert observed[0].consumed == original.size + 1, observed[0].requests
    assert all(0 < value <= 65536 for value in observed[0].requests)
    assert observed[0].source.closed
    assert str(tmp_path) not in response.text and original.key not in response.text
    assert filename.stat().st_size == 8 * 1024 * 1024
    db_session.expire_all()
    metadata = await db_session.scalar(
        select(Document).where(Document.kind == original.prefix, Document.sha256 == original.sha)
    )
    assert metadata.size == original.size
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.action == audit.A.DOC_DOWNLOAD)
        )
        == 0
    )
    store.put(original.key, original.data)
    restored = await auth_client.get(original.url)
    assert restored.status_code == 200 and restored.content == original.data
    assert restored.headers["x-content-type-options"] == "nosniff"


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("replacement", [b"X" * len(ORIGINAL), b"short"])
async def test_changed_or_truncated_original_is_never_a_success(
    auth_client, db_session, surface, replacement
):
    original = await stored_original(auth_client, db_session, surface)
    storage.get_storage().put(original.key, replacement)
    integrity_refused(await auth_client.get(original.url))
    storage.get_storage().put(original.key, original.data)
    restored = await auth_client.get(original.url)
    assert restored.status_code == 200 and restored.content == original.data


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", SURFACES)
async def test_remote_original_ignores_lying_length_and_closes_short_reads(
    auth_client, db_session, surface
):
    original = await stored_original(auth_client, db_session, surface)
    body = ObservedBody(io.BytesIO(original.data + b"extra synthetic bytes"), short_reads=True)
    store = object.__new__(storage.S3Storage)
    store._bucket, store._prefix = "synthetic-original-bucket", ""
    store._client = SimpleNamespace(get_object=lambda **kwargs: {"Body": body, "ContentLength": 1})
    storage.set_storage(store)
    integrity_refused(await auth_client.get(original.url))
    assert body.consumed == original.size + 1, body.requests
    assert all(0 < value <= 65536 for value in body.requests)
    assert body.source.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", SURFACES)
async def test_download_preserves_admitted_original_after_policy_reduction(
    auth_client, db_session, monkeypatch, surface
):
    monkeypatch.setattr(filesec.settings, "max_upload_mb", 3)
    original = await stored_original(auth_client, db_session, surface, b"x" * (2 * 1024 * 1024))
    monkeypatch.setattr(filesec.settings, "max_upload_mb", 1)
    response = await auth_client.get(original.url)
    assert response.status_code == 200 and response.content == original.data
    assert original.size > filesec.max_bytes()


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["capture", "archive", "logo", "receipt"])
async def test_nullable_historical_metadata_retains_read_and_hash_contract(
    auth_client, db_session, surface
):
    original = await stored_original(auth_client, db_session, surface)
    if surface in {"capture", "archive"}:
        await db_session.execute(delete(Document).where(Document.kind == original.prefix))
    else:
        row = await db_session.get(original.model, original.row_id)
        setattr(row, "logo_size" if surface == "logo" else "receipt_size", None)
    await db_session.commit()
    response = await auth_client.get(original.url)
    assert response.status_code == 200 and response.content == original.data
    storage.get_storage().put(original.key, b"x" * original.size)
    integrity_refused(await auth_client.get(original.url))


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", SURFACES)
async def test_zero_size_historical_original_and_invalid_negative_metadata(
    auth_client, db_session, monkeypatch, surface
):
    original = await stored_original(auth_client, db_session, surface, b"")
    calls = []
    real_get = storage.get_storage().get

    def get(key, **kwargs):
        calls.append(kwargs)
        return real_get(key, **kwargs)

    monkeypatch.setattr(storage.get_storage(), "get", get)
    response = await auth_client.get(original.url)
    assert response.status_code == 200 and response.content == b""
    assert calls == [{"max_bytes": 1}]
    row = await db_session.get(original.model, original.row_id)
    if surface in {"capture", "archive"}:
        row = await db_session.scalar(select(Document).where(Document.kind == original.prefix))
    setattr(row, {"logo": "logo_size", "receipt": "receipt_size"}.get(surface, "size"), -1)
    await db_session.commit()
    calls.clear()
    integrity_refused(await auth_client.get(original.url))
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("fault", ["missing", "interrupted"])
async def test_original_missing_and_interrupted_storage_remain_distinct(
    auth_client, db_session, monkeypatch, surface, fault
):
    original = await stored_original(auth_client, db_session, surface)
    if fault == "missing":
        storage.get_storage().delete(original.key)
    else:

        def interrupted(*args, **kwargs):
            raise storage.StorageReadError("private backend diagnostic")

        monkeypatch.setattr(storage.get_storage(), "get", interrupted)
    response = await auth_client.get(original.url)
    assert response.status_code == (404 if fault == "missing" else 503)
    assert "private backend" not in response.text
    assert "content-disposition" not in response.headers
