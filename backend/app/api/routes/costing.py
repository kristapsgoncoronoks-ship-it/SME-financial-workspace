"""Cost-allocation master-data API (WO-14 / F1.1) — departments, cost centers,
projects.

Thin controllers over `app/services/costing.py` (Slice 1): the unique-code
guard, the status-transition rules and the optimistic-concurrency check all
live in the service; these handlers only parse, call and map errors.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import NoReturn

from fastapi import APIRouter, Depends, HTTPException, Request, Response, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, refresh_current_identity, require_perm
from app.api.upload_read import read_upload
from app.core import authz, storage
from app.core.errors import AppError
from app.core.security_headers import content_disposition
from app.models.costing import CostCenter, Department, Project
from app.models.customer import Customer
from app.models.document import Document
from app.models.project_link import ProjectDocument
from app.schemas.costing import (
    CostCenterCreate,
    CostCenterOut,
    DepartmentOut,
    MasterCreate,
    MasterUpdate,
    ProjectCreate,
    ProjectOut,
)
from app.schemas.project_profit import (
    CostEntryIn,
    CostEntryOut,
    OfferIn,
    OfferOut,
    OfferTransitionIn,
    PlanRowIn,
    PlanTrackingOut,
    ProjectDocumentOut,
    ProjectPnlOut,
)
from app.services import (
    audit,
    costing,
    crm,
    documents,
    project_offers,
    project_profit,
    project_thumbnails,
)

# Structural authorization (ADR-0024): the masters feed the cost-allocation
# pickers on invoice and expense forms, so reading them declares INVOICE_READ —
# held by EVERY business role (same rationale as the tax-code and currency
# catalogues). Managing them is org configuration (SETTINGS_MANAGE, per-route).
router = APIRouter(
    prefix="/masters",
    tags=["masters"],
    dependencies=[Depends(require_perm(authz.Permission.INVOICE_READ))],
)
_ADMIN = [Depends(require_perm(authz.Permission.SETTINGS_MANAGE))]


def _raise(exc: costing.CostingError) -> NoReturn:
    """Map service errors to the wire: stale write / rule violation → 409;
    a missing (or other tenant's — indistinguishable, §4.4) id → opaque 404."""
    if isinstance(exc, costing.NotFoundError):
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    raise HTTPException(status.HTTP_409_CONFLICT, str(exc))


# --- Departments -----------------------------------------------------------


@router.get("/departments", response_model=list[DepartmentOut])
async def list_departments(current: CurrentUser, db: DbSession, include_archived: bool = False):
    return await costing.list_entities(
        db, Department, current.org_id, include_archived=include_archived
    )


@router.post(
    "/departments",
    response_model=DepartmentOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=_ADMIN,
)
async def create_department(body: MasterCreate, current: CurrentUser, db: DbSession):
    try:
        return await costing.create_department(db, current.org_id, code=body.code, name=body.name)
    except costing.CostingError as exc:
        _raise(exc)


@router.patch("/departments/{entity_id}", response_model=DepartmentOut, dependencies=_ADMIN)
async def update_department(
    entity_id: str, body: MasterUpdate, current: CurrentUser, db: DbSession
):
    try:
        return await costing.update(
            db,
            Department,
            current.org_id,
            entity_id,
            expected_version=body.version,
            name=body.name,
            status=body.status,
        )
    except costing.CostingError as exc:
        _raise(exc)


# --- Cost centers ----------------------------------------------------------


@router.get("/cost-centers", response_model=list[CostCenterOut])
async def list_cost_centers(current: CurrentUser, db: DbSession, include_archived: bool = False):
    return await costing.list_entities(
        db, CostCenter, current.org_id, include_archived=include_archived
    )


@router.post(
    "/cost-centers",
    response_model=CostCenterOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=_ADMIN,
)
async def create_cost_center(body: CostCenterCreate, current: CurrentUser, db: DbSession):
    try:
        return await costing.create_cost_center(
            db, current.org_id, code=body.code, name=body.name, department_id=body.department_id
        )
    except costing.CostingError as exc:
        _raise(exc)


@router.patch("/cost-centers/{entity_id}", response_model=CostCenterOut, dependencies=_ADMIN)
async def update_cost_center(
    entity_id: str, body: MasterUpdate, current: CurrentUser, db: DbSession
):
    try:
        return await costing.update(
            db,
            CostCenter,
            current.org_id,
            entity_id,
            expected_version=body.version,
            name=body.name,
            status=body.status,
        )
    except costing.CostingError as exc:
        _raise(exc)


# --- Projects --------------------------------------------------------------


@router.get("/projects", response_model=list[ProjectOut])
async def list_projects(current: CurrentUser, db: DbSession, include_archived: bool = False):
    return await costing.list_entities(
        db, Project, current.org_id, include_archived=include_archived
    )


@router.post(
    "/projects",
    response_model=ProjectOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=_ADMIN,
)
async def create_project(body: ProjectCreate, current: CurrentUser, db: DbSession):
    try:
        return await costing.create_project(
            db,
            current.org_id,
            code=body.code,
            name=body.name,
            start_date=body.start_date,
            end_date=body.end_date,
        )
    except costing.CostingError as exc:
        _raise(exc)


@router.patch("/projects/{entity_id}", response_model=ProjectOut, dependencies=_ADMIN)
async def update_project(entity_id: str, body: MasterUpdate, current: CurrentUser, db: DbSession):
    try:
        return await costing.update(
            db,
            Project,
            current.org_id,
            entity_id,
            expected_version=body.version,
            name=body.name,
            status=body.status,
        )
    except costing.CostingError as exc:
        _raise(exc)


@router.get("/offers-pipeline")
async def offers_pipeline(current: CurrentUser, db: DbSession):
    """CRM light (WO-H): the kanban read over the EXISTING offer pipeline —
    offers grouped by status with days-in-stage and the staleness flag. Rides
    the router-level INVOICE_READ like every other offers read."""
    return await crm.pipeline(db, current.org_id)


class ProjectCustomerIn(BaseModel):
    """WO-E: which customer this project is FOR (None unlinks). The arrival
    notice resolves its recipient through this link at send time."""

    customer_id: str | None = None


@router.put("/projects/{entity_id}/customer", response_model=ProjectOut, dependencies=_ADMIN)
async def set_project_customer(
    entity_id: str, body: ProjectCustomerIn, current: CurrentUser, db: DbSession
):
    project = await db.scalar(
        select(Project).where(Project.org_id == current.org_id, Project.id == entity_id)
    )
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")
    if body.customer_id is not None:
        customer = await db.scalar(
            select(Customer).where(
                Customer.org_id == current.org_id, Customer.id == body.customer_id
            )
        )
        if customer is None:  # unknown or other-tenant — indistinguishable, §4.4
            raise HTTPException(status.HTTP_404_NOT_FOUND, "customer not found")
    prior = project.customer_id
    project.customer_id = body.customer_id
    await audit.record(
        db,
        "project.customer_set",
        target_type="project",
        target_id=project.id,
        meta={"customer_id": body.customer_id, "prior_customer_id": prior},
    )
    await db.commit()
    await db.refresh(project)
    return project


# --- Project profitability (phase 1, docs/design/project-profitability.md) ---
#
# Subresources of the project master rather than a second router: the project
# IS the master row, and the pickers/screens already know this prefix. Reads
# ride the router-level INVOICE_READ; booking costs and attaching contracts is
# bookkeeping, not org configuration, so mutations declare INVOICE_WRITE
# (NOT the masters' SETTINGS_MANAGE — a finance user who cannot edit org
# settings can still book a wage line onto a job).

_BOOKKEEPING = [Depends(require_perm(authz.Permission.INVOICE_WRITE))]


@router.get("/projects-pnl-summary", response_model=list[ProjectPnlOut])
async def projects_pnl_summary(current: CurrentUser, db: DbSession):
    """Every project's headline figures — the list screen's question is "which
    contracts lose money", so profit and margin ride with code/name/status."""
    return [ProjectPnlOut(**row) for row in await project_profit.pnl_summary(db, current.org_id)]


@router.get("/projects/{entity_id}/pnl", response_model=ProjectPnlOut)
async def project_pnl(entity_id: str, current: CurrentUser, db: DbSession):
    """The LIVE project P&L (revenue − costs, NET EUR — the response's `basis`
    states it). Phase 2 adds the freeze at close."""
    try:
        return ProjectPnlOut(**await project_profit.pnl(db, current.org_id, entity_id))
    except project_profit.ProjectProfitError as exc:
        _raise_pp(exc)


def _raise_pp(exc: project_profit.ProjectProfitError) -> NoReturn:
    if isinstance(exc, project_profit.NotFoundError):
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


def _entry_out(e) -> CostEntryOut:
    return CostEntryOut(
        id=e.id,
        label=e.label,
        category=e.category,
        amount=str(e.amount),
        currency=e.currency,
        entry_date=e.entry_date.isoformat() if e.entry_date else None,
        note=e.note,
        created_by=e.created_by,
        created_at=e.created_at.isoformat(),
    )


@router.get("/projects/{entity_id}/cost-entries", response_model=list[CostEntryOut])
async def list_cost_entries(entity_id: str, current: CurrentUser, db: DbSession):
    try:
        rows = await project_profit.list_cost_entries(db, current.org_id, entity_id)
    except project_profit.ProjectProfitError as exc:
        _raise_pp(exc)
    return [_entry_out(e) for e in rows]


@router.post(
    "/projects/{entity_id}/cost-entries",
    response_model=CostEntryOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=_BOOKKEEPING,
)
async def add_cost_entry(entity_id: str, body: CostEntryIn, current: CurrentUser, db: DbSession):
    try:
        entry = await project_profit.add_cost_entry(
            db,
            current.org_id,
            entity_id,
            label=body.label,
            category=body.category,
            amount=body.amount,
            entry_date=body.entry_date,
            note=body.note,
            created_by=current.email,
        )
    except project_profit.ProjectProfitError as exc:
        _raise_pp(exc)
    await audit.record(
        db,
        "project.cost_entry_add",
        target_type="project",
        target_id=entity_id,
        meta={"label": entry.label, "category": entry.category, "amount": str(entry.amount)},
    )
    await db.commit()
    await db.refresh(entry)
    return _entry_out(entry)


@router.delete(
    "/projects/{entity_id}/cost-entries/{entry_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=_BOOKKEEPING,
)
async def delete_cost_entry(entity_id: str, entry_id: str, current: CurrentUser, db: DbSession):
    try:
        entry = await project_profit.delete_cost_entry(db, current.org_id, entity_id, entry_id)
    except project_profit.ProjectProfitError as exc:
        _raise_pp(exc)
    # The audit meta carries WHAT was removed: after the commit the event is the
    # only trace the line existed, and a P&L that changed needs an explanation.
    await audit.record(
        db,
        "project.cost_entry_delete",
        target_type="project",
        target_id=entity_id,
        meta={"label": entry.label, "category": entry.category, "amount": str(entry.amount)},
    )
    await db.commit()


def _doc_out(d) -> ProjectDocumentOut:
    return ProjectDocumentOut(
        id=d.id,
        kind=d.kind,
        filename=d.filename,
        content_type=d.content_type,
        uploaded_by=d.uploaded_by,
        created_at=d.created_at.isoformat(),
        shared_with_customer=d.shared_with_customer,
    )


@router.get("/projects/{entity_id}/documents", response_model=list[ProjectDocumentOut])
async def list_project_documents(entity_id: str, current: CurrentUser, db: DbSession):
    try:
        rows = await project_profit.list_documents(db, current.org_id, entity_id)
    except project_profit.ProjectProfitError as exc:
        _raise_pp(exc)
    return [_doc_out(d) for d in rows]


@router.post(
    "/projects/{entity_id}/documents",
    response_model=ProjectDocumentOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=_BOOKKEEPING,
)
async def attach_project_document(
    entity_id: str,
    current: CurrentUser,
    db: DbSession,
    file: UploadFile,
    kind: str = "contract",
):
    data = await read_upload(file)
    try:
        row = await project_profit.attach_document(
            db,
            current.org_id,
            entity_id,
            data=data,
            filename=file.filename or "document",
            content_type=file.content_type,
            kind=kind,
            uploaded_by=current.email,
        )
    except project_profit.ProjectProfitError as exc:
        _raise_pp(exc)
    await audit.record(
        db,
        "project.document_attach",
        target_type="project",
        target_id=entity_id,
        meta={"kind": row.kind, "filename": row.filename, "sha256": row.sha256},
    )
    await db.commit()
    await db.refresh(row)
    return _doc_out(row)


class DocumentShareIn(BaseModel):
    shared: bool


@router.put(
    "/projects/{entity_id}/documents/{document_id}/share",
    response_model=ProjectDocumentOut,
    dependencies=_BOOKKEEPING,
)
async def share_project_document(
    entity_id: str, document_id: str, body: DocumentShareIn, current: CurrentUser, db: DbSession
):
    """WO-I: per-document portal sharing, OFF by default — the client portal
    shows exactly what someone chose to show. Toggling either way is audited."""
    row = await db.scalar(
        select(ProjectDocument).where(
            ProjectDocument.org_id == current.org_id,
            ProjectDocument.project_id == entity_id,
            ProjectDocument.id == document_id,
        )
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    row.shared_with_customer = body.shared
    await audit.record(
        db,
        "project.document_share",
        target_type="project",
        target_id=entity_id,
        meta={"document_id": document_id, "filename": row.filename, "shared": body.shared},
    )
    await db.commit()
    await db.refresh(row)
    return _doc_out(row)


@router.get("/projects/{entity_id}/documents/{document_id}/download")
async def download_project_document(
    entity_id: str, document_id: str, request: Request, current: CurrentUser, db: DbSession
):
    """Served inert (attachment + nosniff), like every document route: an
    uploaded contract is still attacker-influenced bytes."""
    try:
        row, data = await project_profit.load_document(db, current.org_id, entity_id, document_id)
        sha = row.sha256
        fresh, _org = await refresh_current_identity(request, db, current)
        authz.require(fresh, authz.Permission.INVOICE_READ)
        row = await project_profit.get_document(db, fresh.org_id, entity_id, document_id)
        if row.sha256 != sha:
            raise project_profit.NotFoundError("Document not found")
    except project_profit.ProjectProfitError as exc:
        _raise_pp(exc)
    except storage.StorageError as exc:
        # Missing objects and adapter base-read failures are availability faults.
        # Preserve typed interrupted/oversize/write errors and their contracts.
        if type(exc) is not storage.StorageError:
            raise
        raise AppError(
            "Stored document could not be read. Check storage and retry.",
            code="storage_read_unavailable",
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
            headers={"Cache-Control": "private, no-store"},
        ) from None
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": content_disposition(row.filename, fallback="attachment"),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get(
    "/projects/{entity_id}/documents/{document_id}/thumbnail",
    response_class=Response,
    responses={
        200: {"content": {"image/jpeg": {"schema": {"type": "string", "format": "binary"}}}}
    },
)
async def project_photo_thumbnail(
    entity_id: str, document_id: str, request: Request, current: CurrentUser, db: DbSession
):
    try:
        sha, size, data = await project_thumbnails.prepare(
            db, current.org_id, entity_id, document_id
        )
        fresh, _org = await refresh_current_identity(request, db, current)
        authz.require(fresh, authz.Permission.INVOICE_READ)
        row = await project_profit.get_document(db, fresh.org_id, entity_id, document_id)
        if row.sha256 != sha or row.kind != "photo":
            raise project_profit.NotFoundError("Document not found")
        current_size = await db.scalar(
            select(Document.size).where(
                Document.org_id == fresh.org_id,
                Document.kind == project_profit.PROJECT_DOCS,
                Document.sha256 == sha,
            )
        )
        if current_size != size:
            raise documents.OriginalIntegrityError()
    except project_profit.ProjectProfitError as exc:
        _raise_pp(exc)
    return Response(
        data,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": 'inline; filename="thumbnail.jpg"',
            "X-Content-Type-Options": "nosniff",
        },
    )


