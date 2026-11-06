from __future__ import annotations

import base64
import binascii
import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from sqlalchemy import func, select

from app.api.deps import CurrentOrg, CurrentUser, DbSession, require_perm
from app.api.routes.invoices import _capture_identity, _detail, persist_invoice
from app.core import authz, storage
from app.core.config import settings
from app.core.errors import AppError
from app.core.security_headers import content_disposition
from app.core.tenant import reset_current_org, set_current_org
from app.models.email_intake import InboundInvoice
from app.schemas.capture_reading import ReadingObservationHistory
from app.schemas.email_intake import (
    MAX_INBOUND_ATTACHMENTS,
    ChannelCadenceIn,
    ChannelHealthOut,
    EmailSettingsOut,
    InboundConfirm,
    InboundEmailIn,
    InboundInvoiceDetail,
    InboundInvoiceOut,
    InboundListOut,
    InboundResult,
)
from app.schemas.image_reading import load_preparation
from app.schemas.invoice import InvoiceDetailOut, ParsedInvoiceDraft
from app.services import (
    access,
    audit,
    capture_reading,
    documents,
    email_intake,
    filesec,
    inbound_health,
    modules,
)
from app.services.email_providers import mailgun

# Structural authorization (ADR-0024): declared PER-ROUTE because POST /inbound
# is a provider webhook (its own authentication; see PUBLIC_ROUTES). The review
# inbox is the metered capture surface — INVOICE_READ (held by EVERY business
# role) preserves the documented open-to-every-tier capture decision (see
# `confirm_inbound`); rotating the inbound address is org configuration.
router = APIRouter(prefix="/email", tags=["email intake"])
_CAPTURE = [Depends(require_perm(authz.Permission.INVOICE_READ))]
_ADMIN = [Depends(require_perm(authz.Permission.SETTINGS_MANAGE))]


async def _guard(db: DbSession, org_id: str):
    await modules.require_enabled(db, org_id, "email_intake")


# --------------------------------------------------------------------------- #
# Inbound webhook (public — called by the email provider's parse hook)
# --------------------------------------------------------------------------- #
def _inbound_auth_failed() -> AppError:
    """The ONE rejection for every inbound-auth failure mode (no secret
    configured, secret absent, secret wrong, unknown recipient token). A single
    construction site guarantees the responses are byte-identical, so a caller
    can never distinguish "bad secret" from "real tenant, wrong secret" from
    "no such tenant" — enumeration safety over the 64-bit address tokens."""
    return AppError(
        "Inbound authentication failed",
        code="inbound_auth_failed",
        status=status.HTTP_401_UNAUTHORIZED,
    )


async def _delivery_replayed(
    db: DbSession, org_id: str, token: str | None, message_id: str | None
) -> bool:
    """Lock admission and revalidate facts after any wait or health commit."""
    if not await email_intake.lock_delivery_workspace(db, org_id):
        raise _inbound_auth_failed()
    if await email_intake.resolve_org(db, token) != org_id:
        raise _inbound_auth_failed()
    if not await modules.is_enabled(db, org_id, "email_intake"):
        await inbound_health.record_failure(
            db, org_id, inbound_health.CHANNEL_EMAIL, inbound_health.ERR_NOT_ACTIVATED
        )
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Email invoice intake is not activated for this workspace",
        )
    return await email_intake.already_delivered(db, org_id, message_id)


