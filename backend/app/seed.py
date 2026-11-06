"""Seed a demo tenant with realistic invoices so the dashboard has data.

Run: `python -m app.seed`  (idempotent — clears the demo org first).
Login: demo@invoiceiq.app / demo1234
"""

from __future__ import annotations

import asyncio
import random
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import delete, select

from app.core.database import SessionLocal, engine
from app.core.development_schema import development_metadata
from app.core.security import hash_password
from app.models import Base
from app.models.invoice import Invoice, InvoiceStatus, LineItem
from app.models.organization import Organization
from app.models.user import User, UserRole
from app.models.vendor import Vendor
from app.services import memberships

DEMO_EMAIL = "demo@invoiceiq.app"

# Extra tenants (name, plan, status) so the platform operator view isn't lonely.
EXTRA_TENANTS = [
    ("Baltic Haulage OÜ", "starter", "active"),
    ("Nordic Freight AB", "pro", "active"),
    ("Adria Logistik d.o.o.", "trial", "suspended"),
]
# Every organization name this seed owns — cleared on reset so re-seeding is idempotent.
DEMO_ORG_NAMES = ["Demo Logistics Ltd", *(t[0] for t in EXTRA_TENANTS)]

VENDORS = [
    ("AWS", "US", "cloud"),
    ("Shell Fleet", "NL", "fuel"),
    ("Staples", "US", "office"),
    ("Lufthansa", "DE", "travel"),
    ("Slack", "US", "software"),
    ("DHL", "DE", "logistics"),
    ("WeWork", "GB", "rent"),
]
CATEGORIES = ["cloud", "fuel", "office", "travel", "software", "logistics", "rent", "consulting"]
_CENTS = Decimal("0.01")


def _q(v: Decimal) -> Decimal:
    return v.quantize(_CENTS, rounding=ROUND_HALF_UP)


# Codes that MATCH the cost-allocation master seeded in `_seed_costing`, so the
# Slice-2 backfill resolves the demo invoices' free-text tags to real links.
_DEMO_CC_CODES = ("CC-1000", "CC-1100", "CC-2000", "CC-3000")
_DEMO_DEPT_CODES = ("OPS", "FIN", "SALES")


