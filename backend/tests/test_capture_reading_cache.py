"""A cached current summary never grants source/attempt/tenant provenance."""

import json
from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.models.extraction_run import ExtractionRun
from app.services import (
    capture_failures,
    capture_reading,
    email_intake,
    extraction,
    image_ocr,
    jobs,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption", ["foreign_owner", "old_attempt", "malformed", "oversized", "noncanonical"]
)
async def test_current_summary_requires_canonical_owner_and_generation(
    auth_client, db_session, corruption
):
    owners = []
    for index in range(2):
        response = await auth_client.post(
            "/api/v1/invoices/upload",
            files={
                "file": (
                    f"cache{index}.csv",
                    f"description,amount\nSynthetic item {index},10.00\n".encode(),
                    "text/csv",
                )
            },
        )
        assert response.status_code == 202, response.text
        owners.append(response.json()["extraction_run_id"])
        await jobs.run_once(
            db_session, f"cache-reader-{index}", kinds=(extraction.UPLOAD_EXTRACT_KIND,)
        )
    first = await db_session.get(ExtractionRun, owners[0], populate_existing=True)
    second = await db_session.get(ExtractionRun, owners[1], populate_existing=True)
    original = first.reading_observation_json
    if corruption == "foreign_owner":
        first.reading_observation_json = second.reading_observation_json
    elif corruption == "old_attempt":
        first.attempt_seq += 1
    elif corruption == "malformed":
        first.reading_observation_json = "{invalid"
    elif corruption == "oversized":
        first.reading_observation_json = json.dumps(
            {**json.loads(original), "untrusted": "x" * 5000}
        )
    else:
        first.reading_observation_json = json.dumps(
            {**json.loads(original), "source_sha256": "f" * 64}
        )
    corrupted = first.reading_observation_json
    await db_session.commit()
    response = await auth_client.get(f"/api/v1/invoices/upload/{owners[0]}")
    assert response.status_code == 200, response.text
    assert response.json()["reading_observation"] is None
    await db_session.refresh(first)
    assert first.reading_observation_json == corrupted  # reads do not repair data
    visible = await auth_client.get(f"/api/v1/invoices/upload/{owners[1]}")
    assert (
        visible.status_code == 200
        and visible.json()["reading_observation"]["id"]
        == json.loads(second.reading_observation_json)["id"]
    )


@pytest.mark.asyncio
async def test_tied_publication_times_use_actual_generation_and_canonical_pointer(
    auth_client, db_session, monkeypatch
):
    class SameTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 5, 12, tzinfo=UTC)

    identities = iter(
        [UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"), UUID("00000000-0000-0000-0000-000000000001")]
    )
    monkeypatch.setattr(capture_reading, "datetime", SameTime)
    monkeypatch.setattr(capture_reading, "uuid4", lambda: next(identities))
    response = await auth_client.post(
        "/api/v1/invoices/upload",
        files={"file": ("tie.csv", b"description,amount\nSynthetic tie,10.00\n", "text/csv")},
    )
    run_id = response.json()["extraction_run_id"]
    await jobs.run_once(db_session, "tie-first", kinds=(extraction.UPLOAD_EXTRACT_KIND,))
    assert (await auth_client.post(f"/api/v1/invoices/upload/{run_id}/retry")).status_code == 202
    await jobs.run_once(db_session, "tie-second", kinds=(extraction.UPLOAD_EXTRACT_KIND,))
    current = await auth_client.get(f"/api/v1/invoices/upload/{run_id}")
    assert current.status_code == 200, current.text
    assert current.json()["reading_observation"]["id"] == "00000000-0000-0000-0000-000000000001"
    assert current.json()["reading_observation"]["attempt_seq"] == 1


@pytest.mark.asyncio
async def test_old_same_source_inbound_failure_cache_uses_actual_failure_counter(
    auth_client, db_session, monkeypatch
):
    me = (await auth_client.get("/api/v1/auth/me")).json()
    org_id = me["organization"]["id"]
    row = await email_intake.process_attachment(
        db_session,
        org_id,
        from_addr=None,
        subject=None,
        filename="failure.csv",
        content_type="text/csv",
        content=b"description,amount\nSynthetic failure,10.00\n",
    )
    await db_session.commit()

    async def failed(*args):
        raise capture_failures.CaptureError(
            capture_failures.UNREADABLE_SCAN, "Synthetic failed reader"
        )

    monkeypatch.setattr(image_ocr, "run_reading", failed)
    await email_intake.extract_inbound(db_session, row.id)
    old = row.reading_observation_json
    assert json.loads(old)["source_failure_seq"] == 1
    await email_intake.extract_inbound(db_session, row.id)
    assert json.loads(row.reading_observation_json)["source_failure_seq"] == row.failure_seq == 2
    row.reading_observation_json = old
    await db_session.commit()
    assert await capture_reading.current_summary(db_session, row, channel="inbound") is None


@pytest.mark.asyncio
async def test_existing_manual_and_region_changes_preserve_reading_observation(
    auth_client, db_session, monkeypatch
):
    from tests.test_capture_region import application, finish

    run, _, _, region, _, _ = await finish(auth_client, db_session, monkeypatch)
    await capture_reading.publish(
        db_session, run, capture_reading.Collector(), channel="upload", success=True
    )
    await db_session.commit()
    before = (await auth_client.get(f"/api/v1/invoices/upload/{run.id}")).json()[
        "reading_observation"
    ]
    manual = await auth_client.post(
        f"/api/v1/invoices/captures/{run.id}/review",
        json={"fields": [{"field": "vendor_name", "reviewed_value": "Synthetic manual supplier"}]},
    )
    assert manual.status_code == 200, manual.text
    region = (
        await auth_client.get(f"/api/v1/invoices/captures/{run.id}/region-reads/{region['id']}")
    ).json()
    applied = await auth_client.post(
        f"/api/v1/invoices/captures/{run.id}/region-reads/{region['id']}/apply",
        json=application(region),
    )
    assert applied.status_code == 200, applied.text
    after = (await auth_client.get(f"/api/v1/invoices/upload/{run.id}")).json()[
        "reading_observation"
    ]
    assert before is not None and before == after
