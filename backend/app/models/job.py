from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import GUID, Base, TimestampMixin, UUIDPrimaryKeyMixin, utcnow

# Job lifecycle.
QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"  # a transient failure that will be retried
DEAD = "dead"  # exhausted retries — the dead-letter state
TERMINAL = (SUCCEEDED, DEAD)


class Job(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A durable, tenant-scoped background job.

    Rows ARE the queue: a worker atomically claims a `queued` job whose
    `run_after` has arrived, runs the registered handler, and marks it
    succeeded — or, on error, schedules a retry with exponential backoff and
    finally moves it to `dead` (dead-letter). An optional `idempotency_key`
    dedupes enqueues so the same unit of work is never queued twice.
    """

    __tablename__ = "jobs"
    __table_args__ = (
        # The worker's claim query: oldest ready job of any tenant.
        Index("ix_jobs_ready", "status", "run_after"),
        # Dedupe: at most one live job per (tenant, kind, idempotency_key).
        UniqueConstraint("org_id", "kind", "idempotency_key", name="uq_jobs_idempotency"),
    )

    org_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    idempotency_key: Mapped[str | None] = mapped_column(String(120), nullable=True)

    status: Mapped[str] = mapped_column(String(12), default=QUEUED, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5, nullable=False)

    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Fresh per claim, retained as evidence after completion/reclaim. NULL is
    # valid for rows created before the additive lease-fencing migration.
    lease_token: Mapped[str | None] = mapped_column(GUID(), nullable=True)

    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
