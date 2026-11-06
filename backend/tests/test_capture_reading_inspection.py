"""Read-only full-document facts; synthetic publications are not recognizer proof."""

import json
import uuid
from datetime import UTC, datetime

import pytest

from app.models.capture_reading import CaptureReadingObservation
from app.models.email_intake import InboundInvoice
from app.models.extraction_run import ExtractionRun
from app.schemas.capture_reading import METADATA_BYTES, PROFILE_ID, ReadingProfileFacts
from app.services import capture_reading


async def seed(client, db, channel="upload"):
    me = (await client.get("/api/v1/auth/me")).json()
    org_id = me["organization"]["id"]
    if channel == "inbound":
        assert (
            await client.put("/api/v1/modules/email_intake", json={"enabled": True})
        ).status_code == 200
        owner = InboundInvoice(
            org_id=org_id,
            received_at=datetime.now(UTC),
            filename="synthetic-facts.pdf",
            sha256="e" * 64,
            status="pending",
        )
    else:
        owner = ExtractionRun(
            org_id=org_id,
            method="ocr",
            status="parsed",
            source_filename="synthetic-facts.pdf",
            source_sha256="e" * 64,
        )
    db.add(owner)
    await db.flush()
    collector = capture_reading.Collector(PROFILE_ID)
    profile = ReadingProfileFacts(
        requested_profile_id=PROFILE_ID,
        effective_profile_id=PROFILE_ID,
        adapter="pdf",
        ocr_state="used",
        engine_name="tesseract",
        engine_version="5.5.2",
        inventory_status="available",
        installed_languages=["eng", "lav"],
        reader_source_sha256="d" * 64,
    )
    collector.accept({"profile": profile.model_dump(mode="json")})
    collector.accept({"page_count": 2})
    for index, kind, count in [(1, "native", 10), (2, "ocr", 20)]:
        digest = ("a" if kind == "native" else "b") * 64
        collector.accept(
            {
                "page": {
                    "page": index,
                    "status": "native" if kind == "native" else "ocr_complete",
                    "selected_kind": kind,
                    f"{kind}_chars": count,
                    f"{kind}_bytes": count,
                    f"{kind}_sha256": digest,
                    "selected_chars": count,
                    "selected_bytes": count,
                    "selected_sha256": digest,
                    "source_width": 612.0,
                    "source_height": 792.0,
                    "source_unit": "points",
                    "elapsed_ms": 0,
                    **(
                        {
                            "raster_width": 600,
                            "raster_height": 800,
                            "render_scale": 1.0,
                            "render_dpi": 300,
                        }
                        if kind == "ocr"
                        else {}
                    ),
                }
            }
        )
    collector.accept({"stream": {"bytes": 31, "sha256": "c" * 64}})
    await capture_reading.publish(db, owner, collector, channel=channel, success=True)
    await db.commit()
    observation = await db.get(
        CaptureReadingObservation, json.loads(owner.reading_observation_json)["id"]
    )
    return owner, observation


def base(owner, channel):
    return (
        f"/api/v1/invoices/captures/{owner.id}/reading-observations"
        if channel == "upload"
        else f"/api/v1/email/inbox/{owner.id}/reading-observations"
    )


async def stored(db, owner, observation):
    await db.refresh(owner)
    await db.refresh(observation)
    return {
        "owner": {column.name: getattr(owner, column.name) for column in owner.__table__.columns},
        "observation": {
            column.name: getattr(observation, column.name)
            for column in observation.__table__.columns
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["upload", "inbound"])
async def test_current_and_historical_facts_read_the_same_canonical_publication_without_writes(
    auth_client, db_session, channel
):
    owner, observation = await seed(auth_client, db_session, channel)
    before = await stored(db_session, owner, observation)
    current = await auth_client.get(base(owner, channel) + "/current")
    assert current.status_code == 200, current.text
    value = current.json()
    assert (
        value["observation_id"] == observation.id
        and value["owner_id"] == owner.id
        and value["channel"] == channel
    )
    assert [page["status"] for page in value["pages"]] == ["native", "ocr_complete"]
    assert value["profile"]["resolved_language"] is None and value["profile"][
        "installed_languages"
    ] == ["eng", "lav"]
    assert value["pages"][1]["render_dpi"] == 300 and value["pages"][1]["raster_width"] == 600
    historical = await auth_client.get(base(owner, channel) + "/" + observation.id)
    assert historical.status_code == 200 and historical.json() == value
    assert len(current.content) <= METADATA_BYTES
    assert "raw_text" not in value and "words" not in value
    assert await stored(db_session, owner, observation) == before


@pytest.mark.asyncio
async def test_retry_generation_and_missing_canonical_hint_never_fall_back_to_history_order(
    auth_client, db_session
):
    owner, observation = await seed(auth_client, db_session)
    owner.attempt_seq += 1
    await db_session.commit()
    current = await auth_client.get(base(owner, "upload") + "/current")
    assert current.status_code == 200 and current.json() is None
    old = await auth_client.get(base(owner, "upload") + "/" + observation.id)
    assert old.status_code == 200 and old.json()["attempt_seq"] == 0
    owner.reading_observation_json = None
    await db_session.commit()
    current = await auth_client.get(base(owner, "upload") + "/current")
    assert current.status_code == 200 and current.json() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["upload", "inbound"])
