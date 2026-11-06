"""Truthful, bounded metadata; fixtures isolate selection, not OCR quality."""

import hashlib
import io
import json
import math
import subprocess
import sys
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from pypdf import PdfWriter
from reportlab.pdfgen import canvas

from app.schemas.capture_reading import (
    METADATA_BYTES,
    PROFILE_ID,
    PageReadingFacts,
    PageStartCheckpoint,
    PdfPageFact,
    ReadingProfileFacts,
    canonical,
)
from app.services import capture_reading, filesec, pdf_ocr
from tests.test_capture_reading_pages import mixed_pdf, word_data


@pytest.fixture
def controlled_engine(monkeypatch):
    import pytesseract

    monkeypatch.setattr(pytesseract, "get_tesseract_version", lambda: "synthetic-test-engine")
    monkeypatch.setattr(pytesseract, "get_languages", lambda: ["eng", "osd"])
    monkeypatch.setattr(
        pytesseract,
        "image_to_data",
        lambda *args, **kwargs: word_data("Scanned service 1 20.00 20.00"),
    )
    return pytesseract


def test_mixed_page_facts_are_complete_hashes_without_retained_text(controlled_engine):
    frames = []
    with capture_reading.collect(PROFILE_ID, emit=frames.append) as observed:
        text, method = pdf_ocr._extract_text(mixed_pdf())
    facts = observed.page_facts()
    assert method == "ocr"
    assert facts.page_count == 2
    assert [page.status for page in facts.pages] == ["native", "ocr_complete"]
    assert facts.pages[1].native_chars == 0
    assert facts.pages[1].native_sha256 == hashlib.sha256(b"").hexdigest()
    assert facts.selected_stream_bytes == len(text.encode())
    assert facts.selected_stream_sha256 == hashlib.sha256(text.encode()).hexdigest()
    assert observed.profile.requested_profile_id == PROFILE_ID
    assert observed.profile.engine_version == "synthetic-test-engine"
    assert observed.profile.language_mode == "engine-default"
    assert observed.profile.resolved_language is None
    encoded = b"\n".join(canonical(frame) for frame in frames)
    assert len(encoded) <= METADATA_BYTES
    assert b"Native service" not in encoded and b"Scanned service" not in encoded
    assert b"20.00" not in encoded


def test_unobserved_timeout_classification_keeps_prior_native_facts_and_no_partial_stream(
    controlled_engine, monkeypatch
):
    def timeout(*args, **kwargs):
        raise RuntimeError("Synthetic native invocation timeout")

    monkeypatch.setattr(controlled_engine, "image_to_data", timeout)
    with capture_reading.collect() as observed:
        with pytest.raises(pdf_ocr.OcrTimedOut):
            pdf_ocr._extract_text(mixed_pdf())
    facts = observed.page_facts(failed=True)
    assert [page.status for page in facts.pages] == ["native", "outcome_unknown"]
    assert facts.pages[1].cause == "unknown"
    assert facts.pages[1].selected_sha256 is None
    assert not facts.selected_stream_complete
    assert observed.profile.requested_profile_id is None


def test_final_combined_stream_uses_existing_bound(controlled_engine, monkeypatch):
    # Each branch fits90 bytes; joining the selected native and OCR pages does not.
    monkeypatch.setitem(filesec.RESOURCE_BYTES, "pdf_decoded_text", 90)
    with capture_reading.collect() as observed:
        with pytest.raises(ValueError, match="decoded text"):
            pdf_ocr._extract_text(mixed_pdf())
    assert [page.status for page in observed.page_facts(failed=True).pages] == [
        "native",
        "ocr_complete",
    ]
    assert observed.stream is None


def test_over50_refusal_records_actual_count_without_fabricated_page_facts():
    writer = PdfWriter()
    for _ in range(51):
        writer.add_blank_page(width=100, height=100)
    output = io.BytesIO()
    writer.write(output)
    with capture_reading.collect() as observed:
        with pytest.raises(ValueError, match="50-page"):
            pdf_ocr._extract_text(output.getvalue())
    facts = observed.page_facts(failed=True)
    assert facts.page_count == 51 and facts.pages is None
    assert observed.profile is None


