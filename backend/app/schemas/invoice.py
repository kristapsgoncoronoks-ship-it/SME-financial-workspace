from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from app.core import bank_id
from app.models.invoice import InvoiceStatus
from app.schemas.dimensions import DimensionFields
from app.schemas.image_reading import ImageReadingPreparation
from app.schemas.received_vat import ReceivedVatBreakdown
from app.schemas.validation import ValidationFinding

CapturedIban = Annotated[str | None, BeforeValidator(bank_id.optional_iban)]


class LineItemIn(BaseModel):
    description: str = Field(min_length=1, max_length=500)
    category: str = Field(default="uncategorized", max_length=80)
    quantity: Decimal = Field(default=Decimal("1"), ge=0)
    unit_price: Decimal = Field(default=Decimal("0"), ge=0)
    amount: Decimal | None = Field(default=None, ge=0)
    tax_rate: Decimal = Field(default=Decimal("0"), ge=0, le=100)

    captured_tax_amount: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    captured_gross_amount: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )


class LineItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    description: str
    category: str
    quantity: Decimal
    unit_price: Decimal
    amount: Decimal
    tax_rate: Decimal

    tax_amount: Decimal | None = None
    gross_amount: Decimal | None = None
    captured_tax_amount: Decimal | None = None
    captured_gross_amount: Decimal | None = None
    source_amounts_match: bool | None = None


class InvoiceCreate(DimensionFields):
    # Either an existing vendor_id or a vendor_name (created/looked up on the fly).
    vendor_id: str | None = None
    vendor_name: str | None = Field(default=None, max_length=200)
    invoice_number: str = Field(min_length=1, max_length=120)
    issue_date: date
    due_date: date | None = None
    currency: str = Field(default="EUR", min_length=3, max_length=3)
    status: InvoiceStatus = InvoiceStatus.pending
    notes: str | None = None
    # EN-16931 BT-13 — the purchase-order reference printed on the SUPPLIER's
    # document. Free text: it is the buyer's own identifier, so no format may
    # be assumed or validated (backlog N1).
    po_reference: str | None = Field(default=None, max_length=60)
    supplier_registration_number: str | None = Field(default=None, max_length=80)
    supplier_vat_id: str | None = Field(default=None, max_length=80)

    source_filename: str | None = None
    supplier_iban: CapturedIban = Field(default=None, repr=False)

    # FX rate stated on the invoice (foreign-currency units per 1 EUR). Optional;
    # when absent, non-EUR totals are converted at the ECB reference rate.
    fx_rate: Decimal | None = Field(default=None, gt=0)
    # Slice 5b: the capture run this invoice was saved from (from the upload
    # draft); linked to the invoice on save. Absent for a manually-entered invoice.
    extraction_run_id: str | None = None
    # Loaded saved-review identity for capture confirmation. Required once a
    # selected-region result has been applied; optional for legacy captures.
    capture_review_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    line_items: list[LineItemIn] = Field(default_factory=list)


class InvoiceUpdate(DimensionFields):
    status: InvoiceStatus | None = None
    due_date: date | None = None
    notes: str | None = None
    po_reference: str | None = Field(default=None, max_length=60)


class InvoiceOut(DimensionFields):
    model_config = ConfigDict(from_attributes=True)
    id: str
    vendor_id: str
    invoice_number: str
    issue_date: date
    due_date: date | None
    currency: str
    status: InvoiceStatus
    subtotal: Decimal
    tax_amount: Decimal
    total: Decimal
    total_eur: Decimal | None = None
    fx_rate: Decimal | None = None
    fx_source: str | None = None
    validation_status: str = "none"
    # AP review-&-approval lifecycle state (additive on the LIST shape, WO-16 —
    # the approvals worklist filter renders it; the detail shape already carried
    # it since Phase 13, same `str | None` type).
    workflow_state: str | None = None
    source_filename: str | None
    # BT-13, on the LIST shape as well as the detail: matching an invoice to a
    # purchase order is something an approver does while scanning the queue,
    # not only after opening one row.
    po_reference: str | None = None
    supplier_registration_number: str | None = None
    supplier_vat_id: str | None = None