# --- Offers/estimates + the invoicing plan (lifecycle phase 4, §5a) ----------


def _offer_out(o) -> OfferOut:
    return OfferOut(
        id=o.id,
        number=o.number,
        version=o.version,
        status=o.status,
        title=o.title,
        currency=o.currency,
        total=str(o.total),
        lines=json.loads(o.line_items_json or "[]"),
        note=o.note,
        created_by=o.created_by,
        created_at=o.created_at.isoformat(),
    )


@router.get("/projects/{entity_id}/offers", response_model=list[OfferOut])
async def list_project_offers(entity_id: str, current: CurrentUser, db: DbSession):
    try:
        rows = await project_offers.list_offers(db, current.org_id, entity_id)
    except project_profit.ProjectProfitError as exc:
        _raise_pp(exc)
    return [_offer_out(o) for o in rows]


@router.post(
    "/projects/{entity_id}/offers",
    response_model=OfferOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=_BOOKKEEPING,
)
async def create_project_offer(entity_id: str, body: OfferIn, current: CurrentUser, db: DbSession):
    try:
        offer = await project_offers.create_offer(
            db,
            current.org_id,
            entity_id,
            title=body.title,
            lines=[li.model_dump(mode="json") for li in body.lines],
            note=body.note,
            created_by=current.email,
        )
    except project_profit.ProjectProfitError as exc:
        _raise_pp(exc)
    await audit.record(
        db,
        "project.offer_create",
        target_type="project",
        target_id=entity_id,
        meta={"number": offer.number, "version": offer.version, "total": str(offer.total)},
    )
    await db.commit()
    await db.refresh(offer)
    return _offer_out(offer)