@router.post("/inbound", response_model=InboundResult)
async def inbound(
    body: InboundEmailIn,
    db: DbSession,
    x_inbound_secret: str | None = Header(default=None, alias="X-Inbound-Secret"),
):
    """Receive a parsed inbound email and drop each attachment into the org's
    review inbox.

    Authentication is a MANDATORY shared secret (header `X-Inbound-Secret` or a
    `secret` body field) presented by the email provider's webhook. FAIL CLOSED:
    an unconfigured secret rejects everything — an unset env var must never
    silently open a document-injection door into a tenant's AP review flow. The
    comparison is constant-time (`hmac.compare_digest`) so it leaks neither
    length nor prefix.

    The tenant is resolved from the RECIPIENT address token, never the sender:
    `From` is trivially forgeable and forwarding breaks SPF/DKIM alignment, so
    the sender can never be an identity. An unknown recipient token returns the
    SAME 401 as a bad secret (never a distinguishable 404) so the endpoint
    cannot be used to enumerate live inbound addresses."""
    expected = settings.inbound_email_secret
    presented = x_inbound_secret or body.secret
    if not expected or presented is None:
        raise _inbound_auth_failed()
    if not hmac.compare_digest(presented.encode(), expected.encode()):
        raise _inbound_auth_failed()

    token = body.token or email_intake.token_from_address(body.to)
    org_id = await email_intake.resolve_org(db, token)
    if org_id is None:
        raise _inbound_auth_failed()

    # H-2: the tenant is known from here on, so every outcome is attributable.
    # An auth failure ABOVE this point deliberately is not — the secret check runs
    # before the token is resolved so a prober cannot enumerate live addresses,
    # which means we genuinely do not know whose delivery it was. Recording it
    # against a guessed org would be a fabrication; the tenant-visible
    # consequence is still caught by `last_success_at` going stale.
    scope = set_current_org(org_id)
    try:
        # BE-009: a redelivery of a message already in the inbox is acknowledged
        # (the provider stops retrying) and stores nothing. Checked before the
        # attempt is recorded — a retry is not a new delivery.
        if await _delivery_replayed(db, org_id, token, body.message_id):
            return InboundResult(
                received=len(body.attachments), queued=0, rejected=0, deduplicated=True
            )
        # Pessimistic-first: this commits BEFORE any document work, so a crash
        # mid-delivery leaves the attempt recorded as not-succeeded rather than
        # leaving no trace at all.
        await inbound_health.begin_attempt(db, org_id, inbound_health.CHANNEL_EMAIL)
        # begin_attempt intentionally commits before document work. Reacquire
        # the shared admission lock and inspect fresh facts before writing.
        if await _delivery_replayed(db, org_id, token, body.message_id):
            return InboundResult(
                received=len(body.attachments), queued=0, rejected=0, deduplicated=True
            )

        # Decode EVERY attachment before writing any of them. A malformed payload
        # is then a clean, classified channel failure with nothing half-written —
        # decoding inside the write loop would either abandon the attempt as
        # merely "incomplete" or commit the attachments that happened to come
        # first. The response is unchanged: still a 422, still nothing stored.
        decoded: list[tuple[str, str | None, bytes]] = []
        for att in body.attachments:
            try:
                decoded.append(
                    (
                        att.filename,
                        att.content_type,
                        base64.b64decode(att.content_base64, validate=True),
                    )
                )
            except (binascii.Error, ValueError):
                await inbound_health.record_failure(
                    db,
                    org_id,
                    inbound_health.CHANNEL_EMAIL,
                    inbound_health.ERR_MALFORMED,
                    f"Attachment {att.filename} is not valid base64",
                )
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    f"Attachment {att.filename} is not valid base64",
                )

        queued = rejected = 0
        for filename, content_type, content in decoded:
            row = await email_intake.process_attachment(
                db,
                org_id,
                from_addr=body.from_addr,
                subject=body.subject,
                filename=filename,
                content_type=content_type,
                content=content,
                message_id=body.message_id,
            )
            if row.status == "rejected":
                rejected += 1
            else:
                queued += 1
        # A delivery whose attachments were all refused is still a SUCCESS for the
        # CHANNEL — the message reached us. Refused documents are H-1's subject
        # (`capture_failures`); conflating them would raise an alarm about the
        # pipe when the problem is the contents.
        await inbound_health.record_success(db, org_id, inbound_health.CHANNEL_EMAIL)
        # Rows + their extract jobs + the health record commit together; the
        # worker parses out-of-band.
        await db.commit()
    finally:
        reset_current_org(scope)

    return InboundResult(received=len(body.attachments), queued=queued, rejected=rejected)


