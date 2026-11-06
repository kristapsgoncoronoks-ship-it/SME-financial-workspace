"""Development DDL preserves the existing tenant-safe PostgreSQL delete policy.

These portable controls exercise compilation and actual SQLite startup. Native
PostgreSQL execution and migrated RLS contracts are qualified separately.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import Column, ForeignKeyConstraint, Index, Integer, MetaData, Table, inspect
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateIndex, CreateTable

from app.models import Base

# Independent expectations from c4d6e8f0a2b4; not derived from the new helper.
LINKS = (
    ("expense_items", "fk_expense_items_project", "project_id", "projects"),
    ("invoices", "fk_invoices_project", "project_id", "projects"),
    ("issued_invoices", "fk_issued_invoices_project", "project_id", "projects"),
    ("issued_invoices", "fk_issued_invoices_issuer", "issuer_id", "issuer_profiles"),
)
OPTIONAL_LINKS = (
    ("expense_transactions", "fk_expense_transactions_item", "item_id", "expense_items"),
    ("issued_invoices", "fk_issued_invoices_partner", "partner_id", "partners"),
    ("recurring_invoices", "fk_recurring_invoices_partner", "partner_id", "partners"),
    (
        "vendor_change_requests",
        "fk_vendor_change_requests_source_document",
        "source_document_id",
        "documents",
    ),
)


def project(metadata, dialect):
    from app.core.development_schema import development_metadata

    return development_metadata(metadata, dialect)


def ddl_snapshot(metadata, dialect):
    return {
        table.key: {
            # SQLAlchemy stores constraints as a set, so a copied table can
            # emit those clauses in a different order. Keep every DDL line
            # and separately pin the meaningful declared column order.
            "table": tuple(
                sorted(
                    line.strip().rstrip(",")
                    for line in str(CreateTable(table).compile(dialect=dialect)).splitlines()
                    if line.strip()
                )
            ),
            "column_order": tuple(column.name for column in table.columns),
            "indexes": sorted(
                str(CreateIndex(index).compile(dialect=dialect)) for index in table.indexes
            ),
        }
        for table in metadata.sorted_tables
    }


def alternate_metadata():
    metadata = MetaData()
    for table in Base.metadata.sorted_tables:
        table.to_metadata(metadata)
    return metadata


def named_fk(metadata, table, name):
    return next(fk for fk in metadata.tables[table].foreign_key_constraints if fk.name == name)


def test_postgresql_ddl_changes_only_eight_actions_and_one_legacy_name():
    pg = postgresql.dialect()
    before = ddl_snapshot(Base.metadata, pg)
    expected = {key: {**value} for key, value in before.items()}
    for table, name, column, _parent in LINKS:
        original = expected[table]["table"]
        changed = [
            re.subn(
                rf"(CONSTRAINT {name} .*ON DELETE SET NULL)$",
                rf"\1 ({column})",
                line,
            )
            for line in original
        ]
        assert sum(count for _line, count in changed) == 1
        expected[table]["table"] = tuple(sorted(line for line, _count in changed))
    for table, name, column, _parent in OPTIONAL_LINKS:
        original = expected[table]["table"]
        assert sum(line.startswith(f"CONSTRAINT {name} ") for line in original) == 1
        expected[table]["table"] = tuple(
            sorted(
                line + f" ON DELETE SET NULL ({column})"
                if line.startswith(f"CONSTRAINT {name} ")
                else line
                for line in original
            )
        )
    changed = [
        re.subn(
            r"^FOREIGN KEY\(partner_id\)",
            "CONSTRAINT fk_issued_partner FOREIGN KEY(partner_id)",
            line,
        )
        for line in expected["issued_invoices"]["table"]
    ]
    assert sum(count for _, count in changed) == 1
    expected["issued_invoices"]["table"] = tuple(sorted(line for line, _ in changed))
    projected = project(Base.metadata, pg)
    assert ddl_snapshot(projected, pg) == expected
    assert projected is not Base.metadata
    for table, name, column, _parent in LINKS + OPTIONAL_LINKS:
        fk = named_fk(projected, table, name)
        assert fk.ondelete == f"SET NULL ({column})"
        assert all(element.ondelete == fk.ondelete for element in fk.elements)
    assert ddl_snapshot(Base.metadata, pg) == before


def test_alternating_concurrent_dialects_preserve_shared_metadata():
    before_pg = ddl_snapshot(Base.metadata, postgresql.dialect())
    before_sqlite = ddl_snapshot(Base.metadata, sqlite.dialect())

    def compile_one(name):
        dialect = postgresql.dialect() if name == "postgresql" else sqlite.dialect()
        metadata = project(Base.metadata, dialect)
        ddl = ddl_snapshot(metadata, dialect)
        if name == "sqlite":
            assert metadata is Base.metadata
            assert ddl == before_sqlite
        else:
            for table, constraint, column, _parent in LINKS + OPTIONAL_LINKS:
                assert re.search(
                    rf"CONSTRAINT {constraint} [^\n]*ON DELETE SET NULL \({column}\)",
                    "\n".join(ddl[table]["table"]),
                )

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(compile_one, ["postgresql", "sqlite"] * 4))
    assert ddl_snapshot(Base.metadata, postgresql.dialect()) == before_pg
    assert ddl_snapshot(Base.metadata, sqlite.dialect()) == before_sqlite


def declaration_metadata(*, invalid=None, schema=None):
    """Small declarative contracts permit source-shape controls before copying."""
    metadata = MetaData(schema=schema, naming_convention={"ix": "ix_%(column_0_label)s"})
    for parent in ("projects", "issuer_profiles"):
        Table(
            parent,
            metadata,
            Column("org_id", Integer, primary_key=True),
            Column("id", Integer, primary_key=True),
        )
    tables = {}
    for name in ("expense_items", "invoices", "issued_invoices"):
        tables[name] = Table(
            name,
            metadata,
            Column("org_id", Integer, nullable=False),
            Column("id", Integer, primary_key=True),
            Column("project_id", Integer, nullable=True),
            *([Column("issuer_id", Integer, nullable=True)] if name == "issued_invoices" else []),
        )
    for parent in ("partners", "documents"):
        tables[parent] = Table(
            parent,
            metadata,
            Column("org_id", Integer, nullable=False),
            Column("id", Integer, primary_key=True),
        )
    tables["issued_invoices"].append_column(Column("partner_id", Integer, nullable=True))
    for table, _, column, _parent in OPTIONAL_LINKS:
        if table not in tables:
            tables[table] = Table(
                table,
                metadata,
                Column("org_id", Integer, nullable=False),
                Column("id", Integer, primary_key=True),
                Column(column, Integer, nullable=True),
            )
    for parent in ("expense_items", "partners", "documents"):
        Index(f"uq_{parent}_org_id", tables[parent].c.org_id, tables[parent].c.id, unique=True)
    for table, name, column, parent in OPTIONAL_LINKS:
        prefix = f"{schema}." if schema else ""
        tables[table].append_constraint(
            ForeignKeyConstraint(
                ["org_id", column], [f"{prefix}{parent}.org_id", f"{prefix}{parent}.id"], name=name
            )
        )
        tables[table].append_constraint(
            ForeignKeyConstraint([column], [f"{prefix}{parent}.id"], ondelete="SET NULL")
        )
    for table, name, column, parent in LINKS:
        if invalid == "missing_constraint" and name == "fk_expense_items_project":
            continue
        local = ["org_id", column]
        target = [f"{parent}.org_id", f"{parent}.id"]
        if invalid == "reversed_columns" and name == "fk_expense_items_project":
            local.reverse()
            target.reverse()
        if invalid == "wrong_target" and name == "fk_expense_items_project":
            target = ["issuer_profiles.org_id", "issuer_profiles.id"]
        if schema:
            target = [f"{schema}.{value}" for value in target]
        tables[table].append_constraint(
            ForeignKeyConstraint(local, target, name=name, ondelete="SET NULL")
        )
    if invalid == "nullable_tenant":
        tables["expense_items"].c.org_id.nullable = True
    if invalid == "required_optional_ref":
        tables["expense_items"].c.project_id.nullable = False
    if invalid == "missing_table":
        metadata.remove(tables["expense_items"])
    return metadata


def test_projection_preserves_metadata_schema_and_naming_convention():
    metadata = declaration_metadata(schema="synthetic_schema")
    projected = project(metadata, postgresql.dialect())
    assert projected.schema == metadata.schema
    assert projected.naming_convention == metadata.naming_convention
    assert set(projected.tables) == set(metadata.tables)
    for table, name, column, parent in LINKS + OPTIONAL_LINKS:
        fk = named_fk(projected, f"synthetic_schema.{table}", name)
        assert fk.ondelete == f"SET NULL ({column})"
        assert [element.target_fullname for element in fk.elements] == [
            f"synthetic_schema.{parent}.org_id",
            f"synthetic_schema.{parent}.id",
        ]


@pytest.mark.parametrize("table,name,column,parent", LINKS)
def test_each_original_constraint_policy_change_is_refused(table, name, column, parent):
    metadata = alternate_metadata()
    fk = named_fk(metadata, table, name)
    fk.ondelete = "CASCADE"
    with pytest.raises(RuntimeError, match=rf"DB-PG-BOOTSTRAP-01.*{table}\.{name}"):
        project(metadata, postgresql.dialect())
    assert fk.ondelete == "CASCADE"


@pytest.mark.parametrize("table,name,column,parent", LINKS)
def test_each_original_element_delete_policy_change_is_refused(table, name, column, parent):
    metadata = alternate_metadata()
    fk = named_fk(metadata, table, name)
    fk.elements[0].ondelete = "CASCADE"
    with pytest.raises(RuntimeError, match=rf"DB-PG-BOOTSTRAP-01.*{table}\.{name}"):
        project(metadata, postgresql.dialect())
    assert fk.ondelete == "SET NULL"
    assert fk.elements[0].ondelete == "CASCADE"


@pytest.mark.parametrize(
    "attribute,value",
    [
        ("onupdate", "CASCADE"),
        ("deferrable", True),
        ("initially", "DEFERRED"),
        ("match", "FULL"),
        ("use_alter", True),
        ("link_to_name", True),
    ],
)
def test_other_incoherent_original_element_options_are_refused(attribute, value):
    metadata = alternate_metadata()
    fk = named_fk(metadata, "expense_items", "fk_expense_items_project")
    setattr(fk.elements[0], attribute, value)
    with pytest.raises(RuntimeError, match="DB-PG-BOOTSTRAP-01"):
        project(metadata, postgresql.dialect())
    assert getattr(fk.elements[0], attribute) == value


@pytest.mark.parametrize(
    "invalid",
    [
        "missing_table",
        "missing_constraint",
        "reversed_columns",
        "wrong_target",
        "nullable_tenant",
        "required_optional_ref",
    ],
)
def test_unexpected_original_contract_structure_is_refused(invalid):
    with pytest.raises(RuntimeError, match="DB-PG-BOOTSTRAP-01"):
        project(declaration_metadata(invalid=invalid), postgresql.dialect())


def test_sqlite_keeps_original_metadata_without_postgresql_policy_requirements():
    metadata = declaration_metadata(invalid="missing_constraint")
    before = ddl_snapshot(metadata, sqlite.dialect())
    assert project(metadata, sqlite.dialect()) is metadata
    assert ddl_snapshot(metadata, sqlite.dialect()) == before


def isolate_lifespan_side_effects(monkeypatch, session_factory):
    from app import main
    from app.core import database
    from app.services import fx

    monkeypatch.setattr(database, "SessionLocal", session_factory)
    monkeypatch.setattr(fx, "ensure_seed_rates", AsyncMock(return_value=0))
    monkeypatch.setattr(fx, "ensure_european_coverage", AsyncMock(return_value=0))
    monkeypatch.setattr(main, "freeze_startup_heap", lambda: None)
    monkeypatch.setattr(main, "log_production_warnings", lambda _log: None)
    return main


@pytest.mark.asyncio
async def test_development_lifespan_creates_sqlite_through_dialect_metadata(monkeypatch, tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'startup.db'}")
    main = isolate_lifespan_side_effects(monkeypatch, async_sessionmaker(engine))
    calls = []

    def selected(metadata, dialect):
        calls.append(dialect.name)
        return project(metadata, dialect)

    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(main.settings, "environment", "development")
    monkeypatch.setattr(main, "development_metadata", selected, raising=False)
    try:
        async with main.lifespan(main.app):
            async with engine.connect() as connection:
                tables = await connection.run_sync(lambda sync: inspect(sync).get_table_names())
                assert set(tables) == set(Base.metadata.tables)
        assert calls == ["sqlite"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_production_lifespan_never_creates_or_projects_development_schema(monkeypatch):
    @asynccontextmanager
    async def synthetic_session():
        yield object()

    main = isolate_lifespan_side_effects(monkeypatch, synthetic_session)

    class ProductionEngine:
        dispose = AsyncMock()

        def begin(self):
            raise AssertionError("Production must leave schema creation to Alembic")

    def forbidden_projection(*_args):
        raise AssertionError("Production must not project development metadata")

    engine = ProductionEngine()
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(main.settings, "environment", "production")
    monkeypatch.setattr(main, "development_metadata", forbidden_projection, raising=False)
    async with main.lifespan(main.app):
        engine.dispose.assert_not_awaited()
    engine.dispose.assert_awaited_once()
