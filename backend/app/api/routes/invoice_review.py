"""Supplier-invoice review & approval routes (Phase 08).

Sits on top of the intake pipeline: an invoice saved from capture enters the AP
lifecycle here — side-by-side review + edit, submit into a policy-driven approval
chain, approve/reject/return/reassign, record-lock after approval with a
controlled-correction path, plus comments and internal attachments.

Every mutating action is optimistic-concurrency-guarded (the client sends the
`version` it read; a stale write is 409), authorization-gated
(`authz.require(...)`), audit-logged, and — for the lifecycle milestones — emits
a webhook event and best-effort email to the relevant approver/submitter.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, UploadFile, status
from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from app.api.deps import CurrentOrg, CurrentUser, DbSession, refresh_current_identity, require_perm
from app.api.upload_read import read_upload
from app.core import authz, storage
from app.core.dimensions import DIMENSION_KEYS
from app.core.money import q2 as _q
from app.core.security_headers import content_disposition
from app.models.approval import (
    STEP_APPROVED,
    STEP_REJECTED,
    ApprovalPolicy,
    ApprovalStep,
)
from app.models.invoice import Invoice, InvoiceStatus, WorkflowState
from app.models.invoice_collab import InvoiceAttachment, InvoiceComment
from app.models.user import User
from app.models.vendor import Vendor
from app.schemas.approval import (
    ApprovalHistoryOut,
    ApprovalPolicyIn,
    ApprovalPolicyOut,
    ApprovalPolicyUpdate,
    ApprovalStepOut,
    CommentIn,
    DecisionIn,
    InvoiceAttachmentOut,
    InvoiceCommentOut,
    LinesUpdate,
    ReassignIn,
    ReconciliationOut,
    ReviewHeaderUpdate,
    ReviewLineOut,
    ReviewOut,
    SubmitIn,
    TransitionIn,
)
from app.schemas.received_vat import ReceivedVatBreakdown
from app.services import (
    agreed_prices,
    audit,
    costing,
    documents,
    filesec,
    fx,
    mailer,
    received_lines,
    validation,
    webhooks,
)
from app.services import approval_policy as ap
from app.services import invoice_workflow as wf
from app.services.vendors import get_or_create_vendor

# Structural authorization (ADR-0024): every review route needs at least
# INVOICE_READ (router-level); edits/submits declare INVOICE_WRITE, approval
# decisions INVOICE_APPROVE, and policy administration SETTINGS_MANAGE per-route.
router = APIRouter(
    tags=["invoice-review"],
    dependencies=[Depends(require_perm(authz.Permission.INVOICE_READ))],
)
_WRITE = [Depends(require_perm(authz.Permission.INVOICE_WRITE))]
_APPROVE = [Depends(require_perm(authz.Permission.INVOICE_APPROVE))]
_ADMIN = [Depends(require_perm(authz.Permission.SETTINGS_MANAGE))]

_P = authz.Permission


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


async def _load(db: DbSession, org_id: str, invoice_id: str) -> Invoice:
    inv = await db.scalar(
        select(Invoice)
        .where(Invoice.id == invoice_id, Invoice.org_id == org_id)
        .options(selectinload(Invoice.line_items), selectinload(Invoice.vendor))
    )
    if inv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invoice not found")
    return inv


def _bump(inv: Invoice) -> None:
    inv.version = (inv.version or 1) + 1


async def _email_for(db: DbSession, user_id: str | None) -> str | None:
    if not user_id:
        return None
    u = await db.get(User, user_id)
    return u.email if u else None


def _recon_out(r: validation.Reconciliation) -> ReconciliationOut:
    """Shape the service's reconciliation result onto the frozen wire model
    (findings travel as plain message strings — the SPA's contract)."""
    return ReconciliationOut(
        computed_subtotal=r.computed_subtotal,
        computed_tax=r.computed_tax,
        computed_total=r.computed_total,
        stated_subtotal=r.stated_subtotal,
        stated_tax=r.stated_tax,
        stated_total=r.stated_total,
        balanced=r.balanced,
        findings=[f.message for f in r.findings],
    )


async def _step_out(db: DbSession, s: ApprovalStep) -> ApprovalStepOut:
    email = s.approver_email or await _email_for(db, s.approver_id)
    return ApprovalStepOut(
        id=s.id,
        seq=s.seq,
        kind=s.kind,
        approver_id=s.approver_id,
        approver_email=email,
        status=s.status,
        decided_by_email=s.decided_by_email,
        decided_at=s.decided_at,
        note=s.note,
    )


async def _review_out(db: DbSession, org_id: str, inv: Invoice) -> ReviewOut:
    steps = await ap.steps_for(db, org_id, inv.id)
    comments = list(
        await db.scalars(
            select(InvoiceComment)
            .where(InvoiceComment.org_id == org_id, InvoiceComment.invoice_id == inv.id)
            .order_by(InvoiceComment.created_at.asc())
        )
    )
    attachments = list(
        await db.scalars(
            select(InvoiceAttachment)
            .where(InvoiceAttachment.org_id == org_id, InvoiceAttachment.invoice_id == inv.id)
            .order_by(InvoiceAttachment.created_at.asc())
        )
    )
    state = inv.workflow_state
    return ReviewOut(
        vat_breakdown=ReceivedVatBreakdown.model_validate(received_lines.vat_breakdown(inv)),
        supplier_iban=inv.supplier_iban,
        supplier_registration_number=inv.supplier_registration_number,
        supplier_vat_id=inv.supplier_vat_id,
        id=inv.id,
        invoice_number=inv.invoice_number,
        vendor_id=inv.vendor_id,
        vendor_name=inv.vendor.name if inv.vendor else "",
        currency=inv.currency,
        issue_date=inv.issue_date,
        due_date=inv.due_date,
        status=inv.status.value,
        workflow_state=state.value,
        workflow_label=wf.LABELS[state],
        version=inv.version,
        locked=wf.is_locked(state),
        editable=wf.is_editable(state),
        allowed_targets=sorted(t.value for t in wf.allowed_targets(state)),
        subtotal=Decimal(inv.subtotal or 0),
        tax_amount=Decimal(inv.tax_amount or 0),
        total=Decimal(inv.total or 0),
        total_eur=inv.total_eur,
        account_code=inv.account_code,
        legal_entity_id=inv.legal_entity_id,
        cost_center=inv.cost_center,
        department=inv.department,
        project=inv.project,
        vehicle=inv.vehicle,
        property_ref=inv.property_ref,
        source_filename=inv.source_filename,
        notes=inv.notes,
        submitted_by=inv.submitted_by,
        submitted_at=inv.submitted_at,
        lines=[ReviewLineOut.model_validate(received_lines.out(li)) for li in inv.line_items],
        reconciliation=_recon_out(validation.reconcile(inv)),
        approval_steps=[await _step_out(db, s) for s in steps],
        comments=[InvoiceCommentOut.model_validate(c) for c in comments],
        attachments=[InvoiceAttachmentOut.model_validate(a) for a in attachments],
    )


async def _notify(
    db: DbSession, org_id: str, to_email: str | None, subject: str, body: str, invoice_id: str
) -> None:
    """Best-effort approver/submitter email (recorded to the outbox; never raises)."""
    if not to_email:
        return
    try:
        await mailer.send(
            db,
            org_id,
            kind="approval",
            to_email=to_email,
            subject=subject,
            body=body,
            invoice_id=invoice_id,
        )
    except Exception:  # noqa: BLE001 — a notification must never break the action
        pass


def _policy_out(p: ApprovalPolicy) -> ApprovalPolicyOut:
    return ApprovalPolicyOut(
        id=p.id,
        name=p.name,
        active=p.active,
        priority=p.priority,
        min_amount=p.min_amount,
        department_id=p.department_id,
        cost_center_id=p.cost_center_id,
        legal_entity_id=p.legal_entity_id,
        vendor_id=p.vendor_id,
        approver_ids=ap._approver_ids(p),
        finance_final=p.finance_final,
        finance_approver_id=p.finance_approver_id,
        version=p.version,
    )


# --------------------------------------------------------------------------- #
# Review payload + editing
# --------------------------------------------------------------------------- #


@router.get("/invoices/{invoice_id}/review", response_model=ReviewOut)
async def get_review(invoice_id: str, current: CurrentUser, db: DbSession):
    """The side-by-side review payload: header + lines + reconciliation + the live
    approval chain + comments + attachments + which transitions are legal now."""
    inv = await _load(db, current.org_id, invoice_id)
    return await _review_out(db, current.org_id, inv)


@router.patch("/invoices/{invoice_id}/review", response_model=ReviewOut, dependencies=_WRITE)
async def edit_header(
    invoice_id: str, body: ReviewHeaderUpdate, current: CurrentUser, db: DbSession
):
    """Edit header fields, supplier, dimensions, account — while the invoice is in
    an editable state. Version-gated; locked (approved+) invoices refuse edits."""
    inv = await _load(db, current.org_id, invoice_id)
    wf.assert_version(inv.version, body.version)
    if not wf.is_editable(inv.workflow_state):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Invoice is {wf.LABELS[inv.workflow_state]} and locked for editing. "
            "Reopen it for correction first.",
        )
    fields = body.model_fields_set
    # Supplier match/create.
    if "vendor_id" in fields and body.vendor_id:
        # SEC (P2 batch 8): the supplied id is client input, so it is checked
        # against the caller's workspace exactly as the create path checks it
        # (`routes/invoices.py::_resolve_vendor`) — an opaque 404, so a foreign
        # id and a nonexistent one are indistinguishable. DB-020's composite FK
        # refuses the write underneath, but a route must not leave the database
        # to be the primary control: without this the request died as an opaque
        # 500 on the constraint, and before DB-020 it committed a cross-tenant
        # link.
        vendor = await db.get(Vendor, body.vendor_id)
        if vendor is None or vendor.org_id != current.org_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Vendor not found")
        inv.vendor_id = vendor.id
    elif "vendor_name" in fields and body.vendor_name:
        vendor = await get_or_create_vendor(db, current.org_id, body.vendor_name)
        inv.vendor_id = vendor.id
    for attr in (
        "invoice_number",
        "supplier_iban",
        "supplier_registration_number",
        "supplier_vat_id",
        "issue_date",
        "due_date",
        "notes",
        "account_code",
        "legal_entity_id",
    ):
        if attr in fields:
            setattr(inv, attr, getattr(body, attr))
    if "currency" in fields and body.currency:
        inv.currency = body.currency.upper()
    # Cost-allocation dimensions (apply only those present; re-resolve master links).
    changed = [k for k in DIMENSION_KEYS if k in fields]
    for k in changed:
        setattr(inv, k, getattr(body, k))
    if changed:
        await costing.apply_links(db, current.org_id, inv, changed)
    _bump(inv)
    await audit.record(
        db,
        audit.A.INVOICE_EDIT,
        target_type="invoice",
        target_id=inv.id,
        meta={"fields": sorted(fields - {"version"})},
    )
    await db.commit()
    await db.refresh(inv, attribute_names=["line_items", "vendor"])
    return await _review_out(db, current.org_id, inv)


