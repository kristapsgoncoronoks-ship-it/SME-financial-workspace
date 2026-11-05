"""Canonical reads bind a fresh source even when the ORM identity map is older."""

import pytest

from app.models.extraction_run import ExtractionRun
from app.services import capture_reading, extraction, jobs
from tests.test_image_reading_metadata_pg import reading_pg as reading_pg
from tests.test_image_reading_metadata_pg import scoped

pytestmark = pytest.mark.asyncio


async def test_current_summary_rechecks_source_after_committed_real_retry(reading_pg):
    client, factory, org_id = reading_pg
    response = await client.post(
        "/api/v1/invoices/upload",
        files={
            "file": (
                "fresh-source.csv",
                b"description,amount\nSynthetic current source,10.00\n",
                "text/csv",
            )
        },
    )
    assert response.status_code == 202, response.text
    owner_id = response.json()["extraction_run_id"]
    async with scoped(factory, org_id) as worker:
        await jobs.run_once(worker, "fresh-cache-reader", kinds=(extraction.UPLOAD_EXTRACT_KIND,))
    async with scoped(factory, org_id) as reader:
        old = await reader.get(ExtractionRun, owner_id)
        assert old.status == "parsed" and old.attempt_seq == 0
        retried = await client.post(f"/api/v1/invoices/upload/{owner_id}/retry")
        assert retried.status_code == 202, retried.text
        assert await capture_reading.current_summary(reader, old, channel="upload") is None
