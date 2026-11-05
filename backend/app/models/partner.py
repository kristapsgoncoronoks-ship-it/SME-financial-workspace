from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import Date, ForeignKey, ForeignKeyConstraint, Index, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import GUID, Base, TimestampMixin, UUIDPrimaryKeyMixin


class Partner(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A counterparty (customer) with a configurable pre-invoicing workflow.

    Some customers require formal paperwork signed BEFORE any invoice may be
    issued: a framework contract and/or an acceptance act. The two `requires_*`
    flags encode the three presets — contract+acceptance, acceptance-only, or
    none (invoice straight away). Prerequisites are relationship-level: once the
    required documents are signed, invoicing to this partner is unlocked.

    Penalty invoicing (billing accrued late-payment interest) is opt-in per
    partner and, per policy, may only be generated once a contract is signed.
    """

    __tablename__ = "partners"
    __table_args__ = (
        Index("ix_partners_org_name", "org_id", "name"),
        Index("uq_partners_org_id", "org_id", "id", unique=True),
    )

    org_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    vat_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    address_line1: Mapped[str | None] = mapped_column(String(200), nullable=True)
    city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    postal_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    country: Mapped[str | None] = mapped_column(String(2), nullable=True)

    # Pre-invoicing workflow. A signed acceptance act may itself require a signed
    # contract first (contract → acceptance → invoice) when both flags are set.
    requires_contract: Mapped[bool] = mapped_column(default=False, nullable=False)
    requires_acceptance: Mapped[bool] = mapped_column(default=False, nullable=False)

    # Penalty invoicing: opt-in, and gated on a signed contract at generation time.
    penalty_enabled: Mapped[bool] = mapped_column(default=False, nullable=False)
    penalty_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)  # % p.a.

    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)

    documents: Mapped[list[PartnerDocument]] = relationship(
        back_populates="partner",
        cascade="all, delete-orphan",
        order_by="PartnerDocument.created_at",
    )


class PartnerDocument(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A formal document in a partner's workflow — a contract or an acceptance act.

    Signing is tracked as a state transition (draft → signed) with a signer and
    date; the gate checks for a SIGNED document of each required kind. (A PDF/file
    can be attached in a later iteration; the state is what gates invoicing.)
    """

    __tablename__ = "partner_documents"
    __table_args__ = (
        Index("ix_partner_docs_partner", "partner_id", "kind"),
        ForeignKeyConstraint(
            ["org_id", "partner_id"],
            ["partners.org_id", "partners.id"],
            name="fk_partner_documents_partner",
            ondelete="CASCADE",
        ),
    )

    org_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    partner_id: Mapped[str] = mapped_column(GUID(), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # contract | acceptance_act
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    reference: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(
        String(12), default="draft", nullable=False
    )  # draft | signed
    signed_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    signed_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    partner: Mapped[Partner] = relationship(back_populates="documents")
