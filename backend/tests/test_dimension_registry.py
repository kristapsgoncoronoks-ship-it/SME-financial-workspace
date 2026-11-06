"""One dimension registry (ADR-0026, board C1.6 / WO-15).

Explore and the fixed by-dimension report must consume the same registry
(`app.core.dimensions`): the five cost-allocation dimensions are groupable in
Explore, both paths bucket untagged spend under the same ``(unassigned)`` label,
and — the acceptance cross-check — both return **identical numbers for the same
cut**.
"""

from __future__ import annotations

import pytest

from app.core import dimensions as core_dimensions
from app.services import explore


def _registry_drift(core_keys, explore_dims) -> list[str]:
    """The parity predicate: core-registry keys missing from Explore's dims."""
    return [k for k in core_keys if k not in explore_dims]


async def _invoice(auth_client, number, lines, **extra):
    """Create an invoice from (amount, tax_rate) line tuples + dimension tags."""
    body = {
        "vendor_name": "Shell Fleet",
        "invoice_number": number,
        "issue_date": "2026-05-10",
        "line_items": [
            {"description": "svc", "quantity": "1", "unit_price": amt, "tax_rate": rate}
            for amt, rate in lines
        ],
        **extra,
    }
    r = await auth_client.post("/api/v1/invoices", json=body)
    assert r.status_code == 201, r.text
    return r.json()


@pytest.mark.asyncio
async def test_explore_catalog_includes_every_core_dimension(auth_client):
    cat = (await auth_client.get("/api/v1/analytics/fields")).json()
    by_key = {d["key"]: d for d in cat["dimensions"]}
    for key, label in core_dimensions.DIMENSIONS.items():
        assert key in by_key, f"core dimension '{key}' missing from the Explore catalog"
        assert by_key[key]["label"] == label
        assert by_key[key]["temporal"] is False


def test_registry_parity_and_drift_detection():
    # The real registries are in parity...
    assert _registry_drift(core_dimensions.DIMENSION_KEYS, explore.DIMENSIONS) == []
    # ...and the predicate actually detects a violation (a parity test that
    # cannot fail proves nothing): drop one key from a fake consumer.
    broken = {k: None for k in core_dimensions.DIMENSION_KEYS if k != "vehicle"}
    assert _registry_drift(core_dimensions.DIMENSION_KEYS, broken) == ["vehicle"]
    # Labels are single-sourced too.
    for k in core_dimensions.DIMENSION_KEYS:
        assert explore.DIMENSIONS[k].label == core_dimensions.DIMENSIONS[k]


@pytest.mark.asyncio
async def test_explore_groups_by_each_cost_dimension(auth_client):
    tags = {k: f"TAG-{k}" for k in core_dimensions.DIMENSION_KEYS}
    await _invoice(auth_client, "T-1", [("100.00", "0")], **tags)
    await _invoice(auth_client, "T-2", [("40.00", "0")])  # untagged
    for key in core_dimensions.DIMENSION_KEYS:
        r = await auth_client.get(f"/api/v1/analytics/explore?currency=EUR&measure=net&dim={key}")
        assert r.status_code == 200, r.text
        rows = {row[key]: row["value"] for row in r.json()["rows"]}
        assert rows[f"TAG-{key}"] == "100.00"
        assert rows[core_dimensions.UNASSIGNED] == "40.00"


