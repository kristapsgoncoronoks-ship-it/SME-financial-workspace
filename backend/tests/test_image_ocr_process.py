"""Bounded preparation/protocol/process ownership; real OCR is qualified separately."""

import asyncio
import io
import json
import os
import struct
import subprocess
import sys
import threading
import time
import zlib
from pathlib import Path

import pytest
from PIL import Image
from pydantic import ValidationError

from app.core.config import settings
from app.schemas.image_reading import ImageReadingPreparation, preparation_warning
from app.services import capture_failures, filesec, image_ocr


@pytest.fixture
def local_image_reader(monkeypatch):
    """Run the actual child operation locally for controlled library spies.

    Process ownership and real OCR are qualified by their separate cohorts.
    """
    monkeypatch.setattr(image_ocr, "read", image_ocr._local_read)
    monkeypatch.setattr(image_ocr, "_emit", lambda message: None)


def image_bytes(size=(32, 16), mode="RGB", format="PNG"):
    with Image.new(mode, size, "white") as image:
        output = io.BytesIO()
        image.save(output, format=format)
        return output.getvalue()


def header_only(width, height):
    """Deliberately undecodable pixels prove refusal happens before load()."""

    def chunk(name, payload):
        return (
            struct.pack(">I", len(payload))
            + name
            + payload
            + struct.pack(">I", zlib.crc32(name + payload) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"invalid pixels"))
        + chunk(b"IEND", b"")
    )


@pytest.mark.parametrize("dimensions", [(8000, 10000), (100001, 1)])
def test_header_refusal_precedes_any_pixel_decode(monkeypatch, dimensions):
    emitted = []
    monkeypatch.setattr(image_ocr, "_emit", emitted.append)

    def forbidden(*args, **kwargs):
        raise AssertionError("Oversized source reached pixel decoding")

    monkeypatch.setattr(Image.Image, "load", forbidden)
    with pytest.raises(image_ocr.ImageOcrError) as error:
        image_ocr._prepare(header_only(*dimensions))
    assert error.value.code == capture_failures.IMAGE_PROCESSING_LIMIT
    preparation = error.value.reading_preparation
    assert preparation is not None
    assert (preparation.source_width, preparation.source_height) == dimensions
    assert preparation.raster_width is None and not preparation.resized
    assert emitted == [{"preparation": preparation.model_dump()}]


@pytest.mark.parametrize("format", ["PNG", "JPEG"])
def test_ordinary_image_preserves_pixels_mode_format_and_no_lossy_notice(monkeypatch, format):
    monkeypatch.setattr(image_ocr, "_emit", lambda message: None)
    content = image_bytes(format=format)
    raster, preparation = image_ocr._prepare(content)
    try:
        with Image.open(io.BytesIO(content)) as original:
            assert raster.mode == original.mode and raster.format == original.format
            assert raster.tobytes() == original.tobytes()
        assert (preparation.raster_width, preparation.raster_height) == (32, 16)
        assert not preparation.resized and preparation_warning(preparation) is None
    finally:
        raster.close()


def test_scaled_read_records_actual_geometry_and_closes_full_source_before_ocr(monkeypatch):
    monkeypatch.setattr(filesec, "IMAGE_OCR_RASTER_PIXELS", 32)
    emitted, closed = [], []
    original_close = Image.Image.close

    def close(image):
        closed.append(image)
        original_close(image)

    monkeypatch.setattr(Image.Image, "close", close)
    monkeypatch.setattr(image_ocr, "_emit", emitted.append)
    raster, preparation = image_ocr._prepare(image_bytes())
    try:
        assert preparation.resized and raster.width * raster.height <= 32
        assert (preparation.raster_width, preparation.raster_height) == raster.size
        assert any(image is not raster and image.size == (32, 16) for image in closed)
        assert emitted[-1] == {"preparation": preparation.model_dump()}
        assert "reduced" in preparation_warning(preparation)
        assert "original" in preparation_warning(preparation)
        with pytest.raises(ValidationError):
            preparation.source_width = 1
    finally:
        raster.close()


def test_prepared_raster_is_closed_when_publication_fails(monkeypatch):
    monkeypatch.setattr(filesec, "IMAGE_OCR_RASTER_PIXELS", 32)
    allocated, closed = [], []
    original_reduce, original_close = Image.Image.reduce, Image.Image.close

    def reduce(source, *args, **kwargs):
        raster = original_reduce(source, *args, **kwargs)
        allocated.extend((source, raster))
        assert raster.width * raster.height <= 32
        return raster

    def close(image):
        closed.append(image)
        original_close(image)

    def emit(message):
        if message["preparation"]["raster_width"] is not None:
            raise OSError("Synthetic response publication failure")

    monkeypatch.setattr(Image.Image, "reduce", reduce)
    monkeypatch.setattr(Image.Image, "close", close)
    monkeypatch.setattr(image_ocr, "_emit", emit)
    with pytest.raises(OSError, match="Synthetic response publication"):
        image_ocr._prepare(image_bytes())
    assert len(allocated) == 2 and all(image in closed for image in allocated)


@pytest.mark.parametrize("mode", ["1", "P"])
def test_reduced_palette_and_one_bit_images_have_measured_usable_rasters(monkeypatch, mode):
    monkeypatch.setattr(filesec, "IMAGE_OCR_RASTER_PIXELS", 32)
    monkeypatch.setattr(image_ocr, "_emit", lambda message: None)
    raster, preparation = image_ocr._prepare(image_bytes(mode=mode))
    try:
        assert preparation.resized and raster.width * raster.height <= 32
        assert raster.mode == ("L" if mode == "1" else "RGB")
        assert (preparation.raster_width, preparation.raster_height) == raster.size
        assert "original" in preparation_warning(preparation)
    finally:
        raster.close()


