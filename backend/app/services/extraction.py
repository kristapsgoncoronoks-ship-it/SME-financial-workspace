"""Extraction-lineage service (Slice 5b).

Records one row per capture attempt at the parse choke point and links it to the
invoice when the reviewed draft is saved. Tenant-scoped by the caller's `org_id`.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from typing import cast

from sqlalchemy import CursorResult, case, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import ConflictError
from app.models.extraction_field import ExtractionField
from app.models.extraction_run import ExtractionRun

# Job kind for async direct-upload OCR (Stage B). Defined here (the capture
# service) so both the enqueuing route and the worker handler import it without a
# circular dependency on job_handlers.
UPLOAD_EXTRACT_KIND = "upload.extract"

log = logging.getLogger("invoiceiq.capture")

#: How long a parser thread may block waiting for one progress write to land
#: before giving up on it. A progress row is worth milliseconds, never a parse.
_PROGRESS_WRITE_TIMEOUT = 10.0


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def record_fields(
    db: AsyncSession, org_id: str, run_id: str, fields, *, commit: bool = True
) -> None:
    """Persist per-field provenance for a run (Slice 5f). `fields` are the parser's
    FieldProvenance items. `commit=False` joins the fenced result transaction."""

    def _clip(v):
        return v[:500] if v else None

    for f in fields:
        db.add(
            ExtractionField(
                org_id=org_id,
                extraction_run_id=run_id,
                field=f.field,
                line_index=getattr(f, "line_index", None),
                value=_clip(f.value),
                status=f.status,
                confidence=f.confidence,
                original_value=_clip(getattr(f, "original_value", None)),
                normalized_value=_clip(getattr(f, "normalized_value", None)),
                reviewed_value=_clip(getattr(f, "reviewed_value", None)),
                provider=(getattr(f, "provider", None) or None),
                low_confidence=bool(getattr(f, "low_confidence", False)),
            )
        )
    if fields and commit:
        await db.commit()


def pending_review_filters(org_id: str) -> tuple:
    """THE definition of "a capture pending human review": parsed, not yet saved
    to an invoice. Both the review-queue route and the composed home dashboard
    build their queries from this one tuple (WO-16) — two hand-written copies of
    the filter would be exactly the drift ADR-0023's projection rule forbids."""
    return (
        ExtractionRun.org_id == org_id,
        ExtractionRun.status == "parsed",
        ExtractionRun.invoice_id.is_(None),
    )


@dataclass
class ReviewQueueSummary:
    """Roll-up of the capture review queue (counts only, no amounts)."""

    pending: int
    low_confidence_fields: int


async def review_queue_summary(db: AsyncSession, org_id: str) -> ReviewQueueSummary:
    """Counts for the composed home dashboard: how many captures await review,
    and how many captured fields across them are flagged low-confidence (the
    same `ExtractionField.low_confidence` flag the queue lists per item)."""
    pending = int(
        await db.scalar(
            select(func.count()).select_from(ExtractionRun).where(*pending_review_filters(org_id))
        )
        or 0
    )
    low = int(
        await db.scalar(
            select(func.count())
            .select_from(ExtractionField)
            .join(ExtractionRun, ExtractionRun.id == ExtractionField.extraction_run_id)
            .where(
                ExtractionField.org_id == org_id,
                ExtractionField.low_confidence.is_(True),
                *pending_review_filters(org_id),
            )
        )
        or 0
    )
    return ReviewQueueSummary(pending=pending, low_confidence_fields=low)


async def fields_for_run(db: AsyncSession, org_id: str, run_id: str) -> list[ExtractionField]:
    # Header rows (line_index NULL) first, then line rows grouped per line —
    # NULLS FIRST explicitly, because SQLite and Postgres default differently.
    return list(
        await db.scalars(
            select(ExtractionField)
            .where(ExtractionField.org_id == org_id, ExtractionField.extraction_run_id == run_id)
            .order_by(ExtractionField.line_index.asc().nullsfirst(), ExtractionField.field.asc())
        )
    )


async def field_counts_for_runs(
    db: AsyncSession, org_id: str, run_ids: list[str]
) -> dict[str, tuple[int, int]]:
    """run_id → (total fields, low-confidence fields) for a set of runs in ONE
    grouped query (BE-012). A run with no field rows is simply absent — the
    caller's `.get(run_id, (0, 0))` says so."""
    if not run_ids:
        return {}
    rows = await db.execute(
        select(
            ExtractionField.extraction_run_id,
            func.count(),
            func.sum(case((ExtractionField.low_confidence.is_(True), 1), else_=0)),
        )
        .where(ExtractionField.org_id == org_id, ExtractionField.extraction_run_id.in_(run_ids))
        .group_by(ExtractionField.extraction_run_id)
    )
    return {run_id: (int(total), int(low or 0)) for run_id, total, low in rows}


