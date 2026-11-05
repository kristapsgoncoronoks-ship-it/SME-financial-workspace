from __future__ import annotations

import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select

from app.api.deps import (
    CurrentOrg,
    CurrentSession,
    CurrentSessionUnscoped,
    CurrentUser,
    DbSession,
    SuspendedTolerantOrg,
    SuspendedTolerantUser,
    assert_expected_identity,
)
from app.core import authz, bank_id, residency
from app.core.config import settings
from app.core.identity import principal
from app.core.ratelimit import client_ip
from app.core.security import hash_password, verify_password
from app.models.email_token import PURPOSE_PASSWORD_RESET, PURPOSE_VERIFY_EMAIL
from app.models.invitation import Invitation
from app.models.membership import Membership
from app.models.organization import Organization
from app.models.session import Session
from app.models.sso import SsoConnection
from app.models.user import User, UserRole
from app.schemas.auth import (
    AuthResponse,
    BankDetailsIn,
    ForgotPasswordRequest,
    LoginRequest,
    MeOut,
    OrganizationOut,
    RegisterRequest,
    ResetPasswordRequest,
    SessionOut,
    Token,
    UserOut,
    VerifyEmailRequest,
)
from app.schemas.tenancy import AcceptInvite, InvitePreview
from app.services import (
    audit,
    memberships,
    oidc,
    saml,
    sessions,
    sso_config,
    sso_transactions,
    team,
    verification,
)

router = APIRouter(prefix="/auth", tags=["auth"])
log = logging.getLogger("invoiceiq.auth")


def _permissions_of(user) -> list[str]:
    """PROD-003: the effective permission list the SPA renders its navigation
    from (`GET /auth/permissions` serves the same list; carrying it on the
    identity read saves the SPA a round trip on every boot)."""
    return sorted(p.value for p in authz.permissions_for(user))


def _ua(request: Request) -> str | None:
    return request.headers.get("user-agent")


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.post("/register", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest, request: Request, db: DbSession) -> AuthResponse:
    existing = await db.scalar(select(User).where(User.email == body.email.lower()))
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")

    org = Organization(name=body.organization_name, region=settings.tenant_region)
    db.add(org)
    await db.flush()  # assign org.id

    user = User(
        org_id=org.id,
        email=body.email.lower(),
        name=body.name,
        hashed_password=await run_in_threadpool(hash_password, body.password),
        role=UserRole.owner,  # the first user is the OWNER of THIS company only
        is_expense_approver=True,  # the owner is an expense approver by default
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    await db.refresh(org)

    # Write the owner's AUTHORITATIVE membership (B1.5); users.org_id/role above
    # are only the new account's initial active-org projection.
    await memberships.ensure(
        db,
        org_id=org.id,
        user_id=user.id,
        role=UserRole.owner,
        is_expense_approver=True,
        email=user.email,
        name=user.name,
    )

    await audit.record(
        db,
        audit.A.REGISTER,
        org_id=org.id,
        actor=(user.id, user.email),
        target_type="organization",
        target_id=org.id,
    )
    # Send the email-verification link (best-effort; recorded to the outbox).
    await verification.send_verification(db, user)
    token = await sessions.start(db, user, user_agent=_ua(request), ip=_ip(request))
    await db.commit()
    return AuthResponse(
        token=Token(access_token=token),
        user=UserOut.model_validate(user),
        organization=OrganizationOut.model_validate(org),
        permissions=_permissions_of(user),
    )


@router.post("/login", response_model=AuthResponse)
async def login(body: LoginRequest, request: Request, db: DbSession) -> AuthResponse:
    user = await db.scalar(select(User).where(User.email == body.email.lower()))
    now = datetime.now(UTC)

    # Brute-force lockout: if the account is currently locked, refuse WITHOUT
    # checking the password (so a correct guess during lockout still fails).
    if user is not None and user.locked_until is not None:
        locked_until = user.locked_until
        if locked_until.tzinfo is None:  # SQLite returns naive datetimes
            locked_until = locked_until.replace(tzinfo=UTC)
        if locked_until > now:
            retry = int((locked_until - now).total_seconds()) + 1
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "Too many failed attempts. Try again later.",
                headers={"Retry-After": str(retry)},
            )

    # bcrypt is 50–300 ms of pure CPU (BE-008): off the loop, so a burst of
    # logins cannot stall every other request on this worker.
    if user is None or not await run_in_threadpool(
        verify_password, body.password, user.hashed_password
    ):
        # Count the failure and lock the account once the threshold is crossed.
        # (A missing user is not counted — nothing to persist and no enumeration.)
        if user is not None:
            user.failed_login_count = (user.failed_login_count or 0) + 1
            if user.failed_login_count >= settings.login_max_failed_attempts:
                user.locked_until = now + timedelta(minutes=settings.login_lockout_minutes)
                user.failed_login_count = 0
            await db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Account is disabled")

    selected = await memberships.login_scope(
        db, user.id, user.org_id, allow_inactive_default=user.is_platform_admin
    )
    if selected is None:
        default_org = await db.get(Organization, user.org_id)
        if default_org is not None and default_org.status != "active":
            raise HTTPException(
                status.HTTP_402_PAYMENT_REQUIRED,
                f"Workspace is {default_org.status}. Contact support.",
            )
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Could not validate credentials")
    membership, org = selected
    residency.assert_region(org)
    # Opt-in email-verification gate (SSO + platform operators exempt).
    if (
        settings.require_email_verification
        and not user.email_verified
        and not user.is_platform_admin
    ):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Please verify your email address before signing in.",
        )
    current = principal(user, membership)
    # Successful login clears any accumulated failure count / lock.
    if user.failed_login_count or user.locked_until:
        user.failed_login_count = 0
        user.locked_until = None
    # Login happens before deps' request-context hook (there is no bearer token
    # yet), so the location is passed explicitly — resolved by the same
    # XFF-resistant rule the rate limiter and deps use. No session_id: the
    # session this login creates does not exist yet at the moment of the event.
    await audit.record(
        db,
        audit.A.LOGIN,
        org_id=current.org_id,
        actor=(user.id, user.email),
        ip=client_ip(request.scope),
    )
    token = await sessions.start(db, current, user_agent=_ua(request), ip=_ip(request))
    await db.commit()
    return AuthResponse(
        token=Token(access_token=token),
        user=UserOut.model_validate(current),
        organization=OrganizationOut.model_validate(org),
        permissions=_permissions_of(current),
    )


