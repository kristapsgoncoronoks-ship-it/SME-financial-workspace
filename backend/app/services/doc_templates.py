"""Dynamic document templates — the phase-5 machinery (owner direction,
2026-08-16).

THE TRUST MODEL, in one paragraph: the platform operator maintains MASTER
templates (demo texts ship now, plainly marked; the owner's lawyer's
standardized texts replace the bodies later through the same surface). A
client ADJUSTS a master into their own saved copy — as many named versions as
they like — and generates project documents from whichever version they
choose. A platform edit never reaches into a client's saved copy: an adjusted
template is the client's document, frozen the moment they saved it.

RENDERING: `{{token}}` substitution over a context built from the org's own
data (company = issuer profile, customer = the chosen customer master row,
project, latest accepted offer, invoicing plan, today's date). Unknown tokens
stay VISIBLY unreplaced — a gap a person can see beats a silently wrong
document, and a lawyer reviewing output must be able to spot what the system
did not know. Industry-neutral throughout: tokens name business objects, never
an industry.

The PDF is deliberately plain (reportlab paragraphs): these are working legal
documents, not marketing. Generated documents ATTACH TO THE PROJECT through
the same `project_documents` slot the uploaded contract uses — one place a
project's papers live, however they came to exist.
"""

from __future__ import annotations

import asyncio
import contextvars
import io
import re
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.models.customer import Customer
from app.models.document_template import (
    TEMPLATE_KINDS,
    OrgTemplate,
    PlatformTemplate,
)
from app.models.issuer import IssuerProfile
from app.services.project_profit import NotFoundError, ProjectProfitError, _project

_TOKEN = re.compile(r"\{\{\s*([a-z_.]+)\s*\}\}")
_DOCUMENT_RENDER_SLOT = threading.BoundedSemaphore(1)
_DOCUMENT_RENDER_EXECUTOR: ThreadPoolExecutor | None = None


class ProjectDocumentRenderingBusy(AppError):
    status = 503
    code = "project_document_render_busy"

    def __init__(self):
        super().__init__(
            "Project document rendering is busy. No document was saved by this attempt. "
            "Wait for the current rendering to finish, then try again."
        )


def _render_document(title: str, body: str, ctx: dict[str, str]) -> tuple[str, bytes]:
    text = render(body, ctx)
    return text, to_pdf(title, text)


async def _render_document_async(title: str, body: str, ctx: dict[str, str]) -> tuple[str, bytes]:
    """Render detached values off loop, admitting one physical job per process.

    Once submitted, even pending work finishes after request cancellation. No
    session/context object enters the worker, and the cancelled caller cannot
    proceed to attachment. Admission follows the physical future, not its
    cancelable asyncio bridge. This is neither a CPU timeout nor a hard kill.
    """
    global _DOCUMENT_RENDER_EXECUTOR
    physical: Future[tuple[str, bytes]] = Future()
    inputs = [(title, body, dict(ctx))]
    slot = _DOCUMENT_RENDER_SLOT
    if not slot.acquire(blocking=False):
        raise ProjectDocumentRenderingBusy()

    def execute() -> None:
        if not physical.set_running_or_notify_cancel():
            return
        try:
            name, source, snapshot = inputs.pop()
            result = _render_document(name, source, snapshot)
        except BaseException as exc:
            physical.set_exception(exc)
        else:
            physical.set_result(result)

    executor = _DOCUMENT_RENDER_EXECUTOR
    try:
        if executor is None:
            executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="project-document")
            _DOCUMENT_RENDER_EXECUTOR = executor
        executor.submit(contextvars.Context().run, execute)
    except BaseException:
        # submit() can enqueue and THEN fail starting its worker. Cancel our
        # proxy before any delayed entry, drop pending source values, and drain
        # the retired executor rather than retaining unowned input/tombstone jobs.
        if physical.cancel():
            inputs.clear()
        _DOCUMENT_RENDER_EXECUTOR = None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
        raise
    finally:
        # Install after submission/retirement bookkeeping: an instantly finished
        # job must not release admission while that bookkeeping is incomplete.
        physical.add_done_callback(lambda _: slot.release())
    bridge = asyncio.wrap_future(physical)
    bridge.add_done_callback(lambda done: None if done.cancelled() else done.exception())
    return await asyncio.shield(bridge)


DEMO_DISCLAIMER = (
    "DEMO TEMPLATE — an EXAMPLE, not legal advice. Review with your own "
    "adviser and adjust before using it in a real agreement."
)

