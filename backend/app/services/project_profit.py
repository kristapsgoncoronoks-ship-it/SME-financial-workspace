"""Project profitability — phase 1 of `docs/design/project-profitability.md`.

The loop this serves, for EVERY kind of customer (industry-neutral by owner
requirement): open a project for a won contract, attach the contract, issue
sales invoices under it, allocate supplier/subcontractor invoices and expense
reports to it, add manual cost lines, and read revenue − costs.

WHAT COUNTS, AND WHY — the P&L's basis, stated once here and echoed on the
wire so a screen can never misstate it:

- **Revenue** = issued invoices on the project, NET (subtotal, before VAT),
  minus credit notes the same way. Drafts and cancelled documents are excluded
  (neither is money); approved-but-unissued IS included — the work is sold
  even if the paper is not yet numbered.
- **Invoice costs** = received invoices allocated to the project, NET where
  the parse captured a subtotal, gross total as the fallback (an OCR'd receipt
  with no tax split is better counted gross than dropped). Whole-invoice
  allocation in phase 1; line-level + % splits are phase 2.
- **Expense costs** = expense items on the project from reports that are
  approved or beyond — a rejected or still-editable draft report is not yet a
  cost. NET of VAT (amount − vat_amount): VAT is a pass-through, and where it
  is NOT recoverable the phase-2 recovered-VAT overlay is the honest place to
  show that, not a silently grossed-up cost.
- **Manual cost lines** = as entered (EUR).

The recycle bin composes correctly for free: a binned invoice vanishes from
every ordinary SELECT (central soft-delete guard), so it leaves the live P&L
and returns on restore — no special handling here, which is the point of a
central guard.

Phase 2 adds the close-time FREEZE. Until then the figure is live and the
screen says so.
"""

from __future__ import annotations

import json as _json
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import money
from app.models.costing import Project
from app.models.document import Document
from app.models.invoice import Invoice, LineItem
from app.models.invoice_project_split import InvoiceProjectSplit
from app.models.project_link import (
    COST_CATEGORIES,
    DOCUMENT_KINDS,
    PHOTO_CONTENT_TYPES,
    ProjectCostEntry,
    ProjectDocument,
)
from app.services import documents

PROJECT_DOCS = "project-docs"
"""Object-store prefix for project attachments (contracts). Its own class, like
receipts/logos/email-attachments, so the integrity sweep and any future
retention rule can address these bytes distinctly."""

# Expense-report states whose items count as project cost. A draft or submitted
# report can still be edited or rejected; approval is the moment the org accepts
# the money left.
_EXPENSE_COST_STATES = ("approved", "processing", "reimbursed")

# Issued lifecycles excluded from revenue: a draft is not a sale yet, a
# cancelled document never was.
_NON_REVENUE_LIFECYCLES = ("draft", "cancelled")


class ProjectProfitError(Exception):
    """Business refusal; routes map it to 4xx."""


class NotFoundError(ProjectProfitError):
    """Unknown OR cross-tenant — indistinguishable by design (§4.4)."""


async def _project(db: AsyncSession, org_id: str, project_id: str) -> Project:
    row = await db.scalar(select(Project).where(Project.org_id == org_id, Project.id == project_id))
    if row is None:
        raise NotFoundError("Project not found")
    return row


# --------------------------------------------------------------------------- #
# Manual cost lines
# --------------------------------------------------------------------------- #


async def add_cost_entry(
    db: AsyncSession,
    org_id: str,
    project_id: str,
    *,
    label: str,
    category: str,
    amount: Decimal,
    entry_date: date | None = None,
    note: str | None = None,
    created_by: str | None = None,
) -> ProjectCostEntry:
    """Book one uninvoiced cost onto the project. Does NOT commit (the route
    commits the entry with its audit event — engineering-rules §3).

    Negative amounts are allowed on purpose: a correction to an earlier line is
    itself a cost line, and forcing deletions instead would erase the history a
    P&L reader wants. Zero is refused — it can only be a mistake."""
    await _project(db, org_id, project_id)
    if category not in COST_CATEGORIES:
        raise ProjectProfitError(f"Unknown cost category '{category}'")
    quantized = money.q2(amount)
    if quantized == Decimal("0"):
        raise ProjectProfitError("A cost entry needs a non-zero amount")
    entry = ProjectCostEntry(
        org_id=org_id,
        project_id=project_id,
        label=label.strip(),
        category=category,
        amount=quantized,
        currency="EUR",
        entry_date=entry_date,
        note=note,
        created_by=created_by,
    )
    if not entry.label:
        raise ProjectProfitError("A cost entry needs a label")
    db.add(entry)
    return entry


