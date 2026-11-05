"""Fresh synthetic text contracts, separate from unavailable OCR-TABLE-01."""

from decimal import Decimal

import pytest

from app.schemas.image_reading import ImageReadingPreparation
from app.services import extraction_provider, image_ocr, pdf_ocr

PREFIX = (
    "Synthetic Supply Ltd\nInvoice Number: SYNTHETIC-ROLES-1\n"
    "Invoice Date: 2026-11-05\nCurrency: EUR\n"
)


@pytest.mark.parametrize("method", ["text-layer", "ocr"])
@pytest.mark.parametrize("numbers", ["2 100 200", "2 100.00 200.00", "2 100,00 200,00"])
def test_explicit_amount_roles_read_integer_or_decimal_cells_without_extra_rows(method, numbers):
    parsed = pdf_ocr.parse_invoice_text(
        PREFIX + f"Description Qty Unit Price Amount\nSKU-42 {numbers}\nTotal EUR 200.00",
        "synthetic-roles.pdf",
        method,
    )
    rows = parsed.draft.line_items
    assert len(rows) == 1
    assert rows[0].description == "SKU-42"
    assert (rows[0].quantity, rows[0].unit_price, rows[0].amount) == (
        Decimal("2"),
        Decimal("100.00"),
        Decimal("200.00"),
    )


@pytest.mark.parametrize("method", ["text-layer", "ocr"])
@pytest.mark.parametrize("integer", [False, True])
@pytest.mark.parametrize("reordered", [False, True])
def test_explicit_net_vat_gross_cells_follow_the_actual_header(method, integer, reordered):
    header = "Description Qty Unit Price "
    header += (
        "Gross Amount VAT Amount Net Amount" if reordered else "Net Amount VAT Amount Gross Amount"
    )
    cells = ["2", "100", "242", "42", "200"] if reordered else ["2", "100", "200", "42", "242"]
    if not integer:
        cells[1:] = [value + ".00" for value in cells[1:]]
    result = pdf_ocr.parse_invoice_text(
        PREFIX + header + "\nConsulting " + " ".join(cells), "synthetic-roles.pdf", method
    )
    assert len(result.draft.line_items) == 1
    row = result.draft.line_items[0]
    assert (row.description, row.quantity, row.unit_price, row.amount) == (
        "Consulting",
        Decimal("2"),
        Decimal("100.00"),
        Decimal("200.00"),
    )
    assert row.captured_tax_amount == Decimal("42.00")
    assert row.captured_gross_amount == Decimal("242.00")
    assert row.tax_rate == 0, "Source amount roles do not authorize inferring a tax-rate policy"


@pytest.mark.parametrize(
    "bad_row", ["Consulting 100.00 200.00", "Consulting 2 100.00 200.00 EXTRA"]
)
def test_missing_or_unmapped_role_cells_require_manual_review(bad_row):
    parsed = pdf_ocr.parse_invoice_text(
        PREFIX + "Description Qty Unit Price Amount\n" + bad_row,
        "synthetic-roles.pdf",
        "text-layer",
    )
    assert parsed.draft.line_items == []
    assert any(
        "column" in warning.lower() and "review" in warning.lower() for warning in parsed.warnings
    )


def test_gross_only_header_never_silently_turns_gross_into_net():
    parsed = pdf_ocr.parse_invoice_text(
        PREFIX
        + "Description Qty Unit Price Gross Amount\nConsulting 2 100.00 242.00\nTotal 242.00",
        "synthetic-roles.pdf",
        "text-layer",
    )
    assert parsed.draft.line_items == []
    assert any(
        "net" in warning.lower() and "review" in warning.lower() for warning in parsed.warnings
    )


@pytest.mark.parametrize("first", [False, True])
def test_explicit_supplier_label_is_read_instead_of_buyer_or_label(first):
    text = "Supplier: Synthetic Supply Ltd\n"
    if not first:
        text = "Bill to: Synthetic Buyer Ltd\n" + text
    parsed = pdf_ocr.parse_invoice_text(
        text + PREFIX.split("\n", 1)[1] + "Consulting 2 100.00 200.00",
        "synthetic-roles.pdf",
        "text-layer",
    )
    assert parsed.draft.vendor_name == "Synthetic Supply Ltd"
    assert len(parsed.draft.line_items) == 1


def test_explicit_supplier_numeric_name_is_header_evidence_not_an_extra_line():
    parsed = pdf_ocr.parse_invoice_text(
        "Supplier: Synthetic Studio 123.45\n" + PREFIX.split("\n", 1)[1] + "Consulting 25.00",
        "synthetic-roles.pdf",
        "text-layer",
    )
    assert parsed.draft.vendor_name == "Synthetic Studio 123.45"
    assert [row.description for row in parsed.draft.line_items] == ["Consulting"]


