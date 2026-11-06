"""Pinned165 native migration tests never reverse the current application DB."""

from __future__ import annotations

import asyncio
import os
from contextlib import contextmanager
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from tests.test_migrations import _run_alembic
from tests.test_pg_migration_schema_contract import PrivateDatabaseURL
from tests.test_planning_ocr_migration_integration import schemas
from tests.test_reconciliation_command_migration import (
    HEAD,
    PARENT,
    _apply,
    _revision,
    _rows,
    _seed,
    _without_version,
)
from tests.test_reconciliation_command_schema import _insert_command


def _database(url, action):
    async def run():
        engine = create_async_engine(url)
        try:
            async with engine.connect() as connection:
                return await connection.run_sync(action)
        finally:
            await engine.dispose()

    return asyncio.run(run())


@contextmanager
def _owned_database(revision, *, historical):
    admin_url = os.environ.get("MIGRATION_TEST_ADMIN_URL")
    app_url = os.environ.get("RLS_TEST_DATABASE_URL")
    if not admin_url or not app_url:
        pytest.skip("requires explicit owned-database admin and non-superuser app URLs")
    import asyncpg

    admin_url = PrivateDatabaseURL(admin_url)
    app = make_url(app_url)
    assert app.username and app.username.replace("_", "").isalnum()
    name = ("reconciliation165_" if historical else "reconciliation_current_") + uuid4().hex

    async def manage(create):
        admin = await asyncpg.connect(admin_url)
        try:
            await admin.execute(
                f'CREATE DATABASE "{name}" OWNER "{app.username}"'
                if create
                else f'DROP DATABASE "{name}" WITH (FORCE)'
            )
        finally:
            await admin.close()

    asyncio.run(manage(True))
    url = PrivateDatabaseURL(app.set(database=name).render_as_string(hide_password=False))
    try:

        def identity(connection):
            assert connection.scalar(text("SELECT current_database()")) == name
            assert connection.execute(
                text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user")
            ).one() == (False, False)
            assert connection.scalar(text("SELECT to_regclass('public.alembic_version') IS NULL"))

        _database(url, identity)
        result = _run_alembic(["upgrade", PARENT if historical else revision], url)
        assert result.returncode == 0, result.stdout + result.stderr
        ids = _database(url, _seed)
        before_rows, before_schema = _database(url, _rows), _database(url, schemas)
        if historical:
            assert (
                _database(url, lambda c: c.scalar(text("SELECT version_num FROM alembic_version")))
                == PARENT
            )
            result = _run_alembic(["upgrade", revision], url)
            assert result.returncode == 0, result.stdout + result.stderr
            assert _without_version(_database(url, _rows)) == before_rows
        initial_rows, initial_schema = _database(url, _rows), _database(url, schemas)

        def final(connection):
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == revision
            assert _rows(connection) == initial_rows
            assert schemas(connection) == initial_schema

        _database(url, final)
        try:
            yield url, ids, before_rows, before_schema
        finally:
            _database(url, final)
    finally:
        asyncio.run(manage(False))


@pytest.fixture(scope="module")
def historical_database():
    assert HEAD == "d1e3f5a7c9b2" and PARENT == "c0d2e4f6a8b1"
    with _owned_database(HEAD, historical=True) as family:
        yield family


async def _scope(db, org):
    await db.execute(text("SELECT set_config('app.current_org', :org, true)"), {"org": org})


@pytest_asyncio.fixture
async def historical_family(historical_database):
    url, ids, _, _ = historical_database
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
            # A historical DDL rollback restores a different SELECT * shape
            # than asyncpg may have prepared inside that transaction. Verify
            # committed residue on a fresh physical connection, not that plan.
            await db.invalidate()
            await _scope(db, "")
            assert await db.run_sync(_rows) == before
            await db.rollback()
    finally:
        await engine.dispose()


