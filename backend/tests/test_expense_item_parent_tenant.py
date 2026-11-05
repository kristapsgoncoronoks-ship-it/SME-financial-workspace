"""The mandatory expense item/report link preserves its tenant at the database."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.core import tenant
from app.models.expense import ExpenseItem, ExpenseReport, ExpenseTransaction
from app.models.organization import Organization
from app.models.user import User


async def _parents(db):
    organizations = [Organization(name=f"Synthetic item parent {n}") for n in (1, 2)]
    db.add_all(organizations)
    await db.flush()
    users = [
        User(
            org_id=org.id,
            email=f"item-parent-{uuid4()}@example.com",
            name="Synthetic claimant",
            hashed_password="synthetic-not-a-login-hash",
        )
        for org in organizations
    ]
    db.add_all(users)
    await db.flush()
    reports = [
        ExpenseReport(
            org_id=org.id,
            employee_id=user.id,
            employee_name=user.name,
            title="Synthetic parent report",
            status="draft",
        )
        for org, user in zip(organizations, users, strict=True)
    ]
    db.add_all(reports)
    await db.commit()
    return organizations, users, reports


def _item_values(org_id, report_id):
    return {
        "id": str(uuid4()),
        "org_id": org_id,
        "report_id": report_id,
        "spend_date": date(2026, 5, 1),
        "category": "meals",
        "description": "Synthetic receipt entry",
        "amount": Decimal("42.00"),
        "vat_amount": Decimal("0.00"),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["insert", "update"])
async def test_explicit_cross_tenant_parent_pair_is_refused_by_database(db_session, operation):
    organizations, _, reports = await _parents(db_session)
    own = _item_values(organizations[0].id, reports[0].id)
    await db_session.execute(ExpenseItem.__table__.insert().values(**own))
    await db_session.commit()
    # Core/direct-ID writes deliberately bypass relationship synchronization and
    # the before_insert denormalization hook: the database must enforce the pair.
    with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
        async with db_session.begin_nested():
            if operation == "insert":
                await db_session.execute(
                    ExpenseItem.__table__.insert().values(
                        **_item_values(organizations[0].id, reports[1].id)
                    )
                )
            else:
                await db_session.execute(
                    ExpenseItem.__table__.update()
                    .where(ExpenseItem.__table__.c.id == own["id"])
                    .values(report_id=reports[1].id)
                )


@pytest.mark.asyncio
async def test_changing_only_item_org_cannot_retain_a_foreign_parent(db_session):
    organizations, _, reports = await _parents(db_session)
    own = _item_values(organizations[0].id, reports[0].id)
    await db_session.execute(ExpenseItem.__table__.insert().values(**own))
    await db_session.commit()
    with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
        async with db_session.begin_nested():
            await db_session.execute(
                ExpenseItem.__table__.update()
                .where(ExpenseItem.__table__.c.id == own["id"])
                .values(org_id=organizations[1].id)
            )


@pytest.mark.asyncio
async def test_orm_append_and_scoped_relationship_roundtrip_preserve_parent_tenant(db_session, _db):
    organizations, _, reports = await _parents(db_session)
    await db_session.refresh(reports[0], attribute_names=["items"])
    item = ExpenseItem(
        spend_date=date(2026, 5, 1),
        category="meals",
        description="Synthetic ORM child",
        amount=Decimal("42.00"),
        vat_amount=Decimal("0.00"),
    )
    reports[0].items.append(item)
    await db_session.commit()
    org_id, report_id, item_id = organizations[0].id, reports[0].id, item.id
    assert (item.org_id, item.report_id) == (org_id, report_id)
    assert "expense_reports.org_id = expense_items.org_id" in str(
        inspect(ExpenseReport).relationships["items"].primaryjoin
    )
    scope = tenant.set_current_org(org_id)
    try:
        async with _db() as reader:
            report = await reader.scalar(
                select(ExpenseReport)
                .where(ExpenseReport.id == report_id)
                .options(selectinload(ExpenseReport.items))
            )
            assert [(entry.id, entry.org_id) for entry in report.items] == [(item_id, org_id)]
            loaded = await reader.scalar(
                select(ExpenseItem)
                .where(ExpenseItem.id == item_id)
                .options(selectinload(ExpenseItem.report))
            )
            assert (loaded.report.id, loaded.report.org_id) == (report_id, org_id)
    finally:
        tenant.reset_current_org(scope)


@pytest.mark.asyncio
@pytest.mark.parametrize("deletion", ["core", "orm"])
async def test_report_cascade_preserves_other_tenant_and_optional_transaction_link(
    db_session, deletion
):
    organizations, users, reports = await _parents(db_session)
    own = _item_values(organizations[0].id, reports[0].id)
    other = _item_values(organizations[1].id, reports[1].id)
    await db_session.execute(ExpenseItem.__table__.insert(), [own, other])
    transaction = ExpenseTransaction(
        org_id=organizations[0].id,
        employee_id=users[0].id,
        txn_date=date(2026, 5, 1),
        description="Synthetic retained card transaction",
        amount=Decimal("42.00"),
        currency="EUR",
        source="card",
        status="assigned",
        item_id=own["id"],
    )
    db_session.add(transaction)
    await db_session.commit()
    org_id, user_id, other_report = organizations[0].id, users[0].id, reports[1].id
    if deletion == "orm":
        await db_session.delete(reports[0])
    else:
        await db_session.execute(
            ExpenseReport.__table__.delete().where(ExpenseReport.__table__.c.id == reports[0].id)
        )
    await db_session.commit()
    await db_session.refresh(transaction)
    assert (
        transaction.org_id,
        transaction.employee_id,
        transaction.item_id,
        transaction.status,
    ) == (
        org_id,
        user_id,
        None,
        "assigned",
    )
    remaining = (
        await db_session.execute(
            select(ExpenseItem.__table__.c.id, ExpenseItem.__table__.c.report_id)
        )
    ).all()
    assert remaining == [(other["id"], other_report)]
    assert (
        await db_session.scalar(
            select(ExpenseReport.__table__.c.id).where(ExpenseReport.__table__.c.id == other_report)
        )
        == other_report
    )
