"""Actual restricted PostgreSQL reads preserve source and business facts."""

import hashlib
import json
import os
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text, update

from app.main import app
from app.models import Base
from app.models.receipt import Receipt
from app.schemas.capture_reading import METADATA_BYTES, ReadingObservationSummary
from tests.test_capture_reading_inspection import base, seed, stored
from tests.test_image_reading_metadata_pg import reading_pg as reading_pg
from tests.test_image_reading_metadata_pg import scoped

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("RLS_TEST_DATABASE_URL"),
        reason="requires migrated non-superuser PostgreSQL",
    ),
]


async def business_snapshot(db, org_id):
    result = {}
    for table in Base.metadata.sorted_tables:
        if "org_id" not in table.c or table.name == "sessions":
            continue
        # Session last-seen is expected authentication metadata, not source or
        # business data. Never publish passwords, token hashes or raw row values.
        rows = [
            dict(row)
            for row in (await db.execute(select(table).where(table.c.org_id == org_id))).mappings()
        ]
        encoded = sorted(json.dumps(row, sort_keys=True, default=str) for row in rows)
        result[table.name] = {
            "count": len(rows),
            "sha256": hashlib.sha256(json.dumps(encoded).encode()).hexdigest(),
        }
    return result


@pytest.mark.parametrize("channel", ["upload", "inbound"])
async def test_native_current_detail_and_history_preserve_all_scoped_business_facts(
    reading_pg, channel
):
    client, factory, org_id = reading_pg
    async with scoped(factory, org_id) as db:
        assert not await db.scalar(
            text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user")
        )
        config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        config.set_main_option(
            "script_location", str(Path(__file__).resolve().parents[1] / "alembic")
        )
        config.set_main_option("path_separator", "os")
        assert (
            await db.scalar(text("SELECT version_num FROM alembic_version"))
            == ScriptDirectory.from_config(config).get_current_head()
        )
        assert await db.scalar(
            text(
                "SELECT relforcerowsecurity FROM pg_class WHERE oid='capture_reading_observations'::regclass"
            )
        )
        owner, observation = await seed(client, db, channel)
        db.add(
            Receipt(
                org_id=org_id,
                amount=Decimal("12.34"),
                received_on=date(2026, 11, 6),
                reference="Synthetic nonempty read-only control",
            )
        )
        await db.commit()
        before = await business_snapshot(db, org_id)
        assert before["receipts"]["count"] == 1
        for suffix in ["/current", "/" + observation.id, ""]:
            response = await client.get(base(owner, channel) + suffix)
            assert response.status_code == 200, response.text
        assert await business_snapshot(db, org_id) == before


@pytest.mark.parametrize("column", ["profile_json", "page_facts_json"])
async def test_native_sql_byte_projection_never_parses_oversized_utf8(
    reading_pg, monkeypatch, column
):
    from app.services import capture_reading

    client, factory, org_id = reading_pg
    async with scoped(factory, org_id) as db:
        owner, observation = await seed(client, db)
        value = json.dumps({"unpublished": "🧾" * 100}, ensure_ascii=False)
        value += " " * (METADATA_BYTES - 1 - len(value))
        assert len(value) < METADATA_BYTES < len(value.encode())
        setattr(observation, column, value)
        await db.commit()
        before = await stored(db, owner, observation)
        called = []

        def must_not_parse(_row):
            called.append(True)
            raise AssertionError("Oversized stored JSON reached the facts parser")

        monkeypatch.setattr(capture_reading, "_facts_for_row", must_not_parse)
        current = await client.get(base(owner, "upload") + "/current")
        assert current.status_code == 200 and current.json() is None
        for suffix in ["/" + observation.id, ""]:
            response = await client.get(base(owner, "upload") + suffix)
            assert response.status_code == 409, response.text
            assert response.json()["code"] == "reading_observation_unavailable"
        assert called == []
        assert await stored(db, owner, observation) == before


async def test_native_formatted_profile_keeps_existing_overall_allowance(reading_pg):
    client, factory, org_id = reading_pg
    async with scoped(factory, org_id) as db:
        owner, observation = await seed(client, db)
        observation.profile_json += " " * 20000
        await db.commit()
        response = await client.get(base(owner, "upload") + "/current")
        assert response.status_code == 200 and response.json()["observation_id"] == observation.id


@pytest.mark.parametrize("channel", ["upload", "inbound"])
async def test_native_other_tenant_owner_and_observation_are_opaque(reading_pg, channel):
    client, factory, org_id = reading_pg
    async with scoped(factory, org_id) as db:
        owner, _ = await seed(client, db, channel)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as other:
        response = await other.post(
            "/api/v1/auth/register",
            json={
                "organization_name": "Synthetic other facts owner",
                "name": "Other owner",
                "email": f"other-facts-{uuid.uuid4().hex}@example.com",
                "password": "Synthetic-Other-Facts-Password-32-Only",
            },
        )
        assert response.status_code == 201, response.text
        other_org = response.json()["organization"]["id"]
        other.headers["Authorization"] = "Bearer " + response.json()["token"]["access_token"]
        async with scoped(factory, other_org) as db:
            foreign_owner, foreign_observation = await seed(other, db, channel)
    own_base, foreign_base = base(owner, channel), base(foreign_owner, channel)
    for path in [
        foreign_base + "/current",
        foreign_base + "/" + foreign_observation.id,
        own_base + "/" + foreign_observation.id,
    ]:
        hidden = await client.get(path)
        missing = await client.get(
            path.replace(foreign_owner.id, str(uuid.uuid4())).replace(
                foreign_observation.id, str(uuid.uuid4())
            )
        )
        assert hidden.status_code == missing.status_code == 404
        assert hidden.json() == missing.json()


