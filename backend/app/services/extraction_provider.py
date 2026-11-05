"""Pluggable extraction providers — the seam so extraction can later use different
OCR, document-AI, or e-invoice parsers without touching the upload route or worker.

A provider turns raw upload bytes into a `ProviderResult`: a draft invoice plus
per-field `FieldCapture`s carrying confidence + provenance. Providers are tried in
registration order (deterministic-first); the first whose `handles()` returns True
wins. The existing parsers (e-invoice XML, PDF text/OCR, CSV, JSON) are wrapped as
the built-in providers; a future OCR/doc-AI vendor is one more `ExtractionProvider`
subclass + `register()` call.

Honest confidence (the request: "do not pretend extraction is always accurate"):
  - structured/deterministic (e-invoice XML, CSV, JSON): exact → confidence None
    (there is no probabilistic score; None means "exact", not "unknown").
  - PDF text-layer: 0.85 (digital text, but field roles are inferred).
  - PDF OCR (scanned image): 0.55 (genuinely uncertain).
A field below `LOW_CONFIDENCE_THRESHOLD`, or with a non-"extracted" status, is
flagged `low_confidence` so the human-review queue surfaces it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from dataclasses import field as dc_field
from decimal import Decimal

from app.schemas.image_reading import ImageReadingPreparation
from app.schemas.invoice import InvoiceCreate, ParsedInvoiceDraft
from app.services import capture_failures
from app.services.capture_failures import CaptureError

LOW_CONFIDENCE_THRESHOLD = Decimal("0.75")

# Per-method confidence for a well-read field. None = deterministic/exact.
_CONFIDENCE: dict[str, Decimal | None] = {
    "e-invoice-xml": None,
    "csv": None,
    "json": None,
    "text-layer": Decimal("0.85"),
    "ocr": Decimal("0.55"),
}

# The top-level header fields we record provenance for. `po_reference` (BT-13)
# joined them with backlog N1: it is captured from the document like the rest,
# so a reviewer must be able to see whether it was READ or is simply absent —
# the difference between "this supplier quoted no PO" and "we did not look".
_HEADER_FIELDS = (
    "invoice_number",
    "vendor_name",
    "issue_date",
    "due_date",
    "currency",
    "po_reference",
    "supplier_registration_number",
    "supplier_vat_id",
    "supplier_iban",
)

# The six line-item fields captured per line (E1.2) — one provenance row per
# (line_index, field), same honest confidence semantics as the header.
_OPTIONAL_LINE_AMOUNTS = ("captured_tax_amount", "captured_gross_amount")
_LINE_FIELDS = (
    "description",
    "category",
    "quantity",
    "unit_price",
    "amount",
    "tax_rate",
) + _OPTIONAL_LINE_AMOUNTS


@dataclass
class FieldCapture:
    """One captured field with honest provenance. `line_index` None = one of the
    header fields in `_HEADER_FIELDS`; n = one field of the draft's
    line_items[n] (E1.2)."""

    field: str
    status: str  # extracted | defaulted | missing
    original: str | None = None
    normalized: str | None = None
    confidence: Decimal | None = None
    provider: str = ""
    low_confidence: bool = False
    line_index: int | None = None

    @property
    def value(self) -> str | None:
        return self.normalized if self.normalized is not None else self.original


@dataclass
class ProviderResult:
    """What a provider returns: the draft plus per-field captures + metadata."""

    method: str
    draft: InvoiceCreate
    provider: str
    warnings: list[str] = dc_field(default_factory=list)
    fields: list[FieldCapture] = dc_field(default_factory=list)
    reading_preparation: ImageReadingPreparation | None = None


class ExtractionProvider(ABC):
    """The interface every extraction backend implements."""

    name: str = "provider"

    @abstractmethod
    def handles(self, filename: str, content: bytes) -> bool:
        """Whether this provider should parse the given file."""

    @abstractmethod
    def extract(self, filename: str, content: bytes) -> ProviderResult:
        """Parse the bytes into a draft + field captures. Raises ValueError on bad
        input (the caller turns that into a failed extraction run)."""


def _flag(status: str, confidence: Decimal | None) -> bool:
    """A field needs a human look if it wasn't cleanly extracted, or its score is
    below the threshold."""
    if status != "extracted":
        return True
    return confidence is not None and confidence < LOW_CONFIDENCE_THRESHOLD


def _line_flag(status: str, confidence: Decimal | None) -> bool:
    """The LINE-field flag rule deliberately differs from the header rule: a
    `defaulted` line field does NOT flag. `category` is structurally defaulted on
    nearly every line of every provider (no parser reads a category) and
    `tax_rate` is legitimately zero/filled on most — flagging them would put
    dozens of meaningless flags on every capture and drown the review queue's
    "needs a look" count. The `defaulted` status still shows honestly in the UI;
    only genuine uncertainty (a sub-threshold OCR/text score) or a truly missing
    value demands review."""
    if status == "missing":
        return True
    return confidence is not None and confidence < LOW_CONFIDENCE_THRESHOLD


def _header_captures(
    draft: InvoiceCreate,
    *,
    method: str,
    provider: str,
    defaulted_fields: frozenset[str] = frozenset(),
) -> list[FieldCapture]:
    """Build provenance for the header fields from a draft, for providers
    (OCR/e-invoice) whose underlying parser doesn't emit per-field provenance
    itself. Status is inferred from presence; confidence is per-method."""
    conf = _CONFIDENCE.get(method)
    values = {
        "invoice_number": draft.invoice_number,
        "vendor_name": draft.vendor_name,
        "issue_date": draft.issue_date.isoformat() if draft.issue_date else None,
        "due_date": draft.due_date.isoformat() if draft.due_date else None,
        "currency": draft.currency,
        "po_reference": draft.po_reference,
        "supplier_registration_number": draft.supplier_registration_number,
        "supplier_vat_id": draft.supplier_vat_id,
        "supplier_iban": draft.supplier_iban,
    }
    out: list[FieldCapture] = []
    for name in _HEADER_FIELDS:
        v = values[name]
        status = (
            "missing"
            if v in (None, "")
            else "defaulted"
            if name in defaulted_fields
            else "extracted"
        )
        out.append(
            FieldCapture(
                field=name,
                status=status,
                original=v if status == "extracted" else None,
                normalized=v,
                confidence=conf if status == "extracted" else None,
                provider=provider,
                low_confidence=_flag(status, conf),
            )
        )
    return out


def _line_captures(draft: InvoiceCreate, *, method: str, provider: str) -> list[FieldCapture]:
    """Line-item provenance (E1.2) for providers whose underlying parser emits a
    draft, not per-cell presence data (PDF text/OCR, e-invoice XML, image OCR).

    Per line, per field: description/quantity/unit_price/amount are present by
    construction → `extracted` with the method's confidence (None = exact for the
    structured e-invoice path, 0.85 text-layer, 0.55 OCR). `category` is never
    read by these parsers → `defaulted` (confidence None — a fill, not a
    probabilistic read). `tax_rate` 0 is the parsers' fill value → `defaulted`;
    a non-zero rate was read/inferred from the document → `extracted`. The line
    flag rule (`_line_flag`) applies — see its docstring for why `defaulted`
    line fields don't flag."""
    conf = _CONFIDENCE.get(method)
    out: list[FieldCapture] = []
    for i, li in enumerate(draft.line_items):
        amount = li.amount if li.amount is not None else (li.quantity * li.unit_price)
        values: dict[str, tuple[str | None, str]] = {
            "description": (li.description, "extracted"),
            "category": (li.category, "defaulted"),
            "quantity": (str(li.quantity), "extracted"),
            "unit_price": (str(li.unit_price), "extracted"),
            "amount": (str(amount), "extracted"),
            "tax_rate": (str(li.tax_rate), "extracted" if li.tax_rate else "defaulted"),
        }
        values.update(
            {
                name: (
                    str(getattr(li, name)) if getattr(li, name) is not None else None,
                    "extracted" if getattr(li, name) is not None else "missing",
                )
                for name in _OPTIONAL_LINE_AMOUNTS
            }
        )
        for name in _LINE_FIELDS:
            v, status = values[name]
            if v in (None, ""):
                status = "missing"
            field_conf = conf if status == "extracted" else None
            out.append(
                FieldCapture(
                    field=name,
                    status=status,
                    original=v,
                    normalized=v,
                    confidence=field_conf,
                    provider=provider,
                    low_confidence=False
                    if name in _OPTIONAL_LINE_AMOUNTS and status == "missing"
                    else _line_flag(status, field_conf),
                    line_index=i,
                )
            )
    return out