async def seed() -> None:
    rng = random.Random(42)  # deterministic demo data
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda connection: development_metadata(Base.metadata, connection.dialect).create_all(
                connection
            )
        )

    async with SessionLocal() as db:
        # Reset every org this seed owns. Deleting the org cascades to its rows
        # (FK ON); the explicit user delete also clears any orphan left by an
        # older run that predates FK enforcement, so re-seeding is idempotent.
        await db.execute(delete(Organization).where(Organization.name.in_(DEMO_ORG_NAMES)))
        await db.execute(delete(User).where(User.email == DEMO_EMAIL))
        await db.commit()

        org = Organization(name="Demo Logistics Ltd")
        db.add(org)
        await db.flush()

        owner = User(
            org_id=org.id,
            email=DEMO_EMAIL,
            name="Demo Owner",
            hashed_password=hash_password("demo1234"),
            role=UserRole.owner,
            is_platform_admin=True,  # so the demo shows the operator view
            is_expense_approver=True,  # the owner approves expenses by default
        )
        db.add(owner)
        org.plan = "pro"

        # A few extra tenants so the platform operator view isn't lonely.
        extra_owners: list[tuple[str, User]] = []
        for i, (tname, plan, tstatus) in enumerate(EXTRA_TENANTS):
            t = Organization(name=tname, plan=plan, status=tstatus)
            db.add(t)
            await db.flush()
            extra = User(
                org_id=t.id,
                email=f"owner{i}@{tname.split()[0].lower()}.test",
                name="Owner",
                hashed_password=hash_password("demo1234"),
                role=UserRole.owner,
                is_expense_approver=True,
            )
            db.add(extra)
            extra_owners.append((t.id, extra))

        # Membership dual-write (Slice 6b/6d): get_current_user requires a LIVE
        # membership in the caller's active org, so a seeded user WITHOUT one can
        # authenticate but is then rejected on every request ("logged in, thrown
        # straight out"). Mirror what register() does for real signups.
        await db.flush()
        await memberships.ensure(
            db,
            org_id=org.id,
            user_id=owner.id,
            role=UserRole.owner,
            is_expense_approver=True,
            email=owner.email,
            name=owner.name,
        )
        for tid, u in extra_owners:
            await memberships.ensure(
                db,
                org_id=tid,
                user_id=u.id,
                role=UserRole.owner,
                is_expense_approver=True,
                email=u.email,
                name=u.name,
            )

        vendors = []
        for name, country, cat in VENDORS:
            v = Vendor(org_id=org.id, name=name, country=country, category=cat)
            db.add(v)
            vendors.append(v)
        await db.flush()

        # AP invoices (both the domestic loop below and the FX loop further down)
        # are collected here so they can be driven through the REAL review →
        # approval → payment-run workflow after they're flushed (R3): the legacy
        # `status` set below is not what cash-position/payment-runs/dashboard
        # read — they read `workflow_state`, which must never diverge from it.
        ap_invoices: list[Invoice] = []

        today = date(2026, 7, 1)
        count = 0
        for month_back in range(12):
            month_start = today - timedelta(days=30 * month_back)
            for _ in range(rng.randint(4, 9)):
                vendor = rng.choice(vendors)
                n_lines = rng.randint(1, 4)
                subtotal = Decimal("0")
                tax_total = Decimal("0")
                items = []
                for _ in range(n_lines):
                    qty = Decimal(rng.randint(1, 20))
                    # Stable per-category base price ± small jitter, not pure
                    # noise: random unit prices made the Supplier-costs page
                    # scream fake ±80% "price changes" (the impossible-time-
                    # series anti-pattern from the demo-data research).
                    base = {
                        "cloud": 240,
                        "fuel": 62,
                        "office": 35,
                        "travel": 410,
                        "software": 180,
                        "logistics": 95,
                        "rent": 800,
                    }.get(vendor.category or "", 120)
                    unit = _q(Decimal(base) * Decimal(str(rng.uniform(0.95, 1.06))))
                    amount = _q(qty * unit)
                    rate = Decimal(rng.choice([0, 9, 21]))
                    tax = _q(amount * rate / Decimal("100"))
                    subtotal += amount
                    tax_total += tax
                    items.append(
                        LineItem(
                            description=f"{(vendor.category or '').title()} service",
                            category=rng.choice(CATEGORIES)
                            if rng.random() < 0.3
                            else vendor.category,
                            quantity=qty,
                            unit_price=unit,
                            amount=amount,
                            tax_rate=rate,
                        )
                    )
                issue = month_start - timedelta(days=rng.randint(0, 20))
                status = rng.choices(
                    [InvoiceStatus.paid, InvoiceStatus.pending, InvoiceStatus.overdue],
                    weights=[6, 3, 1],
                )[0]
                count += 1
                ap_inv = Invoice(
                    org_id=org.id,
                    vendor_id=vendor.id,
                    invoice_number=f"INV-{2026}-{count:04d}",
                    issue_date=issue,
                    due_date=issue + timedelta(days=30),
                    currency="EUR",
                    status=status,
                    subtotal=_q(subtotal),
                    tax_amount=_q(tax_total),
                    total=_q(subtotal + tax_total),
                    # Free-text cost-allocation tags (the master-table links are
                    # resolved from these by the Slice-2 backfill below).
                    cost_center=_DEMO_CC_CODES[count % len(_DEMO_CC_CODES)],
                    department=_DEMO_DEPT_CODES[count % len(_DEMO_DEPT_CODES)],
                    line_items=items,
                )
                db.add(ap_inv)
                ap_invoices.append(ap_inv)
        # --- FX: seed ECB rates + a few foreign-currency invoices ---
        from app.services import fx

        await fx.ensure_seed_rates(db, today)

        vendor_by_name = {v.name: v for v in vendors}
        foreign = [
            ("AWS", "USD", Decimal("12500.00"), date(2026, 5, 15)),
            ("Slack", "USD", Decimal("4200.00"), date(2026, 4, 10)),
            ("AWS", "USD", Decimal("9800.00"), date(2026, 6, 5)),
            ("WeWork", "GBP", Decimal("8600.00"), date(2026, 3, 20)),
            ("WeWork", "GBP", Decimal("7300.00"), date(2026, 6, 12)),
            ("Lufthansa", "CHF", Decimal("5400.00"), date(2026, 5, 2)),
        ]
        for vname, ccy, amount, issue in foreign:
            fx_vendor = vendor_by_name.get(vname)
            if fx_vendor is None:
                continue
            resolved = await fx.resolve_rate(db, ccy, issue)
            if resolved is None:
                continue
            # Stated rate ~2.5% WORSE than ECB (a lower rate ⇒ fewer foreign units
            # per EUR ⇒ more EUR paid) — the classic bank FX markup to recover.
            stated = (resolved.rate * Decimal("0.975")).quantize(
                Decimal("0.000001"), rounding=ROUND_HALF_UP
            )
            total_eur = _q(amount / stated)
            count += 1
            fx_inv = Invoice(
                org_id=org.id,
                vendor_id=fx_vendor.id,
                invoice_number=f"INV-{2026}-{count:04d}",
                issue_date=issue,
                due_date=issue + timedelta(days=30),
                currency=ccy,
                status=InvoiceStatus.paid,
                subtotal=amount,
                tax_amount=Decimal("0"),
                total=amount,
                total_eur=total_eur,
                fx_rate=stated,
                fx_source="stated",
                line_items=[
                    LineItem(
                        description=f"{(fx_vendor.category or '').title()} service ({ccy})",
                        category=fx_vendor.category,
                        quantity=Decimal("1"),
                        unit_price=amount,
                        amount=amount,
                        tax_rate=Decimal("0"),
                    )
                ],
            )
            db.add(fx_inv)
            ap_invoices.append(fx_inv)

        # --- Issued invoices (accounts-receivable) so the issuing reports have data ---
        issued = await _seed_issued(db, org.id, rng)
        # --- Partners with pre-invoicing workflow + penalty invoicing ---
        partner_ct = await _seed_partners(db, org.id)
        # --- Cost-allocation master data (Slice 1) ---
        costing_ct = await _seed_costing(db, org.id)
        # --- Slice 2: resolve the invoices' free-text tags to master links ---
        from app.services import costing, currencies, payments, tax_codes

        await db.flush()  # SessionLocal has autoflush off; make invoices visible
        linked = await costing.backfill_invoice_links(db, org.id)
        # --- Slice 3c: seed the AR payment ledger from the settled invoices ---
        ledger = await payments.backfill_ledger(db, org.id)
        # --- Slice 4a / 5a: seed the tenant's reference catalogues ---
        tax_ct = await tax_codes.seed_standard(db, org.id)
        cur_ct = await currencies.seed_standard(db, org.id)
        # --- R3: drive every AP invoice through the REAL review/approval/
        # payment-run workflow so `workflow_state` (what cash-position/payment
        # -runs/dashboard read) never diverges from the legacy `status` (what
        # the Invoices list badge reads) — see docs/plan/plan-a/wo/WO-28-R3.md.
        # Planted price history (Supplier-costs movers) joins the AP pool
        # BEFORE the workflow drive — every AP invoice rides the real rails.
        price_invoices, price_points = await _seed_price_history(db, org.id)
        ap_invoices.extend(price_invoices)
        ap_workflow = await _drive_ap_workflow(db, org.id, owner, ap_invoices, rng)
        # --- The lifecycle storyline: customers → offers → schedule →
        # acceptance → portal.
        story = await _seed_lifecycle_demo(db, org.id, owner)
        story["price_points"] = price_points

        await db.commit()
        print(f"Seeded '{org.name}' with {count} invoices across {len(vendors)} vendors.")
        print(f"Issued {issued} outbound invoices (paid/overdue/open mix).")
        print(f"Seeded {ledger} AR payment-ledger entries (Slice 3c).")
        print(f"Seeded {tax_ct} tax codes (Slice 4a), {cur_ct} currencies (Slice 5a).")
        print(f"Created {partner_ct} partners (workflow + penalty demo).")
        print(
            f"Created {costing_ct} cost-allocation master records (departments/cost-centers/projects)."
        )
        print(
            f"Linked invoices to master data (Slice 2 backfill): "
            f"{linked['cost_center']} cost-center, {linked['department']} department."
        )
        print(
            f"Drove {len(ap_invoices)} AP invoices through the real review->approval "
            f"workflow across {ap_workflow['_payment_runs']} payment run(s) "
            f"(workflow_state mix: {ap_workflow})."
        )
        print(
            f"Lifecycle storyline: {story['customers']} customers, {story['offers']} offers, "
            f"{story['assignments']} schedule assignments, {story['price_points']} price-history "
            f"purchases; acceptance recorded on the hero project."
        )
        print(f"Client portal (hero customer): /portal/{story['portal_token']}")
        print(f"Login: {DEMO_EMAIL} / demo1234")


