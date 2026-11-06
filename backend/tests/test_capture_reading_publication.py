"""Atomic owner/history publication and compatibility without external providers."""

import hashlib
import json

import pytest
from sqlalchemy import func, select

from app.core import storage
from app.models.capture_reading import CaptureReadingObservation
from app.models.extraction_run import ExtractionRun
from app.services import (
    capture_failures,
    capture_reading,
    documents,
    email_intake,
    extraction,
    extraction_provider,
    image_ocr,
    jobs,
)

CSV = b"description,amount\nSynthetic observation item,10.00\n"


async def upload(client):
    response = await client.post(
        "/api/v1/invoices/upload", files={"file": ("reading.csv", CSV, "text/csv")}
    )
    assert response.status_code == 202, response.text
    return response.json()["extraction_run_id"]


@pytest.mark.asyncio
async def test_legacy_adapter_stays_unknown_and_history_reads_only_summaries(
    auth_client, db_session
):
    run_id = await upload(auth_client)
    await jobs.run_once(db_session, "facts-test", kinds=(extraction.UPLOAD_EXTRACT_KIND,))
    response = await auth_client.get(f"/api/v1/invoices/upload/{run_id}")
    assert response.status_code == 200, response.text
    summary = response.json()["reading_observation"]
    assert summary["outcome"] == "success" and summary["profile_state"] == "unknown"
    assert summary["profile_id"] is None and summary["adapter"] is None
    assert summary["source_sha256"] == hashlib.sha256(CSV).hexdigest()
    history = await auth_client.get(f"/api/v1/invoices/captures/{run_id}/reading-observations")
    assert history.status_code == 200, history.text
    assert history.json() == {"items": [summary], "has_more": False, "next_cursor": None}
    assert "pages" not in summary and "profile" not in summary
    assert "Synthetic observation item" not in history.text
    result = await extraction.extract_upload(db_session, run_id)
    assert "skipped" in result
    assert await db_session.scalar(select(func.count()).select_from(CaptureReadingObservation)) == 1


@pytest.mark.asyncio
async def test_known_request_survives_before_adapter_failure_without_fake_effective_profile(
    auth_client, db_session
):
    from app.schemas.capture_reading import PROFILE_ID

    owner_id = await upload(auth_client)
    owner = await db_session.get(ExtractionRun, owner_id)
    storage.get_storage().put(
        storage.content_key(documents.UPLOADS, owner.org_id, owner.source_sha256),
        b"changed-own-test-bytes",
    )
    await jobs.run_once(
        db_session, "before-adapter-reader", kinds=(extraction.UPLOAD_EXTRACT_KIND,)
    )
    await db_session.refresh(owner)
    assert owner.status == "failed" and owner.failure_code == "stored_file_changed"
    record = await db_session.scalar(
        select(CaptureReadingObservation).where(
            CaptureReadingObservation.extraction_run_id == owner_id
        )
    )
    assert record.profile_json is None
    assert json.loads(record.page_facts_json)["requested_profile_id"] == PROFILE_ID
    summary = json.loads(owner.reading_observation_json)
    assert summary["requested_profile_id"] == PROFILE_ID
    assert (
        summary["profile_id"] is None
        and summary["adapter"] is None
        and summary["profile_state"] == "unknown"
    )


@pytest.mark.asyncio
async def test_retry_clears_current_summary_but_retains_immutable_history(auth_client, db_session):
    run_id = await upload(auth_client)
    await jobs.run_once(db_session, "first-reader", kinds=(extraction.UPLOAD_EXTRACT_KIND,))
    old = await auth_client.get(f"/api/v1/invoices/upload/{run_id}")
    old_summary = old.json()["reading_observation"]
    retry = await auth_client.post(f"/api/v1/invoices/upload/{run_id}/retry")
    assert retry.status_code == 202, retry.text
    queued = await auth_client.get(f"/api/v1/invoices/upload/{run_id}")
    assert queued.json()["reading_observation"] is None
    before = await auth_client.get(f"/api/v1/invoices/captures/{run_id}/reading-observations")
    assert before.json()["items"] == [old_summary]
    await jobs.run_once(db_session, "second-reader", kinds=(extraction.UPLOAD_EXTRACT_KIND,))
    history = await auth_client.get(f"/api/v1/invoices/captures/{run_id}/reading-observations")
    assert [item["attempt_seq"] for item in history.json()["items"]] == [1, 0]
    assert history.json()["items"][1] == old_summary