def test_unknown_native_result_and_remaining_pages_do_not_invent_text():
    observed = capture_reading.Collector()
    observed.accept({"page_count": 3})
    observed.accept({"page_started": {"page": 2}})
    facts = observed.page_facts(failed=True)
    assert [page.status for page in facts.pages] == [
        "not_attempted",
        "outcome_unknown",
        "not_attempted",
    ]
    assert all(page.selected_sha256 is None for page in facts.pages)


def test50_complete_facts_and_checkpoints_fit_reporting_bound():
    observed = capture_reading.Collector()
    observed.accept({"page_count": 50})
    for index in range(1, 51):
        observed.accept({"page_started": {"page": index}})
        fact = PdfPageFact(
            page=index,
            status="ocr_complete",
            selected_kind="ocr",
            **capture_reading.text_facts("", "ocr"),
            **capture_reading.text_facts("", "selected"),
        )
        observed.accept({"page": fact.model_dump(mode="json")})
    capture = observed.page_facts()
    assert len(capture.pages) == 50 and observed.metadata_bytes <= METADATA_BYTES
    assert len(canonical(capture)) <= METADATA_BYTES


@pytest.mark.parametrize("field", ["text", "page_text", "words", "error", "path"])
def test_reading_facts_reject_raw_content_and_diagnostics(field):
    with pytest.raises(ValidationError):
        PageReadingFacts.model_validate({field: "not retained"})


def test_metadata_limit_is_part_of_existing_output_not_extra(monkeypatch):
    observed = capture_reading.Collector()
    observed.metadata_bytes = METADATA_BYTES
    with pytest.raises(ValueError, match="reporting limit"):
        observed.accept({"page_count": 1})
    assert observed.page_count is None
    assert filesec.RESOURCE_BYTES["pdf_parser_output"] == 4 * 1024 * 1024


def test_unused_engine_is_unknown_and_cannot_be_claimed_as_observed():
    with pytest.raises(ValidationError, match="unused"):
        ReadingProfileFacts(adapter="pdf", ocr_state="not_needed", engine_name="tesseract")
    observed = capture_reading.Collector(PROFILE_ID)
    assert observed.profile is None
    assert json.loads(canonical(observed.page_facts()))["pages"] is None


def native_pdf(lines: list[str]) -> bytes:
    output = io.BytesIO()
    document = canvas.Canvas(output, pagesize=(360, 240))
    for line in lines:
        if line:
            document.drawString(20, 190, line)
        document.showPage()
    document.save()
    return output.getvalue()


def test_native_only_profile_does_not_probe_an_unused_engine(controlled_engine, monkeypatch):
    def forbidden(*args):
        raise AssertionError("Unused OCR engine was probed")

    monkeypatch.setattr(controlled_engine, "get_languages", forbidden)
    monkeypatch.setattr(controlled_engine, "get_tesseract_version", forbidden)
    monkeypatch.setattr(controlled_engine, "image_to_data", forbidden)
    with capture_reading.collect() as observed:
        text, method = pdf_ocr._extract_text(
            native_pdf(["Synthetic native invoice with enough text"])
        )
    assert method == "text-layer" and text
    assert observed.profile.ocr_state == "not_needed" and observed.profile.engine_name is None
    assert observed.profile.inventory_status == "not_probed"
    policies = {policy.name: policy for policy in observed.profile.policies}
    assert policies["pdf_wall"].scope == "per_pdf_operation"
    assert policies["pdf_cpu"].scope == policies["pdf_address_space"].scope == "per_native_process"
    assert policies["tesseract_timeout"].scope == "per_tesseract_invocation"