@router.put("/invoices/{invoice_id}/lines", response_model=ReviewOut, dependencies=_WRITE)
async def edit_lines(invoice_id: str, body: LinesUpdate, current: CurrentUser, db: DbSession):
    """Replace the invoice's line items (tax-validated), then recompute the header
    totals and FX. Version-gated; editable states only."""
    inv = await _load(db, current.org_id, invoice_id)
    wf.assert_version(inv.version, body.version)
    if not wf.is_editable(inv.workflow_state):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Invoice is locked for editing; reopen for correction first."
        )
    # Tax validation before we touch anything.
    for li in body.lines:
        if li.tax_rate < 0 or li.tax_rate > 100:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"Line '{li.description}': tax rate must be between 0 and 100.",
            )
    before_source = received_lines.source_evidence(inv.line_items)
    new_items, subtotal, tax_total = received_lines.build(body.lines)
    inv.line_items[:] = new_items  # cascade delete-orphan replaces the set
    inv.subtotal = _q(subtotal)
    inv.tax_amount = _q(tax_total)
    inv.total = _q(subtotal + tax_total)
    inv.total_eur, inv.fx_source = await fx.eur_total(
        db, inv.total, inv.currency, inv.issue_date, inv.fx_rate
    )
    _bump(inv)
    await audit.record(
        db,
        audit.A.INVOICE_EDIT,
        target_type="invoice",
        target_id=inv.id,
        meta={
            "lines": len(new_items),
            "total": str(inv.total),
            "captured_amounts_before": before_source,
            "captured_amounts_after": received_lines.source_evidence(new_items),
        },
    )
    await db.commit()
    await db.refresh(inv, attribute_names=["line_items", "vendor"])
    return await _review_out(db, current.org_id, inv)


