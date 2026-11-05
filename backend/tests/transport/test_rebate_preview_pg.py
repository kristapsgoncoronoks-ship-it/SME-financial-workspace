"""Native RLS/API qualification of bounded, no-write rebate planning."""

from __future__ import annotations

import json
import os
from decimal import Decimal
from time import perf_counter

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.database import get_session
from app.main import app
from app.services.transport import rebate
from tests.transport.conftest import make_entity
from tests.transport.test_rebate_preview import (
    PERIOD,
    PREVIEW,
    assert_approved_frozen_claim_and_history_survive,
    assert_preview_pages,
    preview_state,
    seed_preview,
)
from tests.transport.test_rebate_preview import (
    test_preview_each_source_read_is_bounded_and_refuses_whole_plan as check_source_bounds,
)
from tests.transport.test_rebate_preview import (
    test_preview_read_permission_module_and_other_tenant_fences as check_authority,
)
from tests.transport.test_wo84_rebate_merge import _rebate, _txn
from tests.transport.test_wo84_rebate_routes import _register_org

PG_URL = os.environ.get("RLS_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not PG_URL, reason="requires migrated non-superuser PostgreSQL")


@pytest_asyncio.fixture
async def rebate_pg(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "smtp_host", None)
    engine = create_async_engine(PG_URL, pool_size=4, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    async def session():
        async with factory() as db:
            yield db

    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_session] = session
    try:
        async with factory() as db:
            assert (
                await db.scalar(
                    text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user")
                )
                is False
            )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            headers, org_id = await _register_org(client)
            async with factory() as db:
                rows = await seed_preview(db, org_id)
            yield client, factory, headers, org_id, rows
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
        await engine.dispose()


@pytest.mark.asyncio
async def test_native_preview_105_rows_exact_pages_and_zero_domain_writes(
    rebate_pg, record_property
):
    client, factory, headers, org_id, rows = rebate_pg
    before = await preview_state(factory, org_id)
    await assert_preview_pages(client, headers, rows)
    after = await preview_state(factory, org_id)
    assert before == after
    record_property("synthetic_scope_rows", 105)
    record_property("tested_page_sizes", "1,37,100")
    record_property("source_line_seq_ties_across_entities", True)
    record_property("no_financial_or_audit_or_outbox_write", True)
    artifact = os.environ.get("REBATE_PREVIEW_ARTIFACT_DIR")
    if artifact:
        from pathlib import Path

        Path(artifact).mkdir(parents=True, exist_ok=True)
        Path(artifact, "native-page-outcome.json").write_text(
            json.dumps(
                {
                    "org_id": org_id,
                    "ordered_ids": [r.id for r in rows],
                    "before": before,
                    "after": after,
                },
                indent=2,
                default=str,
            )
            + "\n"
        )


@pytest.mark.asyncio
async def test_native_close_replans_corrected_source_in_an_independent_session(
    rebate_pg, record_property
):
    client, factory, headers, org_id, _rows = rebate_pg
    before = await preview_state(factory, org_id)
    first = await client.get(PREVIEW, headers=headers, params={"period": PERIOD, "page_size": 100})
    assert first.status_code == 200 and first.json()["rebate_total_eur"] == "1.05"
    assert await preview_state(factory, org_id) == before
    # A legitimate existing recording correction, AFTER the observed preview.
    async with factory() as db:
        await _rebate(db, org_id, amount_local=Decimal("2.10"))
        await db.commit()
    corrected = await preview_state(factory, org_id)
    second = await client.get(PREVIEW, headers=headers, params={"period": PERIOD})
    assert second.json()["rebate_total_eur"] == "2.10"
    assert await preview_state(factory, org_id) == corrected
    # New close session receives its old period/supplier contract only.
    async with factory() as db:
        applied = await rebate.merge_period(db, org_id, PERIOD)
        assert applied.rebate_total_eur == Decimal("2.10") and applied.changed == 105
        assert all(a.new_net_eur_eff == Decimal("9.98") for a in applied.allocations)
        assert all(a.share_eur == Decimal("0.02") for a in applied.allocations)
        await db.commit()
    applied_state = await preview_state(factory, org_id)
    current = await client.get(PREVIEW, headers=headers, params={"period": PERIOD})
    assert current.json()["changed"] == 0 and current.json()["lines_allocated"] == 105
    assert await preview_state(factory, org_id) == applied_state
    record_property("preview_is_never_an_apply_input", True)
    record_property("independent_close_session_uses_new_source", True)


@pytest.mark.asyncio
async def test_native_tied_cent_preview_matches_independent_close_and_idempotent_rerun(rebate_pg):
    client, factory, headers, org_id, rows = rebate_pg
    async with factory() as db:
        await _rebate(db, org_id, amount_local=Decimal("1.00"))
        await db.commit()
    before = await preview_state(factory, org_id)
    projected = []
    for page in (1, 2):
        response = await client.get(
            PREVIEW, headers=headers, params={"period": PERIOD, "page": page}
        )
        assert response.status_code == 200, response.text
        assert response.json()["rebate_total_eur"] == "1.00"
        assert response.json()["lines_allocated"] == 105 and response.json()["changed"] == 100
        projected.extend(response.json()["allocations"])
    assert [a["fuel_transaction_id"] for a in projected] == [row.id for row in rows]
    assert sum(Decimal(a["share_eur"]) for a in projected) == Decimal("1.00")
    assert sum(a["share_eur"] == "0.00" for a in projected) == 5
    assert await preview_state(factory, org_id) == before
    async with factory() as db:
        applied = await rebate.merge_period(db, org_id, PERIOD)
        assert applied.changed == 100
        assert {a.fuel_transaction_id: a.share_eur for a in applied.allocations} == {
            a["fuel_transaction_id"]: Decimal(a["share_eur"]) for a in projected
        }
        await db.commit()
    applied_state = await preview_state(factory, org_id)
    async with factory() as db:
        rerun = await rebate.merge_period(db, org_id, PERIOD)
        assert rerun.changed == 0 and rerun.rebate_total_eur == Decimal("1.00")
        await db.commit()
    assert await preview_state(factory, org_id) == applied_state


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["fuel", "documents", "history", "active_pairs"])
async def test_native_source_caps_are_actual_limit_reads_and_whole_scope_refusals(
    rebate_pg, monkeypatch, source
):
    client, factory, _headers, _org_id, _rows = rebate_pg
    async with factory() as db:
        await check_source_bounds(client, db, factory, monkeypatch, source)


