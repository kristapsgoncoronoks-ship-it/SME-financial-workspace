"""Additive lease identity: historical data, RLS, parity and rollback evidence."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import MetaData, Table, inspect, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.tenant import reset_current_org, set_current_org
from app.models import job as jobmodel
from app.models.job import Job
from app.services import jobs
from tests.test_migrations import BACKEND_DIR, _run_alembic
from tests.test_planning_ocr_migration_integration import (
    _insert,
    database,
    migrate,
)
from tests.test_planning_ocr_migration_integration import (
    migration_url as migration_url,
)

HEAD = "b2e4f6a8c0d3"
PARENT = "8d1f3a5c7e90"
STAMP = datetime(2026, 11, 5, 12, tzinfo=UTC)


def graph():
    config = Config()
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return ScriptDirectory.from_config(config)


def job_rows(connection):
    table = Table("jobs", MetaData(), autoload_with=connection)
    return list(connection.execute(select(table).order_by(table.c.id)).mappings())


def job_catalog(connection):
    inspector = inspect(connection)
    result = {
        "indexes": inspector.get_indexes("jobs"),
        "unique": inspector.get_unique_constraints("jobs"),
        "fks": inspector.get_foreign_keys("jobs"),
    }
    if connection.dialect.name == "postgresql":
        assert (
            connection.scalar(
                text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user")
            )
            is False
        )
        result["rls"] = list(
            connection.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid='jobs'::regclass"
                )
            )
        )
        result["policies"] = list(
            connection.execute(
                text(
                    "SELECT policyname,roles,cmd,qual,with_check FROM pg_policies "
                    "WHERE schemaname='public' AND tablename='jobs' ORDER BY policyname"
                )
            )
        )
    return result


def seed_legacy(connection):
    org_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    for index, org_id in enumerate(org_ids):
        _insert(connection, "organizations", id=org_id, name=f"Synthetic legacy lease {index}")
    for index, status in enumerate(
        (jobmodel.QUEUED, jobmodel.RUNNING, jobmodel.SUCCEEDED, jobmodel.DEAD)
    ):
        _insert(
            connection,
            "jobs",
            id=str(uuid.uuid4()),
            org_id=org_ids[index % 2],
            kind="test.lease-migration",
            payload_json='{"legacy":true}',
            idempotency_key=f"historical-{index}",
            status=status,
            attempts=index,
            max_attempts=5,
            run_after=STAMP,
            locked_at=STAMP if status == jobmodel.RUNNING else None,
            locked_by="legacy-worker" if status == jobmodel.RUNNING else None,
            last_error="historical provider evidence" if status == jobmodel.DEAD else None,
            result_json='{"historical":"result"}' if status == jobmodel.SUCCEEDED else None,
            created_at=STAMP,
            updated_at=STAMP,
        )
    return org_ids


def test_generation_migration_is_the_additive_157th_revision():
    revisions = graph()
    # This suite exercises the historical lease revision explicitly. The
    # complete graph/head is checked by test_planning_ocr_migration_integration.
    assert len(revisions.get_heads()) == 1
    assert revisions.get_revision(HEAD).down_revision == PARENT
    assert len(list(revisions.walk_revisions(head=HEAD))) == 157


@pytest.mark.slow
def test_legacy_rows_null_column_data_rls_and_downgrade_roundtrip(migration_url):
    migrate(migration_url, "upgrade", PARENT)
    database(migration_url, seed_legacy)
    before = database(migration_url, job_rows)
    before_catalog = database(migration_url, job_catalog)
    migrate(migration_url, "upgrade", HEAD)

    def upgraded(connection):
        columns = inspect(connection).get_columns("jobs")
        assert {column["name"] for column in columns} == set(Job.__table__.c.keys())
        token = next(column for column in columns if column["name"] == "lease_token")
        assert token["nullable"] is True and token["default"] is None
        assert str(token["type"]) == (
            "UUID" if connection.dialect.name == "postgresql" else "CHAR(36)"
        )
        rows = job_rows(connection)
        assert all(row["lease_token"] is None for row in rows)
        return [{key: value for key, value in row.items() if key != "lease_token"} for row in rows]

    assert database(migration_url, upgraded) == before
    assert database(migration_url, job_catalog) == before_catalog
    # Current model parity belongs at the current graph head. Later additive
    # revisions must not change this historical lease/data contract either.
    migrate(migration_url, "upgrade", "head")
    assert database(migration_url, upgraded) == before
    assert database(migration_url, job_catalog) == before_catalog
    parity = _run_alembic(["check"], migration_url)
    assert parity.returncode == 0, parity.stdout + parity.stderr
    migrate(migration_url, "downgrade", HEAD)
    migrate(migration_url, "downgrade", PARENT)
    assert database(migration_url, job_rows) == before
    assert database(migration_url, job_catalog) == before_catalog
    migrate(migration_url, "upgrade", HEAD)
    assert database(migration_url, upgraded) == before


@pytest.mark.slow
def test_committed_generation_prevents_downgrade_even_from_hidden_tenant(migration_url):
    migrate(migration_url, "upgrade", HEAD)
    org_ids = database(migration_url, seed_legacy)

    async def actual_claim_and_complete():
        engine = create_async_engine(migration_url)
        sm = async_sessionmaker(engine, expire_on_commit=False)
        token = set_current_org(org_ids[0])
        try:
            async with sm() as db:
                row = await jobs.claim(db, "actual-migrated-worker", now=STAMP)
                assert row is not None
                identity = row.lease_token
                assert str(uuid.UUID(identity)) == identity
                completed = await jobs._complete(db, row, {"actual": "completed"})
                assert completed.status == jobmodel.SUCCEEDED
                assert completed.lease_token == identity
        finally:
            reset_current_org(token)
            await engine.dispose()

    asyncio.run(actual_claim_and_complete())
    before, before_catalog = database(migration_url, job_rows), database(migration_url, job_catalog)
    if migration_url.startswith("postgresql"):

        def inherited_tenant_refusal(connection):
            connection.execute(
                text("SELECT set_config('app.current_org', :org, true)"), {"org": org_ids[1]}
            )
            assert (
                connection.scalar(text("SELECT count(*) FROM jobs WHERE lease_token IS NOT NULL"))
                == 0
            )
            with Operations.context(MigrationContext.configure(connection)):
                with pytest.raises(RuntimeError, match="Job lease retention refusal"):
                    graph().get_revision(HEAD).module.downgrade()
            assert (
                connection.scalar(text("SELECT count(*) FROM jobs WHERE lease_token IS NOT NULL"))
                == 1
            )

        database(migration_url, inherited_tenant_refusal)
    refused = _run_alembic(["downgrade", PARENT], migration_url)
    assert refused.returncode != 0
    assert "Job lease retention refusal" in refused.stderr
    assert database(migration_url, job_rows) == before
    assert database(migration_url, job_catalog) == before_catalog
    assert (
        database(migration_url, lambda c: c.scalar(text("SELECT version_num FROM alembic_version")))
        == HEAD
    )
    migrate(migration_url, "upgrade", HEAD)
    assert database(migration_url, job_rows) == before