@pytest.mark.asyncio
async def test_explore_matches_fixed_report_same_cut(auth_client):
    """The C1.6 acceptance cross-check: Explore (line grain, `gross`) and the
    fixed by-dimension report (invoice grain, `Invoice.total`) return identical
    figures for the same cut.

    Fixtures use tax rates exact at 2dp (100.00 @ 20% -> 20.00; 40.00 @ 21% ->
    8.40) so the per-line-quantized invoice total and Explore's unrounded gross
    sum are exactly equal and the assertion is string-exact, not tolerance-based.
    """
    # TRUCK-01: one multi-line invoice (line grain != invoice grain) + one more.
    await _invoice(
        auth_client, "X-1", [("100.00", "20"), ("40.00", "21")], vehicle="TRUCK-01"
    )  # 100 + 20 + 40 + 8.40 = 168.40
    await _invoice(auth_client, "X-2", [("25.00", "0")], vehicle="TRUCK-01")  # 25.00
    await _invoice(auth_client, "X-3", [("50.00", "20")], vehicle="VAN-7")  # 60.00
    await _invoice(auth_client, "X-4", [("10.00", "0")])  # untagged -> (unassigned)

    fixed = (
        await auth_client.get("/api/v1/analytics/by-dimension", params={"dimension": "vehicle"})
    ).json()
    fixed_rows = {row["value"]: row for row in fixed["rows"]}

    pivot = (
        await auth_client.get("/api/v1/analytics/explore?currency=EUR&measure=gross&dim=vehicle")
    ).json()
    pivot_totals = {row["vehicle"]: row["value"] for row in pivot["rows"]}

    counts = (
        await auth_client.get("/api/v1/analytics/explore?measure=invoices&dim=vehicle")
    ).json()
    pivot_counts = {row["vehicle"]: int(row["value"]) for row in counts["rows"]}

    # Both paths see the same buckets...
    assert set(pivot_totals) == set(fixed_rows) == {"TRUCK-01", "VAN-7", "(unassigned)"}
    # ...and identical numbers per bucket.
    for value, row in fixed_rows.items():
        assert pivot_totals[value] == row["total"], value
        assert pivot_counts[value] == row["invoice_count"], value
    assert pivot_totals["TRUCK-01"] == "193.40"
    assert pivot_totals["(unassigned)"] == "10.00"


@pytest.mark.asyncio
async def test_explore_two_dimensions_with_cost_dimension(auth_client):
    await _invoice(auth_client, "M-1", [("100.00", "0")], vehicle="TRUCK-01")
    await _invoice(auth_client, "M-2", [("50.00", "0")], vehicle="TRUCK-01")
    r = (
        await auth_client.get(
            "/api/v1/analytics/explore?currency=EUR&measure=net&dim=month&dim=vehicle"
        )
    ).json()
    assert [d["key"] for d in r["dimensions"]] == ["month", "vehicle"]
    assert r["rows"] == [{"month": "2026-05", "vehicle": "TRUCK-01", "value": "150.00"}]


@pytest.mark.asyncio
async def test_explore_cost_dimension_cross_tenant_zero_rows(auth_client, client):
    # Identical-looking data on both sides — distinct data can pass a broken
    # filter by accident.
    await _invoice(auth_client, "S-1", [("500.00", "0")], vehicle="TRUCK-01")
    # `auth_client` and `client` share one underlying AsyncClient; keep tenant
    # A's token so we can query as A again after switching to B.
    tenant_a_token = auth_client.headers["Authorization"]

    reg = await client.post(
        "/api/v1/auth/register",
        json={
            "organization_name": "Other Co",
            "name": "O",
            "email": "o-dimreg@o.io",
            "password": "supersecret2",
        },
    )
    client.headers["Authorization"] = f"Bearer {reg.json()['token']['access_token']}"
    await client.post(
        "/api/v1/invoices",
        json={
            "vendor_name": "Shell Fleet",
            "invoice_number": "S-1",
            "issue_date": "2026-05-10",
            "line_items": [
                {"description": "svc", "quantity": "1", "unit_price": "9.00", "tax_rate": "0"}
            ],
            "vehicle": "TRUCK-01",
        },
    )

    b = (await client.get("/api/v1/analytics/explore?currency=EUR&measure=net&dim=vehicle")).json()
    assert b["rows"] == [{"vehicle": "TRUCK-01", "value": "9.00"}]  # B's own row only
    auth_client.headers["Authorization"] = tenant_a_token
    a = (
        await auth_client.get("/api/v1/analytics/explore?currency=EUR&measure=net&dim=vehicle")
    ).json()
    assert a["rows"] == [{"vehicle": "TRUCK-01", "value": "500.00"}]  # A unchanged


@pytest.mark.asyncio
async def test_explore_rejects_unknown_dimension_still(auth_client):
    r = await auth_client.get("/api/v1/analytics/explore?currency=EUR&measure=net&dim=not_a_dim")
    assert r.status_code == 422
    assert "not_a_dim" in r.json()["detail"]