@pytest.mark.parametrize("provider_name", ["pdf", "image"])
def test_filled_quantity_and_unit_have_no_invented_original_or_confidence(
    monkeypatch, provider_name
):
    text = PREFIX + "Consulting 25.00"
    parsed = pdf_ocr.parse_invoice_text(text, "synthetic-roles.pdf", "text-layer")
    if provider_name == "pdf":
        monkeypatch.setattr(pdf_ocr, "parse_pdf", lambda *_args: parsed)
        provider = extraction_provider.PdfProvider()
    else:
        preparation = ImageReadingPreparation(
            source_width=32, source_height=16, raster_width=32, raster_height=16
        )
        monkeypatch.setattr(image_ocr, "read", lambda _: image_ocr.ImageReading(text, preparation))
        provider = extraction_provider.ImageProvider()
    result = provider.extract(
        "synthetic-roles.pdf" if provider_name == "pdf" else "synthetic-roles.png",
        b"controlled source seam",
    )
    fields = {field.field: field for field in result.fields if field.line_index == 0}
    for field in ["quantity", "unit_price"]:
        assert fields[field].status == "defaulted"
        assert fields[field].original is None and fields[field].confidence is None
    assert fields["quantity"].normalized == "1"
    assert fields["unit_price"].normalized == "25.00"
    assert fields["amount"].status == "extracted" and fields["amount"].original == "25.00"


def test_explicit_integer_cell_retains_original_and_normalized_provenance(monkeypatch):
    parsed = pdf_ocr.parse_invoice_text(
        PREFIX + "Description Qty Unit Price Amount\nConsulting 2 100 200",
        "synthetic-roles.pdf",
        "text-layer",
    )
    monkeypatch.setattr(pdf_ocr, "parse_pdf", lambda *_args: parsed)
    result = extraction_provider.PdfProvider().extract(
        "synthetic-roles.pdf", b"%PDF controlled parser seam"
    )
    cells = {field.field: field for field in result.fields if field.line_index == 0}
    assert cells["amount"].status == "extracted" and cells["amount"].original == "200"
    assert cells["amount"].normalized == "200.00"
    assert cells["quantity"].original == "2" and cells["unit_price"].original == "100"


@pytest.mark.parametrize("row", ["12345 2 100 200", "01.06.2026 SKU-42 2 100 200"])
def test_numeric_sku_and_leading_transaction_date_remain_description(row):
    parsed = pdf_ocr.parse_invoice_text(
        PREFIX + "Description Qty Unit Price Amount\n" + row, "synthetic-roles.pdf", "text-layer"
    )
    assert len(parsed.draft.line_items) == 1
    assert parsed.draft.line_items[0].description == row.rsplit(" ", 3)[0]
    assert parsed.draft.line_items[0].amount == Decimal("200.00")


def test_no_explicit_table_does_not_turn_integer_identifiers_dates_or_summaries_into_rows():
    parsed = pdf_ocr.parse_invoice_text(
        PREFIX + "Reference 12345\nOrder 2026\nDate 01.06.2026\nSubtotal 200\nVAT 42\nTotal 242",
        "synthetic-roles.pdf",
        "text-layer",
    )
    assert parsed.draft.line_items == []


def test_legacy_unlabelled_supplier_and_decimal_row_stay_compatible():
    parsed = pdf_ocr.parse_invoice_text(
        PREFIX + "SKU-42 2 100.00 200.00", "synthetic-roles.pdf", "text-layer"
    )
    assert parsed.draft.vendor_name == "Synthetic Supply Ltd"
    assert len(parsed.draft.line_items) == 1 and parsed.draft.line_items[0].description == "SKU-42"
    assert parsed.draft.line_items[0].amount == Decimal("200.00")


@pytest.mark.parametrize(
    "label", ["Supplier:", "Supplier: First Synthetic Ltd\nSupplier: Second Synthetic Ltd"]
)
def test_empty_or_conflicting_explicit_supplier_never_falls_back_to_the_buyer(label):
    result = pdf_ocr.parse_invoice_text(
        "Bill to: Synthetic Buyer Ltd\n"
        + label
        + "\n"
        + PREFIX.split("\n", 1)[1]
        + "Service 25.00",
        "synthetic-roles.pdf",
        "text-layer",
    )
    assert result.draft.vendor_name is None
    assert any(
        "supplier" in warning.lower() and "review" in warning.lower() for warning in result.warnings
    )