@pytest.mark.asyncio
async def test_native_preview_fresh_read_role_module_and_overlapping_tenant_scope(rebate_pg):
    client, factory, _headers, _org_id, _rows = rebate_pg
    async with factory() as db:
        await check_authority(client, db, factory)


@pytest.mark.asyncio
async def test_native_preview_and_close_preserve_approved_frozen_claims_and_history(rebate_pg):
    client, factory, _headers, _org_id, _rows = rebate_pg
    async with factory() as db:
        await assert_approved_frozen_claim_and_history_survive(client, db, factory)


@pytest.mark.asyncio
@pytest.mark.parametrize("count,groups", [(105, 1), (1005, 8)])
async def test_native_preview_source_query_count_stays_bounded_across_groups(
    rebate_pg, count, groups, record_property
):
    client, factory, _headers, _org_id, _rows = rebate_pg
    headers, org_id = await _register_org(client)
    async with factory() as db:
        await seed_preview(db, org_id, count=count, groups=groups)
        entity = await make_entity(db, org_id, country="EE", seed=1020, documents=False)
        await _txn(
            db,
            org_id,
            entity.id,
            line_seq=1,
            qty=Decimal("1"),
            net_eur=Decimal("10"),
            supplier="GAP",
            country="EE",
        )
        await _rebate(
            db, org_id, amount_local=Decimal("1"), supplier="GAP", country="EE", period="2026-04"
        )
        await db.commit()
    before = await preview_state(factory, org_id)
    selects = []

    def observe(_connection, _cursor, statement, *_args):
        if statement.lstrip().upper().startswith("SELECT") and any(
            "FROM " + name in statement for name in ("fuel_transactions", "vat_off_invoice_rebates")
        ):
            selects.append(statement)

    engine = factory.kw["bind"]
    event.listen(engine.sync_engine, "before_cursor_execute", observe)
    started = perf_counter()
    try:
        response = await client.get(PREVIEW, headers=headers, params={"period": PERIOD})
    finally:
        elapsed = perf_counter() - started
        event.remove(engine.sync_engine, "before_cursor_execute", observe)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["lines_allocated"] == count and body["changed"] == count
    assert Decimal(body["rebate_total_eur"]) == Decimal(count) / Decimal("100")
    assert len(body["allocations"]) == 100 and len(body["source_warnings"]) == 1
    assert len(selects) == 4 and all("LIMIT" in statement for statement in selects)
    assert await preview_state(factory, org_id) == before
    record_property("planned_fuel_rows", count)
    record_property("recorded_groups", groups)
    record_property("financial_source_selects", len(selects))
    record_property("local_elapsed_seconds", round(elapsed, 6))
    artifact = os.environ.get("REBATE_PREVIEW_ARTIFACT_DIR")
    if artifact:
        from pathlib import Path

        Path(artifact, f"native-resource-{count}-{groups}.json").write_text(
            json.dumps(
                {
                    "scope": "Synthetic local native PG only; not production capacity qualification",
                    "planned_fuel_rows": count,
                    "groups": groups,
                    "allocations_returned": 100,
                    "financial_source_selects": len(selects),
                    "all_sources_have_limit": True,
                    "elapsed_seconds": elapsed,
                    "durable_state_unchanged": True,
                    "sql": selects,
                },
                indent=2,
            )
            + "\n"
        )