@router.post("/inbound/mailgun", response_model=InboundResult)
async def inbound_mailgun(request: Request, db: DbSession):
    """Mailgun-native counterpart to `POST /inbound` (E1.6 — the email-intake
    provider adapter). Reads Mailgun's own inbound-route `multipart/form-data`
    payload directly (`recipient`, `sender`, `subject`, `timestamp`, `token`,
    `signature`, `attachment-count` + `attachment-1..N` file parts) instead of
    requiring the caller to have already normalised it into `InboundEmailIn`,
    and feeds each attachment through the SAME `email_intake.process_attachment`
    pipeline the generic endpoint above uses — no logic is duplicated.

    Authentication is a SECOND, provider-native layer on top of the shared
    recipient-token tenant resolution both endpoints use: Mailgun's own
    HMAC-SHA256(signing_key, timestamp+token) signature
    (`app.services.email_providers.mailgun.verify_signature`), plus a freshness
    window (`mailgun.is_fresh`) so a captured valid signature cannot be replayed
    indefinitely. FAILS CLOSED exactly like `/inbound`: an unconfigured signing
    key, a missing/bad/stale signature, or an unresolvable recipient token all
    return the SAME `_inbound_auth_failed()` response — a prober can never
    distinguish "wrong signature" from "no such tenant" from "Mailgun not
    configured"."""
    async with request.form() as form:

        def _s(key: str) -> str | None:
            value = form.get(key)
            return value if isinstance(value, str) else None

        signing_key = settings.mailgun_signing_key
        token_field = _s("token") or ""
        timestamp = _s("timestamp") or ""
        signature = _s("signature") or ""
        if (
            not signing_key
            or not mailgun.verify_signature(
                token=token_field, timestamp=timestamp, signature=signature, signing_key=signing_key
            )
            or not mailgun.is_fresh(timestamp)
        ):
            raise _inbound_auth_failed()

        recipient = _s("recipient") or _s("to")
        recipient_token = email_intake.token_from_address(recipient)
        org_id = await email_intake.resolve_org(db, recipient_token)
        if org_id is None:
            raise _inbound_auth_failed()

        sender = _s("sender") or _s("from")
        subject = _s("subject")
        try:
            count = int(_s("attachment-count") or "0")
        except ValueError:
            count = 0

        queued = rejected = 0
        scope = set_current_org(org_id)
        try:
            # The module gate and the health writes run INSIDE the tenant scope, exactly
            # as the generic /inbound route does. Both queries filter `org_id`
            # explicitly, so running them outside was not a leak — but it left the
            # ORM tenant guard (the second of the three isolation layers) switched off
            # for those statements, and left two sibling routes doing the same thing
            # differently, which is how the next person copies the wrong one.
            # BE-009: Mailgun retries a route that did not answer 2xx for up to
            # 8 hours; the Message-Id it posts identifies the message across them.
            message_id = _s("Message-Id") or _s("message-id")
            if await _delivery_replayed(db, org_id, recipient_token, message_id):
                return InboundResult(received=count, queued=0, rejected=0, deduplicated=True)
            # BE-020: the same bounds the JSON route enforces in its schema. More
            # parts than a message may carry is a malformed delivery, classified
            # and refused before any part is read into memory.
            if count > MAX_INBOUND_ATTACHMENTS:
                await inbound_health.record_failure(
                    db,
                    org_id,
                    inbound_health.CHANNEL_EMAIL,
                    inbound_health.ERR_MALFORMED,
                    f"{count} attachments; at most {MAX_INBOUND_ATTACHMENTS} are accepted per message",
                )
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    f"At most {MAX_INBOUND_ATTACHMENTS} attachments are accepted per message",
                )
            await inbound_health.begin_attempt(db, org_id, inbound_health.CHANNEL_EMAIL)
            if await _delivery_replayed(db, org_id, recipient_token, message_id):
                return InboundResult(received=count, queued=0, rejected=0, deduplicated=True)
            per_part_cap = filesec.max_bytes()
            for i in range(1, count + 1):
                upload = form.get(f"attachment-{i}")
                if upload is None or isinstance(upload, str):
                    continue  # malformed part — never a reason to fail the whole message
                try:
                    # Metadata is only an early refusal; unknown/incorrect size
                    # still goes through the cap+1 reader before processing.
                    if upload.size is not None and upload.size > per_part_cap:
                        rejected += 1
                        continue
                    content = await filesec.read_bounded(upload)
                except filesec.FileTooLarge:
                    rejected += 1
                    continue  # Other supported attachments can still be queued.
                finally:
                    await upload.close()
                row = await email_intake.process_attachment(
                    db,
                    org_id,
                    from_addr=sender,
                    subject=subject,
                    filename=upload.filename or f"attachment-{i}",
                    content_type=upload.content_type,
                    content=content,
                    message_id=message_id,
                )
                if row.status == "rejected":
                    rejected += 1
                else:
                    queued += 1
            await inbound_health.record_success(db, org_id, inbound_health.CHANNEL_EMAIL)
            await db.commit()
        finally:
            reset_current_org(scope)

        return InboundResult(received=count, queued=queued, rejected=rejected)


