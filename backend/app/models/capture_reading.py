"""Published, source-owned facts about one terminal document reading.

The application appends these rows under the owner's publication guard. Full
text is transient; the JSON columns contain only bounded profile/page facts.
"""

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import GUID, Base, UUIDPrimaryKeyMixin, utcnow


class CaptureReadingObservation(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "capture_reading_observations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "extraction_run_id"],
            ["extraction_runs.org_id", "extraction_runs.id"],
            name="fk_capture_reading_run",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["org_id", "inbound_invoice_id"],
            ["inbound_invoices.org_id", "inbound_invoices.id"],
            name="fk_capture_reading_inbound",
            ondelete="CASCADE",
        ),
        UniqueConstraint("org_id", "id", name="uq_capture_reading_org_id"),
        CheckConstraint(
            "(extraction_run_id IS NOT NULL AND inbound_invoice_id IS NULL) OR "
            "(extraction_run_id IS NULL AND inbound_invoice_id IS NOT NULL)",
            name="ck_capture_reading_one_owner",
        ),
        CheckConstraint(
            "attempt_seq IS NULL OR attempt_seq >= 0",
            name="ck_capture_reading_attempt_nonnegative",
        ),
        CheckConstraint(
            "(extraction_run_id IS NOT NULL AND attempt_seq IS NOT NULL) OR "
            "(inbound_invoice_id IS NOT NULL AND attempt_seq IS NULL)",
            name="ck_capture_reading_owner_generation",
        ),
        CheckConstraint("outcome IN ('success', 'failure')", name="ck_capture_reading_outcome"),
        Index(
            "ix_capture_reading_org_run_history", "org_id", "extraction_run_id", "created_at", "id"
        ),
        Index(
            "ix_capture_reading_org_inbound_history",
            "org_id",
            "inbound_invoice_id",
            "created_at",
            "id",
        ),
    )

    org_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    # Publication time, with no updated_at or application update path.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), nullable=False
    )
    extraction_run_id: Mapped[str | None] = mapped_column(GUID(), nullable=True)
    inbound_invoice_id: Mapped[str | None] = mapped_column(GUID(), nullable=True)
    source_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempt_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    outcome: Mapped[str] = mapped_column(String(8), nullable=False)
    failure_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    profile_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    page_facts_json: Mapped[str | None] = mapped_column(Text, nullable=True)