# --------------------------------------------------------------------------- #
# Built-in providers — thin wrappers over the existing deterministic parsers.
# --------------------------------------------------------------------------- #


class PdfProvider(ExtractionProvider):
    name = "pdf"

    def handles(self, filename: str, content: bytes) -> bool:
        return filename.lower().endswith(".pdf")

    def extract(self, filename: str, content: bytes) -> ProviderResult:
        from app.services import pdf_ocr

        try:
            parsed = pdf_ocr.parse_pdf(filename, content)
        except pdf_ocr.OcrTimedOut as exc:
            raise CaptureError(capture_failures.PROCESSING_TIMEOUT, str(exc)) from exc
        except pdf_ocr.OcrUnavailable as exc:
            raise CaptureError(
                capture_failures.CAPTURE_UNAVAILABLE,
                "PDF support is not installed on the server "
                f"(pdfplumber/pypdfium2/pytesseract + tesseract binary): {exc}",
            )
        # pdf_ocr reports the method (embedded XML / text-layer / OCR); label the
        # provider accordingly so the metadata reflects the real backend used.
        prov = {"e-invoice-xml": "pdf-einvoice", "text-layer": "pdf-text", "ocr": "pdf-ocr"}.get(
            parsed.method, "pdf"
        )
        return ProviderResult(
            method=parsed.method,
            draft=parsed.draft,
            provider=prov,
            warnings=list(parsed.warnings),
            fields=_header_captures(
                parsed.draft,
                method=parsed.method,
                provider=prov,
                defaulted_fields=frozenset(
                    field.field
                    for field in parsed.fields
                    if field.line_index is None and field.status == "defaulted"
                )
                if parsed.method in ("text-layer", "ocr")
                else frozenset(),
            )
            + _line_captures(parsed.draft, method=parsed.method, provider=prov),
        )


