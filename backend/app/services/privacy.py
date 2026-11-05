"""GDPR right-to-erasure (Art. 17) — DSAR handling that respects the law's own
limits (ADR-0020).

Erasure is NOT an unconditional delete. Art. 17(3)(b) exempts data a controller
must keep to meet a legal obligation, and an active **legal hold** (preservation
duty) overrides erasure entirely. So a request keyed by a data subject's email
resolves each personal-data location to one of:

  • erase   — pseudonymise / delete the personal data now,
  • retain  — deliberately kept for a statutory / integrity reason, reported,
  • blocked — a legal hold is active; nothing is erased until it is released.

What we do per location:
  users            → pseudonymise (email→erased token, name redacted, deactivated);
                     the ROW stays so audit attribution + FKs remain intact.
  expenses         → redact the author name on that person's expense reports.
  inbound_email    → delete the raw inbound record + its stored attachment bytes.
  issued_invoices  → RETAIN: issued tax invoices are statutory records (buyer
                     identity is required content); erasure is exempt.
  audit_trail      → RETAIN: the hash-chained trail is a tamper-evident integrity
                     record; redacting it would break the chain.

Every executed erasure is audited as `privacy.erasure` with a HASHED subject
reference + per-location counts (never the cleartext email or any raw value), so
we prove the request was handled without re-storing the subject's identity.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.archive_export import ArchiveExport
from app.models.email_intake import InboundInvoice
from app.models.expense import ExpenseReport
from app.models.issued_invoice import IssuedInvoice
from app.models.membership import Membership
from app.models.user import User
from app.models.workspace_export import WorkspaceExport
from app.services import audit, documents, export_artefacts, retention

log = logging.getLogger("invoiceiq.privacy")

ERASE, RETAIN, BLOCKED = "erase", "retain", "blocked"
_REDACTED_NAME = "Erased user"

_RETAIN_REASON = {
    "issued_invoices": "Statutory accounting retention — issued tax invoices are kept; "
    "erasure is exempt (GDPR Art. 17(3)(b)).",
    "audit_trail": "Kept as a tamper-evident integrity record; redacting it would break "
    "the audit hash chain.",
}


@dataclass
class Location:
    key: str
    label: str
    matched: int
    action: str
    reason: str | None = None


@dataclass
class ErasureReport:
    email: str
    on_hold: bool
    locations: list[Location] = field(default_factory=list)
    executed: bool = False


async def _export_snapshot_location(db: AsyncSession, org_id: str, on_hold: bool) -> Location:
    """Disclose potentially affected snapshots, without claiming content matching.

    A full workspace snapshot can contain any subject's data, independent of its
    requester. We do not reopen ZIPs during a DSAR or select a new purge policy.
    Stream only lifecycle timestamps and report existing purge eligibility;
    eligibility is not proof that the daily purge has actually removed bytes.
    """
    count = unknown = 0
    earliest = latest = None

    def utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    for model in (ArchiveExport, WorkspaceExport):
        rows = await db.stream(
            select(model.link_expires_at, model.downloaded_at)
            .where(model.org_id == org_id, model.sha256.is_not(None), model.purged_at.is_(None))
            .execution_options(yield_per=200)
        )
        try:
            async for expires, downloaded in rows:
                count += 1
                deadlines = []
                if expires is not None:
                    deadlines.append(utc(expires))
                if downloaded is not None:
                    deadlines.append(utc(downloaded) + export_artefacts.GRACE)
                if not deadlines:
                    unknown += 1
                    continue
                eligible = min(deadlines)
                earliest = eligible if earliest is None else min(earliest, eligible)
                latest = eligible if latest is None else max(latest, eligible)
        finally:
            await rows.close()
    reason = (
        "Stored archive/workspace snapshots are potentially affected; their contents have not "
        "been scanned for this subject. Existing export retention applies. "
    )
    if earliest is not None and latest is not None:
        reason += f"Recorded purge eligibility ranges from {earliest.isoformat()} to {latest.isoformat()}. "
    reason += f"{unknown} stored snapshots have unknown purge eligibility. "
    if on_hold:
        reason += "An active legal hold has suspended artifact purging. "
    reason += "These are eligibility dates, not confirmation of deletion."
    return Location(
        "export_artifacts",
        "Stored export snapshots (potential subject data)",
        count,
        RETAIN,
        reason,
    )


def _subject_hash(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()[:16]


async def _scan(db: AsyncSession, org_id: str, email: str) -> tuple[list[str], dict[str, int]]:
    """Locate the subject across personal-data locations. Returns the matched
    user ids and per-location match counts."""
    email = email.strip()
    # B1.5: the subject belongs to this org iff they hold a MEMBERSHIP here —
    # `users.org_id` is only the active-org pointer, so a member currently
    # switched into another org must still be found by this org's DSAR scan
    # (status is irrelevant: a suspended member is still this org's data).
    user_ids = list(
        await db.scalars(
            select(User.id)
            .join(Membership, and_(Membership.user_id == User.id, Membership.org_id == org_id))
            .where(func.lower(User.email) == email.lower())
        )
    )

    async def _count(model, *where) -> int:
        return int(await db.scalar(select(func.count()).select_from(model).where(*where)) or 0)

    counts = {
        "users": len(user_ids),
        "expenses": await _count(
            ExpenseReport, ExpenseReport.org_id == org_id, ExpenseReport.employee_id.in_(user_ids)
        )
        if user_ids
        else 0,
        "inbound_email": await _count(
            InboundInvoice,
            InboundInvoice.org_id == org_id,
            func.lower(InboundInvoice.from_addr) == email.lower(),
        ),
        "issued_invoices": await _count(
            IssuedInvoice,
            IssuedInvoice.org_id == org_id,
            func.lower(IssuedInvoice.buyer_email) == email.lower(),
        ),
        "audit_trail": await _count(
            audit.AuditEvent,
            audit.AuditEvent.org_id == org_id,
            func.lower(audit.AuditEvent.actor_email) == email.lower(),
        ),
    }
    return user_ids, counts


def _locations(counts: dict[str, int], on_hold: bool) -> list[Location]:
    def erasable(key, label):
        act = BLOCKED if on_hold else ERASE
        reason = (
            "A legal hold is active — erasure is suspended until it is released."
            if on_hold
            else None
        )
        return Location(key, label, counts[key], act, reason)

    return [
        erasable("users", "User accounts"),
        erasable("expenses", "Expense-report author name"),
        erasable("inbound_email", "Inbound email records"),
        Location(
            "issued_invoices",
            "Issued tax invoices (buyer)",
            counts["issued_invoices"],
            RETAIN,
            _RETAIN_REASON["issued_invoices"],
        ),
        Location(
            "audit_trail",
            "Audit trail (actor)",
            counts["audit_trail"],
            RETAIN,
            _RETAIN_REASON["audit_trail"],
        ),
    ]


async def preview(db: AsyncSession, org_id: str, email: str) -> ErasureReport:
    """What an erasure WOULD do — classifies every location, mutates nothing."""
    on_hold = await retention.is_on_hold(db, org_id)
    _, counts = await _scan(db, org_id, email)
    exports = await _export_snapshot_location(db, org_id, on_hold)
    return ErasureReport(
        email=email,
        on_hold=on_hold,
        locations=[*_locations(counts, on_hold), exports],
        executed=False,
    )


async def erase(
    db: AsyncSession, org_id: str, email: str, *, actor_email: str | None
) -> ErasureReport:
    """Execute the erasure. Refuses (no-op) while a legal hold is active. Audited
    with a hashed subject reference + per-location counts."""
    on_hold = await retention.is_on_hold(db, org_id)
    user_ids, counts = await _scan(db, org_id, email)
    exports = await _export_snapshot_location(db, org_id, on_hold)
    if on_hold:
        return ErasureReport(
            email=email,
            on_hold=True,
            locations=[*_locations(counts, True), exports],
            executed=False,
        )

    subject = _subject_hash(email)

    # users → pseudonymise (email is globally unique, so at most one row).
    for uid in user_ids:
        erased_email = f"erased-{subject}@erased.invalid"
        await db.execute(
            update(User)
            .where(User.id == uid)
            .values(email=erased_email, name=_REDACTED_NAME, is_active=False)
        )
        # Keep the membership identity snapshot consistent with the erasure, and
        # suspend the memberships so the person loses access everywhere.
        await db.execute(
            update(Membership)
            .where(Membership.user_id == uid)
            .values(email=erased_email, name=_REDACTED_NAME, status="suspended")
        )
    # expenses → redact the author name on this person's reports.
    if user_ids:
        await db.execute(
            update(ExpenseReport)
            .where(ExpenseReport.employee_id.in_(user_ids))
            .values(employee_name=_REDACTED_NAME)
        )
    # inbound email → delete rows + stored attachment bytes.
    inbounds = list(
        await db.scalars(
            select(InboundInvoice).where(
                InboundInvoice.org_id == org_id,
                func.lower(InboundInvoice.from_addr) == email.strip().lower(),
            )
        )
    )
    for row in inbounds:
        if row.sha256:
            try:
                await documents.delete(documents.EMAIL_ATTACHMENTS, org_id, row.sha256)
            except Exception as exc:  # noqa: BLE001 - byte cleanup must not block erasure
                log.warning("erasure: attachment cleanup failed for %s: %s", row.id, exc)
        await db.delete(row)

    erased = {
        "users": counts["users"],
        "expenses": counts["expenses"],
        "inbound_email": counts["inbound_email"],
    }
    retained = {
        "issued_invoices": counts["issued_invoices"],
        "audit_trail": counts["audit_trail"],
        "export_artifacts": exports.matched,
    }
    await audit.record(
        db,
        "privacy.erasure",
        org_id=org_id,
        meta={"subject": subject, "erased": erased, "retained": retained},
    )
    await db.commit()

    return ErasureReport(
        email=email, on_hold=False, locations=[*_locations(counts, False), exports], executed=True
    )
