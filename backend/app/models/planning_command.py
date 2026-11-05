"""Actor-scoped planning creation receipts; never a financial command alias."""

from __future__ import annotations

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import GUID, Base, TimestampMixin, UUIDPrimaryKeyMixin


class PlanningCommand(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "planning_commands"
    __table_args__ = (
        UniqueConstraint(
            "org_id", "actor_user_id", "command_key", name="uq_planning_command_identity"
        ),
    )

    org_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    actor_user_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    command_key: Mapped[str] = mapped_column(String(120), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    # Snapshot identity, deliberately no FK changing project-deletion policy.
    assignment_id: Mapped[str] = mapped_column(GUID(), nullable=False)
    result_json: Mapped[str] = mapped_column(Text, nullable=False)
