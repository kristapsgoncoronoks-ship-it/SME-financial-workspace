"""Supervised local image reading; original admission/storage stay authoritative."""

from __future__ import annotations

import asyncio
import io
import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import ParamSpec, TypeVar

from fastapi.concurrency import run_in_threadpool

from app.schemas.image_reading import ImageReadingPreparation, preparation_warning
from app.services import capture_failures, filesec

_P = ParamSpec("_P")
_T = TypeVar("_T")
_cancel: ContextVar[threading.Event | None] = ContextVar("image_ocr_cancel", default=None)
DECODE_GRACE_SECONDS = 20
_LIMIT_DETAIL = (
    "This image reached the safe reading limits. Use a smaller image or enter the fields manually."
)
_ERRORS = {
    capture_failures.IMAGE_PROCESSING_LIMIT: _LIMIT_DETAIL,
    capture_failures.PROCESSING_TIMEOUT: "Reading this image exceeded the processing deadline.",
    capture_failures.CAPTURE_UNAVAILABLE: "Image reading is unavailable on this worker.",
    capture_failures.UNREADABLE_SCAN: "Could not read this image safely. Try a clearer image.",
}


class ImageOcrError(capture_failures.CaptureError):
    def __init__(self, code: str, preparation: ImageReadingPreparation | None = None) -> None:
        super().__init__(code, _ERRORS[code])
        self.reading_preparation = preparation


@dataclass(frozen=True)
class ImageReading:
    text: str
    preparation: ImageReadingPreparation

    @property
    def warnings(self) -> list[str]:
        warning = preparation_warning(self.preparation)
        return [warning] if warning else []


def preparation_for_error(error: BaseException) -> ImageReadingPreparation | None:
    preparation = getattr(error, "reading_preparation", None)
    return preparation if isinstance(preparation, ImageReadingPreparation) else None


async def run_owned(function: Callable[_P, _T], *args: _P.args, **kwargs: _P.kwargs) -> _T:
    """Signal and join native image cleanup through repeated task cancellation."""
    event = threading.Event()
    token = _cancel.set(event)
    work = asyncio.create_task(run_in_threadpool(function, *args, **kwargs))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        event.set()
        while not work.done():
            try:
                await asyncio.shield(work)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not work.cancelled():
            work.exception()
        raise
    finally:
        _cancel.reset(token)


async def run_reading(function: Callable[[str, bytes], _T], filename: str, content: bytes) -> _T:
    """Own image decoder cancellation; keep other parser paths unchanged."""
    if content.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")):
        return await run_owned(function, filename, content)
    return await run_in_threadpool(function, filename, content)


