"""Bounded, facts-only provenance for an actually published local reading.

No page text, words or native diagnostic prose belongs in these DTOs.
"""

from __future__ import annotations

import json
from typing import Annotated, ClassVar, Final, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

PROFILE_ID: Final = "builtin-local-default-v1"
METADATA_BYTES = 128 * 1024
Count = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
PositiveFinite = Annotated[float, Field(strict=True, gt=0, allow_inf_nan=False)]
PageStatus = Literal[
    "native", "ocr_complete", "timed_out", "failed", "outcome_unknown", "not_attempted"
]
PAGE_STATUSES: tuple[PageStatus, ...] = (
    "native",
    "ocr_complete",
    "timed_out",
    "failed",
    "outcome_unknown",
    "not_attempted",
)
FailureCode = Literal[
    "unsupported_format",
    "malformed_document",
    "unreadable_scan",
    "image_processing_limit",
    "image_reading_busy",
    "capture_unavailable",
    "stored_file_missing",
    "stored_file_too_large",
    "stored_file_changed",
    "security_rejected",
    "processing_timeout",
    "internal_error",
    "unknown_failure",
]


def canonical(value: BaseModel | dict) -> bytes:
    data = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return json.dumps(
        data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


class _Facts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    byte_limit: ClassVar[int] = METADATA_BYTES

    @field_validator(
        "schema_version", "render_dpi", "documents_read", mode="before", check_fields=False
    )
    @classmethod
    def integer_literals_are_facts(cls, value):
        if value is not None and type(value) is not int:
            raise ValueError("Integer reading facts cannot be coerced")
        return value

    @model_validator(mode="after")
    def bounded_serialization(self) -> Self:
        if len(canonical(self)) > self.byte_limit:
            raise ValueError("Reading facts exceed their reporting limit")
        return self


class ReadingPolicyFact(_Facts):
    name: Literal[
        "tesseract_timeout",
        "pdf_wall",
        "pdf_cpu",
        "pdf_address_space",
        "pdf_pages",
        "pdf_render",
        "pdf_render_edge",
        "pdf_render_dpi",
        "decoded_text",
        "child_output",
        "image_source",
        "image_source_edge",
        "image_raster",
        "image_raster_edge",
        "image_address_space",
        "image_file",
        "image_text",
        "image_output",
        "image_wall",
        "image_cpu",
        "image_preparation_grace",
    ]
    value: Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]
    unit: Literal["seconds", "bytes", "pages", "pixels", "dpi"]
    scope: Literal[
        "per_tesseract_invocation",
        "per_pdf_operation",
        "per_native_process",
        "per_page_raster",
        "selected_text_aggregate",
        "per_image_source",
        "per_image_raster",
        "per_file",
        "per_image_operation",
        "per_image_preparation",
    ]


class ReadingProfileFacts(_Facts):
    byte_limit = 16 * 1024
    schema_version: Literal[1] = 1
    requested_profile_id: Literal["builtin-local-default-v1"] | None = None
    effective_profile_id: Literal["builtin-local-default-v1"] | None = None
    adapter: Literal["pdf", "image"]
    ocr_state: Literal["used", "not_needed", "unknown"]
    engine_name: Literal["tesseract"] | None = None
    engine_version: str | None = Field(default=None, max_length=120)
    inventory_status: Literal["not_probed", "available", "truncated", "unavailable", "unknown"] = (
        "not_probed"
    )
    installed_languages: list[Annotated[str, Field(max_length=32)]] | None = Field(
        default=None, max_length=32
    )
    language_mode: Literal["engine-default", "explicit", "unknown"] = "engine-default"
    resolved_language: str | None = Field(default=None, max_length=32)
    psm_mode: Literal["engine-default", "explicit", "unknown"] = "engine-default"
    resolved_psm: int | None = Field(default=None, strict=True, ge=0, le=13)
    oem_mode: Literal["engine-default", "explicit", "unknown"] = "engine-default"
    resolved_oem: int | None = Field(default=None, strict=True, ge=0, le=3)
    reader_source_sha256: Sha256 | None = None
    preprocessing_version: str | None = Field(
        default=None, max_length=64, pattern=r"^[\x20-\x7e]+$"
    )
    observed_at: AwareDatetime | None = None
    policies: list[ReadingPolicyFact] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def truthful_profile(self) -> Self:
        if len({policy.name for policy in self.policies}) != len(self.policies):
            raise ValueError("Reading policy keys must be unique")
        if self.ocr_state != "used" and self.engine_name is not None:
            raise ValueError("An unused OCR engine cannot be reported as observed")
        if self.inventory_status == "available" and self.installed_languages is None:
            raise ValueError("An available inventory must be observed")
        if self.observed_at is not None and self.observed_at.utcoffset() is None:
            raise ValueError("Profile observation time must be timezone-aware")
        return self


