from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, UploadFile, status
from sqlalchemy import func, select

from app.api.deps import CurrentUser, DbSession, refresh_current_identity, require_perm
from app.api.upload_read import read_upload
from app.core import authz, bank_id, storage
from app.models.document_version import OWNER_ISSUER_LOGO
from app.models.issued_invoice import IssuedInvoice
from app.schemas.document_version import DocumentVersionOut
from app.schemas.issuer import IssuerProfileIn, IssuerProfileOut
from app.services import document_versions, documents, filesec, issuer

# Structural authorization (ADR-0024): reading the issuer profile/logo powers the
# issuing screens (ISSUED_READ, router-level); editing the legal entity, its
# registry and the logo is org administration (SETTINGS_MANAGE, per-route).
router = APIRouter(
    prefix="/issuer",
    tags=["issuer"],
    dependencies=[Depends(require_perm(authz.Permission.ISSUED_READ))],
)
_ADMIN = [Depends(require_perm(authz.Permission.SETTINGS_MANAGE))]


def _apply(profile, body: IssuerProfileIn) -> None:
    for field, value in body.model_dump(exclude_unset=True).items():
        if field in ("country", "default_currency", "nace_code") and value:
            value = value.upper()
        # Format gate (WO-2): the issuer IBAN/BIC become the DEBTOR account of
        # every SEPA payment file — malformed values are refused at write time
        # (422 invalid_iban/invalid_bic), stored normalized otherwise.
        if field == "iban" and value:
            value = bank_id.assert_iban(value)
        if field == "bic" and value:
            value = bank_id.assert_bic(value)
        setattr(profile, field, value)


_LOGO_KINDS = frozenset({"png", "jpeg"})
_LOGO_MIME = {"png": "image/png", "jpeg": "image/jpeg"}


def _out(profile) -> IssuerProfileOut:
    data = IssuerProfileOut.model_validate(profile)
    data.missing_fields = issuer.missing_fields(profile)
    data.is_complete = not data.missing_fields
    data.has_logo = profile.logo_sha256 is not None
    return data


@router.get("", response_model=IssuerProfileOut)
async def get_issuer(current: CurrentUser, db: DbSession):
    return _out(await issuer.get_or_create(db, current.org_id))


@router.put("", response_model=IssuerProfileOut, dependencies=_ADMIN)
async def update_issuer(body: IssuerProfileIn, current: CurrentUser, db: DbSession):
    """Update the org's DEFAULT issuer entity (the legacy single-issuer surface)."""
    profile = await issuer.get_or_create(db, current.org_id)
    _apply(profile, body)
    await _refuse_prefix_clash(db, profile)
    await db.commit()
    await db.refresh(profile)
    return _out(profile)


async def _refuse_prefix_clash(db, profile) -> None:
    # DB-002: two entities numbering with one prefix would generate the same
    # invoice number; the org-wide unique constraint would then refuse the
    # issue and burn a number. Refuse the PREFIX instead, at save time.
    try:
        await issuer.assert_prefixes_unique(db, profile)
    except issuer.PrefixInUse as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))


# --- Issuer registry: MULTIPLE legal entities, each with its own numbering series ---


@router.get("/registry", response_model=list[IssuerProfileOut])
async def list_registry(current: CurrentUser, db: DbSession):
    await issuer.get_or_create(db, current.org_id)  # ensure at least the default exists
    return [_out(p) for p in await issuer.list_issuers(db, current.org_id)]


@router.post(
    "/registry",
    response_model=IssuerProfileOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=_ADMIN,
)
async def create_registry_issuer(body: IssuerProfileIn, current: CurrentUser, db: DbSession):
    """Register an additional issuer legal entity (its own gap-free numbering series)."""
    await issuer.get_or_create(db, current.org_id)  # first entity is the default
    profile = await issuer.create_issuer(db, current.org_id)
    _apply(profile, body)
    await _refuse_prefix_clash(db, profile)
    await db.commit()
    await db.refresh(profile)
    return _out(profile)


