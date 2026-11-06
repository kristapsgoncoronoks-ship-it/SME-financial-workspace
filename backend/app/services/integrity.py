"""Document-integrity verification (ADR-0008 / arch Phase 2.8).

The primary database is backed by managed-Postgres PITR and object storage is
versioned — those are platform-level backups. What the *app* owns is proving the
integrity of what it stored: every document reference in the DB (receipts, logos,
inbound email attachments, and the original supplier-invoice uploads) is
content-addressed by sha256, so we can re-hash the stored object and confirm it
still exists and still matches. This catches silent corruption, a lost object, or
a storage/DB drift that a backup alone won't.

Tenant-scoped: `verify_documents(db, org_id)` checks one tenant. A full sweep runs
one job per org (see the scheduler / jobs). Never raises — a failure to read an
object is a finding, not an exception.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import NotNullable, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import money, storage
from app.models.document_version import (
    OWNER_EXPENSE_RECEIPT,
    OWNER_ISSUER_LOGO,
    DocumentVersion,
)
from app.models.email_intake import InboundInvoice
from app.models.expense import ExpenseItem, ExpenseReport
from app.models.extraction_run import ExtractionRun
from app.models.invoice import Invoice
from app.models.issued_invoice import IssuedInvoice
from app.models.issuer import IssuerProfile
from app.models.payment import Payment
from app.models.receipt import Receipt
from app.models.supplier_payment import SupplierPayment
from app.services import documents

_ZERO = Decimal("0")


@dataclass
class DocIssue:
    kind: str  # receipt | logo | email_attachment | upload
    entity_id: str
    problem: str  # missing | mismatch | error
    detail: str = ""


@dataclass
class IntegrityReport:
    checked: int = 0
    ok: int = 0
    issues: list[DocIssue] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return not self.issues


SCOPE_DOCUMENTS = "documents"
SCOPE_LEDGER = "ledger"
SCOPE_VERSIONS = "versions"


async def reference_count(db: AsyncSession, org_id: str, scope: str) -> int:
    """PERF-009: how many references the named sweep would touch for this
    tenant — counted from the same predicates the sweep reads, so the route
    can decide BEFORE doing the work whether it fits a request or belongs on
    the queue. Indexed counts only; no object is loaded."""
    if scope == SCOPE_DOCUMENTS:
        receipts = await db.scalar(
            select(func.count(ExpenseItem.id))
            .join(ExpenseReport, ExpenseReport.id == ExpenseItem.report_id)
            .where(ExpenseReport.org_id == org_id, ExpenseItem.receipt_sha256.is_not(None))
        )
        logos = await db.scalar(
            select(func.count(IssuerProfile.id)).where(
                IssuerProfile.org_id == org_id, IssuerProfile.logo_sha256.is_not(None)
            )
        )
        attachments = await db.scalar(
            select(func.count(InboundInvoice.id)).where(
                InboundInvoice.org_id == org_id,
                InboundInvoice.sha256.is_not(None),
                InboundInvoice.status != "rejected",
            )
        )
        uploads = await db.scalar(
            select(func.count(func.distinct(ExtractionRun.source_sha256))).where(
                ExtractionRun.org_id == org_id, ExtractionRun.source_sha256.is_not(None)
            )
        )
        return int(receipts or 0) + int(logos or 0) + int(attachments or 0) + int(uploads or 0)
    if scope == SCOPE_LEDGER:
        issued = await db.scalar(
            select(func.count(IssuedInvoice.id)).where(IssuedInvoice.org_id == org_id)
        )
        received = await db.scalar(select(func.count(Invoice.id)).where(Invoice.org_id == org_id))
        receipts = await db.scalar(select(func.count(Receipt.id)).where(Receipt.org_id == org_id))
        return int(issued or 0) + int(received or 0) + int(receipts or 0)
    if scope == SCOPE_VERSIONS:
        versions = await db.scalar(
            select(func.count(DocumentVersion.id)).where(DocumentVersion.org_id == org_id)
        )
        return int(versions or 0)
    raise ValueError(f"unknown integrity scope {scope!r}")


async def _check(
    report: IntegrityReport, kind: str, entity_id: str, prefix: str, org_id: str, sha256: str
) -> None:
    report.checked += 1
    try:
        actual = await documents.digest(prefix, org_id, sha256)
    except storage.StorageReadError:
        report.issues.append(
            DocIssue(
                kind,
                entity_id,
                "error",
                "Stored document could not be read. Check storage and retry.",
            )
        )
        return
    except storage.StorageError:
        report.issues.append(DocIssue(kind, entity_id, "missing", "object not found in storage"))
        return
    except Exception as exc:  # noqa: BLE001 — any backend error is a finding
        report.issues.append(
            DocIssue(
                kind,
                entity_id,
                "error",
                f"{type(exc).__name__}: stored document verification failed.",
            )
        )
        return
    if actual is None:
        report.issues.append(DocIssue(kind, entity_id, "missing", "no object bytes"))
        return
    if actual != sha256:
        report.issues.append(
            DocIssue(kind, entity_id, "mismatch", f"expected {sha256[:12]}…, got {actual[:12]}…")
        )
    else:
        report.ok += 1


async def verify_documents(db: AsyncSession, org_id: str) -> IntegrityReport:
    """Re-hash every storage-backed document reference for one tenant."""
    report = IntegrityReport()

    # Receipts (ExpenseItem → ExpenseReport carries org_id).
    rows = await db.execute(
        select(ExpenseItem.id, NotNullable(ExpenseItem.receipt_sha256))
        .join(ExpenseReport, ExpenseReport.id == ExpenseItem.report_id)
        .where(ExpenseReport.org_id == org_id, ExpenseItem.receipt_sha256.is_not(None))
    )
    for item_id, sha in rows:
        await _check(report, "receipt", item_id, documents.RECEIPTS, org_id, sha)

    # Logos.
    for profile_id, sha in await db.execute(
        select(IssuerProfile.id, NotNullable(IssuerProfile.logo_sha256)).where(
            IssuerProfile.org_id == org_id, IssuerProfile.logo_sha256.is_not(None)
        )
    ):
        await _check(report, "logo", profile_id, documents.LOGOS, org_id, sha)

    # Inbound email attachments (retained rows only — rejected ones store no bytes).
    for row_id, sha in await db.execute(
        select(InboundInvoice.id, NotNullable(InboundInvoice.sha256)).where(
            InboundInvoice.org_id == org_id,
            InboundInvoice.sha256.is_not(None),
            InboundInvoice.status != "rejected",
        )
    ):
        await _check(report, "email_attachment", row_id, documents.EMAIL_ATTACHMENTS, org_id, sha)

    # Original supplier-invoice uploads (the legal record). The parse/OCR worker
    # reads these bytes back from object storage, so a silent corruption or lost
    # object here is a corrupted *original* — it must be swept like any other
    # reference. Content-addressed storage means many runs (re-uploads / retries)
    # can share one sha; hash each distinct object ONCE, keyed to a representative
    # run so a finding still points at a concrete row.
    seen_uploads: set[str] = set()
    for run_id, sha in await db.execute(
        select(ExtractionRun.id, NotNullable(ExtractionRun.source_sha256)).where(
            ExtractionRun.org_id == org_id, ExtractionRun.source_sha256.is_not(None)
        )
    ):
        if sha in seen_uploads:
            continue
        seen_uploads.add(sha)
        await _check(report, "upload", run_id, documents.UPLOADS, org_id, sha)

    return report


async def _ledger_sum(db: AsyncSession, org_id: str, column, value) -> Decimal:
    total = await db.scalar(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(
            Payment.org_id == org_id, column == value
        )
    )
    return money.q2(Decimal(total or 0))


async def verify_ledger(db: AsyncSession, org_id: str) -> IntegrityReport:
    """Verify the payment-ledger invariants for one tenant, both sides of the
    house (Slices 3c/5c for AR; DB-013 for AP): every issued invoice's
    `amount_paid` cache equals the SUM of its payment-ledger entries, every
    received (supplier) invoice's `amount_paid` cache equals the SUM of its
    supplier-payment entries, and no receipt is allocated beyond the amount
    received. Never raises — a broken invariant is a finding, not an exception."""
    report = IntegrityReport()

    # 1) amount_paid cache == SUM(payments.amount) per issued invoice.
    invoices = list(await db.scalars(select(IssuedInvoice).where(IssuedInvoice.org_id == org_id)))
    ar_sums: dict[str, Decimal] = (
        {
            inv_id: money.q2(Decimal(total or 0))
            for inv_id, total in await db.execute(
                select(Payment.issued_invoice_id, func.sum(Payment.amount))
                .where(Payment.org_id == org_id)
                .group_by(Payment.issued_invoice_id)
            )
        }
        if invoices
        else {}
    )
    for inv in invoices:
        report.checked += 1
        ledger = ar_sums.get(inv.id, money.q2(_ZERO))
        cache = money.q2(Decimal(inv.amount_paid or _ZERO))
        if ledger != cache:
            report.issues.append(
                DocIssue(
                    "invoice_ledger",
                    inv.id,
                    "mismatch",
                    f"amount_paid {cache} != ledger sum {ledger}",
                )
            )
        else:
            report.ok += 1

    # 1b) DB-013: the AP mirror. `invoices.amount_paid` is a running-total cache
    # of `supplier_payments` (services.ap_payments keeps it in step through
    # signed ledger entries); the cache was never checked against its ledger.
    # One grouped sum, then one pass over the cached column: every invoice of
    # the tenant is checked, including those with no ledger entry at all (a
    # cache above zero with an empty ledger is the drift most likely to occur —
    # a direct column write that bypassed the service).
    ap_sums: dict[str, Decimal] = {
        inv_id: money.q2(Decimal(total or 0))
        for inv_id, total in await db.execute(
            select(SupplierPayment.invoice_id, func.sum(SupplierPayment.amount))
            .where(SupplierPayment.org_id == org_id)
            .group_by(SupplierPayment.invoice_id)
        )
    }
    for inv_id, cached in await db.execute(
        select(Invoice.id, Invoice.amount_paid).where(Invoice.org_id == org_id)
    ):
        report.checked += 1
        ledger = ap_sums.get(inv_id, _ZERO)
        cache = money.q2(Decimal(cached or _ZERO))
        if ledger != cache:
            report.issues.append(
                DocIssue(
                    "supplier_invoice_ledger",
                    inv_id,
                    "mismatch",
                    f"amount_paid {cache} != supplier ledger sum {ledger}",
                )
            )
        else:
            report.ok += 1

    # 2) SUM(allocations) <= receipt.amount per receipt.
    receipts = list(await db.scalars(select(Receipt).where(Receipt.org_id == org_id)))
    receipt_sums: dict[str, Decimal] = (
        {
            receipt_id: money.q2(Decimal(total or 0))
            for receipt_id, total in await db.execute(
                select(NotNullable(Payment.receipt_id), func.sum(Payment.amount))
                .where(Payment.org_id == org_id, Payment.receipt_id.is_not(None))
                .group_by(Payment.receipt_id)
            )
        }
        if receipts
        else {}
    )
    for r in receipts:
        report.checked += 1
        allocated = receipt_sums.get(r.id, money.q2(_ZERO))
        if allocated > money.q2(Decimal(r.amount)):
            report.issues.append(
                DocIssue(
                    "receipt_allocation",
                    r.id,
                    "over_allocated",
                    f"allocated {allocated} > received {money.q2(Decimal(r.amount))}",
                )
            )
        else:
            report.ok += 1

    return report


async def verify_versions(db: AsyncSession, org_id: str) -> IntegrityReport:
    """Verify the document-version chain invariants for one tenant (Slice 5g):
    every single-file slot has EXACTLY ONE current version, that current version's
    sha matches the owner's `*_sha256` cache, and every owner holding a file has a
    version slot. A drift here means the history and the live pointer disagree —
    exactly the audit gap this table exists to close. Never raises."""
    report = IntegrityReport()

    # The live pointer each slot's current version must agree with.
    expected: dict[tuple[str, str], str] = {}
    for pid, sha in await db.execute(
        select(IssuerProfile.id, NotNullable(IssuerProfile.logo_sha256)).where(
            IssuerProfile.org_id == org_id, IssuerProfile.logo_sha256.is_not(None)
        )
    ):
        expected[(OWNER_ISSUER_LOGO, pid)] = sha
    for item_id, sha in await db.execute(
        select(ExpenseItem.id, NotNullable(ExpenseItem.receipt_sha256))
        .join(ExpenseReport, ExpenseReport.id == ExpenseItem.report_id)
        .where(ExpenseReport.org_id == org_id, ExpenseItem.receipt_sha256.is_not(None))
    ):
        expected[(OWNER_EXPENSE_RECEIPT, item_id)] = sha

    # Group every version row by its slot.
    slots: dict[tuple[str, str], list[DocumentVersion]] = {}
    for v in await db.scalars(select(DocumentVersion).where(DocumentVersion.org_id == org_id)):
        slots.setdefault((v.owner_type, v.owner_id), []).append(v)

    # 1) Exactly one current per slot; the current sha matches the owner cache.
    for slot, versions in slots.items():
        report.checked += 1
        currents = [v for v in versions if v.is_current]
        if len(currents) != 1:
            report.issues.append(
                DocIssue(
                    "document_version",
                    slot[1],
                    "current_count",
                    f"{slot[0]}: {len(currents)} current versions (expected 1)",
                )
            )
            continue
        cur = currents[0]
        exp = expected.get(slot)
        if exp is not None and cur.sha256 != exp:
            report.issues.append(
                DocIssue(
                    "document_version",
                    slot[1],
                    "mismatch",
                    f"{slot[0]}: current v{cur.version} sha {cur.sha256[:12]}… "
                    f"!= owner cache {exp[:12]}…",
                )
            )
        else:
            report.ok += 1

    # 2) Every owner that holds a file must have a version slot.
    for slot in expected:
        if slot not in slots:
            report.checked += 1
            report.issues.append(
                DocIssue(
                    "document_version",
                    slot[1],
                    "missing",
                    f"{slot[0]}: owner has a stored file but no version history",
                )
            )

    return report
