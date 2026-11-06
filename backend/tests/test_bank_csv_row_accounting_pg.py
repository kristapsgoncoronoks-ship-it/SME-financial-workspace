"""Native Numeric(14,2) edges and atomic CSV refusal in an owned test tenant."""

import os
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic.script import ScriptDirectory
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core import tenant
from app.core.config import settings
from app.core.database import get_session
from app.main import app
from app.models.audit import AuditEvent
from app.models.bank_import import BankLine, BankStatement
from app.models.expense import ExpenseImportBatch, ExpenseTransaction
from app.models.organization import Organization
from tests.test_bank_csv_row_accounting import NUMERIC_API_CASES, RANGE_DETAIL, _numeric_csv

PG_URL = os.environ.get("RLS_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not PG_URL, reason="requires migrated non-superuser PostgreSQL")


@pytest_asyncio.fixture
async def bank_csv_pg(monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", None)
    engine = create_async_engine(PG_URL, pool_size=2, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    previous = dict(app.dependency_overrides)
    org_id = None

    async def session():
        async with factory() as db:
            yield db

    app.dependency_overrides[get_session] = session
    try:
        async with factory() as db:
            flags = (
                await db.execute(
                    text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user")
                )
            ).one()
            assert flags == (False, False)
            backend = Path(__file__).resolve().parents[1]
            assert (
                await db.scalar(text("SELECT version_num FROM alembic_version"))
                == ScriptDirectory(str(backend / "alembic")).get_current_head()
            )
            columns = (
                await db.execute(
                    text(
                        "SELECT table_name,numeric_precision,numeric_scale FROM information_schema.columns "
                        "WHERE table_schema='public' AND column_name='amount' "
                        "AND table_name IN ('bank_lines','expense_transactions') ORDER BY table_name"
                    )
                )
            ).all()
            assert columns == [("bank_lines", 14, 2), ("expense_transactions", 14, 2)]
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
        ) as client:
            registered = await client.post(
                "/api/v1/auth/register",
                json={
                    "organization_name": "Synthetic native CSV decimal boundary",
                    "name": "Synthetic native CSV owner",
                    "email": f"bank-csv-{uuid4()}@example.com",
                    "password": "Synthetic-Native-CSV-Password-32",
                },
            )
            assert registered.status_code == 201, registered.text
            identity = registered.json()
            org_id = identity["organization"]["id"]
            client.headers.update({"Authorization": "Bearer " + identity["token"]["access_token"]})
            for module in ["issuing", "expenses"]:
                response = await client.put(f"/api/v1/modules/{module}", json={"enabled": True})
                assert response.status_code == 200, response.text
            yield client, factory, org_id
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
        try:
            if org_id is not None:
                token = tenant.set_current_org(org_id)
                try:
                    async with factory.begin() as db:
                        # This disposable fixture owns the entire synthetic
                        # tenant, including its intentionally RESTRICTed trail.
                        await db.execute(delete(AuditEvent).where(AuditEvent.org_id == org_id))
                        await db.execute(delete(Organization).where(Organization.id == org_id))
                        assert (
                            await db.scalar(
                                select(func.count())
                                .select_from(Organization)
                                .where(Organization.id == org_id)
                            )
                            == 0
                        )
                finally:
                    tenant.reset_current_org(token)
        finally:
            await engine.dispose()


async def upload(client, content):
    return await client.post(
        "/api/v1/reconciliation/import", files={"file": ("numeric.csv", content, "text/csv")}
    )


async def bank_state(factory, org_id):
    token = tenant.set_current_org(org_id)
    try:
        async with factory() as db:
            statements = list(
                (
                    await db.execute(
                        select(BankStatement.id, BankStatement.sha256)
                        .where(BankStatement.org_id == org_id)
                        .order_by(BankStatement.id)
                    )
                ).all()
            )
            lines = list(
                (
                    await db.execute(
                        select(
                            BankLine.id,
                            BankLine.amount,
                            BankLine.currency,
                            BankLine.direction,
                            BankLine.status,
                        )
                        .where(BankLine.org_id == org_id)
                        .order_by(BankLine.id)
                    )
                ).all()
            )
            return statements, lines
    finally:
        tenant.reset_current_org(token)


@pytest.mark.asyncio
async def test_native_csv_stores_actual_numeric_edges_and_preserves_duplicates(bank_csv_pg):
    client, factory, org_id = bank_csv_pg
    content = (
        b"date,description,amount,currency\n"
        b"2026-11-05,Maximum credit,999999999999.99,EUR\n"
        b"2026-11-05,Maximum debit,-999999999999.99,EUR\n"
        b"2026-11-05,Rounded within maximum,999999999999.994,EUR\n"
    )
    response = await upload(client, content)
    assert response.status_code == 201, response.text
    assert response.json()["imported"] == 3 and response.json()["warnings"] == []
    before = await bank_state(factory, org_id)
    assert sorted(row[1] for row in before[1]) == [
        Decimal("-999999999999.99"),
        Decimal("999999999999.99"),
        Decimal("999999999999.99"),
    ]
    statement_id = response.json()["statement_id"]
    listed = await client.get(f"/api/v1/reconciliation/statements/{statement_id}/lines")
    assert listed.status_code == 200, listed.text
    assert sorted(row["amount"] for row in listed.json()) == [
        "-999999999999.99",
        "999999999999.99",
        "999999999999.99",
    ]
    token = tenant.set_current_org(org_id)
    try:
        async with factory.begin() as db:
            # Independently prove the migrated PostgreSQL typmod, not just an
            # application constant or SQLite's permissive Numeric behavior.
            for amount in [Decimal("1000000000000.00"), Decimal("-1000000000000.00")]:
                with pytest.raises(DBAPIError) as refused:
                    async with db.begin_nested():
                        db.add(
                            BankLine(
                                org_id=org_id,
                                statement_id=statement_id,
                                line_date=date(2026, 11, 5),
                                description="Synthetic refused direct DB edge",
                                amount=amount,
                                direction="credit",
                                currency="EUR",
                            )
                        )
                        await db.flush()
                assert refused.value.orig.sqlstate == "22003"
    finally:
        tenant.reset_current_org(token)
    assert (await upload(client, content)).status_code == 409
    assert await bank_state(factory, org_id) == before
    split = await upload(
        client,
        b"date,description,debit,credit,currency\n"
        b"2026-11-05,Debit precedence,12.34,56.78,EUR\n"
        b"2026-11-05,Finite split credit,0,56.78,EUR\n",
    )
    assert split.status_code == 201, split.text
    after = await bank_state(factory, org_id)
    assert len(after[0]) == 2 and len(after[1]) == 5
    assert any(row[1:4] == (Decimal("-12.34"), "EUR", "debit") for row in after[1])
    assert any(row[1:4] == (Decimal("56.78"), "EUR", "credit") for row in after[1])
    split_lines = await client.get(
        f"/api/v1/reconciliation/statements/{split.json()['statement_id']}/lines"
    )
    assert split_lines.status_code == 200, split_lines.text
    assert sorted(row["amount"] for row in split_lines.json()) == ["-12.34", "56.78"]


@pytest.mark.asyncio
async def test_native_invalid_numeric_file_never_commits_earlier_valid_bank_rows(bank_csv_pg):
    client, factory, org_id = bank_csv_pg
    seeded = await upload(
        client, b"date,description,amount,currency\n2026-11-05,Synthetic baseline,-12.34,EUR\n"
    )
    assert seeded.status_code == 201
    before = await bank_state(factory, org_id)
    for value, layout in NUMERIC_API_CASES:
        response = await upload(client, _numeric_csv(value, layout))
        assert response.status_code == 422, response.text
        assert response.json()["detail"] == RANGE_DETAIL
        assert value not in response.text and "synthetic-private" not in response.text
        assert await bank_state(factory, org_id) == before
        readable = await client.get(
            f"/api/v1/reconciliation/statements/{seeded.json()['statement_id']}/lines"
        )
        assert readable.status_code == 200, readable.text
        assert len(readable.json()) == 1 and readable.json()[0]["amount"] == "-12.34"
    token = tenant.set_current_org(org_id)
    try:
        async with factory() as db:
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(ExpenseImportBatch)
                    .where(ExpenseImportBatch.org_id == org_id)
                )
                == 0
            )
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(ExpenseTransaction)
                    .where(ExpenseTransaction.org_id == org_id)
                )
                == 0
            )
    finally:
        tenant.reset_current_org(token)