@router.get("/me", response_model=MeOut)
async def me(current: SuspendedTolerantUser, org: SuspendedTolerantOrg) -> MeOut:
    """The identity read. Suspended-tolerant (PROD-001): the SPA boots from this
    call, and a suspended tenant's members must still be able to see WHY they
    have no access — and the owner must reach Plan & billing to fix it. The
    response carries `organization.status`; every data route stays gated."""
    return MeOut(
        user=UserOut.model_validate(current),
        organization=OrganizationOut.model_validate(org),
        permissions=_permissions_of(current),
    )


@router.put("/bank-details", response_model=MeOut)
async def set_bank_details(
    body: BankDetailsIn, current: CurrentUser, db: DbSession, org: CurrentOrg
) -> MeOut:
    """Set the caller's OWN payout bank details (IBAN/BIC), used when an expense
    reimbursement batch is exported as a SEPA credit transfer."""
    user = await db.get(User, current.id)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found")
    # Format gate (WO-2): this IBAN becomes a SEPA creditor account in the
    # reimbursement payout file — a malformed one is refused at write time
    # (422 invalid_iban/invalid_bic via the AppError handler).
    user.iban = bank_id.assert_iban(body.iban) if body.iban else None
    user.bic = bank_id.assert_bic(body.bic) if body.bic else None
    await db.commit()
    await db.refresh(user)
    return MeOut(
        user=UserOut.model_validate(current).model_copy(
            update={"iban": user.iban, "bic": user.bic}
        ),
        organization=OrganizationOut.model_validate(org),
        permissions=_permissions_of(current),
    )


@router.get("/permissions")
async def my_permissions(current: CurrentUser) -> dict:
    """The caller's resolved business role + effective permissions. The UI uses
    this to hide what it must not show — but the backend still enforces every
    permission (this endpoint is advisory, never the gate)."""
    return {
        "role": authz.business_role(current).value,
        "permissions": sorted(p.value for p in authz.permissions_for(current)),
    }


@router.get("/authz-matrix")
async def authz_matrix(current: CurrentUser) -> dict:
    """The full role→permission matrix (for the members & roles admin screen)."""
    return authz.matrix()


# --- Organizations + switching (Slice 6c) ----------------------------------