def test_short_native_page_is_replaced_once_in_original_source_order(controlled_engine):
    with capture_reading.collect() as observed:
        text, _ = pdf_ocr._extract_text(
            native_pdf(["Short", "Synthetic native page second with enough text"])
        )
    assert "Short" not in text and text.index("Scanned service") < text.index("Synthetic native")
    pages = observed.page_facts().pages
    assert [page.status for page in pages] == ["ocr_complete", "native"]
    assert pages[0].native_chars == 5
    assert pages[0].native_sha256 == hashlib.sha256(b"Short").hexdigest()


def test_profile_uses_frozen_actual_timeout_and_labels_truncated_inventory(
    controlled_engine, monkeypatch
):
    from app.core.config import settings

    timeout = settings.ocr_process_timeout_seconds
    seen = []

    def recognize(*args, **kwargs):
        seen.append(kwargs["timeout"])
        return word_data("Scanned service 1 20.00 20.00")

    monkeypatch.setattr(controlled_engine, "image_to_data", recognize)
    monkeypatch.setattr(
        controlled_engine, "get_languages", lambda: [f"lang{index}" for index in range(40)]
    )
    with capture_reading.collect() as observed:
        monkeypatch.setattr(settings, "ocr_process_timeout_seconds", 27)
        pdf_ocr._extract_text(mixed_pdf())
    assert seen == [timeout]
    assert observed.profile.inventory_status == "truncated"
    assert len(observed.profile.installed_languages) == 32
    assert {policy.name: policy.value for policy in observed.profile.policies}[
        "tesseract_timeout"
    ] == timeout


def test_observed_blank_page_can_have_complete_reading_and_failed_parse(
    controlled_engine, monkeypatch
):
    monkeypatch.setattr(controlled_engine, "image_to_data", lambda *args, **kwargs: word_data(""))
    with capture_reading.collect() as observed:
        with pytest.raises(ValueError, match="Could not read any text"):
            pdf_ocr._parse_pdf("blank.pdf", native_pdf([""]))
    facts = observed.page_facts(failed=True)
    assert facts.selected_stream_complete and facts.selected_stream_bytes == 0
    assert facts.pages[0].status == "ocr_complete"
    assert facts.pages[0].selected_sha256 == hashlib.sha256(b"").hexdigest()


@pytest.mark.parametrize("value", [True, "1", 1.0])
@pytest.mark.parametrize("field", ["page_count", "source_failure_seq", "selected_stream_bytes"])
def test_counts_are_not_coerced(value, field):
    with pytest.raises(ValidationError):
        PageReadingFacts.model_validate({field: value})


@pytest.mark.parametrize("value", [True, "1", 1.0])
def test_page_ids_are_not_coerced(value):
    with pytest.raises(ValidationError):
        PageStartCheckpoint.model_validate({"page": value})


@pytest.mark.parametrize("value", [True, "12.5", float("inf"), float("nan")])
def test_geometry_is_finite_numeric_fact(value):
    with pytest.raises(ValidationError):
        PageStartCheckpoint(page=1, source_width=value)


def test_unknown_preexec_runtime_error_does_not_claim_engine_execution(
    controlled_engine, monkeypatch
):
    monkeypatch.setenv(capture_reading.NATIVE_ENABLED_ENV, "1")

    def failed_before_spawn(*args, **kwargs):
        raise RuntimeError("Synthetic pre-exec library failure")

    monkeypatch.setattr(controlled_engine, "image_to_data", failed_before_spawn)
    with capture_reading.collect() as observed:
        with pytest.raises(pdf_ocr.OcrTimedOut):
            pdf_ocr._extract_text(mixed_pdf())
    assert observed.profile.ocr_state == "unknown"
    assert observed.profile.engine_name is None
    assert observed.page_facts(failed=True).pages[1].status == "outcome_unknown"
    assert observed.page_facts(failed=True).pages[1].cause == "unknown"