class InvoiceDetailOut(InvoiceOut):
    supplier_iban_masked: str | None = None
    vat_breakdown: ReceivedVatBreakdown
    vendor_name: str
    notes: str | None
    line_items: list[LineItemOut]
    validation_findings: list[ValidationFinding] = Field(default_factory=list)
    validated_by: str | None = None
    validated_at: datetime | None = None
    # AP settlement (Phase 13). `payment_status` is derived (paid/partial/open/overdue).
    # (`workflow_state` moved to the InvoiceOut base — WO-16, same type/default.)
    amount_paid: Decimal = Decimal("0")
    paid_date: date | None = None
    outstanding: Decimal = Decimal("0")
    payment_status: str = "open"


class InvoiceListItemOut(InvoiceOut):
    """Worklist context, separate from the document/detail contract."""

    supplier_name: str | None = Field(
        default=None,
        description=(
            "Current authorized supplier-master name, not a frozen document identity. "
            "Null when the supplier is unavailable."
        ),
    )


class InvoiceListOut(BaseModel):
    items: list[InvoiceListItemOut]
    total: int
    page: int
    page_size: int


class FieldProvenance(BaseModel):
    """Per-field capture provenance: how an invoice field was obtained.

    Two scopes (E1.2): `line_index` None = one of the five header fields;
    `line_index = n` = one field of `line_items[n]` (description / category /
    quantity / unit_price / amount / tax_rate / captured tax / captured gross) — the same honest semantics
    apply to both.

    `status`: extracted (read from the source) | defaulted (filled in) | missing
    (absent, no default). Extraction is NOT assumed accurate — every field carries:
      • `confidence` (0..1) — set by probabilistic providers (OCR/AI); None for
        deterministic structured parsers (there is no score to report), so a None
        confidence means "exact", not "unknown".
      • `original_value` / `normalized_value` — the raw captured text vs. the cleaned
        value used downstream (equal for deterministic parsers today; the seam lets
        a provider report both).
      • `reviewed_value` — a human-corrected value, set when someone reviews the
        capture; None until then.
      • `provider` — which extraction provider produced the field.
      • `low_confidence` — a flag the review queue keys on (below threshold, or a
        non-extracted status)."""

    field: str
    value: str | None = None  # effective (normalized) value — kept for back-compat
    status: str = "extracted"
    confidence: Decimal | None = None
    original_value: str | None = None
    normalized_value: str | None = None
    reviewed_value: str | None = None
    provider: str | None = None
    low_confidence: bool = False
    line_index: int | None = None  # None = header field; n = line_items[n] (E1.2)


class ParsedInvoiceDraft(BaseModel):
    """Result of parsing an uploaded file — a *draft* the user confirms."""

    draft: InvoiceCreate
    warnings: list[str] = Field(default_factory=list)
    reading_preparation: ImageReadingPreparation | None = None
    # How the invoice was read: e-invoice-xml | text-layer | ocr | csv | json.
    method: str = "unknown"
    # Slice 5b: the recorded capture run; echo it back on save to link the lineage.
    extraction_run_id: str | None = None
    # Slice 5f: per-field provenance (populated by the deterministic parsers).
    fields: list[FieldProvenance] = Field(default_factory=list)


class DuplicateCandidateOut(BaseModel):
    invoice_id: str
    vendor_id: str
    vendor_name: str
    invoice_number: str
    issue_date: str | None = None
    total: str
    currency: str
    status: str


class ScoredCandidateOut(DuplicateCandidateOut):
    """E1.4: a same-vendor, DIFFERENT-number invoice with a close amount and
    issue date — advisory only, never a gate. `score` is 0-100, `reason` is a
    plain-English explanation for the review UI."""

    score: int
    reason: str


class DuplicateReportOut(BaseModel):
    """Same-number invoices split by supplier — `exact` (same supplier, likely a true
    duplicate) vs `cross_supplier` (a different supplier, usually a coincidence).
    `scored` (E1.4, additive) is a weaker, same-vendor amount/date-proximity signal
    for a DIFFERENT invoice number — empty unless the caller supplies vendor/total/
    currency/issue_date."""

    invoice_number: str
    exact: list[DuplicateCandidateOut] = Field(default_factory=list)
    cross_supplier: list[DuplicateCandidateOut] = Field(default_factory=list)
    scored: list[ScoredCandidateOut] = Field(default_factory=list)


class CaptureReviewItem(BaseModel):
    """One parsed-but-unconfirmed capture in the human-review queue."""

    extraction_run_id: str
    method: str
    status: str
    source_filename: str | None = None
    invoice_number: str | None = None
    vendor_name: str | None = None
    warning_count: int
    total_fields: int
    low_confidence_fields: int
    duplicate_candidate: bool = False
    created_at: datetime


