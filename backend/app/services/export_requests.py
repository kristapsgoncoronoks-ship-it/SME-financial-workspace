"""The identical queued-request writes shared by the two export services."""

from __future__ import annotations

from typing import TypeVar

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.archive_export import ArchiveExport
from app.models.workspace_export import WorkspaceExport
from app.services import audit, jobs

_ExportRequest = TypeVar("_ExportRequest", ArchiveExport, WorkspaceExport)


async def enqueue_request(
    db: AsyncSession,
    model: type[_ExportRequest],
    org_id: str,
    *,
    email: str,
    user_id: str,
    job_kind: str,
    audit_action: str,
    target_type: str,
) -> _ExportRequest:
    """Append an admitted request, its queued job and the existing request audit.

    Each service retains its admission, normalization and coalescing rules and
    supplies its fixed model/job/audit vocabulary. The caller owns the transaction;
    an audit returning None keeps the existing best-effort behavior.
    """
    row = model(org_id=org_id, requested_email=email, requested_by_user_id=user_id, status="queued")
    db.add(row)
    await db.flush()
    job = await jobs.enqueue(db, job_kind, {"export_id": row.id}, org_id=org_id, commit=False)
    row.job_id = job.id
    await audit.record(
        db,
        audit_action,
        target_type=target_type,
        target_id=row.id,
        meta={"to": email},
        org_id=org_id,
        actor=(user_id, email),
    )
    return row