@pytest.mark.parametrize(
    "bad",
    [
        {"raster_width": 1},
        {"resized": True},
        {
            "raster_width": 2,
            "raster_height": 2,
            "resized": False,
        },
    ],
)
def test_preparation_refuses_invented_or_incomplete_effective_geometry(bad):
    with pytest.raises(ValidationError):
        ImageReadingPreparation(source_width=32, source_height=16, **bad)


def child_program(monkeypatch, program, *, ready=None):
    real = subprocess.Popen
    seen = []

    def spawn(command, **kwargs):
        assert not {"SECRET_KEY", "DATABASE_URL", "SMTP_PASSWORD", "JWT_SIGNING_KEY"} & set(
            kwargs["env"]
        )
        assert kwargs["env"]["TMPDIR"] == kwargs["cwd"]
        assert (
            float(kwargs["env"]["OCR_PROCESS_TIMEOUT_SECONDS"])
            == settings.ocr_process_timeout_seconds
        )
        process = real([sys.executable, "-c", program], **kwargs)
        seen.append((process, Path(kwargs["cwd"])))
        if ready is not None:
            deadline = time.monotonic() + 5
            while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            assert ready.exists(), "Owned native fixture did not enter its hold"
        return process

    monkeypatch.setattr(image_ocr.subprocess, "Popen", spawn)
    return seen


@pytest.mark.parametrize("message", ["[]", '{"error": []}', '{"preparation": {}}', '{"text": 4}'])
def test_malformed_native_protocol_is_a_safe_refusal(monkeypatch, message):
    seen = child_program(monkeypatch, f"print({message!r})")
    with pytest.raises(image_ocr.ImageOcrError):
        image_ocr.read(image_bytes())
    assert seen[0][0].poll() is not None and not seen[0][1].exists()


def test_native_output_is_never_clipped_into_partial_success(monkeypatch):
    program = f"print('x' * {filesec.RESOURCE_BYTES['image_ocr_output'] + 1})"
    seen = child_program(monkeypatch, program)
    with pytest.raises(image_ocr.ImageOcrError) as error:
        image_ocr.read(image_bytes())
    assert error.value.code == capture_failures.IMAGE_PROCESSING_LIMIT
    assert seen[0][0].poll() is not None and not seen[0][1].exists()


def held_scratch_program(receipt):
    return (
        "import json,time\nfrom pathlib import Path\nfrom PIL import Image\n"
        "from pytesseract.pytesseract import save\n"
        "with save(Image.new('RGB',(1,1),'white')) as (anchor,source):\n"
        f"    Path({str(receipt)!r}).write_text(json.dumps([anchor,source]))\n"
        "    time.sleep(10)\n"
    )


def assert_reaped_and_clean(seen, receipt):
    process, directory = seen[0]
    assert process.poll() is not None and not directory.exists()
    with pytest.raises(ProcessLookupError):
        os.kill(process.pid, 0)
    scratch = [Path(name) for name in json.loads(receipt.read_text())]
    assert all(not path.exists() for path in scratch)


def test_native_deadline_reaps_process_and_actual_tesseract_scratch(monkeypatch, tmp_path):
    receipt = tmp_path / "scratch.json"
    monkeypatch.setattr(settings, "ocr_process_timeout_seconds", 0.05)
    monkeypatch.setattr(image_ocr, "DECODE_GRACE_SECONDS", 0.05)
    seen = child_program(monkeypatch, held_scratch_program(receipt), ready=receipt)
    with pytest.raises(image_ocr.ImageOcrError) as error:
        image_ocr.read(image_bytes())
    assert error.value.code == capture_failures.PROCESSING_TIMEOUT
    assert_reaped_and_clean(seen, receipt)


@pytest.mark.asyncio
@pytest.mark.parametrize("repeated", [False, True])
async def test_cancellation_joins_reaping_through_repeated_cancel(monkeypatch, tmp_path, repeated):
    receipt = tmp_path / "scratch.json"
    seen = child_program(monkeypatch, held_scratch_program(receipt), ready=receipt)
    stopped, release = threading.Event(), threading.Event()
    original_stop = image_ocr._stop

    def held_cleanup(process):
        original_stop(process)
        stopped.set()
        assert release.wait(timeout=10)

    monkeypatch.setattr(image_ocr, "_stop", held_cleanup)
    task = asyncio.create_task(image_ocr.run_owned(image_ocr.read, image_bytes()))
    try:
        deadline = time.monotonic() + 5
        while not receipt.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert receipt.exists()
        task.cancel()
        assert await asyncio.to_thread(stopped.wait, 5)
        assert not task.done()
        if repeated:
            task.cancel()
            for _ in range(5):
                await asyncio.sleep(0)
            assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert_reaped_and_clean(seen, receipt)


def test_private_io_failure_is_sanitized_and_classified(monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError("private original and credentials must not be returned")

    monkeypatch.setattr(image_ocr.tempfile, "TemporaryDirectory", unavailable)
    with pytest.raises(image_ocr.ImageOcrError) as error:
        image_ocr.read(image_bytes())
    assert error.value.code == capture_failures.CAPTURE_UNAVAILABLE
    assert "private original" not in str(error.value)