@pytest.mark.parametrize("channel", ["upload", "inbound"])
async def test_native_committed_hint_widening_is_bounded_in_the_fresh_join(
    reading_pg, monkeypatch, channel
):
    from app.models.email_intake import InboundInvoice
    from app.models.extraction_run import ExtractionRun
    from app.services import capture_reading

    client, factory, org_id = reading_pg
    model = ExtractionRun if channel == "upload" else InboundInvoice
    async with scoped(factory, org_id) as writer:
        owner, observation = await seed(client, writer, channel)
        owner_id, observation_id = owner.id, observation.id
        original_hint = owner.reading_observation_json

    async with scoped(factory, org_id) as reader:
        stale_owner = await reader.get(model, owner_id)
        assert stale_owner.reading_observation_json == original_hint
        widened = original_hint + " " * (
            ReadingObservationSummary.byte_limit + 1 - len(original_hint.encode())
        )
        async with scoped(factory, org_id) as writer:
            await writer.execute(
                update(model)
                .where(model.org_id == org_id, model.id == owner_id)
                .values(reading_observation_json=widened)
            )
            await writer.commit()

        projected_hint_bytes = []
        original_execute = reader.execute

        async def inspect_join(statement, *args, **kwargs):
            result = await original_execute(statement, *args, **kwargs)
            if "owner_cache" in result.keys():
                frozen = result.freeze()
                projected_hint_bytes.extend(
                    None if row.owner_cache is None else len(row.owner_cache.encode())
                    for row in frozen()
                )
                return frozen()
            return result

        parsed = []
        original_parse = capture_reading._facts_for_row

        def must_not_parse(row):
            parsed.append(row.id)
            raise AssertionError("A widened current hint reached the facts parser")

        monkeypatch.setattr(reader, "execute", inspect_join)
        monkeypatch.setattr(capture_reading, "_facts_for_row", must_not_parse)
        assert (
            await capture_reading.current_observation(reader, stale_owner, channel=channel) is None
        )
        assert projected_hint_bytes == [None]
        assert parsed == []
        assert stale_owner.reading_observation_json == original_hint
        monkeypatch.setattr(capture_reading, "_facts_for_row", original_parse)

        for suffix in ["/" + observation_id, ""]:
            response = await client.get(base(stale_owner, channel) + suffix)
            assert response.status_code == 200, response.text
        async with scoped(factory, org_id) as verifier:
            assert (
                await verifier.scalar(
                    select(model.reading_observation_json).where(
                        model.org_id == org_id, model.id == owner_id
                    )
                )
                == widened
            )


@pytest.mark.parametrize("channel", ["upload", "inbound"])
@pytest.mark.parametrize("hint_kind", ["null", "formatted_boundary", "oversize", "utf8_oversize"])
async def test_native_inspection_owner_projects_metadata_without_hiding_history(
    reading_pg, channel, hint_kind
):
    from app.services import capture_reading

    client, factory, org_id = reading_pg
    async with scoped(factory, org_id) as db:
        owner, observation = await seed(client, db, channel)
        original_hint = owner.reading_observation_json
        if hint_kind == "null":
            hint = None
        elif hint_kind == "utf8_oversize":
            hint = json.dumps(
                {**json.loads(original_hint), "untrusted": "🧾" * 1000}, ensure_ascii=False
            )
            assert len(hint) < ReadingObservationSummary.byte_limit < len(hint.encode())
        else:
            size = ReadingObservationSummary.byte_limit + (hint_kind == "oversize")
            hint = original_hint + " " * (size - len(original_hint.encode()))
            assert len(hint.encode()) == size
        owner.reading_observation_json = hint
        owner.draft_json = "UNRELATED_FACTS_OWNER_BLOB" * 1024
        owner.reading_preparation_json = "UNRELATED_FACTS_OWNER_PREPARATION" * 1024
        await db.commit()
        before = await stored(db, owner, observation)

        current_owner = await capture_reading.inspection_owner(
            db, org_id, owner.id, channel=channel, include_current=True
        )
        assert set(current_owner._mapping) == {
            "id",
            "org_id",
            "status",
            "source_sha256",
            "sha256",
            "attempt_seq",
            "failure_seq",
            "reading_observation_json",
        }
        assert current_owner.reading_observation_json == (
            hint if hint_kind == "formatted_boundary" else None
        )
        current = await capture_reading.current_observation(db, current_owner, channel=channel)
        if hint_kind == "formatted_boundary":
            assert str(current.observation_id) == observation.id
        else:
            assert current is None

        historical_owner = await capture_reading.inspection_owner(
            db, org_id, owner.id, channel=channel, include_current=False
        )
        assert set(historical_owner._mapping) == {"id", "org_id"}
        for suffix in ["/" + observation.id, ""]:
            response = await client.get(base(owner, channel) + suffix)
            assert response.status_code == 200, response.text
            assert "UNRELATED_FACTS_OWNER" not in response.text
        assert await stored(db, owner, observation) == before