async def list_cost_entries(
    db: AsyncSession, org_id: str, project_id: str
) -> list[ProjectCostEntry]:
    await _project(db, org_id, project_id)
    return list(
        await db.scalars(
            select(ProjectCostEntry)
            .where(ProjectCostEntry.org_id == org_id, ProjectCostEntry.project_id == project_id)
            .order_by(ProjectCostEntry.created_at.desc(), ProjectCostEntry.id)
        )
    )


async def delete_cost_entry(
    db: AsyncSession, org_id: str, project_id: str, entry_id: str
) -> ProjectCostEntry:
    """Remove a cost line. Returns the deleted row so the route can audit WHAT
    was removed, not just that something was. Does NOT commit."""
    entry = await db.scalar(
        select(ProjectCostEntry).where(
            ProjectCostEntry.org_id == org_id,
            ProjectCostEntry.project_id == project_id,
            ProjectCostEntry.id == entry_id,
        )
    )
    if entry is None:
        raise NotFoundError("Cost entry not found")
    await db.delete(entry)
    return entry


# --------------------------------------------------------------------------- #
# The contract (and other project documents)
# --------------------------------------------------------------------------- #


def _require_image(data: bytes, content_type: str | None) -> None:
    """WO-F: a 'photo' must actually be one — declared type in the closed set
    AND leading bytes that agree (JPEG/PNG/WebP/HEIC magic). Declared-type-only
    checks trust the attacker; bytes-only checks let a mislabelled download
    masquerade later. Both must hold."""
    if content_type not in PHOTO_CONTENT_TYPES:
        raise ProjectProfitError("A photo must be a JPEG, PNG, WebP or HEIC image")
    is_jpeg = data[:3] == b"\xff\xd8\xff"
    is_png = data[:8] == b"\x89PNG\r\n\x1a\n"
    is_webp = data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    is_heic = data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"heif", b"mif1", b"msf1")
    if not (is_jpeg or is_png or is_webp or is_heic):
        raise ProjectProfitError("The uploaded file does not look like an image")


async def attach_document(
    db: AsyncSession,
    org_id: str,
    project_id: str,
    *,
    data: bytes,
    filename: str,
    content_type: str | None,
    kind: str = "contract",
    uploaded_by: str | None = None,
) -> ProjectDocument:
    """Store the bytes and link them to the project. Does NOT commit."""
    await _project(db, org_id, project_id)
    if kind not in DOCUMENT_KINDS:
        raise ProjectProfitError(f"Unknown document kind '{kind}'")
    if not data:
        raise ProjectProfitError("The uploaded file is empty")
    if kind == "photo":
        _require_image(data, content_type)
    sha, _size = await documents.store(
        PROJECT_DOCS, org_id, data, content_type, db=db, filename=filename, uploaded_by=uploaded_by
    )
    row = ProjectDocument(
        org_id=org_id,
        project_id=project_id,
        kind=kind,
        sha256=sha,
        filename=filename[:255],
        content_type=content_type,
        uploaded_by=uploaded_by,
    )
    db.add(row)
    return row


async def list_documents(db: AsyncSession, org_id: str, project_id: str) -> list[ProjectDocument]:
    await _project(db, org_id, project_id)
    return list(
        await db.scalars(
            select(ProjectDocument)
            .where(ProjectDocument.org_id == org_id, ProjectDocument.project_id == project_id)
            .order_by(ProjectDocument.created_at.desc(), ProjectDocument.id)
        )
    )