@router.post(
    "/projects/{entity_id}/offers/{offer_id}/revise",
    response_model=OfferOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=_BOOKKEEPING,
)
async def revise_project_offer(
    entity_id: str, offer_id: str, body: OfferIn, current: CurrentUser, db: DbSession
):
    try:
        revision = await project_offers.revise_offer(
            db,
            current.org_id,
            offer_id,
            title=body.title,
            lines=[li.model_dump(mode="json") for li in body.lines],
            note=body.note,
            created_by=current.email,
        )
    except project_profit.ProjectProfitError as exc:
        _raise_pp(exc)
    await audit.record(
        db,
        "project.offer_revise",
        target_type="project",
        target_id=entity_id,
        meta={"number": revision.number, "version": revision.version, "total": str(revision.total)},
    )
    await db.commit()
    await db.refresh(revision)
    return _offer_out(revision)


@router.post(
    "/projects/{entity_id}/offers/{offer_id}/transition",
    response_model=OfferOut,
    dependencies=_BOOKKEEPING,
)
async def transition_project_offer(
    entity_id: str, offer_id: str, body: OfferTransitionIn, current: CurrentUser, db: DbSession
):
    try:
        offer, seeded = await project_offers.transition_offer(
            db, current.org_id, offer_id, body.status, actor=current.email
        )
    except project_profit.ProjectProfitError as exc:
        _raise_pp(exc)
    await audit.record(
        db,
        "project.offer_transition",
        target_type="project",
        target_id=entity_id,
        meta={"number": offer.number, "status": offer.status, "plan_rows_seeded": seeded},
    )
    await db.commit()
    await db.refresh(offer)
    return _offer_out(offer)


