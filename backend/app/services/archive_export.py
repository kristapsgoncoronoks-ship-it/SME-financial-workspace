"""WO-AI — the ex-client archive export (owner decision 2026-08-16 §1.C).

The archive SURVIVES a client who leaves, for the full retention period, and
"an ex-client can request a one-time EXPORT of their archive; no live login is
retained". Two consequences the decision itself named as worth building for:
the ex-client's owner cannot log in, and the pre-expiry notice still goes to
the last recorded owner address. This module is the export half:

- A LIVE owner asks from the Archive screen (`request_for_org`); an EX-client's
  owner asks by email from a public form (`request_by_email`) — the address
  must be an owner's of that workspace, and the response never says whether it
  was (the forgot-password posture: no enumeration, always "sent").
- The worker builds ONE zip for the whole archive (`build_zip_file`): a
  `manifest.csv`, a `records.json` with every field and line item, and every
  source document that still has its bytes. A record whose bytes are gone is
  listed as `missing`, never dropped, never a reason to fail the export.
- The zip lives in the `exports` document class and the owner gets a ONE-TIME
  link — an `EmailToken` with purpose `archive_export` (hashed, single use,
  seven days), the same credential a password reset uses. Opening the link
  consumes it; a second open is a 404. Nothing here retains a login.

Never inline in a request (R60): the build runs on the worker
(`job_handlers.ARCHIVE_EXPORT`).
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import tempfile
import zipfile
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.csv_safety import sanitize_cell
from app.core.storage import StorageError, StorageReadError
from app.models.archive_export import ArchiveExport
from app.models.archived_invoice import ArchivedInvoice
from app.models.email_token import PURPOSE_ARCHIVE_EXPORT
from app.models.membership import Membership
from app.models.organization import Organization
from app.models.user import User, UserRole
from app.services import (
    audit,
    document_registry,
    documents,
    export_artefacts,
    jobs,
    mailer,
    verification,
)

log = logging.getLogger("invoiceiq.archive_export")

ARCHIVE_EXPORT_KIND = "archive.export"
LINK_TTL = timedelta(days=7)
# A second request for the same address inside this window reuses the first —
# the public form must not be a way to flood an owner's inbox.
REQUEST_COALESCE = timedelta(hours=1)
MANIFEST = "manifest.csv"
RECORDS = "records.json"

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_name(name: str | None, fallback: str) -> str:
    cleaned = _SAFE.sub("_", name or "").strip("._")
    return cleaned or fallback


async def owner_users(db: AsyncSession, org_id: str) -> list[User]:
    """The workspace's owners, most recent membership first — ACTIVE or not.
    For a live tenant that is the accountable audience; for an ex-client it is
    the last recorded owner, which is exactly whom the decision names."""
    rows = await db.execute(
        select(User, Membership.status)
        .join(Membership, Membership.user_id == User.id)
        .where(Membership.org_id == org_id, Membership.role == UserRole.owner)
        .order_by(Membership.created_at.desc())
    )
    seen: set[str] = set()
    out: list[User] = []
    for user, _status in rows:
        if user.id not in seen:
            seen.add(user.id)
            out.append(user)
    return out


async def _recent_request(db: AsyncSession, org_id: str, email: str) -> ArchiveExport | None:
    since = datetime.now(UTC) - REQUEST_COALESCE
    rows = list(
        await db.scalars(
            select(ArchiveExport)
            .where(
                ArchiveExport.org_id == org_id,
                ArchiveExport.requested_email == email,
                ArchiveExport.status.in_(("queued", "ready")),
            )
            .order_by(ArchiveExport.created_at.desc())
        )
    )
    for row in rows:
        created = row.created_at
        if created.tzinfo is None:  # SQLite returns naive datetimes
            created = created.replace(tzinfo=UTC)
        if created >= since:
            return row
    return None


async def request_for_org(
    db: AsyncSession, org_id: str, *, email: str, user_id: str
) -> ArchiveExport:
    """Record one request and queue the build. Coalesces with a live request
    for the same address made in the last hour. Does not commit."""
    email = email.strip().lower()
    existing = await _recent_request(db, org_id, email)
    if existing is not None:
        return existing
    row = ArchiveExport(
        org_id=org_id, requested_email=email, requested_by_user_id=user_id, status="queued"
    )
    db.add(row)
    await db.flush()
    job = await jobs.enqueue(
        db, ARCHIVE_EXPORT_KIND, {"export_id": row.id}, org_id=org_id, commit=False
    )
    row.job_id = job.id
    await audit.record(
        db,
        audit.A.ARCHIVE_EXPORT_REQUESTED,
        target_type="archive_export",
        target_id=row.id,
        meta={"to": email},
        org_id=org_id,
        actor=(user_id, email),
    )
    return row


async def request_by_email(db: AsyncSession, email: str) -> int:
    """The public door: every workspace whose OWNER (live or last recorded) this
    address belongs to gets a request. Returns how many — for the log only;
    the route answers the same way whatever the number."""
    email = email.strip().lower()
    users = list(await db.scalars(select(User).where(User.email == email)))
    count = 0
    for user in users:
        org_ids = list(
            await db.scalars(
                select(Membership.org_id).where(
                    Membership.user_id == user.id, Membership.role == UserRole.owner
                )
            )
        )
        for org_id in org_ids:
            await request_for_org(db, org_id, email=email, user_id=user.id)
            count += 1
    return count


async def list_requests(db: AsyncSession, org_id: str) -> list[ArchiveExport]:
    return list(
        await db.scalars(
            select(ArchiveExport)
            .where(ArchiveExport.org_id == org_id)
            .order_by(ArchiveExport.created_at.desc())
        )
    )


async def build_zip_file(db: AsyncSession, org_id: str) -> tuple[str, int, int]:
    """(owned ZIP path, records, missing documents). Caller removes the file.

    Every archived record is retained; a missing document is listed, not dropped.
    """
    org = await db.get(Organization, org_id)
    rows = list(
        await db.scalars(
            select(ArchivedInvoice)
            .where(ArchivedInvoice.org_id == org_id)
            .order_by(ArchivedInvoice.archived_at, ArchivedInvoice.id)
        )
    )
    manifest = io.StringIO()
    writer = csv.writer(manifest)
    writer.writerow(
        [
            "archive_id",
            "original_invoice_id",
            "invoice_number",
            "vendor_name",
            "issue_date",
            "currency",
            "subtotal",
            "tax_amount",
            "total",
            "archived_at",
            "expires_at",
            "document",
            "document_status",
        ]
    )
    records: list[dict] = []
    missing = 0
    descriptor, path = tempfile.mkstemp(prefix="archive-export-", suffix=".zip")
    os.close(descriptor)
    try:
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for r in rows:
                doc_path = ""
                doc_status = "none"
                if r.source_sha256:
                    doc = await document_registry.find(
                        db, org_id, sha256=r.source_sha256, kind=documents.UPLOADS
                    )
                    try:
                        source = await documents.load_verified_file(
                            documents.UPLOADS,
                            org_id,
                            r.source_sha256,
                            size=doc.size if doc is not None else None,
                        )
                    except StorageReadError:
                        # A read failure does not prove the bytes are gone, and
                        # must not produce a partial ready export.
                        raise
                    except StorageError:
                        source = None
                    if source is None:
                        doc_status = "missing"
                        missing += 1
                    else:
                        doc_path = f"documents/{r.id}-{_safe_name(r.source_filename, 'document')}"
                        try:
                            zf.write(source.path, doc_path)
                        finally:
                            source.cleanup()
                        doc_status = "included"
                try:
                    lines = json.loads(r.line_items_json or "[]")
                except ValueError:
                    lines = []
                record = {
                    "archive_id": r.id,
                    "original_invoice_id": r.original_invoice_id,
                    "invoice_number": r.invoice_number,
                    "vendor_name": r.vendor_name,
                    "issue_date": r.issue_date.isoformat() if r.issue_date else None,
                    "currency": r.currency,
                    "subtotal": str(r.subtotal) if r.subtotal is not None else None,
                    "tax_amount": str(r.tax_amount) if r.tax_amount is not None else None,
                    "total": str(r.total) if r.total is not None else None,
                    "line_items": lines,
                    "original_deleted_at": (
                        r.original_deleted_at.isoformat() if r.original_deleted_at else None
                    ),
                    "original_deleted_by": r.original_deleted_by,
                    "archived_at": r.archived_at.isoformat() if r.archived_at else None,
                    "expires_at": r.expires_at.isoformat() if r.expires_at else None,
                    "document": doc_path or None,
                    "document_status": doc_status,
                }
                records.append(record)
                writer.writerow(
                    [
                        sanitize_cell(record[k])
                        for k in (
                            "archive_id",
                            "original_invoice_id",
                            "invoice_number",
                            "vendor_name",
                            "issue_date",
                            "currency",
                            "subtotal",
                            "tax_amount",
                            "total",
                            "archived_at",
                            "expires_at",
                            "document",
                            "document_status",
                        )
                    ]
                )
            zf.writestr(MANIFEST, manifest.getvalue())
            zf.writestr(
                RECORDS,
                json.dumps(
                    {
                        "workspace": org.name if org else None,
                        "exported_at": datetime.now(UTC).isoformat(),
                        "records": records,
                    },
                    indent=2,
                    default=str,
                ),
            )
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            log.warning("Could not remove a private archive export staging file")
        raise
    return path, len(rows), missing


def _download_link(raw: str) -> str:
    return f"{settings.app_base_url.rstrip('/')}/api/v1/archive/export/download/{raw}"


async def run_export(db: AsyncSession, org_id: str, export_id: str) -> dict:
    """The worker's half: build, store, issue the one-time link, email it.
    Marks the request `failed` with the reason on any error. Does not commit."""
    row = await db.scalar(
        select(ArchiveExport).where(ArchiveExport.org_id == org_id, ArchiveExport.id == export_id)
    )
    if row is None:
        raise LookupError(f"archive export {export_id} not found for org {org_id}")
    user = await db.get(User, row.requested_by_user_id)
    if user is None:
        row.status = "failed"
        row.error = "requesting owner no longer exists"
        return {"export_id": row.id, "status": row.status}
    path, records, missing = await build_zip_file(db, org_id)
    try:
        org = await db.get(Organization, org_id)
        filename = f"archive-{_safe_name(org.name if org else None, 'workspace')}.zip"
        sha, size = await documents.store_file(
            documents.EXPORTS, org_id, path, "application/zip", db=db, filename=filename
        )
    finally:
        try:
            os.unlink(path)
        except OSError:
            log.warning("Could not remove a private archive export staging file")
    now = datetime.now(UTC)
    row.sha256, row.size, row.records, row.missing_documents = sha, size, records, missing
    row.status = "ready"
    row.ready_at = now
    row.link_expires_at = now + LINK_TTL
    raw = await verification.issue(db, user, PURPOSE_ARCHIVE_EXPORT, LINK_TTL)
    subject, body = mailer.archive_export_email(
        workspace=org.name if org else "your workspace",
        link=_download_link(raw),
        records=records,
        missing_documents=missing,
        ttl_days=LINK_TTL.days,
    )
    await mailer.send(
        db, org_id, kind="archive_export", to_email=row.requested_email, subject=subject, body=body
    )
    return {
        "export_id": row.id,
        "status": row.status,
        "records": records,
        "missing_documents": missing,
        "bytes": size,
        "sha256": sha,
    }


async def download(db: AsyncSession, raw: str) -> tuple[documents.StoredFile, str, str] | None:
    """Consume the one-time link and hand back (staged file, filename, org_id) — or
    None for an unknown, used or expired link, indistinguishably. UNSCOPED:
    the public route has no tenant; the token names it."""
    token = await verification.consume(db, raw, PURPOSE_ARCHIVE_EXPORT)
    if token is None:
        return None
    row = await db.scalar(
        select(ArchiveExport)
        .where(
            ArchiveExport.org_id == token.org_id,
            ArchiveExport.requested_by_user_id == token.user_id,
            ArchiveExport.status == "ready",
            ArchiveExport.sha256.is_not(None),
        )
        .order_by(ArchiveExport.ready_at.desc())
        .limit(1)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None or row.sha256 is None:
        return None
    try:
        staged = await export_artefacts.prepare_file(row.org_id, row.sha256, row.size)
    except StorageReadError:
        # Preserve the token/download transaction for a retry after recovery.
        raise
    except StorageError:
        staged = None
    if staged is None:
        log.error("archive export %s: stored zip %s is missing", row.id, row.sha256)
        return None
    try:
        row.status = "downloaded"
        row.downloaded_at = datetime.now(UTC)
        org = await db.get(Organization, row.org_id)
        await audit.record(
            db,
            audit.A.ARCHIVE_EXPORT_DOWNLOADED,
            target_type="archive_export",
            target_id=row.id,
            meta={"bytes": row.size, "records": row.records},
            org_id=row.org_id,
            actor=(token.user_id, row.requested_email),
        )
        return (
            staged,
            f"archive-{_safe_name(org.name if org else None, 'workspace')}.zip",
            row.org_id,
        )
    except BaseException:
        staged.cleanup()
        raise