@router.get("/organizations")
async def my_organizations(session: CurrentSessionUnscoped, db: DbSession) -> list[dict]:
    """Every organization the caller is a member of (the org switcher's data).
    Runs unscoped so all memberships are visible; filtered by the caller's id."""
    rows = await db.execute(
        select(Membership.org_id, Organization.name, Membership.role)
        .outerjoin(Organization, Organization.id == Membership.org_id)
        .where(Membership.user_id == session.user_id, Membership.status == "active")
        .order_by(Membership.created_at.desc())
    )
    return [
        {
            "org_id": org_id,
            "name": name if name is not None else "?",
            "role": role.value,
            "current": org_id == session.org_id,
        }
        for org_id, name, role in rows
    ]


@router.post("/switch-org/{org_id}")
async def switch_org(
    org_id: str, request: Request, session: CurrentSessionUnscoped, db: DbSession
) -> dict:
    """Switch the caller's ACTIVE organization. Only an org the caller has a live
    membership in is allowed (opaque 404 otherwise). Only this session changes;
    the same token keeps working and other sessions retain their selections."""
    # Serialize switches of this one session and re-read after the lock. Other
    # sessions for the same account remain independent.
    locked = await db.scalar(
        select(Session)
        .where(Session.id == session.id, Session.user_id == session.user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked is None or locked.revoked_at is not None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Could not validate credentials")
    session = locked
    assert_expected_identity(request, session.org_id, session.user_id)
    user = await db.get(User, session.user_id)
    membership = await memberships.get(db, org_id, session.user_id)
    org = await db.get(Organization, org_id)
    if (
        user is None
        or membership is None
        or membership.status != "active"
        or org is None
        or org.status != "active"
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Organization not found")
    residency.assert_region(org)

    # Never rewrite the shared account's org/role as a request projection.
    session.org_id = org_id
    await audit.record(
        db, audit.A.SWITCH_ORG, org_id=org_id, actor=(user.id, user.email), target_id=org_id
    )
    await db.commit()

    return {
        "org_id": org_id,
        "name": org.name if org else "?",
        "role": membership.role.value,
    }


# --- Sessions + revocation (Slice 4) ---------------------------------------


@router.post("/logout")
async def logout(session: CurrentSession, db: DbSession) -> dict:
    """Revoke the current session — the token stops working immediately (not just
    client-side)."""
    await sessions.revoke(db, session.id)
    return {"logged_out": True}


@router.get("/sessions", response_model=list[SessionOut])
async def list_sessions(session: CurrentSession, db: DbSession) -> list[SessionOut]:
    """List this user's active sessions (the current one flagged)."""
    rows = await sessions.list_active(db, session.user_id)
    return [
        SessionOut(
            id=s.id,
            created_at=s.created_at,
            last_seen_at=s.last_seen_at,
            user_agent=s.user_agent,
            ip=s.ip,
            current=(s.id == session.id),
        )
        for s in rows
    ]


@router.post("/sessions/revoke-others")
async def revoke_other_sessions(session: CurrentSession, db: DbSession) -> dict:
    """Sign out everywhere else — revoke all of this user's sessions except this
    one."""
    n = await sessions.revoke_all(db, session.user_id, except_jti=session.id)
    return {"revoked": n}


@router.delete("/sessions/{session_id}")
async def revoke_session(session_id: str, session: CurrentSession, db: DbSession) -> dict:
    """Revoke one of this user's OWN sessions by id (opaque 404 otherwise)."""
    target = await db.get(Session, session_id)
    if target is None or target.user_id != session.user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    await sessions.revoke(db, session_id)
    return {"revoked": True}


# --- Email verification + password reset (Slice 3) -------------------------


@router.post("/verify-email")
async def verify_email(body: VerifyEmailRequest, db: DbSession) -> dict:
    """Confirm an email address from the link token. Idempotent-ish: a used or
    expired token is a clean 400, never a leak of whether the account exists."""
    row = await verification.consume(db, body.token, PURPOSE_VERIFY_EMAIL)
    if row is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired verification link")
    user = await db.get(User, row.user_id)
    if user is not None:
        user.email_verified = True
        await audit.record(
            db, audit.A.EMAIL_VERIFY, org_id=user.org_id, actor=(user.id, user.email)
        )
    await db.commit()
    return {"verified": True}


@router.post("/resend-verification")
async def resend_verification(current: CurrentUser, db: DbSession) -> dict:
    """Re-send the verification link to the signed-in user (no-op if already
    verified). Always 200 to avoid signalling state."""
    if not current.email_verified:
        await verification.send_verification(db, current)
        await db.commit()
    return {"sent": True}


@router.post("/forgot-password")
async def forgot_password(body: ForgotPasswordRequest, db: DbSession) -> dict:
    """Begin a password reset. ALWAYS returns 200 with the same body — it never
    reveals whether the email is registered (no user enumeration)."""
    user = await db.scalar(select(User).where(User.email == body.email.lower()))
    if user is not None and user.is_active:
        await verification.send_password_reset(db, user)
        await audit.record(
            db, audit.A.PASSWORD_RESET_REQUEST, org_id=user.org_id, actor=(user.id, user.email)
        )
        await db.commit()
    return {"sent": True}


@router.post("/reset-password")
async def reset_password(body: ResetPasswordRequest, db: DbSession) -> dict:
    """Set a new password from a reset-link token (single-use). A used/expired
    token is a clean 400."""
    row = await verification.consume(db, body.token, PURPOSE_PASSWORD_RESET)
    if row is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired reset link")
    user = await db.get(User, row.user_id)
    if user is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired reset link")
    user.hashed_password = await run_in_threadpool(hash_password, body.new_password)
    # A verified reset also proves control of the mailbox.
    user.email_verified = True
    await audit.record(db, audit.A.PASSWORD_RESET, org_id=user.org_id, actor=(user.id, user.email))
    await db.commit()
    # Revoke every existing session — a reset must lock out any hijacked token.
    await sessions.revoke_all(db, user.id)
    return {"reset": True}


# --- SSO (OIDC) login (ADR-0021) -------------------------------------------


def _sso_error_redirect() -> RedirectResponse:
    return RedirectResponse(
        f"{settings.sso_error_url}?sso_error=1", status_code=status.HTTP_302_FOUND
    )


@router.get("/sso/{slug}/authorize", include_in_schema=False)
async def sso_authorize(slug: str, request: Request, db: DbSession):
    """Begin OIDC login: redirect the browser to the tenant's IdP with PKCE +
    a signed, stateless `state`."""
    conn = await sso_config.get_by_slug(db, slug)
    if conn is None or not conn.enabled or conn.protocol != "oidc":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "SSO is not enabled for this workspace")
    try:
        disco = await oidc.discover(conn.issuer or "")
        verifier, challenge = oidc.pkce_pair()
        nonce = secrets.token_urlsafe(16)
        state = oidc.sign_state(conn.id, nonce, verifier)
        url = oidc.build_authorize_url(
            disco["authorization_endpoint"],
            client_id=conn.client_id or "",
            redirect_uri=settings.sso_redirect_uri,
            state=state,
            nonce=nonce,
            code_challenge=challenge,
        )
    except Exception as exc:  # noqa: BLE001 - IdP unreachable / misconfigured
        log.warning("SSO authorize failed for %s: %s", slug, exc)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Identity provider is unreachable")
    secret = await sso_transactions.start(db, conn.org_id, state)
    response = RedirectResponse(url, status_code=status.HTTP_302_FOUND)
    response.set_cookie(
        sso_transactions.cookie_name(state),
        secret,
        max_age=sso_transactions.TTL_SECONDS,
        httponly=True,
        secure=settings.environment == "production" or request.url.scheme == "https",
        samesite="lax",
        path=sso_transactions.COOKIE_PATH,
    )
    return response


@router.get("/sso/callback", include_in_schema=False)
async def sso_callback(
    request: Request,
    db: DbSession,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    """OIDC redirect back: validate + JIT-provision, then bounce to the SPA with
    our internal token in the URL fragment. Any failure → the SPA login page."""

    def finish(response: RedirectResponse) -> RedirectResponse:
        if state:
            response.delete_cookie(
                sso_transactions.cookie_name(state),
                path=sso_transactions.COOKIE_PATH,
                httponly=True,
                samesite="lax",
                secure=settings.environment == "production" or request.url.scheme == "https",
            )
        return response

    if not state:
        return finish(_sso_error_redirect())
    try:
        st = oidc.read_state(state)
        conn = await db.get(SsoConnection, st["conn"])
        if conn is None or not conn.enabled or conn.protocol != "oidc":
            raise oidc.SsoError("connection unavailable")
        org = await db.get(Organization, conn.org_id)
        if org is None or org.status != "active":
            raise oidc.SsoError("organization unavailable")
        if not await sso_transactions.consume(
            db, conn.org_id, state, request.cookies.get(sso_transactions.cookie_name(state))
        ):
            raise oidc.SsoError("login transaction unavailable")
        if error or not code:
            raise oidc.SsoError("login was canceled or incomplete")
        user, org = await oidc.finish_login(
            db, conn, code=code, nonce=st["nonce"], code_verifier=st["cv"]
        )
    except oidc.SsoError as exc:
        log.warning("SSO callback rejected: %s", exc)
        return finish(_sso_error_redirect())
    except Exception:  # noqa: BLE001 - never 500 into a browser redirect flow
        log.exception("SSO callback error")
        return finish(_sso_error_redirect())
    token = await sessions.start(db, user, user_agent=_ua(request), ip=_ip(request))
    await db.commit()
    return finish(
        RedirectResponse(
            f"{settings.sso_post_login_url}#access_token={token}", status_code=status.HTTP_302_FOUND
        )
    )


# --- SAML (SP request side + metadata; ADR-0021) ---------------------------


def _saml_ids(slug: str) -> tuple[str, str]:
    base = settings.api_public_base_url.rstrip("/")
    sp_entity_id = f"{base}/saml/{slug}"
    acs_url = f"{base}{settings.api_v1_prefix}/auth/sso/saml/acs"
    return sp_entity_id, acs_url


@router.get("/sso/{slug}/saml/metadata", include_in_schema=False)
async def saml_metadata(slug: str, db: DbSession):
    """Our SP metadata XML — the customer registers this with their IdP."""
    conn = await sso_config.get_by_slug(db, slug)
    if conn is None or conn.protocol != "saml":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No SAML connection for this workspace")
    sp_entity_id, acs_url = _saml_ids(slug)
    return Response(
        saml.sp_metadata_xml(sp_entity_id=sp_entity_id, acs_url=acs_url),
        media_type="application/xml",
    )


@router.get("/sso/{slug}/saml/login", include_in_schema=False)
async def saml_login(slug: str, db: DbSession):
    """SP-initiated SAML login: redirect to the IdP with a fresh AuthnRequest."""
    conn = await sso_config.get_by_slug(db, slug)
    if conn is None or not conn.enabled or conn.protocol != "saml":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "SAML SSO is not enabled for this workspace")
    if not conn.saml_sso_url:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "SAML connection is not fully configured")
    sp_entity_id, acs_url = _saml_ids(slug)
    xml = saml.build_authn_request(
        request_id="_" + uuid.uuid4().hex,
        issue_instant=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        sp_entity_id=sp_entity_id,
        acs_url=acs_url,
        idp_sso_url=conn.saml_sso_url,
    )
    url = saml.redirect_binding_url(conn.saml_sso_url, xml, relay_state=slug)
    return RedirectResponse(url, status_code=status.HTTP_302_FOUND)


@router.post("/sso/saml/acs", include_in_schema=False)
async def saml_acs():
    """Assertion Consumer Service — the DELIBERATE boundary (ADR-0021). Validating
    a signed SAML assertion needs a vetted XML-DSig library + a real IdP; we
    refuse rather than ship an unvalidated (bypassable) path."""
    try:
        saml.consume_assertion()
    except saml.SamlNotReady as exc:
        raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, str(exc))


