"""Document registry API (Slice 5d) — the metadata catalog of stored originals.

Read-only, admin-gated (it spans every user's uploads in the tenant). Metadata
only; the bytes are served by each owner's existing download route.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.deps import CurrentUser, DbSession, require_perm
from app.core import authz
from app.schemas.document import DocumentOut, DocumentPageOut
from app.services import document_registry

# Structural authorization (ADR-0024): the registry spans every user's uploads
# in the tenant, so it is admin-gated — router-level SETTINGS_MANAGE.
router = APIRouter(
    prefix="/documents",
    tags=["documents"],
    dependencies=[Depends(require_perm(authz.Permission.SETTINGS_MANAGE))],
)


@router.get("", response_model=list[DocumentOut])
async def list_documents(current: CurrentUser, db: DbSession, kind: str | None = None):
    return await document_registry.list_for_org(db, current.org_id, kind=kind)


@router.get("/page", response_model=DocumentPageOut)
async def document_page(
    current: CurrentUser,
    db: DbSession,
    kind: str | None = Query(default=None, max_length=24),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    rows, total, kinds = await document_registry.page_for_org(
        db, current.org_id, kind=kind, limit=limit, offset=offset
    )
    return DocumentPageOut(
        items=[DocumentOut.model_validate(row) for row in rows], total=total, kinds=kinds
    )
