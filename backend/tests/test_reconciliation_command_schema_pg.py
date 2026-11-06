"""Current-head native command storage honors global actor identity and FORCE RLS."""

from __future__ import annotations

from datetime import timedelta

import pytest
import pytest_asyncio
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from tests.test_migrations import BACKEND_DIR
from tests.test_reconciliation_command_migration import _rows
from tests.test_reconciliation_command_migration_pg import _owned_database, _scope
from tests.test_reconciliation_command_schema import _insert_command


@pytest.fixture(scope="module")
def current_database():
    config = Config()
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    head = ScriptDirectory.from_config(config).get_current_head()
    assert head is not None
    with _owned_database(head, historical=False) as family:
        yield family


@pytest_asyncio.fixture
async def current_family(current_database):
    url, ids, _, _ = current_database
    engine = create_async_engine(url)
    try:
        async with engine.connect() as db:
            transaction = await db.begin()
            await _scope(db, "")
            before = await db.run_sync(_rows)
            try:
                yield db, ids
            finally:
                await transaction.rollback()
            await _scope(db, "")
            assert await db.run_sync(_rows) == before
            await db.rollback()
    finally:
        await engine.dispose()


async def test_current_table_has_global_actor_tenant_pair_and_actual_force_rls(current_family):
    db, (a, b) = current_family
    assert (
        await db.execute(
            text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user")
        )
    ).one() == (False, False)
    assert (
        await db.execute(
            text(
                "SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid='reconciliation_commands'::regclass"
            )
        )
    ).one() == (True, True)
    policy = (
        await db.execute(
            text(
                "SELECT qual,with_check FROM pg_policies WHERE tablename='reconciliation_commands' AND policyname='tenant_isolation'"
            )
        )
    ).one()
    assert "app.current_org" in policy.qual and "app.current_org" in policy.with_check
    # The global actor's default company is A; this is a storage FK test, not
    # a substitute for the service's live B Membership/session admission.
    await _scope(db, b["org"])
    command = await db.run_sync(lambda c: _insert_command(c, b, actor=a["user"]))
    row = (
        await db.execute(
            text(
                "SELECT org_id::text,actor_user_id::text,recorded_at FROM reconciliation_commands WHERE id=:id"
            ),
            {"id": command},
        )
    ).one()
    assert row.org_id == b["org"] and row.actor_user_id == a["user"]
    assert row.recorded_at.utcoffset() == timedelta(0)
    await _scope(db, a["org"])
    assert await db.scalar(text("SELECT count(*) FROM reconciliation_commands")) == 0


@pytest.mark.parametrize("operation", ["insert", "update"])
async def test_current_force_rls_refuses_foreign_owned_correct_pair(current_family, operation):
    db, (a, b) = current_family
    own = await db.run_sync(lambda c: _insert_command(c, a)) if operation == "update" else None
    await _scope(db, a["org"])
    with pytest.raises(DBAPIError) as caught:
        async with db.begin_nested():
            if operation == "insert":
                await db.run_sync(lambda c: _insert_command(c, b))
            else:
                await db.execute(
                    text(
                        "UPDATE reconciliation_commands SET org_id=:org,line_id=:line WHERE id=:id"
                    ),
                    {"org": b["org"], "line": b["line"], "id": own},
                )
    assert caught.value.orig.sqlstate == "42501"


async def test_current_pair_and_actor_restrict_cannot_be_bypassed_by_global_id(current_family):
    db, (a, b) = current_family
    with pytest.raises(IntegrityError) as caught:
        async with db.begin_nested():
            await db.run_sync(lambda c: _insert_command(c, a, line_id=b["line"]))
    assert caught.value.orig.sqlstate == "23503"
    assert caught.value.orig.__cause__.constraint_name == "fk_reconciliation_commands_line"
    await db.run_sync(lambda c: _insert_command(c, b, actor=a["user"]))
    with pytest.raises(IntegrityError) as caught:
        async with db.begin_nested():
            await db.execute(text("DELETE FROM users WHERE id=:id"), {"id": a["user"]})
    assert caught.value.orig.sqlstate == "23503"
    await db.execute(text("DELETE FROM bank_statements WHERE id=:id"), {"id": b["statement"]})
    assert await db.scalar(text("SELECT count(*) FROM reconciliation_commands")) == 0
