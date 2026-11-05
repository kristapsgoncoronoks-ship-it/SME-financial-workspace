"""Source-backed capture regressions; these do not establish real-invoice accuracy."""

import io
from decimal import Decimal

import pytest

from app.services import extraction_provider, pdf_ocr
from tests.test_image_ocr_process import local_image_reader as local_image_reader


@pytest.mark.parametrize("percent", ["21.00%", "21,00 %", "21%"])
def test_percentage_column_is_not_an_invoice_line_amount(percent):
    result = pdf_ocr.parse_invoice_text(
        "Acme Ltd\nInvoice Number: INV-1\nInvoice Date: 2026-10-20\n"
        f"Cloud compute 2 100.00 200.00 {percent}\n"
        f"Object storage 1 50.00 50.00 {percent}\n"
        "Subtotal 250.00\nVAT 52.50\nTotal 302.50",
        "invoice.pdf",
        "text-layer",
    )
    lines = result.draft.line_items
    assert [line.amount for line in lines] == [Decimal("200.00"), Decimal("50.00")]
    assert [line.unit_price for line in lines] == [Decimal("100.00"), Decimal("50.00")]
    assert [line.quantity for line in lines] == [Decimal("2"), Decimal("1")]


@pytest.mark.parametrize("method", ["text-layer", "ocr"])
def test_latvian_headers_and_summaries_do_not_become_expense_lines(method):
    result = pdf_ocr.parse_invoice_text(
        "SIA Paraugs\nRēķins Nr. REK-2026-42\nRēķina datums: 20.10.2026\n"
        "Valūta: EUR\nPakalpojums 2 100,00 200,00\nMateriāli 1 50,00 50,00\n"
        "Summa bez PVN EUR 250,00\nPVN 21% 52,50\nKopā apmaksai EUR 302,50",
        "supplier-file.pdf",
        method,
    )
    draft = result.draft
    assert draft.invoice_number == "REK-2026-42"
    assert draft.issue_date.isoformat() == "2026-10-20"
    assert [line.description for line in draft.line_items] == ["Pakalpojums", "Materiāli"]
    assert [line.amount for line in draft.line_items] == [Decimal("200.00"), Decimal("50.00")]
    assert all(line.tax_rate == Decimal("21.00") for line in draft.line_items)
    assert any("302.50" in warning for warning in result.warnings)


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Currency: USD", "USD"),
        ("Valūta: CHF", "CHF"),
        ("Valuta: GBP", "GBP"),
        ("Currency: ALL", "ALL"),
        ("Currency: usd", "USD"),
        ("Currency: USD;", "USD"),
        ("Valūta: CHF.", "CHF"),
    ],
)
def test_explicit_currency_label_is_preserved(label, expected):
    result = pdf_ocr.parse_invoice_text(
        f"Acme Ltd\nInvoice No: INV-1\nInvoice Date: 2026-10-20\n{label}\nService 10.00",
        "invoice.pdf",
        "text-layer",
    )
    assert result.draft.currency == expected


def test_explicit_currency_label_wins_over_a_symbol_in_other_document_text():
    result = pdf_ocr.parse_invoice_text(
        "Acme Ltd\nCurrency: USD\nEUR account fee €1.00\nService 10.00",
        "invoice.pdf",
        "text-layer",
    )
    assert result.draft.currency == "USD"


@pytest.mark.parametrize(
    "label",
    [
        "Currency not specified",
        "Valūta nav norādīta",
        "CurrencyUSD",
        "Currency all amounts in EUR",
        "Currency try paying in EUR",
    ],
)
def test_missing_currency_or_missing_label_boundary_is_not_a_currency_code(label):
    result = pdf_ocr.parse_invoice_text(
        f"Acme Ltd\n{label}\nService 10.00", "invoice.pdf", "text-layer"
    )
    assert result.draft.currency == "EUR"
    assert any(field.field == "currency" and field.status == "defaulted" for field in result.fields)


@pytest.mark.parametrize("description", ["PVN konsultācija", "Kopā apmaksai pārskata sagatavošana"])
def test_latvian_product_names_are_not_dropped_as_summary_rows(description):
    result = pdf_ocr.parse_invoice_text(
        f"Acme Ltd\n{description} 1 25.00 25.00", "invoice.pdf", "text-layer"
    )
    assert len(result.draft.line_items) == 1
    assert result.draft.line_items[0].description == description
    assert result.draft.line_items[0].amount == Decimal("25.00")


