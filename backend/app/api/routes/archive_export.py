"""WO-AI — the PUBLIC half of the ex-client archive export.

An ex-client's owner has no login ("no live login is retained" — owner
decision 2026-08-16 §1.C), so both doors here are unauthenticated:

- `POST /archive/export/request` — the owner's address. Always answers
  `{"sent": true}`; whether the address is an owner's of any workspace is not
  revealed (the forgot-password posture). Requests for the same address are
  coalesced for an hour, so the form cannot flood an inbox.
- `GET /archive/export/download/{token}` — the one-time link from the email.
  The token is an `EmailToken` (hashed, single use, seven days); a used,
  expired or unknown one is an opaque 404. The zip is served inert
  (attachment + nosniff) like every other stored byte.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.api.deps import DbSession
from app.api.file_response import TemporaryFileResponse
from app.core.security_headers import content_disposition
from app.schemas.archive import ArchiveExportRequestIn
from app.services import archive_export as export_svc

router = APIRouter(prefix="/archive/export", tags=["archive"])


@router.post("/request")
async def request_archive_export_by_email(body: ArchiveExportRequestIn, db: DbSession) -> dict:
    await export_svc.request_by_email(db, body.email)
    await db.commit()
    return {"sent": True}


@router.get("/download/{token}")
async def download_archive_export(token: str, db: DbSession):
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
                "Content-Disposition": content_disposition(filename, fallback="archive.zip"),
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
            },
        )
    except BaseException:
        staged.cleanup()
        raise
