"""Own-account calendar connections; provider callbacks are browser-bound public routes."""

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app.api.deps import CurrentSession, CurrentUser, DbSession, require_perm
from app.core import authz
from app.core.config import settings
from app.services import calendar_providers, calendar_sync

_PREFIX = "/schedule/calendar-connections"
router = APIRouter(
    prefix=_PREFIX,
    tags=["schedule"],
    dependencies=[Depends(require_perm(authz.Permission.INVOICE_READ))],
)
public_router = APIRouter(prefix=_PREFIX, tags=["schedule"])


class ConnectOut(BaseModel):
    authorization_url: str


class CalendarConnectionOut(BaseModel):
    id: str
    status: str
    calendar_name: str
    last_synced_at: str | None
    error: str | None
    pending: int
    failed: int


class CalendarProviderOut(BaseModel):
    provider: str
    configured: bool
    reason: str | None
    connection: CalendarConnectionOut | None


class CalendarConnectionsOut(BaseModel):
    providers: list[CalendarProviderOut]


class CalendarRetryOut(BaseModel):
    status: str


def _cookie_path() -> str:
    from urllib.parse import urlsplit

    base_path = urlsplit(settings.api_public_base_url).path.rstrip("/")
    return f"{base_path}{settings.api_v1_prefix.rstrip('/')}{_PREFIX}"


@router.get("", response_model=CalendarConnectionsOut)
async def list_connections(current: CurrentUser, db: DbSession):
    return await calendar_sync.status(db, current.org_id, current.id)


@router.post("/{provider}/connect", response_model=ConnectOut)
async def connect(
    provider: str, current: CurrentUser, session: CurrentSession, db: DbSession, response: Response
):
    try:
        url, browser = await calendar_sync.begin_oauth(
            db, current.org_id, current.id, session.id, provider
        )
    except calendar_sync.CalendarError as exc:
        raise HTTPException(400, str(exc)) from None
    response.set_cookie(
        calendar_sync.OAUTH_COOKIE,
        browser,
        max_age=calendar_sync.OAUTH_SECONDS,
        httponly=True,
        secure=settings.api_public_base_url.startswith("https://"),
        samesite="lax",
        path=_cookie_path(),
    )
    return ConnectOut(authorization_url=url)


@public_router.get(
    "/{provider}/callback",
    status_code=303,
    response_class=RedirectResponse,
    responses={303: {"description": "Return to work planning with the connection outcome."}},
)
async def callback(
    provider: str,
    request: Request,
    db: DbSession,
    state: str = "",
    code: str | None = None,
    error: str | None = None,
):
    success = False
    pending = False
    try:
        success = await calendar_sync.complete_oauth(
            db,
            provider,
            state,
            request.cookies.get(calendar_sync.OAUTH_COOKIE, ""),
            code if not error else None,
        )
        pending = not success
    except (calendar_sync.CalendarError, calendar_providers.ProviderError):
        # Never reflect provider error text, tokens, state or authorization code.
        await db.rollback()
    response = RedirectResponse(
        calendar_sync.return_uri(success=success, pending=pending), status_code=303
    )
    response.delete_cookie(calendar_sync.OAUTH_COOKIE, path=_cookie_path())
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@router.delete("/{provider}", status_code=204)
async def disconnect(provider: str, current: CurrentUser, db: DbSession):
    try:
        await calendar_sync.disconnect(db, current.org_id, current.id, provider)
    except calendar_sync.CalendarError as exc:
        raise HTTPException(400, str(exc)) from None
    response = Response(status_code=204)
    response.delete_cookie(calendar_sync.OAUTH_COOKIE, path=_cookie_path())
    return response


@router.post("/{provider}/retry", status_code=202, response_model=CalendarRetryOut)
async def retry(provider: str, current: CurrentUser, db: DbSession):
    try:
        await calendar_sync.retry(db, current.org_id, current.id, provider)
    except calendar_sync.CalendarError as exc:
        raise HTTPException(409, str(exc)) from None
    return {"status": "queued"}