# --------------------------------------------------------------------------- #
# Submit → approve / reject / return / reassign
# --------------------------------------------------------------------------- #


@router.post("/invoices/{invoice_id}/submit", response_model=ReviewOut, dependencies=_WRITE)
async def submit(
    invoice_id: str, body: SubmitIn, current: CurrentUser, db: DbSession, org: CurrentOrg
):
    """Submit a draft for approval: reconcile, evaluate the governing policy, and
    build the approval chain. Refuses an unbalanced invoice (AP control)."""
    inv = await _load(db, current.org_id, invoice_id)
    wf.assert_version(inv.version, body.version)
    wf.assert_transition(inv.workflow_state, WorkflowState.submitted)
    if not inv.line_items:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Add at least one line first.")
    recon = validation.reconcile(inv)
    if recon.blocking:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Invoice does not reconcile: " + "; ".join(f.message for f in recon.blocking),
        )
    # WO-G phase 2: the org-opt-in overcharge gate. Advisory for everyone
    # (run_checks surfaces the finding); a refusal only where the org chose it.
    if org.overcharge_block_enabled:
        over = await agreed_prices.check_invoice(db, inv)
        if over:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "Exceeds agreed supplier prices: " + "; ".join(f.message for f in over),
            )
    # Fresh chain (a resubmission after return/correction supersedes the old one).
    await db.execute(
        delete(ApprovalStep).where(
            ApprovalStep.org_id == current.org_id, ApprovalStep.invoice_id == inv.id
        )
    )
    policy = await ap.evaluate(db, current.org_id, inv)
    steps = await ap.build_chain(db, current.org_id, inv, policy)
    inv.workflow_state = ap.chain_state(steps)  # submitted
    inv.submitted_by = current.id
    inv.submitted_at = datetime.now(UTC)
    if body.note:
        await _add_comment(db, current, inv.id, f"[submitted] {body.note}")
    _bump(inv)
    await audit.record(
        db,
        audit.A.INVOICE_SUBMIT,
        target_type="invoice",
        target_id=inv.id,
        meta={"policy": policy.name if policy else None, "steps": len(steps)},
    )
    await webhooks.emit(
        db,
        current.org_id,
        "invoice.submitted",
        {
            "id": inv.id,
            "invoice_number": inv.invoice_number,
            "total": str(inv.total),
            "steps": len(steps),
        },
    )
    nxt = ap.pending_step(steps)
    if nxt is not None:
        await _notify(
            db,
            current.org_id,
            await _email_for(db, nxt.approver_id),
            f"Approval needed: {inv.invoice_number}",
            f"Invoice {inv.invoice_number} ({inv.total} {inv.currency}) needs your approval.",
            inv.id,
        )
    await db.commit()
    await db.refresh(inv, attribute_names=["line_items", "vendor"])
    return await _review_out(db, current.org_id, inv)


