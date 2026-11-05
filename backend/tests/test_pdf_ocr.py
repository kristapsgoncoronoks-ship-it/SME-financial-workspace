"""PDF ingestion tests: text-layer extraction and the Tesseract OCR fallback.

These require the optional PDF/OCR stack; they skip cleanly if it (or the
tesseract binary) is not installed, so the core suite stays green everywhere.
"""

import io
import shutil
from decimal import Decimal

import pytest

pytest.importorskip("reportlab")
pytest.importorskip("pdfplumber")
pytest.importorskip("pypdfium2")
pytest.importorskip("pytesseract")
pytest.importorskip("PIL")

from PIL import Image, ImageDraw, ImageFont  # noqa: E402
from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.lib.utils import ImageReader  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402

from tests.test_image_ocr_process import local_image_reader as local_image_reader  # noqa: E402

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

_LINES = [
    "Acme Cloud Services Ltd",
    "123 Server Road, Dublin, IE",
    "",
    "INVOICE",
    "Invoice Number: INV-2026-0042",
    "Invoice Date: 2026-03-15",
    "",
    "Description                        Qty     Unit Price      Amount",
    f"{'Cloud compute':<34} {'2':>3}     {'100.00':>9}     {'200.00':>9}",
    f"{'Object storage':<34} {'1':>3}     {'50.00':>9}     {'50.00':>9}",
    "",
    "Subtotal                                                   250.00",
    "VAT (21%)                                                   52.50",
    "Total                                                      302.50",
]


def _text_pdf() -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("Courier", 11)
    y = 800
    for ln in _LINES:
        c.drawString(40, y, ln)
        y -= 20
    c.showPage()
    c.save()
    return buf.getvalue()


def _scanned_pdf() -> bytes:
    """Image-only PDF (no text layer) → forces the OCR path."""
    W, H = 1240, 1754
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    font = ImageFont.truetype(FONT, 26)
    y = 80
    for ln in _LINES:
        d.text((70, y), ln, fill="black", font=font)
        y += 46
    png = io.BytesIO()
    img.save(png, format="PNG")
    png.seek(0)

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(W, H))
    c.drawImage(ImageReader(png), 0, 0, width=W, height=H)
    c.showPage()
    c.save()
    return buf.getvalue()


@pytest.mark.asyncio
async def test_pdf_text_layer_upload_and_save(auth_client, parse_upload):
    files = {"file": ("invoice.pdf", io.BytesIO(_text_pdf()), "application/pdf")}
    up = await parse_upload(auth_client, files)
    draft = up["draft"]
    assert draft["invoice_number"] == "INV-2026-0042"
    assert draft["issue_date"] == "2026-03-15"
    assert len(draft["line_items"]) == 2

    saved = await auth_client.post("/api/v1/invoices", json=draft)
    assert saved.status_code == 201, saved.text
    body = saved.json()
    # 2*100 + 1*50 = 250 subtotal; inferred 21% VAT ⇒ 52.50 tax ⇒ 302.50 total
    assert body["subtotal"] == "250.00"
    assert body["tax_amount"] == "52.50"
    assert body["total"] == "302.50"


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract binary not installed")
def test_pdf_ocr_fallback_reads_scanned_invoice():
    from app.services import pdf_ocr

    text, method = pdf_ocr.extract_text(_scanned_pdf())
    assert method == "ocr"
    assert "INV-2026-0042" in text.replace(" ", "").replace("O", "0") or "INV-2026-0042" in text

    result = pdf_ocr.parse_pdf("scan.pdf", _scanned_pdf())
    d = result.draft
    assert d.invoice_number == "INV-2026-0042"
    assert len(d.line_items) == 2
    assert sum((li.amount for li in d.line_items), start=Decimal("0")) == Decimal("250.00")


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract binary not installed")
def test_image_invoice_ocr_reads_all_visual_rows():
    from app.services.extraction_provider import ImageProvider

    image = Image.new("RGB", (1240, 1754), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(FONT, 26)
    for index, line in enumerate(_LINES):
        draw.text((70, 80 + index * 46), line, fill="black", font=font)
    output = io.BytesIO()
    image.save(output, format="PNG")
    image.close()

    result = ImageProvider().extract("scan.png", output.getvalue())
    assert result.method == "ocr"
    assert result.draft.invoice_number == "INV-2026-0042"
    assert [line.quantity for line in result.draft.line_items] == [Decimal("2"), Decimal("1")]
    assert [line.unit_price for line in result.draft.line_items] == [
        Decimal("100.00"),
        Decimal("50.00"),
    ]
    assert [line.amount for line in result.draft.line_items] == [
        Decimal("200.00"),
        Decimal("50.00"),
    ]


# ---------------------------------------------------------------------------
# STIR-P2-01 (Stirling-PDF, reference integration 2026-09-07): every native
# Tesseract invocation carries the configured runtime budget, and a timeout is a
# typed, classified capture outcome — not a bare RuntimeError that becomes
# `internal_error`.


def test_pdf_ocr_native_call_has_timeout_and_raises_typed_timeout(monkeypatch):
    import pytesseract

    from app.core.config import settings
    from app.services import pdf_ocr

    seen = []

    def _timeout(*args, **kwargs):
        seen.append(kwargs.get("timeout"))
        raise RuntimeError("Tesseract process timeout")

    monkeypatch.setattr(pytesseract, "image_to_data", _timeout)
    with pytest.raises(pdf_ocr.OcrTimedOut) as caught:
        pdf_ocr._ocr(_scanned_pdf())
    assert seen == [settings.ocr_process_timeout_seconds]
    assert "page 1" in str(caught.value)
    assert 0 < settings.ocr_process_timeout_seconds <= 300


def test_pdf_ocr_real_engine_errors_are_not_disguised_as_timeouts(monkeypatch):
    import pytesseract

    from app.services import pdf_ocr

    def _engine_error(*args, **kwargs):
        raise pytesseract.TesseractError(1, "Error opening data file")

    monkeypatch.setattr(pytesseract, "image_to_data", _engine_error)
    with pytest.raises(pytesseract.TesseractError):
        pdf_ocr._ocr(_scanned_pdf())


def test_pdf_provider_maps_ocr_timeout_to_capture_outcome(monkeypatch):
    from app.services import capture_failures, extraction_provider, pdf_ocr

    def _timeout(*_args, **_kwargs):
        raise pdf_ocr.OcrTimedOut("OCR exceeded 120 seconds on page 2")

    monkeypatch.setattr(pdf_ocr, "parse_pdf", _timeout)
    with pytest.raises(capture_failures.CaptureError) as caught:
        extraction_provider.PdfProvider().extract("scan.pdf", b"%PDF-1.7")
    assert caught.value.code == capture_failures.PROCESSING_TIMEOUT
    kind = capture_failures.KINDS[capture_failures.PROCESSING_TIMEOUT]
    assert kind.retry_helps is True and kind.user_fixable is True
    assert capture_failures.code_for(caught.value) == capture_failures.PROCESSING_TIMEOUT


def test_image_provider_passes_timeout_and_classifies_it(monkeypatch, local_image_reader):
    import pytesseract

    from app.core.config import settings
    from app.services import capture_failures, extraction_provider

    image = Image.new("RGB", (20, 20), "white")
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    seen = []

    def _timeout(*args, **kwargs):
        seen.append(kwargs.get("timeout"))
        raise RuntimeError("Tesseract process timeout")

    monkeypatch.setattr(pytesseract, "image_to_data", _timeout)
    with pytest.raises(capture_failures.CaptureError) as caught:
        extraction_provider.ImageProvider().extract("scan.png", buf.getvalue())
    assert seen == [settings.ocr_process_timeout_seconds]
    assert caught.value.code == capture_failures.PROCESSING_TIMEOUT


def test_image_provider_engine_error_stays_unreadable_scan(monkeypatch, local_image_reader):
    import pytesseract

    from app.services import capture_failures, extraction_provider

    image = Image.new("RGB", (20, 20), "white")
    buf = io.BytesIO()
    image.save(buf, format="PNG")

    def _engine_error(*args, **kwargs):
        raise pytesseract.TesseractError(1, "Error opening data file")

    monkeypatch.setattr(pytesseract, "image_to_data", _engine_error)
    with pytest.raises(capture_failures.CaptureError) as caught:
        extraction_provider.ImageProvider().extract("scan.png", buf.getvalue())
    assert caught.value.code == capture_failures.UNREADABLE_SCAN


def test_receipt_image_ocr_is_bounded_too(monkeypatch, local_image_reader):
    """`receipt_ocr._text_from` had its own unbounded `image_to_data` call
    (the archive's patch missed it) — it goes through the same bounded seam."""
    import pytesseract

    from app.core.config import settings
    from app.services import pdf_ocr, receipt_ocr

    image = Image.new("RGB", (20, 20), "white")
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    seen = []

    def _timeout(*args, **kwargs):
        seen.append(kwargs.get("timeout"))
        raise RuntimeError("Tesseract process timeout")

    monkeypatch.setattr(pytesseract, "image_to_data", _timeout)
    with pytest.raises(pdf_ocr.OcrTimedOut):
        receipt_ocr._text_from("receipt.png", buf.getvalue())
    assert seen == [settings.ocr_process_timeout_seconds]


@pytest.mark.asyncio
async def test_receipt_scan_route_reports_a_timeout_as_503_not_500(auth_client, monkeypatch):
    from app.services import pdf_ocr, receipt_ocr

    def _timeout(filename, content):
        raise pdf_ocr.OcrTimedOut("OCR exceeded 120 seconds")

    monkeypatch.setattr(receipt_ocr, "suggest", _timeout)
    await auth_client.put("/api/v1/modules/expenses", json={"enabled": True})
    image = Image.new("RGB", (20, 20), "white")
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    r = await auth_client.post(
        "/api/v1/expenses/receipt-scan",
        files={"file": ("receipt.png", buf.getvalue(), "image/png")},
    )
    assert r.status_code == 503, r.text
    assert r.json()["detail"] == pdf_ocr.OCR_TIMED_OUT_DETAIL


@pytest.mark.asyncio
async def test_bank_statement_routes_report_a_timeout_as_503_not_500(auth_client, monkeypatch):
    """Both statement imports (expenses and reconciliation) share `bank_statement.parse`,
    whose PDF path reaches the bounded OCR; each route maps the timeout itself."""
    from app.services import bank_statement, pdf_ocr

    def _timeout(filename, content):
        raise pdf_ocr.OcrTimedOut("OCR exceeded 120 seconds on page 1")

    monkeypatch.setattr(bank_statement, "parse", _timeout)
    await auth_client.put("/api/v1/modules/expenses", json={"enabled": True})
    files = {
        "file": ("statement.csv", b"Date,Description,Amount\n2026-05-01,fuel,-1.00\n", "text/csv")
    }
    r = await auth_client.post("/api/v1/expenses/import/bank-statement", files=files)
    assert r.status_code == 503, r.text
    assert (
        r.json()["detail"] == pdf_ocr.OCR_TIMED_OUT_DETAIL
    )  # an instruction, not "OCR exceeded 120 s"
    await auth_client.put("/api/v1/modules/issuing", json={"enabled": True})
    r = await auth_client.post("/api/v1/reconciliation/import", files=files)
    assert r.status_code == 503, r.text
    assert r.json()["detail"] == pdf_ocr.OCR_TIMED_OUT_DETAIL