async def _seed_price_history(db, org_id: str) -> tuple[list[Invoice], int]:
    """Supplier price history for the Supplier-costs movers: the same two
    items bought month after month — one drifting up ~12%, one easing down.
    Returned invoices join the AP pool so they ride the real workflow."""
    supplier = Vendor(org_id=org_id, name="Būvmateriālu bāze SIA", country="LV", category="office")
    db.add(supplier)
    await db.flush()
    today = date.today()
    invoices: list[Invoice] = []
    price_points = 0
    riser = [Decimal(p) for p in ("3.20", "3.20", "3.35", "3.40", "3.55", "3.60")]
    faller = [Decimal(p) for p in ("12.80", "12.80", "12.60", "12.40", "12.40", "12.10")]
    for m in range(6):
        when = today - timedelta(days=30 * (5 - m) + 3)
        lines = [
            ("Packing film roll", Decimal("40"), riser[m]),
            ("Protective floor board", Decimal("15"), faller[m]),
        ]
        subtotal = _q(sum((q * p for _, q, p in lines), Decimal("0")))
        inv = Invoice(
            org_id=org_id,
            vendor_id=supplier.id,
            invoice_number=f"BMB-{when.year}-{m + 1:03d}",
            issue_date=when,
            due_date=when + timedelta(days=14),
            currency="EUR",
            status=InvoiceStatus.paid,
            subtotal=subtotal,
            tax_amount=Decimal("0"),
            total=subtotal,
        )
        db.add(inv)
        await db.flush()
        for desc, qty, price in lines:
            db.add(
                LineItem(
                    invoice_id=inv.id,
                    description=desc,
                    category="office",
                    quantity=qty,
                    unit_price=price,
                    amount=_q(qty * price),
                    tax_rate=Decimal("0"),
                )
            )
            price_points += 1
        invoices.append(inv)
    await db.flush()
    return invoices, price_points


