"""PROD-009 — the whole-workspace data export.

The audit's finding was that a customer could put everything into this platform
and had no way to take it out again: the only export that existed read ONE
table (`archived_invoices`, WO-AI) for a client who had already left. This is
the other half — a live owner asking for everything the workspace holds, so
that leaving is a decision rather than a threat.

**Driven off the registry, never a hand-written list.** The set of tables is
`core.tenant.TENANT_MODELS` (the same registry the ORM tenant guard and the RLS
set-equality test are held to) plus the two child tables that carry no `org_id`
of their own and are reached through an already-scoped parent. A table added to
the product is therefore in the export the day it is registered, and
`tests/test_prod009_workspace_export.py` asserts the two sets are equal in both
directions — a hand-written list would have been a promise that silently rots.

**Credentials never leave.** Every column whose name matches `_SECRET_NAME` is
written as `null` with its name listed in the manifest's `redacted`, unless it
appears in `_NOT_SECRET` with a stated reason. The audit chain's own hashes are
the one such exception: they are the proof the trail was not edited, and an
export without them could not be verified by anyone.

**Bounded, and honest when it cannot be.** The zip is built into a temporary
file, one table and one document at a time; stored sources, the finished ZIP and
HTTP delivery use finite file reads. The existing
`settings.workspace_export_max_bytes` policy remains: streaming does not raise
it or certify aggregate disk/worker capacity. The in-memory test store still
owns its complete stored blob.

Delivery reuses WO-AI's rails exactly: the artefact goes to the `exports`
document class, the owner gets a ONE-TIME `EmailToken` link (purpose
`workspace_export`, seven days), opening it consumes it. Unlike WO-AI there is
NO public door: a whole-workspace dump of a live tenant is reachable only by an
authenticated owner of that tenant.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import zipfile
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.storage import StorageError, StorageReadError
from app.core.tenant import TENANT_MODELS, include_deleted
from app.models.base import Base
from app.models.document import Document
from app.models.email_token import PURPOSE_WORKSPACE_EXPORT
from app.models.invoice import LineItem
from app.models.issued_invoice import IssuedInvoiceLine
from app.models.membership import Membership
from app.models.organization import Organization
from app.models.user import User
from app.models.workspace_export import WorkspaceExport
from app.services import audit, documents, export_artefacts, jobs, mailer, verification

log = logging.getLogger("invoiceiq.workspace_export")


class ExportTooLarge(Exception):
    """The build crossed `settings.workspace_export_max_bytes`. Raised DURING
    the build so nothing bigger is written, and caught by `run_export`, which
    marks the request failed with the reason."""


WORKSPACE_EXPORT_KIND = "workspace.export"
LINK_TTL = timedelta(days=7)
#: A second request inside this window reuses the first. Building a whole
#: workspace is the most expensive thing a tenant can ask the worker to do, so
#: a double-click must not queue it twice.
REQUEST_COALESCE = timedelta(hours=1)

MANIFEST = "manifest.json"
README = "README.txt"
#: One JSON-lines file per table under this directory: one row per line, in the
#: table's own column names. JSONL because a whole workspace does not fit in a
#: single JSON document a reader can stream.
DATA_DIR = "data"
DOCS_DIR = "documents"
DOCS_INDEX = "documents/index.json"

#: Child tables with no `org_id` of their own — deliberately outside
#: `TENANT_MODELS` (see its comment), reached through an already-scoped parent,
#: and therefore named here so the export is complete anyway.
CHILD_MODELS = (LineItem, IssuedInvoiceLine)

#: Column names that carry a credential. Matched case-insensitively against the
#: column NAME, so a new secret column is redacted the day it is added rather
#: than the day someone remembers to list it.
_SECRET_NAME = re.compile(
    r"(password|secret|token|api_key|apikey|private_key|credential|signing)", re.I
)

#: Columns whose NAME matches `_SECRET_NAME` but which hold no credential. Each
#: needs a reason, and the test reads this table to prove every entry is real.
_NOT_SECRET: dict[tuple[str, str], str] = {}

#: Columns that hold a credential the NAME cannot reveal, because the secret is
#: embedded in free text the platform itself wrote. The name pattern is a good
#: first pass and a bad last one, and the review panel found the case that
#: proves it: `email_messages.body` stores every message this workspace has
#: sent, and several of those bodies ARE live credentials — a password-reset
#: link (1 h), an email-verification link (24 h), an INVITATION link (14 days,
#: and the invite's own `invitations.token` column is redacted two files away),
#: a customer-portal link, and the one-time download link of an earlier export.
#: Redacting the column defeats all of them at once; the row keeps `kind`,
#: `subject`, `to_email` and its timestamps, which is the part a reader of
#: their own outbox actually needs.
_ALWAYS_REDACT: dict[tuple[str, str], str] = {
    ("calendar_oauth_attempts", "state_hash"): "OAuth state binding is authentication material",
    ("calendar_oauth_attempts", "browser_hash"): "OAuth browser binding is authentication material",
    (
        "calendar_oauth_attempts",
        "sealed_verifier",
    ): "sealed OAuth PKCE verifier must never leave the vault",
    (
        "calendar_oauth_attempts",
        "session_id",
    ): "OAuth attempt session binding is authentication material",
    ("email_messages", "body"): (
        "message bodies embed live one-time links (password reset, email "
        "verification, invitation, customer portal, export download); a "
        "column-name rule cannot see a secret inside free text"
    ),
}


def _tenant_models() -> tuple[Any, ...]:
    return tuple(TENANT_MODELS) + CHILD_MODELS


def _redacted_columns(model: Any) -> set[str]:
    table = model.__tablename__
    return {
        c.key
        for c in sa_inspect(model).columns
        if (table, c.key) in _ALWAYS_REDACT
        or (_SECRET_NAME.search(c.key) and (table, c.key) not in _NOT_SECRET)
    }


def _encode(value: Any) -> Any:
    """One row value as something JSON can carry, losing nothing that matters.

    `Decimal` becomes its own STRING, never a float: this is money leaving the
    system, and a float would round it on the way out (ADR-0010)."""
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        # SQLite hands back naive datetimes for `DateTime(timezone=True)` and
        # Postgres hands back aware ones, so without this the customer-facing
        # format of every timestamp would depend on which database produced the
        # file. Naive values are UTC by construction everywhere in this codebase.
        return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bytes):
        # ADR-0008 left no binary columns; if one reappears, say so rather than
        # guessing an encoding.
        return f"[{len(value)} bytes not exported]"
    if isinstance(value, float):
        return value
    return str(value)


def _row_dict(model: Any, row: Any, redacted: set[str]) -> dict[str, Any]:
    return {
        c.key: (None if c.key in redacted else _encode(getattr(row, c.key)))
        for c in sa_inspect(model).columns
    }


async def _write_table(zf: zipfile.ZipFile, db: AsyncSession, model: Any, org_id: str) -> int:
    """One table as JSON lines. Returns how many rows were written."""
    redacted = _redacted_columns(model)
    stmt: Select[Base] = select(model)
    if model is User:
        # `users.org_id` is the ACTIVE-org pointer, not this workspace's roster
        # (B1.5), which is why the tenant guard special-cases `User` too. Using
        # it here would silently drop any member whose active workspace is
        # currently a different one — from the very file whose purpose is
        # completeness — while `memberships.jsonl` still listed them.
        stmt = stmt.where(
            User.id.in_(select(Membership.user_id).where(Membership.org_id == org_id))
        )
    elif hasattr(model, "org_id"):
        # Belt as well as the guard's braces: the tenant scope is applied by
        # `core.tenant` and by RLS, and named again here so this module reads
        # as scoped on its own.
        stmt = stmt.where(model.org_id == org_id)
    elif model is LineItem:
        from app.models.invoice import Invoice

        stmt = stmt.where(
            LineItem.invoice_id.in_(select(Invoice.id).where(Invoice.org_id == org_id))
        )
    elif model is IssuedInvoiceLine:
        from app.models.issued_invoice import IssuedInvoice

        stmt = stmt.where(
            IssuedInvoiceLine.invoice_id.in_(
                select(IssuedInvoice.id).where(IssuedInvoice.org_id == org_id)
            )
        )
    count = 0
    with zf.open(f"{DATA_DIR}/{model.__tablename__}.jsonl", "w") as fh:
        result = await db.stream(stmt)
        async for row in result.scalars():
            line = json.dumps(_row_dict(model, row, redacted), ensure_ascii=False)
            fh.write(line.encode("utf-8") + b"\n")
            count += 1
    return count


async def build_zip_file(db: AsyncSession, org_id: str) -> tuple[str, dict]:
    """Build the export into a temporary file. Returns (path, summary).

    The caller owns the file and must remove it. Binned rows are INCLUDED —
    they are the workspace's data until the bin purges them, and each row
    carries its own `deleted_at` so a reader can tell.
    """
    org = await db.get(Organization, org_id)
    models = _tenant_models()
    ceiling = settings.workspace_export_max_bytes
    fd, path = tempfile.mkstemp(prefix="workspace-export-", suffix=".zip")
    os.close(fd)

    def _check_size() -> None:
        """Abort the moment the file on disk crosses the ceiling.

        Checking only at the end bounded the worker's MEMORY and nothing else:
        a tenant far over the ceiling still wrote the whole thing to disk
        first, and the deployment's `/tmp` is an `emptyDir` with no size limit,
        so the refusal arrived after the node's disk was gone (review, A2)."""
        if os.path.getsize(path) > ceiling:
            raise ExportTooLarge(
                f"the export passed the {ceiling}-byte ceiling this deployment stores in one piece"
            )

    per_table: dict[str, int] = {}
    redacted_index: dict[str, list[str]] = {}
    doc_count = missing = 0
    try:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            with include_deleted():
                for model in models:
                    per_table[model.__tablename__] = await _write_table(zf, db, model, org_id)
                    _check_size()
                    cols = sorted(_redacted_columns(model))
                    if cols:
                        redacted_index[model.__tablename__] = cols

                # NOT the `exports` class. A produced zip is registered as a
                # document like any other, so without this exclusion export #2
                # embeds export #1 and #3 embeds #2: the artefact grows
                # geometrically until it trips its own ceiling, carries forward
                # rows that erasure or retention has since removed, and reports
                # every purged predecessor to the customer as a missing
                # document (PROD-009 review, A1).
                stored = list(
                    await db.scalars(
                        select(Document)
                        .where(Document.org_id == org_id, Document.kind != documents.EXPORTS)
                        .order_by(Document.kind, Document.sha256)
                    )
                )
            index: list[dict[str, Any]] = []
            for doc in stored:
                entry: dict[str, Any] = {
                    "sha256": doc.sha256,
                    "kind": doc.kind,
                    "size": doc.size,
                    "mime": doc.mime,
                    "filename": doc.filename,
                    "path": f"{DOCS_DIR}/{doc.kind}/{doc.sha256}",
                }
                try:
                    source = await documents.load_verified_file(
                        doc.kind, org_id, doc.sha256, size=doc.size
                    )
                except StorageReadError:
                    # A backend failure does not prove the bytes are gone.
                    # Abort so the job can retry a complete export after recovery.
                    raise
                except StorageError:
                    source = None
                if source is None:
                    entry["status"] = "missing"
                    entry.pop("path")
                    missing += 1
                else:
                    entry["status"] = "present"
                    try:
                        zf.write(source.path, f"{DOCS_DIR}/{doc.kind}/{doc.sha256}")
                    finally:
                        source.cleanup()
                    doc_count += 1
                    _check_size()
                index.append(entry)
            zf.writestr(DOCS_INDEX, json.dumps(index, indent=2, ensure_ascii=False))

            summary = {
                "workspace": org.name if org else None,
                "workspace_id": org_id,
                "exported_at": datetime.now(UTC).isoformat(),
                "format": "one JSON-lines file per table under data/, one file per stored document under documents/",
                "tables": per_table,
                "table_count": len(per_table),
                "row_count": sum(per_table.values()),
                "documents": doc_count,
                "missing_documents": missing,
                "redacted": redacted_index,
                "includes_deleted_rows": True,
            }
            zf.writestr(MANIFEST, json.dumps(summary, indent=2, ensure_ascii=False))
            zf.writestr(README, _readme(summary))
        _check_size()
    except BaseException:
        os.unlink(path)
        raise
    return path, summary