@router.get("/projects/{entity_id}/invoicing-plan", response_model=PlanTrackingOut)
async def get_invoicing_plan(entity_id: str, current: CurrentUser, db: DbSession):
    try:
        return PlanTrackingOut(**await project_offers.plan_tracking(db, current.org_id, entity_id))
    except project_profit.ProjectProfitError as exc:
        _raise_pp(exc)


@router.put(
    "/projects/{entity_id}/invoicing-plan",
    response_model=PlanTrackingOut,
    dependencies=_BOOKKEEPING,
)
async def put_invoicing_plan(
    entity_id: str, body: list[PlanRowIn], current: CurrentUser, db: DbSession
):
    try:
        await project_offers.set_plan(
            db, current.org_id, entity_id, [(r.label, r.amount) for r in body]
        )
    except project_profit.ProjectProfitError as exc:
        _raise_pp(exc)
    await audit.record(
        db,
        "project.invoicing_plan_set",
        target_type="project",
        target_id=entity_id,
        meta={"rows": len(body), "total": str(sum((r.amount for r in body), Decimal(0)))},
    )
    await db.commit()
    return PlanTrackingOut(**await project_offers.plan_tracking(db, current.org_id, entity_id))


class GenerateDocumentIn(BaseModel):
    template_scope: str  # 'own' | 'platform'
    template_id: str
    customer_id: str | None = None