# --------------------------------------------------------------------------- #
# Settings (the inbound address)
# --------------------------------------------------------------------------- #
@router.get("/settings", response_model=EmailSettingsOut, dependencies=_CAPTURE)
async def get_settings(current: CurrentUser, db: DbSession):
    await _guard(db, current.org_id)
    intake = await email_intake.get_or_create(db, current.org_id)
    total = (
        await db.scalar(
            select(func.count(InboundInvoice.id)).where(InboundInvoice.org_id == current.org_id)
        )
        or 0
    )
    pending = (
        await db.scalar(
            select(func.count(InboundInvoice.id)).where(
                InboundInvoice.org_id == current.org_id, InboundInvoice.status == "pending"
            )
        )
        or 0
    )
    return EmailSettingsOut(
        address=email_intake.address_for(intake.token),
        domain=settings.inbound_email_domain,
        pending=pending,
        total=total,
    )


@router.post("/settings/rotate", response_model=EmailSettingsOut, dependencies=_ADMIN)
async def rotate_address(current: CurrentUser, db: DbSession):
    await _guard(db, current.org_id)
    intake = await email_intake.rotate(db, current.org_id)
    return EmailSettingsOut(
        address=email_intake.address_for(intake.token), domain=settings.inbound_email_domain
    )


# --------------------------------------------------------------------------- #
# Channel health (H-2)
# --------------------------------------------------------------------------- #
@router.get("/health", response_model=list[ChannelHealthOut], dependencies=_CAPTURE)
async def channel_health(current: CurrentUser, db: DbSession):
    """Is the inbound pipe still open — and how would we know?

    An inbound channel can die completely while every other screen stays green,
    because "no invoices arrived" and "nothing could get through" look identical
    from a document count. This reports the outcome of the last delivery ATTEMPT
    and the time of the last SUCCESSFUL one, which is the only thing that
    separates them.

    A channel that has never been used is still listed: leaving it out would make
    its absence indistinguishable from it being healthy. Read-only, tenant-scoped.

    Lives under `/email` because the email address is the only inbound channel
    today; the service behind it is channel-keyed, so a future pull-based channel
    is a row here, not a rewrite."""
    await _guard(db, current.org_id)
    return [
        ChannelHealthOut.model_validate(s, from_attributes=True)
        for s in await inbound_health.status_for(db, current.org_id)
    ]


@router.put("/health/{channel}", response_model=ChannelHealthOut, dependencies=_ADMIN)
async def set_channel_cadence(
    channel: str, body: ChannelCadenceIn, current: CurrentUser, db: DbSession
):
    """State how often this workspace expects deliveries on a channel — the only
    thing that lets quiet be called broken. `null` withdraws the expectation.

    Audited: it changes when the workspace will be told its intake is silent, so
    a later "why did nobody warn us?" has an answer."""
    await _guard(db, current.org_id)
    if channel not in inbound_health.CHANNELS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown inbound channel")
    before = await inbound_health.status_for(db, current.org_id)
    old = next((s.expected_cadence_days for s in before if s.channel == channel), None)
    await inbound_health.set_expected_cadence(
        db, current.org_id, channel, body.expected_cadence_days
    )
    # §4.16: audited old→new.
    await audit.record(
        db,
        action="inbound.cadence_changed",
        target_type="inbound_channel",
        target_id=channel,
        meta={"old": old, "new": body.expected_cadence_days},
    )
    await db.commit()
    status_now = await inbound_health.status_for(db, current.org_id)
    return ChannelHealthOut.model_validate(
        next(s for s in status_now if s.channel == channel), from_attributes=True
    )


