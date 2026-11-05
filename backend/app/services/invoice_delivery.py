"""Frozen delivery intent on the existing outbox and durable worker queue.

SMTP cannot prove recipient delivery or exactly-once acceptance. A worker crash
after a send started is an unknown outcome, never an automatic second send.
"""

from datetime import UTC, datetime

from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import storage
from app.core.config import settings
from app.core.errors import ConflictError, NotFoundError
from app.models.email_message import EmailMessage
from app.models.issued_invoice import IssuedInvoice
from app.services import audit, document_registry, documents, issued_commands, jobs, mailer

KIND = "invoice.deliver"


async def _invoice(db: AsyncSession, org_id: str, invoice_id: str | None) -> IssuedInvoice | None:
    return await db.scalar(
        select(IssuedInvoice)
        .where(IssuedInvoice.org_id == org_id, IssuedInvoice.id == invoice_id)
        .execution_options(populate_existing=True)
    )


def _eligible(invoice: IssuedInvoice | None) -> bool:
    return (
        invoice is not None
        and invoice.voided_at is None
        and invoice.lifecycle in {"issued", "disputed", "written_off"}
    )


async def lookup(db: AsyncSession, org_id: str, key: str) -> EmailMessage | None:
    return await db.scalar(
        select(EmailMessage).where(EmailMessage.org_id == org_id, EmailMessage.command_key == key)
    )


async def queue(
    db: AsyncSession,
    org_id: str,
    *,
    invoice: IssuedInvoice,
    key: str,
    recipient: str,
    resend: bool,
    subject: str,
    body: str,
    pdf: bytes,
) -> EmailMessage:
    digest = issued_commands.fingerprint(
        {"invoice_id": invoice.id, "recipient": recipient, "resend": resend}
    )
    message = await lookup(db, org_id, key)
    if message is None and not resend:
        message = await db.scalar(
            select(EmailMessage).where(
                EmailMessage.org_id == org_id,
                EmailMessage.invoice_id == invoice.id,
                EmailMessage.is_primary.is_(True),
            )
        )
    if message is not None:
        if message.command_hash != digest:
            raise ConflictError("Delivery command inputs changed.", code="command_payload_conflict")
        if (
            message.status != "blocked"
            or not settings.smtp_enabled
            or message.attachment_sha256 is not None
        ):
            return message
    else:
        message = EmailMessage(
            org_id=org_id,
            invoice_id=invoice.id,
            is_primary=not resend,
            command_key=key,
            command_hash=digest,
            kind="invoice",
            to_email=recipient,
            subject=subject,
            body=body,
            from_email=settings.smtp_from,
            status="blocked",
        )
        db.add(message)
        await db.flush()
    if not settings.smtp_enabled:
        message.error = "SMTP is not configured. Configure the mail relay, then retry this command."
        return message
    # Freeze the PDF before enqueueing. The row, registry, job and queue audit
    # commit in the caller's transaction; no SMTP runs on the request path.
    message.attachment_filename = f"{invoice.number}.pdf"
    message.attachment_sha256, _ = await documents.store(
        "invoice-delivery",
        org_id,
        pdf,
        "application/pdf",
        db=db,
        filename=message.attachment_filename,
    )
    message.status = "queued"
    message.error = None
    await jobs.enqueue(
        db,
        KIND,
        {"message_id": message.id},
        org_id=org_id,
        idempotency_key=message.id,
        commit=False,
    )
    event = await audit.record(
        db,
        audit.A.ISSUED_DELIVERY_QUEUED,
        org_id=org_id,
        target_type="issued_invoice",
        target_id=invoice.id,
        meta={"message_id": message.id},
    )
    if event is None:
        raise RuntimeError("Delivery queue audit could not be recorded.")
    return message