async def _seed_lifecycle_demo(db, org_id: str, owner) -> dict:
    """The storyline workspace: five customers with legible arcs, so every
    screen shipped in WO-A…WO-I has something true to show.

    Research-backed rules (docs/design/demo-dataset.md): all identities are
    SYNTHETIC-SAFE — emails only at IANA-held example.com, IBANs mod-97-valid
    on the fictitious `BANK` code, 9-prefixed LV VAT numbers outside the real
    register's ranges, +371 5-series phones (unassignable). All dates are
    RELATIVE to today so the demo never stales, imperfect states are planted
    on purpose (a stale offer, an overdue invoice, a dormant customer), and
    everything flows through the SERVICE layer where invariants and audit
    trails live — the demo workspace is built the way a real one is used.
    """
    from datetime import UTC, datetime

    from app.models.customer import Customer
    from app.models.issued_invoice import IssuedInvoice, IssuedInvoiceLine
    from app.models.next_action import OrgDeadline
    from app.services import crm, portal, project_offers, project_profit, scheduling

    today = date.today()
    now = datetime.now(UTC)

    def _cust(name: str, email: str | None, lifecycle: str, phone_n: int) -> Customer:
        c = Customer(
            org_id=org_id,
            name=name,
            email=email,
            phone=f"+371 5000 000{phone_n}",
            vat_number=f"LV9{phone_n}00000000{phone_n}",
            country="LV",
            lifecycle=lifecycle,
        )
        db.add(c)
        return c

    hero = _cust('SIA "Upmala" birojs', "reception@example.com", "active", 1)
    weekly = _cust('SIA "Ziedu darbnīca"', "darbnica@example.com", "active", 2)
    prospect = _cust('SIA "Jaunais projekts"', "info@example.net", "prospect", 3)
    dormant = _cust('SIA "Klusais nams"', "nams@example.org", "dormant", 4)
    lost = _cust('SIA "Cits ceļš"', None, "lost", 5)
    await db.flush()

    await crm.add_note(
        db, org_id, hero.id, body="Prefers morning calls; gate code 4711.", created_by=owner.email
    )
    await crm.add_note(
        db,
        org_id,
        prospect.id,
        body="Met at the trade fair — wants the quote revised down if possible.",
        created_by=owner.email,
    )
    await crm.add_note(
        db,
        org_id,
        lost.id,
        body="Chose a cheaper competitor; keep for next season.",
        created_by=owner.email,
    )

    # Projects, linked to their customers (the WO-E link every later module uses).
    from app.models.costing import Project

    def _project(code: str, name: str, customer: Customer) -> Project:
        p = Project(org_id=org_id, code=code, name=name, status="active", customer_id=customer.id)
        db.add(p)
        return p

    hero_prj = _project("PRJ-UPMALA", "Office refurbishment, phase 1", hero)
    weekly_prj = _project("PRJ-ZIEDU", "Workshop fit-out", weekly)
    prospect_prj = _project("PRJ-JAUNAIS", "Site survey & estimate", prospect)
    lost_prj = _project("PRJ-CITS", "Annual maintenance bid", lost)
    await db.flush()

    offers = 0

    async def _offer(project, lines: list[dict], *, days_ago: int):
        nonlocal offers
        o = await project_offers.create_offer(
            db, org_id, project.id, title=None, lines=lines, created_by=owner.email
        )
        offers += 1
        o.created_at = now - timedelta(days=days_ago)
        return o

    # HERO ARC: offer sent, viewed in the portal, accepted → plan seeded.
    hero_offer = await _offer(
        hero_prj,
        [
            {"description": "Demolition and preparation", "amount": "1800.00"},
            {"description": "Materials and fit-out", "amount": "5200.00"},
            {"description": "Finishing and handover", "amount": "2400.00"},
        ],
        days_ago=45,
    )
    await project_offers.transition_offer(db, org_id, hero_offer.id, "sent", actor=owner.email)
    hero_offer.viewed_at = now - timedelta(days=43)  # the portal's quote-viewed stamp
    await project_offers.transition_offer(
        db, org_id, hero_offer.id, "accepted", actor="customer (portal)"
    )

    # PROSPECT: sent 20 days ago, silent — the Pipeline's red "chase" card.
    stale = await _offer(
        prospect_prj, [{"description": "Survey and estimate", "amount": "450.00"}], days_ago=20
    )
    await project_offers.transition_offer(db, org_id, stale.id, "sent", actor=owner.email)
    from app.models.crm import OfferStageEvent

    await db.flush()
    for ev in await db.scalars(select(OfferStageEvent).where(OfferStageEvent.offer_id == stale.id)):
        ev.created_at = now - timedelta(days=20)

    # WEEKLY: a fresh draft still being shaped.
    await _offer(
        weekly_prj,
        [{"description": "Workshop shelving and counters", "amount": "3100.00"}],
        days_ago=2,
    )

    # LOST: sent and declined, so Won/Lost columns both carry history.
    lost_offer = await _offer(
        lost_prj, [{"description": "Annual maintenance", "amount": "6200.00"}], days_ago=60
    )
    await project_offers.transition_offer(db, org_id, lost_offer.id, "sent", actor=owner.email)
    await project_offers.transition_offer(db, org_id, lost_offer.id, "rejected", actor=owner.email)

    # Schedule: the hero's work is DONE (feeds the acceptance nudge); the
    # weekly customer has work THIS WEEK (confirmed tomorrow, planned in 3 days
    # — the latter arms a 48h arrival notice once the org opt-in below is on).
    from app.models.organization import Organization

    org_row = await db.get(Organization, org_id)
    org_row.client_notice_hours = 48

    assignments = 0

    async def _assign(project: Project, start: datetime, hours: int, status: str, note: str | None):
        nonlocal assignments
        row, _warn = await scheduling.create(
            db,
            org_id,
            project_id=project.id,
            assignee_user_id=owner.id,
            starts_at=start,
            ends_at=start + timedelta(hours=hours),
            all_day=False,
            note=note,
            created_by=owner.email,
        )
        assignments += 1
        if status != "planned":
            for step in ("confirmed", "done") if status == "done" else (status,):
                await scheduling.transition(
                    db,
                    org_id,
                    row.id,
                    step,
                    actor_user_id=owner.id,
                    actor_may_plan=True,
                )
        return row

    day9 = now.replace(hour=9, minute=0, second=0, microsecond=0)
    done1 = await _assign(hero_prj, day9 - timedelta(days=30), 8, "done", "Demolition crew day")
    done1.starts_at = day9 - timedelta(days=30)  # keep window honest after transition
    await _assign(hero_prj, day9 - timedelta(days=12), 8, "done", "Fit-out and finishing")
    await _assign(weekly_prj, day9 + timedelta(days=1), 6, "confirmed", "Bring the signed contract")
    await _assign(weekly_prj, day9 + timedelta(days=3), 8, "planned", None)

    # The hero's contract: generated bytes, shared into the portal; a site
    # photo rides the same rail (WO-F).
    contract = await project_profit.attach_document(
        db,
        org_id,
        hero_prj.id,
        data=b"%PDF-1.7 demo contract for PRJ-UPMALA" + b" " * 64,
        filename="contract-signed.pdf",
        content_type="application/pdf",
        kind="contract",
        uploaded_by=owner.email,
    )
    contract.shared_with_customer = True
    await project_profit.attach_document(
        db,
        org_id,
        hero_prj.id,
        data=b"\xff\xd8\xff\xe0" + bytes(64),
        filename="site-before.jpg",
        content_type="image/jpeg",
        kind="photo",
        uploaded_by=owner.email,
    )

    # Acceptance recorded on the hero project (WO-D), evidence attached.
    await project_profit.record_acceptance(
        db,
        org_id,
        hero_prj.id,
        document_id=contract.id,
        note="Walk-through completed; snag list empty.",
        accepted_by=owner.email,
    )

    # Two issued invoices carry the hero's revenue: one PAID instalment, one
    # OPEN and 10 days overdue — the imperfection every real book has.
    import json as _json

    from app.services import issuer as issuer_svc

    profile = await issuer_svc.get_or_create(db, org_id)
    seller = _json.dumps(issuer_svc.seller_snapshot(profile))

    def _issued_for(
        customer, project_id: str | None, number: str, total: str, *, days_ago: int, overdue: bool
    ) -> None:
        net = Decimal(total)
        vat = _q(net * Decimal("0.21"))
        inv = IssuedInvoice(
            org_id=org_id,
            customer_id=customer.id,
            project_id=project_id,
            number=number,
            buyer_name=customer.name,
            buyer_vat_number=customer.vat_number,
            currency="EUR",
            vat_scheme="standard",
            seller_json=seller,
            subtotal=net,
            tax_total=vat,
            total=net + vat,
            amount_paid=Decimal("0") if overdue else net + vat,
            # `paid` is DERIVED from amount_paid (issued_status); the stored
            # lifecycle of a settled invoice is `issued` (DB-011 CHECK).
            lifecycle="issued",
            issue_date=today - timedelta(days=days_ago),
            due_date=today - timedelta(days=days_ago - 20),
            issued_at=now - timedelta(days=days_ago),
        )
        inv.lines = [
            IssuedInvoiceLine(
                description="Contracted instalment",
                quantity=Decimal("1"),
                unit_price=net,
                vat_rate=Decimal("21"),
                net_amount=net,
            )
        ]
        db.add(inv)

    _issued_for(hero, hero_prj.id, "DEMO-UP-001", "4700.00", days_ago=35, overdue=False)
    _issued_for(hero, hero_prj.id, "DEMO-UP-002", "2350.00", days_ago=30, overdue=True)

    # A recurring obligation for Next actions ("prepare the VAT report").
    db.add(
        OrgDeadline(
            org_id=org_id,
            name="Prepare the VAT report",
            cadence="monthly",
            due_day=15,
            lead_days=7,
            created_by=owner.email,
        )
    )

    # The hero's portal link — printed at the end of seeding.
    link = await portal.get_or_create_link(db, org_id, hero.id, created_by=owner.email)

    # The dormant customer's history: one old paid invoice, eight months back.
    _issued_for(dormant, None, "DEMO-KN-001", "640.00", days_ago=240, overdue=False)

    await db.flush()
    return {
        "customers": 5,
        "offers": offers,
        "assignments": assignments,
        "portal_token": link.token,
    }