async def record(
    db: AsyncSession,
    org_id: str,
    *,
    filename: str | None,
    sha256: str | None,
    method: str,
    status: str,
    field_count: int = 0,
    warning_count: int = 0,
    note: str | None = None,
) -> ExtractionRun:
    """Persist a capture attempt (no invoice yet). Commits."""
    run = ExtractionRun(
        org_id=org_id,
        source_filename=filename,
        source_sha256=sha256,
        method=method,
        status=status,
        field_count=field_count,
        warning_count=warning_count,
        note=(note[:2000] if note else None),
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


async def link_to_invoice(db: AsyncSession, org_id: str, run_id: str, invoice_id: str) -> bool:
    """Attach a still-unlinked capture run to the saved invoice (status→saved).
    Tenant-scoped; only an unlinked run in this org is touched. Does NOT commit —
    the caller commits with the invoice save. Returns whether a row was linked."""
    result = await db.execute(
        update(ExtractionRun)
        .where(
            ExtractionRun.id == run_id,
            ExtractionRun.org_id == org_id,
            ExtractionRun.invoice_id.is_(None),
            ExtractionRun.status == "parsed",
        )
        .values(invoice_id=invoice_id, status="saved")
    )
    return cast(CursorResult, result).rowcount > 0


async def list_for_invoice(db: AsyncSession, org_id: str, invoice_id: str) -> list[ExtractionRun]:
    return list(
        await db.scalars(
            select(ExtractionRun)
            .where(ExtractionRun.org_id == org_id, ExtractionRun.invoice_id == invoice_id)
            .order_by(ExtractionRun.created_at.asc())
        )
    )


async def invoice_ids_with_documents(
    db: AsyncSession, org_id: str, invoice_ids: list[str]
) -> set[str]:
    """Which of `invoice_ids` have >=1 linked capture with REAL stored bytes
    (`source_sha256 IS NOT NULL` — a manually-typed invoice with no captured
    file is not "vaulted"). One query over the whole set, never a loop
    calling `list_for_invoice` per invoice — the transport vertical's
    document-presence gate (R10, `BA_fleet_fuel.md` C8's `docs_index()`)
    needs exactly this "one-query set, avoids N+1" shape."""
    if not invoice_ids:
        return set()
    rows = await db.scalars(
        select(ExtractionRun.invoice_id).where(
            ExtractionRun.org_id == org_id,
            ExtractionRun.invoice_id.in_(invoice_ids),
            ExtractionRun.source_sha256.is_not(None),
        )
    )
    return {r for r in rows if r is not None}


# --------------------------------------------------------------------------- #
# Async direct-upload capture (Stage B): the parse/OCR runs on the WORKER tier,
# not in the web request. The route stores the bytes + creates a QUEUED run and
# enqueues UPLOAD_EXTRACT_KIND; `extract_upload` (below) parses off-tier and
# stores the draft; the client polls `get_capture` for the result.
# --------------------------------------------------------------------------- #


async def check_duplicate_upload(
    db: AsyncSession, org_id: str, sha256: str, *, override: bool
) -> None:
    """Advisory re-upload guard (E1.3): raises when byte-identical content was
    already captured in this org, unless the caller explicitly overrides.

    Fails CLOSED by default (blocks): unlike the fuzzy, amount/date/vendor
    duplicate signals in `app/services/duplicates.py` (E1.4), which are
    deliberately advisory-never-block, an exact SHA-256 match is a near-certain
    true duplicate (the same bytes, not merely a similar invoice). It is still
    weaker than the bank-statement re-import guard
    (`app/services/reconciliation.py::import_statement`, which has NO escape
    hatch at all): here the block is user-escapable via `override`, because a
    deliberate re-upload (retrying after a mistake, a demo, a genuinely
    duplicated real-world document) is a legitimate case invoices — unlike a
    bank statement — can have.

    A prior run whose `status == "failed"` captured nothing, so re-uploading
    the same bytes to try again is not "the same document already on file" —
    excluded from the match, and needs no override.
    """
    if override:
        return
    dup = await db.scalar(
        select(ExtractionRun)
        .where(
            ExtractionRun.org_id == org_id,
            ExtractionRun.source_sha256 == sha256,
            ExtractionRun.status != "failed",
        )
        .order_by(ExtractionRun.created_at.desc())
        .limit(1)
    )
    if dup is None:
        return
    when = dup.created_at.date().isoformat()
    fate = (
        "and it was saved to an invoice."
        if dup.status == "saved"
        else "and it's still pending review."
    )
    raise ConflictError(
        f"You already uploaded this file on {when} {fate} Upload it again if this is intentional.",
        code="duplicate_upload",
    )


async def start_capture(
    db: AsyncSession, org_id: str, *, filename: str | None, sha256: str, commit: bool = True
) -> ExtractionRun:
    """Create a queued capture. Pass commit=False to join caller-owned admission."""
    from app.services import capture_progress

    run = ExtractionRun(
        org_id=org_id,
        source_filename=(filename[:255] if filename else None),
        source_sha256=sha256,
        method="pending",
        status="queued",
        # WO-X: stamped at creation, so "waiting for a worker" is a state the
        # screen can name rather than the absence of one.
        stage=capture_progress.QUEUED,
    )
    db.add(run)
    if commit:
        await db.commit()
        await db.refresh(run)
    else:
        await db.flush()  # assign the capture ID before constructing its job intent
    return run


async def get_capture(
    db: AsyncSession, org_id: str, run_id: str, *, for_update: bool = False
) -> ExtractionRun | None:
    stmt = select(ExtractionRun).where(ExtractionRun.id == run_id, ExtractionRun.org_id == org_id)
    if for_update:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return await db.scalar(stmt)


def attempt_job_key(run: ExtractionRun) -> str:
    return f"upload-extract:{run.id}:{run.attempt_seq}"


async def has_pending_attempt(db: AsyncSession, run: ExtractionRun) -> bool:
    from app.models.job import Job

    keys = [attempt_job_key(run)]
    if run.attempt_seq == 0:
        keys.append(f"upload-extract:{run.id}")  # pre-fence initial admission
    return bool(
        await db.scalar(
            select(Job.id)
            .where(
                Job.org_id == run.org_id,
                Job.kind == UPLOAD_EXTRACT_KIND,
                Job.idempotency_key.in_(keys),
                Job.status.in_(("queued", "running")),
            )
            .limit(1)
        )
    )


async def clear_fields_for_retry(
    db: AsyncSession, org_id: str, run: ExtractionRun, *, discard_review: bool
) -> int:
    """Drop a capture's provenance rows so a retry can re-parse from scratch, and
    REFUSE when that would silently destroy a human's corrections.

    A retry deletes every `ExtractionField` for the run. For a failed capture
    that is exactly right — there is nothing there but a machine's first guess.
    For a capture someone has already reviewed it is not: the `reviewed_value`
    rows they typed go with the rest, and the previous guard did not cover it
    (it refused only once the run had become an invoice or was `saved`), so a
    capture that was parsed AND reviewed BUT NOT YET SAVED was in scope.

    The fix is deliberately NOT to preserve the corrections across the re-parse.
    A re-parse may use a different provider and produce a different field set,
    so re-applying an old correction could attach a human's decision to a field
    they never saw — a worse failure than losing it, because it looks like a
    decision. Discarding is the honest behaviour; what was missing is that the
    human must ask for it.

    Returns the number of corrections discarded (0 when there were none).
    """
    reviewed = int(
        await db.scalar(
            select(func.count())
            .select_from(ExtractionField)
            .where(
                ExtractionField.org_id == org_id,
                ExtractionField.extraction_run_id == run.id,
                ExtractionField.reviewed_value.is_not(None),
            )
        )
        or 0
    )
    if reviewed and not discard_review:
        raise ConflictError(
            f"This capture carries {reviewed} correction(s) someone has already made. "
            "Re-extracting replaces them with a fresh machine read. "
            "Retry with discard_review=true if that is what you want.",
            code="capture_has_review",
        )
    await db.execute(
        delete(ExtractionField).where(
            ExtractionField.org_id == org_id,
            ExtractionField.extraction_run_id == run.id,
        )
    )
    return reviewed


def _progress_sink(db: AsyncSession, run: ExtractionRun):
    """Progress uses a short, separate transaction and the same attempt fence.

    Parser threads may outlive a handler deadline. They must never use its
    session after cancellation, or write progress onto a newer attempt.
    """
    loop = asyncio.get_running_loop()
    factory = async_sessionmaker(db.bind, expire_on_commit=False, autoflush=False)
    org_id, run_id, attempt_seq = run.org_id, run.id, run.attempt_seq
    last: dict[str, object] = {}

    async def _write(stage: str, pages_done: int, pages_total: int | None) -> None:
        from app.services import capture_progress

        async with factory() as progress_db:
            current = await get_capture(progress_db, org_id, run_id, for_update=True)
            if (
                not sink.active
                or current is None
                or current.attempt_seq != attempt_seq
                or current.status != "queued"
                or current.invoice_id is not None
                or not capture_progress.is_forward(current.stage, stage)
            ):
                return
            current.stage = stage
            current.pages_done = pages_done
            if pages_total is not None:
                current.pages_total = pages_total
            await progress_db.commit()

    class Sink:
        active = True

        def close(self) -> None:
            self.active = False

        def __call__(self, stage: str, pages_done: int, pages_total: int | None) -> None:
            from app.services import capture_progress

            if not self.active or capture_progress.on_loop_thread():
                return
            seen = (stage, pages_done, pages_total)
            if last.get("seen") == seen:
                return
            last["seen"] = seen
            future = asyncio.run_coroutine_threadsafe(_write(stage, pages_done, pages_total), loop)
            try:
                future.result(timeout=_PROGRESS_WRITE_TIMEOUT)
            except TimeoutError:
                future.cancel()
                raise

    sink = Sink()
    return sink


async def extract_upload(db: AsyncSession, run_id: str, attempt_seq: int = 0) -> dict:
    """Parse off-lock; publish exactly once for the job's durable generation.

    A duplicate/reclaimed job can spend CPU, but cannot publish twice. Generation
    zero is the only meaning of a legacy payload, never "whatever is current".
    """
    from app.services import (
        capture_failures,
        capture_memory,
        capture_pages,
        capture_progress,
        documents,
        image_ocr,
    )
    from app.services.parser import parse_invoice_file

    run = await db.scalar(
        select(ExtractionRun)
        .where(ExtractionRun.id == run_id)
        .execution_options(populate_existing=True)
    )
    if run is None:
        return {"skipped": "run gone"}
    if run.attempt_seq != attempt_seq or run.status != "queued" or run.invoice_id is not None:
        return {"skipped": "attempt superseded or complete"}
    org_id = run.org_id
    read_failure = None
    try:
        content = await documents.load_capture(documents.UPLOADS, org_id, run.source_sha256)
    except capture_failures.CaptureError as exc:
        read_failure = exc
        content = None
    # No open read transaction/row lock during native parsing and rendering.
    await db.commit()
    draft = None
    failure_note = None
    failure_code = None
    reason = None
    prepared = None
    reading_preparation = None
    if read_failure is not None:
        failure_note = str(read_failure)
        failure_code = read_failure.code
        reason = read_failure.code
    elif content is None:
        failure_note = "stored upload missing"
        failure_code = capture_failures.STORED_FILE_MISSING
        reason = "missing bytes"
    else:
        sink = _progress_sink(db, run)
        token = capture_progress.install(sink)
        try:
            draft = await image_ocr.run_reading(
                parse_invoice_file, run.source_filename or "upload", content
            )
        except Exception as exc:  # noqa: BLE001 — deterministic parser failure
            failure_note = str(exc)[:2000] or exc.__class__.__name__
            failure_code = capture_failures.code_for(exc)
            reason = exc.__class__.__name__
            reading_preparation = image_ocr.preparation_for_error(exc)
        finally:
            sink.close()
            capture_progress.uninstall(token)
        if draft is not None and not run.preview_json:
            prepared = await capture_pages.prepare(content)

    # Refresh the identity map under the lock. An earlier SELECT is never proof
    # that this generation is still current after asynchronous/external work.
    run = await get_capture(db, org_id, run_id, for_update=True)
    if (
        run is None
        or run.attempt_seq != attempt_seq
        or run.status != "queued"
        or run.invoice_id is not None
    ):
        await db.rollback()
        return {"skipped": "attempt superseded or complete"}
    if draft is None:
        run.method = "failed"
        run.status = "failed"
        run.note = failure_note
        run.failure_code = failure_code
        run.failure_seq = (run.failure_seq or 0) + 1
        run.stage = capture_progress.DONE
        run.reading_preparation_json = (
            reading_preparation.model_dump_json() if reading_preparation is not None else None
        )
        await db.commit()
        return {"status": "failed", "reason": reason}

    if not run.preview_json and prepared is not None:
        await capture_pages.persist(db, run, prepared)
    draft.draft.extraction_run_id = run.id
    draft.extraction_run_id = run.id
    run.method = draft.method
    run.status = "parsed"
    run.failure_code = None
    run.field_count = len(draft.draft.line_items)
    run.warning_count = len(draft.warnings)
    run.note = draft.warnings[0] if draft.warnings else None
    run.draft_json = draft.model_dump_json()
    run.reading_preparation_json = (
        draft.reading_preparation.model_dump_json()
        if draft.reading_preparation is not None
        else None
    )
    run.stage = capture_progress.DONE
    await record_fields(db, org_id, run.id, draft.fields, commit=False)
    await capture_memory.observe_fields(
        db, org_id, draft.draft.vendor_name, [f.field for f in draft.fields], commit=False
    )
    await db.commit()
    return {"status": "parsed", "method": draft.method}
