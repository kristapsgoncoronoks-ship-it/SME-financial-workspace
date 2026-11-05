"""The platform archive — the CLIENT's own view of it.

`docs/design/platform-archive.md`. When an invoice's 30-day recycle bin expires,
the row is destroyed and a copy lands in the archive. The owner's decision is
that the client's own company owner can still see it there, for the retention
period, and download the source document.

That decision is what makes the archive defensible. A store clients cannot see
is retention done TO them and has to be explained; a store they can read is a
feature they use, and "your records are kept for N years and you can look at
them" is a sentence that goes in a DPA and an onboarding screen.

READ-ONLY. There is no restore-from-archive route and there should not be: the
bin restores into live books, the archive only shows. Pulling a three-year-old
invoice back into the ledger would reopen a closed accounting period and can
collide with invoice numbers issued since.

Router-level `ARCHIVE_READ`, which is held by OWNER and ADMINISTRATOR and by no
other business role. An archive holds the records a client believes they deleted;
"anyone who can read invoices" is the wrong audience for it.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from app.api.deps import CurrentUser, DbSession, refresh_current_identity, require_perm
from app.core import authz, storage
from app.core.security_headers import content_disposition
from app.schemas.archive import (
    ArchivedInvoiceOut,
    ArchiveExportOut,
    ArchiveListOut,
)
from app.services import archive as svc
from app.services import archive_export as export_svc
from app.services import document_registry, documents, plans

router = APIRouter(
    prefix="/archive",
    tags=["archive"],
    dependencies=[Depends(require_perm(authz.Permission.ARCHIVE_READ))],
)


def _out(row) -> ArchivedInvoiceOut:
    try:
        lines = json.loads(row.line_items_json or "[]")
    except ValueError:
        lines = []
    return ArchivedInvoiceOut(
        id=row.id,
        original_invoice_id=row.original_invoice_id,
        invoice_number=row.invoice_number,
        vendor_name=row.vendor_name,
        issue_date=row.issue_date.isoformat() if row.issue_date else None,
        currency=row.currency,
        total=str(row.total) if row.total is not None else None,
        line_items=lines,
        has_document=bool(row.source_sha256),
        source_filename=row.source_filename,
        original_deleted_at=row.original_deleted_at.isoformat()
        if row.original_deleted_at
        else None,
        original_deleted_by=row.original_deleted_by,
        archived_at=row.archived_at.isoformat(),
        expires_at=row.expires_at.isoformat(),
    )


@router.get("", response_model=ArchiveListOut)
async def list_archive(
    current: CurrentUser,
    db: DbSession,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """This organisation's archive, newest first.

    Paged from the first version, deliberately: the recycle-bin screen shipped
    without asking for a page size, took the server's default, and then printed
    the unpaginated total above a truncated table — a screen stating something
    untrue about deleted records. An archive holding years of them would make the
    same mistake worse.
    """
    page = await svc.page(db, current.org_id, limit=limit, offset=offset)
    return ArchiveListOut(
        items=[_out(r) for r in page.items],
        total=page.total,
        retention_years=page.retention_years,
        expiry_notice_days=page.expiry_notice_days,
        # WO-AD: what an upgrade buys, read from the ladder — never restated here.
        longest_plan_retention_years=plans.longest_archive_retention_years(),
    )


def _export_out(row) -> ArchiveExportOut:
    iso = lambda d: d.isoformat() if d else None  # noqa: E731 - four fields, one shape
    return ArchiveExportOut(
        id=row.id,
        status=row.status,
        requested_email=row.requested_email,
        created_at=row.created_at.isoformat(),
        ready_at=iso(row.ready_at),
        link_expires_at=iso(row.link_expires_at),
        downloaded_at=iso(row.downloaded_at),
        records=row.records,
        missing_documents=row.missing_documents,
        size=row.size,
    )


@router.post("/export", response_model=ArchiveExportOut, status_code=status.HTTP_202_ACCEPTED)
async def request_archive_export(current: CurrentUser, db: DbSession):
    """WO-AI — ask for the whole archive as one zip (manifest, records, every
    source document that still has its bytes). The build runs on the worker
    and the ONE-TIME download link is emailed to the requesting owner; it is
    never returned here. Owner only: the archive is the company's, and the
    owner decision names the owner as the one who takes it when they leave.
    A second request inside an hour reuses the live one."""
    if authz.business_role(current) is not authz.Role.OWNER:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Only the workspace owner can export the archive"
        )
    row = await export_svc.request_for_org(
        db, current.org_id, email=current.email, user_id=current.id
    )
    await db.commit()
    return _export_out(row)


@router.get("/export-requests", response_model=list[ArchiveExportOut])
async def list_archive_exports(current: CurrentUser, db: DbSession):
    """Every export request of the workspace, newest first — status only."""
    return [_export_out(r) for r in await export_svc.list_requests(db, current.org_id)]


@router.get("/{archive_id}", response_model=ArchivedInvoiceOut)
async def get_archived(archive_id: str, current: CurrentUser, db: DbSession):
    row = await svc.get(db, current.org_id, archive_id)
    if row is None:
        # Opaque: unknown and cross-tenant are indistinguishable (§4.4).
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return _out(row)


@router.get("/{archive_id}/document")
async def download_archived_document(
    archive_id: str, current: CurrentUser, db: DbSession, request: Request
):
    """The original invoice PDF, kept with the record.

    This is most of the value of the archive: the record says what the invoice
    was, the document proves it to a tax authority. Served inert (attachment +
    nosniff) exactly as the live document routes are — an archived file is still
    attacker-influenced bytes that once came in over the internet.
    """
    row = await svc.get(db, current.org_id, archive_id)
    if row is None or not row.source_sha256:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    sha = row.source_sha256
    doc = await document_registry.find(db, current.org_id, sha256=sha, kind=documents.UPLOADS)
    size = doc.size if doc is not None else None
    try:
        data = await documents.load_verified_original(
            documents.UPLOADS, current.org_id, sha, size=size
        )
    except storage.StorageReadError:
        raise
    except storage.StorageError:
        # The row outlived its bytes. A 404 says so without leaking storage
        # internals; the integrity sweep is where this should be noticed.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Stored document missing") from None
    fresh, _ = await refresh_current_identity(request, db, current)
    authz.require(fresh, authz.Permission.ARCHIVE_READ)
    row = await svc.get(db, current.org_id, archive_id)
    if row is None or row.source_sha256 != sha:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Archived document changed while preparing the download"
        )
    doc = await document_registry.find(db, current.org_id, sha256=sha, kind=documents.UPLOADS)
    if doc is not None and data is not None and len(data) != doc.size:
        raise documents.OriginalIntegrityError()
    if data is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Stored document missing")
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": content_disposition(
                row.source_filename or "document", fallback="attachment"
            ),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )
