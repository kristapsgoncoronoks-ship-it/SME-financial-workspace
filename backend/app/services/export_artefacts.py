"""PROD-009 — destroying export bytes once their link is dead.

Found while building the whole-workspace export: NOTHING ever deleted a
produced export zip. WO-AI stored its archive zip in the `exports` document
class, handed out a link that expires after seven days, and then kept the bytes
for ever. The `exports` prefix appears in exactly two places in the codebase —
the store and the load — and in neither is there a delete. So a workspace that
asked for an export every month accumulated a copy of its own data every month,
each one readable by anyone who could read the bucket, long after the only
credential that pointed at it had expired.

That was a small leak while an export was one table. It is not small now that
an export is the whole workspace, so this runs daily for every tenant, beside
the recycle-bin and archive purges:

  a request whose link has expired, or which has already been downloaded and
  sat for `GRACE`, loses its bytes. The ROW stays — the request, its size, its
  counts and now its `purged_at` are the record that an export happened, and
  that record is what an audit asks for. Only the copy of the data goes.

Bytes first, then the mark (WO-V's rule: an object whose row says it is gone
must really be gone), and both request tables are answered by the same pass.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.core.storage import StorageError, StorageReadTooLarge
from app.models.archive_export import ArchiveExport
from app.models.document import Document
from app.models.workspace_export import WorkspaceExport
from app.services import audit, documents, retention

log = logging.getLogger("invoiceiq.export_artefacts")

EXPORT_PURGE_KIND = "export.purge_expired"

#: A downloaded export keeps its bytes this long, so that "the download
#: failed halfway, send it again" is a support answer rather than a rebuild.
GRACE = timedelta(days=1)

_TABLES: tuple[type[ArchiveExport] | type[WorkspaceExport], ...] = (ArchiveExport, WorkspaceExport)


def _integrity_error() -> AppError:
    return AppError(
        "Stored export integrity could not be verified. Restore the original export and retry.",
        code="export_integrity_unverified",
        status=503,
        headers={"Cache-Control": "private, no-store"},
    )


async def prepare_file(org_id: str, sha256: str, size: int | None) -> documents.StoredFile | None:
    """Stage only the admitted export before consuming its download transaction.

    Recorded sizes bound the storage read; historical rows without a size retain
    their original read contract. Both paths verify the recorded digest. This
    stages bytes only: the caller owns cleanup, token, audit and commit/rollback.
    """
    if size is not None and size < 0:
        raise _integrity_error()
    try:
        staged = await documents.load_file(
            documents.EXPORTS,
            org_id,
            sha256,
            max_bytes=max(1, size) if size is not None else None,
        )
    except StorageReadTooLarge as exc:
        raise _integrity_error() from exc
    if staged is None:
        return None
    if (size is not None and staged.size != size) or staged.sha256 != sha256:
        staged.cleanup()
        raise _integrity_error()
    return staged


async def purge_expired(db: AsyncSession, org_id: str, *, now: datetime | None = None) -> dict:
    """Destroy the stored bytes of every export past its link. Does not commit.

    Returns `{table: count}` for the audit line, and audits only when something
    was actually destroyed — a daily no-op should not write a trail entry per
    tenant per day."""
    now = now or datetime.now(UTC)
    # ADR-0019: a legal hold suspends ALL purging for the tenant — preservation
    # overrides minimisation. Every other destruction path in the product asks
    # (`retention.purge`, the invoice bin, the archive purge, GDPR erasure) and
    # this one did not, which would have destroyed the only assembled snapshot
    # of the workspace as it stood when the matter opened, while the hold was
    # live (PROD-009 review, ARCH-A).
    if await retention.is_on_hold(db, org_id):
        return {"held": True}
    purged: dict[str, int] = {}
    for model in _TABLES:
        rows: list[ArchiveExport | WorkspaceExport] = list(
            await db.scalars(
                select(model).where(
                    model.org_id == org_id,
                    model.sha256.is_not(None),
                    model.purged_at.is_(None),
                    or_(
                        model.link_expires_at < now,
                        model.downloaded_at < now - GRACE,
                    ),
                )
            )
        )
        for row in rows:
            try:
                await documents.delete(documents.EXPORTS, org_id, row.sha256)
            except StorageError:  # pragma: no cover - backend-specific
                log.warning("export %s: could not delete %s", row.id, row.sha256)
                continue
            # The registry row goes with the bytes. Leaving it made the object
            # store and the `documents` table disagree, and the next export
            # then read that row, failed to load it, and reported the
            # workspace's own purged predecessor to its owner as a document
            # that "could no longer be found in storage" (review, ARCH-C).
            await db.execute(
                delete(Document).where(
                    Document.org_id == org_id,
                    Document.sha256 == row.sha256,
                    Document.kind == documents.EXPORTS,
                )
            )
            row.purged_at = now
            purged[model.__tablename__] = purged.get(model.__tablename__, 0) + 1
    if purged:
        await audit.record(
            db,
            audit.A.EXPORT_ARTEFACTS_PURGED,
            target_type="export",
            target_id=org_id,
            meta=purged,
            org_id=org_id,
        )
    return purged