def _stop(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def read(content: bytes) -> ImageReading:
    try:
        return _read(content)
    except OSError as exc:
        # Private mount/decoder paths and native diagnostics stay internal.
        raise ImageOcrError(capture_failures.CAPTURE_UNAVAILABLE) from exc


def _read(content: bytes) -> ImageReading:
    """Read complete bounded native output, never a clipped or partial success."""
    from app.core.config import settings

    if len(content) > filesec.max_bytes():
        raise ValueError(filesec.too_large_message())
    if sys.platform != "linux":
        raise ImageOcrError(capture_failures.CAPTURE_UNAVAILABLE)
    event = _cancel.get()
    if event is not None and event.is_set():
        raise asyncio.CancelledError()
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
        "MAX_UPLOAD_MB": str(settings.max_upload_mb),
        "OCR_PROCESS_TIMEOUT_SECONDS": str(settings.ocr_process_timeout_seconds),
        "OMP_THREAD_LIMIT": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1",
    }
    if os.environ.get("TESSDATA_PREFIX"):
        env["TESSDATA_PREFIX"] = os.environ["TESSDATA_PREFIX"]
    limit = filesec.RESOURCE_BYTES["image_ocr_output"]
    preparation = None
    with tempfile.TemporaryDirectory(prefix="invoice-image-") as directory:
        env["TMPDIR"] = directory
        with (
            tempfile.TemporaryFile(dir=directory) as source,
            tempfile.TemporaryFile(dir=directory) as output,
        ):
            source.write(content)
            source.seek(0)
            process = subprocess.Popen(
                [sys.executable, "-m", "app.services.image_ocr"],
                stdin=source,
                stdout=output,
                stderr=subprocess.DEVNULL,
                cwd=directory,
                env=env,
                start_new_session=True,
            )
            offset, result = 0, None
            deadline = (
                time.monotonic() + settings.ocr_process_timeout_seconds + DECODE_GRACE_SECONDS
            )
            try:
                while True:
                    if event is not None and event.is_set():
                        raise asyncio.CancelledError()
                    size = os.fstat(output.fileno()).st_size
                    if size > limit:
                        raise ImageOcrError(capture_failures.IMAGE_PROCESSING_LIMIT, preparation)
                    data = os.pread(output.fileno(), size - offset, offset)
                    for line in data.splitlines(keepends=True):
                        if not line.endswith(b"\n"):
                            break
                        offset += len(line)
                        try:
                            message = json.loads(line)
                            if not isinstance(message, dict):
                                raise ValueError("Invalid native reading response")
                            if "preparation" in message:
                                preparation = ImageReadingPreparation.model_validate(
                                    message["preparation"]
                                )
                            if "text" in message or "error" in message:
                                result = message
                        except (ValueError, TypeError) as exc:
                            raise ImageOcrError(
                                capture_failures.UNREADABLE_SCAN, preparation
                            ) from exc
                    if process.poll() is not None:
                        if os.fstat(output.fileno()).st_size > offset:
                            if data and not data.endswith(b"\n"):
                                raise ImageOcrError(capture_failures.UNREADABLE_SCAN, preparation)
                            continue
                        break
                    if time.monotonic() >= deadline:
                        raise ImageOcrError(capture_failures.PROCESSING_TIMEOUT, preparation)
                    time.sleep(0.03)
                if process.returncode != 0 or result is None:
                    raise ImageOcrError(capture_failures.IMAGE_PROCESSING_LIMIT, preparation)
                if "error" in result:
                    code = result["error"]
                    if not isinstance(code, str) or code not in _ERRORS:
                        code = capture_failures.UNREADABLE_SCAN
                    raise ImageOcrError(code, preparation)
                text = result.get("text")
                if (
                    not isinstance(text, str)
                    or len(text.encode("utf-8")) > filesec.RESOURCE_BYTES["image_ocr_text"]
                    or preparation is None
                    or preparation.raster_width is None
                    or preparation.raster_height is None
                ):
                    raise ImageOcrError(capture_failures.IMAGE_PROCESSING_LIMIT, preparation)
                return ImageReading(text, preparation)
            finally:
                _stop(process)


def _prepare(content: bytes):
    from PIL import Image

    try:
        source: Image.Image = Image.open(io.BytesIO(content))
    except Image.DecompressionBombError as exc:
        raise ImageOcrError(capture_failures.IMAGE_PROCESSING_LIMIT) from exc
    raster = None
    try:
        preparation = ImageReadingPreparation(
            source_width=source.width, source_height=source.height
        )
        _emit({"preparation": preparation.model_dump()})
        if (
            source.format not in {"PNG", "JPEG"}
            or source.width * source.height > filesec.IMAGE_OCR_SOURCE_PIXELS
            or max(source.size) > filesec.IMAGE_OCR_SOURCE_EDGE
        ):
            raise ImageOcrError(capture_failures.IMAGE_PROCESSING_LIMIT, preparation)
        factor = math.ceil(
            max(
                1.0,
                math.sqrt(source.width * source.height / filesec.IMAGE_OCR_RASTER_PIXELS),
                max(source.size) / filesec.IMAGE_OCR_RASTER_EDGE,
            )
        )
        if factor > 1:
            # Integer reduction avoids a large full-width resampling temporary.
            # Never enlarge it to fill the envelope: disclose its actual size.
            if source.mode in {"1", "P"}:
                # Pillow's integer reducer requires a continuous-tone mode.
                # Keep unchanged ordinary images on their original path.
                mode = (
                    "L"
                    if source.mode == "1"
                    else "RGBA"
                    if "transparency" in source.info
                    else "RGB"
                )
                converted = source.convert(mode)
                source.close()
                source = converted
            raster = source.reduce(factor)
            source.close()
        else:
            # Preserve the original mode/format for unchanged ordinary images.
            raster = source
        preparation = preparation.model_copy(
            update={
                "raster_width": raster.width,
                "raster_height": raster.height,
                "resized": factor > 1,
            }
        )
        _emit({"preparation": preparation.model_dump()})
        return raster, preparation
    except BaseException:
        if raster is not None and raster is not source:
            raster.close()
        source.close()
        raise


