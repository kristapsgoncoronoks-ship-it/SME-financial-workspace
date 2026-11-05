"""Measured preparation belongs to the published reading, never a fake draft."""

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from app.core import storage
from app.models.email_intake import InboundInvoice
from app.models.expense import ExpenseItem
from app.models.extraction_run import ExtractionRun
from app.schemas.image_reading import ImageReadingPreparation
from app.services import capture_failures, documents, email_intake, extraction, filesec, image_ocr
from tests.test_image_ocr_process import image_bytes

TEXT = (
    "SYNTHETIC SUPPLIER\nInvoice Number: INV-PREP-1\nInvoice Date: 2026-05-01\n"
    "Service 1 10.00 10.00\nTotal EUR 10.00"
)


def reading_fixture(monkeypatch):
    content = image_bytes()
    monkeypatch.setattr(filesec, "IMAGE_OCR_RASTER_PIXELS", 32)
    monkeypatch.setattr(image_ocr, "_emit", lambda message: None)
    raster, preparation = image_ocr._prepare(content)
    raster.close()
    return content, image_ocr.ImageReading(TEXT, preparation)


async def admitted_upload(client, content):
    result = await client.post(
        "/api/v1/invoices/upload", files={"file": ("preparation.png", content, "image/png")}
    )
    assert result.status_code == 202, result.text
    return result.json()["extraction_run_id"]


