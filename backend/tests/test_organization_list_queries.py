"""BE-013: the complete account switcher costs one read, not one per company."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select

from app.api.routes.auth import my_organizations
from app.models.membership import Membership
from app.models.organization import Organization
from app.models.user import User, UserRole


@pytest.mark.asyncio
@pytest.mark.parametrize("company_count", [1, 10, 50])
async def test_switcher_reads_complete_account_population_once(
    auth_client, db_session, _db, company_count, record_property
):
    me = (await auth_client.get("/api/v1/auth/me")).json()
    uid, current_id = me["user"]["id"], me["organization"]["id"]
    origin = datetime(2026, 1, 1, tzinfo=UTC)
    original = await db_session.scalar(select(Membership).where(Membership.user_id == uid))
    original.created_at = origin
    expected = [{"org_id": current_id, "name": "Acme", "role": "owner", "current": True}]
    for n in range(1, company_count):
        org = Organization(name=f"Synthetic company {n}", status="inactive" if n == 1 else "active")
        db_session.add(org)
        await db_session.flush()
        db_session.add(
            Membership(
                org_id=org.id,
                user_id=uid,
                role=UserRole.accountant,
                status="active",
                created_at=origin + timedelta(days=n),
            )
        )
        expected.insert(
            0, {"org_id": org.id, "name": org.name, "role": "accountant", "current": False}
        )

    hidden = Organization(name="Synthetic suspended membership")
    foreign = Organization(name="Synthetic unrelated account")
    db_session.add_all([hidden, foreign])
    await db_session.flush()
    other = User(
        org_id=foreign.id,
        email="unrelated-account@example.com",
        name="Synthetic other account",
        hashed_password="synthetic-unused-hash",
        role=UserRole.owner,
    )
    db_session.add(other)
    await db_session.flush()
    db_session.add_all(
        [
            Membership(org_id=hidden.id, user_id=uid, role=UserRole.owner, status="suspended"),
            Membership(org_id=foreign.id, user_id=other.id, role=UserRole.owner, status="active"),
        ]
    )
    await db_session.commit()

    statements = []

    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    engine = db_session.bind.sync_engine
    event.listen(engine, "before_cursor_execute", observe)
    try:
        # A fresh session prevents an identity-map hit from disguising N+1.
        async with _db() as fresh:
            actual = await my_organizations(SimpleNamespace(user_id=uid, org_id=current_id), fresh)
    finally:
        event.remove(engine, "before_cursor_execute", observe)

    assert actual == expected
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    record_property("companies", company_count)
    record_property("organization_list_statements", len(statements))
    assert len(statements) == 1, f"{company_count} companies required {len(statements)} reads"

    # The real authenticated endpoint returns the same wire population and
    # preserves the standing policy that active membership can list an inactive org.
    response = await auth_client.get("/api/v1/auth/organizations")
    assert response.status_code == 200, response.text
    assert response.json() == expected


@pytest.mark.asyncio
async def test_organization_switcher_rejects_anonymous_accounts(client):
    response = await client.get("/api/v1/auth/organizations")
    assert response.status_code == 401