def test_known_execution_timeout_preserves_witness_and_timeout_adapter(
    controlled_engine, monkeypatch
):
    monkeypatch.setenv(capture_reading.NATIVE_ENABLED_ENV, "1")
    entered = []

    class OwnedProcess:
        def communicate(self, *, timeout):
            raise subprocess.TimeoutExpired("synthetic-owned-process", timeout)

    process = OwnedProcess()

    @contextmanager
    def manager(process, seconds):
        entered.append((process, seconds))
        try:
            process.communicate(timeout=seconds)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Synthetic known native timeout") from exc
        yield b""

    monkeypatch.setattr(controlled_engine.pytesseract, "timeout_manager", manager)

    def timed_out(*args, **kwargs):
        with controlled_engine.pytesseract.timeout_manager(process, kwargs["timeout"]):
            pass

    monkeypatch.setattr(controlled_engine, "image_to_data", timed_out)
    with capture_reading.collect() as observed:
        with pytest.raises(pdf_ocr.OcrTimedOut):
            pdf_ocr._extract_text(mixed_pdf())
    assert entered and observed.profile.ocr_state == "used"
    assert observed.page_facts(failed=True).pages[1].status == "timed_out"
    assert observed.page_facts(failed=True).pages[1].cause == "engine_timeout"
    assert controlled_engine.pytesseract.timeout_manager is manager
    assert "communicate" not in process.__dict__


def test_spawned_process_without_timeout_expiry_is_used_but_page_outcome_unknown(
    controlled_engine, monkeypatch
):
    monkeypatch.setenv(capture_reading.NATIVE_ENABLED_ENV, "1")

    class OwnedProcess:
        def communicate(self, *, timeout):
            return None, b""

    process = OwnedProcess()

    @contextmanager
    def manager(process, seconds):
        yield process.communicate(timeout=seconds)[1]

    monkeypatch.setattr(controlled_engine.pytesseract, "timeout_manager", manager)

    def failed_after_spawn(*args, **kwargs):
        with controlled_engine.pytesseract.timeout_manager(process, kwargs["timeout"]):
            raise RuntimeError("Synthetic unrelated post-spawn failure")

    monkeypatch.setattr(controlled_engine, "image_to_data", failed_after_spawn)
    with capture_reading.collect() as observed:
        with pytest.raises(pdf_ocr.OcrTimedOut):
            pdf_ocr._extract_text(mixed_pdf())
    assert observed.profile.ocr_state == "used"
    assert observed.page_facts(failed=True).pages[1].status == "outcome_unknown"
    assert observed.page_facts(failed=True).pages[1].cause == "unknown"
    assert controlled_engine.pytesseract.timeout_manager is manager
    assert "communicate" not in process.__dict__


def test_missing_engine_does_not_claim_a_completed_read_or_timeout(controlled_engine, monkeypatch):
    def missing(*args, **kwargs):
        raise controlled_engine.TesseractNotFoundError()

    monkeypatch.setattr(controlled_engine, "image_to_data", missing)
    with capture_reading.collect() as observed:
        with pytest.raises(controlled_engine.TesseractNotFoundError):
            pdf_ocr._extract_text(mixed_pdf())
    assert observed.profile is None
    assert observed.page_facts(failed=True).pages[1].status == "outcome_unknown"
    assert observed.page_facts(failed=True).pages[1].selected_sha256 is None


def test_collector_module_import_does_not_load_orm():
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import app.services.capture_reading; assert 'sqlalchemy' not in sys.modules",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert process.returncode == 0, process.stderr


def test_local_inventory_is_installation_only_and_supervised(controlled_engine, monkeypatch):
    from app.services import pdf_process

    report = capture_reading._local_inventory()
    assert report.documents_read == 0 and report.engine_invoked_for_reading is False
    assert report.engine_version == "synthetic-test-engine"
    assert all(
        profile.ocr_state == "unknown" and profile.engine_name is None
        for profile in report.default_profiles
    )
    calls = []

    def supervised(*args):
        calls.append(args)
        return report.model_dump(mode="json")

    monkeypatch.setattr(pdf_process, "run", supervised)
    assert capture_reading.local_inventory() == report
    assert calls == [("inventory", "local-inventory", b"")]


