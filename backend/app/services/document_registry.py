"""Document-registry service (Slice 5d).

`register` upserts one row per distinct `(org, sha256, kind)` stored object. It is
called from the storage choke point (`documents.store`) and does NOT commit — the
caller's own commit (the receipt save, the logo save, the attachment job)
persists it in the same transaction.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import Document


async def register(
    db: AsyncSession,
    org_id: str,
    *,
    sha256: str,
    size: int,
    kind: str,
    mime: str | None = None,
    filename: str | None = None,
    uploaded_by: str | None = None,
) -> Document:
    """Record (or touch) the metadata for a stored object. Content-addressed, so a
    re-store of the same bytes under the same kind updates the existing row rather
    than duplicating. Does not commit."""
    existing = await db.scalar(
        select(Document).where(
            Document.org_id == org_id, Document.sha256 == sha256, Document.kind == kind
        )
    )
    if existing is not None:
        if filename:
            existing.filename = filename
        if mime:
            existing.mime = mime
        return existing
    doc = Document(
        org_id=org_id,
        sha256=sha256,
        size=size,
        kind=kind,
        mime=mime,
        filename=filename,
        uploaded_by=uploaded_by,
    )
    connection = await db.connection()
    if connection.dialect.name == "sqlite":
        # sqlite3 legacy transaction mode does not BEGIN for a SELECT or a
        # SAVEPOINT. Releasing a first savepoint would otherwise commit this
        # document before the caller commits. Preserve the original flush-only
        # contract without changing transaction mode for the rest of the app.
        raw = await connection.get_raw_connection()
        driver = raw.driver_connection
        if driver is not None and not driver.in_transaction:
            await connection.exec_driver_sql("BEGIN")
    try:
        # A second connection can pass the same absence check. Isolate only
        # this insert: never roll back the caller's pending invoice/job work.
        async with db.begin_nested():
            db.add(doc)
            await db.flush()
    except IntegrityError:
        existing = await find(db, org_id, sha256=sha256, kind=kind)
        if existing is None:
            raise  # unrelated constraint failure; do not manufacture a replay
        if filename:
            existing.filename = filename
        if mime:
            existing.mime = mime
        return existing
    return doc


async def list_for_org(db: AsyncSession, org_id: str, *, kind: str | None = None) -> list[Document]:
    stmt = select(Document).where(Document.org_id == org_id)
    if kind:
        stmt = stmt.where(Document.kind == kind)
    return list(await db.scalars(stmt.order_by(Document.created_at.desc())))


async def page_for_org(
    db: AsyncSession,
    org_id: str,
    *,
    kind: str | None,
    limit: int,
    offset: int,
) -> tuple[list[Document], int, list[str]]:
    """Bound registry rows without truncating totals or the tenant's kind choices.

    The legacy list remains compatible. This reads metadata only, never source
    bytes, and does not flush, commit or mutate registry/financial records.
    Counts and pages describe current reads, not a frozen export snapshot.
    """
    stmt = select(Document).where(Document.org_id == org_id)
    if kind:
        stmt = stmt.where(Document.kind == kind)
    total = (
        await db.scalar(
            select(func.count()).select_from(stmt.subquery()).execution_options(autoflush=False)
        )
        or 0
    )
    rows = list(
        await db.scalars(
            stmt.order_by(Document.created_at.desc(), Document.id.desc())
            .limit(limit)
            .offset(offset)
            .execution_options(autoflush=False)
        )
    )
    # Facets cover the entire tenant, not just a page or the selected kind.
    kinds = list(
        await db.scalars(
            select(Document.kind)
            .where(Document.org_id == org_id)
            .distinct()
            .order_by(Document.kind)
            .execution_options(autoflush=False)
        )
    )
    return rows, total, kinds


async def find(db: AsyncSession, org_id: str, *, sha256: str, kind: str) -> Document | None:
    """The registry row for one stored object of this tenant — None when the
    bytes were never vaulted under that kind (WO-AF: a statement digested
    before vaulting existed). Tenant-scoped, so a foreign sha is simply absent."""
    return await db.scalar(
        select(Document).where(
            Document.org_id == org_id, Document.sha256 == sha256, Document.kind == kind
        )
    )


async def admitted_size(
    db: AsyncSession, org_id: str, *, sha256: str | None, kind: str
) -> int | None:
    """Fresh admission metadata without a cached ORM row or transaction changes.

    Missing historical registration stays None; zero and negative values remain
    visible for the caller's existing empty/integrity guards. This never flushes,
    commits or locks another service's document row.
    """
    return await db.scalar(
        select(Document.size)
        .where(Document.org_id == org_id, Document.sha256 == sha256, Document.kind == kind)
        .execution_options(autoflush=False)
    )


async def vaulted(db: AsyncSession, org_id: str, *, kind: str, shas: set[str]) -> set[str]:
    """Which of `shas` have bytes on file under `kind` for this tenant — one
    query for a whole worklist, so a screen can offer a download only where
    one can be served."""
    if not shas:
        return set()
    rows = await db.scalars(
        select(Document.sha256).where(
            Document.org_id == org_id, Document.kind == kind, Document.sha256.in_(shas)
        )
    )
    return set(rows)
