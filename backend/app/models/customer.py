"""Sales customer master — the billing counterparty for ISSUED invoices.

Distinct from `Vendor` (AP/supplier master) and `Partner` (the pre-invoicing
workflow counterparty). A Customer holds reusable billing + shipping details, tax
identifiers, payment terms and a default currency, so an invoice can be raised
from it without re-typing the buyer block. The buyer details are still SNAPSHOTTED
onto each issued invoice at issue time (the customer record may change later).
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import GUID, Base, TimestampMixin, UUIDPrimaryKeyMixin


class Customer(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "customers"
    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_customers_org_id"),
        Index("ix_customers_org_active", "org_id", "is_active"),
        CheckConstraint(
            "lifecycle IN ('prospect', 'active', 'dormant', 'lost')",
            name="ck_customers_lifecycle",
        ),
    )

    org_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    legal_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    vat_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    registration_number: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Primary contact (a fuller list lives in CustomerContact).
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # CRM light (WO-H): the relationship stage as a COLUMN — prospect|active|
    # dormant|lost. Deliberately NOT a lead entity (documented anti-pattern:
    # the hard lead→customer conversion step manufactures duplicates).
    lifecycle: Mapped[str] = mapped_column(
        String(16), default="active", server_default="active", nullable=False
    )

    # Billing address (appears on the invoice).
    address_line1: Mapped[str | None] = mapped_column(String(200), nullable=True)
    address_line2: Mapped[str | None] = mapped_column(String(200), nullable=True)
    city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    postal_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    country: Mapped[str | None] = mapped_column(String(2), nullable=True)

    # Shipping / delivery address (nullable ⇒ same as billing).
    ship_address_line1: Mapped[str | None] = mapped_column(String(200), nullable=True)
    ship_address_line2: Mapped[str | None] = mapped_column(String(200), nullable=True)
    ship_city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    ship_postal_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    ship_country: Mapped[str | None] = mapped_column(String(2), nullable=True)

    # Billing defaults applied when raising an invoice for this customer.
    payment_terms_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    default_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    contacts: Mapped[list[CustomerContact]] = relationship(
        back_populates="customer",
        cascade="all, delete-orphan",
        order_by="CustomerContact.created_at",
    )


class CustomerContact(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An additional named contact for a customer (AP clerk, buyer, etc.)."""

    __tablename__ = "customer_contacts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "customer_id"],
            ["customers.org_id", "customers.id"],
            name="fk_customer_contacts_customer",
            ondelete="CASCADE",
        ),
        Index("ix_customer_contacts_org_customer", "org_id", "customer_id"),
    )

    org_id: Mapped[str] = mapped_column(
        GUID(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    customer_id: Mapped[str] = mapped_column(GUID(), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    role: Mapped[str | None] = mapped_column(String(80), nullable=True)

    customer: Mapped[Customer] = relationship(back_populates="contacts")