def test_existing_summary_rate_inference_is_labelled_derived_not_a_read_line_cell(monkeypatch):
    parsed = pdf_ocr.parse_invoice_text(
        PREFIX + "Consulting 2 100.00 200.00\nSubtotal 200.00\nVAT 42.00\nTotal 242.00",
        "synthetic-roles.pdf",
        "text-layer",
    )
    assert parsed.draft.line_items[0].tax_rate == Decimal("21.00")
    monkeypatch.setattr(pdf_ocr, "parse_pdf", lambda *_args: parsed)
    result = extraction_provider.PdfProvider().extract(
        "synthetic-roles.pdf", b"%PDF controlled seam"
    )
    rate = next(f for f in result.fields if f.line_index == 0 and f.field == "tax_rate")
    assert rate.status == "defaulted" and rate.original is None and rate.confidence is None
    assert rate.normalized == "21.00"


def test_total_only_existing_fallback_keeps_its_values_but_does_not_invent_line_evidence(
    monkeypatch,
):
    parsed = pdf_ocr.parse_invoice_text(PREFIX + "Total 25.00", "synthetic-roles.pdf", "text-layer")
    assert len(parsed.draft.line_items) == 1 and parsed.draft.line_items[0].amount == Decimal(
        "25.00"
    )
    monkeypatch.setattr(pdf_ocr, "parse_pdf", lambda *_args: parsed)
    result = extraction_provider.PdfProvider().extract(
        "synthetic-roles.pdf", b"%PDF controlled seam"
    )
    fields = [f for f in result.fields if f.line_index == 0]
    assert all(f.original is None and f.confidence is None for f in fields)
    assert all(f.status in {"defaulted", "missing"} for f in fields)


@pytest.mark.parametrize(
    "cells", ["-2 100 200", "2 -100 200", "2 100 -200", "2 100..00 200", "2 100 200.123"]
)
def test_invalid_or_unsupported_explicit_numeric_cells_are_not_filled_or_rounded(cells):
    parsed = pdf_ocr.parse_invoice_text(
        PREFIX + "Description Qty Unit Price Amount\nConsulting " + cells,
        "synthetic-roles.pdf",
        "text-layer",
    )
    assert parsed.draft.line_items == []
    assert any("column" in warning and "review" in warning for warning in parsed.warnings)


def test_supplier_larger_than_existing_field_is_not_silently_truncated():
    parsed = pdf_ocr.parse_invoice_text(
        "Supplier: " + "Synthetic " * 25 + "Ltd\n" + PREFIX.split("\n", 1)[1] + "Consulting 25.00",
        "synthetic-roles.pdf",
        "text-layer",
    )
    assert parsed.draft.vendor_name is None
    assert any(
        "supplier name" in warning.lower() and "review" in warning.lower()
        for warning in parsed.warnings
    )


@pytest.mark.parametrize("method", ["text-layer", "ocr"])
@pytest.mark.parametrize("separator", [".", ","])
def test_fractional_quantity_and_unit_price_precision_are_preserved_with_stated_q2_net(
    monkeypatch, method, separator
):
    cells = "1.25 1.2345 1.54 0.32 1.86".replace(".", separator)
    parsed = pdf_ocr.parse_invoice_text(
        PREFIX + "Description Qty Unit Price Net Amount VAT Amount Gross Amount\nFuel " + cells,
        "synthetic-precision.pdf",
        method,
    )
    assert len(parsed.draft.line_items) == 1
    row = parsed.draft.line_items[0]
    assert row.quantity == Decimal("1.25") and row.unit_price == Decimal("1.2345")
    assert row.amount == Decimal("1.54")
    assert row.captured_tax_amount == Decimal("0.32")
    assert row.captured_gross_amount == Decimal("1.86")
    assert row.tax_rate == 0
    monkeypatch.setattr(pdf_ocr, "parse_pdf", lambda *_args: parsed)
    result = extraction_provider.PdfProvider().extract(
        "synthetic-precision.pdf", b"%PDF controlled seam"
    )
    fields = {f.field: f for f in result.fields if f.line_index == 0}
    assert fields["unit_price"].original == "1" + separator + "2345"
    assert fields["unit_price"].normalized == "1.2345"
    assert fields["quantity"].original == "1" + separator + "25"


@pytest.mark.parametrize("separator", [".", ","])
def test_three_decimal_unit_price_uses_the_explicit_q2_amount_separator(separator):
    cells = "2.50 0.815 2.04".replace(".", separator)
    parsed = pdf_ocr.parse_invoice_text(
        PREFIX + "Description Qty Unit Price Amount\nFuel " + cells,
        "synthetic-unit-precision.pdf",
        "text-layer",
    )
    assert len(parsed.draft.line_items) == 1
    row = parsed.draft.line_items[0]
    assert row.quantity == Decimal("2.50")
    assert row.unit_price == Decimal("0.815")
    assert row.amount == Decimal("2.04")
