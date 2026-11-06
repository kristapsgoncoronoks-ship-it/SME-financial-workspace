"""Bank reconciliation (Phase 12).

Persist an imported bank statement into `bank_statements` + `bank_lines`, then
MATCH each line to a cash movement already on our books: an incoming CREDIT to a
`receipt` or a direct issued-invoice payment (money received), an outgoing DEBIT to
a paid `reimbursement_batch` or supplier `payment_run` (money paid). Matching is
ADVISORY — `suggest_matches`
ranks candidates by amount (within a small tolerance), date proximity, and a
reference/description hit; a human confirms. Nothing here mutates a receipt, a
batch, or the payment ledger — it only records which bank line corresponds to
which movement, so the statement and the books agree line by line. Tenant-scoped.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import money
from app.models.bank_import import BankLine, BankStatement
from app.models.expense import ReimbursementBatch
from app.models.payment import Payment
from app.models.payment_run import PaymentRun
from app.models.receipt import Receipt
from app.services.bank_statement import StatementResult

_TOL = Decimal("0.02")  # cents of slack on an amount match
_ZERO = Decimal("0")
KINDS = ("receipt", "reimbursement", "payment_run", "issued_payment")


class ReconError(Exception):
    """Business-rule violation (duplicate import, bad match target, tolerance)."""


@dataclass
class Candidate:
    kind: str
    id: str
    amount: Decimal
    date: date
    reference: str | None
    days_off: int
    score: int
    # Current cash targets use the EUR ledger; this is not the bank's currency.
    currency: str = "EUR"


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


async def _already_imported(db: AsyncSession, org_id: str, sha: str) -> bool:
    """The fast-path duplicate check (the unique index is the real guard)."""
    dup = await db.scalar(
        select(BankStatement.id).where(BankStatement.org_id == org_id, BankStatement.sha256 == sha)
    )
    return dup is not None


async def import_statement(
    db: AsyncSession,
    org_id: str,
    *,
    filename: str,
    sha: str,
    result: StatementResult,
    created_by: str | None = None,
) -> BankStatement:
    """Persist a parsed statement + its lines. Refuses a duplicate upload (same
    bytes) and an empty statement. Commits."""
    if not result.transactions:
        raise ReconError("no transactions recognised in the statement")
    if await _already_imported(db, org_id, sha):
        raise ReconError("this statement was already imported")
    lower = filename.lower()
    source_format = (
        "csv" if lower.endswith(".csv") else "camt" if result.method == "camt.053" else "pdf"
    )
    stmt = BankStatement(
        org_id=org_id,
        filename=filename[:300],
        source_format=source_format,
        method=result.method,
        sha256=sha,
        line_count=len(result.transactions),
        created_by=created_by,
    )
    db.add(stmt)
    try:
        await db.flush()  # assign stmt.id for the composite FK
    except IntegrityError:
        # DB-004: the SELECT above is the fast path; the unique index
        # uq_bank_statements_org_sha is the guarantee. Two uploads of the same
        # bytes racing past the SELECT used to BOTH import — the second now
        # fails here and is answered exactly like the sequential duplicate.
        # This function owns its transaction ("Commits."), so a rollback here
        # discards nothing of a caller's.
        await db.rollback()
        raise ReconError("this statement was already imported") from None
    for t in result.transactions:
        signed = money.q2(t.amount if t.direction == "credit" else -t.amount)
        db.add(
            BankLine(
                org_id=org_id,
                statement_id=stmt.id,
                line_date=t.date,
                description=(t.description or "")[:300],
                amount=signed,
                direction=t.direction,
                currency=(t.currency or None),
                balance=money.q2(t.balance) if t.balance is not None else None,
                status="unmatched",
            )
        )
    await db.commit()
    await db.refresh(stmt)
    return stmt


async def list_statements(db: AsyncSession, org_id: str) -> list[BankStatement]:
    return list(
        await db.scalars(
            select(BankStatement)
            .where(BankStatement.org_id == org_id)
            .order_by(BankStatement.created_at.desc())
        )
    )


async def get_statement(db: AsyncSession, org_id: str, statement_id: str) -> BankStatement | None:
    return await db.scalar(
        select(BankStatement).where(
            BankStatement.org_id == org_id, BankStatement.id == statement_id
        )
    )


async def lines_for(db: AsyncSession, org_id: str, statement_id: str) -> list[BankLine]:
    return list(
        await db.scalars(
            select(BankLine)
            .where(BankLine.org_id == org_id, BankLine.statement_id == statement_id)
            .order_by(BankLine.line_date.asc(), BankLine.created_at.asc())
        )
    )


async def get_line(db: AsyncSession, org_id: str, line_id: str) -> BankLine | None:
    return await db.scalar(
        select(BankLine).where(BankLine.org_id == org_id, BankLine.id == line_id)
    )


async def statement_counts(db: AsyncSession, org_id: str, statement_id: str) -> dict[str, int]:
    counts = {"unmatched": 0, "matched": 0, "ignored": 0}
    rows = await db.execute(
        select(BankLine.status, func.count(BankLine.id))
        .where(BankLine.org_id == org_id, BankLine.statement_id == statement_id)
        .group_by(BankLine.status)
    )
    for status, n in rows:
        counts[status] = int(n)
    return counts


async def _matched_targets(db: AsyncSession, org_id: str, kind: str) -> set[str]:
    rows = await db.scalars(
        select(BankLine.matched_id).where(
            BankLine.org_id == org_id,
            BankLine.matched_kind == kind,
            BankLine.status == "matched",
            BankLine.matched_id.is_not(None),
        )
    )
    return {r for r in rows if r}


def _rank(days_off: int, ref_hit: bool) -> int:
    """Higher is better: near-date wins, an exact reference/description hit boosts."""
    return (100 - min(days_off, 100)) + (25 if ref_hit else 0)


# Every reconcilable target is denominated in the organisation's base currency:
# receipts and direct payments carry no currency of their own, and reimbursement
# batches and payment runs are stored as `total_eur` by name. A bank line the
# bank stated in some OTHER currency is therefore not comparable to any of them —
# only its magnitude would match, which is exactly how a USD credit settles a EUR
# invoice of the same number.
_BASE_CURRENCY = "EUR"


def foreign_currency_of(line: BankLine) -> str | None:
    """The line's currency when it is stated AND is not the base — else None.

    A NULL currency is not treated as foreign. The source said nothing, and
    guessing that silence means "foreign" would block every CSV and PDF
    statement ever imported.
    """
    ccy = (getattr(line, "currency", None) or "").strip().upper()
    return ccy if ccy and ccy != _BASE_CURRENCY else None


async def suggest_matches(
    db: AsyncSession, org_id: str, line: BankLine, limit: int = 8
) -> list[Candidate]:
    """Rank the cash movements that plausibly correspond to this bank line. A credit
    (money in) is matched to receipts; a debit (money out) to paid reimbursement
    payouts. Candidates already reconciled to another line are excluded (1:1).

    A line in a currency other than the base has NO candidates — not a ranked
    list the user might accept anyway. Suggesting one would mean offering a
    500.00 USD credit as settlement for a 500.00 EUR receivable.
    """
    if foreign_currency_of(line) is not None:
        return []
    magnitude = money.q2(abs(Decimal(line.amount)))
    desc = (line.description or "").lower()
    out: list[Candidate] = []

    # PERF-001 (audit 2026-09-05): the amount window is the ONE selective
    # predicate here, and it used to be applied in Python after loading the
    # tenant's entire receipts / payments / batches / runs tables. Every
    # amount column is Numeric(14, 2), so `abs(q2(amount) - magnitude) <= TOL`
    # is exactly `amount BETWEEN magnitude - TOL AND magnitude + TOL` — the
    # database returns the handful of rows that can match, and the date and
    # reference ranking below runs over those alone. The exclusion of targets
    # already reconciled stays a set: it is bounded by matched lines, not by
    # the cash tables.
    low, high = magnitude - _TOL, magnitude + _TOL

    if Decimal(line.amount) >= _ZERO:  # credit → receipts + direct issued payments
        taken_r = await _matched_targets(db, org_id, "receipt")
        for r in await db.scalars(
            select(Receipt).where(Receipt.org_id == org_id, Receipt.amount.between(low, high))
        ):
            if r.id in taken_r or abs(money.q2(Decimal(r.amount)) - magnitude) > _TOL:
                continue
            ref_hit = bool(r.reference and r.reference.lower() in desc)
            days_off = abs((r.received_on - line.line_date).days)
            out.append(
                Candidate(
                    "receipt",
                    r.id,
                    money.q2(Decimal(r.amount)),
                    r.received_on,
                    r.reference,
                    days_off,
                    _rank(days_off, ref_hit),
                )
            )
        # Direct issued-invoice payments (PATCH /issued/payment) — a Payment row with
        # no receipt (positive = money in). A receipt-backed allocation is excluded.
        taken_p = await _matched_targets(db, org_id, "issued_payment")
        for p in await db.scalars(
            select(Payment).where(
                Payment.org_id == org_id,
                Payment.receipt_id.is_(None),
                Payment.amount > _ZERO,
                Payment.amount.between(low, high),
            )
        ):
            if p.id in taken_p or abs(money.q2(Decimal(p.amount)) - magnitude) > _TOL:
                continue
            ref_hit = bool(p.reference and p.reference.lower() in desc)
            days_off = abs((p.paid_on - line.line_date).days)
            out.append(
                Candidate(
                    "issued_payment",
                    p.id,
                    money.q2(Decimal(p.amount)),
                    p.paid_on,
                    p.reference,
                    days_off,
                    _rank(days_off, ref_hit),
                )
            )
    else:  # debit → paid reimbursement payouts + paid supplier payment runs
        taken_r = await _matched_targets(db, org_id, "reimbursement")
        for b in await db.scalars(
            select(ReimbursementBatch).where(
                ReimbursementBatch.org_id == org_id,
                ReimbursementBatch.status == "paid",
                ReimbursementBatch.total_eur.between(low, high),
            )
        ):
            if b.id in taken_r or abs(money.q2(Decimal(b.total_eur)) - magnitude) > _TOL:
                continue
            ref_hit = bool(b.reference and b.reference.lower() in desc)
            when = b.paid_at.date() if b.paid_at else line.line_date
            days_off = abs((when - line.line_date).days)
            out.append(
                Candidate(
                    "reimbursement",
                    b.id,
                    money.q2(Decimal(b.total_eur)),
                    when,
                    b.reference,
                    days_off,
                    _rank(days_off, ref_hit),
                )
            )
        taken_p = await _matched_targets(db, org_id, "payment_run")
        for run in await db.scalars(
            select(PaymentRun).where(
                PaymentRun.org_id == org_id,
                PaymentRun.status == "paid",
                PaymentRun.total_eur.between(low, high),
            )
        ):
            if run.id in taken_p or abs(money.q2(Decimal(run.total_eur)) - magnitude) > _TOL:
                continue
            ref_hit = bool(run.reference and run.reference.lower() in desc)
            when = run.paid_at.date() if run.paid_at else line.line_date
            days_off = abs((when - line.line_date).days)
            out.append(
                Candidate(
                    "payment_run",
                    run.id,
                    money.q2(Decimal(run.total_eur)),
                    when,
                    run.reference,
                    days_off,
                    _rank(days_off, ref_hit),
                )
            )

    out.sort(key=lambda c: c.score, reverse=True)
    return out[:limit]


async def confirm_match(
    db: AsyncSession, org_id: str, line: BankLine, kind: str, target_id: str
) -> BankLine:
    """Reconcile a bank line to a receipt (credit) or a paid reimbursement payout
    (debit). Validates direction, that the target exists in this org, that the
    amounts agree within tolerance, and that the target is not already reconciled to
    another line (1:1). Commits.

    Lock and refresh the current bank line before inspecting it, then lock the
    target. This serializes competing actions on one line as well as confirms of
    one target onto different lines. A repeated exact association still validates
    the target but does not write the line again. No client-version check is implied.
    """
    line = await _locked_line(db, org_id, line)
    if kind not in KINDS:
        raise ReconError("unknown match kind")
    if line.status == "ignored":
        raise ReconError("restore the ignored line before matching it")
    same_target = line.matched_id == target_id
    if line.status == "matched" and not same_target:
        # PostgreSQL accepts UUID spellings such as uppercase but reads their
        # canonical identity back; target lookup below still validates the input.
        try:
            same_target = UUID(line.matched_id or "") == UUID(target_id)
        except ValueError:
            pass
    replay = line.status == "matched" and line.matched_kind == kind and same_target
    if line.status != "unmatched" and not replay:
        raise ReconError("unmatch the line before matching it to another item")
    # Checked here as well as in `suggest_matches`, because confirm takes a
    # caller-supplied target id and is reachable without ever asking for a
    # suggestion. A gate that only filters the suggestion list is decoration.
    foreign = foreign_currency_of(line)
    if foreign is not None:
        raise ReconError(
            f"this line is in {foreign} and cannot be reconciled against "
            f"{_BASE_CURRENCY} records; convert or record it separately"
        )
    signed = Decimal(line.amount)
    magnitude = money.q2(abs(signed))

    if kind == "receipt":
        if signed < _ZERO:
            raise ReconError("a debit line cannot match a receipt (money received)")
        target = await db.scalar(
            select(Receipt)
            .where(Receipt.org_id == org_id, Receipt.id == target_id)
            .with_for_update()
        )
        if target is None:
            raise ReconError("receipt not found")
        tgt_amount = money.q2(Decimal(target.amount))
    elif kind == "issued_payment":
        if signed < _ZERO:
            raise ReconError("a debit line cannot match an issued-invoice payment (money in)")
        target = await db.scalar(
            select(Payment)
            .where(
                Payment.org_id == org_id,
                Payment.id == target_id,
                Payment.receipt_id.is_(None),
            )
            .with_for_update()
        )
        if target is None:
            raise ReconError("issued-invoice payment not found")
        tgt_amount = money.q2(abs(Decimal(target.amount)))
    elif kind == "reimbursement":
        if signed > _ZERO:
            raise ReconError("a credit line cannot match a reimbursement payout (money paid)")
        target = await db.scalar(
            select(ReimbursementBatch)
            .where(ReimbursementBatch.org_id == org_id, ReimbursementBatch.id == target_id)
            .with_for_update()
        )
        if target is None:
            raise ReconError("reimbursement batch not found")
        if target.status != "paid":
            raise ReconError("only a paid payout batch can be reconciled")
        tgt_amount = money.q2(Decimal(target.total_eur))
    else:  # payment_run
        if signed > _ZERO:
            raise ReconError("a credit line cannot match a supplier payment run (money paid)")
        target = await db.scalar(
            select(PaymentRun)
            .where(PaymentRun.org_id == org_id, PaymentRun.id == target_id)
            .with_for_update()
        )
        if target is None:
            raise ReconError("payment run not found")
        if target.status != "paid":
            raise ReconError("only a paid payment run can be reconciled")
        tgt_amount = money.q2(Decimal(target.total_eur))

    if abs(tgt_amount - magnitude) > _TOL:
        raise ReconError(f"amount mismatch: bank line {magnitude} vs target {tgt_amount}")

    other = await db.scalar(
        select(func.count(BankLine.id)).where(
            BankLine.org_id == org_id,
            BankLine.matched_kind == kind,
            BankLine.matched_id == target_id,
            BankLine.status == "matched",
            BankLine.id != line.id,
        )
    )
    if other:
        raise ReconError("that item is already reconciled to another bank line")

    if replay:
        await db.commit()
        return line
    line.status = "matched"
    line.matched_kind = kind
    line.matched_id = target_id
    await db.commit()
    return line


async def _locked_line(db: AsyncSession, org_id: str, line: BankLine) -> BankLine:
    current = await db.scalar(
        select(BankLine)
        .where(BankLine.org_id == org_id, BankLine.id == line.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if current is None:
        raise ReconError("bank line not found")
    return current


async def unmatch(db: AsyncSession, org_id: str, line: BankLine) -> BankLine:
    """Return a matched/ignored line to the unmatched pool. Commits."""
    line = await _locked_line(db, org_id, line)
    if line.status == "unmatched":
        await db.commit()
        return line
    line.status = "unmatched"
    line.matched_kind = None
    line.matched_id = None
    await db.commit()
    return line


async def set_ignored(db: AsyncSession, org_id: str, line: BankLine) -> BankLine:
    """Mark a line as intentionally unreconcilable (bank fees, interest). A matched
    line must be unmatched first. Commits."""
    line = await _locked_line(db, org_id, line)
    if line.status == "matched":
        raise ReconError("unmatch the line before ignoring it")
    if line.status == "ignored":
        await db.commit()
        return line
    line.status = "ignored"
    line.matched_kind = None
    line.matched_id = None
    await db.commit()
    return line