@router.get("/invite/{token}", response_model=InvitePreview)
async def preview_invite(token: str, db: DbSession) -> InvitePreview:
    inv = await db.scalar(
        select(Invitation).where(Invitation.token == token, Invitation.accepted.is_(False))
    )
    if inv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invitation not found or already used")
    if team.invitation_expired(inv):
        # 410 Gone lets the UI show a distinct "expired" state (vs invalid/used).
        raise HTTPException(status.HTTP_410_GONE, "This invitation has expired")
    org = await db.get(Organization, inv.org_id)
    if org is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Organization not found")
    return InvitePreview(email=inv.email, organization_name=org.name, role=inv.role)


@router.post("/accept-invite", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
async def accept_invite(body: AcceptInvite, request: Request, db: DbSession) -> AuthResponse:
    try:
        result = await team.accept_invitation(db, body.token, body.name, body.password)
    except team.InviteAuthError:
        # The email already has an account; the wrong password was supplied.
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "This email already has an account — sign in with your existing password to join.",
        )
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invitation not found or already used")
    user, org_id = result
    org = await db.get(Organization, org_id)
    if org is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Organization not found")
    membership = await memberships.get(db, org_id, user.id)
    if membership is None or membership.status != "active":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Could not validate credentials")
    current = principal(user, membership)
    token = await sessions.start(db, current, user_agent=_ua(request), ip=_ip(request))
    await db.commit()
    return AuthResponse(
        token=Token(access_token=token),
        user=UserOut.model_validate(current),
        organization=OrganizationOut.model_validate(org),
        permissions=_permissions_of(current),
    )