# --------------------------------------------------------------------------- #
# Review inbox
# --------------------------------------------------------------------------- #
async def _load(
    db: DbSession, org_id: str, inbound_id: str, *, for_update: bool = False
) -> InboundInvoice:
    stmt = select(InboundInvoice).where(
        InboundInvoice.id == inbound_id, InboundInvoice.org_id == org_id
    )
    if for_update:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    row = await db.scalar(stmt)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Inbound invoice not found")
    return row


async def _locked_inbound(
    db: DbSession, org_id: str, inbound_id: str, request: Request, current: CurrentUser
) -> InboundInvoice:
    row = await _load(db, org_id, inbound_id, for_update=True)
    await _capture_identity(request, db, current)
    await _guard(db, org_id)
    await db.refresh(row)
    return row


@router.get("/inbox", response_model=InboundListOut, dependencies=_CAPTURE)
async def list_inbox(
    current: CurrentUser,
    db: DbSession,
    status_: str | None = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=500),
):
    await _guard(db, current.org_id)
    filters = [InboundInvoice.org_id == current.org_id]
    if status_:
        filters.append(InboundInvoice.status == status_)
    total = await db.scalar(select(func.count(InboundInvoice.id)).where(*filters)) or 0
    rows = await db.scalars(
        select(InboundInvoice)
        .where(*filters)
        .order_by(InboundInvoice.received_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return InboundListOut(items=[InboundInvoiceOut.model_validate(r) for r in rows], total=total)


@router.get("/inbox/{inbound_id}", response_model=InboundInvoiceDetail, dependencies=_CAPTURE)
async def get_inbound(inbound_id: str, current: CurrentUser, db: DbSession):
    await _guard(db, current.org_id)
    row = await _load(db, current.org_id, inbound_id)
    draft = None
    if row.draft_json:
        try:
            draft = ParsedInvoiceDraft.model_validate_json(row.draft_json)
        except ValueError:
            draft = None
    detail = InboundInvoiceDetail.model_validate(row)
    detail.draft = draft
    detail.reading_preparation = load_preparation(row.reading_preparation_json)
    detail.reading_observation = await capture_reading.current_summary(db, row, channel="inbound")
    # A rejected (quarantined) attachment carries a sha256 for the audit trail but
    # its bytes were never retained — so it has no downloadable file.
    detail.has_file = row.status != "rejected" and row.sha256 is not None
    return detail


@router.get(
    "/inbox/{inbound_id}/reading-observations",
    response_model=ReadingObservationHistory,
    dependencies=_CAPTURE,
)
async def inbound_reading_history(
    inbound_id: str,
    current: CurrentUser,
    db: DbSession,
    cursor: str | None = Query(default=None, max_length=128),
):
    await _guard(db, current.org_id)
    row = await _load(db, current.org_id, inbound_id)
    return await capture_reading.history(
        db, current.org_id, row.id, channel="inbound", cursor=cursor
    )


@router.post(
    "/inbox/{inbound_id}/confirm",
    response_model=InvoiceDetailOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=_CAPTURE,
)
async def confirm_inbound(
    inbound_id: str,
    body: InboundConfirm,
    request: Request,
    current: CurrentUser,
    current_org: CurrentOrg,
    db: DbSession,
):
    """Confirm a parsed inbound invoice into a real Invoice — using the same
    persistence path as a manual upload. Accepts an edited draft override."""
    # Same metered capture flow as /invoices and /invoices/upload — quota-governed,
    # open to every tier; not INVOICE_WRITE-gated. WO-47: keyed by the ORG's
    # plan, not the caller's role.
    await _guard(db, current.org_id)
    await access.enforce_invoice_quota(db, current.org_id, current_org.plan)
    row = await _locked_inbound(db, current.org_id, inbound_id, request, current)
    if row.status == "confirmed":
        raise HTTPException(status.HTTP_409_CONFLICT, "This invoice was already confirmed")
    if row.status == "discarded":
        raise HTTPException(status.HTTP_409_CONFLICT, "This attachment was discarded")

    draft = body.draft
    if draft is None:
        if not row.draft_json:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "This attachment has no parsed draft to confirm",
            )
        draft = ParsedInvoiceDraft.model_validate_json(row.draft_json).draft

    async def verify_capture_identity() -> None:
        await _capture_identity(request, db, current)
        await _guard(db, current.org_id)

    invoice, vendor_name = await persist_invoice(
        db, current.org_id, draft, verify_capture_identity=verify_capture_identity, commit=False
    )
    row.invoice_id = invoice.id
    row.status = "confirmed"
    event = await audit.record(
        db,
        audit.A.INBOUND_CONFIRM,
        target_type="invoice",
        target_id=invoice.id,
        meta={"inbound_id": inbound_id, "number": invoice.invoice_number},
    )
    if event is None:
        raise AppError(
            "The invoice confirmation could not be recorded. Try again.",
            code="inbound_confirmation_unavailable",
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    await db.commit()
    return _detail(invoice, vendor_name)


@router.post("/inbox/{inbound_id}/discard", response_model=InboundInvoiceOut, dependencies=_CAPTURE)
async def discard_inbound(inbound_id: str, request: Request, current: CurrentUser, db: DbSession):
    await _guard(db, current.org_id)
    row = await _locked_inbound(db, current.org_id, inbound_id, request, current)
    if row.status == "confirmed":
        raise HTTPException(status.HTTP_409_CONFLICT, "A confirmed invoice cannot be discarded")
    row.status = "discarded"
    await db.commit()
    await db.refresh(row)
    return InboundInvoiceOut.model_validate(row)


@router.delete("/inbox/{inbound_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=_CAPTURE)
async def delete_inbound(inbound_id: str, request: Request, current: CurrentUser, db: DbSession):
    await _guard(db, current.org_id)
    row = await _locked_inbound(db, current.org_id, inbound_id, request, current)
    await db.delete(row)
    await db.commit()


@router.get("/inbox/{inbound_id}/file", dependencies=_CAPTURE)
async def download_file(inbound_id: str, request: Request, current: CurrentUser, db: DbSession):
    await _guard(db, current.org_id)
    row = await _load(db, current.org_id, inbound_id)
    if row.status == "rejected" or not row.sha256:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No stored file for this attachment")
    sha, size = row.sha256, row.size
    try:
        # Preserve the admitted historical size, including empty originals;
        # today's upload policy must not make an older original inaccessible.
        content = await documents.load_verified_original(
            documents.EMAIL_ATTACHMENTS, current.org_id, sha, size=size
        )
    except storage.StorageReadError:
        raise  # Shared sanitized retry guidance; not a missing-object claim.
    except storage.StorageError:
        raise HTTPException(404, "Stored attachment missing") from None
    await _capture_identity(request, db, current)
    await _guard(db, current.org_id)
    row = await _load(db, current.org_id, inbound_id)
    if row.status == "rejected" or (row.sha256, row.size) != (sha, size):
        raise HTTPException(404, "Attachment changed while preparing the download")
    if content is None:
        raise HTTPException(404, "Stored attachment missing")
    await audit.record(
        db,
        audit.A.DOC_DOWNLOAD,
        target_type="inbound_invoice",
        target_id=inbound_id,
        meta={"filename": row.filename},
    )
    await db.commit()
    filename = row.filename or "attachment"
    # Serve inert: force download, never inline, and stop MIME sniffing.
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": content_disposition(filename, fallback="attachment"),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )
