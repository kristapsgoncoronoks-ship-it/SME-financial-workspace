"""PDF ingestion with OCR fallback.

Strategy (deterministic-first, OCR only when needed):
  1. Try the PDF's embedded text layer (pdfplumber) — exact, fast, no OCR.
  2. If the page has little/no text (a scanned or image-only PDF), rasterise
     each page with pypdfium2 and run Tesseract OCR.
  3. Run a heuristic invoice parser over the extracted text to produce a
     *draft* invoice (vendor, number, date, line items, inferred VAT rate).

Everything here is best-effort and advisory: the result is a draft a human
confirms — exactly like the CSV/JSON path. The heavy libraries are imported
lazily so the rest of the app runs without them installed.

System dependency: the Tesseract binary (`tesseract-ocr`) must be on PATH for
the OCR fallback. The text-layer path needs only Python packages.
"""

from __future__ import annotations

import io
import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from app.core.config import settings
from app.schemas.invoice import FieldProvenance, InvoiceCreate, LineItemIn, ParsedInvoiceDraft

# Render at this DPI for OCR; 300 is the accuracy/speed sweet spot for Tesseract.
_OCR_DPI = 300
# Below this many characters of embedded text, assume the page is scanned.
_TEXT_LAYER_MIN_CHARS = 25
# Hard cap on pages we rasterise/OCR — bounds CPU/memory against a maliciously
# huge or decompression-bomb PDF (an invoice is a handful of pages, not 10,000).
_MAX_OCR_PAGES = 50

# A monetary token: any number ending in a 2-digit decimal group, either
# separator, optional sign/parentheses (credits) and thousands separators.
# e.g. 60.00  1,234.56  1.234,56  -12.00  (9.90)
_MONEY_RE = re.compile(r"[-(]?\d[\d.,]*[.,]\d{2}\)?")
# Legacy 2-decimal amount (kept for callers that want the simplest signal).
_AMOUNT_RE = _MONEY_RE

# Dates and times — masked out before money parsing so a transaction date like
# 01.06.2026 is never mistaken for the amount 01.06.
_DATE_RE = re.compile(
    r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|\d{1,2}[./ ][A-Za-z]{3,9}[./ ]\d{2,4})\b"
)
_TIME_RE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
_PERCENT_RE = re.compile(r"(?<![\w.,])[-+]?\d+(?:[.,]\d+)?\s*%")
_LEADING_DATE_RE = re.compile(r"^\s*(\d{4}-\d{2}-\d{2}|\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\b")

# A row is a summary/total row (not a line item) when, after stripping numbers,
# dates and punctuation, the remaining label is one of these.
_SUMMARY_LABELS = (
    "total",
    "subtotal",
    "sub total",
    "grand total",
    "net total",
    "gross total",
    "total due",
    "total net",
    "total gross",
    "amount due",
    "balance",
    "balance due",
    "vat",
    "tax",
    "to pay",
    "invoice total",
    "total excl",
    "total incl",
    "carried forward",
)

# Conservative local labels: match the complete label (plus an optional currency
# code), so a real service named "PVN konsultācija" remains an invoice item.
_LV_SUBTOTAL_LABELS = frozenset(("summa bez pvn", "starpsumma"))
_LV_TAX_LABELS = frozenset(("pvn",))
_LV_TOTAL_LABELS = frozenset(
    ("summa ar pvn", "kopā", "kopa", "kopā apmaksai", "kopa apmaksai", "apmaksai")
)
_LV_SUMMARY_LABELS = _LV_SUBTOTAL_LABELS | _LV_TAX_LABELS | _LV_TOTAL_LABELS

_INVOICE_NO_RE = re.compile(
    r"\b(?:invoice|rēķins|rekins|rēķina|rekina)\s*"
    r"(?:no\.?|number|nr\.?|#)\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9\-/]{1,40})",
    re.IGNORECASE,
)
_DATE_LABEL_RE = re.compile(
    r"(?:invoice\s*date|issue\s*date|rēķina\s*datums|rekina\s*datums|date)\s*[:#]?\s*"
    r"([0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{1,2}[./ ][A-Za-z]{3,9}[./ ][0-9]{2,4}|[0-9]{1,2}[./][0-9]{1,2}[./][0-9]{2,4})",
    re.IGNORECASE,
)
_CURRENCY = {"€": "EUR", "$": "USD", "£": "GBP"}
_CURRENCY_LABEL_RE = re.compile(
    r"^[ \t]*(?:currency|valūta|valuta)(?:[ \t]*[:#][ \t]*|[ \t]+)"
    r"([A-Z]{3})[ \t]*(?:[.;,][ \t]*)?\r?$",
    re.IGNORECASE | re.MULTILINE,
)
_SUPPLIER_LABEL_RE = re.compile(r"^supplier\s*:\s*(.*?)\s*$", re.IGNORECASE)
_TABLE_ROLE_RE = re.compile(
    r"(description|quantity|qty|unit\s+price|net\s+amount|vat\s+amount|tax\s+amount|"
    r"gross\s+amount|amount)(?:\s+|$)",
    re.IGNORECASE,
)
# Only complete numeric cells under explicit column roles. Keep the shared
# decimal money signal unchanged for unheaded invoices/receipts/statements.
_TABLE_NUMBER = r"(?:\d+(?:[.,]\d+)?|\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d{1,3}(?:\.\d{3})+(?:,\d+)?)"
_TABLE_NUMBER_RE = re.compile(rf"(?:[-+]?{_TABLE_NUMBER}|\({_TABLE_NUMBER}\))\Z")


@dataclass
class _RowReading:
    item: LineItemIn
    originals: dict[str, str | None]


class OcrUnavailable(RuntimeError):
    """Raised when the PDF/OCR stack (or Tesseract binary) is not installed."""


# What a finance operator reads when a route hits the budget (the exception's own
# text — budget and page — is for the log). No stack vocabulary, an instruction.
OCR_TIMED_OUT_DETAIL = (
    "Reading this file took longer than the server allows. Try again in a moment, "
    "or send a shorter or clearer scan."
)


class OcrTimedOut(RuntimeError):
    """One native Tesseract invocation exceeded `ocr_process_timeout_seconds`
    (STIR-P2-01). Distinct from `OcrUnavailable`: the stack IS installed; this
    document (or this worker's load) blew the per-page budget."""


def ocr_image_to_data(pytesseract, image, *, page: int | None = None) -> dict:
    """The ONE call site for `pytesseract.image_to_data`, with the runtime
    budget applied. pytesseract signals a timeout with a bare `RuntimeError`
    (its `timeout_manager`), which is re-raised here as `OcrTimedOut`;
    `TesseractError` (a real engine failure) passes through unchanged."""
    budget = settings.ocr_process_timeout_seconds
    try:
        return pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT, timeout=budget)
    except pytesseract.TesseractError:
        raise
    except RuntimeError as exc:
        where = f" on page {page}" if page is not None else ""
        raise OcrTimedOut(f"OCR exceeded {budget:g} seconds{where}") from exc


def _num(s: str) -> Decimal:
    """Normalise a single numeric/money token to Decimal, locale-aware.

    Handles 1,234.56 (US), 1.234,56 (EU), plain 1234.56, negatives and
    parenthesised credits. The decimal separator is taken to be the *last*
    '.'/',' when both appear; a lone thousands group (",ddd"/".ddd") is treated
    as thousands.
    """
    tok = s.strip()
    neg = tok.startswith("-") or tok.startswith("(")
    tok = tok.strip("()+-").replace(" ", "")
    if not tok:
        return Decimal("0")
    if "." in tok and "," in tok:
        if tok.rfind(".") > tok.rfind(","):
            tok = tok.replace(",", "")  # comma = thousands
        else:
            tok = tok.replace(".", "").replace(",", ".")  # dot = thousands
    elif "," in tok:
        after = tok.rsplit(",", 1)[1]
        if tok.count(",") == 1 and len(after) != 3:
            tok = tok.replace(",", ".")  # comma = decimal
        else:
            tok = tok.replace(",", "")  # comma = thousands
    try:
        value = Decimal(tok)
    except InvalidOperation:
        return Decimal("0")
    return -value if neg else value


def _mask(text: str) -> str:
    """Blank dates, times and percentages without shifting monetary positions."""

    def blank(m: re.Match) -> str:
        return " " * (m.end() - m.start())

    return _PERCENT_RE.sub(blank, _TIME_RE.sub(blank, _DATE_RE.sub(blank, text)))


def _split_leading_date(text: str) -> tuple[str, str]:
    m = _LEADING_DATE_RE.match(text)
    if not m:
        return "", text
    return m.group(1), text[m.end() :]


def _label_of(text: str) -> str:
    """The alphabetic label of a row: text minus numbers/dates/punctuation."""
    stripped = _MONEY_RE.sub(" ", _mask(text))
    stripped = re.sub(r"[\d%.,:;#()\-/€$£]", " ", stripped)
    return re.sub(r"\s+", " ", stripped).strip().lower()


def _is_summary_row(text: str) -> bool:
    # A transaction row starts with a date — never a totals row (guards against a
    # station literally named "Total …" or "Q8" being dropped).
    if _LEADING_DATE_RE.match(text):
        return False
    label = _label_of(text)
    if not label:
        return False
    if _localized_summary_label(text) in _LV_SUMMARY_LABELS:
        return True
    words = label.split()
    for s in _SUMMARY_LABELS:
        sw = s.split()
        # Label must START with the summary phrase and add at most 2 qualifier
        # words (e.g. "total excl vat"), so "total lyon est diesel" stays a line.
        if words[: len(sw)] == sw and (len(words) - len(sw)) <= 2:
            return True
    return False


def _localized_summary_label(text: str) -> str:
    from app.services.fx import CURRENCY_BY_CODE

    label = _label_of(text)
    # Strip only recognized currency codes at the label's edge, never PVN or an
    # arbitrary product token. Monetary values may appear before or after EUR.
    words = label.split()
    if words and words[-1].upper() in CURRENCY_BY_CODE:
        label = " ".join(words[:-1])
    return label


def _to_date(value: str) -> date | None:
    value = value.strip()
    for fmt in (
        "%Y-%m-%d",
        "%d.%m.%Y",
        "%d/%m/%Y",
        "%m/%d/%Y",
        "%d %b %Y",
        "%d %B %Y",
        "%d.%m.%y",
        "%d/%m/%y",
    ):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------- #
# Text extraction
# --------------------------------------------------------------------------- #
def _extract_text_layer(content: bytes) -> str:
    import pdfplumber

    with pdfplumber.open(io.BytesIO(content)) as pdf:
        from app.services import filesec

        if len(pdf.pages) > _MAX_OCR_PAGES:
            raise ValueError("PDF exceeds the 50-page processing limit.")
        parts = []
        used = 0
        for page in pdf.pages:
            text = page.extract_text() or ""
            used += len(text.encode("utf-8"))
            if used > filesec.RESOURCE_BYTES["pdf_decoded_text"]:
                raise ValueError("PDF decoded text exceeds the processing limit.")
            parts.append(text)
    return "\n".join(parts).strip()


