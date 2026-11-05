"""SYS08: historical policy rollback portability on disposable databases only.

The old migration intentionally discards incompatible global overrides in both
directions. An empty-schema roundtrip does not qualify live-data restoration.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url

from tests.test_migrations import _run_alembic
from tests.test_pg_migration_schema_contract import PrivateDatabaseURL, database

PREVIOUS = "a3f556aac756"
POLICY_CHANGE = "01160bebbd7f"


@pytest.fixture(params=["sqlite", "postgres"])
def policy_database_url(request, tmp_path):
    if request.param == "sqlite":
        yield f"sqlite+aiosqlite:///{tmp_path / 'policy-roundtrip.db'}"
        return
    admin_url = os.environ.get("MIGRATION_TEST_ADMIN_URL")
    app_url = os.environ.get("RLS_TEST_DATABASE_URL")
    if not admin_url or not app_url:
        pytest.skip("requires explicit disposable-database admin and application URLs")
    import asyncpg

    app = make_url(app_url)
    assert app.username and app.username.replace("_", "").isalnum()
    name = "policy_rollback_" + uuid.uuid4().hex

    async def manage(create):
        connection = await asyncpg.connect(admin_url)
        try:
            if create:
                await connection.execute(f'CREATE DATABASE "{name}" OWNER "{app.username}"')
            else:
                await connection.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        finally:
            await connection.close()

    asyncio.run(manage(True))
    try:
        yield PrivateDatabaseURL(app.set(database=name).render_as_string(hide_password=False))
    finally:
        asyncio.run(manage(False))


def _policy_shape(connection):
    columns = inspect(connection).get_columns("role_policies")
    return {
        column["name"]: (
            str(column["type"]),
            getattr(column["type"], "timezone", None),
            column["nullable"],
            column["default"],
        )
        for column in columns
    }


def _alembic(url, *args):
    result = _run_alembic(list(args), url)
    assert result.returncode == 0, result.stdout + result.stderr


def test_historical_policy_downgrade_matches_original_schema_and_empty_roundtrip(
    policy_database_url,
):
    url = policy_database_url
    _alembic(url, "upgrade", PREVIOUS)
    original = database(url, _policy_shape)

    def seed_role(connection):
        if connection.dialect.name == "postgresql":
            assert (
                connection.scalar(
                    text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user")
                )
                is False
            )
        connection.execute(
            text(
                "INSERT INTO role_policies (id, role, monthly_invoice_limit, monthly_upload_limit) "
                "VALUES (:id, 'owner', 123, 456)"
            ),
            {"id": str(uuid.uuid4())},
        )

    database(url, seed_role)
    _alembic(url, "upgrade", POLICY_CHANGE)

    def seed_plan(connection):
        assert "role_policies" not in inspect(connection).get_table_names()
        assert connection.scalar(text("SELECT count(*) FROM plan_policies")) == 0
        connection.execute(
            text(
                "INSERT INTO plan_policies (id, plan, monthly_invoice_limit, monthly_upload_limit) "
                "VALUES (:id, 'pro', 789, 987)"
            ),
            {"id": str(uuid.uuid4())},
        )

    database(url, seed_plan)
    _alembic(url, "downgrade", PREVIOUS)
    assert database(url, _policy_shape) == original

    def assert_discarded(connection):
        assert "plan_policies" not in inspect(connection).get_table_names()
        assert connection.scalar(text("SELECT count(*) FROM role_policies")) == 0

    database(url, assert_discarded)
    _alembic(url, "upgrade", "head")
    _alembic(url, "downgrade", "base")
    assert database(url, lambda c: set(inspect(c).get_table_names())) <= {"alembic_version"}
    _alembic(url, "upgrade", "head")