async def test_foreign_observation_and_unknown_identity_are_opaque(
    auth_client, db_session, channel
):
    first, observation = await seed(auth_client, db_session, channel)
    second, _ = await seed(auth_client, db_session, channel)
    wrong_owner = await auth_client.get(base(second, channel) + "/" + observation.id)
    absent = await auth_client.get(base(second, channel) + "/" + str(uuid.uuid4()))
    malformed = await auth_client.get(base(first, channel) + "/not-a-uuid")
    assert wrong_owner.status_code == absent.status_code == malformed.status_code == 404
    assert wrong_owner.json() == absent.json()


@pytest.mark.asyncio
@pytest.mark.parametrize("column", ["profile_json", "page_facts_json"])
async def test_invalid_stored_facts_refuse_detail_and_history_without_echo_or_repair(
    auth_client, db_session, column
):
    owner, observation = await seed(auth_client, db_session)
    setattr(observation, column, '{"raw_text":"PRIVATE_UNPUBLISHED_WORD_STREAM"}')
    await db_session.commit()
    before = await stored(db_session, owner, observation)
    for suffix in ["/" + observation.id, ""]:
        response = await auth_client.get(base(owner, "upload") + suffix)
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "reading_observation_unavailable"
        assert "PRIVATE_UNPUBLISHED_WORD_STREAM" not in response.text
    current = await auth_client.get(base(owner, "upload") + "/current")
    assert current.status_code == 200 and current.json() is None
    assert await stored(db_session, owner, observation) == before


@pytest.mark.asyncio
async def test_existing_formatted_profile_above_sixteen_kib_remains_accepted(
    auth_client, db_session
):
    owner, observation = await seed(auth_client, db_session)
    observation.profile_json += " " * 20000
    await db_session.commit()
    assert 16 * 1024 < len(observation.profile_json.encode()) < METADATA_BYTES
    response = await auth_client.get(base(owner, "upload") + "/current")
    assert response.status_code == 200 and response.json()["observation_id"] == observation.id
    detail = await auth_client.get(base(owner, "upload") + "/" + observation.id)
    assert detail.status_code == 200


@pytest.mark.asyncio
async def test_utf8_oversize_is_bounded_before_json_parsing(auth_client, db_session, monkeypatch):
    owner, observation = await seed(auth_client, db_session)
    profile = json.loads(observation.profile_json)
    profile["engine_version"] = "🧾" * 120
    observation.profile_json = json.dumps(profile, ensure_ascii=False)
    facts = capture_reading._facts_for_row(observation)
    owner.reading_observation_json = capture_reading.canonical(
        capture_reading._summary(facts)
    ).decode()
    observation.profile_json += " " * (METADATA_BYTES - 1 - len(observation.profile_json))
    assert len(observation.profile_json) < METADATA_BYTES < len(observation.profile_json.encode())
    await db_session.commit()
    before = await stored(db_session, owner, observation)
    original = capture_reading._facts_for_row
    parsed = []

    def bounded(row):
        parsed.append(row.id)
        assert row.profile_json is None or len(row.profile_json.encode()) <= METADATA_BYTES
        assert row.page_facts_json is None or len(row.page_facts_json.encode()) <= METADATA_BYTES
        return original(row)

    monkeypatch.setattr(capture_reading, "_facts_for_row", bounded)
    current = await auth_client.get(base(owner, "upload") + "/current")
    assert current.status_code == 200 and current.json() is None
    for suffix in ["/" + observation.id, ""]:
        response = await auth_client.get(base(owner, "upload") + suffix)
        assert response.status_code == 409, response.text
    assert parsed == []
    assert await stored(db_session, owner, observation) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["upload", "inbound"])
@pytest.mark.parametrize("column", ["profile_json", "page_facts_json"])
async def test_empty_stored_json_is_invalid_while_null_remains_legacy_unknown(
    auth_client, db_session, channel, column
):
    owner, observation = await seed(auth_client, db_session, channel)
    setattr(observation, column, None)
    facts = capture_reading._facts_for_row(observation)
    owner.reading_observation_json = capture_reading.canonical(
        capture_reading._summary(facts)
    ).decode()
    await db_session.commit()
    prefix = base(owner, channel)
    before_null = await stored(db_session, owner, observation)

    current = await auth_client.get(prefix + "/current")
    assert current.status_code == 200 and current.json() is not None
    assert current.json()["observation_id"] == observation.id
    if column == "profile_json":
        assert current.json()["profile"] is None
    else:
        assert current.json()["pages"] is None
        assert current.json()["page_observation_state"] == "not_observed"
    for suffix in ["/" + observation.id, ""]:
        response = await auth_client.get(prefix + suffix)
        assert response.status_code == 200, response.text
    assert await stored(db_session, owner, observation) == before_null

    # Keep the same legitimate NULL-era cache: a malformed empty string must
    # not acquire its meaning merely because it is also false in Python.
    setattr(observation, column, "")
    await db_session.commit()
    before_empty = await stored(db_session, owner, observation)
    current = await auth_client.get(prefix + "/current")
    assert current.status_code == 200 and current.json() is None
    for suffix in ["/" + observation.id, ""]:
        response = await auth_client.get(prefix + suffix)
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "reading_observation_unavailable"
    assert await stored(db_session, owner, observation) == before_empty