class CaptureReviewQueueOut(BaseModel):
    items: list[CaptureReviewItem]
    total: int


class CaptureFailureItem(BaseModel):
    """One capture that failed, as an operator sees it (H-1).

    `summary` and `remediation` come from the closed vocabulary in
    `services/capture_failures.py::KINDS`, keyed by the stable `code`. `detail` is
    the raw library message — shown behind a disclosure for support, never as THE
    explanation. `document_retained` is the "what DID succeed" half of the record:
    the original is still stored and re-readable.
    """

    channel: str  # upload | email
    ref_id: str
    code: str
    summary: str
    remediation: str
    retry_helps: bool
    user_fixable: bool
    detail: str | None = None
    source_filename: str | None = None
    sha256: str | None = None
    document_retained: bool
    failed_at: datetime
    repeat_count: int = 1
    acknowledged_at: datetime | None = None
    acknowledged_by: str | None = None
    acknowledgement_note: str | None = None


class CaptureFailureGroup(BaseModel):
    """Repeats of one cause, so a systemic breakage reads as a single line."""

    code: str
    summary: str
    remediation: str
    count: int
    unacknowledged: int


class CaptureFailureWorklistOut(BaseModel):
    items: list[CaptureFailureItem]
    groups: list[CaptureFailureGroup] = Field(default_factory=list)
    total: int
    unacknowledged: int


class BulkOutcomeOut(BaseModel):
    """What happened to ONE record in a batch. `result` is applied | skipped |
    failed; a SKIP is an ordinary outcome carrying a reason, not a failure —
    burying "already acknowledged" in an error count teaches operators to ignore
    error counts."""

    ref_id: str
    result: str
    reason: str | None = None


class BulkAcknowledgeItem(BaseModel):
    channel: str = Field(min_length=1, max_length=12)
    ref_id: str = Field(min_length=1, max_length=64)


class BulkAcknowledgeIn(BaseModel):
    """Acknowledge many failed captures at once.

    `agreed_count` is the number of records the client DISPLAYED when the human
    clicked. If it disagrees with what the server received, the list moved under
    them and the whole batch is refused rather than applied to a set they never
    saw. Omit it (null) only for a script, which has no displayed list to protect.

    `selection` records HOW the records were chosen. `filter` is accepted here
    because acknowledging is reversible; it is refused for anything that is not.
    """

    items: list[BulkAcknowledgeItem] = Field(min_length=1)
    agreed_count: int | None = Field(default=None, ge=0)
    selection: str = Field(default="explicit")
    note: str | None = Field(default=None, max_length=1000)


class BulkAcknowledgeOut(BaseModel):
    applied: int
    skipped: int
    failed: int
    outcomes: list[BulkOutcomeOut] = Field(default_factory=list)
    # The ids that ACTUALLY changed, derived from the write rather than from what
    # was requested — the mechanical basis for an undo.
    applied_ids: list[str] = Field(default_factory=list)
    worklist: CaptureFailureWorklistOut


class BulkDeleteIn(BaseModel):
    """Delete many DRAFT invoices.

    `selection` must be `explicit`. Deleting cannot be undone, so "everything
    matching this filter" — a set the operator never enumerated — is refused
    outright (422 `bulk_filter_not_allowed`) rather than quietly narrowed."""

    invoice_ids: list[str] = Field(min_length=1)
    agreed_count: int | None = Field(default=None, ge=0)
    selection: str = Field(default="explicit")


class BulkDeleteOut(BaseModel):
    deleted: int
    skipped: int
    failed: int
    outcomes: list[BulkOutcomeOut] = Field(default_factory=list)
    # What each invoice looked like at the moment it was binned — frozen so a
    # reader of the audit trail need not go and look up a row whose values may
    # since have changed.
    deleted_records: list[dict] = Field(default_factory=list)


class DeleteInvoiceIn(BaseModel):
    """Confirming a consequential deletion.

    Carries the VERSION the client was shown, never the warning text itself: a
    client that could supply its own wording could have the audit trail record an
    acceptance of words nobody ever displayed. The server holds the text; the
    client only says which one it read.
    """

    acknowledged_warning_version: str | None = Field(default=None, max_length=40)


class BinnedInvoiceOut(BaseModel):
    """One row of the Trash screen."""

    invoice_id: str
    invoice_number: str | None = None
    vendor_name: str | None = None
    issue_date: str | None = None
    currency: str | None = None
    total: str | None = None
    deleted_at: str
    deleted_by: str | None = None
    # Whole days before the purge may take it. Floored at 0: a record the purge
    # has not collected yet is still restorable, and a negative number reads as
    # "already gone".
    days_left: int


