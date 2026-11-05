"""Cash-position dashboard (Phase 15).

A single read-only roll-up of the whole cash cycle: accounts RECEIVABLE (issued
invoices — outstanding / overdue / aging, reusing the canonical receivables
report), accounts PAYABLE (received supplier invoices — outstanding / overdue /
scheduled / queued in a run, via the AP status logic), and BANK RECONCILIATION
(how many imported lines are still unmatched). NET position = receivables −
payables. Aggregates existing data; writes nothing. Tenant-scoped.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import money
from app.models.bank_import import BankLine
from app.models.invoice import Invoice, WorkflowState
from app.services import issued_reports

_ZERO = Decimal("0")
# Supplier-invoice workflow states that represent an open payable (approved to pay,
# not yet fully settled). Draft/submitted/rejected/cancelled are not yet payable;
# paid/archived are done.
_PAYABLE_STATES = (
    WorkflowState.approved,
    WorkflowState.scheduled_for_payment,
    WorkflowState.partially_paid,
)


async def _ar_summary(db: AsyncSession, org_id: str, today: date) -> dict:
    rep = await issued_reports.receivables_scalars(db, org_id, today=today)
    assert rep.aging is not None  # asked for with the bands (the roll-up publishes them)
    return {
        "currency": rep.currency,
        "outstanding": money.q2(rep.total_outstanding),
        "overdue": money.q2(rep.overdue_outstanding),
        "avg_days_to_pay": rep.avg_days_to_pay,
        "aging": [
            {"label": b.label, "count": b.count, "outstanding": money.q2(b.outstanding)}
            for b in rep.aging
        ],
    }


async def _ap_row(db: AsyncSession, org_id: str, today: date, currency: str):
    """The five payable aggregates in ONE statement: outstanding and overdue in
    the report currency, the count across every currency, how many are
    scheduled, how many sit in a run. Split out of `_ap_summary` (PERF-018) so
    the narrow dashboard read and the full roll-up share ONE definition of the
    arithmetic — the extra aggregates ride the same scan and cost the narrow
    caller nothing, so there is no second, drift-prone copy of the sum."""
    from app.services.ap_aging import outstanding_expr, overdue_condition

    outstanding = outstanding_expr()
    in_currency = Invoice.currency == currency
    return (
        await db.execute(
            select(
                func.coalesce(func.sum(case((in_currency, outstanding), else_=0)), 0),
                func.coalesce(
                    func.sum(case((and_(in_currency, overdue_condition(today)), outstanding), else_=0)),
                    0,
                ),
                func.count(Invoice.id),
                func.sum(
                    case(
                        (Invoice.workflow_state == WorkflowState.scheduled_for_payment, 1), else_=0
                    )
                ),
                func.sum(case((Invoice.payment_run_id.is_not(None), 1), else_=0)),
            ).where(Invoice.org_id == org_id, Invoice.workflow_state.in_(_PAYABLE_STATES))
        )
    ).one()


async def _ap_summary(db: AsyncSession, org_id: str, today: date, currency: str) -> dict:
    """Open payables, aggregated in the database (PERF-002).

    This used to hydrate every open payable and reduce it in Python for five
    scalars; the perf harness measured it flat only because it seeded no payable
    state (PERF-004) — with real rows it grew 5.5× across 4× of data. The same
    rule per row (`ap_status.outstanding_of` / `status_of == OVERDUE`) is now
    one grouped query.

    BEHAVIOUR CHANGE, deliberate and stated: the amounts are now summed in the
    REPORT CURRENCY only. The Python version summed every payable's outstanding
    regardless of its currency and the roll-up labelled the figure with the AR
    report's currency — PLN + SEK + EUR presented as one EUR number, the exact
    defect WO-8 removed from every other summary (§4.14: no cross-currency sums
    without a recorded conversion). The counts still span every currency (a
    count is not a money sum), and the currencies left out are surfaced in
    `other_currencies`, never silently dropped."""
    row = await _ap_row(db, org_id, today, currency)
    others = await db.scalars(
        select(Invoice.currency)
        .where(
            Invoice.org_id == org_id,
            Invoice.workflow_state.in_(_PAYABLE_STATES),
            Invoice.currency != currency,
        )
        .distinct()
    )
    return {
        "outstanding": money.q2(Decimal(row[0] or 0)),
        "overdue": money.q2(Decimal(row[1] or 0)),
        "count": int(row[2] or 0),
        "scheduled": int(row[3] or 0),
        "in_run": int(row[4] or 0),
        "other_currencies": sorted(others),
    }


async def _recon_summary(db: AsyncSession, org_id: str) -> dict:
    counts = {"unmatched": 0, "matched": 0, "ignored": 0}
    rows = await db.execute(
        select(BankLine.status, func.count(BankLine.id))
        .where(BankLine.org_id == org_id)
        .group_by(BankLine.status)
    )
    for status, n in rows:
        if status in counts:
            counts[status] = int(n)
    unmatched_amount = await db.scalar(
        select(func.coalesce(func.sum(func.abs(BankLine.amount)), 0)).where(
            BankLine.org_id == org_id, BankLine.status == "unmatched"
        )
    )
    return {
        **counts,
        "unmatched_amount": money.q2(Decimal(unmatched_amount or 0)),
    }


async def net_figures(
    db: AsyncSession,
    org_id: str,
    today: date | None = None,
    *,
    receivables: issued_reports.ReceivablesScalars | None = None,
) -> dict:
    """The four figures the composed dashboard's cash card renders — currency,
    receivables outstanding, payables outstanding, net — and NOTHING else.

    PERF-018 (2026-09-09). The dashboard used to call `summary()` and throw
    away five sixths of it: the AR aging bands, the AP count / scheduled / in-run
    / other-currency list, and the whole bank-reconciliation roll-up, which is
    two more statements against `bank_lines`. Worse, `summary()` reads the
    canonical receivables report itself, so a workspace with the issuing module
    on paid for that report TWICE per dashboard request — once here, once for
    the receivables card. `receivables=` lets the caller hand in the scalars it
    already read; the arithmetic is unchanged and still this module's
    (`net = AR outstanding − AP outstanding`, both in the AR report's currency,
    the rule `summary()` has followed since PERF-002)."""
    today = today or date.today()
    rep = receivables or await issued_reports.receivables_scalars(
        db, org_id, today=today, with_aging=False
    )
    ar_outstanding = money.q2(rep.total_outstanding)
    ap_outstanding = money.q2(Decimal((await _ap_row(db, org_id, today, rep.currency))[0] or 0))
    return {
        "currency": rep.currency,
        "receivables_outstanding": ar_outstanding,
        "payables_outstanding": ap_outstanding,
        "net_position": money.q2(ar_outstanding - ap_outstanding),
    }


async def summary(db: AsyncSession, org_id: str, today: date | None = None) -> dict:
    """The full cash-position roll-up (AR + AP + reconciliation + net)."""
    today = today or date.today()
    ar = await _ar_summary(db, org_id, today)
    ap = await _ap_summary(db, org_id, today, ar["currency"])
    recon = await _recon_summary(db, org_id)
    net = money.q2(Decimal(ar["outstanding"]) - Decimal(ap["outstanding"]))
    return {
        "currency": ar["currency"],
        "receivables": ar,
        "payables": ap,
        "reconciliation": recon,
        "net_position": net,
    }
