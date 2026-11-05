"""Per-page selection/facts regressions; synthetic fixtures are not customer OCR evidence."""

import io

from PIL import Image, ImageDraw
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from app.services import pdf_ocr


def mixed_pdf() -> bytes:
    output = io.BytesIO()
    document = canvas.Canvas(output, pagesize=(360, 240))
    document.drawString(20, 210, "Synthetic Supplier")
    document.drawString(20, 190, "Invoice Number: MIXED-001")
    document.drawString(20, 170, "Native service 1 10.00 10.00")
    document.showPage()
    with Image.new("RGB", (360, 240), "white") as image:
        ImageDraw.Draw(image).text((20, 30), "Scanned service 1 20.00 20.00", fill="black")
        document.drawImage(ImageReader(image), 0, 0, width=360, height=240)
    document.showPage()
    document.save()
    return output.getvalue()


def word_data(text: str) -> dict:
    words = text.split()
    return {
        "text": words,
        "conf": ["90"] * len(words),
        "left": [index * 100 for index in range(len(words))],
        "top": [20] * len(words),
        "height": [20] * len(words),
    }


def test_mixed_pdf_keeps_native_page_and_reads_scanned_page(monkeypatch):
    """Real PDF native counts plus controlled engine output isolate selection."""
    import pdfplumber
    import pytesseract

    content = mixed_pdf()
    with pdfplumber.open(io.BytesIO(content)) as document:
        counts = [len((page.extract_text() or "").strip()) for page in document.pages]
    assert counts[0] >= 25 and counts[1] == 0
    calls = []

    def recognize(_image, **kwargs):
        calls.append(kwargs)
        return word_data("Scanned service 1 20.00 20.00")

    monkeypatch.setattr(pytesseract, "image_to_data", recognize)
    text, method = pdf_ocr._extract_text(content)
    assert method == "ocr"
    assert text.index("Native service") < text.index("Scanned service")
    assert len(calls) == 1
    assert set(calls[0]) == {"output_type", "timeout"}
    draft = pdf_ocr.parse_invoice_text(text, "mixed.pdf", method)
    assert [line.description for line in draft.draft.line_items] == [
        "Native service",
        "Scanned service",
    ]
