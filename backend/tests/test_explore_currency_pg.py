"""Actual native-currency reads and an admitted-query/other-unit insert race."""

import asyncio
import csv
import io
import json
import os
from pathlib import Path

import openpyxl
import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import tenant
from tests import test_explore_scope_pg
from tests.test_explore_currency import financial_state, native_invoice, seed_native_controls

explore_pg = test_explore_scope_pg.explore_pg

PG_URL = os.environ.get("RLS_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not PG_URL, reason="requires migrated non-superuser PostgreSQL")


async def native_state(factory, org_id):
    token = tenant.set_current_org(org_id)
    try:
        async with factory() as db:
            return await financial_state(db, org_id)
    finally:
        tenant.reset_current_org(token)


def record_artifact(name, body):
    folder = os.environ.get("EXPLORE_CURRENCY_ARTIFACT_DIR")
    if folder:
        root = Path(folder)
        root.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(json.dumps(body, indent=2) + "\n")


@pytest.mark.asyncio
async def test_native_units_files_foreign_opacity_and_unchanged_facts(explore_pg):
    client, factory, engine, register = explore_pg
    foreign = await register()
    await native_invoice(client, "FOREIGN-USD", "USD", "99999.00", "2")
    own = await register()
    await seed_native_controls(client)
    before = {org: await native_state(factory, org) for org in (own, foreign)}
    grouped = []
    observations = []

    def record(_conn, _cursor, statement, parameters, _context, _many):
        if "GROUP BY" in statement and "line_items" in statement:
            grouped.append((statement, parameters))

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        for currency, values in [
            ("usd", ("300.00", "30.00", "330.00")),
            ("eur", ("50.00", "5.00", "55.00")),
        ]:
            for measure, value in zip(("net", "tax", "gross"), values, strict=True):
                response = await client.get(
                    "/api/v1/analytics/explore",
                    params={"measure": measure, "currency": currency, "dim": "currency"},
                )
                assert response.status_code == 200, response.text
                result = response.json()
                assert result["rows"] == [{"currency": currency.upper(), "value": value}]
                assert (
                    result["currency"] == currency.upper() and result["currency_basis"] == "native"
                )
                assert response.headers["x-explore-currency"] == currency.upper()
                observations.append(result)
        for format in ("csv", "xlsx", "pdf"):
            response = await client.get(
                "/api/v1/analytics/explore",
                params={
                    "currency": "USD",
                    "dim": "category",
                    "format": format,
                    "export_contract": 2,
                },
            )
            assert response.status_code == 200, response.text
            assert response.headers["x-explore-currency"] == "USD"
            assert response.headers["x-explore-currency-basis"] == "native"
            assert response.headers["x-explore-export-contract"] == "2"
            assert (
                f"explore-USD-up-to-100-groups.{format}" in response.headers["content-disposition"]
            )
            expected = [["Category", "Net spend (USD)"], ["Synthetic currency basis", "300.00"]]
            if format == "csv":
                assert list(csv.reader(io.StringIO(response.text))) == expected
            elif format == "xlsx":
                workbook = openpyxl.load_workbook(io.BytesIO(response.content))
                assert [list(row) for row in workbook.active.values] == expected
            else:
                from pypdf import PdfReader

                rendered = "\n".join(
                    page.extract_text() for page in PdfReader(io.BytesIO(response.content)).pages
                )
                assert "Net spend (USD)" in rendered and "300.00" in rendered
        assert len(grouped) == 9
        for measure in ("net", "tax", "gross"):
            refused = await client.get("/api/v1/analytics/explore", params={"measure": measure})
            assert (
                refused.status_code == 422 and refused.json()["code"] == "explore_currency_required"
            )
        assert len(grouped) == 9  # no inference query or unfiltered monetary aggregate
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)
    after = {org: await native_state(factory, org) for org in (own, foreign)}
    assert after == before
    assert all("LIMIT" in sql and "invoices.currency =" in sql for sql, _ in grouped)
    assert all(101 in parameters for _, parameters in grouped)
    record_artifact(
        "native-currency-scope.json",
        {
            "observations": observations,
            "grouped_queries": len(grouped),
            "max_fetch": 101,
            "financial_table_hashes_before": before,
            "financial_table_hashes_after": after,
            "facts_unchanged": True,
            "missing_unit_refusals_before_aggregate": 3,
            "foreign_source_excluded": True,
        },
    )


@pytest.mark.asyncio
async def test_native_other_currency_insert_cannot_enter_selected_unit(explore_pg, monkeypatch):
    client, factory, _engine, register = explore_pg
    own = await register()
    await native_invoice(client, "RACE-USD", "USD", "100.00", "2")
    reached = asyncio.Event()
    release = asyncio.Event()
    original = AsyncSession.execute
    held = False

    async def gated(self, statement, *args, **kwargs):
        nonlocal held
        sql = str(statement)
        if not held and "GROUP BY" in sql and "line_items" in sql:
            held = True
            reached.set()
            await release.wait()
        return await original(self, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", gated)
    request = asyncio.create_task(
        client.get("/api/v1/analytics/explore", params={"currency": "USD", "dim": "category"})
    )
    try:
        await asyncio.wait_for(reached.wait(), timeout=10)
        assert not request.done()
        # Real second-session POST commits the EUR row after the USD request
        # was admitted, before its actual aggregate executes. No SQL or response
        # is replaced: the gate controls only this declared scheduling boundary.
        inserted = await native_invoice(client, "RACE-EUR", "EUR", "777.00")
        assert inserted["currency"] == "EUR" and inserted["total"] == "854.70"
        after_insert_before_read = await native_state(factory, own)
        release.set()
        response = await asyncio.wait_for(request, timeout=10)
        assert response.status_code == 200, response.text
        assert response.json()["rows"] == [
            {"category": "Synthetic currency basis", "value": "100.00"}
        ]
        assert response.json()["currency"] == "USD"
        assert response.headers["x-explore-currency"] == "USD"
        count = await client.get(
            "/api/v1/analytics/explore", params={"measure": "lines", "dim": "category"}
        )
        assert count.status_code == 200
        assert count.json()["rows"] == [{"category": "Synthetic currency basis", "value": "2"}]
        after_read = await native_state(factory, own)
        assert after_read == after_insert_before_read
        record_artifact(
            "native-currency-insert-race.json",
            {
                "boundary": "Actual HTTP request paused immediately before its real aggregate; real other-session EUR POST committed before release.",
                "request_wait_observed": True,
                "inserted_EUR_visible_to_count": True,
                "selected_USD_rows": response.json()["rows"],
                "financial_table_hashes_before_read": after_insert_before_read,
                "financial_table_hashes_after_read": after_read,
                "facts_unchanged_by_reads": True,
            },
        )
    finally:
        release.set()
        if not request.done():
            await asyncio.wait_for(request, timeout=10)
