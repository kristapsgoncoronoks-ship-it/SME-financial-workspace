"""V01b: bounded PDF child protocol. No document bytes or credentials in logs.

File-backed IPC avoids buffering arbitrary child output in the API/worker. A
private directory, file-size/CPU/address-space limits and a parent wall deadline
bound native/library failures. Killing the process group also stops OCR children.
This is resource isolation, not an OS sandbox for arbitrary executable code.
"""

from __future__ import annotations

import base64
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Any

WALL_SECONDS = 90
CPU_SECONDS = 60
MAX_ATTACHMENTS = 16


def _stop(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def run(operation: str, filename: str, content: bytes):
    from app.core.config import settings
    from app.services import capture_failures, capture_progress, capture_reading, filesec

    def failure(message: str, cause="resource_or_native_exit"):
        capture_reading.operation_failed(cause)
        return capture_failures.CaptureError(capture_failures.PROCESSING_TIMEOUT, message)

    if len(content) > filesec.max_bytes():
        raise ValueError(filesec.too_large_message())
    if sys.platform != "linux":
        raise capture_failures.CaptureError(
            capture_failures.CAPTURE_UNAVAILABLE, "PDF processing requires a Linux worker."
        )
    limit = filesec.RESOURCE_BYTES["pdf_parser_output"]
    # Explicit environment: never copy database, SMTP, storage or JWT credentials.
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
        "OMP_THREAD_LIMIT": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1",
        "MAX_UPLOAD_MB": str(settings.max_upload_mb),
        "OCR_PROCESS_TIMEOUT_SECONDS": str(
            capture_reading.timeout_seconds(settings.ocr_process_timeout_seconds)
        ),
    }
    env.update(capture_reading.native_environment())
    with tempfile.TemporaryDirectory(prefix="invoice-pdf-") as directory:
        # A killed OCR child cannot run its library's scratch-file cleanup.
        env["TMPDIR"] = directory
        with tempfile.TemporaryFile() as source, tempfile.TemporaryFile() as output:
            source.write(content)
            source.seek(0)
            process = subprocess.Popen(
                [sys.executable, "-m", "app.services.pdf_process", operation, filename[:255]],
                stdin=source,
                stdout=output,
                stderr=subprocess.DEVNULL,
                cwd=directory,
                env=env,
                start_new_session=True,
            )
            offset = 0
            result = None
            deadline = time.monotonic() + WALL_SECONDS
            try:
                while True:
                    # pread does not disturb the child's shared file offset.
                    size = os.fstat(output.fileno()).st_size
                    if size > limit:
                        raise failure("PDF processing exceeded its output limit.")
                    data = os.pread(output.fileno(), size - offset, offset)
                    for line in data.splitlines(keepends=True):
                        if not line.endswith(b"\n"):
                            break
                        offset += len(line)
                        try:
                            message = json.loads(line)
                        except ValueError:
                            capture_reading.operation_failed("result_incomplete")
                            raise
                        if "progress" in message:
                            capture_progress.report(**message["progress"])
                        elif "reading" in message:
                            capture_reading.record(message["reading"])
                        else:
                            result = message
                    if process.poll() is not None:
                        if data and not data.endswith(b"\n"):
                            raise failure(
                                "PDF processing returned an incomplete result.", "result_incomplete"
                            )
                        # Drain a final complete message after the exit observation.
                        if os.fstat(output.fileno()).st_size > offset:
                            continue
                        break
                    if time.monotonic() >= deadline:
                        raise failure(
                            "Reading this PDF exceeded the processing deadline.",
                            "supervisor_timeout",
                        )
                    time.sleep(0.03)
                if process.returncode != 0 or result is None:
                    raise failure(
                        "PDF processing exceeded a resource limit or stopped unexpectedly.",
                        "resource_or_native_exit"
                        if process.returncode != 0
                        else "result_incomplete",
                    )
                if "error" in result:
                    code = result.get("code", capture_failures.MALFORMED_DOCUMENT)
                    raise capture_failures.CaptureError(code, result["error"])
                return result["result"]
            finally:
                # Always kill the group, including descendants after a normal exit.
                _stop(process)


def _emit(message: dict) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _child(operation: str, filename: str) -> None:
    import resource

    from app.services import capture_failures, capture_progress, capture_reading, filesec

    memory = filesec.RESOURCE_BYTES["pdf_parser_memory"]
    output = filesec.RESOURCE_BYTES["pdf_parser_output"]
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    resource.setrlimit(resource.RLIMIT_CPU, (CPU_SECONDS, CPU_SECONDS))
    resource.setrlimit(resource.RLIMIT_FSIZE, (output, output))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    content = sys.stdin.buffer.read(filesec.max_bytes() + 1)
    if len(content) > filesec.max_bytes():
        raise ValueError("PDF exceeds the input limit.")
    from app.services import einvoice, pdf_ocr

    capture_progress.install(
        lambda stage, done, total: _emit(
            {"progress": {"stage": stage, "pages_done": done, "pages_total": total}}
        )
    )
    context = (
        capture_reading.collect(os.environ.get(capture_reading.NATIVE_PROFILE_ENV), emit=_emit)
        if os.environ.get(capture_reading.NATIVE_ENABLED_ENV) == "1"
        else nullcontext()
    )
    with context:
        result: Any
        try:
            if operation == "invoice":
                result = pdf_ocr._parse_pdf(filename, content).model_dump(mode="json")
            elif operation == "text":
                result = pdf_ocr._extract_text(content)
            elif operation == "inventory":
                result = capture_reading._local_inventory().model_dump(mode="json")
            elif operation == "embedded":
                embedded = einvoice._extract_embedded_xml(content)
                result = (
                    base64.b64encode(embedded).decode("ascii") if embedded is not None else None
                )
            else:
                raise ValueError("Unknown PDF operation.")
            _emit({"result": result})
        except (pdf_ocr.OcrTimedOut, MemoryError) as exc:
            if isinstance(exc, MemoryError):
                capture_reading.operation_failed("resource_or_native_exit")
            _emit(
                {
                    "error": "PDF processing exceeded its resource budget.",
                    "code": capture_failures.PROCESSING_TIMEOUT,
                }
            )
        except pdf_ocr.OcrUnavailable:
            _emit(
                {
                    "error": "PDF support is unavailable on the worker.",
                    "code": capture_failures.CAPTURE_UNAVAILABLE,
                }
            )
        except Exception:
            _emit({"error": "Could not read this PDF safely. Try a shorter or clearer document."})
        finally:
            if operation in ("invoice", "text"):
                capture_reading.observe_profile("pdf")


if __name__ == "__main__":
    _child(sys.argv[1], sys.argv[2])