class PdfPageFact(_Facts):
    byte_limit = 1536
    page: int = Field(strict=True, ge=1, le=50)
    status: PageStatus
    cause: (
        Literal[
            "native_text_selected",
            "short_native_text",
            "engine_timeout",
            "supervisor_timeout",
            "classified_native_failure",
            "resource_or_native_exit",
            "result_incomplete",
            "processing_stopped",
            "unknown",
        ]
        | None
    ) = None
    selected_kind: Literal["native", "ocr"] | None = None
    native_chars: Count | None = None
    native_bytes: Count | None = None
    native_sha256: Sha256 | None = None
    ocr_chars: Count | None = None
    ocr_bytes: Count | None = None
    ocr_sha256: Sha256 | None = None
    selected_chars: Count | None = None
    selected_bytes: Count | None = None
    selected_sha256: Sha256 | None = None
    source_width: PositiveFinite | None = None
    source_height: PositiveFinite | None = None
    source_unit: Literal["points"] | None = None
    raster_width: int | None = Field(default=None, strict=True, ge=1, le=20_000)
    raster_height: int | None = Field(default=None, strict=True, ge=1, le=20_000)
    render_scale: PositiveFinite | None = None
    render_dpi: Literal[300] | None = None
    elapsed_ms: Count | None = None

    @model_validator(mode="after")
    def coherent_page(self) -> Self:
        for prefix in ("native", "ocr", "selected"):
            facts = [getattr(self, f"{prefix}_{suffix}") for suffix in ("chars", "bytes", "sha256")]
            if any(value is None for value in facts) and not all(value is None for value in facts):
                raise ValueError("Complete text facts must contain counts and hash together")
        if (self.source_width is None) != (self.source_height is None):
            raise ValueError("Source geometry must be measured together")
        if (self.raster_width is None) != (self.raster_height is None):
            raise ValueError("Raster geometry must be measured together")
        if self.raster_width is not None and self.raster_height is not None:
            if self.raster_width * self.raster_height > 40_000_000:
                raise ValueError("Raster exceeds existing per-page limit")
        if self.status in ("native", "ocr_complete"):
            expected = "native" if self.status == "native" else "ocr"
            if self.selected_kind != expected or self.selected_sha256 is None:
                raise ValueError("Completed pages must identify the complete selected reading")
        elif any(
            value is not None
            for value in (self.selected_chars, self.selected_bytes, self.selected_sha256)
        ):
            raise ValueError("An incomplete page cannot report complete selected output")
        return self


class PageStartCheckpoint(_Facts):
    byte_limit = 256
    page: int = Field(strict=True, ge=1, le=50)
    state: Literal["started"] = "started"
    source_width: PositiveFinite | None = None
    source_height: PositiveFinite | None = None
    source_unit: Literal["points"] | None = None
    raster_width: int | None = Field(default=None, strict=True, ge=1, le=20_000)
    raster_height: int | None = Field(default=None, strict=True, ge=1, le=20_000)
    render_dpi: Literal[300] | None = None