async def _current_step(db: DbSession, org_id: str, inv: Invoice) -> ApprovalStep:
    steps = await ap.steps_for(db, org_id, inv.id)
    step = ap.pending_step(steps)
    if step is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "No pending approval step for this invoice."
        )
    return step


def _guard_decider(inv: Invoice, current, step: ApprovalStep) -> None:
    """Segregation of duties + assigned-approver check for a decision."""
    if (
        inv.submitted_by
        and inv.submitted_by == current.id
        and not getattr(current, "is_platform_admin", False)
    ):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "You cannot approve an invoice you submitted."
        )
    if not ap.is_assigned_approver(step, current):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "This approval step is assigned to another approver."
        )


@router.post("/invoices/{invoice_id}/approve", response_model=ReviewOut, dependencies=_APPROVE)
async def approve(invoice_id: str, body: DecisionIn, current: CurrentUser, db: DbSession):
    """Approve the current step. Advances the chain; the last approval locks the record."""
    inv = await _load(db, current.org_id, invoice_id)
    wf.assert_version(inv.version, body.version)
    if inv.workflow_state not in (WorkflowState.submitted, WorkflowState.partially_approved):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Invoice is not awaiting approval."
        )
    step = await _current_step(db, current.org_id, inv)
    _guard_decider(inv, current, step)
    step.status = STEP_APPROVED
    step.decided_by = current.id
    step.decided_by_email = current.email
    step.decided_at = datetime.now(UTC)
    step.note = body.note
    steps = await ap.steps_for(db, current.org_id, inv.id)
    new_state = ap.chain_state(steps)
    wf.assert_transition(inv.workflow_state, new_state)
    inv.workflow_state = new_state
    _bump(inv)
    await audit.record(
        db,
        audit.A.INVOICE_APPROVE,
        target_type="invoice",
        target_id=inv.id,
        meta={"step": step.seq, "result": new_state.value},
    )
    if new_state == WorkflowState.approved:
        inv.locked_at = datetime.now(UTC)
        inv.locked_by = current.email
        await audit.record(
            db, audit.A.INVOICE_LOCK, target_type="invoice", target_id=inv.id, meta={}
        )
        await webhooks.emit(
            db,
            current.org_id,
            "invoice.approved",
            {"id": inv.id, "invoice_number": inv.invoice_number, "by": current.email},
        )
        await _notify(
            db,
            current.org_id,
            await _email_for(db, inv.submitted_by),
            f"Approved: {inv.invoice_number}",
            f"Invoice {inv.invoice_number} has been fully approved.",
            inv.id,
        )
    else:  # partially approved → notify the next approver
        nxt = ap.pending_step(steps)
        if nxt is not None:
            await _notify(
                db,
                current.org_id,
                await _email_for(db, nxt.approver_id),
                f"Approval needed: {inv.invoice_number}",
                f"Invoice {inv.invoice_number} needs your approval (next in the chain).",
                inv.id,
            )
    await db.commit()
    await db.refresh(inv, attribute_names=["line_items", "vendor"])
    return await _review_out(db, current.org_id, inv)


