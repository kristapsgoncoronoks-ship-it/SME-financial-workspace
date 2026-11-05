"""Facts-only local observations and source-owned, atomic publication.

Collectors cross the existing thread/native seams, never persist page text and
never infer an engine from a filename, provider label or current configuration.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import UUID, uuid4

from app.core.errors import ValidationError
from app.schemas.capture_reading import (
    METADATA_BYTES,
    PAGE_STATUSES,
    PROFILE_ID,
    FailureCode,
    LocalReaderInventory,
    PageReadingFacts,
    PageStartCheckpoint,
    PdfPageFact,
    ReadingObservationFacts,
    ReadingObservationHistory,
    ReadingObservationSummary,
    ReadingPolicyFact,
    ReadingProfileFacts,
    canonical,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_current: ContextVar[Collector | None] = ContextVar("capture_reading", default=None)
NATIVE_ENABLED_ENV = "CAPTURE_READING_FACTS"
NATIVE_PROFILE_ENV = "CAPTURE_READING_REQUESTED_PROFILE"


def text_facts(text: str, prefix: str) -> dict:
    encoded = text.encode("utf-8")
    return {
        f"{prefix}_chars": len(text),
        f"{prefix}_bytes": len(encoded),
        f"{prefix}_sha256": hashlib.sha256(encoded).hexdigest(),
    }


class Collector:
    def __init__(
        self, requested_profile_id: str | None = None, *, emit: Callable[[dict], None] | None = None
    ) -> None:
        from app.core.config import settings

        if requested_profile_id not in (None, PROFILE_ID):
            raise ValueError("Unknown local reading profile")
        self.requested_profile_id: Literal["builtin-local-default-v1"] | None = (
            PROFILE_ID if requested_profile_id is not None else None
        )
        self.timeout_seconds = settings.ocr_process_timeout_seconds
        self.emit = emit
        self.metadata_bytes = 0
        self.profile: ReadingProfileFacts | None = None
        self.page_state: Literal["observed", "not_observed", "not_applicable"] = "not_observed"
        self.page_count: int | None = None
        self.pages: dict[int, PdfPageFact] = {}
        self.started: dict[int, PageStartCheckpoint] = {}
        self.stream: tuple[int, str] | None = None
        self.failure_cause: (
            Literal["supervisor_timeout", "resource_or_native_exit", "result_incomplete"] | None
        ) = None

    def accept(self, frame: dict) -> None:
        if not isinstance(frame, dict) or len(frame) != 1:
            raise ValueError("Invalid reading facts frame")
        size = len(canonical({"reading": frame})) + 1
        if self.metadata_bytes + size > METADATA_BYTES:
            raise ValueError("Reading facts exceed the operation reporting limit")
        if "profile" in frame:
            profile = ReadingProfileFacts.model_validate(frame["profile"])
            if profile.requested_profile_id != self.requested_profile_id:
                raise ValueError("Native requested profile differs from the admitted request")
            if self.profile is not None:
                raise ValueError("Reading profile can only be observed once")
            self.profile = profile
        elif "page_count" in frame:
            value = PageReadingFacts(page_count=frame["page_count"])
            if self.page_count is not None:
                raise ValueError("Source page count can only be observed once")
            self.page_state, self.page_count = "observed", value.page_count
        elif "page_state" in frame:
            value = PageReadingFacts(page_observation_state=frame["page_state"])
            if self.page_count is not None or self.pages or self.started:
                raise ValueError("Observed pages cannot become unobserved")
            self.page_state = value.page_observation_state
        elif "page_started" in frame:
            start = PageStartCheckpoint.model_validate(frame["page_started"])
            if self.page_count is None or start.page > self.page_count:
                raise ValueError("Page checkpoint has no measured source page")
            if start.page in self.started or start.page in self.pages:
                raise ValueError("Page start can only be observed once")
            self.started[start.page] = start
        elif "page" in frame:
            page = PdfPageFact.model_validate(frame["page"])
            if self.page_count is None or page.page > self.page_count or page.page in self.pages:
                raise ValueError("Page fact is duplicate or has no measured source page")
            self.pages[page.page] = page
        elif "stream" in frame:
            stream = frame["stream"]
            if not isinstance(stream, dict) or set(stream) != {"bytes", "sha256"}:
                raise ValueError("Invalid complete stream facts")
            facts = PageReadingFacts(
                selected_stream_complete=True,
                selected_stream_bytes=stream["bytes"],
                selected_stream_sha256=stream["sha256"],
            )
            if self.stream is not None:
                raise ValueError("Complete selected stream can only be observed once")
            assert facts.selected_stream_bytes is not None
            assert facts.selected_stream_sha256 is not None
            self.stream = (facts.selected_stream_bytes, facts.selected_stream_sha256)
        elif "operation_failure" in frame:
            cause = frame["operation_failure"]
            if (
                cause not in ("supervisor_timeout", "resource_or_native_exit", "result_incomplete")
                or self.failure_cause is not None
            ):
                raise ValueError("Invalid or duplicate observed operation failure")
            self.failure_cause = cause
        else:
            raise ValueError("Unknown reading facts frame")
        self.metadata_bytes += size
        if self.emit is not None:
            self.emit({"reading": frame})

    def page_facts(self, *, failed: bool = False) -> PageReadingFacts:
        pages = dict(self.pages)
        if failed:
            for index, start in self.started.items():
                if index not in pages:
                    known_timeout = self.failure_cause == "supervisor_timeout"
                    pages[index] = PdfPageFact(
                        **start.model_dump(exclude={"state"}),
                        status="timed_out" if known_timeout else "outcome_unknown",
                        cause="supervisor_timeout"
                        if known_timeout
                        else (self.failure_cause or "unknown"),
                        selected_kind="ocr",
                    )
            # A measured source count proves existence, never completed text.
            if self.page_count is not None and self.page_count <= 50:
                for index in range(1, self.page_count + 1):
                    if index not in pages:
                        pages[index] = PdfPageFact(
                            page=index, status="not_attempted", cause="processing_stopped"
                        )
        selected = self.stream
        return PageReadingFacts(
            requested_profile_id=self.requested_profile_id,
            page_observation_state=self.page_state,
            page_count=self.page_count,
            pages=[pages[index] for index in sorted(pages)]
            if pages
            else ([] if self.page_count == 0 else None),
            selected_stream_complete=selected is not None,
            selected_stream_bytes=selected[0] if selected else None,
            selected_stream_sha256=selected[1] if selected else None,
        )


@contextmanager
def collect(
    requested_profile_id: str | None = None, *, emit: Callable[[dict], None] | None = None
) -> Iterator[Collector]:
    collector = Collector(requested_profile_id, emit=emit)
    token = _current.set(collector)
    try:
        yield collector
    finally:
        _current.reset(token)


def active() -> Collector | None:
    return _current.get()


def record(frame: dict) -> None:
    collector = active()
    if collector is not None:
        collector.accept(frame)


def operation_failed(
    cause: Literal["supervisor_timeout", "resource_or_native_exit", "result_incomplete"],
) -> None:
    collector = active()
    if collector is not None and collector.failure_cause is None:
        record({"operation_failure": cause})


def timeout_seconds(default: float) -> float:
    collector = active()
    return collector.timeout_seconds if collector is not None else default


@contextmanager
def engine_execution(engine) -> Iterator[dict[str, bool]]:
    """Observe a spawned reader in its isolated child without changing timeout.

    pytesseract calls timeout_manager only after its reading Popen succeeded.
    Installation probes do not enter it. Never patch a shared API/worker parent.
    """
    execution = {"started": False, "timed_out": False}
    adapter = getattr(engine, "pytesseract", None)
    manager = getattr(adapter, "timeout_manager", None)
    if (
        adapter is None
        or active() is None
        or os.environ.get(NATIVE_ENABLED_ENV) != "1"
        or not callable(manager)
    ):
        yield execution
        return

    @contextmanager
    def observed(*args, **kwargs):
        import subprocess

        process = args[0] if args else kwargs.get("proc")
        communicate = getattr(process, "communicate", None)
        patched = False
        had_own_method = "communicate" in getattr(process, "__dict__", {})
        if callable(communicate):
            execution["started"] = True

            def observed_communicate(*values, **options):
                try:
                    return communicate(*values, **options)
                except subprocess.TimeoutExpired:
                    execution["timed_out"] = True
                    raise

            try:
                process.communicate = observed_communicate
                patched = True
            except (AttributeError, TypeError):
                pass  # A missing witness remains unknown; retain native behavior.
        try:
            with manager(*args, **kwargs) as value:
                yield value
        finally:
            if patched:
                if had_own_method:
                    process.communicate = communicate
                else:
                    del process.communicate

    adapter.timeout_manager = observed
    try:
        yield execution
    finally:
        adapter.timeout_manager = manager


def native_environment() -> dict[str, str]:
    collector = active()
    if collector is None:
        return {}
    values = {NATIVE_ENABLED_ENV: "1"}
    if collector.requested_profile_id is not None:
        values[NATIVE_PROFILE_ENV] = collector.requested_profile_id
    return values


def complete_stream(text: str) -> None:
    facts = text_facts(text, "selected")
    record({"stream": {"bytes": facts["selected_bytes"], "sha256": facts["selected_sha256"]}})


def _engine_installation(
    engine: Any,
) -> tuple[
    str | None, list[str] | None, Literal["available", "truncated", "unavailable", "unknown"]
]:
    """Only called inside an already supervised native operation."""
    version: str | None = None
    languages: list[str] | None = None
    inventory: Literal["available", "truncated", "unavailable", "unknown"] = "unknown"
    try:
        observed_version = str(engine.get_tesseract_version())
        if len(observed_version) <= 120:
            version = observed_version
        installed = engine.get_languages()
        if isinstance(installed, list) and all(isinstance(name, str) for name in installed):
            inventory = "available"
            if len(installed) > 32 or any(len(name) > 32 for name in installed):
                inventory = "truncated"
            languages = sorted(name for name in installed if len(name) <= 32)[:32]
    except (OSError, RuntimeError):
        inventory = "unavailable"
    return version, languages, inventory


def observe_profile(
    adapter: Literal["pdf", "image"], *, used_engine=None, not_needed: bool = False
) -> None:
    collector = active()
    if collector is None or collector.profile is not None:
        return
    from app.services import filesec

    policies = []

    def policy(name, value, unit, scope):
        policies.append(ReadingPolicyFact(name=name, value=value, unit=unit, scope=scope))

    policy("tesseract_timeout", collector.timeout_seconds, "seconds", "per_tesseract_invocation")
    value: float
    if adapter == "pdf":
        from app.services import pdf_process

        for name, value, unit, scope in (
            ("pdf_wall", pdf_process.WALL_SECONDS, "seconds", "per_pdf_operation"),
            ("pdf_cpu", pdf_process.CPU_SECONDS, "seconds", "per_native_process"),
            (
                "pdf_address_space",
                filesec.RESOURCE_BYTES["pdf_parser_memory"],
                "bytes",
                "per_native_process",
            ),
            ("pdf_pages", 50, "pages", "per_pdf_operation"),
            ("pdf_render", 40_000_000, "pixels", "per_page_raster"),
            ("pdf_render_edge", 20_000, "pixels", "per_page_raster"),
            ("pdf_render_dpi", 300, "dpi", "per_page_raster"),
            (
                "decoded_text",
                filesec.RESOURCE_BYTES["pdf_decoded_text"],
                "bytes",
                "selected_text_aggregate",
            ),
            (
                "child_output",
                filesec.RESOURCE_BYTES["pdf_parser_output"],
                "bytes",
                "per_pdf_operation",
            ),
        ):
            policy(name, value, unit, scope)
        reader = Path(__file__).with_name("pdf_ocr.py")
        preprocessing = "selected-page-text-v1"
    else:
        from app.services import image_ocr

        for name, value, unit, scope in (
            ("image_source", filesec.IMAGE_OCR_SOURCE_PIXELS, "pixels", "per_image_source"),
            ("image_source_edge", filesec.IMAGE_OCR_SOURCE_EDGE, "pixels", "per_image_source"),
            ("image_raster", filesec.IMAGE_OCR_RASTER_PIXELS, "pixels", "per_image_raster"),
            ("image_raster_edge", filesec.IMAGE_OCR_RASTER_EDGE, "pixels", "per_image_raster"),
            (
                "image_address_space",
                filesec.RESOURCE_BYTES["image_ocr_memory"],
                "bytes",
                "per_native_process",
            ),
            ("image_file", filesec.RESOURCE_BYTES["image_ocr_temp_file"], "bytes", "per_file"),
            (
                "image_text",
                filesec.RESOURCE_BYTES["image_ocr_text"],
                "bytes",
                "selected_text_aggregate",
            ),
            (
                "image_output",
                filesec.RESOURCE_BYTES["image_ocr_output"],
                "bytes",
                "per_image_operation",
            ),
            (
                "image_wall",
                collector.timeout_seconds + image_ocr.DECODE_GRACE_SECONDS,
                "seconds",
                "per_image_operation",
            ),
            (
                "image_cpu",
                math.ceil(collector.timeout_seconds + image_ocr.DECODE_GRACE_SECONDS),
                "seconds",
                "per_native_process",
            ),
            (
                "image_preparation_grace",
                image_ocr.DECODE_GRACE_SECONDS,
                "seconds",
                "per_image_preparation",
            ),
        ):
            policy(name, value, unit, scope)
        reader = Path(__file__).with_name("image_ocr.py")
        preprocessing = "local-image-ocr-v1"
    version, languages = None, None
    inventory: Literal["not_probed", "available", "truncated", "unavailable", "unknown"] = (
        "not_probed"
    )
    if used_engine is not None:
        version, languages, inventory = _engine_installation(used_engine)
    profile = ReadingProfileFacts(
        requested_profile_id=cast(
            Literal["builtin-local-default-v1"] | None, collector.requested_profile_id
        ),
        effective_profile_id=PROFILE_ID,
        adapter=adapter,
        ocr_state="used"
        if used_engine is not None
        else ("not_needed" if not_needed else "unknown"),
        engine_name="tesseract" if used_engine is not None else None,
        engine_version=version,
        inventory_status=inventory,
        installed_languages=languages,
        reader_source_sha256=hashlib.sha256(reader.read_bytes()).hexdigest(),
        preprocessing_version=preprocessing,
        observed_at=datetime.now(UTC),
        policies=policies,
    )
    record({"profile": profile.model_dump(mode="json")})


async def current_summaries(
    db: AsyncSession, owners: list, *, channel: Literal["upload", "inbound"]
) -> dict[str, ReadingObservationSummary | None]:
    """Treat the small cache as a hint, never canonical source/attempt evidence.

    Corrupt/stale/foreign hints remain unknown without read mutation. Batches
    load at most20 bounded canonical records; every row is org+owner scoped.
    """
    from sqlalchemy import and_, literal, select

    from app.models.capture_reading import CaptureReadingObservation as Observation
    from app.models.email_intake import InboundInvoice
    from app.models.extraction_run import ExtractionRun

    result: dict[str, ReadingObservationSummary | None] = {owner.id: None for owner in owners}
    eligible = []
    for owner in owners:
        value = owner.reading_observation_json
        valid_states = (
            ("parsed", "saved", "failed")
            if channel == "upload"
            else ("pending", "failed", "confirmed", "discarded")
        )
        if owner.status not in valid_states or value is None or len(value) > 4096:
            continue
        try:
            cached = ReadingObservationSummary.model_validate_json(value)
        except ValueError:
            continue
        source_sha = owner.source_sha256 if channel == "upload" else owner.sha256
        attempt_seq = owner.attempt_seq if channel == "upload" else None
        if cached.source_sha256 != source_sha or cached.attempt_seq != attempt_seq:
            continue
        if owner.status == "failed" and cached.outcome != "failure":
            continue
        if owner.status in ("parsed", "saved", "pending") and cached.outcome != "success":
            continue
        if cached.outcome == "failure" and cached.source_failure_seq != owner.failure_seq:
            continue
        eligible.append((owner, cached))
    owner_column = (
        Observation.extraction_run_id if channel == "upload" else Observation.inbound_invoice_id
    )
    owner_model = ExtractionRun if channel == "upload" else InboundInvoice
    source_column = ExtractionRun.source_sha256 if channel == "upload" else InboundInvoice.sha256
    attempt_column = ExtractionRun.attempt_seq if channel == "upload" else literal(None)
    for offset in range(0, len(eligible), 20):
        batch = eligible[offset : offset + 20]
        scope = {owner.org_id for owner, _ in batch}
        if len(scope) != 1:
            raise ValueError("Current reading summary batches must share one tenant")
        with db.no_autoflush:
            rows = await db.execute(
                select(
                    Observation,
                    owner_model.status,
                    source_column,
                    attempt_column,
                    owner_model.failure_seq,
                    owner_model.reading_observation_json,
                )
                .join(
                    owner_model,
                    and_(owner_model.org_id == Observation.org_id, owner_model.id == owner_column),
                )
                .where(
                    Observation.org_id == next(iter(scope)),
                    owner_column.in_([owner.id for owner, _ in batch]),
                    Observation.id.in_([str(cached.id) for _, cached in batch]),
                )
                .execution_options(populate_existing=True)
            )
        records = {row[0].id: row for row in rows}
        for owner, cached in batch:
            record = records.get(str(cached.id))
            if record is None:
                continue
            row, current_state, current_sha, current_attempt, current_failure, current_cache = (
                record
            )
            if current_cache != owner.reading_observation_json:
                continue
            if current_sha != cached.source_sha256 or current_attempt != cached.attempt_seq:
                continue
            if current_state == "failed" and cached.outcome != "failure":
                continue
            if current_state in ("parsed", "saved", "pending") and cached.outcome != "success":
                continue
            if current_state not in (
                ("parsed", "saved", "failed")
                if channel == "upload"
                else ("pending", "failed", "confirmed", "discarded")
            ):
                continue
            if cached.outcome == "failure" and cached.source_failure_seq != current_failure:
                continue
            actual_owner = row.extraction_run_id if channel == "upload" else row.inbound_invoice_id
            if actual_owner != owner.id or row.org_id != owner.org_id:
                continue
            if any(
                value is not None and len(value) > METADATA_BYTES
                for value in (row.profile_json, row.page_facts_json)
            ):
                continue
            try:
                facts = _facts_for_row(row)
                canonical_summary = _summary(facts)
            except ValueError:
                continue
            source_sha = owner.source_sha256 if channel == "upload" else owner.sha256
            attempt_seq = owner.attempt_seq if channel == "upload" else None
            if (
                facts.source_sha256 == source_sha
                and facts.attempt_seq == attempt_seq
                and canonical_summary == cached
            ):
                result[owner.id] = canonical_summary
    return result


async def current_summary(
    db: AsyncSession, owner, *, channel: Literal["upload", "inbound"]
) -> ReadingObservationSummary | None:
    return (await current_summaries(db, [owner], channel=channel))[owner.id]


def _summary(facts: ReadingObservationFacts) -> ReadingObservationSummary:
    counts = None
    if facts.pages is not None:
        counts = dict.fromkeys(PAGE_STATUSES, 0)
        for page in facts.pages:
            counts[page.status] += 1
    profile = facts.profile
    return ReadingObservationSummary(
        id=facts.observation_id,
        source_sha256=facts.source_sha256,
        attempt_seq=facts.attempt_seq,
        source_failure_seq=facts.source_failure_seq,
        outcome=facts.outcome,
        failure_code=facts.failure_code,
        published_at=facts.created_at,
        profile_id=profile.effective_profile_id if profile else None,
        requested_profile_id=facts.requested_profile_id,
        adapter=profile.adapter if profile else None,
        profile_state="available"
        if profile
        else ("not_applicable" if facts.page_observation_state == "not_applicable" else "unknown"),
        page_count=facts.page_count,
        page_status_counts=counts,
        facts_sha256=hashlib.sha256(canonical(facts)).hexdigest(),
    )


async def publish(
    db: AsyncSession,
    owner,
    collector: Collector,
    *,
    channel: Literal["upload", "inbound"],
    success: bool,
    failure_code: str | None = None,
) -> ReadingObservationSummary:
    """Caller owns the already-fresh source/lease lock and atomic commit."""
    from app.models.capture_reading import CaptureReadingObservation

    pages = PageReadingFacts(
        **collector.page_facts(failed=not success).model_dump(exclude={"source_failure_seq"}),
        source_failure_seq=owner.failure_seq if not success else None,
    )
    facts = ReadingObservationFacts(
        **pages.model_dump(),
        observation_id=uuid4(),
        channel=channel,
        owner_id=owner.id,
        source_sha256=owner.source_sha256 if channel == "upload" else owner.sha256,
        attempt_seq=owner.attempt_seq if channel == "upload" else None,
        outcome="success" if success else "failure",
        failure_code=cast(FailureCode | None, failure_code),
        created_at=datetime.now(UTC),
        profile=collector.profile,
    )
    summary = _summary(facts)
    db.add(
        CaptureReadingObservation(
            id=str(facts.observation_id),
            org_id=owner.org_id,
            created_at=facts.created_at,
            extraction_run_id=owner.id if channel == "upload" else None,
            inbound_invoice_id=owner.id if channel == "inbound" else None,
            source_sha256=facts.source_sha256,
            attempt_seq=facts.attempt_seq,
            outcome=facts.outcome,
            failure_code=facts.failure_code,
            profile_json=canonical(facts.profile).decode() if facts.profile else None,
            page_facts_json=canonical(pages).decode(),
        )
    )
    owner.reading_observation_json = canonical(summary).decode()
    return summary


def _facts_for_row(row) -> ReadingObservationFacts:
    pages = (
        PageReadingFacts.model_validate_json(row.page_facts_json)
        if row.page_facts_json
        else PageReadingFacts()
    )
    return ReadingObservationFacts(
        **pages.model_dump(),
        observation_id=row.id,
        channel="upload" if row.extraction_run_id else "inbound",
        owner_id=row.extraction_run_id or row.inbound_invoice_id,
        source_sha256=row.source_sha256,
        attempt_seq=row.attempt_seq,
        outcome=row.outcome,
        failure_code=row.failure_code,
        created_at=row.created_at.replace(tzinfo=UTC)
        if row.created_at.tzinfo is None
        else row.created_at,
        profile=ReadingProfileFacts.model_validate_json(row.profile_json)
        if row.profile_json
        else None,
    )


async def history(
    db: AsyncSession,
    org_id: str,
    owner_id: str,
    *,
    channel: Literal["upload", "inbound"],
    cursor: str | None = None,
) -> ReadingObservationHistory:
    from sqlalchemy import and_, or_, select

    from app.models.capture_reading import CaptureReadingObservation as Observation

    owner_column = (
        Observation.extraction_run_id if channel == "upload" else Observation.inbound_invoice_id
    )
    statement = select(Observation).where(Observation.org_id == org_id, owner_column == owner_id)
    if cursor is not None:
        try:
            if len(cursor) > 128 or not cursor.isascii():
                raise ValueError
            raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
            value = json.loads(raw)
            if (
                not isinstance(value, list)
                or len(value) != 3
                or type(value[0]) is not int
                or value[0] != 1
                or type(value[1]) is not str
                or type(value[2]) is not str
            ):
                raise ValueError
            created, identity = datetime.fromisoformat(value[1]), str(UUID(value[2]))
            if created.utcoffset() is None:
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise ValidationError("Invalid reading history cursor", code="invalid_cursor") from None
        statement = statement.where(
            or_(
                Observation.created_at < created,
                and_(Observation.created_at == created, Observation.id < identity),
            )
        )
    rows = list(
        await db.scalars(
            statement.order_by(Observation.created_at.desc(), Observation.id.desc()).limit(21)
        )
    )
    more = len(rows) > 20
    selected = rows[:20]
    next_cursor = None
    if more:
        last = selected[-1]
        created = (
            last.created_at.replace(tzinfo=UTC)
            if last.created_at.tzinfo is None
            else last.created_at
        )
        next_cursor = (
            base64.urlsafe_b64encode(
                json.dumps([1, created.isoformat(), last.id], separators=(",", ":")).encode()
            )
            .decode()
            .rstrip("=")
        )
    return ReadingObservationHistory(
        items=[_summary(_facts_for_row(row)) for row in selected],
        has_more=more,
        next_cursor=next_cursor,
    )


def _local_inventory() -> LocalReaderInventory:
    """Native-only fixed installation/config probe; it never reads a document."""
    version, languages, inventory = None, None, "unavailable"
    try:
        import pytesseract
    except ImportError:
        pass
    else:
        version, languages, inventory = _engine_installation(pytesseract)
    profiles = []
    for adapter in ("pdf", "image"):
        with collect() as observed:
            observe_profile(cast(Literal["pdf", "image"], adapter))
        assert observed.profile is not None
        profiles.append(observed.profile)
    return LocalReaderInventory.model_validate(
        {
            "observed_at": datetime.now(UTC),
            "engine_version": version,
            "inventory_status": inventory,
            "installed_languages": languages,
            "default_profiles": profiles,
        }
    )


def local_inventory() -> LocalReaderInventory:
    """Reuse existing90s PDF supervisor/CPU/address-space/output bounds."""
    from app.services.pdf_process import run

    return LocalReaderInventory.model_validate(run("inventory", "local-inventory", b""))


if __name__ == "__main__":
    if sys.argv[1:] != ["--local-inventory"]:
        raise SystemExit("Usage: python -m app.services.capture_reading --local-inventory")
    sys.stdout.write(canonical(local_inventory()).decode("utf-8") + "\n")