class PageReadingFacts(_Facts):
    requested_profile_id: Literal["builtin-local-default-v1"] | None = None
    source_failure_seq: Count | None = None
    page_observation_state: Literal["observed", "not_observed", "not_applicable"] = "not_observed"
    page_count: Count | None = None
    selected_stream_complete: bool = Field(default=False, strict=True)
    selected_stream_bytes: Count | None = None
    selected_stream_sha256: Sha256 | None = None
    pages: list[PdfPageFact] | None = Field(default=None, max_length=50)

    @model_validator(mode="after")
    def coherent_pages(self) -> Self:
        if self.pages is not None:
            indices = [page.page for page in self.pages]
            if indices != sorted(set(indices)):
                raise ValueError("Page facts must be unique and in source order")
            if self.page_count is None or any(index > self.page_count for index in indices):
                raise ValueError("Page facts require an observed source page count")
            if not self.pages and self.page_count != 0:
                raise ValueError("An empty page set must be actually observed empty")
        if self.selected_stream_complete:
            if self.selected_stream_bytes is None or self.selected_stream_sha256 is None:
                raise ValueError("A complete selected stream requires its count and hash")
            if self.selected_stream_bytes > 2 * 1024 * 1024:
                raise ValueError("Selected stream exceeds the existing text budget")
        elif self.selected_stream_bytes is not None or self.selected_stream_sha256 is not None:
            raise ValueError("An incomplete stream cannot have complete output facts")
        return self


class ReadingObservationFacts(PageReadingFacts):
    schema_version: Literal[1] = 1
    observation_id: UUID
    channel: Literal["upload", "inbound"]
    owner_id: UUID
    source_sha256: Sha256 | None = None
    attempt_seq: Count | None = None
    outcome: Literal["success", "failure"]
    failure_code: FailureCode | None = None
    created_at: AwareDatetime
    profile: ReadingProfileFacts | None = None


class ReadingObservationSummary(_Facts):
    byte_limit = 4 * 1024
    id: UUID
    source_sha256: Sha256 | None = None
    attempt_seq: Count | None = None
    source_failure_seq: Count | None = None
    outcome: Literal["success", "failure"]
    failure_code: FailureCode | None = None
    published_at: AwareDatetime
    profile_id: Literal["builtin-local-default-v1"] | None = None
    requested_profile_id: Literal["builtin-local-default-v1"] | None = None
    adapter: Literal["pdf", "image"] | None = None
    profile_state: Literal["available", "unknown", "not_applicable"] = "unknown"
    page_count: Count | None = None
    page_status_counts: dict[PageStatus, Count] | None = None
    facts_sha256: Sha256


class ReadingObservationHistory(_Facts):
    byte_limit = 96 * 1024
    items: list[ReadingObservationSummary] = Field(max_length=20)
    has_more: bool = Field(strict=True)
    next_cursor: str | None = Field(default=None, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class LocalReaderInventory(_Facts):
    """Fixed worker-local CLI report; not a capture or a live fleet registry."""

    byte_limit = 48 * 1024
    schema_version: Literal[1] = 1
    scope: Literal["local-worker-installation"] = "local-worker-installation"
    documents_read: Literal[0] = 0
    engine_invoked_for_reading: Literal[False] = False
    observed_at: AwareDatetime
    engine_name: Literal["tesseract"] = "tesseract"
    engine_version: str | None = Field(default=None, max_length=120)
    inventory_status: Literal["available", "truncated", "unavailable", "unknown"]
    installed_languages: list[Annotated[str, Field(max_length=32)]] | None = Field(
        default=None, max_length=32
    )
    default_profiles: list[ReadingProfileFacts] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def configuration_only(self) -> Self:
        if {profile.adapter for profile in self.default_profiles} != {"pdf", "image"}:
            raise ValueError("Local inventory must describe both existing local adapters")
        if any(
            profile.engine_name is not None or profile.ocr_state != "unknown"
            for profile in self.default_profiles
        ):
            raise ValueError("An installation probe cannot claim a reading used an engine")
        return self