@router.post("/invoices/{invoice_id}/reject", response_model=ReviewOut, dependencies=_APPROVE)
async def reject(invoice_id: str, body: DecisionIn, current: CurrentUser, db: DbSession):
    """Reject the current step — ends the chain, invoice → Rejected."""
    inv = await _load(db, current.org_id, invoice_id)
    wf.assert_version(inv.version, body.version)
    if inv.workflow_state not in (WorkflowState.submitted, WorkflowState.partially_approved):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Invoice is not awaiting approval."
        )
    step = await _current_step(db, current.org_id, inv)
    _guard_decider(inv, current, step)
    step.status = STEP_REJECTED
    step.decided_by = current.id
    step.decided_by_email = current.email
    step.decided_at = datetime.now(UTC)
    step.note = body.note
    steps = await ap.steps_for(db, current.org_id, inv.id)
    ap.skip_pending(steps)
    wf.assert_transition(inv.workflow_state, WorkflowState.rejected)
    inv.workflow_state = WorkflowState.rejected
    _bump(inv)
    await audit.record(
        db,
        audit.A.INVOICE_REJECT,
        target_type="invoice",
        target_id=inv.id,
        meta={"step": step.seq, "note": body.note},
    )
    await webhooks.emit(
        db,
        current.org_id,
        "invoice.rejected",
        {"id": inv.id, "invoice_number": inv.invoice_number, "by": current.email},
    )
    await _notify(
        db,
        current.org_id,
        await _email_for(db, inv.submitted_by),
        f"Rejected: {inv.invoice_number}",
        f"Invoice {inv.invoice_number} was rejected."
        + (f" Reason: {body.note}" if body.note else ""),
        inv.id,
    )
    await db.commit()
    await db.refresh(inv, attribute_names=["line_items", "vendor"])
    return await _review_out(db, current.org_id, inv)


@router.post("/invoices/{invoice_id}/return", response_model=ReviewOut, dependencies=_APPROVE)
async def return_for_correction(
    invoice_id: str, body: DecisionIn, current: CurrentUser, db: DbSession
):
    """Return an in-flight invoice to the submitter for correction (→ Draft)."""
    inv = await _load(db, current.org_id, invoice_id)
    wf.assert_version(inv.version, body.version)
    if inv.workflow_state not in (WorkflowState.submitted, WorkflowState.partially_approved):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Invoice is not awaiting approval."
        )
    step = await _current_step(db, current.org_id, inv)
    _guard_decider(inv, current, step)
    steps = await ap.steps_for(db, current.org_id, inv.id)
    ap.skip_pending(steps)
    wf.assert_transition(inv.workflow_state, WorkflowState.draft)
    inv.workflow_state = WorkflowState.draft
    _bump(inv)
    if body.note:
        await _add_comment(db, current, inv.id, f"[returned] {body.note}")
    await audit.record(
        db,
        audit.A.INVOICE_RETURN,
        target_type="invoice",
        target_id=inv.id,
        meta={"note": body.note},
    )
    await webhooks.emit(
        db,
        current.org_id,
        "invoice.returned",
        {"id": inv.id, "invoice_number": inv.invoice_number, "by": current.email},
    )
    await _notify(
        db,
        current.org_id,
        await _email_for(db, inv.submitted_by),
        f"Returned for correction: {inv.invoice_number}",
        f"Invoice {inv.invoice_number} was returned." + (f" {body.note}" if body.note else ""),
        inv.id,
    )
    await db.commit()
    await db.refresh(inv, attribute_names=["line_items", "vendor"])
    return await _review_out(db, current.org_id, inv)


