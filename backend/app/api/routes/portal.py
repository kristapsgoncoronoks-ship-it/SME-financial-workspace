"""The PUBLIC client portal (WO-I): the customer's magic link opens this.

Unauthenticated by design — the per-customer secret token IS the credential
(PUBLIC_ROUTES-listed with that reason; the calendar-feed precedent).
Tenant resolution mirrors the email-intake webhook exactly: an UNSCOPED
token lookup, then every query runs under the resolved tenant's explicit
scope, and an unknown/revoked token 404s with nothing to enumerate.

Serves ONLY the token's customer: their live offers (viewing stamps the
quote-viewed signal), their issued invoices with status, and the project
documents someone explicitly shared. The offer decision rides the one
existing transition machinery — accepted/declined from here is the same
event as from inside the app, audited with the portal actor.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import Response
from pydantic import BaseModel

from app.api.deps import DbSession
from app.core.security_headers import content_disposition
from app.core.tenant import reset_current_org, set_current_org
from app.services import audit, portal, project_profit

router = APIRouter(prefix="/portal", tags=["portal"])


class DecisionIn(BaseModel):
    decision: str  # accepted | rejected


async def _resolve(db: DbSession, token: str) -> tuple[str, str]:
    resolved = await portal.resolve_token(db, token)
    if resolved is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown portal link")
    return resolved


@router.get("/{token}")
async def portal_summary(token: str, db: DbSession):
    org_id, customer_id = await _resolve(db, token)
    scope = set_current_org(org_id)
    try:
        data = await portal.summary(db, org_id, customer_id)
        await db.commit()  # the quote-viewed stamps
    finally:
        reset_current_org(scope)
    return data


@router.post("/{token}/offers/{offer_id}/decision")
async def portal_offer_decision(token: str, offer_id: str, body: DecisionIn, db: DbSession):
    org_id, customer_id = await _resolve(db, token)
    scope = set_current_org(org_id)
    try:
        try:
            offer = await portal.decide_offer(db, org_id, customer_id, offer_id, body.decision)
        except portal.NotFoundError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        except portal.PortalError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        await audit.record(
            db,
            "offer.portal_decision",
            org_id=org_id,
            target_type="offer",
            target_id=offer_id,
            meta={"decision": offer.status, "customer_id": customer_id, "via": "portal"},
        )
        await db.commit()
    finally:
        reset_current_org(scope)
    return {"offer_id": offer_id, "status": offer.status}


@router.get("/{token}/documents/{document_id}")
async def portal_document(token: str, document_id: str, db: DbSession):
    """Served inert (attachment + nosniff), like every document route."""
    org_id, customer_id = await _resolve(db, token)
    scope = set_current_org(org_id)
    try:
        try:
            row = await portal.load_shared_document(db, org_id, customer_id, document_id)
            _, data = await project_profit.load_document(db, org_id, row.project_id, row.id)
            sha = row.sha256
            await db.flush()
            db.expire_all()
            if await _resolve(db, token) != (org_id, customer_id):
                raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
            row = await portal.load_shared_document(db, org_id, customer_id, document_id)
            if row.sha256 != sha:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
        except (portal.NotFoundError, project_profit.ProjectProfitError) as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found") from exc
    finally:
        reset_current_org(scope)
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": content_disposition(row.filename, fallback="attachment"),
            "X-Content-Type-Options": "nosniff",
        },
    )