@pytest.mark.asyncio
async def test_failure_publishes_known_facts_without_a_fake_draft(
    auth_client, db_session, monkeypatch
):
    run_id = await upload(auth_client)

    async def fail(*args):
        capture_reading.record({"page_count": 2})
        capture_reading.record({"page_started": {"page": 1}})
        raise capture_failures.CaptureError(
            capture_failures.PROCESSING_TIMEOUT, "Synthetic timeout"
        )

    monkeypatch.setattr(image_ocr, "run_reading", fail)
    await jobs.run_once(db_session, "failed-reader", kinds=(extraction.UPLOAD_EXTRACT_KIND,))
    owner = await db_session.get(ExtractionRun, run_id, populate_existing=True)
    assert owner.status == "failed" and owner.draft_json is None
    summary = json.loads(owner.reading_observation_json)
    assert summary["outcome"] == "failure" and summary["failure_code"] == "processing_timeout"
    history = await db_session.scalar(
        select(CaptureReadingObservation).where(
            CaptureReadingObservation.extraction_run_id == run_id
        )
    )
    assert "text" not in json.loads(history.page_facts_json)
    assert [page["status"] for page in json.loads(history.page_facts_json)["pages"]] == [
        "outcome_unknown",
        "not_attempted",
    ]


@pytest.mark.asyncio
async def test_rollback_keeps_owner_fields_and_history_together(
    auth_client, db_session, monkeypatch
):
    run_id = await upload(auth_client)
    commit = db_session.commit

    async def fail_publication_commit():
        if any(isinstance(row, CaptureReadingObservation) for row in db_session.new):
            raise RuntimeError("Synthetic publication commit failed")
        await commit()

    monkeypatch.setattr(db_session, "commit", fail_publication_commit)
    with pytest.raises(RuntimeError, match="publication commit"):
        await extraction.extract_upload(db_session, run_id)
    await db_session.rollback()
    owner = await db_session.get(ExtractionRun, run_id, populate_existing=True)
    assert owner.status == "queued" and owner.draft_json is None
    assert owner.reading_observation_json is None
    assert await db_session.scalar(select(func.count()).select_from(CaptureReadingObservation)) == 0


@pytest.mark.asyncio
async def test_custom_legacy_ocr_provider_is_not_stamped_as_observed_tesseract(
    auth_client, db_session, monkeypatch
):
    original = extraction_provider.CsvProvider()

    class Legacy(extraction_provider.ExtractionProvider):
        name = "synthetic-legacy-provider"

        def handles(self, filename, content):
            return True

        def extract(self, filename, content):
            result = original.extract(filename, content)
            result.method, result.provider = "ocr", self.name
            return result

    monkeypatch.setattr(extraction_provider, "_PROVIDERS", [Legacy()])
    run_id = await upload(auth_client)
    await jobs.run_once(db_session, "legacy-reader", kinds=(extraction.UPLOAD_EXTRACT_KIND,))
    owner = await db_session.get(ExtractionRun, run_id, populate_existing=True)
    assert owner.status == "parsed" and owner.method == "ocr"
    summary = json.loads(owner.reading_observation_json)
    assert summary["profile_state"] == "unknown" and summary["adapter"] is None
    history = await db_session.scalar(
        select(CaptureReadingObservation).where(
            CaptureReadingObservation.extraction_run_id == run_id
        )
    )
    assert history.profile_json is None


@pytest.mark.asyncio
async def test_history_is_opaque_for_unknown_owner_and_rejects_invalid_cursor(
    auth_client, db_session
):
    run_id = await upload(auth_client)
    malformed = await auth_client.get(
        f"/api/v1/invoices/captures/{run_id}/reading-observations", params={"cursor": "bad!"}
    )
    assert malformed.status_code == 422, malformed.text
    foreign = await auth_client.get(
        "/api/v1/invoices/captures/00000000-0000-0000-0000-000000000000/reading-observations"
    )
    assert foreign.status_code == 404


@pytest.mark.asyncio
async def test_inbound_success_requires_existing_state_transition_to_publish_again(
    auth_client, db_session
):
    org_id = (await auth_client.get("/api/v1/auth/me")).json()["organization"]["id"]
    row = await email_intake.process_attachment(
        db_session,
        org_id,
        from_addr=None,
        subject=None,
        filename="inbound.csv",
        content_type="text/csv",
        content=CSV,
    )
    await db_session.commit()
    await jobs.run_once(db_session, "inbound-first", kinds=(email_intake.EXTRACT_KIND,))
    await db_session.refresh(row)
    original = row.reading_observation_json
    assert row.status == "pending"
    assert await email_intake.extract_inbound(db_session, row.id) == {"skipped": "status=pending"}
    await jobs.enqueue(
        db_session,
        email_intake.EXTRACT_KIND,
        {"inbound_id": row.id},
        org_id=org_id,
        idempotency_key=f"synthetic-duplicate:{row.id}",
    )
    await jobs.run_once(db_session, "inbound-duplicate", kinds=(email_intake.EXTRACT_KIND,))
    await db_session.refresh(row)
    assert row.status == "pending" and row.reading_observation_json == original
    assert await db_session.scalar(select(func.count()).select_from(CaptureReadingObservation)) == 1