@router.post("/invoices/{invoice_id}/reassign", response_model=ReviewOut, dependencies=_APPROVE)
async def reassign(invoice_id: str, body: ReassignIn, current: CurrentUser, db: DbSession):
    """Reassign a pending approval step to another approver (or make it open)."""
    inv = await _load(db, current.org_id, invoice_id)
    wf.assert_version(inv.version, body.version)
    steps = await ap.steps_for(db, current.org_id, inv.id)
    target = None
    if body.step_id:
        target = next((s for s in steps if s.id == body.step_id), None)
    else:
        target = ap.pending_step(steps)
    if target is None or target.status != "pending":
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "No pending step to reassign.")
    target.approver_id = body.approver_id
    target.approver_email = await _email_for(db, body.approver_id)
    _bump(inv)
    await audit.record(
        db,
        audit.A.INVOICE_REASSIGN,
        target_type="invoice",
        target_id=inv.id,
        meta={"step": target.seq, "approver_id": body.approver_id},
    )
    await webhooks.emit(
        db,
        current.org_id,
        "invoice.reassigned",
        {"id": inv.id, "invoice_number": inv.invoice_number, "approver_id": body.approver_id},
    )
    await _notify(
        db,
        current.org_id,
        target.approver_email,
        f"Approval reassigned to you: {inv.invoice_number}",
        f"You have been assigned to approve invoice {inv.invoice_number}.",
        inv.id,
    )
    await db.commit()
    await db.refresh(inv, attribute_names=["line_items", "vendor"])
    return await _review_out(db, current.org_id, inv)


# --------------------------------------------------------------------------- #
# Generic transition (cancel / dispute / schedule / pay / archive / correction)
# --------------------------------------------------------------------------- #


@router.post("/invoices/{invoice_id}/transition", response_model=ReviewOut, dependencies=_WRITE)
async def transition(invoice_id: str, body: TransitionIn, current: CurrentUser, db: DbSession):
    """Move the invoice to another lifecycle state (schedule payment, mark paid,
    dispute, cancel, archive) or reopen an approved invoice for correction (→
    Draft, which invalidates the approval chain and clears the lock)."""
    try:
        target = WorkflowState(body.target)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown state '{body.target}'.")
    inv = await _load(db, current.org_id, invoice_id)
    wf.assert_version(inv.version, body.version)
    wf.assert_transition(inv.workflow_state, target)

    action = audit.A.INVOICE_TRANSITION
    if target == WorkflowState.draft:
        # Controlled correction: invalidate approvals + clear the record lock.
        await db.execute(
            delete(ApprovalStep).where(
                ApprovalStep.org_id == current.org_id, ApprovalStep.invoice_id == inv.id
            )
        )
        inv.locked_at = None
        inv.locked_by = None
        inv.submitted_by = None
        inv.submitted_at = None
        action = audit.A.INVOICE_CORRECTION
    elif target == WorkflowState.paid:
        inv.status = InvoiceStatus.paid  # sync the legacy aging status
    inv.workflow_state = target
    _bump(inv)
    if body.note:
        await _add_comment(db, current, inv.id, f"[{target.value}] {body.note}")
    await audit.record(
        db,
        action,
        target_type="invoice",
        target_id=inv.id,
        meta={"to": target.value},
    )
    if target == WorkflowState.scheduled_for_payment:
        await webhooks.emit(
            db,
            current.org_id,
            "invoice.scheduled_for_payment",
            {"id": inv.id, "invoice_number": inv.invoice_number},
        )
    elif target == WorkflowState.paid:
        await webhooks.emit(
            db,
            current.org_id,
            "invoice.paid",
            {"id": inv.id, "invoice_number": inv.invoice_number},
        )
    await db.commit()
    await db.refresh(inv, attribute_names=["line_items", "vendor"])
    return await _review_out(db, current.org_id, inv)


@router.get("/invoices/{invoice_id}/approvals", response_model=ApprovalHistoryOut)
async def approval_history(invoice_id: str, current: CurrentUser, db: DbSession):
    """The live approval chain + the immutable audit history of decisions."""
    await _load(db, current.org_id, invoice_id)
    steps = await ap.steps_for(db, current.org_id, invoice_id)
    events, _ = await audit.list_events(db, current.org_id, page=1, page_size=200)
    hist = [
        {
            "action": e.action,
            "actor": e.actor_email,
            "at_ms": e.at_ms,
            "meta": e.meta,
        }
        for e in events
        if e.target_id == invoice_id and e.action.startswith("invoice.")
    ]
    return ApprovalHistoryOut(steps=[await _step_out(db, s) for s in steps], events=hist)


