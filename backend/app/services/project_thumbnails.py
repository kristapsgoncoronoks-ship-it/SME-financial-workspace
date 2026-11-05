"""Optional bounded photo derivatives; original bytes remain the evidence."""

from __future__ import annotations

import asyncio
import io
import subprocess
import sys
import tempfile
import weakref
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi.concurrency import run_in_threadpool

from app.core.errors import AppError
from app.services import filesec

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

MAX_EDGE = 320
MAX_PIXELS = 8_000_000
RENDER_SECONDS = 6


@dataclass
class _Lane:
    lock: asyncio.Lock
    users: int = 0


_locks: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


@asynccontextmanager
async def _decoder_slot():
    loop = asyncio.get_running_loop()
    lane = _locks.setdefault(loop, _Lane(asyncio.Lock()))
    lane.users += 1
    try:
        async with lane.lock:
            yield
    finally:
        lane.users -= 1
        if lane.users == 0:
            del _locks[loop]


def unavailable() -> AppError:
    return AppError(
        "A small preview is unavailable for this photo. Download the original to view it.",
        code="project_photo_preview_unavailable",
        status=422,
        headers={"Cache-Control": "private, no-store"},
    )


def _render(content: bytes) -> bytes:
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(content)) as source:
        if (
            source.format not in {"JPEG", "PNG", "WEBP"}
            or source.width * source.height > MAX_PIXELS
        ):
            raise ValueError("unsupported preview geometry/format")
        # Decode only the first frame, orient it for viewing, and omit EXIF,
        # location, textual and color-profile metadata from the derivative.
        source.seek(0)
        image = ImageOps.exif_transpose(source)
        try:
            image.thumbnail((MAX_EDGE, MAX_EDGE))
            rgba = image.convert("RGBA")
            try:
                background = Image.new("RGBA", rgba.size, "white")
                try:
                    background.alpha_composite(rgba)
                    rgb = background.convert("RGB")
                    try:
                        output = io.BytesIO()
                        rgb.save(output, format="JPEG", quality=75)
                        data = output.getvalue()
                    finally:
                        rgb.close()
                finally:
                    background.close()
            finally:
                rgba.close()
        finally:
            image.close()
    if len(data) > filesec.RESOURCE_BYTES["project_thumbnail"]:
        raise ValueError("preview output budget")
    return data


def render(content: bytes) -> bytes:
    if len(content) > filesec.RESOURCE_BYTES["project_thumbnail_input"]:
        raise unavailable()
    try:
        # A real file bounds output independently of pipe-buffer growth. The
        # child inherits the private temporary file and the file-size rlimit.
        with tempfile.TemporaryFile() as output:
            subprocess.run(
                [sys.executable, "-m", "app.services.project_thumbnails", "--render"],
                input=content,
                stdout=output,
                stderr=subprocess.DEVNULL,
                timeout=RENDER_SECONDS,
                check=True,
            )
            output.seek(0)
            data = output.read(filesec.RESOURCE_BYTES["project_thumbnail"] + 1)
        if (
            not data.startswith(b"\xff\xd8\xff")
            or len(data) > filesec.RESOURCE_BYTES["project_thumbnail"]
        ):
            raise ValueError("invalid preview output")
        return data
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise unavailable() from exc


async def _prepare_admitted(
    db: AsyncSession, org_id: str, project_id: str, document_id: str
) -> tuple[str, int, bytes]:
    # The native decoder child only needs file budgets and image libraries.
    # Keep database models and financial services in the API-side operation.
    from sqlalchemy import select

    from app.models.document import Document
    from app.services import documents, project_profit

    row = await project_profit.get_document(db, org_id, project_id, document_id)
    if row.kind != "photo":
        raise project_profit.NotFoundError("Document not found")
    sha = row.sha256
    size = await db.scalar(
        select(Document.size).where(
            Document.org_id == org_id,
            Document.kind == project_profit.PROJECT_DOCS,
            Document.sha256 == sha,
        )
    )
    if size is not None and size < 0:
        raise documents.OriginalIntegrityError()
    if size is None or size > filesec.RESOURCE_BYTES["project_thumbnail_input"]:
        raise unavailable()
    _row, content = await project_profit.load_document(
        db, org_id, project_id, document_id, expected_identity=(sha, size)
    )
    data = await run_in_threadpool(render, content)
    return sha, size, data


async def prepare(
    db: AsyncSession, org_id: str, project_id: str, document_id: str
) -> tuple[str, int, bytes]:
    # One source read and decoder per server loop. Acquire before loading the
    # original so an offscreen grid cannot hold many original blobs in memory.
    async with _decoder_slot():
        # A native asyncio Task.cancel() can abandon a threadpool await even
        # while the storage read or decoder child is still running. Keep the
        # admission slot and request-owned DB session alive until our owned
        # operation finishes, then propagate cancellation to the caller.
        work = asyncio.create_task(_prepare_admitted(db, org_id, project_id, document_id))
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            while not work.done():
                try:
                    await asyncio.shield(work)
                except asyncio.CancelledError:
                    # Repeated disconnect/shutdown cancellation must not
                    # release the slot ahead of the same admitted operation.
                    continue
                except Exception:
                    break
            if not work.cancelled():
                work.exception()  # Retrieve a failure from the abandoned request.
            raise


if __name__ == "__main__":
    if sys.platform == "linux":
        import resource

        memory = filesec.RESOURCE_BYTES["capture_renderer_memory"]
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
        resource.setrlimit(resource.RLIMIT_CPU, (4, 4))
        maximum = filesec.RESOURCE_BYTES["project_thumbnail"]
        resource.setrlimit(resource.RLIMIT_FSIZE, (maximum, maximum))
    source = sys.stdin.buffer.read(filesec.RESOURCE_BYTES["project_thumbnail_input"] + 1)
    if len(source) > filesec.RESOURCE_BYTES["project_thumbnail_input"]:
        raise ValueError("preview input budget")
    sys.stdout.buffer.write(_render(source))