async def _seed_costing(db, org_id: str) -> int:
    """A small, realistic cost-allocation master set for the demo tenant:
    departments, cost centers rolled up to them, and a couple of projects."""
    from app.models.costing import CostCenter, Department, Project

    deps = {
        "OPS": "Operations",
        "FIN": "Finance",
        "SALES": "Sales & Marketing",
    }
    dep_ids: dict[str, str] = {}
    for code, name in deps.items():
        d = Department(org_id=org_id, code=code, name=name)
        db.add(d)
        await db.flush()
        dep_ids[code] = d.id

    cost_centers = [
        ("CC-1000", "Fleet & Logistics", "OPS"),
        ("CC-1100", "Warehouse", "OPS"),
        ("CC-2000", "Accounting", "FIN"),
        ("CC-3000", "Field Sales", "SALES"),
    ]
    for code, name, dep in cost_centers:
        db.add(CostCenter(org_id=org_id, code=code, name=name, department_id=dep_ids[dep]))

    projects = [
        ("PRJ-BALTIC", "Baltic Expansion 2026", "active"),
        ("PRJ-ERP", "ERP Migration", "active"),
        ("PRJ-2025Q4", "Q4-2025 Cost Review", "closed"),
    ]
    for code, name, status in projects:
        db.add(Project(org_id=org_id, code=code, name=name, status=status))

    await db.flush()
    return len(deps) + len(cost_centers) + len(projects)


