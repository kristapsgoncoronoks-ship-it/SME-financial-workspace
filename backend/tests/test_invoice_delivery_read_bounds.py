"""Frozen outbox attachment reads use their own admitted metadata and identity."""

from pathlib import Path

import pytest
from sqlalchemy import delete, func, select, update

from app.core import storage
from app.core.config import settings
from app.models.audit import AuditEvent
from app.models.document import Document
from app.models.email_message import EmailMessage
from app.models.issued_invoice import IssuedInvoice
from app.services import audit, documents, invoice_delivery, jobs, mailer
from tests.test_issued_attachment_read_bounds import ObservedBody
from tests.test_r011_invoice_commands import queue


async def intent(client, db, monkeypatch, content=None):
    invoice, message = await queue(client, monkeypatch)
    row = await db.get(EmailMessage, message["id"])
    if content is not None:
        row.attachment_sha256, _ = await documents.store(
            "invoice-delivery", row.org_id, content, "application/pdf", db=db
        )
        await db.commit()
    key = storage.content_key("invoice-delivery", row.org_id, row.attachment_sha256)
    original = storage.get_storage().get(key)
    return invoice, row, key, original


async def sent_audits(db):
    return await db.scalar(
        select(func.count()).select_from(AuditEvent).where(AuditEvent.action == audit.A.ISSUED_SENT)
    )


@pytest.mark.asyncio
async def test_sparse_delivery_growth_stops_at_admitted_size_and_preserves_frozen_intent(
    auth_client, db_session, tmp_path, monkeypatch
):
    store = storage.LocalStorage(str(tmp_path))
    storage.set_storage(store)
    _, row, key, original = await intent(auth_client, db_session, monkeypatch)
    frozen = (row.attachment_sha256, row.to_email, row.subject, row.body, row.command_hash)
    path = store._path(key)
    with path.open("r+b") as source:
        source.truncate(32 * 1024 * 1024)
    real_open, bodies, sends = Path.open, [], []

    def observed(candidate, *args, **kwargs):
        source = real_open(candidate, *args, **kwargs)
        if candidate == path and args == ("rb",):
            source = ObservedBody(source)
            bodies.append(source)
        return source

    monkeypatch.setattr(Path, "open", observed)
    monkeypatch.setattr(mailer, "_smtp_send", lambda *args: sends.append(args))
    with pytest.raises(jobs.PermanentJobError):
        await invoice_delivery.deliver(db_session, row.org_id, row.id)
    assert len(bodies) == 1 and bodies[0].consumed == len(original) + 1
    assert all(0 < size <= 65536 for size in bodies[0].requests) and bodies[0].source.closed
    assert path.stat().st_size == 32 * 1024 * 1024 and sends == []
    await db_session.refresh(row)
    assert row.status == "failed" and row.sent_at is None
    assert (row.attachment_sha256, row.to_email, row.subject, row.body, row.command_hash) == frozen
    assert await sent_audits(db_session) == 0
    # Restoring an object never automatically resends a failed frozen intent.
    store.put(key, original)
    assert await invoice_delivery.deliver(db_session, row.org_id, row.id) == {"status": "failed"}
    assert sends == []