def test_native_process_death_is_unknown_not_an_observed_page_timeout(monkeypatch):
    from app.services import capture_failures, pdf_process

    original = subprocess.Popen
    children = []
    program = (
        "import json,os,signal\n"
        "print(json.dumps({'reading':{'page_count':2}}),flush=True)\n"
        "print(json.dumps({'reading':{'page_started':{'page':1}}}),flush=True)\n"
        "os.kill(os.getpid(),signal.SIGKILL)\n"
    )

    def spawn(command, **kwargs):
        process = original([sys.executable, "-c", program], **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(pdf_process.subprocess, "Popen", spawn)
    with capture_reading.collect() as observed:
        with pytest.raises(capture_failures.CaptureError) as caught:
            pdf_process.run("text", "native-death.pdf", b"%PDF-1.7\n")
    assert caught.value.code == capture_failures.PROCESSING_TIMEOUT  # unchanged public contract
    assert all(child.poll() is not None for child in children)
    facts = observed.page_facts(failed=True)
    assert facts.pages[0].status == "outcome_unknown"
    assert facts.pages[0].cause == "resource_or_native_exit"


@pytest.mark.parametrize(
    "mode,status,cause",
    [
        ("deadline", "timed_out", "supervisor_timeout"),
        ("incomplete", "outcome_unknown", "result_incomplete"),
    ],
)
def test_native_parent_records_only_the_observed_failure_cause(monkeypatch, mode, status, cause):
    from app.services import capture_failures, pdf_process

    original = subprocess.Popen
    program = (
        "import json,time\n"
        "print(json.dumps({'reading':{'page_count':2}}),flush=True)\n"
        "print(json.dumps({'reading':{'page_started':{'page':1}}}),flush=True)\n"
    ) + ("time.sleep(10)\n" if mode == "deadline" else "print('{incomplete',end='',flush=True)\n")
    children = []

    def spawn(command, **kwargs):
        process = original([sys.executable, "-c", program], **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(pdf_process.subprocess, "Popen", spawn)
    monkeypatch.setattr(pdf_process, "WALL_SECONDS", 0.15)
    with capture_reading.collect() as observed:
        with pytest.raises(capture_failures.CaptureError):
            pdf_process.run("text", "native-failure.pdf", b"%PDF-1.7\n")
    assert all(child.poll() is not None for child in children)
    fact = observed.page_facts(failed=True).pages[0]
    assert (fact.status, fact.cause) == (status, cause)
    assert fact.selected_sha256 is None


def test_pdf_dpi_is_target_while_bounded_scale_and_raster_are_effective_facts(
    controlled_engine, monkeypatch
):
    closed, scales = [], []

    class Raster:
        width = height = math.floor(math.sqrt(40_000_000))
        size = (width, height)

        def close(self):
            closed.append("image")

    class Bitmap:
        def to_pil(self):
            return Raster()

        def close(self):
            closed.append("bitmap")

    class Page:
        def get_width(self):
            return 7200.0

        def get_height(self):
            return 7200.0

        def render(self, *, scale):
            scales.append(scale)
            return Bitmap()

        def close(self):
            closed.append("page")

    class Document:
        def __init__(self, content):
            pass

        def __len__(self):
            return 1

        def __getitem__(self, index):
            assert index == 0
            return Page()

        def close(self):
            closed.append("document")

    monkeypatch.setitem(sys.modules, "pypdfium2", SimpleNamespace(PdfDocument=Document))
    with capture_reading.collect() as observed:
        pdf_ocr._ocr_pages(b"controlled oversized page")
    fact = observed.page_facts().pages[0]
    assert fact.render_dpi == 300  # Configured target, not an achieved DPI claim.
    assert fact.render_scale == scales[0] and 72 * fact.render_scale < 300
    assert fact.raster_width == fact.raster_height == Raster.width
    assert fact.raster_width * fact.raster_height <= 40_000_000
    assert closed == ["image", "bitmap", "page", "document"]