async def _seed_issued(db, org_id: str, rng: random.Random) -> int:
    """Create a spread of ISSUED invoices — several partners, a paid/overdue/open
    mix — plus a complete issuer profile and the enabled issuing module, so the
    invoice-reports surface is populated in the demo."""
    import json

    from app.models.issued_invoice import IssuedInvoice, IssuedInvoiceLine
    from app.services import issuer as issuer_svc
    from app.services import modules as modules_svc
    from app.services import vat as vat_svc

    profile = await issuer_svc.get_or_create(db, org_id)
    profile.legal_name = "Demo Logistics Ltd"
    profile.vat_number = "EE100200300"
    profile.registration_number = "EE-16000000"
    profile.address_line1 = "Tartu mnt 10"
    profile.city = "Tallinn"
    profile.postal_code = "10145"
    profile.country = "EE"
    profile.email = "billing@demologistics.test"
    profile.iban = "EE471000001020145685"
    profile.bic = "EEUHEE2X"
    profile.payment_terms_days = 30
    profile.default_penalty_rate = Decimal("8")  # 8% p.a. default late-payment interest
    await db.flush()
    await modules_svc.set_enabled(db, org_id, "issuing", True)

    partners = [
        ("Meridian Freight OÜ", "EE100111222", "EE", "ap@meridianfreight.test"),
        ("Baltic Cold Chain AS", "EE100333444", "EE", "invoices@balticcold.test"),
        ("Vilnius Logistics UAB", "LT100555666", "LT", "finance@vilniuslog.test"),
        ("Riga Port Services SIA", "LV40103777888", "LV", "ap@rigaport.test"),
        ("Helsinki Haul Oy", "FI29999999", "FI", "laskut@helsinkihaul.test"),
    ]
    today = date(2026, 7, 1)
    schemes = ["standard", "standard", "standard", "reverse_charge", "intra_eu"]
    count = 0
    for m in range(6):  # ~6 months of history
        month_start = today - timedelta(days=30 * m)
        for _ in range(rng.randint(2, 4)):
            name, vat_no, country, buyer_email = rng.choice(partners)
            scheme = rng.choice(schemes)
            n_lines = rng.randint(1, 3)
            raw = [
                {
                    "description": rng.choice(
                        [
                            "Freight forwarding",
                            "Warehousing",
                            "Customs brokerage",
                            "Last-mile delivery",
                        ]
                    ),
                    "quantity": Decimal(rng.randint(1, 12)),
                    "unit": "C62",
                    "unit_price": _q(Decimal(rng.uniform(80, 1200))),
                    "vat_rate": Decimal("22") if scheme == "standard" else Decimal("0"),
                }
                for _ in range(n_lines)
            ]
            result = vat_svc.compute(raw, scheme)

            issue = month_start - timedelta(days=rng.randint(0, 18))
            due = issue + timedelta(days=30)
            number = f"{profile.invoice_prefix}{issue.year}-{profile.next_number:04d}"
            profile.next_number += 1

            # Settlement: older invoices mostly paid; recent ones open; some overdue.
            amount_paid = Decimal("0")
            paid_date = None
            roll = rng.random()
            if due < today and roll < 0.7:  # paid
                amount_paid = result.total
                paid_date = due - timedelta(days=rng.randint(-5, 20))
            elif roll < 0.85 and result.total > 0:  # partial
                amount_paid = _q(result.total * Decimal("0.4"))

            db.add(
                IssuedInvoice(
                    org_id=org_id,
                    number=number,
                    issue_date=issue,
                    due_date=due,
                    currency="EUR",
                    buyer_name=name,
                    buyer_email=buyer_email,
                    buyer_vat_number=vat_no,
                    buyer_country=country,
                    penalty_rate=profile.default_penalty_rate,  # inherit the 8% p.a. default
                    seller_json=json.dumps(issuer_svc.seller_snapshot(profile)),
                    vat_scheme=scheme,
                    note=vat_svc.SCHEME_NOTES.get(scheme),
                    subtotal=result.subtotal,
                    tax_total=result.tax_total,
                    total=result.total,
                    amount_paid=amount_paid,
                    paid_date=paid_date,
                    lines=[
                        IssuedInvoiceLine(
                            position=i + 1,
                            description=li["description"],
                            quantity=li["quantity"],
                            unit=li["unit"],
                            unit_price=li["unit_price"],
                            vat_rate=li["vat_rate"],
                            net_amount=li["net_amount"],
                        )
                        for i, li in enumerate(result.lines)
                    ],
                )
            )
            count += 1
    return count