# --------------------------------------------------------------------------- #
# Comments + internal attachments
# --------------------------------------------------------------------------- #


async def _add_comment(db: DbSession, current, invoice_id: str, body: str) -> InvoiceComment:
    c = InvoiceComment(
        org_id=current.org_id,
        invoice_id=invoice_id,
        author_id=current.id,
        author_name=current.email,
        body=body,
    )
    db.add(c)
    await db.flush()
    return c


@router.get("/invoices/{invoice_id}/comments", response_model=list[InvoiceCommentOut])
async def list_comments(invoice_id: str, current: CurrentUser, db: DbSession):
    await _load(db, current.org_id, invoice_id)
    rows = list(
        await db.scalars(
            select(InvoiceComment)
            .where(InvoiceComment.org_id == current.org_id, InvoiceComment.invoice_id == invoice_id)
            .order_by(InvoiceComment.created_at.asc())
        )
    )
    return [InvoiceCommentOut.model_validate(c) for c in rows]


@router.post(
    "/invoices/{invoice_id}/comments",
    response_model=InvoiceCommentOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_comment(invoice_id: str, body: CommentIn, current: CurrentUser, db: DbSession):
    await _load(db, current.org_id, invoice_id)
    text = (body.body or "").strip()
    if not text:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Comment cannot be empty.")
    c = await _add_comment(db, current, invoice_id, text)
    await audit.record(
        db, audit.A.INVOICE_COMMENT, target_type="invoice", target_id=invoice_id, meta={}
    )
    await db.commit()
    await db.refresh(c)
    return InvoiceCommentOut.model_validate(c)


@router.get("/invoices/{invoice_id}/attachments", response_model=list[InvoiceAttachmentOut])
async def list_attachments(invoice_id: str, current: CurrentUser, db: DbSession):
    await _load(db, current.org_id, invoice_id)
    rows = list(
        await db.scalars(
            select(InvoiceAttachment)
            .where(
                InvoiceAttachment.org_id == current.org_id,
                InvoiceAttachment.invoice_id == invoice_id,
            )
            .order_by(InvoiceAttachment.created_at.asc())
        )
    )
    return [InvoiceAttachmentOut.model_validate(a) for a in rows]


@router.post(
    "/invoices/{invoice_id}/attachments",
    response_model=InvoiceAttachmentOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=_WRITE,
)
async def add_attachment(
    invoice_id: str, current: CurrentUser, db: DbSession, file: UploadFile, note: str | None = None
):
    """Attach an internal working document (contract, PO, email) to the invoice."""
    await _load(db, current.org_id, invoice_id)
    data = await read_upload(file)
    if not data:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Empty file.")
    # Security gate (filesec choke point): block executables / archives / scripts
    # + malware-scan BEFORE storing — an internal attachment is still attacker-
    # supplied bytes. A plain text / PDF / image contract-or-PO is allowed.
    try:
        filesec.reject_active_content(data)
    except filesec.FileRejected as exc:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, str(exc))
    sha, size = await documents.store(
        documents.INVOICE_ATTACHMENTS,
        current.org_id,
        data,
        file.content_type,
        db=db,
        filename=file.filename,
        uploaded_by=current.email,
    )
    row = InvoiceAttachment(
        org_id=current.org_id,
        invoice_id=invoice_id,
        filename=file.filename or "attachment",
        mime=file.content_type,
        size=size,
        sha256=sha,
        note=note,
        uploaded_by=current.id,
        uploaded_by_email=current.email,
    )
    db.add(row)
    await audit.record(
        db,
        audit.A.INVOICE_ATTACH,
        target_type="invoice",
        target_id=invoice_id,
        meta={"filename": row.filename, "size": size},
    )
    await db.commit()
    await db.refresh(row)
    return InvoiceAttachmentOut.model_validate(row)