@router.post(
    "/projects/{entity_id}/generate-document",
    response_model=ProjectDocumentOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=_BOOKKEEPING,
)
async def generate_project_document(
    entity_id: str, body: GenerateDocumentIn, current: CurrentUser, db: DbSession
):
    """Render the chosen template (the client's saved version or a platform
    master) against this project and attach the PDF to the project's documents
    — one slot for a project's papers, however they came to exist."""
    from app.services import doc_templates

    try:
        row, _text = await doc_templates.generate_project_document(
            db,
            current.org_id,
            entity_id,
            template_id=body.template_id,
            template_scope=body.template_scope,
            customer_id=body.customer_id,
            uploaded_by=current.email,
        )
    except project_profit.ProjectProfitError as exc:
        _raise_pp(exc)
    await audit.record(
        db,
        "project.document_generate",
        target_type="project",
        target_id=entity_id,
        meta={
            "template_scope": body.template_scope,
            "template_id": body.template_id,
            "filename": row.filename,
        },
    )
    await db.commit()
    await db.refresh(row)
    return _doc_out(row)


# --------------------------------------------------------------------------- #
# WO-D: acceptance & handover + the adjustable final invoice (composer).
# --------------------------------------------------------------------------- #


class AcceptanceIn(BaseModel):
    document_id: str | None = None
    note: str | None = Field(default=None, max_length=2000)


