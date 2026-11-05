"""Additive planning receipts, schema parity and evidence-preserving rollback."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.organization import Organization
from app.models.planning_command import PlanningCommand
from app.models.user import User, UserRole
from tests.test_migrations import BACKEND_DIR, _run_alembic
from tests.test_planning_ocr_migration_integration import migration_url as _migration_url

migration_url = _migration_url

HEAD = "8d1f3a5c7e90"
PARENT = "f0b2d4e6a8c1"


def _migrate(url, *args):
    result = _run_alembic(list(args), url)
    assert result.returncode == 0, result.stderr


def _database(url, action):
    async def run():
        engine = create_async_engine(url)
        try:
            async with engine.begin() as conn:
                return await conn.run_sync(action)
        finally:
            await engine.dispose()

    return asyncio.run(run())


@pytest.mark.slow
def test_empty_planning_receipt_migration_roundtrip_and_exact_model(migration_url):
    _migrate(migration_url, "upgrade", HEAD)
    columns, constraints, indexes, foreign_keys = _database(
        migration_url,
        lambda conn: (
            inspect(conn).get_columns("planning_commands"),
            inspect(conn).get_unique_constraints("planning_commands"),
            inspect(conn).get_indexes("planning_commands"),
            inspect(conn).get_foreign_keys("planning_commands"),
        ),
    )
    assert {column["name"] for column in columns} == {
        column.name for column in PlanningCommand.__table__.columns
    }
    assert all(not column["nullable"] for column in columns)
    assert {
        column["name"]: column["type"].length
        for column in columns
        if column["name"] in {"command_key", "payload_hash"}
    } == {"command_key": 120, "payload_hash": 64}
    assert any(
        set(c["column_names"]) == {"org_id", "actor_user_id", "command_key"} for c in constraints
    )
    assert any(i["name"] == "ix_planning_commands_org_id" for i in indexes)
    assert {
        (
            tuple(key["constrained_columns"]),
            key["referred_table"],
            tuple(key["referred_columns"]),
            key["options"]["ondelete"],
        )
        for key in foreign_keys
    } == {
        (("org_id",), "organizations", ("id",), "CASCADE"),
        (("actor_user_id",), "users", ("id",), "RESTRICT"),
    }
    _migrate(migration_url, "downgrade", PARENT)
    assert "planning_commands" not in _database(
        migration_url, lambda conn: inspect(conn).get_table_names()
    )
    _migrate(migration_url, "upgrade", HEAD)
    assert "planning_commands" in _database(
        migration_url, lambda conn: inspect(conn).get_table_names()
    )


@pytest.mark.slow
def test_populated_planning_downgrade_refuses_without_erasing_receipt(migration_url):
    _migrate(migration_url, "upgrade", HEAD)
    org_id, actor_id, receipt_id, key = (str(uuid4()) for _ in range(4))

    async def seed():
        engine = create_async_engine(migration_url)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as db:
                db.add(Organization(id=org_id, name="Synthetic planning migration"))
                await db.flush()
                db.add(
                    User(
                        id=actor_id,
                        org_id=org_id,
                        email=f"{actor_id}@example.com",
                        name="Synthetic planner",
                        hashed_password="fixture-only",
                        role=UserRole.owner,
                    )
                )
                await db.flush()
                db.add(
                    PlanningCommand(
                        id=receipt_id,
                        org_id=org_id,
                        actor_user_id=actor_id,
                        command_key=key,
                        payload_hash="0" * 64,
                        payload_json='{"synthetic":true}',
                        assignment_id=str(uuid4()),
                        result_json='{"synthetic":true}',
                    )
                )
                await db.commit()
        finally:
            await engine.dispose()

    asyncio.run(seed())
    # Select all rows through the explicit migration connection; the downgrade
    # must not depend on an application actor or an extant assignment target.
    before = _database(
        migration_url, lambda conn: list(conn.execute(select(PlanningCommand.__table__)).mappings())
    )
    if migration_url.startswith("postgresql"):

        def inherited_tenant_downgrade(conn):
            assert (
                conn.scalar(
                    text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user")
                )
                is False
            )
            conn.execute(
                text("SELECT set_config('app.current_org', :org, true)"),
                {"org": str(uuid4())},
            )
            assert conn.scalar(text("SELECT count(*) FROM planning_commands")) == 0
            config = Config()
            config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
            revision = ScriptDirectory.from_config(config).get_revision(HEAD).module
            with Operations.context(MigrationContext.configure(conn)):
                with pytest.raises(RuntimeError, match="Planning receipt retention refusal"):
                    revision.downgrade()
            assert conn.scalar(text("SELECT count(*) FROM planning_commands")) == 1

        _database(migration_url, inherited_tenant_downgrade)
    refused = _run_alembic(["downgrade", PARENT], migration_url)
    assert refused.returncode != 0
    assert "Planning receipt retention refusal" in refused.stderr
    after = _database(
        migration_url, lambda conn: list(conn.execute(select(PlanningCommand.__table__)).mappings())
    )
    assert after == before
    assert (
        _database(
            migration_url, lambda conn: conn.scalar(text("SELECT version_num FROM alembic_version"))
        )
        == HEAD
    )
    _migrate(migration_url, "upgrade", HEAD)
    assert (
        _database(
            migration_url,
            lambda conn: list(conn.execute(select(PlanningCommand.__table__)).mappings()),
        )
        == before
    )