async def _seed_partners(db, org_id: str) -> int:
    """Two partners demonstrating the pre-invoicing workflow + penalty invoicing:
    one fully signed with overdue invoices (penalty-ready), one awaiting signature."""
    import json
    from datetime import date, timedelta

    from app.models.issued_invoice import IssuedInvoice, IssuedInvoiceLine
    from app.models.partner import Partner, PartnerDocument
    from app.services import issuer as issuer_svc

    profile = await issuer_svc.get_or_create(db, org_id)

    # Partner A — contract + acceptance both signed → ready; penalty enabled.
    ready = Partner(
        org_id=org_id,
        name="Northwind Traders GmbH",
        email="ap@northwind.test",
        vat_number="DE311111111",
        country="DE",
        requires_contract=True,
        requires_acceptance=True,
        penalty_enabled=True,
        penalty_rate=Decimal("12"),
    )
    db.add(ready)
    await db.flush()
    db.add(
        PartnerDocument(
            org_id=org_id,
            partner_id=ready.id,
            kind="contract",
            title="Framework agreement 2026",
            status="signed",
            signed_by="J. Schmidt",
            signed_date=date(2026, 1, 15),
        )
    )
    db.add(
        PartnerDocument(
            org_id=org_id,
            partner_id=ready.id,
            kind="acceptance_act",
            title="Acceptance act — Q1 delivery",
            status="signed",
            signed_by="J. Schmidt",
            signed_date=date(2026, 2, 1),
        )
    )

    # Two overdue invoices linked to Partner A so a penalty invoice can be generated.
    for i, amount in enumerate([Decimal("4200.00"), Decimal("2600.00")], start=1):
        profile.next_number += 1
        number = f"{profile.invoice_prefix}2026-P{profile.next_number:03d}"
        issue = date(2026, 1, 10 + i)
        db.add(
            IssuedInvoice(
                org_id=org_id,
                partner_id=ready.id,
                kind="standard",
                number=number,
                issue_date=issue,
                due_date=issue + timedelta(days=30),
                currency="EUR",
                buyer_name=ready.name,
                buyer_email=ready.email,
                buyer_vat_number=ready.vat_number,
                buyer_country=ready.country,
                penalty_rate=Decimal("12"),
                seller_json=json.dumps(issuer_svc.seller_snapshot(profile)),
                vat_scheme="standard",
                subtotal=amount,
                tax_total=Decimal("0"),
                total=amount,
                lines=[
                    IssuedInvoiceLine(
                        position=1,
                        description="Freight forwarding",
                        quantity=Decimal("1"),
                        unit="C62",
                        unit_price=amount,
                        vat_rate=Decimal("0"),
                        net_amount=amount,
                    )
                ],
            )
        )

    # Partner B — documents added but UNSIGNED → invoicing blocked (shows the gate).
    pending = Partner(
        org_id=org_id,
        name="Contoso Baltic SIA",
        email="finance@contoso.test",
        vat_number="LV40199999999",
        country="LV",
        requires_contract=True,
        requires_acceptance=True,
        penalty_enabled=False,
    )
    db.add(pending)
    await db.flush()
    db.add(
        PartnerDocument(
            org_id=org_id,
            partner_id=pending.id,
            kind="contract",
            title="Framework agreement (draft)",
            status="draft",
        )
    )
    return 2