@router.put("/registry/{issuer_id}", response_model=IssuerProfileOut, dependencies=_ADMIN)
async def update_registry_issuer(
    issuer_id: str, body: IssuerProfileIn, current: CurrentUser, db: DbSession
):
    profile = await issuer.get_by_id(db, current.org_id, issuer_id)
    if profile is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Issuer not found")
    _apply(profile, body)
    await _refuse_prefix_clash(db, profile)
    await db.commit()
    await db.refresh(profile)
    return _out(profile)


@router.post("/registry/{issuer_id}/default", response_model=IssuerProfileOut, dependencies=_ADMIN)
async def set_registry_default(issuer_id: str, current: CurrentUser, db: DbSession):
    target = await issuer.set_default(db, current.org_id, issuer_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Issuer not found")
    await db.commit()
    await db.refresh(target)
    return _out(target)


@router.delete("/registry/{issuer_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=_ADMIN)
async def delete_registry_issuer(issuer_id: str, current: CurrentUser, db: DbSession):
    profile = await issuer.get_by_id(db, current.org_id, issuer_id)
    if profile is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Issuer not found")
    if profile.is_default:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Cannot delete the default issuer; set another default first."
        )
    referenced = await db.scalar(
        select(func.count(IssuedInvoice.id)).where(
            IssuedInvoice.org_id == current.org_id, IssuedInvoice.issuer_id == issuer_id
        )
    )
    if referenced:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Cannot delete an issuer that has invoices; it is part of their audit record.",
        )
    await db.delete(profile)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/logo", response_model=IssuerProfileOut, dependencies=_ADMIN)
async def upload_logo(current: CurrentUser, db: DbSession, file: UploadFile):
    content = await read_upload(file, "logo")
    # Security gate: real PNG/JPEG type + malware scan (same gate as every upload).
    try:
        kind = await filesec.check_async(file.filename or "logo", content, allowed=_LOGO_KINDS)
    except filesec.FileRejected as exc:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, str(exc))
    profile = await issuer.get_or_create(db, current.org_id)
    sha, size = await documents.store(
        documents.LOGOS,
        current.org_id,
        content,
        _LOGO_MIME[kind],
        db=db,
        filename=file.filename,
        uploaded_by=current.email,
    )
    await document_versions.record(
        db,
        current.org_id,
        OWNER_ISSUER_LOGO,
        profile.id,
        sha256=sha,
        size=size,
        mime=_LOGO_MIME[kind],
        filename=file.filename,
        uploaded_by=current.email,
    )
    profile.logo_mime = _LOGO_MIME[kind]
    profile.logo_sha256 = sha
    profile.logo_size = size
    await db.commit()
    await db.refresh(profile)
    return _out(profile)


@router.get("/logo")
async def get_logo(current: CurrentUser, db: DbSession, request: Request):
    profile = await issuer.get_or_create(db, current.org_id)
    if not profile.logo_sha256:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No logo set")
    identity = (profile.id, profile.logo_sha256, profile.logo_size, profile.logo_mime)
    try:
        content = await documents.load_verified_original(
            documents.LOGOS, current.org_id, profile.logo_sha256, size=profile.logo_size
        )
    except storage.StorageReadError:
        raise
    except storage.StorageError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Stored logo missing") from None
    fresh, _ = await refresh_current_identity(request, db, current)
    authz.require(fresh, authz.Permission.ISSUED_READ)
    profiles = await issuer.list_issuers(db, current.org_id)
    visible = profiles[0] if profiles else None
    if (
        visible is None
        or (visible.id, visible.logo_sha256, visible.logo_size, visible.logo_mime) != identity
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Logo changed while preparing the download")
    if content is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Stored logo missing")
    return Response(
        content=content,
        media_type=visible.logo_mime or "image/png",
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"},
    )


@router.get("/logo/versions", response_model=list[DocumentVersionOut], dependencies=_ADMIN)
async def logo_versions(current: CurrentUser, db: DbSession):
    """The supersession history of this org's logo (newest first)."""
    profile = await issuer.get_or_create(db, current.org_id)
    rows = await document_versions.history(db, current.org_id, OWNER_ISSUER_LOGO, profile.id)
    return [DocumentVersionOut.model_validate(r) for r in rows]
