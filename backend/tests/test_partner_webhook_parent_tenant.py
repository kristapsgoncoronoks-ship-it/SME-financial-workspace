"""Mandatory partner/document and endpoint/delivery pairs retain tenant and CASCADE."""

from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core import tenant
from app.models import Base
from app.models.organization import Organization
from app.models.partner import Partner, PartnerDocument
from app.models.webhook import WebhookDelivery, WebhookEndpoint


def _link(kind):
    if kind == "partner":
        return Partner, PartnerDocument, "partner_id"
    return WebhookEndpoint, WebhookDelivery, "endpoint_id"


def _child_values(kind, org_id, parent_id):
    _, _, column = _link(kind)
    values = {"id": str(uuid4()), "org_id": org_id, column: parent_id}
    if kind == "partner":
        values.update(kind="contract", title="Retained signed prerequisite", status="signed")
    else:
        values.update(event_type="ping", payload_json='{"synthetic":true}', status="pending")
    return values


async def _parents(db_session, kind):
    parent, _, _ = _link(kind)
    organizations = [Organization(name="Synthetic parent " + letter) for letter in "AB"]
    db_session.add_all(organizations)
    await db_session.flush()
    parents = []
    for org in organizations:
        values = (
            {"name": "Synthetic partner"}
            if kind == "partner"
            else {"url": "https://example.invalid/never-called", "secret": "synthetic-never-used"}
        )
        parents.append(parent(org_id=org.id, **values))
    db_session.add_all(parents)
    await db_session.commit()
    return organizations, parents


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["partner", "webhook"])
@pytest.mark.parametrize("operation", ["insert", "parent_link", "child_org", "parent_org"])
async def test_cross_tenant_parent_pairs_are_refused(db_session, kind, operation):
    organizations, parents = await _parents(db_session, kind)
    parent, child, column = _link(kind)
    own = _child_values(kind, organizations[0].id, parents[0].id)
    await db_session.execute(child.__table__.insert().values(**own))
    await db_session.commit()
    with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
        async with db_session.begin_nested():
            if operation == "insert":
                statement = child.__table__.insert().values(
                    **_child_values(kind, organizations[0].id, parents[1].id)
                )
            elif operation == "parent_link":
                statement = (
                    child.__table__.update()
                    .where(child.id == own["id"])
                    .values(**{column: parents[1].id})
                )
            elif operation == "child_org":
                statement = (
                    child.__table__.update()
                    .where(child.id == own["id"])
                    .values(org_id=organizations[1].id)
                )
            else:
                statement = (
                    parent.__table__.update()
                    .where(parent.id == parents[0].id)
                    .values(org_id=organizations[1].id)
                )
            await db_session.execute(statement)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["partner", "webhook"])
async def test_parent_link_stays_mandatory(db_session, kind):
    organizations, _ = await _parents(db_session, kind)
    with pytest.raises(IntegrityError, match="NOT NULL constraint failed"):
        async with db_session.begin_nested():
            await db_session.execute(
                _link(kind)[1]
                .__table__.insert()
                .values(**_child_values(kind, organizations[0].id, None))
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["partner", "webhook"])
async def test_same_tenant_orm_insert_and_scoped_read_remain_valid(db_session, _db, kind):
    organizations, parents = await _parents(db_session, kind)
    _, child, _ = _link(kind)
    children = [
        child(**_child_values(kind, org.id, parent.id))
        for org, parent in zip(organizations, parents, strict=True)
    ]
    db_session.add_all(children)
    await db_session.commit()
    own_id, foreign_id, org_id = children[0].id, children[1].id, organizations[0].id
    token = tenant.set_current_org(org_id)
    try:
        async with _db() as reader:
            own = await reader.scalar(select(child).where(child.id == own_id))
            assert own is not None and own.org_id == org_id
            assert await reader.scalar(select(child).where(child.id == foreign_id)) is None
    finally:
        tenant.reset_current_org(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["partner", "webhook"])
@pytest.mark.parametrize("deletion", ["core", "orm"])
async def test_parent_delete_cascades_only_own_children(db_session, kind, deletion):
    organizations, parents = await _parents(db_session, kind)
    parent, child, _ = _link(kind)
    values = [
        _child_values(kind, org.id, parent.id)
        for org, parent in zip(organizations, parents, strict=True)
    ]
    await db_session.execute(child.__table__.insert(), values)
    await db_session.commit()
    before = [
        dict(row)
        for row in (
            await db_session.execute(select(child.__table__).where(child.id == values[1]["id"]))
        ).mappings()
    ]
    if deletion == "orm":
        await db_session.delete(parents[0])
    else:
        await db_session.execute(parent.__table__.delete().where(parent.id == parents[0].id))
    await db_session.commit()
    after = [dict(row) for row in (await db_session.execute(select(child.__table__))).mappings()]
    assert after == before


def test_four_dated_optional_links_keep_original_set_null_contracts():
    for table, column, target in (
        ("expense_transactions", "item_id", "expense_items"),
        ("issued_invoices", "partner_id", "partners"),
        ("recurring_invoices", "partner_id", "partners"),
        ("vendor_change_requests", "source_document_id", "documents"),
    ):
        model = Base.metadata.tables[table]
        links = [fk for fk in model.foreign_key_constraints if list(fk.column_keys) == [column]]
        assert len(links) == 1 and links[0].referred_table.name == target
        assert links[0].ondelete == "SET NULL" and model.c[column].nullable
