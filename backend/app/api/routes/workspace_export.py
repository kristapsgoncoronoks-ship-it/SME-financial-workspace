"""PROD-009 — the whole-workspace data export.

Three doors, and no public way to ASK for one. WO-AI's archive export has an
unauthenticated email form because an ex-client's login is gone by definition;
this export is a live tenant's ENTIRE dataset, so requesting one requires being
signed in as an owner of that workspace, and the only thing that fetches one is
the single-use link emailed to that owner's own address.

- `POST /workspace/export` — owner only, 202, coalesced for an hour.
- `GET  /workspace/export-requests` — the request log; status only, never a link.
- `GET  /workspace/export/download/{token}` — the one-time link from the email.
  Unauthenticated BY THE TOKEN, which is an `EmailToken` (hashed, single use,
  seven days, issued to that owner): a used, expired, purged or unknown one is
  the same opaque 404. Served inert (attachment + nosniff) like every other
  stored byte.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import CurrentUser, DbSession, require_perm
from app.api.file_response import TemporaryFileResponse
from app.core import authz
from app.core.security_headers import content_disposition
from app.models.workspace_export import WorkspaceExport
from app.schemas.workspace_export import WorkspaceExportOut
from app.services import workspace_export as export_svc

router = APIRouter(
    prefix="/workspace",
    tags=["workspace"],
    dependencies=[Depends(require_perm(authz.Permission.SETTINGS_MANAGE))],
)
#: The one-time download link carries its own credential (a hashed, single-use
#: `EmailToken`), so it cannot sit under the permission dependency above — the
#: person opening it is following a link from their inbox, not a session. It is
#: on the reasoned `PUBLIC_ROUTES` allow-list beside WO-AI's, for the same
#: reason and with the same posture: unknown, used, expired and purged are all
#: the same opaque 404.
public_router = APIRouter(prefix="/workspace", tags=["workspace"])


def _out(row: WorkspaceExport) -> WorkspaceExportOut:
    return WorkspaceExportOut(
        id=row.id,
        status=row.status,
        requested_email=row.requested_email,
        created_at=row.created_at.isoformat(),
        ready_at=row.ready_at.isoformat() if row.ready_at else None,
        link_expires_at=row.link_expires_at.isoformat() if row.link_expires_at else None,
        downloaded_at=row.downloaded_at.isoformat() if row.downloaded_at else None,
        purged_at=row.purged_at.isoformat() if row.purged_at else None,
        rows=row.rows,
        tables=row.tables,
        documents=row.documents,
        missing_documents=row.missing_documents,
        size=row.size,
        error=row.error,
    )


@router.post("/export", response_model=WorkspaceExportOut, status_code=status.HTTP_202_ACCEPTED)
async def request_workspace_export(current: CurrentUser, db: DbSession):
    """Ask for everything this workspace holds as one zip: every table as JSON
    lines, every stored file, a manifest. The build runs on the worker and the
    ONE-TIME link is emailed to the requesting owner; it is never returned here,
    so reading the API is not a way to get one.

    Owner only. This is the whole company's data in one file — including every
    member's rows — and the owner is the person who is entitled to take it.
    A second request inside an hour reuses the live one: building a workspace
    is the most expensive thing a tenant can ask the worker to do."""
    if authz.business_role(current) is not authz.Role.OWNER:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Only the workspace owner can export the workspace"
        )
    row = await export_svc.request_for_org(
        db, current.org_id, email=current.email, user_id=current.id
    )
    await db.commit()
    return _out(row)


@router.get("/export-requests", response_model=list[WorkspaceExportOut])
async def list_workspace_exports(current: CurrentUser, db: DbSession):
    """Every export request of the workspace, newest first — status only."""
    return [_out(r) for r in await export_svc.list_requests(db, current.org_id)]


@public_router.get("/export/download/{token}")
async def download_workspace_export(token: str, db: DbSession):
    found = await export_svc.download(db, token)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invalid, used or expired export link")
    staged, filename, _org_id = found
    try:
        await db.commit()  # the consumed token and the downloaded stamp
        return TemporaryFileResponse(
            staged,
            media_type="application/zip",
            headers={
                "Content-Disposition": content_disposition(filename, fallback="workspace.zip"),
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
            },
        )
    except BaseException:
        staged.cleanup()
        raise
