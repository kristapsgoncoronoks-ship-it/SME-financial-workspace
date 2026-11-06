"""Actual ordinary PostgreSQL report parity, bounded queries and read ownership."""

import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

from app.models.organization import Organization
from app.services import integrity
from tests.test_image_reading_metadata_pg import reading_pg as reading_pg
from tests.test_image_reading_metadata_pg import scoped
from tests.test_integrity_ledger_groups import (
    business_reads,
    commands,
    legacy_verify,
    seed_growth,
    seed_rich,
    snapshot,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("RLS_TEST_DATABASE_URL"),
        reason="requires migrated ordinary-role PostgreSQL",
    ),
]


async def role_and_RLS(db):
    role = (
        (
            await db.execute(
                text(
                    "SELECT current_user AS name,rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"
                )
            )
        )
        .mappings()
        .one()
    )
    assert not role["rolsuper"] and not role["rolbypassrls"]
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "alembic"))
    config.set_main_option("path_separator", "os")
    head = await db.scalar(text("SELECT version_num FROM alembic_version"))
    assert head == ScriptDirectory.from_config(config).get_current_head()
    rows = (
        (
            await db.execute(
                text(
                    "SELECT relname,relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid IN ('payments'::regclass,'receipts'::regclass,'issued_invoices'::regclass,'invoices'::regclass,'supplier_payments'::regclass) ORDER BY relname"
                )
            )
        )
        .mappings()
        .all()
    )
    assert len(rows) == 5 and all(
        row["relrowsecurity"] and row["relforcerowsecurity"] for row in rows
    )
    return {"role": dict(role), "head": head, "RLS": [dict(r) for r in rows]}


async def test_native_signed_foreign_and_binned_report_parity_is_read_only(
    reading_pg, record_property
):
    _, factory, org = reading_pg
    async with scoped(factory, org) as db:
        authority = await role_and_RLS(db)
        own = await seed_rich(db, org)
        foreign = Organization(name="Synthetic ordinary foreign groups")
        db.add(foreign)
        await db.commit()
        foreign_id = foreign.id
    async with scoped(factory, foreign_id) as db:
        other = await seed_rich(db, foreign_id)
        before_other = await snapshot(db, foreign_id)
    async with scoped(factory, org) as db:
        before = await snapshot(db, org)
        reference = await legacy_verify()(db, org)
        caller_transaction = db.sync_session.get_transaction()
        assert caller_transaction is not None
        with commands(db) as observed:
            report = await integrity.verify_ledger(db, org)
        assert db.sync_session.get_transaction() is caller_transaction
        assert asdict(report) == asdict(reference)
        assert report.checked == 18 and report.ok == 14 and len(report.issues) == 4
        assert report.issues[-1].entity_id == own["over_receipt"]
        assert not {x.entity_id for x in report.issues}.intersection(other["issued_ids"])
        assert len(business_reads(observed)) == 6
        assert all(s.lstrip().startswith("SELECT") for s in observed)
        assert db.in_transaction()
        assert await snapshot(db, org) == before
        record_property("authority", json.dumps(authority, sort_keys=True))
        record_property("own_snapshot", json.dumps(before, sort_keys=True))
        record_property(
            "report_sha256",
            hashlib.sha256(json.dumps(asdict(report), sort_keys=True).encode()).hexdigest(),
        )
    async with scoped(factory, foreign_id) as db:
        assert await snapshot(db, foreign_id) == before_other


@pytest.mark.parametrize("count", [0, 1, 64])
async def test_native_query_growth_includes_required_tenant_setup(
    reading_pg, count, record_property
):
    _, factory, org = reading_pg
    async with scoped(factory, org) as db:
        await role_and_RLS(db)
        await seed_growth(db, org, count)
        before = await snapshot(db, org)
        await db.rollback()
        with commands(db) as observed:
            report = await integrity.verify_ledger(db, org)
        assert report.healthy and report.checked == 3 * count
        assert len(business_reads(observed)) <= 6
        assert len(observed) <= 7
        assert sum("set_config(" in s for s in observed) == 1
        assert all(s.lstrip().startswith("SELECT") for s in observed)
        assert db.in_transaction()
        assert await snapshot(db, org) == before
        record_property("business_reads", len(business_reads(observed)))
        record_property("all_cursor_calls", len(observed))