# The demo masters. Industry-neutral by owner requirement; bodies are examples
# the operator replaces with the lawyer's standardized texts when they land.
DEMO_TEMPLATES: list[dict] = [
    {
        "key": "demo-contract",
        "kind": "contract",
        "name": "Service contract (demo)",
        "description": "A simple two-party service contract skeleton.",
        "body": f"""{DEMO_DISCLAIMER}

SERVICE CONTRACT {{{{project.code}}}}

Date: {{{{date}}}}

Between {{{{company.legal_name}}}} (reg. no. {{{{company.reg_number}}}}, "the Contractor")
and {{{{customer.name}}}} (VAT no. {{{{customer.vat_number}}}}, "the Customer").

1. SUBJECT. The Contractor performs the work described in project
   {{{{project.code}}}} — {{{{project.name}}}} — under the terms below.

2. PRICE AND PAYMENT. The agreed total is {{{{offer.total}}}} EUR per accepted
   offer {{{{offer.number}}}}. Invoicing follows the agreed schedule
   (contracted total {{{{plan.contracted_total}}}} EUR); each invoice is payable
   within the term stated on it.

3. CHANGES. Additional work, unexpected costs and damages are agreed in
   writing and settled with the final invoice.

4. ACCEPTANCE. Completion is confirmed by a signed acceptance document;
   the final invoice follows acceptance.

Signed:

________________________            ________________________
{{{{company.legal_name}}}}            {{{{customer.name}}}}
""",
    },
    {
        "key": "demo-acceptance",
        "kind": "acceptance",
        "name": "Acceptance & handover act (demo)",
        "description": "Confirms the work was delivered and accepted.",
        "body": f"""{DEMO_DISCLAIMER}

ACCEPTANCE AND HANDOVER ACT

Project: {{{{project.code}}}} — {{{{project.name}}}}
Date: {{{{date}}}}

{{{{company.legal_name}}}} ("the Contractor") hands over, and
{{{{customer.name}}}} ("the Customer") accepts, the work performed under
project {{{{project.code}}}}.

The Customer confirms the work has been completed and accepted. Claims made
after signing are handled per the contract. The final invoice may follow this
act, adjusted for any additional costs or damages agreed by the parties.

Signed:

________________________            ________________________
{{{{company.legal_name}}}}            {{{{customer.name}}}}
""",
    },
    {
        "key": "demo-offer-cover",
        "kind": "offer",
        "name": "Offer cover letter (demo)",
        "description": "A short cover page for a priced offer.",
        "body": f"""{DEMO_DISCLAIMER}

OFFER {{{{offer.number}}}}

Date: {{{{date}}}}

{{{{customer.name}}}},

{{{{company.legal_name}}}} offers to perform the work of project
{{{{project.code}}}} — {{{{project.name}}}} — for a total of
{{{{offer.total}}}} EUR.

This offer is valid for 30 days. Acceptance in writing forms the basis of the
contract and the invoicing schedule.

Kind regards,
{{{{company.legal_name}}}}
""",
    },
]


async def ensure_demos(db: AsyncSession) -> int:
    """Idempotently seed the demo masters. Keyed by slug: a demo is inserted
    only if its key is absent, so an operator's later EDIT of a master is never
    overwritten by a restart. Returns how many were inserted. Does NOT commit."""
    inserted = 0
    for demo in DEMO_TEMPLATES:
        exists = await db.scalar(
            select(PlatformTemplate.id).where(PlatformTemplate.key == demo["key"])
        )
        if exists is None:
            db.add(PlatformTemplate(**demo))
            inserted += 1
    return inserted


# --------------------------------------------------------------------------- #
# Platform (operator) surface
# --------------------------------------------------------------------------- #


async def platform_list(
    db: AsyncSession, *, include_inactive: bool = False
) -> list[PlatformTemplate]:
    stmt = select(PlatformTemplate).order_by(PlatformTemplate.kind, PlatformTemplate.name)
    if not include_inactive:
        stmt = stmt.where(PlatformTemplate.active.is_(True))
    return list(await db.scalars(stmt))


