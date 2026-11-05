"""Transport claim routes — the VAT refund claim lifecycle (WO-76) plus the
claim-SCOPED admin surfaces and filing artifacts of slice 2 (WO-77: receipt-
control waivers R15, the manual status code R17/R12, and the G2.12 workbook /
evidence-pack downloads). Org-level configuration lives in `admin.py`, the
customer lifecycle in `customers.py`.

Thin controllers ONLY (engineering-rules §3): parse → structural permission
gate → call the already-gated service → shape the response. Every refusal a
handler surfaces is raised BY THE SERVICE as an `app.core.errors.AppError`
and rendered by the one `app.main` handler as `{"detail", "code"}` — the
routes map nothing themselves, so the wire vocabulary (`module_not_enabled`,
`claim_not_found`, `period_not_ended`, `below_minimum`, `customer_not_active`,
`country_not_activated`, `unresolved_invoice_refs`, `duplicate_invoice_lock`,
`invoice_document_missing`, `claim_not_draft`, `claim_not_locked`, …) cannot
drift from the service layer (master-context §4.20).

Authorization (ADR-0024, structural): router-level `VAT_READ`; the stricter
verbs override per-route — `VAT_WRITE` on create/build-lines (booking claim
data), `VAT_SUBMIT` on submit/withdraw (acquiring/releasing invoice locks is
the consequential act the WO-49 permission split isolates: an ACCOUNTANT
holds VAT_WRITE but not VAT_SUBMIT, mirroring ISSUED_WRITE vs ISSUED_SEND).
The module entitlement (`transport`, default off) is enforced INSIDE every
service entry point (`module_not_enabled`, 403) — ADR-P3 rule 3 with the
WO-49 defense-in-depth convention that no transport service trusts its
caller to have checked it.

Mutating handlers commit AFTER the service returns (the `payment_runs`
pattern); the services audit in the same transaction (§4.16), so a refused
submit rolls back locks, freeze, status flip AND the never-warranted audit
row together — the D5 nothing-mutated guarantee holds over HTTP. GET
handlers never commit: `submission_checklist`'s idempotent default-rule
seeding is flushed but discarded with the request session, keeping reads
observably side-effect-free (§4.19 — reading the checklist or stage changes
NOTHING about the claim).

`lock.submit_claim`'s `today` parameter is a TEST SEAM and is deliberately
NOT exposed on the wire — a client that could post its own clock could
bypass the R7 period-end gate. The route always lets the service default to
the real date.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request, Response

from app.api.deps import CurrentUser, DbSession, refresh_current_identity, require_perm
from app.api.file_response import TemporaryFileResponse
from app.core import authz
from app.core.security_headers import content_disposition
from app.models.transport.receipt_waiver import VatReceiptWaiver
from app.models.transport.vat_claim import VatRefundClaim, VatRefundClaimLine
from app.schemas.transport_admin import (
    RemovedOut,
    StatusCodeSetIn,
    WaiverOut,
    WaiverSetIn,
)
from app.schemas.transport_claim import (
    ChecklistItemOut,
    ClaimCreateIn,
    ClaimDecisionIn,
    ClaimLineOut,
    ClaimOut,
    ClaimPaymentIn,
    ClaimSubmitIn,
    StageOut,
)
from app.services.transport import checklist as checklist_svc
from app.services.transport import claim as claim_svc
from app.services.transport import claim_lines as claim_lines_svc
from app.services.transport import claim_pack as claim_pack_svc
from app.services.transport import decision as decision_svc
from app.services.transport import lock as lock_svc
from app.services.transport import status as status_svc
from app.services.transport import waiver as waiver_svc

# Structural authorization (ADR-0024): router-level VAT_READ; the mutating
# routes declare the stricter VAT_WRITE / VAT_SUBMIT per-route below.
router = APIRouter(
    prefix="/transport/claims",
    tags=["transport"],
    dependencies=[Depends(require_perm(authz.Permission.VAT_READ))],
)
_WRITE = [Depends(require_perm(authz.Permission.VAT_WRITE))]
_SUBMIT = [Depends(require_perm(authz.Permission.VAT_SUBMIT))]

# The two filing-artifact content types (WO-77): xlsx per the `analytics.py`
# explore-xlsx precedent, zip per the `issued.py` batch-PDF precedent.
_XLSX_CT = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_ZIP_CT = "application/zip"


def _claim_out(claim: VatRefundClaim) -> ClaimOut:
    return ClaimOut(
        id=claim.id,
        entity_id=claim.entity_id,
        refund_country=claim.refund_country,
        ref_period=claim.ref_period,
        status=claim.status,
        status_code=claim.status_code,
        status_note=claim.status_note,
        decision_date=claim.decision_date,
        action_deadline=claim.action_deadline,
        submitted_date=claim.submitted_date,
        approved_date=claim.approved_date,
        paid_date=claim.paid_date,
        paid_amount=claim.paid_amount,
        vat_eur=claim.vat_eur,
        vat_local=claim.vat_local,
        currency=claim.currency,
        fee_pct=claim.fee_pct,
        fee_min=claim.fee_min,
        fee_eur=claim.fee_eur,
        created_at=claim.created_at,
    )


def _line_out(line: VatRefundClaimLine) -> ClaimLineOut:
    return ClaimLineOut(
        id=line.id,
        claim_id=line.claim_id,
        invoice_ref=line.invoice_ref,
        vat_id=line.vat_id,
        invoice_id=line.invoice_id,
        goods_code=line.goods_code,
        product_group=line.product_group,
        net_eur=line.net_eur,
        vat_eur=line.vat_eur,
        net_local=line.net_local,
        vat_local=line.vat_local,
        currency=line.currency,
        frozen_at=line.frozen_at,
        rejected_at=line.rejected_at,
        unmatched_suppliers=(
            json.loads(line.unmatched_suppliers) if line.unmatched_suppliers else None
        ),
    )


@router.get("", response_model=list[ClaimOut])
async def list_claims(current: CurrentUser, db: DbSession):
    rows = await claim_svc.list_claims(db, current.org_id)
    return [_claim_out(c) for c in rows]


@router.post("", response_model=ClaimOut, dependencies=_WRITE)
async def create_claim(body: ClaimCreateIn, current: CurrentUser, db: DbSession):
    """Get-or-create on the R1 grain `(entity, refund_country, ref_period)`.
    Deliberately 200, not 201: the service is idempotent by design ("same
    key upserts, never duplicates") and does not report whether it created —
    a 201 on the second identical call would be a lie on the wire."""
    claim = await claim_svc.get_or_create_claim(
        db,
        current.org_id,
        entity_id=body.entity_id,
        refund_country=body.refund_country,
        ref_period=body.ref_period,
    )
    await db.commit()
    return _claim_out(claim)


@router.get("/{claim_id}", response_model=ClaimOut)
async def get_claim(claim_id: str, current: CurrentUser, db: DbSession):
    claim = await claim_svc.get_claim(db, current.org_id, claim_id)
    return _claim_out(claim)


@router.get("/{claim_id}/lines", response_model=list[ClaimLineOut])
async def list_claim_lines(claim_id: str, current: CurrentUser, db: DbSession):
    """The claim's materialized lines — a draft's live rebuild, or exactly
    what was frozen at submission (`frozen_at` set)."""
    rows = await claim_lines_svc.list_claim_lines(db, current.org_id, claim_id)
    return [_line_out(ln) for ln in rows]


@router.post("/{claim_id}/lines", response_model=list[ClaimLineOut], dependencies=_WRITE)
async def build_claim_lines(claim_id: str, current: CurrentUser, db: DbSession):
    """(Re)materialize the draft claim's lines from its fuel transactions
    (R2 grain, R16 resolution, R15 waiver exclusion — all in the service)."""
    rows = await claim_lines_svc.build_claim_lines(db, current.org_id, claim_id)
    await db.commit()
    return [_line_out(ln) for ln in rows]


@router.get("/{claim_id}/checklist", response_model=list[ChecklistItemOut])
async def get_checklist(claim_id: str, current: CurrentUser, db: DbSession):
    """The advisory submission checklist (R45) — read-only on the wire: no
    commit, so even the evaluator's idempotent default-rule seeding is
    discarded with the request session (see module docstring)."""
    items = await checklist_svc.submission_checklist(db, current.org_id, claim_id)
    return [
        ChecklistItemOut(key=i.key, label=i.label, scope=i.scope, ok=i.ok, reason=i.reason)
        for i in items
    ]


@router.get("/{claim_id}/stage", response_model=StageOut)
async def get_stage(claim_id: str, current: CurrentUser, db: DbSession):
    """The system-derived pre-submission stage 1A/1B/1C/1E (R17) — a
    read-only preview for a draft claim; refused (409 `claim_not_draft`)
    once the claim leaves `draft`."""
    stage = await status_svc.derive_stage(db, current.org_id, claim_id)
    return StageOut(stage=stage)


@router.post("/{claim_id}/submit", response_model=ClaimOut, dependencies=_SUBMIT)
async def submit_claim(claim_id: str, body: ClaimSubmitIn, current: CurrentUser, db: DbSession):
    """The D5 gate chain (R7 → R8 → R44 → R3 → R6 → R15 stamp → R10 →
    freeze → lock → status "2"), entirely in `lock.submit_claim`. A refusal
    at ANY gate reaches the client as 409/422 with the service's stable
    `code` and mutates nothing (rolled back with the uncommitted session)."""
    claim = await lock_svc.submit_claim(
        db,
        current.org_id,
        claim_id=claim_id,
        invoices=[(i.supplier, i.invoice_ref, i.fuel_transaction_id) for i in body.invoices],
        override_minimum=body.override_minimum,
    )
    await db.commit()
    return _claim_out(claim)


@router.post("/{claim_id}/withdraw", response_model=ClaimOut, dependencies=_SUBMIT)
async def withdraw_claim(claim_id: str, current: CurrentUser, db: DbSession):
    """The ONLY lock-releasing transition (R5) — gated by the same
    VAT_SUBMIT permission as submit: releasing invoice locks re-opens the
    invoices to other claims, the mirror image of the consequential act."""
    claim = await lock_svc.withdraw_claim(db, current.org_id, claim_id)
    await db.commit()
    return _claim_out(claim)


@router.post("/{claim_id}/decision", response_model=ClaimOut, dependencies=_SUBMIT)
async def record_claim_decision(
    claim_id: str, body: ClaimDecisionIn, current: CurrentUser, db: DbSession
):
    """WO-L (§13): the member state's answer — approved / rejected / partial.
    Partial marks the named frozen lines rejected and recomputes the frozen
    figures on the surviving base at the FROZEN fee rate (fee.py's seam).
    Same VAT_SUBMIT gate as submit/withdraw — recording an authority's
    decision is the same class of consequential act."""
    claim = await decision_svc.record_decision(
        db,
        current.org_id,
        claim_id,
        outcome=body.outcome,
        rejected_refs=body.rejected_refs,
        decision_date=body.decision_date,
    )
    await db.commit()
    return _claim_out(claim)


@router.post("/{claim_id}/payment", response_model=ClaimOut, dependencies=_SUBMIT)
async def record_claim_payment(
    claim_id: str, body: ClaimPaymentIn, current: CurrentUser, db: DbSession
):
    """WO-T: the refund landed — `approved` -> `paid`, with the amount and the
    date. This is the last edge of the claim lifecycle and the one that was
    missing: `recovery.median_days_to_refund` has existed since WO-81 and
    reported `null` for every workspace because nothing wrote either end of the
    interval it measures.

    `VAT_SUBMIT`, matching decision and withdraw rather than the looser
    VAT_WRITE — recording that a member state paid is a claim-lifecycle
    assertion, the same class of consequential act, and the audit event it
    writes carries the variance against the approved base."""
    claim = await decision_svc.record_payment(
        db,
        current.org_id,
        claim_id,
        paid_amount=body.paid_amount,
        paid_date=body.paid_date,
    )
    await db.commit()
    return _claim_out(claim)


# --------------------------------------------------------------------------- #
# WO-77 — receipt-control waivers (R15), the manual status code (R17/R12) and
# the two filing artifacts (G2.12). All claim-scoped, so they live here rather
# than in the org-level `admin.py`.
# --------------------------------------------------------------------------- #


def _waiver_out(row: VatReceiptWaiver) -> WaiverOut:
    return WaiverOut(id=row.id, claim_id=row.claim_id, supplier=row.supplier)


@router.get("/{claim_id}/waivers", response_model=list[WaiverOut])
async def list_waivers(claim_id: str, current: CurrentUser, db: DbSession):
    """The claim's receipt-control waivers (R15) — the rows an operator set
    because a supplier genuinely never invoiced."""
    rows = await waiver_svc.list_waivers(db, current.org_id, claim_id)
    return [_waiver_out(r) for r in rows]


@router.post("/{claim_id}/waivers", response_model=WaiverOut, dependencies=_WRITE)
async def set_waiver(claim_id: str, body: WaiverSetIn, current: CurrentUser, db: DbSession):
    """Waive a supplier on a DRAFT claim (R15). VAT_WRITE, not VAT_SUBMIT: a
    waiver configures how the claim's lines are BUILT (the service excludes
    the supplier's transactions before resolution) — it flips no status and
    takes no lock. Refuses 422 `waiver_supplier_has_invoices` when the
    supplier HAS a registered invoice for the refund country (waiving would
    drop claimable VAT), 409 `claim_not_draft` once the claim has left
    draft. Idempotent on (claim, supplier) — a repeat returns the row."""
    row = await waiver_svc.set_waiver(db, current.org_id, claim_id=claim_id, supplier=body.supplier)
    await db.commit()
    return _waiver_out(row)


@router.delete("/{claim_id}/waivers/{supplier}", response_model=RemovedOut, dependencies=_WRITE)
async def remove_waiver(claim_id: str, supplier: str, current: CurrentUser, db: DbSession):
    """Un-waive a supplier on a draft claim. `removed=false` is the
    idempotent no-op (nothing was waived), not a failure — the service's own
    return contract."""
    removed = await waiver_svc.remove_waiver(
        db, current.org_id, claim_id=claim_id, supplier=supplier
    )
    await db.commit()
    return RemovedOut(removed=removed)


@router.post("/{claim_id}/status-code", response_model=ClaimOut, dependencies=_SUBMIT)
async def set_status_code(
    claim_id: str, body: StatusCodeSetIn, current: CurrentUser, db: DbSession
):
    """Stamp a MANUAL workflow code (R17) + optional R12 `action_deadline` on
    an already-submitted claim. VAT_SUBMIT because this is the claim-STATUS
    surface: it moves the claim through the post-filing workflow. The service
    refuses a system-derived AUTO code (409 `status_code_system_controlled`),
    an unknown code (422 `unknown_status_code`) and any manual code while the
    claim is still draft (409 `claim_not_submitted`); it never touches the
    coarse engine `status` column."""
    claim = await status_svc.set_status_code(
        db, current.org_id, claim_id, body.code, action_deadline=body.action_deadline
    )
    await db.commit()
    return _claim_out(claim)


@router.get("/{claim_id}/workbook")
async def download_workbook(claim_id: str, current: CurrentUser, db: DbSession):
    """The Excel claim workbook of a FROZEN claim (G2.12) — the figures
    document filed with the refunding member state. VAT_READ (the
    router-level default): it is a read-only rendering of claim data the
    `/lines` route already serves, not an accounting-ledger export
    (`EXPORT_RUN` guards that different surface). Every refusal is the
    service's own and fails CLOSED: `claim_not_frozen` (a draft's lines can
    still drift), `synthetic_line_in_pack` (R3), `claim_totals_drift`
    (§4.10 — the renderer recomputes and refuses a drifted header),
    `claim_currency_mismatch` (§4.14)."""
    data = await claim_pack_svc.build_workbook(db, current.org_id, claim_id)
    return Response(
        content=data,
        media_type=_XLSX_CT,
        headers={
            "Content-Disposition": content_disposition(f"claim-{claim_id}-workbook.xlsx"),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get(
    "/{claim_id}/evidence",
    response_class=Response,
    responses={
        200: {
            "description": "Complete frozen claim evidence ZIP",
            "content": {_ZIP_CT: {"schema": {"type": "string", "format": "binary"}}},
        }
    },
)
async def download_evidence_pack(
    claim_id: str, current: CurrentUser, db: DbSession, request: Request
):
    """The ZIP evidence pack of a FROZEN claim (G2.12, §3.K K6): the same
    workbook (rendered from the SAME loaded pack — identical lines and
    totals by construction) plus every vaulted invoice document and a
    SHA-256 `manifest.csv`, under the §3.M M1 vault tree. Same refusals as
    the workbook, plus `evidence_document_unavailable` when a vaulted
    document's bytes are gone — an incomplete filing bundle is worse than
    none (a rejected claim's invoices stay locked, R5)."""
    prepared = await claim_pack_svc.build_evidence_pack_file(db, current.org_id, claim_id)
    try:
        # End the read snapshot before refreshing the same pinned authority and
        # the claim/source facts used to prepare this complete private artifact.
        await db.rollback()
        fresh, _org = await refresh_current_identity(request, db, current)
        authz.require(fresh, authz.Permission.VAT_READ)
        await claim_pack_svc.validate_evidence_pack(db, fresh.org_id, claim_id, prepared)
        return TemporaryFileResponse(
            prepared,
            media_type=_ZIP_CT,
            headers={
                "Content-Disposition": content_disposition(f"claim-{claim_id}-evidence.zip"),
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "private, no-store",
            },
        )
    except BaseException:
        prepared.cleanup()
        raise
