"""Actor-owned committed bank-line transitions; existing cash rows are untouched."""

from __future__ import annotations

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

MAX_VERSION = 2147483647

# Freeze Python's str.strip whitespace definition for a portable nonblank check.
_WHITESPACE = " \t\n\r\v\f\x1c\x1d\x1e\x1f\u0085\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000"
_nonblank_reason = "reason"
for _space in _WHITESPACE:
    _nonblank_reason = f"replace({_nonblank_reason}, '{_space}', '')"


class ReconciliationCommand(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "reconciliation_commands"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "line_id"],
            ["bank_lines.org_id", "bank_lines.id"],
            name="fk_reconciliation_commands_line",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "org_id", "actor_user_id", "command_key", name="uq_reconciliation_command_identity"
        ),
        UniqueConstraint(
            "org_id", "line_id", "after_version", name="uq_reconciliation_command_line_version"
        ),
        Index("ix_reconciliation_commands_org_line_version", "org_id", "line_id", "after_version"),
        CheckConstraint("contract_version = 1", name="ck_reconciliation_commands_contract"),
        CheckConstraint(
            "operation IN ('match', 'ignore', 'unmatch', 'restore')",
            name="ck_reconciliation_commands_operation",
        ),
        CheckConstraint(
            "length(command_key) >= 1 AND length(command_key) <= 120 AND length(trim(command_key)) > 0",
            name="ck_reconciliation_commands_key",
        ),
        CheckConstraint(
            "length(payload_hash) = 64 AND length(result_hash) = 64",
            name="ck_reconciliation_commands_hashes",
        ),
        CheckConstraint(
            "expected_version = CAST(expected_version AS INTEGER) AND expected_version >= 1 AND expected_version <= 2147483647",
            name="ck_reconciliation_commands_expected_version",
        ),
        CheckConstraint(
            "before_version = CAST(before_version AS INTEGER) AND before_version >= 1 AND before_version <= 2147483647",
            name="ck_reconciliation_commands_before_version",
        ),
        CheckConstraint(
            "after_version = CAST(after_version AS INTEGER) AND after_version >= 1 AND after_version <= 2147483647",
            name="ck_reconciliation_commands_after_version",
        ),
        CheckConstraint(
            "expected_version = before_version AND after_version = CAST(before_version AS BIGINT) + 1",
            name="ck_reconciliation_commands_transition",
        ),
        CheckConstraint(
            "reason IS NULL OR length(reason) <= 1000", name="ck_reconciliation_commands_reason"
        ),
        CheckConstraint(
            f"operation <> 'ignore' OR (reason IS NOT NULL AND length({_nonblank_reason}) > 0)",
            name="ck_reconciliation_commands_ignore_reason",
        ),
    )

    org_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    # A global user can act in another organization through Membership. This
    # historical actor is deliberately not owned by a membership/default org.
    actor_user_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    line_id: Mapped[str] = mapped_column(GUID(), nullable=False)
    command_key: Mapped[str] = mapped_column(String(120), nullable=False)
    contract_version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    operation: Mapped[str] = mapped_column(String(16), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    result_json: Mapped[str] = mapped_column(Text, nullable=False)
    result_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    before_state_json: Mapped[str] = mapped_column(Text, nullable=False)
    after_state_json: Mapped[str] = mapped_column(Text, nullable=False)
    expected_version: Mapped[int] = mapped_column(Integer, nullable=False)
    before_version: Mapped[int] = mapped_column(Integer, nullable=False)
    after_version: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), nullable=False
    )