async def platform_upsert(
    db: AsyncSession,
    *,
    key: str,
    kind: str,
    name: str,
    body: str,
    description: str | None = None,
    active: bool = True,
) -> PlatformTemplate:
    """Create or update ONE master by its stable key — the surface the lawyer's
    standardized texts arrive through. Does NOT commit."""
    if kind not in TEMPLATE_KINDS:
        raise ProjectProfitError(f"Unknown template kind '{kind}'")
    if not body.strip():
        raise ProjectProfitError("A template needs a body")
    row = await db.scalar(select(PlatformTemplate).where(PlatformTemplate.key == key))
    if row is None:
        row = PlatformTemplate(key=key, kind=kind, name=name, body=body)
        db.add(row)
    row.kind = kind
    row.name = name
    row.body = body
    row.description = description
    row.active = active
    return row


# --------------------------------------------------------------------------- #
# Tenant surface — adjust, save versions, choose
# --------------------------------------------------------------------------- #


async def org_list(db: AsyncSession, org_id: str) -> dict:
    """Everything this org can generate from: the active platform masters plus
    their own saved versions. Seeds the demo masters on first read (committed —
    the same seed-on-first-read precedent as the plan-policy matrix), so every
    deployment has documents to start from without a manual step."""
    if await ensure_demos(db):
        await db.commit()
    masters = await platform_list(db)
    own = list(
        await db.scalars(
            select(OrgTemplate)
            .where(OrgTemplate.org_id == org_id)
            .order_by(OrgTemplate.kind, OrgTemplate.name)
        )
    )
    return {"platform": masters, "own": own}


async def org_create(
    db: AsyncSession,
    org_id: str,
    *,
    name: str,
    kind: str | None = None,
    body: str | None = None,
    source_platform_id: str | None = None,
    created_by: str | None = None,
) -> OrgTemplate:
    """Save an adjusted version. Start from a platform master (its body is the
    starting text, its kind is inherited) or from scratch (kind + body given).
    Multiple saved versions per kind are the point — the client chooses per
    document."""
    if source_platform_id:
        master = await db.get(PlatformTemplate, source_platform_id)
        if master is None or not master.active:
            raise NotFoundError("Template not found")
        kind = kind or master.kind
        body = body if body is not None else master.body
    if not kind or kind not in TEMPLATE_KINDS:
        raise ProjectProfitError(f"Unknown template kind '{kind}'")
    if not body or not body.strip():
        raise ProjectProfitError("A template needs a body")
    row = OrgTemplate(
        org_id=org_id,
        source_platform_id=source_platform_id,
        kind=kind,
        name=name.strip()[:200],
        body=body,
        created_by=created_by,
    )
    if not row.name:
        raise ProjectProfitError("A template needs a name")
    db.add(row)
    return row


async def _own(db: AsyncSession, org_id: str, template_id: str) -> OrgTemplate:
    row = await db.scalar(
        select(OrgTemplate).where(OrgTemplate.org_id == org_id, OrgTemplate.id == template_id)
    )
    if row is None:
        raise NotFoundError("Template not found")
    return row


async def org_update(
    db: AsyncSession,
    org_id: str,
    template_id: str,
    *,
    name: str | None = None,
    body: str | None = None,
    active: bool | None = None,
) -> OrgTemplate:
    row = await _own(db, org_id, template_id)
    if name is not None:
        if not name.strip():
            raise ProjectProfitError("A template needs a name")
        row.name = name.strip()[:200]
    if body is not None:
        if not body.strip():
            raise ProjectProfitError("A template needs a body")
        row.body = body
    if active is not None:
        row.active = active
    return row


async def org_delete(db: AsyncSession, org_id: str, template_id: str) -> OrgTemplate:
    """Returns the deleted row so the route audits WHAT was removed."""
    row = await _own(db, org_id, template_id)
    await db.delete(row)
    return row


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