@router.get("/invoices/{invoice_id}/attachments/{attachment_id}/download")
async def download_attachment(
    invoice_id: str, attachment_id: str, current: CurrentUser, db: DbSession, request: Request
):
    await _load(db, current.org_id, invoice_id)
    row = await db.scalar(
        select(InvoiceAttachment).where(
            InvoiceAttachment.id == attachment_id,
            InvoiceAttachment.invoice_id == invoice_id,
            InvoiceAttachment.org_id == current.org_id,
        )
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")
    identity = (row.sha256, row.size, row.filename, row.mime)
    try:
        data = await documents.load_verified_original(
            documents.INVOICE_ATTACHMENTS, current.org_id, row.sha256, size=row.size
        )
    except storage.StorageReadError:
        raise
    except storage.StorageError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment bytes missing") from None
    fresh, _ = await refresh_current_identity(request, db, current)
    authz.require(fresh, authz.Permission.INVOICE_READ)
    await _load(db, current.org_id, invoice_id)
    row = await db.scalar(
        select(InvoiceAttachment).where(
            InvoiceAttachment.id == attachment_id,
            InvoiceAttachment.invoice_id == invoice_id,
            InvoiceAttachment.org_id == current.org_id,
        )
    )
    if row is None or (row.sha256, row.size, row.filename, row.mime) != identity:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "Attachment changed while preparing the download"
        )
    if data is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment bytes missing")
    return Response(
        content=data,
        media_type=row.mime or "application/octet-stream",
        headers={
            "Content-Disposition": content_disposition(row.filename, fallback="attachment"),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


# --------------------------------------------------------------------------- #
# Approval policies (org configuration)
# --------------------------------------------------------------------------- #


@router.get("/approval-policies", response_model=list[ApprovalPolicyOut])
async def list_policies(current: CurrentUser, db: DbSession):
    rows = list(
        await db.scalars(
            select(ApprovalPolicy)
            .where(ApprovalPolicy.org_id == current.org_id)
            .order_by(ApprovalPolicy.priority.asc(), ApprovalPolicy.created_at.asc())
        )
    )
    return [_policy_out(p) for p in rows]


@router.post(
    "/approval-policies",
    response_model=ApprovalPolicyOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=_ADMIN,
)
async def create_policy(body: ApprovalPolicyIn, current: CurrentUser, db: DbSession):
    import json

    p = ApprovalPolicy(
        org_id=current.org_id,
        name=body.name,
        active=body.active,
        priority=body.priority,
        min_amount=body.min_amount,
        department_id=body.department_id,
        cost_center_id=body.cost_center_id,
        legal_entity_id=body.legal_entity_id,
        vendor_id=body.vendor_id,
        approver_ids=json.dumps(body.approver_ids) if body.approver_ids else None,
        finance_final=body.finance_final,
        finance_approver_id=body.finance_approver_id,
    )
    db.add(p)
    await audit.record(
        db,
        audit.A.POLICY_CREATE,
        target_type="approval_policy",
        target_id=p.id,
        meta={"name": p.name},
    )
    await db.commit()
    await db.refresh(p)
    return _policy_out(p)


@router.patch(
    "/approval-policies/{policy_id}", response_model=ApprovalPolicyOut, dependencies=_ADMIN
)
async def update_policy(
    policy_id: str, body: ApprovalPolicyUpdate, current: CurrentUser, db: DbSession
):
    import json

    p = await db.scalar(
        select(ApprovalPolicy).where(
            ApprovalPolicy.id == policy_id, ApprovalPolicy.org_id == current.org_id
        )
    )
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Policy not found")
    if body.version != p.version:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Policy changed (your {body.version}, current {p.version})."
        )
    fields = body.model_fields_set - {"version"}
    for attr in (
        "name",
        "active",
        "priority",
        "min_amount",
        "department_id",
        "cost_center_id",
        "legal_entity_id",
        "vendor_id",
        "finance_final",
        "finance_approver_id",
    ):
        if attr in fields:
            setattr(p, attr, getattr(body, attr))
    if "approver_ids" in fields:
        p.approver_ids = json.dumps(body.approver_ids) if body.approver_ids else None
    p.version += 1
    await audit.record(
        db,
        audit.A.POLICY_UPDATE,
        target_type="approval_policy",
        target_id=p.id,
        meta={"fields": sorted(fields)},
    )
    await db.commit()
    await db.refresh(p)
    return _policy_out(p)


@router.delete(
    "/approval-policies/{policy_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=_ADMIN
)
async def delete_policy(policy_id: str, current: CurrentUser, db: DbSession):
    p = await db.scalar(
        select(ApprovalPolicy).where(
            ApprovalPolicy.id == policy_id, ApprovalPolicy.org_id == current.org_id
        )
    )
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Policy not found")
    await audit.record(
        db,
        audit.A.POLICY_DELETE,
        target_type="approval_policy",
        target_id=p.id,
        meta={"name": p.name},
    )
    await db.delete(p)
    await db.commit()
