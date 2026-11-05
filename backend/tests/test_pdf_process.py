"""Real process-boundary regressions: kill/reap, bounded output and recovery."""

import io
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
from pypdf import PdfWriter

from app.services import capture_failures, capture_progress, einvoice, pdf_ocr, pdf_process


def pdf(*, pages=1, attachments=0, attachment_size=10):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=100, height=100)
    for number in range(attachments):
        writer.add_attachment(f"{number}.xml", b"x" * attachment_size)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def test_attachment_count_and_decoded_bytes_rejected():
    for content in (pdf(attachments=17), pdf(attachments=1, attachment_size=4_194_305)):
        with pytest.raises(capture_failures.CaptureError):
            einvoice.extract_embedded_xml(content)
    assert einvoice.extract_embedded_xml(pdf()) is None


def test_page_cap_rejects_instead_of_returning_partial_result():
    with pytest.raises(capture_failures.CaptureError):
        pdf_ocr.extract_text(pdf(pages=51))


def test_worker_hard_limits_and_no_inherited_secrets(monkeypatch):
    real = subprocess.Popen
    seen = []
    monkeypatch.setenv("SECRET_KEY", "must-never-reach-pdf-worker")

    def spawn(command, **kwargs):
        assert "SECRET_KEY" not in kwargs["env"]
        process = real(command, **kwargs)
        seen.append(process)
        return process

    monkeypatch.setattr(pdf_process.subprocess, "Popen", spawn)
    assert einvoice.extract_embedded_xml(pdf()) is None
    assert seen[0].poll() == 0
    with pytest.raises(ProcessLookupError):
        os.kill(seen[0].pid, 0)


@pytest.mark.parametrize("program", ["while True: pass", "print('x' * 9_000_000)"])
def test_runaway_child_is_reaped_and_next_document_proceeds(monkeypatch, program):
    real = subprocess.Popen
    seen = []

    def spawn(command, **kwargs):
        process = real([sys.executable, "-c", program], **kwargs)
        seen.append(process)
        return process

    with monkeypatch.context() as patch:
        patch.setattr(pdf_process.subprocess, "Popen", spawn)
        patch.setattr(pdf_process, "WALL_SECONDS", 0.2)
        with pytest.raises(capture_failures.CaptureError) as error:
            einvoice.extract_embedded_xml(pdf())
        assert error.value.code == capture_failures.PROCESSING_TIMEOUT
    assert seen[0].poll() is not None
    with pytest.raises(ProcessLookupError):
        os.kill(seen[0].pid, 0)
    assert einvoice.extract_embedded_xml(pdf()) is None


def test_timed_out_pdf_child_cleans_actual_ocr_scratch_and_recovers(monkeypatch, tmp_path):
    """A killed OCR context cannot run pytesseract's Python cleanup finally."""
    real = subprocess.Popen
    receipt = tmp_path / "ocr-scratch.json"
    program = (
        "import json,time\n"
        "from pathlib import Path\n"
        "from PIL import Image\n"
        "from pytesseract.pytesseract import save\n"
        "image=Image.new('RGB',(1,1),'white')\n"
        "with save(image) as (anchor,source):\n"
        f"    Path({str(receipt)!r}).write_text(json.dumps([anchor,source]))\n"
        "    time.sleep(10)\n"
    )
    seen = []

    def spawn(command, **kwargs):
        process = real([sys.executable, "-c", program], **kwargs)
        seen.append((process, Path(kwargs["cwd"])))
        deadline = time.monotonic() + 5
        while not receipt.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        if not receipt.exists():
            process.kill()
            process.wait()
            raise AssertionError("Synthetic OCR scratch fixture did not start")
        return process

    recorded = []
    try:
        with monkeypatch.context() as patch:
            patch.setattr(pdf_process.subprocess, "Popen", spawn)
            patch.setattr(pdf_process, "WALL_SECONDS", 0.1)
            with pytest.raises(capture_failures.CaptureError) as error:
                einvoice.extract_embedded_xml(pdf())
        assert error.value.code == capture_failures.PROCESSING_TIMEOUT
        process, working = seen[0]
        assert process.poll() is not None and not working.exists()
        with pytest.raises(ProcessLookupError):
            os.kill(process.pid, 0)
        recorded = [Path(name) for name in json.loads(receipt.read_text())]
        assert len(recorded) == 2
        assert all(not path.exists() for path in recorded)
    finally:
        # The failure-first control also removes only its own exact files.
        for path in recorded:
            assert path.name.startswith("tess_") and path.parent in {
                Path(tempfile.gettempdir()),
                seen[0][1],
            }
            path.unlink(missing_ok=True)
    assert einvoice.extract_embedded_xml(pdf()) is None


def test_progress_crosses_process_boundary():
    from reportlab.pdfgen import canvas

    output = io.BytesIO()
    document = canvas.Canvas(output)
    document.drawString(20, 100, "Synthetic invoice with text long enough to avoid OCR")
    document.save()
    seen = []
    token = capture_progress.install(lambda stage, done, total: seen.append(stage))
    try:
        parsed = pdf_ocr.parse_pdf("synthetic.pdf", output.getvalue())
    finally:
        capture_progress.uninstall(token)
    assert parsed.method == "text-layer"
    assert seen == [capture_progress.READING, capture_progress.INTERPRETING]


@pytest.mark.asyncio
async def test_timeout_persisted_then_another_upload_succeeds(auth_client, db_session, monkeypatch):
    from app.models.extraction_run import ExtractionRun
    from app.services import jobs

    with monkeypatch.context() as patch:
        patch.setattr(pdf_process, "WALL_SECONDS", 0)
        response = await auth_client.post(
            "/api/v1/invoices/upload", files={"file": ("slow.pdf", pdf(), "application/pdf")}
        )
        assert response.status_code == 202
        await jobs.run_once(db_session, "isolation-test")
    run = await db_session.get(ExtractionRun, response.json()["extraction_run_id"])
    await db_session.refresh(run)
    assert run.status == "failed"
    assert run.failure_code == capture_failures.PROCESSING_TIMEOUT
    assert run.stage == capture_progress.DONE
    response = await auth_client.post(
        "/api/v1/invoices/upload",
        files={"file": ("next.csv", b"description,amount\nSynthetic item,10.00\n", "text/csv")},
    )
    assert response.status_code == 202
    await jobs.run_once(db_session, "isolation-test")
    next_run = await db_session.get(ExtractionRun, response.json()["extraction_run_id"])
    await db_session.refresh(next_run)
    assert next_run.status == "parsed"


@pytest.mark.parametrize("mode", ["cpu", "memory"])
def test_real_child_kernel_limits(monkeypatch, mode):
    real = subprocess.Popen
    expression = "bytes(2_000_000_000)" if mode == "memory" else "spin()"
    program = (
        "from app.services import pdf_process, pdf_ocr\n"
        "pdf_process.CPU_SECONDS = 1\n"
        "def spin():\n    while True: pass\n"
        f"pdf_ocr._parse_pdf = lambda *args: {expression}\n"
        "pdf_process._child('invoice', 'synthetic.pdf')\n"
    )

    def spawn(command, **kwargs):
        return real([sys.executable, "-c", program], **kwargs)

    monkeypatch.setattr(pdf_process.subprocess, "Popen", spawn)
    with pytest.raises(capture_failures.CaptureError) as error:
        pdf_process.run("invoice", "synthetic.pdf", pdf())
    assert error.value.code == capture_failures.PROCESSING_TIMEOUT