def _reconstruct_lines(data: dict) -> str:
    """Rebuild visual rows from Tesseract word boxes.

    `image_to_string` reads multi-column / wide-gap tables column-by-column,
    which tears a table row (description | qty | unit | amount) into separate
    lines. We instead take the word bounding boxes and cluster them by their
    vertical position, so words sharing a row rejoin left-to-right — the row
    structure an invoice table needs.
    """
    words = []
    for i in range(len(data["text"])):
        text = (data["text"][i] or "").strip()
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        if not text or conf < 0:
            continue
        words.append(
            {"x": data["left"][i], "y": data["top"][i], "h": data["height"][i], "text": text}
        )

    if not words:
        return ""

    words.sort(key=lambda w: (w["y"], w["x"]))
    heights = sorted(w["h"] for w in words)
    tol = max(6.0, heights[len(heights) // 2] * 0.6)

    rows: list[list[dict]] = [[words[0]]]
    row_y = words[0]["y"]
    for w in words[1:]:
        if abs(w["y"] - row_y) <= tol:
            rows[-1].append(w)
        else:
            rows.append([w])
            row_y = w["y"]

    lines = []
    for row in rows:
        row.sort(key=lambda w: w["x"])
        lines.append(" ".join(w["text"] for w in row))
    return "\n".join(lines)


def _ocr(content: bytes) -> str:
    try:
        import pypdfium2 as pdfium
        import pytesseract
    except ImportError as e:  # pragma: no cover - env guard
        raise OcrUnavailable(str(e)) from e

    from app.services import capture_progress

    pdf = pdfium.PdfDocument(content)
    try:
        out: list[str] = []
        scale = _OCR_DPI / 72.0
        # WO-X: this loop is the only genuinely slow phase of a capture, and the
        # only one that is divisible into countable units — so it is the only one
        # that reports a page count. The total is what will ACTUALLY be read (the
        # DoS cap applied), not the document's page count, so "page 20 of 20" on a
        # 60-page file is not a lie about work still to come.
        if len(pdf) > _MAX_OCR_PAGES:
            raise ValueError("PDF exceeds the 50-page processing limit.")
        total = len(pdf)
        capture_progress.report(capture_progress.OCR, pages_done=0, pages_total=total)
        for i in range(total):  # cap pages (DoS guard)
            page = pdf[i]
            width, height = page.get_width(), page.get_height()
            if not all(math.isfinite(value) and value > 0 for value in (width, height)):
                raise ValueError("PDF page has invalid dimensions.")
            bounded_scale = min(
                scale, math.sqrt(40_000_000 / (width * height)), 20_000 / max(width, height)
            )
            bitmap = page.render(scale=bounded_scale)
            image = bitmap.to_pil()
            data = ocr_image_to_data(pytesseract, image, page=i + 1)
            out.append(_reconstruct_lines(data))
            from app.services import filesec

            if (
                sum(len(text.encode("utf-8")) for text in out)
                > filesec.RESOURCE_BYTES["pdf_decoded_text"]
            ):
                raise ValueError("PDF decoded text exceeds the processing limit.")
            image.close()
            bitmap.close()
            page.close()
            capture_progress.report(capture_progress.OCR, pages_done=i + 1, pages_total=total)
        return "\n".join(out).strip()
    finally:
        pdf.close()


def _extract_text(content: bytes) -> tuple[str, str]:
    """Return (text, method) where method is 'text-layer' or 'ocr'."""
    from app.services import capture_progress

    capture_progress.report(capture_progress.READING)
    try:
        text = _extract_text_layer(content)
    except ImportError as e:  # pragma: no cover - env guard
        raise OcrUnavailable(str(e)) from e

    if len(text) >= _TEXT_LAYER_MIN_CHARS:
        return text, "text-layer"

    # Scanned / image-only PDF → OCR.
    return _ocr(content), "ocr"


# --------------------------------------------------------------------------- #
# Heuristic invoice parsing
# --------------------------------------------------------------------------- #
def _find_labeled_amount(
    lines: list[str], keywords: tuple[str, ...], exclude: tuple[str, ...] = ()
) -> Decimal | None:
    # Scan bottom-up (totals sit at the foot) and ignore transaction rows so a
    # station literally named "Total …" can't be mistaken for the grand total.
    for line in reversed(lines):
        if _LEADING_DATE_RE.match(line):
            continue
        low = line.lower()
        if any(k in low for k in keywords) and not any(x in low for x in exclude):
            amts = _MONEY_RE.findall(_mask(line))
            if amts:
                return _num(amts[-1])
    return None


def _find_localized_amount(lines: list[str], labels: frozenset[str]) -> Decimal | None:
    for line in reversed(lines):
        if _LEADING_DATE_RE.match(line) or _localized_summary_label(line) not in labels:
            continue
        amounts = _MONEY_RE.findall(_mask(line))
        if amounts:
            return _num(amounts[-1])
    return None


def _read_legacy_row(line: str) -> _RowReading | None:
    """Parse ONE reconstructed row into a line item, or None if it isn't one.

    Transaction-aware: a leading date is kept as part of the description (so a
    fuel/toll statement's date+station+product survives), dates/times inside the
    row are masked before money parsing, and the rightmost money token is the
    line amount. Any number left trailing the description is the quantity.
    """
    if _is_summary_row(line):
        return None

    date_prefix, rest = _split_leading_date(line)
    masked = _mask(rest)
    money = list(_MONEY_RE.finditer(masked))
    if not money:
        return None

    first = money[0].start()
    desc_core = rest[:first].strip(" .\t-|:#")
    amounts = [_num(m.group()) for m in money]
    amount = amounts[-1]
    unit = amounts[-2] if len(amounts) >= 2 else amount

    qty = Decimal("1")
    quantity_original = None
    qm = re.search(r"^(.*?)(\d+(?:[.,]\d+)?)\s*$", desc_core)
    if len(amounts) >= 2 and qm and qm.group(1).strip():
        desc_core = qm.group(1).strip()
        qty = _num(qm.group(2))
        if qty > 0:
            quantity_original = qm.group(2)

    description = (f"{date_prefix} {desc_core}".strip() if date_prefix else desc_core).strip()
    description_original = description or None
    if not description or description.replace(" ", "").isdigit():
        description = desc_core or date_prefix or "Item"
        if description == "Item":
            description_original = None

    amount_original = money[-1].group() if amount > 0 else None
    if amount <= 0 and qty and unit:
        amount = qty * unit
    if amount <= 0:
        return None

    item = LineItemIn(
        description=description[:500],
        category="uncategorized",
        quantity=qty if qty > 0 else Decimal("1"),
        unit_price=unit if unit > 0 else amount,
        amount=amount,
        tax_rate=Decimal("0"),
    )
    return _RowReading(
        item,
        {
            "description": description_original,
            "quantity": quantity_original,
            "unit_price": money[-2].group() if len(amounts) >= 2 and unit > 0 else None,
            "amount": amount_original,
        },
    )


def _parse_row(line: str) -> LineItemIn | None:
    reading = _read_legacy_row(line)
    return reading.item if reading is not None else None


def _table_roles(line: str) -> tuple[str, ...] | None:
    text = re.sub(r"\s+", " ", line.replace("|", " ")).strip()
    roles = []
    while text:
        match = _TABLE_ROLE_RE.match(text)
        if match is None:
            return None
        role = re.sub(r"\s+", " ", match.group(1).lower())
        role = {
            "qty": "quantity",
            "unit price": "unit_price",
            "net amount": "amount",
            "vat amount": "captured_tax_amount",
            "tax amount": "captured_tax_amount",
            "gross amount": "captured_gross_amount",
        }.get(role, role)
        roles.append(role)
        text = text[match.end() :]
    if len(roles) < 2 or roles[0] != "description":
        return None
    return tuple(roles)


def _read_table_row(line: str, roles: tuple[str, ...]) -> _RowReading | None:
    if "amount" not in roles or len(set(roles)) != len(roles) or _is_summary_row(line):
        return None
    tokens = list(re.finditer(r"\S+", _mask(line)))
    numeric_roles = roles[1:]
    if len(tokens) <= len(numeric_roles):
        return None
    cells = tokens[-len(numeric_roles) :]
    if any(_TABLE_NUMBER_RE.fullmatch(cell.group()) is None for cell in cells):
        return None
    description = line[: cells[0].start()].strip(" \t|")
    if not description:
        return None
    values: dict[str, Decimal] = {}
    originals: dict[str, str | None] = {"description": description}
    amount_cell = cells[numeric_roles.index("amount")].group()
    decimal_mark = re.search(r"([.,])\d{2}\)?$", amount_cell)
    separator = decimal_mark.group(1) if decimal_mark is not None else None
    for role, cell in zip(numeric_roles, cells, strict=True):
        original = cell.group()
        normalized = original
        if role in {"quantity", "unit_price"} and "," in original and "." not in original:
            if separator == ",":
                # A stated q2 amount disambiguates 0,815 as a fractional
                # unit/quantity in this row; no shared money rule changes.
                normalized = original.replace(",", ".")
            elif separator is None and re.fullmatch(r"[-+]?\d+,\d{3}", original):
                return None  # No source evidence distinguishes decimal/thousands.
        # Preserve source precision; only q2 amount roles may be normalized to
        # cents, never silently rounded or manufactured by a zero fallback.
        try:
            value = _num(normalized)
            if value < 0 or not value.is_finite():
                return None
            if value == 0 and any(char in "123456789" for char in original):
                return None
            if role in {"amount", "captured_tax_amount", "captured_gross_amount"}:
                rounded = value.quantize(Decimal("0.01"))
                if rounded != value:
                    return None
                value = rounded
        except InvalidOperation:
            return None
        values[role] = value
        originals[role] = original
    if values["amount"] <= 0:
        return None
    try:
        item = LineItemIn(
            description=description[:500],
            quantity=values.get("quantity", Decimal("1")),
            unit_price=values.get("unit_price", values["amount"]),
            amount=values["amount"],
            captured_tax_amount=values.get("captured_tax_amount"),
            captured_gross_amount=values.get("captured_gross_amount"),
        )
    except ValueError:
        return None
    return _RowReading(item, originals)


def _read_line_items(lines: list[str]) -> tuple[list[_RowReading], list[str], bool]:
    readings = []
    warnings = []
    roles = None
    table_seen = False
    for line in lines:
        header = _table_roles(line)
        if header is not None:
            roles, table_seen = header, True
            if "amount" not in roles or len(set(roles)) != len(roles):
                warnings.append(
                    "Table columns do not identify one net/line amount; review the original and enter these rows manually."
                )
            continue
        if _SUPPLIER_LABEL_RE.fullmatch(line) or _is_summary_row(line):
            continue
        reading = _read_table_row(line, roles) if roles is not None else _read_legacy_row(line)
        if reading is not None:
            readings.append(reading)
        elif roles is not None and any(character.isdigit() for character in _mask(line)):
            warning = "Some table column cells could not be mapped; review the original and enter missing rows manually."
            if warning not in warnings:
                warnings.append(warning)
    return readings, warnings, table_seen


def _extract_line_items(lines: list[str]) -> list[LineItemIn]:
    readings, _, _ = _read_line_items(lines)
    return [reading.item for reading in readings]


def parse_invoice_text(text: str, filename: str, method: str) -> ParsedInvoiceDraft:
    warnings: list[str] = [
        f"Extracted via {method.upper()} — verify every figure before saving."
        if method == "ocr"
        else "Parsed from the PDF text layer — verify before saving."
    ]
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    defaulted_fields: dict[str, str] = {}

    # Vendor: first line that looks like a name.
    vendor = next(
        (
            ln
            for ln in lines
            if len(ln) > 2 and any(c.isalpha() for c in ln) and "invoice" not in ln.lower()
        ),
        None,
    )
    supplier_labels = [
        match.group(1) for line in lines if (match := _SUPPLIER_LABEL_RE.fullmatch(line))
    ]
    suppliers = list(dict.fromkeys(label for label in supplier_labels if label))
    if len(suppliers) == 1 and len(suppliers[0]) <= 200:
        vendor = suppliers[0]
    elif len(suppliers) == 1:
        vendor = None
        warnings.append(
            "The supplier name does not fit the name field; review the original and enter it manually."
        )
    elif suppliers:
        vendor = None
        warnings.append(
            "Conflicting supplier labels were read; review the original and select the supplier manually."
        )
    elif supplier_labels:
        vendor = None
        warnings.append(
            "The supplier label has no name; review the original and enter it manually."
        )

    m = _INVOICE_NO_RE.search(text)
    invoice_number = m.group(1) if m else _stem(filename)
    if not m:
        warnings.append("Invoice number not detected; used the filename.")
        defaulted_fields["invoice_number"] = invoice_number

    issue = None
    dm = _DATE_LABEL_RE.search(text)
    if dm:
        issue = _to_date(dm.group(1))
    if issue is None:
        issue = date.today()
        warnings.append("Issue date not detected; defaulted to today.")
        defaulted_fields["issue_date"] = issue.isoformat()

    from app.services.fx import CURRENCY_BY_CODE

    currency_match = _CURRENCY_LABEL_RE.search(text)
    currency = currency_match.group(1).upper() if currency_match else None
    if currency not in CURRENCY_BY_CODE:
        currency = None
    if currency is None:
        for sym, code in _CURRENCY.items():
            if sym in text:
                currency = code
                break
    if currency is None:
        currency = "EUR"
        defaulted_fields["currency"] = currency
        warnings.append("Currency not detected; defaulted to EUR.")

    readings, column_warnings, table_seen = _read_line_items(lines)
    items = [reading.item for reading in readings]
    warnings.extend(column_warnings)

    subtotal = _find_labeled_amount(lines, ("subtotal", "sub-total", "sub total"))
    if subtotal is None:
        subtotal = _find_localized_amount(lines, _LV_SUBTOTAL_LABELS)
    tax = _find_labeled_amount(lines, ("vat", "tax"))
    if tax is None:
        tax = _find_localized_amount(lines, _LV_TAX_LABELS)
    total = _find_labeled_amount(
        lines,
        ("total", "amount due", "balance due"),
        exclude=("subtotal", "sub-total", "sub total"),
    )
    if total is None:
        total = _find_localized_amount(lines, _LV_TOTAL_LABELS)

    # If we found a subtotal + tax, infer one VAT rate and apply it to the lines
    # so the saved invoice's recomputed tax matches the document.
    if items and subtotal and tax and subtotal > 0:
        rate = (tax / subtotal * Decimal("100")).quantize(Decimal("0.01"))
        if rate <= 100:
            items = [li.model_copy(update={"tax_rate": rate}) for li in items]
            warnings.append(
                f"Applied inferred VAT rate {rate}% to all lines (subtotal {subtotal}, tax {tax})."
            )

    if not items:
        if total and not table_seen:
            items = [
                LineItemIn(
                    description="Invoice total (OCR-detected)",
                    category="uncategorized",
                    quantity=Decimal("1"),
                    unit_price=total,
                    amount=total,
                    tax_rate=Decimal("0"),
                )
            ]
            warnings.append("No line items detected; captured the document total as a single line.")
        elif table_seen:
            warnings.append(
                "No table rows could be read safely — review the column warnings and enter them manually."
            )
        else:
            warnings.append("No line items or total detected — enter them manually.")

    if total is not None:
        warnings.append(
            f"Document total on page: {currency} {total} — reconcile against the parsed lines."
        )

    draft = InvoiceCreate(
        vendor_name=vendor,
        invoice_number=invoice_number,
        issue_date=issue,
        currency=currency,
        source_filename=filename,
        line_items=items,
    )
    line_fields = []
    for index, item in enumerate(items):
        # A document total copied into an invented line is a derived fill,
        # not source line evidence (even when its numeric value was read).
        originals = readings[index].originals if index < len(readings) else {}
        for field in (
            "description",
            "quantity",
            "unit_price",
            "amount",
            "category",
            "tax_rate",
            "captured_tax_amount",
            "captured_gross_amount",
        ):
            value = getattr(item, field)
            original = originals.get(field)
            line_fields.append(
                FieldProvenance(
                    field=field,
                    line_index=index,
                    value=str(value) if value is not None else None,
                    normalized_value=str(value) if value is not None else None,
                    original_value=original,
                    status="missing"
                    if value is None
                    else "extracted"
                    if original is not None
                    else "defaulted",
                )
            )
    return ParsedInvoiceDraft(
        draft=draft,
        warnings=warnings,
        method=method,
        fields=[
            FieldProvenance(
                field=name,
                value=value,
                normalized_value=value,
                status="defaulted",
                low_confidence=True,
            )
            for name, value in defaulted_fields.items()
        ]
        + line_fields,
    )


def _stem(filename: str) -> str:
    base = filename.rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[0][:120] or "INV"


def _parse_pdf(filename: str, content: bytes) -> ParsedInvoiceDraft:
    """Parse a PDF into a draft invoice.

    Deterministic-first: if the PDF is a Factur-X/ZUGFeRD hybrid carrying an
    embedded e-invoice XML, read that exactly (no OCR). Otherwise use the text
    layer, falling back to OCR for scanned documents.
    """
    from app.services import einvoice

    try:
        embedded = einvoice._extract_embedded_xml(content)
    except ValueError:
        raise  # resource rejection must never silently fall back to another parser
    except Exception:  # corrupt optional metadata may fall back to text
        embedded = None
    if embedded:
        result = einvoice.parse_xml_bytes(embedded, filename)
        result.warnings.insert(
            0, "Read the e-invoice XML embedded in this PDF (Factur-X/ZUGFeRD) — no OCR needed."
        )
        return result

    text, method = _extract_text(content)
    if not text:
        raise ValueError("Could not read any text from the PDF (empty or unreadable).")
    from app.services import capture_progress

    capture_progress.report(capture_progress.INTERPRETING)
    return parse_invoice_text(text, filename, method)


def extract_text(content: bytes) -> tuple[str, str]:
    """Read untrusted PDF text/OCR in a bounded child, including bank/receipt paths."""
    from app.services.pdf_process import run

    text, method = run("text", "document.pdf", content)
    return text, method


def parse_pdf(filename: str, content: bytes) -> ParsedInvoiceDraft:
    """Read an invoice in a killable process; no PDF library runs in the caller."""
    from app.services.pdf_process import run

    return ParsedInvoiceDraft.model_validate(run("invoice", filename, content))