async def _drive_ap_workflow(
    db, org_id: str, owner: User, invoices: list[Invoice], rng: random.Random
) -> dict[str, int]:
    """Advance every seeded AP invoice through the REAL review -> approval ->
    payment-run endpoint FUNCTIONS (not a hand-set enum), so `workflow_state` —
    what cash-position/payment-runs/the dashboard actually read — never diverges
    from the legacy aging `status` the Invoices list badge reads (R3,
    docs/plan/plan-a/wo/WO-28-R3.md). These are plain `async def`s: FastAPI's
    Depends/Annotated machinery only activates through the real HTTP dispatcher,
    so calling them directly with `current=current, db=db` runs the exact same
    body (audit records, webhook emit, best-effort mailer.send) with zero
    network I/O, since the fresh demo org has no webhook endpoints and
    `settings.smtp_enabled` defaults False.

    The demo owner (`is_platform_admin=True`) is the ONLY user in the demo org,
    so the payment-run maker<>checker control (WO-9) is satisfied via the SAME
    explicit, audited `override_sod=True` path a real single-admin org would
    use — never a silent bypass; every override is recorded to the audit log
    exactly as it would be for a live customer."""
    from app.api.routes import invoice_review
    from app.api.routes import payment_runs as payment_run_routes
    from app.core.identity import principal
    from app.schemas.approval import DecisionIn, SubmitIn, TransitionIn
    from app.schemas.payment_run import RunApprove, RunCreate, RunPay

    membership = await memberships.get(db, owner.org_id, owner.id)
    assert membership is not None
    current = principal(owner, membership)
    org_row = await db.get(Organization, owner.org_id)
    assert org_row is not None
    to_pay: list[Invoice] = []
    for inv in invoices:
        review = await invoice_review.submit(
            inv.id, SubmitIn(version=inv.version), current=current, db=db, org=org_row
        )
        review = await invoice_review.approve(
            inv.id, DecisionIn(version=review.version), current=current, db=db
        )
        if inv.status == InvoiceStatus.paid:
            # Full cycle: schedule now, settle via a real payment run below.
            await invoice_review.transition(
                inv.id,
                TransitionIn(version=review.version, target="scheduled_for_payment"),
                current=current,
                db=db,
            )
            to_pay.append(inv)
        elif inv.status == InvoiceStatus.pending and rng.random() < 0.35:
            # Some pending invoices are already queued for payment (not yet
            # paid) so the payment-run candidate pool isn't empty either —
            # legacy `status` stays 'pending', which is not a contradiction:
            # "scheduled to be paid soon" is still "not yet paid".
            await invoice_review.transition(
                inv.id,
                TransitionIn(version=review.version, target="scheduled_for_payment"),
                current=current,
                db=db,
            )
        # The rest of 'pending' and every 'overdue' invoice stays 'approved' —
        # an open payable; ap_status.status_of derives OVERDUE from the
        # (already-past) due_date at read time, never from a stored flag.

    # Settle every 'paid'-status invoice through a REAL payment run (creates the
    # AP ledger entries + run history, exactly like a customer's data) — batched
    # so the demo's Payment Runs screen shows several runs, not one giant one.
    n_runs = 0
    for start in range(0, len(to_pay), 7):
        batch = to_pay[start : start + 7]
        run = await payment_run_routes.create_run(
            RunCreate(invoice_ids=[i.id for i in batch], method="bank_transfer"),
            current=current,
            db=db,
        )
        n_runs += 1
        run = await payment_run_routes.approve_run(
            run.id, RunApprove(version=run.version, override_sod=True), current=current, db=db
        )
        await payment_run_routes.pay_run(
            run.id,
            RunPay(version=run.version, reference=f"SEED-RUN-{n_runs:02d}", override_sod=True),
            current=current,
            db=db,
        )

    counts: dict[str, int] = {}
    for inv in invoices:
        counts[inv.workflow_state.value] = counts.get(inv.workflow_state.value, 0) + 1
    counts["_payment_runs"] = n_runs
    return counts


if __name__ == "__main__":
    asyncio.run(seed())