def test_existing_dated_total_station_and_english_amounts_still_parse():
    result = pdf_ocr.parse_invoice_text(
        "EuroFuel\nInvoice No: EF-42\nInvoice Date: 2026-10-20\n"
        "01.06.2026 Total Lyon Est Diesel 61.00 1.71 104.31\n"
        "Subtotal 104.31\nVAT 0.00\nTotal 104.31",
        "fuel.pdf",
        "text-layer",
    )
    assert len(result.draft.line_items) == 1
    assert result.draft.line_items[0].amount == Decimal("104.31")
    assert "Total Lyon" in result.draft.line_items[0].description


@pytest.mark.parametrize(
    "label",
    [
        "PVN 21% 52,50",
        "Summa bez PVN 250,00",
        "Summa bez PVN EUR 250,00",
        "Kopā apmaksai 302,50",
        "Kopā apmaksai EUR 302,50",
    ],
)
def test_localized_summary_rows_are_not_items(label):
    assert pdf_ocr._parse_row(label) is None


def test_arbitrary_three_letter_product_token_is_not_a_currency_suffix():
    line = pdf_ocr._parse_row("Kopā apmaksai ABC 25,00")
    assert line is not None
    assert line.description == "Kopā apmaksai ABC"


def test_pdf_default_header_values_remain_reviewable_not_extracted(monkeypatch):
    parsed = pdf_ocr.parse_invoice_text(
        "Acme Ltd\nService 25.00", "supplier-copy.pdf", "text-layer"
    )
    monkeypatch.setattr(pdf_ocr, "parse_pdf", lambda *_: parsed)
    result = extraction_provider.PdfProvider().extract("supplier-copy.pdf", b"%PDF")
    headers = {field.field: field for field in result.fields if field.line_index is None}
    for name in ("invoice_number", "issue_date", "currency"):
        assert headers[name].status == "defaulted"
        assert headers[name].confidence is None
        assert headers[name].original is None
        assert headers[name].low_confidence is True
    assert headers["vendor_name"].status == "extracted"


def test_pdf_explicit_header_values_keep_existing_method_confidence(monkeypatch):
    parsed = pdf_ocr.parse_invoice_text(
        "Acme Ltd\nInvoice No: INV-42\nInvoice Date: 2026-10-20\nCurrency: EUR\nService 25.00",
        "supplier-copy.pdf",
        "text-layer",
    )
    monkeypatch.setattr(pdf_ocr, "parse_pdf", lambda *_: parsed)
    result = extraction_provider.PdfProvider().extract("supplier-copy.pdf", b"%PDF")
    headers = {field.field: field for field in result.fields if field.line_index is None}
    for name in ("invoice_number", "issue_date", "currency"):
        assert headers[name].status == "extracted"
        assert headers[name].confidence == Decimal("0.85")


def _image_bytes():
    image_module = pytest.importorskip("PIL.Image")
    image = image_module.new("RGB", (30, 30), "white")
    out = io.BytesIO()
    image.save(out, format="PNG")
    image.close()
    return out.getvalue()


def test_image_invoice_reconstructs_visual_rows_using_bounded_word_data(
    monkeypatch, local_image_reader
):
    tesseract = pytest.importorskip("pytesseract")
    from app.core.config import settings

    words = [
        ("Acme", 5, 1),
        ("Ltd", 35, 1),
        ("Compute", 5, 30),
        ("2", 90, 30),
        ("100.00", 120, 30),
        ("200.00", 190, 30),
        ("Storage", 5, 60),
        ("1", 90, 60),
        ("50.00", 120, 60),
        ("50.00", 190, 60),
    ]
    # Tesseract may return words in reading-block order, not table-row order.
    words = sorted(words, key=lambda word: word[1])
    calls = []

    def data(image, **kwargs):
        calls.append(kwargs)
        return {
            "text": [w[0] for w in words],
            "left": [w[1] for w in words],
            "top": [w[2] for w in words],
            "height": [10] * len(words),
            "conf": [90] * len(words),
        }

    monkeypatch.setattr(tesseract, "image_to_data", data)
    monkeypatch.setattr(
        tesseract, "image_to_string", lambda *_a, **_k: pytest.fail("column text path used")
    )
    result = extraction_provider.ImageProvider().extract("invoice.png", _image_bytes())
    assert [line.description for line in result.draft.line_items] == ["Compute", "Storage"]
    assert [line.amount for line in result.draft.line_items] == [
        Decimal("200.00"),
        Decimal("50.00"),
    ]
    assert calls == [
        {"output_type": tesseract.Output.DICT, "timeout": settings.ocr_process_timeout_seconds}
    ]