def test_actual165_cli_and_empty_rollback_preserve_native_source_security_and_marker(
    historical_database,
):
    url, _, old_rows, old_schema = historical_database
    initial = _database(url, schemas)
    for direction, target in [("downgrade", PARENT), ("upgrade", HEAD)]:
        result = _run_alembic([direction, target], url)
        assert result.returncode == 0, result.stdout + result.stderr
        assert (
            _database(url, lambda c: c.scalar(text("SELECT version_num FROM alembic_version")))
            == target
        )
        assert (
            _without_version(_database(url, _rows)) if target == HEAD else _database(url, _rows)
        ) == old_rows
        assert _database(url, schemas) == (initial if target == HEAD else old_schema)


@pytest.mark.parametrize("kind", ["state", "association"])
@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
async def test_native_global_preflight_masks_hidden_bad_history_and_restores_guc(
    historical_family, kind, direction
):
    db, (a, b) = historical_family
    if direction == "upgrade":
        await db.run_sync(_apply, "downgrade")
    await db.execute(
        text(
            "UPDATE bank_lines SET "
            + ("status='lost'" if kind == "state" else "matched_kind='receipt'")
            + " WHERE id=:id"
        ),
        {"id": b["line"]},
    )
    before, schema = await db.run_sync(_rows), await db.run_sync(schemas)
    await _scope(db, a["org"])
    with pytest.raises(
        RuntimeError, match="invalid_state=1" if kind == "state" else "invalid_association=1"
    ) as caught:
        await db.run_sync(_apply, direction)
    assert all(
        value not in str(caught.value)
        for ids in (a, b)
        for value in (ids["org"], ids["user"], ids["statement"], *ids["lines"])
    )
    assert await db.scalar(text("SELECT current_setting('app.current_org',true)")) == a["org"]
    await _scope(db, "")
    assert await db.run_sync(_rows) == before and await db.run_sync(schemas) == schema


@pytest.mark.parametrize("evidence", ["history", "version"])
async def test_hidden_receipt_or_advanced_version_refuses_destructive_downgrade_globally(
    historical_family, evidence
):
    db, (a, b) = historical_family
    if evidence == "history":
        await db.run_sync(lambda c: _insert_command(c, b, actor=a["user"]))
    else:
        await db.execute(text("UPDATE bank_lines SET version=2 WHERE id=:id"), {"id": b["line"]})
    before, schema = await db.run_sync(_rows), await db.run_sync(schemas)
    await _scope(db, a["org"])
    assert await db.scalar(text("SELECT count(*) FROM reconciliation_commands")) == 0
    with pytest.raises(RuntimeError, match="Keep additive schema"):
        await db.run_sync(_apply, "downgrade")
    assert await db.scalar(text("SELECT current_setting('app.current_org',true)")) == a["org"]
    await _scope(db, "")
    assert await db.run_sync(_rows) == before and await db.run_sync(schemas) == schema


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
async def test_native_late_failure_rolls_back_every_ddl_and_scoped_policy(
    historical_family, monkeypatch, direction
):
    db, (a, _b) = historical_family
    if direction == "upgrade":
        await db.run_sync(_apply, "downgrade")
    migration = _revision()
    target = migration._check_installed if direction == "upgrade" else migration.op.drop_column

    def fail(*args, **kwargs):
        target(*args, **kwargs)
        raise RuntimeError("Synthetic final reconciliation failure")

    monkeypatch.setattr(
        migration if direction == "upgrade" else migration.op,
        "_check_installed" if direction == "upgrade" else "drop_column",
        fail,
    )
    before, schema = await db.run_sync(_rows), await db.run_sync(schemas)
    await _scope(db, a["org"])
    with pytest.raises(RuntimeError, match="Synthetic final"):
        await db.run_sync(lambda c: _apply(c, direction, migration))
    assert await db.scalar(text("SELECT current_setting('app.current_org',true)")) == a["org"]
    await _scope(db, "")
    assert await db.run_sync(_rows) == before and await db.run_sync(schemas) == schema