def _emit(message: dict) -> None:
    encoded = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
    if len(encoded) > filesec.RESOURCE_BYTES["image_ocr_output"]:
        raise ImageOcrError(capture_failures.IMAGE_PROCESSING_LIMIT)
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def _recognize(raster) -> str:
    import pytesseract

    from app.services import pdf_ocr

    try:
        data = pdf_ocr.ocr_image_to_data(pytesseract, raster)
    except pytesseract.TesseractNotFoundError as exc:
        raise ImageOcrError(capture_failures.CAPTURE_UNAVAILABLE) from exc
    except pytesseract.TesseractError as exc:
        if exc.status in (-signal.SIGKILL, -signal.SIGXCPU, -signal.SIGXFSZ):
            raise ImageOcrError(capture_failures.IMAGE_PROCESSING_LIMIT) from exc
        raise ImageOcrError(capture_failures.UNREADABLE_SCAN) from exc
    return pdf_ocr._reconstruct_lines(data)


def _local_read(content: bytes) -> ImageReading:
    """The native child operation, isolated separately from its IPC owner."""
    from app.services import pdf_ocr

    preparation = None
    raster = None
    try:
        raster, preparation = _prepare(content)
        text = _recognize(raster)
        if len(text.encode("utf-8")) > filesec.RESOURCE_BYTES["image_ocr_text"]:
            raise ImageOcrError(capture_failures.IMAGE_PROCESSING_LIMIT, preparation)
        return ImageReading(text, preparation)
    except ImageOcrError as exc:
        if exc.reading_preparation is None and preparation is not None:
            raise ImageOcrError(exc.code, preparation) from exc
        raise
    except pdf_ocr.OcrTimedOut as exc:
        raise ImageOcrError(capture_failures.PROCESSING_TIMEOUT, preparation) from exc
    except MemoryError as exc:
        raise ImageOcrError(capture_failures.IMAGE_PROCESSING_LIMIT, preparation) from exc
    except (ImportError, pdf_ocr.OcrUnavailable) as exc:
        raise ImageOcrError(capture_failures.CAPTURE_UNAVAILABLE, preparation) from exc
    except Exception as exc:
        raise ImageOcrError(capture_failures.UNREADABLE_SCAN, preparation) from exc
    finally:
        if raster is not None:
            raster.close()


def _child() -> None:
    import resource

    from app.core.config import settings

    memory = filesec.RESOURCE_BYTES["image_ocr_memory"]
    temporary = filesec.RESOURCE_BYTES["image_ocr_temp_file"]
    cpu = math.ceil(settings.ocr_process_timeout_seconds + DECODE_GRACE_SECONDS)
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    resource.setrlimit(resource.RLIMIT_FSIZE, (temporary, temporary))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    try:
        content = sys.stdin.buffer.read(filesec.max_bytes() + 1)
        if len(content) > filesec.max_bytes():
            raise ImageOcrError(capture_failures.IMAGE_PROCESSING_LIMIT)
        reading = _local_read(content)
        _emit({"text": reading.text})
    except ImageOcrError as exc:
        _emit({"error": exc.code})
    except MemoryError:
        _emit({"error": capture_failures.IMAGE_PROCESSING_LIMIT})
    except ImportError:
        _emit({"error": capture_failures.CAPTURE_UNAVAILABLE})
    except Exception:
        _emit({"error": capture_failures.UNREADABLE_SCAN})


if __name__ == "__main__":
    _child()
