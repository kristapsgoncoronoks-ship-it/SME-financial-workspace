"""Email invoice intake — inbound address custody + attachment processing.

Each org gets a unique inbound address `<token>@<inbound-domain>`. An email
provider's inbound-parse webhook posts the message (with attachments) to the app;
every attachment is run through the SAME extraction engine (`parser.parse_invoice_file`)
as a manual upload and lands in a per-org review inbox as an `InboundInvoice`. A
user reviews the parsed draft and confirms it into a real `Invoice` — nothing is
persisted as an invoice without that human confirm (deterministic-first, review-gated).
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.email_intake import EmailIntake, InboundInvoice
from app.models.organization import Organization
from app.services import capture_failures, documents, filesec, jobs
from app.services.parser import parse_invoice_file

# Job kind for out-of-band attachment extraction (registered in job_handlers).
EXTRACT_KIND = "email.extract"


def _new_token() -> str:
    # URL/address-safe, lowercase hex — unambiguous as an email local-part.
    return secrets.token_hex(8)


def address_for(token: str) -> str:
    return f"{token}@{settings.inbound_email_domain}"


def token_from_address(addr: str | None) -> str | None:
    """Extract the token (local-part) from an inbound `to` address. Tolerates a
    `Name <token@domain>` form and a bare token."""
    if not addr:
        return None
    value = addr.strip()
    if "<" in value and ">" in value:
        value = value[value.index("<") + 1 : value.index(">")]
    value = value.strip()
    local = value.split("@", 1)[0].strip().lower()
    return local or None


async def get_or_create(db: AsyncSession, org_id: str) -> EmailIntake:
    row = await db.scalar(select(EmailIntake).where(EmailIntake.org_id == org_id))
    if row is not None:
        return row
    row = EmailIntake(org_id=org_id, token=_new_token())
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def rotate(db: AsyncSession, org_id: str) -> EmailIntake:
    row = await get_or_create(db, org_id)
    row.token = _new_token()
    await db.commit()
    await db.refresh(row)
    return row


async def resolve_org(db: AsyncSession, token: str | None) -> str | None:
    """Map an inbound token to its org id. Runs unscoped (the webhook is
    unauthenticated), so it can find any tenant's intake config."""
    if not token:
        return None
    return await db.scalar(select(EmailIntake.org_id).where(EmailIntake.token == token.lower()))


async def lock_delivery_workspace(db: AsyncSession, org_id: str) -> bool:
    """Serialize delivery admission until the caller's commit or rollback.

    A message has several attachment rows, so an attachment uniqueness rule
    cannot identify its delivery. The existing workspace row also covers old
    duplicate Message-IDs without rewriting them. NO KEY UPDATE serializes
    admissions while remaining compatible with child inserts' KEY SHARE locks.
    The pessimistic health commit releases this lock: callers must reacquire it
    and recheck the recipient/module/message before storing any attachment.
    SQLite ignores the row lock; concurrency qualification is PostgreSQL-only.
    """
    return (
        await db.scalar(
            select(Organization.id).where(Organization.id == org_id).with_for_update(key_share=True)
        )
        is not None
    )


def _method_for(filename: str) -> str:
    lower = filename.lower()
    for ext in ("pdf", "xml", "csv", "json"):
        if lower.endswith("." + ext):
            return ext
    return "unknown"


async def process_attachment(
    db: AsyncSession,
    org_id: str,
    *,
    from_addr: str | None,
    subject: str | None,
    filename: str,
    content_type: str | None,
    content: bytes,
    message_id: str | None = None,
) -> InboundInvoice:
    """Parse one email attachment into a review-inbox row (best-effort). A parse
    failure is recorded as a `failed` row (with the reason), never raised — one
    bad attachment must not drop the rest of the message.

    Security gate FIRST: a malicious attachment (malware, executable, script
    disguised as an invoice, wrong type) is marked `rejected` with the reason and
    its bytes are NOT stored — never parsed, never OCR'd, never vaulted."""
    sha = hashlib.sha256(content).hexdigest()
    row = InboundInvoice(
        org_id=org_id,
        from_addr=(from_addr or None) and from_addr[:320],
        subject=(subject or None) and subject[:500],
        message_id=(message_id or None) and message_id[:255],
        received_at=datetime.now(UTC),
        filename=(filename or "attachment")[:300],
        content_type=(content_type or None) and content_type[:120],
        size=len(content),
        sha256=sha,
        method=_method_for(filename or ""),
    )
    try:
        await filesec.check_async(filename or "attachment", content, allowed=filesec.INVOICE_KINDS)
    except filesec.FileRejected as exc:
        # Quarantine: keep the metadata for the audit trail, drop the bytes.
        row.status = "rejected"
        row.error = str(exc)
        row.failure_code = capture_failures.SECURITY_REJECTED
        row.failure_seq = (row.failure_seq or 0) + 1  # F-06: a new failure event
        db.add(row)
        return row

    # Passed the security gate → retain the original in object storage (keyed by
    # the sha256 already recorded), then hand extraction to the worker tier so a
    # burst of attachments never ties up the API with OCR (ADR-0009). The row is
    # QUEUED here; the worker parses it into a review draft (or marks it failed).
    await documents.store(
        documents.EMAIL_ATTACHMENTS,
        org_id,
        content,
        row.content_type,
        db=db,
        filename=getattr(row, "filename", None),
    )
    row.status = "queued"
    db.add(row)
    await db.flush()  # assign row.id before enqueuing the extract job
    await jobs.enqueue(
        db,
        EXTRACT_KIND,
        {"inbound_id": row.id},
        org_id=org_id,
        idempotency_key=f"extract:{row.id}",
        commit=False,
    )
    return row