async def get_document(
    db: AsyncSession, org_id: str, project_id: str, document_id: str
) -> ProjectDocument:
    row = await db.scalar(
        select(ProjectDocument)
        .where(
            ProjectDocument.org_id == org_id,
            ProjectDocument.project_id == project_id,
            ProjectDocument.id == document_id,
        )
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise NotFoundError("Document not found")
    return row


async def load_document(
    db: AsyncSession,
    org_id: str,
    project_id: str,
    document_id: str,
    *,
    expected_identity: tuple[str, int] | None = None,
) -> tuple[ProjectDocument, bytes]:
    row = await get_document(db, org_id, project_id, document_id)
    sha = row.sha256
    registry_size = select(Document.size).where(
        Document.org_id == org_id, Document.kind == PROJECT_DOCS, Document.sha256 == sha
    )
    size = await db.scalar(registry_size)
    if expected_identity is not None:
        # Optional processing callers admit one exact source/size before
        # entering this loader. A fresh lookup cannot widen that read budget.
        if sha != expected_identity[0]:
            raise NotFoundError("Document not found")
        if size != expected_identity[1]:
            raise documents.OriginalIntegrityError()
    data = await documents.load_verified_original(PROJECT_DOCS, org_id, sha, size=size)
    if data is None:
        raise NotFoundError("Stored document missing")
    row = await get_document(db, org_id, project_id, document_id)
    if row.sha256 != sha:
        raise NotFoundError("Document not found")
    if await db.scalar(registry_size) != size:
        raise documents.OriginalIntegrityError()
    return row, data


# --------------------------------------------------------------------------- #
# The P&L
# --------------------------------------------------------------------------- #


def _ledger_figures(ledger: dict) -> dict:
    zero = Decimal("0")
    figures = _figures(
        ledger.get("invoiced", zero),
        ledger.get("credited", zero),
        ledger.get("invoice_costs", zero),
        ledger.get("expense_costs", zero),
        ledger.get("manual_costs", zero),
    )
    issues = ledger.get("issues", [])
    if issues:
        figures["profit"] = None
        if any(issue["kind"] == "issued" for issue in issues):
            figures["revenue"] = None
            figures["credited"] = None
        for kind, bucket in (
            ("received", "invoice_costs"),
            ("expense_report_item", "expense_costs"),
            ("manual", "manual_costs"),
        ):
            if any(issue["kind"] == kind for issue in issues):
                figures[bucket] = None
                figures["costs"] = None
    return {
        **figures,
        "basis_version": 2,
        "complete": not issues,
        "issues": issues,
        "fx_trace": ledger.get("fx_trace", []),
    }


async def _live_figures(db: AsyncSession, org_id: str, project_id: str) -> dict:
    from app.services.project_currency import project_ledgers

    rows = await project_ledgers(db, org_id, project_id=project_id)
    return _ledger_figures(rows.get(project_id, {}))


def _figures(
    invoiced: Decimal, credited: Decimal, inv_costs: Decimal, expense: Decimal, entries: Decimal
) -> dict:
    """The seven headline figures from their five inputs — ONE arithmetic for
    the single-project and the bulk path, so the two can never drift."""
    invoiced, credited = money.q2(invoiced), money.q2(credited)
    inv_costs, expense, entries = money.q2(inv_costs), money.q2(expense), money.q2(entries)
    revenue = money.q2(invoiced - credited)
    costs = money.q2(inv_costs + expense + entries)
    return {
        "revenue": str(revenue),
        "credited": str(credited),
        "costs": str(costs),
        "invoice_costs": str(inv_costs),
        "expense_costs": str(expense),
        "manual_costs": str(entries),
        "profit": str(money.q2(revenue - costs)),
    }


async def _live_figures_bulk(db: AsyncSession, org_id: str) -> dict[str, dict]:
    from app.services.project_currency import project_ledgers

    rows = await project_ledgers(db, org_id)
    return {pid: _ledger_figures(ledger) for pid, ledger in rows.items()}


def _compose(project: Project, live: dict, estimate: Decimal | None) -> dict:
    """The wire row for one project from its live figures and estimate: frozen
    figures plus labelled `adjustments` for a closed project with a snapshot,
    the live figures otherwise. Shared by `pnl` and `pnl_summary` so the list
    and the detail can never disagree on what a row means."""
    adjustments: dict[str, str] = {}
    legacy_figures: dict[str, str] = {}
    issues = list(live.get("issues", []))
    adjustments_complete = live.get("complete", False)
    if project.status in ("closed", "archived") and project.closed_pnl_json:
        try:
            frozen = _json.loads(project.closed_pnl_json)
        except (ValueError, TypeError):
            frozen = {}
        if not isinstance(frozen, dict):
            frozen = {}
        figures = frozen
        try:
            valid_values = {
                key: frozen[key]
                for key in _FROZEN_KEYS
                if isinstance(frozen.get(key), str) and Decimal(frozen[key]).is_finite()
            }
        except InvalidOperation:
            valid_values = {}
        trusted = (
            frozen.get("basis_version") == 2
            and frozen.get("complete") is True
            and len(valid_values) == len(_FROZEN_KEYS)
        )
        if not trusted:
            legacy_figures = valid_values
            figures = dict.fromkeys(_FROZEN_KEYS)
            issues.insert(
                0,
                {
                    "code": "legacy_snapshot_unverified",
                    "detail": "The stored snapshot has no verified currency basis. It is unchanged; review its source evidence before an explicit correction/reopen.",
                },
            )
            basis = "legacy_currency_unverified"
            complete = False
            adjustments_complete = False
        else:
            complete = True
            basis = "net_eur_frozen"
            if adjustments_complete:
                for key in _FROZEN_KEYS:
                    delta = money.q2(Decimal(live[key]) - Decimal(frozen[key]))
                    if delta != Decimal("0"):
                        adjustments[key] = str(delta)
    else:
        complete = live.get("complete", False)
        figures = live if complete else dict.fromkeys(_FROZEN_KEYS)
        basis = "net_eur_live" if complete else "incomplete_eur_live"
    trace = figures.get("fx_trace", live.get("fx_trace", []))
    mixed_cost_basis = any(row.get("source_basis") == "gross_fallback" for row in trace)
    if mixed_cost_basis and basis in ("net_eur_live", "net_eur_frozen"):
        basis = "eur_mixed_cost_" + ("frozen" if basis.endswith("frozen") else "live")
    revenue = Decimal(figures["revenue"]) if complete else None
    profit = Decimal(figures["profit"]) if complete else None
    margin = (
        str((profit / revenue * 100).quantize(Decimal("0.1")))
        if revenue is not None and profit is not None and revenue > Decimal("0")
        else None
    )

    return {
        "project_id": project.id,
        "code": project.code,
        "name": project.name,
        "status": project.status,
        **figures,
        "margin_pct": margin,
        # The estimate half of estimated-vs-actual — the latest ACCEPTED offer.
        "estimated_revenue": str(estimate) if estimate is not None else None,
        # Acceptance & handover (WO-D): the customer's recorded sign-off. The
        # screen renders whatever the wire says — no local acceptance logic.
        "accepted_at": project.accepted_at.isoformat() if project.accepted_at else None,
        "accepted_by": project.accepted_by,
        "acceptance_document_id": project.acceptance_document_id,
        "acceptance_note": project.acceptance_note,
        # Stated by the server so no screen can misstate it — the same rule the
        # Trash and Archive screens follow for their windows.
        "basis": basis,
        "basis_version": 2,
        "reporting_currency": "EUR",
        "complete": complete,
        "issues": issues,
        "fx_trace": trace,
        "legacy_figures": legacy_figures,
        "adjustments_complete": adjustments_complete,
        "adjustments": adjustments,
        "pnl_frozen_at": project.pnl_frozen_at.isoformat() if project.pnl_frozen_at else None,
    }


async def pnl(db: AsyncSession, org_id: str, project_id: str) -> dict:
    """The project P&L, NET EUR. LIVE while the project is open; FROZEN once it
    closes with a snapshot (phase 2): the stored figure is what every screen
    shows, and anything that arrived after the close surfaces as a labelled
    `adjustments` delta per figure — displayed drift, never silent drift. The
    wire's `basis` field is the single source of which mode the numbers are in.

    Every aggregate re-asserts org_id even though the tenant guard also applies
    — belt-and-braces on the one figure a client quotes to their bank."""
    from app.services import project_offers

    project = await _project(db, org_id, project_id)
    live = await _live_figures(db, org_id, project_id)
    estimate = await project_offers.estimated_revenue(db, org_id, project_id)
    return _compose(project, live, estimate)


# --------------------------------------------------------------------------- #
# Phase 2 — allocation: line > split > whole-invoice
# --------------------------------------------------------------------------- #


def _split_shares(remainder: Decimal, splits: list) -> dict[str, Decimal]:
    """Divide `remainder` by the split percentages. Each share is quantized;
    the rounding residue lands on the largest PERCENTAGE (deterministic
    project-id tiebreak), so the parts always sum to exactly the remainder —
    an allocation must never invent or lose a cent.

    Largest PERCENT, not largest rounded share: 33.33/33.33/33.34 of 10.00
    rounds every share to 3.33, so "largest share" degenerates to an arbitrary
    id tiebreak — the drift test caught exactly that."""
    if not splits or remainder <= Decimal("0"):
        return {}
    shares = {
        row.project_id: money.q2(remainder * Decimal(row.percent) / Decimal("100"))
        for row in splits
    }
    residue = money.q2(remainder - sum(shares.values(), Decimal("0")))
    if residue != Decimal("0"):
        largest = max(splits, key=lambda r: (Decimal(r.percent), r.project_id)).project_id
        shares[largest] = money.q2(shares[largest] + residue)
    return shares


async def set_allocation(
    db: AsyncSession,
    org_id: str,
    invoice_id: str,
    *,
    project_id: str | None,
    splits: list[tuple[str, Decimal]] | None,
    lines: dict[str, str | None] | None,
) -> dict:
    """Replace one invoice's allocation in a single call — all three levels at
    once, so the caller can never leave them contradicting each other.

    - `project_id`: the whole-invoice fallback (None clears it).
    - `splits`: (project_id, percent) rows replacing ALL existing splits; they
      must sum to exactly 100. An empty list clears the splits.
    - `lines`: {line_id: project_id | None} for the lines being (re)tagged;
      lines not named are left alone.

    Every referenced project must exist IN THIS ORG (opaque 404 otherwise) and
    every line must belong to this invoice. Does NOT commit — the route commits
    the change with its audit event."""
    inv = await db.scalar(select(Invoice).where(Invoice.org_id == org_id, Invoice.id == invoice_id))
    if inv is None:
        raise NotFoundError("Invoice not found")

    referenced = {p for p, _ in (splits or [])}
    referenced |= {p for p in (lines or {}).values() if p}
    if project_id:
        referenced.add(project_id)
    for pid in referenced:
        await _project(db, org_id, pid)

    if splits is not None:
        if splits:
            total = sum((Decimal(pct) for _, pct in splits), Decimal("0"))
            if total != Decimal("100"):
                raise ProjectProfitError(f"Split percentages must sum to exactly 100 (got {total})")
            if len({p for p, _ in splits}) != len(splits):
                raise ProjectProfitError("Each project may appear in the split only once")
            if any(Decimal(pct) <= Decimal("0") for _, pct in splits):
                raise ProjectProfitError("Split percentages must be positive")
        await db.execute(
            sa_delete(InvoiceProjectSplit).where(
                InvoiceProjectSplit.org_id == org_id,
                InvoiceProjectSplit.invoice_id == invoice_id,
            )
        )
        for pid, pct in splits or []:
            db.add(
                InvoiceProjectSplit(
                    org_id=org_id,
                    invoice_id=invoice_id,
                    project_id=pid,
                    percent=money.q2(Decimal(pct)),
                )
            )

    if lines:
        line_rows = {
            li.id: li
            for li in await db.scalars(select(LineItem).where(LineItem.invoice_id == invoice_id))
        }
        for line_id, line_pid in lines.items():
            li = line_rows.get(line_id)
            if li is None:
                raise NotFoundError("Line not found on this invoice")
            li.project_id = line_pid

    inv.project_id = project_id
    return {
        "invoice_id": invoice_id,
        "project_id": project_id,
        "splits": [(p, str(money.q2(Decimal(pct)))) for p, pct in (splits or [])],
        "lines_tagged": len(lines or {}),
    }


# --------------------------------------------------------------------------- #
# Phase 2 — the freeze at close
# --------------------------------------------------------------------------- #

_FROZEN_KEYS = (
    "revenue",
    "credited",
    "costs",
    "invoice_costs",
    "expense_costs",
    "manual_costs",
    "profit",
)


async def snapshot_close(db: AsyncSession, org_id: str, project_id: str) -> dict:
    """Compute and store the P&L at the moment of closing. Called by
    `costing.update` inside the SAME transaction as the status change, so the
    close and its frozen figure commit together or not at all. Does NOT commit."""
    figures = await _live_figures(db, org_id, project_id)
    if not figures["complete"]:
        from app.core.errors import ValidationError

        raise ValidationError(
            "Cannot close this project: EUR conversion evidence is incomplete. Review the project currency issues."
        )
    project = await _project(db, org_id, project_id)
    project.closed_pnl_json = _json.dumps(figures)
    project.pnl_frozen_at = datetime.now(UTC)
    return figures


async def clear_snapshot(db: AsyncSession, org_id: str, project_id: str) -> None:
    """Reopening discards the snapshot — a reopened project is live again by
    definition, and the caller (costing.update) audits the reopen. No commit."""
    project = await _project(db, org_id, project_id)
    project.closed_pnl_json = None
    project.pnl_frozen_at = None


async def pnl_summary(db: AsyncSession, org_id: str) -> list[dict]:
    """Every project's headline figures for the list screen — the question the
    list answers is "which contracts lose money", so profit and margin ride
    along with code/name/status. Frozen figures for closed projects with a
    snapshot, live otherwise.

    PERF-006: a fixed number of statements for the whole tenant (the projects,
    the bulk live figures, the bulk estimates) — it used to run the detail
    path once per project, which walks each allocated invoice one query at a
    time, so a tenant with a few hundred projects paid thousands of round-trips
    for one list screen. The rows are composed by the same `_compose` the
    detail route uses; a test holds the two paths equal figure for figure."""
    from app.services import project_offers

    projects = list(
        await db.scalars(select(Project).where(Project.org_id == org_id).order_by(Project.code))
    )
    if not projects:
        return []
    live = await _live_figures_bulk(db, org_id)
    estimates = await project_offers.estimated_revenue_bulk(db, org_id)
    empty = _ledger_figures({})
    return [_compose(pr, live.get(pr.id, empty), estimates.get(pr.id)) for pr in projects]


async def get_allocation(db: AsyncSession, org_id: str, invoice_id: str) -> dict:
    """One invoice's current allocation, in exactly the shape `set_allocation`
    accepts — read it, edit it, PUT it back, no translation in between."""
    inv = await db.scalar(select(Invoice).where(Invoice.org_id == org_id, Invoice.id == invoice_id))
    if inv is None:
        raise NotFoundError("Invoice not found")
    splits = list(
        await db.scalars(
            select(InvoiceProjectSplit)
            .where(
                InvoiceProjectSplit.org_id == org_id,
                InvoiceProjectSplit.invoice_id == invoice_id,
            )
            .order_by(InvoiceProjectSplit.percent.desc(), InvoiceProjectSplit.project_id)
        )
    )
    tagged = list(
        await db.execute(
            select(LineItem.id, LineItem.project_id).where(
                LineItem.invoice_id == invoice_id, LineItem.project_id.is_not(None)
            )
        )
    )
    return {
        "invoice_id": invoice_id,
        "project_id": inv.project_id,
        "splits": [{"project_id": s.project_id, "percent": str(s.percent)} for s in splits],
        "lines": {row.id: row.project_id for row in tagged},
    }


# --------------------------------------------------------------------------- #
# WO-D: acceptance & handover + the adjustable final invoice.
#
# Acceptance is a stamped EVENT on the project (not a status): the customer
# has signed that the work is done — the thing that makes the final invoice
# unarguable (§5a). The final invoice itself is ISSUED THROUGH THE NORMAL
# issuing flow; what lives here is the COMPOSER — plan remainder ± the
# owner's explicitly labelled adjustment lines (adjustable by decision:
# unexpected costs and damages either way reconcile in the open instead of
# hiding) — plus the per-org acceptance gate and the sign rule: a total at
# or below zero is a CREDIT NOTE's job, never a negative invoice.
# --------------------------------------------------------------------------- #


class GateError(ProjectProfitError):
    """The org requires acceptance before the guided final invoice."""


async def record_acceptance(
    db: AsyncSession,
    org_id: str,
    project_id: str,
    *,
    document_id: str | None,
    note: str | None,
    accepted_by: str,
) -> Project:
    project = await _project(db, org_id, project_id)
    if project.accepted_at is not None:
        raise ProjectProfitError("already accepted — revoke the existing acceptance first")
    if document_id is not None:
        doc = await db.scalar(
            select(ProjectDocument).where(
                ProjectDocument.org_id == org_id,
                ProjectDocument.project_id == project_id,
                ProjectDocument.id == document_id,
            )
        )
        if doc is None:
            raise NotFoundError("acceptance document not found on this project")
    project.accepted_at = datetime.now(UTC)
    project.accepted_by = accepted_by
    project.acceptance_document_id = document_id
    project.acceptance_note = note
    await db.flush()
    return project


async def revoke_acceptance(db: AsyncSession, org_id: str, project_id: str) -> dict:
    """Clear the stamp; returns what was cleared so the audit can carry it."""
    project = await _project(db, org_id, project_id)
    if project.accepted_at is None:
        raise ProjectProfitError("this project is not accepted")
    prior = {
        "accepted_at": project.accepted_at.isoformat(),
        "accepted_by": project.accepted_by,
        "acceptance_document_id": project.acceptance_document_id,
    }
    project.accepted_at = None
    project.accepted_by = None
    project.acceptance_document_id = None
    project.acceptance_note = None
    await db.flush()
    return prior


async def final_invoice_draft(
    db: AsyncSession,
    org_id: str,
    project_id: str,
    adjustments: list[dict] | None = None,
) -> dict:
    """Compose the final invoice's lines for the issuing form: the contracted
    remainder plus each labelled adjustment (positive or negative). Refuses a
    non-positive total (that is a credit note) and, when the org opted into
    the gate, refuses until acceptance is recorded."""
    from app.models.organization import Organization
    from app.services import project_offers

    project = await _project(db, org_id, project_id)
    org = await db.get(Organization, org_id)
    gate_required = bool(org and org.final_invoice_requires_acceptance)
    if gate_required and project.accepted_at is None:
        raise GateError("this workspace requires a recorded acceptance before the final invoice")

    tracking = await project_offers.plan_tracking(db, org_id, project_id)
    if not tracking["complete"]:
        from app.core.errors import ValidationError

        raise ValidationError(
            "Cannot issue a final invoice: the EUR remainder needs complete source/rate evidence."
        )
    remainder = Decimal(tracking["remaining"])

    lines: list[dict] = []
    if remainder > 0:
        lines.append(
            {
                "description": f"Final invoice — contracted remainder ({project.code})",
                "quantity": "1",
                "unit_price": str(money.q2(remainder)),
            }
        )
    total = remainder if remainder > 0 else Decimal("0")
    for adj in adjustments or []:
        label = str(adj.get("label", "")).strip()
        amount = money.q2(Decimal(str(adj.get("amount", "0"))))
        if not label:
            raise ProjectProfitError("every adjustment needs a label — that is the point")
        if amount == 0:
            raise ProjectProfitError(f"adjustment {label!r} is zero — remove it")
        lines.append(
            {"description": f"Adjustment — {label}", "quantity": "1", "unit_price": str(amount)}
        )
        total += amount

    total = money.q2(total)
    if total <= 0:
        raise ProjectProfitError(
            "the adjusted total is not positive — money flowing TO the customer "
            "is a credit note against the original invoice, never a negative invoice"
        )
    return {
        "project_id": project_id,
        "contracted_total": tracking["contracted_total"],
        "issued_total": tracking["issued_total"],
        "remainder": str(money.q2(remainder)),
        "lines": lines,
        "total": str(total),
        "gate_required": gate_required,
        "accepted_at": project.accepted_at.isoformat() if project.accepted_at else None,
    }
