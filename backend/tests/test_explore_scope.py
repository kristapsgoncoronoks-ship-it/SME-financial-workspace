"""Bounded Explore results disclose scope without changing serialized row contracts."""

import csv
import io
from decimal import Decimal

import openpyxl
import pytest
from sqlalchemy import event, select

from app.models.invoice import Invoice, LineItem
from tests.test_explore import _mk


async def seed_groups(client, db, count):
    response = await _mk(
        client, "Synthetic Explore", "EXPLORE-SCOPE", "2026-01-10", "group-0000", "1.00"
    )
    assert response.status_code == 201, response.text
    invoice = await db.scalar(select(Invoice).where(Invoice.id == response.json()["id"]))
    assert invoice is not None
    db.add_all(
        [
            LineItem(
                invoice_id=invoice.id,
                description="Synthetic group",
                category=f"group-{i:04}",
                quantity=Decimal("1"),
                unit_price=Decimal(i + 1),
                amount=Decimal(i + 1),
            )
            for i in range(1, count)
        ]
    )
    await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "count,limit,truncated",
    [(0, 100, False), (100, 100, False), (101, 100, True), (1000, 1000, False), (1001, 1000, True)],
)
async def test_scope_and_single_bounded_group_query(
    auth_client, db_session, count, limit, truncated
):
    if count:
        await seed_groups(auth_client, db_session, count)
    statements = []
    engine = db_session.bind.sync_engine

    def record(_conn, _cursor, statement, parameters, _context, _many):
        if "GROUP BY" in statement and "line_items" in statement:
            statements.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", record)
    try:
        response = await auth_client.get(f"/api/v1/analytics/explore?dim=category&limit={limit}")
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert response.status_code == 200, response.text
    result = response.json()
    assert {k: result[k] for k in ("scope", "limit", "returned_rows", "truncated")} == {
        "scope": "bounded_groups",
        "limit": limit,
        "returned_rows": min(count, limit),
        "truncated": truncated,
    }
    assert len(result["rows"]) == min(count, limit)
    assert response.headers["x-explore-returned-rows"] == str(min(count, limit))
    assert response.headers["x-explore-truncated"] == str(truncated).lower()
    assert [row["value"] for row in result["rows"]] == [
        f"{i}.00" for i in range(count, max(count - limit, 0), -1)
    ]
    assert len(statements) == 1
    assert "LIMIT" in statements[0][0]
    assert limit + 1 in statements[0][1]


@pytest.mark.asyncio
@pytest.mark.parametrize("format", ["csv", "xlsx", "pdf"])
async def test_download_scope_headers_and_unchanged_columns(auth_client, db_session, format):
    await seed_groups(auth_client, db_session, 3)
    response = await auth_client.get(
        f"/api/v1/analytics/explore?dim=category&limit=2&format={format}"
    )
    assert response.status_code == 200, response.text
    assert {
        key: response.headers[f"x-explore-{key}"]
        for key in ("scope", "limit", "returned-rows", "truncated")
    } == {
        "scope": "bounded_groups",
        "limit": "2",
        "returned-rows": "2",
        "truncated": "true",
    }
    assert f"explore-up-to-2-groups.{format}" in response.headers["content-disposition"]
    expected = [["Category", "Net spend"], ["group-0002", "3.00"], ["group-0001", "2.00"]]
    if format == "csv":
        assert list(csv.reader(io.StringIO(response.text))) == expected
    elif format == "xlsx":
        workbook = openpyxl.load_workbook(io.BytesIO(response.content))
        assert workbook.sheetnames == ["Explore"]
        assert [list(row) for row in workbook.active.values] == expected
    else:
        from pypdf import PdfReader

        text = "\n".join(
            page.extract_text() for page in PdfReader(io.BytesIO(response.content)).pages
        )
        assert "group-0002" in text and "group-0001" in text and "group-0000" not in text


@pytest.mark.asyncio
async def test_default_filter_scope_and_limit_validation(auth_client, db_session):
    await seed_groups(auth_client, db_session, 3)
    for suffix in ("status=pending", "currency=USD", "start=2027-01-01", "category=absent"):
        result = (await auth_client.get(f"/api/v1/analytics/explore?dim=category&{suffix}")).json()
        assert result["limit"] == 100
        assert result["returned_rows"] == 0 and result["truncated"] is False
    for limit in (0, 1001):
        assert (
            await auth_client.get(f"/api/v1/analytics/explore?dim=category&limit={limit}")
        ).status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("format", ["json", "csv", "xlsx", "pdf"])
async def test_bounded_formats_keep_report_read_permission(role_client, format):
    employee = await role_client("user")
    denied = await employee.get(f"/api/v1/analytics/explore?dim=category&format={format}")
    assert denied.status_code == 403
    assert "x-explore-scope" not in denied.headers
