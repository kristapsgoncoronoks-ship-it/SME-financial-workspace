"""Read-only rebate planning uses the close engine's complete Decimal scope."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import event, select

from app.models import Base
from app.services.transport import rebate
from tests.transport.conftest import make_entity
from tests.transport.test_wo84_rebate_merge import _rebate, _txn
from tests.transport.test_wo84_rebate_routes import (
    _enable_transport,
    _member_with_role,
    _register_org,
)

PREVIEW = "/api/v1/transport/rebates/preview"
PERIOD = "2026-05"


async def preview_state(factory, org_id):
    """Exact durable domain rows, including audit/outbox; auth reads may log."""
    async with factory() as db:
        return {
            name: [
                dict(row)
                for row in (
                    await db.execute(
                        select(Base.metadata.tables[name])
                        .where(Base.metadata.tables[name].c.org_id == org_id)
                        .order_by(Base.metadata.tables[name].c.id)
                    )
                ).mappings()
            ]
            for name in (
                "fuel_transactions",
                "vat_off_invoice_rebates",
                "audit_events",
                "jobs",
                "webhook_deliveries",
                "invoices",
                "issuer_profiles",
                "vat_refund_claims",
                "vat_claim_lines",
                "vat_claimed_invoices",
            )
        }


async def seed_preview(db, org_id, count=105, groups=1):
    await _enable_transport(db, org_id)
    entities = [
        await make_entity(db, org_id, country="LV", seed=50 + index, documents=False)
        for index in range(2)
    ]
    rows = [
        await _txn(
            db,
            org_id,
            entities[index % 2].id,
            line_seq=index // 2 + 1,
            qty=Decimal("1"),
            net_eur=Decimal("10"),
            supplier="Q8" if groups == 1 else f"NET{index % groups:02d}",
        )
        for index in range(count)
    ]
    for group in range(groups):
        group_count = sum(index % groups == group for index in range(count))
        await _rebate(
            db,
            org_id,
            amount_local=Decimal(group_count) / Decimal("100"),
            supplier="Q8" if groups == 1 else f"NET{group:02d}",
        )
    await db.commit()
    return sorted(rows, key=lambda row: (row.line_seq, row.id))


async def assert_preview_pages(client, headers, rows):
    ids = [row.id for row in rows]
    for size in (1, 37, 100):
        found = []
        for page in range(1, (len(rows) + size - 1) // size + 1):
            response = await client.get(
                PREVIEW,
                headers=headers,
                params={"period": PERIOD, "page": page, "page_size": size},
            )
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["lines_allocated"] == body["changed"] == len(rows)
            assert body["rebate_total_eur"] == "1.05"
            assert body["period"] == PERIOD and body["supplier"] is None
            assert body["page"] == page and body["page_size"] == size
            assert len(body["allocations"]) <= size
            found.extend(a["fuel_transaction_id"] for a in body["allocations"])
            for allocation in body["allocations"]:
                assert allocation["net_eur"] == allocation["old_net_eur_eff"] == "10.00"
                assert allocation["new_net_eur_eff"] == "9.99"
                assert allocation["share_eur"] == "0.01"
        assert found == ids and len(set(found)) == len(ids)
    beyond = await client.get(PREVIEW, headers=headers, params={"period": PERIOD, "page": 3})
    assert beyond.json()["allocations"] == []
    assert beyond.json()["lines_allocated"] == 105


@pytest.mark.asyncio
async def test_preview_is_exact_decimal_plan_without_applying(client, db_session, _db):
    headers, org_id = await _register_org(client)
    await _enable_transport(db_session, org_id)
    entity = await make_entity(db_session, org_id, country="LV", documents=False)
    line = await _txn(
        db_session, org_id, entity.id, line_seq=1, qty=Decimal("1000"), net_eur=Decimal("1350")
    )
    await _rebate(db_session, org_id, amount_local=Decimal("50"))
    await db_session.commit()
    before = await preview_state(_db, org_id)

    response = await client.get(PREVIEW, headers=headers, params={"period": PERIOD})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["rebate_total_eur"] == "50.00"
    assert body["lines_allocated"] == body["changed"] == 1
    assert body["allocations"][0]["fuel_transaction_id"] == line.id
    assert body["allocations"][0]["new_net_eur_eff"] == "1300.00"
    await db_session.refresh(line)
    assert line.net_eur_eff == Decimal("1350.00")
    assert await preview_state(_db, org_id) == before


@pytest.mark.asyncio
async def test_preview_all_pages_retain_whole_plan_and_write_nothing(client, db_session, _db):
    headers, org_id = await _register_org(client)
    rows = await seed_preview(db_session, org_id)
    before = await preview_state(_db, org_id)
    await assert_preview_pages(client, headers, rows)
    assert await preview_state(_db, org_id) == before


@pytest.mark.asyncio
async def test_preview_tied_entity_line_numbers_share_the_canonical_close_plan(db_session):
    from tests.transport.test_wo84_rebate_merge import _org

    org, first = await _org(db_session)
    entities = [first] + [
        await make_entity(db_session, org.id, country="LV", seed=index + 900, documents=False)
        for index in range(2)
    ]
    rows = [
        await _txn(
            db_session, org.id, entity.id, line_seq=1, qty=Decimal("1"), net_eur=Decimal("10")
        )
        for entity in entities
    ]
    await _rebate(db_session, org.id, amount_local=Decimal("1"))
    await db_session.commit()
    observed = await rebate.preview_period(db_session, org.id, PERIOD)
    assert [a.fuel_transaction_id for a in observed.allocations] == sorted(r.id for r in rows)
    assert [a.share_eur for a in observed.allocations] == [
        Decimal("0.33"),
        Decimal("0.34"),
        Decimal("0.33"),
    ]
    assert sum(a.share_eur for a in observed.allocations) == Decimal("1.00")
    ordered = sorted(rows, key=lambda row: row.id)
    for shuffled in (list(reversed(ordered)), ordered[1:] + ordered[:1]):
        assert tuple(rebate._allocate(shuffled, Decimal("1"))) == observed.allocations
    applied = await rebate.merge_period(db_session, org.id, PERIOD)
    assert applied == observed
    await db_session.commit()
    second = await rebate.preview_period(db_session, org.id, PERIOD)
    assert second.changed == 0 and second.lines_allocated == 3
    assert second.rebate_total_eur == Decimal("1.00")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {},
        {"period": "2026-13"},
        {"period": PERIOD, "page": 0},
        {"period": PERIOD, "page_size": 101},
        {"period": PERIOD, "page_size": 0},
    ],
)
async def test_preview_requires_valid_complete_scope_and_page_bounds(client, db_session, params):
    headers, org_id = await _register_org(client)
    await _enable_transport(db_session, org_id)
    response = await client.get(PREVIEW, headers=headers, params=params)
    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_preview_read_permission_module_and_other_tenant_fences(client, db_session, _db):
    owner, org_id = await _register_org(client)
    await seed_preview(db_session, org_id, count=1)
    auditor = await _member_with_role(client, owner, db_session, "auditor")
    employee = await _member_with_role(client, owner, db_session, "user")
    before = await preview_state(_db, org_id)
    assert (
        await client.get(PREVIEW, headers=auditor, params={"period": PERIOD})
    ).status_code == 200
    assert (
        await client.get(PREVIEW, headers=employee, params={"period": PERIOD})
    ).status_code == 403
    assert (await client.get(PREVIEW, params={"period": PERIOD})).status_code == 401
    other, other_org = await _register_org(client)
    unavailable = await client.get(PREVIEW, headers=other, params={"period": PERIOD})
    assert unavailable.status_code == 403 and unavailable.json()["code"] == "module_not_enabled"
    await _enable_transport(db_session, other_org)
    empty = await client.get(PREVIEW, headers=other, params={"period": PERIOD})
    assert empty.status_code == 200 and empty.json()["allocations"] == []
    assert empty.json()["rebate_total_eur"] == "0.00"
    assert await preview_state(_db, org_id) == before


@pytest.mark.asyncio
async def test_preview_exact_supplier_scope_retains_all_countries_and_warnings(
    client, db_session, _db
):
    headers, org_id = await _register_org(client)
    await seed_preview(db_session, org_id, count=1)
    entity = await make_entity(db_session, org_id, country="EE", seed=1001, documents=False)
    await _txn(
        db_session,
        org_id,
        entity.id,
        line_seq=1,
        qty=Decimal("1"),
        net_eur=Decimal("20"),
        supplier="OTHER",
        country="EE",
    )
    await _rebate(db_session, org_id, amount_local=Decimal("2"), supplier="OTHER", country="EE")
    await _txn(
        db_session,
        org_id,
        entity.id,
        line_seq=2,
        qty=Decimal("1"),
        net_eur=Decimal("20"),
        country="EE",
    )
    await _rebate(db_session, org_id, amount_local=Decimal("2"), country="EE", period="2026-04")
    await db_session.commit()
    before = await preview_state(_db, org_id)
    whole = await client.get(PREVIEW, headers=headers, params={"period": PERIOD})
    assert whole.json()["rebate_total_eur"] == "2.01"
    assert whole.json()["lines_allocated"] == 2
    assert len(whole.json()["source_warnings"]) == 1
    assert "Q8/EE" in whole.json()["source_warnings"][0]
    exact = await client.get(PREVIEW, headers=headers, params={"period": PERIOD, "supplier": "Q8"})
    assert exact.json()["rebate_total_eur"] == "0.01" and exact.json()["lines_allocated"] == 1
    assert exact.json()["source_warnings"] == whole.json()["source_warnings"]
    unmatched = await client.get(
        PREVIEW, headers=headers, params={"period": PERIOD, "supplier": "q8"}
    )
    assert unmatched.json()["rebate_total_eur"] == "0.00"
    assert unmatched.json()["source_warnings"] == []
    assert await preview_state(_db, org_id) == before


@pytest.mark.asyncio
async def test_preview_without_current_documents_still_bounds_and_returns_source_warnings(
    client, db_session, _db, monkeypatch
):
    headers, org_id = await _register_org(client)
    await _enable_transport(db_session, org_id)
    entity = await make_entity(db_session, org_id, country="LV", seed=1005, documents=False)
    await _txn(db_session, org_id, entity.id, line_seq=1, qty=Decimal("1"), net_eur=Decimal("10"))
    await _rebate(db_session, org_id, amount_local=Decimal("1"), period="2026-04")
    await db_session.commit()
    before = await preview_state(_db, org_id)
    # Inclusive capacity boundaries: zero current documents, one historical
    # row and one active pair are complete valid source populations.
    monkeypatch.setattr(rebate, "PREVIEW_MAX_REBATE_ROWS", 0)
    monkeypatch.setattr(rebate, "PREVIEW_MAX_HISTORY_ROWS", 1)
    monkeypatch.setattr(rebate, "PREVIEW_MAX_ACTIVE_PAIRS", 1)
    observed = await client.get(PREVIEW, headers=headers, params={"period": PERIOD})
    assert observed.status_code == 200, observed.text
    assert observed.json()["allocations"] == [] and observed.json()["rebate_total_eur"] == "0.00"
    assert observed.json()["lines_allocated"] == observed.json()["changed"] == 0
    assert len(observed.json()["source_warnings"]) == 1
    assert "Q8/LV" in observed.json()["source_warnings"][0]
    monkeypatch.setattr(rebate, "PREVIEW_MAX_HISTORY_ROWS", 0)
    refused = await client.get(PREVIEW, headers=headers, params={"period": PERIOD})
    assert refused.status_code == 413 and refused.json()["code"] == "rebate_preview_too_large"
    assert "allocations" not in refused.json()
    assert await preview_state(_db, org_id) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["missing", "zero_qty", "exceeds"])
async def test_preview_financial_refusal_never_returns_a_partial_group(
    client, db_session, _db, invalid
):
    headers, org_id = await _register_org(client)
    await seed_preview(db_session, org_id, count=1)
    entity = await make_entity(db_session, org_id, country="EE", seed=1002, documents=False)
    if invalid != "missing":
        await _txn(
            db_session,
            org_id,
            entity.id,
            line_seq=1,
            qty=Decimal("0" if invalid == "zero_qty" else "1"),
            net_eur=Decimal("1"),
            supplier="ZZZ",
            country="EE",
        )
    await _rebate(db_session, org_id, amount_local=Decimal("2"), supplier="ZZZ", country="EE")
    await db_session.commit()
    before = await preview_state(_db, org_id)
    response = await client.get(PREVIEW, headers=headers, params={"period": PERIOD})
    expected = {
        "missing": "rebate_has_no_transactions",
        "zero_qty": "rebate_no_litres_to_allocate",
        "exceeds": "rebate_exceeds_net",
    }[invalid]
    assert response.status_code == 422 and response.json()["code"] == expected
    assert "allocations" not in response.json() and "rebate_total_eur" not in response.json()
    assert await preview_state(_db, org_id) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["fuel", "documents", "history", "active_pairs"])
async def test_preview_each_source_read_is_bounded_and_refuses_whole_plan(
    client, db_session, _db, monkeypatch, source
):
    headers, org_id = await _register_org(client)
    await seed_preview(db_session, org_id, count=2)
    entity = await make_entity(db_session, org_id, country="EE", seed=1003, documents=False)
    await _txn(
        db_session,
        org_id,
        entity.id,
        line_seq=1,
        qty=Decimal("1"),
        net_eur=Decimal("20"),
        country="EE",
    )
    await _rebate(db_session, org_id, amount_local=Decimal("2"), country="EE", period="2026-04")
    await db_session.commit()
    constant, cap = {
        "fuel": ("PREVIEW_MAX_FUEL_ROWS", 1),
        "documents": ("PREVIEW_MAX_REBATE_ROWS", 0),
        "history": ("PREVIEW_MAX_HISTORY_ROWS", 1),
        "active_pairs": ("PREVIEW_MAX_ACTIVE_PAIRS", 1),
    }[source]
    monkeypatch.setattr(rebate, constant, cap)
    before = await preview_state(_db, org_id)
    selects = []

    def observe(_connection, _cursor, statement, *_args):
        if statement.lstrip().upper().startswith("SELECT") and any(
            "FROM " + name in statement for name in ("fuel_transactions", "vat_off_invoice_rebates")
        ):
            selects.append(statement)

    event.listen(db_session.bind.sync_engine, "before_cursor_execute", observe)
    try:
        response = await client.get(PREVIEW, headers=headers, params={"period": PERIOD})
    finally:
        event.remove(db_session.bind.sync_engine, "before_cursor_execute", observe)
    assert response.status_code == 413 and response.json()["code"] == "rebate_preview_too_large"
    assert "allocations" not in response.json() and "rebate_total_eur" not in response.json()
    assert selects and all("LIMIT" in statement for statement in selects)
    assert await preview_state(_db, org_id) == before
    result = await rebate.merge_period(db_session, org_id, PERIOD)
    assert result.lines_allocated == 2
    await db_session.rollback()


@pytest.mark.asyncio
async def test_preview_does_not_convert_fx_or_call_apply(client, db_session, monkeypatch):
    headers, org_id = await _register_org(client)
    await seed_preview(db_session, org_id, count=1)

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("a preview must not perform FX conversion or apply a merge")

    monkeypatch.setattr(rebate.fx, "to_eur", forbidden)
    monkeypatch.setattr(rebate, "merge_period", forbidden)
    response = await client.get(PREVIEW, headers=headers, params={"period": PERIOD})
    assert response.status_code == 200 and response.json()["changed"] == 1
    assert (await client.post(PREVIEW, headers=headers, json={"period": PERIOD})).status_code == 405


@pytest.mark.asyncio
async def test_preview_source_correction_is_replanned_by_close(client, db_session):
    headers, org_id = await _register_org(client)
    await seed_preview(db_session, org_id, count=1)
    observed = await client.get(PREVIEW, headers=headers, params={"period": PERIOD})
    assert observed.json()["allocations"][0]["new_net_eur_eff"] == "9.99"
    await _rebate(db_session, org_id, amount_local=Decimal("2"))
    await db_session.commit()
    result = await rebate.merge_period(db_session, org_id, PERIOD)
    assert result.rebate_total_eur == Decimal("2.00")
    assert result.allocations[0].new_net_eur_eff == Decimal("8.00")
    await db_session.commit()
    fresh = await client.get(PREVIEW, headers=headers, params={"period": PERIOD})
    assert fresh.json()["changed"] == 0 and fresh.json()["rebate_total_eur"] == "2.00"


async def assert_approved_frozen_claim_and_history_survive(client, db, factory):
    from app.services.transport import decision
    from tests.transport.conftest import activate_entity, enable_transport
    from tests.transport.test_wo93_client_status import _submitted_claim

    headers, org_id = await _register_org(client)
    await enable_transport(db, org_id)
    entity = await make_entity(db, org_id, country="EE", seed=1010)
    await activate_entity(db, org_id, entity.id, "LV")
    await db.commit()
    claim = await _submitted_claim(db, org_id, entity.id)
    await decision.record_decision(
        db, org_id, claim.id, outcome="approved", decision_date=date(2026, 7, 2)
    )
    historical = await _txn(
        db,
        org_id,
        entity.id,
        line_seq=1,
        qty=Decimal("1"),
        net_eur=Decimal("10"),
        period="2026-04",
    )
    await _rebate(db, org_id, amount_local=Decimal("1"), period="2026-04")
    await _rebate(db, org_id, amount_local=Decimal("50"))
    await db.commit()
    before = await preview_state(factory, org_id)
    frozen_names = ("vat_refund_claims", "vat_claim_lines", "vat_claimed_invoices")
    assert before["vat_refund_claims"][0]["status"] == "approved"
    assert before["vat_refund_claims"][0]["vat_eur"] == Decimal("420.00")
    assert before["vat_refund_claims"][0]["fee_eur"] == Decimal("42.00")
    assert before["vat_claim_lines"] and all(r["frozen_at"] for r in before["vat_claim_lines"])
    assert before["vat_claimed_invoices"]

    response = await client.get(PREVIEW, headers=headers, params={"period": PERIOD})
    assert response.status_code == 200 and response.json()["changed"] == 1
    assert await preview_state(factory, org_id) == before
    # This actual engine apply receives no preview object or version/token.
    result = await rebate.merge_period(db, org_id, PERIOD)
    assert result.changed == 1 and result.rebate_total_eur == Decimal("50.00")
    await db.commit()
    applied = await preview_state(factory, org_id)
    assert all(applied[name] == before[name] for name in frozen_names)
    assert [r for r in applied["fuel_transactions"] if r["id"] == historical.id] == [
        r for r in before["fuel_transactions"] if r["id"] == historical.id
    ]
    # No historical backfill, no repeated audit on an idempotent rerun.
    rerun = await rebate.merge_period(db, org_id, PERIOD)
    assert rerun.changed == 0
    await db.commit()
    assert await preview_state(factory, org_id) == applied


@pytest.mark.asyncio
async def test_preview_and_close_preserve_approved_frozen_claims_and_historical_period(
    client, db_session, _db
):
    await assert_approved_frozen_claim_and_history_survive(client, db_session, _db)