def _readme(summary: dict) -> str:
    return (
        f"Export of the workspace {summary['workspace']!r} "
        f"({summary['workspace_id']}), taken {summary['exported_at']}.\n\n"
        f"{summary['row_count']} rows across {summary['table_count']} tables, "
        f"{summary['documents']} stored files"
        + (
            f", {summary['missing_documents']} whose bytes are no longer held.\n"
            if summary["missing_documents"]
            else ".\n"
        )
        + "\n"
        "data/<table>.jsonl  one row per line, in the database's own column names.\n"
        "                    Amounts are strings, never floating point.\n"
        "                    Rows in the recycle bin are included and carry a\n"
        "                    deleted_at value.\n"
        "documents/          the stored files, by document class and content hash.\n"
        "documents/index.json  what each file is, and any whose bytes are gone.\n"
        "manifest.json       row counts per table and the columns held back.\n\n"
        "Columns holding a credential (passwords, API secrets, feed and invite\n"
        "tokens) are exported as null and named in manifest.json under\n"
        '"redacted". They are of no use outside this platform and would be a\n'
        "liability inside this file.\n"
    )


async def _recent_request(db: AsyncSession, org_id: str) -> WorkspaceExport | None:
    since = datetime.now(UTC) - REQUEST_COALESCE
    rows = list(
        await db.scalars(
            select(WorkspaceExport)
            # Coalesce a build IN FLIGHT, never one already delivered. Including
            # `ready` meant a second ask inside the hour returned 202 and the
            # page said "we're building it" while nothing was queued and no
            # second email was sent — so an owner whose first email went to spam
            # had no way to get another (PROD-009 review, A3).
            .where(WorkspaceExport.org_id == org_id, WorkspaceExport.status == "queued")
            .order_by(WorkspaceExport.created_at.desc())
        )
    )
    for row in rows:
        created = row.created_at
        if created.tzinfo is None:  # SQLite returns naive datetimes
            created = created.replace(tzinfo=UTC)
        if created >= since:
            return row
    return None


async def request_for_org(
    db: AsyncSession, org_id: str, *, email: str, user_id: str
) -> WorkspaceExport:
    """Record one request and queue the build. Coalesces with a live request
    from the last hour. Does not commit."""
    email = email.strip().lower()
    existing = await _recent_request(db, org_id)
    if existing is not None:
        return existing
    row = WorkspaceExport(
        org_id=org_id, requested_email=email, requested_by_user_id=user_id, status="queued"
    )
    db.add(row)
    await db.flush()
    job = await jobs.enqueue(
        db, WORKSPACE_EXPORT_KIND, {"export_id": row.id}, org_id=org_id, commit=False
    )
    row.job_id = job.id
    await audit.record(
        db,
        audit.A.WORKSPACE_EXPORT_REQUESTED,
        target_type="workspace_export",
        target_id=row.id,
        meta={"to": email},
        org_id=org_id,
        actor=(user_id, email),
    )
    return row


async def list_requests(db: AsyncSession, org_id: str) -> list[WorkspaceExport]:
    return list(
        await db.scalars(
            select(WorkspaceExport)
            .where(WorkspaceExport.org_id == org_id)
            .order_by(WorkspaceExport.created_at.desc())
        )
    )


def _download_link(raw: str) -> str:
    return f"{settings.app_base_url.rstrip('/')}/api/v1/workspace/export/download/{raw}"