@pytest.mark.parametrize("outcome", ["success", "resource", "timeout"])
async def test_upload_persists_effective_preparation_on_success_and_failure(
    auth_client, db_session, monkeypatch, outcome
):
    content, reading = reading_fixture(monkeypatch)
    run_id = await admitted_upload(auth_client, content)

    def controlled(_content):
        assert _content == content
        if outcome != "success":
            code = (
                capture_failures.IMAGE_PROCESSING_LIMIT
                if outcome == "resource"
                else capture_failures.PROCESSING_TIMEOUT
            )
            raise image_ocr.ImageOcrError(code, reading.preparation)
        return reading

    monkeypatch.setattr(image_ocr, "read", controlled)
    await extraction.extract_upload(db_session, run_id)
    row = await db_session.get(ExtractionRun, run_id)
    assert (
        ImageReadingPreparation.model_validate_json(row.reading_preparation_json)
        == reading.preparation
    )
    response = await auth_client.get(f"/api/v1/invoices/upload/{run_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["reading_preparation"] == reading.preparation.model_dump()
    if outcome == "success":
        assert body["status"] == "parsed" and body["draft"] is not None
        assert body["draft"]["reading_preparation"] == reading.preparation.model_dump()
        assert any(
            "reduced" in warning and "original" in warning for warning in body["draft"]["warnings"]
        )
    else:
        assert body["status"] == "failed" and body["draft"] is None and row.draft_json is None
        assert row.failure_code == (
            capture_failures.IMAGE_PROCESSING_LIMIT
            if outcome == "resource"
            else capture_failures.PROCESSING_TIMEOUT
        )
    assert await documents.load(documents.UPLOADS, row.org_id, row.source_sha256) == content


async def test_new_upload_attempt_clears_old_preparation_without_inventing_a_replacement(
    auth_client, db_session, monkeypatch
):
    content, reading = reading_fixture(monkeypatch)
    monkeypatch.setattr(image_ocr, "read", lambda content: reading)
    run_id = await admitted_upload(auth_client, content)
    await extraction.extract_upload(db_session, run_id)
    row = await db_session.get(ExtractionRun, run_id)
    previous = row.reading_preparation_json
    assert previous is not None
    result = await auth_client.post(f"/api/v1/invoices/upload/{run_id}/retry")
    assert result.status_code == 202, result.text
    await db_session.refresh(row)
    assert row.attempt_seq == 1 and row.reading_preparation_json is None and row.draft_json is None
    pending = await auth_client.get(f"/api/v1/invoices/upload/{run_id}")
    assert pending.json()["reading_preparation"] is None
    assert storage.sha256_hex(content) == row.source_sha256


@pytest.mark.parametrize("failed", [False, True])
async def test_inbound_preparation_is_fenced_published_and_read_back(
    auth_client, db_session, monkeypatch, failed
):
    content, reading = reading_fixture(monkeypatch)
    org_id = (await auth_client.get("/api/v1/auth/me")).json()["organization"]["id"]
    assert (
        await auth_client.put("/api/v1/modules/email_intake", json={"enabled": True})
    ).status_code == 200
    # Current email admission intentionally excludes images. Exercise the
    # worker's existing image-parser compatibility with an explicitly seeded
    # historical queue row, without widening that admission policy.
    sha, _ = await documents.store(documents.EMAIL_ATTACHMENTS, org_id, content, "image/png")
    row = InboundInvoice(
        org_id=org_id,
        received_at=datetime.now(UTC),
        filename="preparation.png",
        content_type="image/png",
        size=len(content),
        sha256=sha,
        status="queued",
    )
    db_session.add(row)
    await db_session.commit()

    def controlled(_content):
        if failed:
            raise image_ocr.ImageOcrError(
                capture_failures.IMAGE_PROCESSING_LIMIT, reading.preparation
            )
        return reading

    monkeypatch.setattr(image_ocr, "read", controlled)
    result = await email_intake.extract_inbound(db_session, row.id)
    assert result.get("status") == ("failed" if failed else "pending"), result
    await db_session.refresh(row)
    assert json.loads(row.reading_preparation_json) == reading.preparation.model_dump()
    result = await auth_client.get(f"/api/v1/email/inbox/{row.id}")
    assert result.status_code == 200
    assert result.json()["reading_preparation"] == reading.preparation.model_dump()
    if failed:
        assert result.json()["draft"] is None and row.draft_json is None
        assert (
            row.status == "failed" and row.failure_code == capture_failures.IMAGE_PROCESSING_LIMIT
        )
    else:
        assert (
            row.status == "pending"
            and result.json()["draft"]["reading_preparation"] == reading.preparation.model_dump()
        )
    assert await documents.load(documents.EMAIL_ATTACHMENTS, org_id, row.sha256) == content


async def test_email_image_admission_stays_rejected_without_preparation(
    auth_client, db_session, monkeypatch
):
    content, _ = reading_fixture(monkeypatch)
    org_id = (await auth_client.get("/api/v1/auth/me")).json()["organization"]["id"]
    row = await email_intake.process_attachment(
        db_session,
        org_id,
        from_addr=None,
        subject=None,
        filename="preparation.png",
        content_type="image/png",
        content=content,
    )
    await db_session.commit()
    assert row.status == "rejected" and row.reading_preparation_json is None
    assert row.failure_code == capture_failures.SECURITY_REJECTED
    with pytest.raises(storage.StorageError):
        await documents.load(documents.EMAIL_ATTACHMENTS, org_id, row.sha256)


async def test_receipt_response_discloses_preparation_and_never_saves_suggested_fields(
    auth_client, db_session, monkeypatch
):
    content, reading = reading_fixture(monkeypatch)
    monkeypatch.setattr(image_ocr, "read", lambda content: reading)
    assert (
        await auth_client.put("/api/v1/modules/expenses", json={"enabled": True})
    ).status_code == 200
    response = await auth_client.post(
        "/api/v1/expenses/receipt-scan", files={"file": ("preparation.png", content, "image/png")}
    )
    assert response.status_code == 200, response.text
    assert response.json()["method"] == "ocr"
    assert response.json()["reading_preparation"] == reading.preparation.model_dump()
    assert response.json()["warnings"] == reading.warnings
    assert await db_session.scalar(select(func.count()).select_from(ExpenseItem)) == 0


async def test_legacy_unknown_preparation_is_not_recomputed_on_get(
    auth_client, db_session, monkeypatch
):
    content = image_bytes()
    run_id = await admitted_upload(auth_client, content)

    def forbidden(*args, **kwargs):
        raise AssertionError("A read recomputed historical image preparation")

    monkeypatch.setattr(image_ocr, "read", forbidden)
    response = await auth_client.get(f"/api/v1/invoices/upload/{run_id}")
    assert response.status_code == 200 and response.json()["reading_preparation"] is None
    row = await db_session.get(ExtractionRun, run_id)
    assert row.reading_preparation_json is None