@pytest.mark.asyncio
@pytest.mark.parametrize("size_delta", [-100000, 1])
async def test_invalid_delivery_admission_metadata_refuses_without_smtp(
    auth_client, db_session, monkeypatch, size_delta
):
    _, row, _key, original = await intent(auth_client, db_session, monkeypatch)
    await db_session.execute(
        update(Document)
        .where(Document.org_id == row.org_id, Document.kind == "invoice-delivery")
        .values(size=len(original) + size_delta)
    )
    await db_session.commit()
    calls, reads, real_load = [], [], documents.load

    async def observed(*args, **kwargs):
        reads.append(kwargs.get("max_bytes"))
        return await real_load(*args, **kwargs)

    monkeypatch.setattr(documents, "load", observed)
    monkeypatch.setattr(mailer, "_smtp_send", lambda *args: calls.append(args))
    with pytest.raises(jobs.PermanentJobError):
        await invoice_delivery.deliver(db_session, row.org_id, row.id)
    assert calls == [] and row.status == "failed" and await sent_audits(db_session) == 0
    if size_delta < 0:
        assert reads == []


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_metadata", [False, True])
async def test_historical_delivery_keeps_frozen_bytes_after_current_upload_cap_reduction(
    auth_client, db_session, monkeypatch, missing_metadata
):
    content = b"%PDF-1.4\n" + b"x" * (2 * 1024 * 1024)
    _, row, _key, _original = await intent(auth_client, db_session, monkeypatch, content)
    if missing_metadata:
        await db_session.execute(
            delete(Document).where(
                Document.org_id == row.org_id, Document.kind == "invoice-delivery"
            )
        )
        await db_session.commit()
    monkeypatch.setattr(settings, "max_upload_mb", 1)
    sends, reads, real_load = [], [], documents.load

    async def observed(*args, **kwargs):
        reads.append(kwargs.get("max_bytes"))
        return await real_load(*args, **kwargs)

    monkeypatch.setattr(documents, "load", observed)
    monkeypatch.setattr(mailer, "_smtp_send", lambda *args: sends.append(args))
    assert await invoice_delivery.deliver(db_session, row.org_id, row.id) == {"status": "sent"}
    assert reads == [None if missing_metadata else len(content)]
    assert len(sends) == 1 and sends[0][-1][1] == content
    assert row.status == "sent" and row.sent_at is not None and await sent_audits(db_session) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", ["changed", "short", "missing"])
async def test_bad_legacy_delivery_object_is_not_sent(
    auth_client, db_session, monkeypatch, replacement
):
    _, row, key, content = await intent(auth_client, db_session, monkeypatch)
    await db_session.execute(
        delete(Document).where(Document.org_id == row.org_id, Document.kind == "invoice-delivery")
    )
    await db_session.commit()
    store = storage.get_storage()
    if replacement == "missing":
        store.delete(key)
    else:
        store.put(key, b"X" + content[1:] if replacement == "changed" else content[:4])
    sends = []
    monkeypatch.setattr(mailer, "_smtp_send", lambda *args: sends.append(args))
    with pytest.raises(jobs.PermanentJobError):
        await invoice_delivery.deliver(db_session, row.org_id, row.id)
    assert sends == [] and row.status == "failed" and row.sent_at is None
    assert await sent_audits(db_session) == 0


@pytest.mark.asyncio
async def test_empty_historical_delivery_uses_nonzero_read_bound(
    auth_client, db_session, monkeypatch
):
    _, row, _key, _content = await intent(auth_client, db_session, monkeypatch, b"")
    sends, reads, real_load = [], [], documents.load

    async def observed(*args, **kwargs):
        reads.append(kwargs.get("max_bytes"))
        return await real_load(*args, **kwargs)

    monkeypatch.setattr(documents, "load", observed)
    monkeypatch.setattr(mailer, "_smtp_send", lambda *args: sends.append(args))
    assert await invoice_delivery.deliver(db_session, row.org_id, row.id) == {"status": "sent"}
    assert reads == [1] and len(sends) == 1 and sends[0][-1][1] == b""


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["lifecycle", "voided"])
async def test_delivery_refreshes_invoice_eligibility_after_attachment_io(
    auth_client, db_session, monkeypatch, changed
):
    from datetime import UTC, datetime

    invoice, row, _key, _content = await intent(auth_client, db_session, monkeypatch)
    sends, real_load = [], documents.load

    async def superseded(*args, **kwargs):
        content = await real_load(*args, **kwargs)
        values = (
            {"lifecycle": "cancelled"}
            if changed == "lifecycle"
            else {"voided_at": datetime.now(UTC)}
        )
        await db_session.execute(
            update(IssuedInvoice)
            .where(IssuedInvoice.id == invoice["id"])
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        return content

    monkeypatch.setattr(documents, "load", superseded)
    monkeypatch.setattr(mailer, "_smtp_send", lambda *args: sends.append(args))
    assert await invoice_delivery.deliver(db_session, row.org_id, row.id) == {"status": "blocked"}
    assert sends == [] and row.sent_at is None and await sent_audits(db_session) == 0