class BinListOut(BaseModel):
    items: list[BinnedInvoiceOut] = Field(default_factory=list)
    total: int
    # Stated by the server rather than hardcoded in the UI, so the retention
    # promise has exactly one source.
    retention_days: int


class CaptureAcknowledgeIn(BaseModel):
    """Acknowledging a failed capture — an optional note saying what was decided.
    Who and when are taken from the session, never from the client."""

    note: str | None = Field(default=None, max_length=1000)


class FieldReviewIn(BaseModel):
    field: str = Field(min_length=1, max_length=40)
    reviewed_value: str = Field(max_length=500)
    # None targets the header row for `field`; n targets line_items[n]'s row (E1.2).
    line_index: int | None = Field(default=None, ge=0)


class CaptureReviewIn(BaseModel):
    """Human corrections for a capture's fields — records the reviewed value per
    field and clears its low-confidence flag."""

    fields: list[FieldReviewIn] = Field(default_factory=list)
    expected_review_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class UploadAccepted(BaseModel):
    """202 response to a direct upload — the parse/OCR is queued on the worker
    tier (Stage B). Poll GET /invoices/upload/{extraction_run_id} for the draft."""

    extraction_run_id: str
    status: str = "queued"  # queued | running


class UploadCommandResult(BaseModel):
    extraction_run_id: str | None
    status: str  # current capture status, or retired; admission is already committed


class ExtractionResult(BaseModel):
    """Poll response for an async upload capture. `draft` is populated once
    `status == "parsed"`; `error` carries the reason when `status == "failed"`."""

    extraction_run_id: str
    status: str  # queued | running | parsed | failed
    method: str | None = None
    draft: ParsedInvoiceDraft | None = None
    error: str | None = None
    reading_preparation: ImageReadingPreparation | None = None
    # WO-X — how far along the parse is, so a long capture can be told apart
    # from a stuck one. `stage` is a code from `capture_progress.STAGES`, or
    # None for a run recorded before the contract existed. `percent` is present
    # ONLY where something measured it (see capture_progress.percent); the
    # screen shows an indeterminate state rather than an invented number.
    stage: str | None = None
    pages_done: int = 0
    pages_total: int | None = None
    percent: int | None = None


class BatchUploadOutcome(BaseModel):
    """What happened to ONE file of a batch upload (WO-X).

    Every file gets a row, in the order it was sent. `code` is the machine-
    readable reason a file was not admitted — the same codes the single-file
    endpoint raises as HTTP errors, so the two paths cannot drift into
    disagreeing about why a file was refused."""

    filename: str
    accepted: bool
    extraction_run_id: str | None = None
    code: str | None = None
    message: str | None = None


class BatchUploadAccepted(BaseModel):
    """202 response to a batch upload: N files in, N outcomes out.

    The response is 202 whenever the request itself was well-formed, INCLUDING
    when every file was refused. A partial batch is the normal case — one
    duplicate among nine good invoices is a Tuesday — and collapsing it into a
    single request-level failure would throw away eight accepted captures to
    report one refusal."""

    accepted: int
    rejected: int
    outcomes: list[BatchUploadOutcome]


class FieldProvenanceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    field: str
    value: str | None = None
    status: str
    confidence: Decimal | None = None
    original_value: str | None = None
    normalized_value: str | None = None
    reviewed_value: str | None = None
    provider: str | None = None
    low_confidence: bool = False
    line_index: int | None = None  # None = header field; n = line_items[n] (E1.2)
    # E1.5 — the extraction learning loop: an ADVISORY suggestion derived from
    # past human corrections of this field for this vendor. `None` when there
    # is no correction history. Set by the route AFTER validation (not present
    # on the ORM row) — NEVER auto-applied to `value`/`reviewed_value`; a human
    # still applies it through the existing review-correction endpoint.
    suggested_value: str | None = None
    suggestion_observed_count: int | None = None
    suggestion_corrected_count: int | None = None


class ExtractionRunOut(BaseModel):
    """A capture-lineage entry — how a received invoice was read."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    method: str
    status: str
    source_filename: str | None = None
    source_sha256: str | None = None
    field_count: int
    warning_count: int
    note: str | None = None
    reading_preparation: ImageReadingPreparation | None = None
    created_at: datetime
    fields: list[FieldProvenanceOut] = []