async def build_context(
    db: AsyncSession, org_id: str, project_id: str, *, customer_id: str | None = None
) -> dict[str, str]:
    """The substitution context, from the org's own data. Missing pieces simply
    produce no key — the token then stays visible in the output, which is the
    designed failure mode."""
    from app.services import project_offers

    project = await _project(db, org_id, project_id)
    ctx: dict[str, str] = {
        "date": datetime.now(UTC).date().isoformat(),
        "project.code": project.code,
        "project.name": project.name,
    }
    if project.start_date:
        ctx["project.start_date"] = project.start_date.isoformat()
    if project.end_date:
        ctx["project.end_date"] = project.end_date.isoformat()

    issuer = await db.scalar(
        select(IssuerProfile)
        .where(IssuerProfile.org_id == org_id)
        .order_by(IssuerProfile.is_default.desc())
    )
    if issuer:
        for token, value in (
            ("company.name", issuer.name or issuer.legal_name),
            ("company.legal_name", issuer.legal_name),
            ("company.reg_number", issuer.registration_number),
            ("company.vat_number", issuer.vat_number),
            ("company.address", issuer.address_line1),
            ("company.city", issuer.city),
        ):
            if value:
                ctx[token] = str(value)

    if customer_id:
        customer = await db.scalar(
            select(Customer).where(Customer.org_id == org_id, Customer.id == customer_id)
        )
        if customer is None:
            raise NotFoundError("Customer not found")
        for token, value in (
            ("customer.name", customer.legal_name or customer.name),
            ("customer.vat_number", customer.vat_number),
            ("customer.address", customer.address_line1),
            ("customer.city", customer.city),
        ):
            if value:
                ctx[token] = str(value)

    estimate = await project_offers.estimated_revenue(db, org_id, project_id)
    if estimate is not None:
        ctx["offer.total"] = str(estimate)
    accepted_number = await db.scalar(
        select(project_offers.ProjectOffer.number)
        .where(
            project_offers.ProjectOffer.org_id == org_id,
            project_offers.ProjectOffer.project_id == project_id,
            project_offers.ProjectOffer.status == "accepted",
        )
        .order_by(project_offers.ProjectOffer.version.desc())
        .limit(1)
    )
    if accepted_number:
        ctx["offer.number"] = accepted_number

    tracking = await project_offers.plan_tracking(db, org_id, project_id)
    if tracking["rows"]:
        if not tracking["complete"]:
            raise ProjectProfitError(
                "Cannot generate a project document: the invoicing remainder needs EUR source/rate evidence."
            )
        ctx["plan.contracted_total"] = tracking["contracted_total"]
        ctx["plan.remaining"] = tracking["remaining"]

    return ctx


def render(body: str, ctx: dict[str, str]) -> str:
    """Replace known tokens; leave unknown ones VISIBLY in place."""

    def _sub(match: re.Match) -> str:
        return ctx.get(match.group(1), match.group(0))

    return _TOKEN.sub(_sub, body)


def to_pdf(title: str, text: str) -> bytes:
    """A deliberately plain paragraph PDF — a working document, not marketing."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        title=title,
        leftMargin=22 * mm,
        rightMargin=22 * mm,
        topMargin=20 * mm,
        bottomMargin=20 * mm,
    )
    style = ParagraphStyle("body", fontName="Helvetica", fontSize=10.5, leading=15)
    flow = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        safe = block.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        flow.append(Paragraph(safe.replace("\n", "<br/>"), style))
        flow.append(Spacer(1, 6))
    doc.build(flow)
    return buf.getvalue()


async def generate_project_document(
    db: AsyncSession,
    org_id: str,
    project_id: str,
    *,
    template_id: str,
    template_scope: str,
    customer_id: str | None = None,
    uploaded_by: str | None = None,
):
    """Render the chosen template against the project and ATTACH the PDF as a
    project document — the same slot the uploaded contract uses, so all of a
    project's papers live in one place however they came to exist.

    `template_scope` says which table the id names ('own' or 'platform') —
    explicit, because guessing across two namespaces invites an id collision
    resolving to the wrong document. Does NOT commit."""
    from app.services import project_profit

    if template_scope == "own":
        tpl = await _own(db, org_id, template_id)
        if not tpl.active:
            raise ProjectProfitError("This saved template is deactivated")
        kind, name, body = tpl.kind, tpl.name, tpl.body
    elif template_scope == "platform":
        master = await db.get(PlatformTemplate, template_id)
        if master is None or not master.active:
            raise NotFoundError("Template not found")
        kind, name, body = master.kind, master.name, master.body
    else:
        raise ProjectProfitError("template_scope must be 'own' or 'platform'")

    ctx = await build_context(db, org_id, project_id, customer_id=customer_id)
    text, pdf = await _render_document_async(name, body, ctx)
    filename = f"{ctx.get('project.code', 'project')}-{kind}.pdf"
    row = await project_profit.attach_document(
        db,
        org_id,
        project_id,
        data=pdf,
        filename=filename,
        content_type="application/pdf",
        kind=kind if kind in ("contract", "acceptance") else "other",
        uploaded_by=uploaded_by,
    )
    return row, text