class EInvoiceProvider(ExtractionProvider):
    name = "einvoice"

    def handles(self, filename: str, content: bytes) -> bool:
        from app.services import einvoice

        lower = filename.lower()
        if lower.endswith(".pdf"):
            return False  # the PDF provider handles embedded XML
        return lower.endswith(".xml") or (
            not lower.endswith((".csv", ".json")) and einvoice.looks_like_einvoice(content)
        )

    def extract(self, filename: str, content: bytes) -> ProviderResult:
        from app.services import einvoice

        parsed = einvoice.parse_xml_bytes(content, filename)  # method "e-invoice-xml"
        return ProviderResult(
            method=parsed.method,
            draft=parsed.draft,
            provider=self.name,
            warnings=list(parsed.warnings),
            fields=_header_captures(parsed.draft, method=parsed.method, provider=self.name)
            + _line_captures(parsed.draft, method=parsed.method, provider=self.name),
        )


def _captures_from_provenance(fields, provider: str) -> list[FieldCapture]:
    """Adapt the CSV/JSON parser's FieldProvenance (5 header fields + per-line
    rows, no confidence) to FieldCapture. Deterministic → confidence stays None;
    a defaulted/missing HEADER field is still flagged so the review queue
    surfaces it, while line fields follow the `_line_flag` rule (a deterministic
    defaulted line cell — e.g. a computed amount — doesn't demand review)."""
    out: list[FieldCapture] = []
    for p in fields:
        line_index = getattr(p, "line_index", None)
        original = getattr(p, "original_value", None)
        flagged = (p.status != "extracted") if line_index is None else _line_flag(p.status, None)
        if p.field in _OPTIONAL_LINE_AMOUNTS and p.status == "missing" and p.original_value is None:
            flagged = False
        out.append(
            FieldCapture(
                field=p.field,
                status=p.status,
                original=original if original is not None else p.value,
                normalized=p.value,
                confidence=None,
                provider=provider,
                low_confidence=flagged,
                line_index=line_index,
            )
        )
    return out


class JsonProvider(ExtractionProvider):
    name = "json"

    def handles(self, filename: str, content: bytes) -> bool:
        return filename.lower().endswith(".json")

    def extract(self, filename: str, content: bytes) -> ProviderResult:
        from app.services import parser

        warnings: list[str] = []
        draft, prov = parser._parse_json(content, filename, warnings)
        if not draft.line_items:
            warnings.append("No line items were found in the file")
        return ProviderResult(
            method="json",
            draft=draft,
            provider=self.name,
            warnings=warnings,
            fields=_captures_from_provenance(prov, self.name),
        )


