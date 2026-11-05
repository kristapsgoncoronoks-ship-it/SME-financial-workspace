"""Mandatory report comments/approval steps belong to the report's tenant."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core import tenant
from app.models.expense import ExpenseComment, ExpenseItem, ExpenseReport, ExpenseTransaction
from app.models.expense_approval import ExpenseApprovalStep
from tests.test_expense_item_parent_tenant import _item_values, _parents


def _model(kind):
    return ExpenseComment if kind == "comment" else ExpenseApprovalStep


def _values(kind, org_id, report_id, user_id):
    values = {"id": str(uuid4()), "org_id": org_id, "report_id": report_id}
    if kind == "comment":
        values.update(author_id=user_id, author_name="Synthetic reviewer", body="Retained review")
    else:
        values.update(seq=0, kind="approver", status="pending")
    return values


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["comment", "step"])
@pytest.mark.parametrize("operation", ["insert", "update"])
async def test_explicit_cross_tenant_report_child_is_refused(db_session, kind, operation):
    organizations, users, reports = await _parents(db_session)
    table = _model(kind).__table__
    own = _values(kind, organizations[0].id, reports[0].id, users[0].id)
    await db_session.execute(table.insert().values(**own))
    await db_session.commit()
    with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
        async with db_session.begin_nested():
            if operation == "insert":
                await db_session.execute(
                    table.insert().values(
                        **_values(kind, organizations[0].id, reports[1].id, users[0].id)
                    )
                )
            else:
                await db_session.execute(
                    table.update().where(table.c.id == own["id"]).values(report_id=reports[1].id)
                )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["comment", "step"])
async def test_org_only_update_cannot_leave_foreign_parent(db_session, kind):
    organizations, users, reports = await _parents(db_session)
    table = _model(kind).__table__
    own = _values(kind, organizations[0].id, reports[0].id, users[0].id)
    await db_session.execute(table.insert().values(**own))
    await db_session.commit()
    with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
        async with db_session.begin_nested():
            await db_session.execute(
                table.update().where(table.c.id == own["id"]).values(org_id=organizations[1].id)
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["comment", "step"])
async def test_parent_remains_mandatory(db_session, kind):
    organizations, users, _ = await _parents(db_session)
    with pytest.raises(IntegrityError, match="NOT NULL constraint failed"):
        async with db_session.begin_nested():
            await db_session.execute(
                _model(kind)
                .__table__.insert()
                .values(**_values(kind, organizations[0].id, None, users[0].id))
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["comment", "step"])
async def test_orm_same_tenant_insert_and_scoped_reads_remain_valid(db_session, _db, kind):
    organizations, users, reports = await _parents(db_session)
    children = [
        _model(kind)(**_values(kind, org.id, report.id, user.id))
        for org, user, report in zip(organizations, users, reports, strict=True)
    ]
    db_session.add_all(children)
    await db_session.commit()
    own_id, other_id, org_id = children[0].id, children[1].id, organizations[0].id
    scope = tenant.set_current_org(org_id)
    try:
        async with _db() as reader:
            own = await reader.scalar(select(_model(kind)).where(_model(kind).id == own_id))
            assert own is not None and own.org_id == org_id
            assert (
                await reader.scalar(select(_model(kind)).where(_model(kind).id == other_id)) is None
            )
    finally:
        tenant.reset_current_org(scope)


@pytest.mark.asyncio
@pytest.mark.parametrize("deletion", ["core", "orm"])
async def test_report_delete_cascades_only_own_children_and_preserves_existing_item_links(
    db_session, deletion
):
    organizations, users, reports = await _parents(db_session)
    other_rows = {}
    for kind in ("comment", "step"):
        rows = [
            _values(kind, org.id, report.id, user.id)
            for org, user, report in zip(organizations, users, reports, strict=True)
        ]
        await db_session.execute(_model(kind).__table__.insert(), rows)
        other_rows[kind] = rows[1]
    own_item = _item_values(organizations[0].id, reports[0].id)
    other_item = _item_values(organizations[1].id, reports[1].id)
    await db_session.execute(ExpenseItem.__table__.insert(), [own_item, other_item])
    transaction = ExpenseTransaction(
        org_id=organizations[0].id,
        employee_id=users[0].id,
        txn_date=date(2026, 5, 1),
        description="Synthetic retained transaction",
        amount=Decimal("42.00"),
        currency="EUR",
        source="card",
        status="assigned",
        item_id=own_item["id"],
    )
    db_session.add(transaction)
    await db_session.commit()
    org_id, other_report = organizations[0].id, reports[1].id
    if deletion == "orm":
        await db_session.delete(reports[0])
    else:
        await db_session.execute(
            ExpenseReport.__table__.delete().where(ExpenseReport.__table__.c.id == reports[0].id)
        )
    await db_session.commit()
    await db_session.refresh(transaction)
    assert transaction.item_id is None and transaction.org_id == org_id
    assert transaction.status == "assigned"
    for kind in ("comment", "step"):
        remaining = (await db_session.execute(select(_model(kind).__table__))).mappings().all()
        assert len(remaining) == 1
        assert all(remaining[0][key] == value for key, value in other_rows[kind].items())
    assert (
        await db_session.execute(
            select(ExpenseItem.__table__.c.id, ExpenseItem.__table__.c.report_id)
        )
    ).all() == [(other_item["id"], other_report)]