@pytest.mark.asyncio
async def test_native_expense_invalid_numeric_file_preserves_existing_import(bank_csv_pg):
    client, factory, org_id = bank_csv_pg

    async def expense_upload(content):
        return await client.post(
            "/api/v1/expenses/import/bank-statement",
            files={"file": ("numeric.csv", content, "text/csv")},
            data={"account_reference": "Synthetic native card", "statement_currency": "EUR"},
        )

    valid = await expense_upload(
        b"date,description,amount,currency\n2026-11-05,Synthetic valid debit,-12.34,EUR\n"
    )
    assert valid.status_code == 200, valid.text
    token = tenant.set_current_org(org_id)
    try:
        async with factory() as db:
            before = list(
                (
                    await db.execute(
                        select(
                            ExpenseTransaction.id,
                            ExpenseTransaction.amount,
                            ExpenseTransaction.currency,
                        ).where(ExpenseTransaction.org_id == org_id)
                    )
                ).all()
            )
            assert len(before) == 1 and before[0][1:] == (Decimal("12.34"), "EUR")
        for value, layout in NUMERIC_API_CASES:
            response = await expense_upload(_numeric_csv(value, layout))
            assert response.status_code == 422 and response.json()["detail"] == RANGE_DETAIL
        async with factory() as db:
            assert (
                list(
                    (
                        await db.execute(
                            select(
                                ExpenseTransaction.id,
                                ExpenseTransaction.amount,
                                ExpenseTransaction.currency,
                            ).where(ExpenseTransaction.org_id == org_id)
                        )
                    ).all()
                )
                == before
            )
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(ExpenseImportBatch)
                    .where(ExpenseImportBatch.org_id == org_id)
                )
                == 1
            )
    finally:
        tenant.reset_current_org(token)
    assert await bank_state(factory, org_id) == ([], [])