async def run_export(db: AsyncSession, org_id: str, export_id: str) -> dict:
    """The worker's half: build, store, issue the one-time link, email it.
    Marks the request `failed` with the reason on any error. Does not commit."""
    row = await db.scalar(
        select(WorkspaceExport).where(
            WorkspaceExport.org_id == org_id, WorkspaceExport.id == export_id
        )
    )
    if row is None:
        raise LookupError(f"workspace export {export_id} not found for org {org_id}")
    user = await db.get(User, row.requested_by_user_id)
    if user is None:
        row.status = "failed"
        row.error = "requesting owner no longer exists"
        return {"export_id": row.id, "status": row.status}

    try:
        path, summary = await build_zip_file(db, org_id)
    except ExportTooLarge as exc:
        row.status = "failed"
        row.error = str(exc)
        log.error("workspace export %s refused: %s", row.id, row.error)
        return {"export_id": row.id, "status": row.status}
    try:
        org = await db.get(Organization, org_id)
        filename = f"workspace-{_safe_name(org.name if org else None)}.zip"
        sha, size = await documents.store_file(
            documents.EXPORTS, org_id, path, "application/zip", db=db, filename=filename
        )
    finally:
        try:
            os.unlink(path)
        except OSError:
            log.warning("Could not remove a private workspace export staging file")
    now = datetime.now(UTC)
    row.sha256, row.size = sha, size
    row.rows = summary["row_count"]
    row.tables = summary["table_count"]
    row.documents = summary["documents"]
    row.missing_documents = summary["missing_documents"]
    row.status = "ready"
    row.ready_at = now
    row.link_expires_at = now + LINK_TTL
    # The EXPORT's org, not `user.org_id` — that is the requester's ACTIVE-org
    # pointer, and this runs on the worker minutes later, after which they may
    # have switched workspaces. And the token id is recorded on the row, so
    # redeeming the link resolves exactly the artefact it was issued for rather
    # than "the newest ready export for this person" (review, S3).
    raw, token = await verification.issue_token(
        db, user, PURPOSE_WORKSPACE_EXPORT, LINK_TTL, org_id=org_id
    )
    row.download_token_id = token.id
    subject, body = mailer.workspace_export_email(
        workspace=org.name if org else "your workspace",
        link=_download_link(raw),
        rows=summary["row_count"],
        tables=summary["table_count"],
        documents=summary["documents"],
        missing_documents=summary["missing_documents"],
        ttl_days=LINK_TTL.days,
    )
    await mailer.send(
        db,
        org_id,
        kind="workspace_export",
        to_email=row.requested_email,
        subject=subject,
        body=body,
    )
    return {
        "export_id": row.id,
        "status": row.status,
        "rows": row.rows,
        "tables": row.tables,
        "documents": row.documents,
        "missing_documents": row.missing_documents,
        "bytes": size,
        "sha256": sha,
    }


