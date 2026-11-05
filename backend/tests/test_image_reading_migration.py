"""A rollback cannot erase measured preparation hidden by inherited tenant scope."""

import json
import uuid
from datetime import UTC, datetime

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from tests.test_migrations import BACKEND_DIR, _run_alembic
from tests.test_planning_ocr_migration_integration import _insert, database, migrate
from tests.test_planning_ocr_migration_integration import migration_url as migration_url

HEAD = "c3f5a7b9d1e4"
PARENT = "b2e4f6a8c0d3"
PREPARATION = json.dumps(
    {
        "preprocess_version": "local-image-ocr-v1",
        "source_width": 8000,
        "source_height": 10000,
        "raster_width": None,
        "raster_height": None,
        "resized": False,
    }
)


@pytest.mark.slow
@pytest.mark.parametrize("table", ["extraction_runs", "inbound_invoices"])
def test_downgrade_preserves_measured_failure_even_when_hidden(migration_url, table):
    migrate(migration_url, "upgrade", HEAD)
    org_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    row_id = str(uuid.uuid4())

    def seed(connection):
        for org_id in org_ids:
            _insert(connection, "organizations", id=org_id, name="Synthetic image provenance")
        values = {
            "id": row_id,
            "org_id": org_ids[0],
            "status": "failed",
            "method": "ocr",
            "reading_preparation_json": PREPARATION,
            "source_filename": "synthetic-resource-refusal.png",
            "filename": "synthetic-resource-refusal.png",
            "received_at": datetime(2026, 11, 5, tzinfo=UTC),
        }
        _insert(connection, table, **values)

    database(migration_url, seed)

    def retained(connection):
        columns = {c["name"] for c in inspect(connection).get_columns(table)}
        assert "reading_preparation_json" in columns
        return connection.scalar(
            text(f'SELECT reading_preparation_json FROM "{table}" WHERE id=:id'),
            {"id": row_id},
        )

    assert database(migration_url, retained) == PREPARATION
    if migration_url.startswith("postgresql"):

        def inherited_tenant(connection):
            connection.execute(
                text("SELECT set_config('app.current_org', :org, true)"), {"org": org_ids[1]}
            )
            assert (
                connection.scalar(
                    text(
                        f'SELECT count(*) FROM "{table}" WHERE reading_preparation_json IS NOT NULL'
                    )
                )
                == 0
            )
            config = Config()
            config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
            revision = ScriptDirectory.from_config(config).get_revision(HEAD)
            with Operations.context(MigrationContext.configure(connection)):
                with pytest.raises(RuntimeError, match="Measured image reading preparation exists"):
                    revision.module.downgrade()
            assert retained(connection) == PREPARATION
            assert connection.scalar(
                text(
                    "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE oid=CAST(:t AS regclass)"
                ),
                {"t": table},
            )

        database(migration_url, inherited_tenant)
    refused = _run_alembic(["downgrade", PARENT], migration_url)
    assert refused.returncode != 0
    assert "Measured image reading preparation exists" in refused.stderr
    assert database(migration_url, retained) == PREPARATION
    assert (
        database(migration_url, lambda c: c.scalar(text("SELECT version_num FROM alembic_version")))
        == HEAD
    )