async def deliver(db: AsyncSession, org_id: str, message_id: str) -> dict:
    message = await db.scalar(
        select(EmailMessage)
        .where(EmailMessage.org_id == org_id, EmailMessage.id == message_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if message is None:
        raise NotFoundError("Delivery intent not found.")
    if message.status in {"sent", "unknown", "failed", "blocked"}:
        return {"status": message.status}
    if message.status == "sending":
        message.status = "unknown"
        message.error = "A previous attempt ended without a confirmed outcome. Check the relay before resending."
        await db.commit()
        return {"status": "unknown"}
    if message.status != "queued":
        raise jobs.PermanentJobError("Delivery intent is not queued.")
    if not settings.smtp_enabled:
        message.status = "blocked"
        message.error = "SMTP is not configured. Restore the mail relay and request a new delivery."
        await db.commit()
        return {"status": "blocked"}
    invoice = await _invoice(db, org_id, message.invoice_id)
    if not _eligible(invoice):
        message.status = "blocked"
        message.error = "The invoice is no longer eligible for delivery. Review its current state."
        await db.commit()
        return {"status": "blocked"}
    size = await document_registry.admitted_size(
        db, org_id, sha256=message.attachment_sha256, kind="invoice-delivery"
    )
    try:
        pdf = await documents.load_verified_original(
            "invoice-delivery", org_id, message.attachment_sha256, size=size
        )
    except documents.OriginalIntegrityError as exc:
        message.status = "failed"
        message.error = "The delivery attachment failed its integrity check. Restore storage before requesting a new delivery."
        await db.commit()
        raise jobs.PermanentJobError("Delivery attachment integrity check failed.") from exc
    except storage.StorageError as exc:
        message.status = "failed"
        message.error = (
            "The delivery attachment is unavailable. Restore storage or request a new delivery."
        )
        await db.commit()
        raise jobs.PermanentJobError("Delivery attachment is unavailable.") from exc
    if pdf is None:
        message.status = "failed"
        message.error = "The delivery attachment is missing. Request a new delivery."
        await db.commit()
        raise jobs.PermanentJobError("Delivery attachment is missing.")
    if (
        await document_registry.admitted_size(
            db, org_id, sha256=message.attachment_sha256, kind="invoice-delivery"
        )
        != size
    ):
        message.status = "failed"
        message.error = "The delivery attachment failed its integrity check. Restore storage before requesting a new delivery."
        await db.commit()
        raise jobs.PermanentJobError("Delivery attachment integrity check failed.")
    # The message lock protects its frozen intent; invoice eligibility and
    # admission metadata may change while the object store is being read.
    invoice = await _invoice(db, org_id, message.invoice_id)
    if not _eligible(invoice):
        message.status = "blocked"
        message.error = "The invoice is no longer eligible for delivery. Review its current state."
        await db.commit()
        return {"status": "blocked"}
    recipient, subject, body = message.to_email, message.subject, message.body
    sender, filename = (
        message.from_email or settings.smtp_from,
        message.attachment_filename or "invoice.pdf",
    )
    message.status = "sending"
    await db.commit()  # persisted BEFORE external I/O; a reclaimed attempt cannot resend blindly
    try:
        await run_in_threadpool(
            mailer._smtp_send, sender, recipient, subject, body, (filename, pdf)
        )
    except Exception:
        # A connection loss can happen after SMTP accepted the message. Do not
        # claim failure or automatically retry a potentially completed send.
        message.status = "unknown"
        message.error = (
            "SMTP acceptance is uncertain. Check the relay log before requesting a resend."
        )
        await db.commit()
        return {"status": "unknown"}
    message.status = "sent"
    message.error = None
    message.sent_at = datetime.now(UTC)
    invoice = await db.scalar(
        select(IssuedInvoice)
        .where(IssuedInvoice.org_id == org_id, IssuedInvoice.id == message.invoice_id)
        .with_for_update()
    )
    if invoice is not None and invoice.sent_at is None:
        invoice.sent_at = message.sent_at
    event = await audit.record(
        db,
        audit.A.ISSUED_SENT,
        org_id=org_id,
        target_type="issued_invoice",
        target_id=message.invoice_id,
        meta={"message_id": message.id, "status": "sent"},
    )
    if event is None:
        raise RuntimeError("Delivery acceptance audit could not be recorded.")
    await db.commit()
    return {"status": "sent"}