class CsvProvider(ExtractionProvider):
    name = "csv"

    def handles(self, filename: str, content: bytes) -> bool:
        return filename.lower().endswith(".csv")

    def extract(self, filename: str, content: bytes) -> ProviderResult:
        from app.services import parser

        warnings: list[str] = []
        draft, prov = parser._parse_csv(content, filename, warnings)
        if not draft.line_items:
            warnings.append("No line items were found in the file")
        return ProviderResult(
            method="csv",
            draft=draft,
            provider=self.name,
            warnings=warnings,
            fields=_captures_from_provenance(prov, self.name),
        )


class ImageProvider(ExtractionProvider):
    """OCR for scanned-image invoices (JPEG / PNG). Reuses the same text→draft
    heuristics as the PDF-OCR path. Degrades gracefully to a ValueError (→ a failed
    extraction run with a clear reason) when the OCR stack isn't installed, so an
    image upload is always *accepted* even where OCR can't run."""

    name = "image-ocr"

    def handles(self, filename: str, content: bytes) -> bool:
        return filename.lower().endswith((".png", ".jpg", ".jpeg"))

    def extract(self, filename: str, content: bytes) -> ProviderResult:
        from app.services import image_ocr, pdf_ocr

        reading = image_ocr.read(content)
        text = reading.text

        parsed = pdf_ocr.parse_invoice_text(text, filename, "ocr")
        return ProviderResult(
            method="ocr",
            draft=parsed.draft,
            provider=self.name,
            warnings=[*parsed.warnings, *reading.warnings],
            reading_preparation=reading.preparation,
            fields=_header_captures(
                parsed.draft,
                method="ocr",
                provider=self.name,
                defaulted_fields=frozenset(
                    field.field
                    for field in parsed.fields
                    if field.line_index is None and field.status == "defaulted"
                ),
            )
            + _line_captures(parsed.draft, method="ocr", provider=self.name),
        )


# Registration order = try order (deterministic-first): PDF, e-invoice XML, JSON,
# CSV, then image OCR.
_PROVIDERS: list[ExtractionProvider] = [
    PdfProvider(),
    EInvoiceProvider(),
    JsonProvider(),
    CsvProvider(),
    ImageProvider(),
]


def register(provider: ExtractionProvider, *, first: bool = False) -> None:
    """Add a provider. `first=True` gives it priority (e.g. a document-AI backend
    that should be tried ahead of the built-ins)."""
    if first:
        _PROVIDERS.insert(0, provider)
    else:
        _PROVIDERS.append(provider)


def providers() -> list[ExtractionProvider]:
    return list(_PROVIDERS)


def select(filename: str, content: bytes) -> ExtractionProvider:
    for p in _PROVIDERS:
        try:
            if p.handles(filename, content):
                return p
        except Exception:  # noqa: BLE001 - a provider's sniff must never break selection
            continue
    # A CaptureError (a ValueError subclass, so every existing `except ValueError`
    # behaves identically) states the classified cause at the site that knows it,
    # so the failed-capture worklist never has to guess it from the message.
    raise CaptureError(
        capture_failures.UNSUPPORTED_FORMAT,
        "Unsupported file type. Upload a .pdf, .xml, .csv, or .json file.",
    )


def run(filename: str, content: bytes) -> ProviderResult:
    """Select a provider and extract. Raises ValueError for unsupported/bad input."""
    return select(filename, content).extract(filename, content)


def to_draft(result: ProviderResult) -> ParsedInvoiceDraft:
    """Map a ProviderResult onto the ParsedInvoiceDraft the route/worker expect."""
    from app.schemas.invoice import FieldProvenance

    fields = [
        FieldProvenance(
            field=c.field,
            value=c.value,
            status=c.status,
            confidence=c.confidence,
            original_value=c.original,
            normalized_value=c.normalized,
            reviewed_value=None,
            provider=c.provider or result.provider,
            low_confidence=c.low_confidence,
            line_index=c.line_index,
        )
        for c in result.fields
    ]
    return ParsedInvoiceDraft(
        draft=result.draft,
        warnings=result.warnings,
        method=result.method,
        fields=fields,
        reading_preparation=result.reading_preparation,
    )