def _extraction_base(row: InboundInvoice) -> tuple:
    """Facts the worker saw before asynchronous storage/parsing work."""
    return (
        row.org_id,
        row.status,
        row.sha256,
        row.filename,
        row.size,
        row.draft_json,
        row.reading_preparation_json,
        row.failure_seq,
        row.invoice_id,
        row.updated_at,
    )


async def extract_inbound(db: AsyncSession, inbound_id: str) -> dict:
    """Worker-side extraction of one queued inbound attachment (idempotent).

    Loads the stored bytes, runs the deterministic-first parser, and updates the
    review-inbox row to `pending` (a draft awaits review) or `failed` (with the
    reason). Runs in the job's tenant scope, off the API tier."""
    from app.services import image_ocr

    row = await db.scalar(
        select(InboundInvoice)
        .where(InboundInvoice.id == inbound_id)
        .execution_options(populate_existing=True)
    )
    if row is None:
        return {"skipped": "row gone"}
    if row.status not in ("queued", "failed"):
        return {"skipped": f"status={row.status}"}  # already parsed/confirmed

    base = _extraction_base(row)
    org_id, filename, source_sha = row.org_id, row.filename, row.sha256
    draft = None
    error = failure_code = failure_reason = None
    reading_preparation = None
    try:
        content = await documents.load_capture(documents.EMAIL_ATTACHMENTS, org_id, source_sha)
    except capture_failures.CaptureError as exc:
        content = None
        error, failure_code = str(exc), exc.code
        failure_reason = exc.code
    if content is None and failure_code is None:
        error, failure_code = "stored attachment missing", capture_failures.STORED_FILE_MISSING
        failure_reason = "missing bytes"

    if content is not None:
        try:
            draft = await image_ocr.run_reading(
                parse_invoice_file, filename or "attachment", content
            )
        except ValueError as exc:
            error, failure_code = str(exc), capture_failures.code_for(exc)
            reading_preparation = image_ocr.preparation_for_error(exc)

    # Parse without holding a document row lock, then join the same publication
    # boundary as confirm/discard/delete. Refresh the identity-map instance:
    # taking a lock alone must not leave the pre-parse status in memory.
    row = await db.scalar(
        select(InboundInvoice)
        .where(InboundInvoice.id == inbound_id, InboundInvoice.org_id == org_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        return {"skipped": "row gone"}
    if _extraction_base(row) != base:
        return {"skipped": "inbound changed"}
    if draft is not None:
        row.draft_json = draft.model_dump_json()
        row.method = draft.method if draft.method and draft.method != "unknown" else row.method
        row.status = "pending"
        row.error = None
        row.failure_code = None
        reading_preparation = draft.reading_preparation
    else:
        row.status = "failed"
        row.error = error
        row.failure_code = failure_code
        row.failure_seq = (row.failure_seq or 0) + 1
    row.reading_preparation_json = (
        reading_preparation.model_dump_json() if reading_preparation is not None else None
    )
    await db.commit()
    if failure_reason is not None:
        return {"status": "failed", "reason": failure_reason}
    return {"status": row.status, "method": row.method}


async def already_delivered(db: AsyncSession, org_id: str, message_id: str | None) -> bool:
    """BE-009: has a message with this provider Message-ID already been stored
    for this tenant? A provider retry — timeout on our side, a 5xx, a queue
    replay — re-posts the same message; without this every attachment landed
    in the inbox again. No Message-ID → no dedupe (never a guessed identity)."""
    if not message_id:
        return False
    row = await db.scalar(
        select(InboundInvoice.id)
        .where(InboundInvoice.org_id == org_id, InboundInvoice.message_id == message_id[:255])
        .limit(1)
    )
    return row is not None