class AdjustmentIn(BaseModel):
    label: str = Field(min_length=1, max_length=200)
    amount: str  # signed decimal string — the service quantizes and refuses 0


class FinalInvoiceDraftIn(BaseModel):
    adjustments: list[AdjustmentIn] = Field(default_factory=list)


class FinalInvoiceLineOut(BaseModel):
    description: str
    quantity: str
    unit_price: str


class FinalInvoiceDraftOut(BaseModel):
    project_id: str
    contracted_total: str
    issued_total: str
    remainder: str
    lines: list[FinalInvoiceLineOut]
    total: str
    gate_required: bool
    accepted_at: str | None


@router.post(
    "/projects/{entity_id}/acceptance",
    response_model=ProjectPnlOut,
    dependencies=_BOOKKEEPING,
)
async def record_acceptance(
    entity_id: str, body: AcceptanceIn, current: CurrentUser, db: DbSession
):
    """Record the customer's acceptance — the sign-off that makes the final
    invoice unarguable. The optional document is the countersigned acceptance
    (generated from the acceptance template, or uploaded)."""
    try:
        await project_profit.record_acceptance(
            db,
            current.org_id,
            entity_id,
            document_id=body.document_id,
            note=body.note,
            accepted_by=current.email,
        )
    except project_profit.ProjectProfitError as exc:
        _raise_pp(exc)
    await audit.record(
        db,
        "project.acceptance_record",
        target_type="project",
        target_id=entity_id,
        meta={"document_id": body.document_id, "note": body.note},
    )
    await db.commit()
    return ProjectPnlOut(**await project_profit.pnl(db, current.org_id, entity_id))


@router.delete(
    "/projects/{entity_id}/acceptance",
    response_model=ProjectPnlOut,
    dependencies=_BOOKKEEPING,
)
async def revoke_acceptance(entity_id: str, current: CurrentUser, db: DbSession):
    try:
        prior = await project_profit.revoke_acceptance(db, current.org_id, entity_id)
    except project_profit.ProjectProfitError as exc:
        _raise_pp(exc)
    # The audit meta carries WHAT was revoked — after commit it is the record.
    await audit.record(
        db,
        "project.acceptance_revoke",
        target_type="project",
        target_id=entity_id,
        meta=prior,
    )
    await db.commit()
    return ProjectPnlOut(**await project_profit.pnl(db, current.org_id, entity_id))


@router.post(
    "/projects/{entity_id}/final-invoice-draft",
    response_model=FinalInvoiceDraftOut,
    dependencies=_BOOKKEEPING,
)
async def final_invoice_draft(
    entity_id: str, body: FinalInvoiceDraftIn, current: CurrentUser, db: DbSession
):
    """Compose the final invoice for the issuing form: contracted remainder ±
    labelled adjustments (owner decision: ADJUSTABLE — damages and unexpected
    costs either way reconcile in the open). Refuses a non-positive total
    (credit note territory) and honours the org's acceptance gate. Pure
    composition — the invoice itself is issued through the normal flow."""
    try:
        draft = await project_profit.final_invoice_draft(
            db,
            current.org_id,
            entity_id,
            adjustments=[a.model_dump() for a in body.adjustments],
        )
    except project_profit.GateError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    except project_profit.ProjectProfitError as exc:
        _raise_pp(exc)
    return FinalInvoiceDraftOut(**draft)