_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_name(name: str | None) -> str:
    cleaned = _SAFE.sub("-", (name or "workspace").strip()).strip("-")
    return (cleaned or "workspace")[:60]


async def download(db: AsyncSession, raw: str) -> tuple[documents.StoredFile, str, str] | None:
    """Consume the one-time link and hand back (staged file, filename, org_id) — or
    None for an unknown, used, expired or already-purged link, all the same.
    UNSCOPED: the link names its own tenant."""
    token = await verification.consume(db, raw, PURPOSE_WORKSPACE_EXPORT)
    if token is None:
        return None
    row = await db.scalar(
        select(WorkspaceExport)
        .where(
            WorkspaceExport.org_id == token.org_id,
            WorkspaceExport.download_token_id == token.id,
            WorkspaceExport.status == "ready",
            WorkspaceExport.sha256.is_not(None),
            WorkspaceExport.purged_at.is_(None),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None or row.sha256 is None:
        return None
    try:
        staged = await export_artefacts.prepare_file(row.org_id, row.sha256, row.size)
    except StorageReadError:
        # Preserve the token/download transaction for a retry after recovery.
        raise
    except StorageError:
        staged = None
    if staged is None:
        log.error("workspace export %s: stored zip %s is missing", row.id, row.sha256)
        return None
    try:
        row.status = "downloaded"
        row.downloaded_at = datetime.now(UTC)
        org = await db.get(Organization, row.org_id)
        await audit.record(
            db,
            audit.A.WORKSPACE_EXPORT_DOWNLOADED,
            target_type="workspace_export",
            target_id=row.id,
            meta={"bytes": row.size, "rows": row.rows},
            org_id=row.org_id,
            actor=(row.requested_by_user_id, row.requested_email),
        )
        return staged, f"workspace-{_safe_name(org.name if org else None)}.zip", row.org_id
    except BaseException:
        staged.cleanup()
        raise
