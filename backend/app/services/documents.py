"""Tenant-aware helpers for storing/loading document bytes via object storage.

The one place the app writes/reads binary originals. Handlers call these instead
of touching `core.storage` directly, so:
  • storage I/O runs off the event loop (threadpool),
  • keys are content-addressed + tenant-prefixed consistently.

Object storage is the sole home for document bytes: the legacy in-DB `*_data`
blob columns and their dual-read fallback were dropped once the migration window
closed (ADR-0008). `prefix` names the document class (`receipts`, `logos`,
`email-attachments`).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import ParamSpec, TypeVar

from fastapi.concurrency import run_in_threadpool

from app.core import storage
from app.core.errors import AppError

log = logging.getLogger("invoiceiq.documents")
_P = ParamSpec("_P")
_T = TypeVar("_T")


async def _file_work(function: Callable[_P, _T], *args: _P.args, **kwargs: _P.kwargs) -> _T:
    """Join owned native I/O before a cancelled caller may remove its files."""
    work = asyncio.create_task(run_in_threadpool(function, *args, **kwargs))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        while not work.done():
            try:
                await asyncio.shield(work)
            except asyncio.CancelledError:
                # Repeated disconnect/shutdown cancellation must not release
                # source or destination ownership ahead of the same operation.
                continue
            except Exception:
                break
        if not work.cancelled():
            work.exception()
        raise


@dataclass(frozen=True)
class StoredFile:
    """A caller-owned, private staged object; remove it on every exit path."""

    path: Path
    sha256: str
    size: int

    def cleanup(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            # Never replace a primary download/commit/cancellation exception.
            log.warning("Could not remove a private document staging file")


def _store_file(prefix: str, org_id: str, path: Path, content_type: str | None) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as source:
        while chunk := source.read(64 * 1024):
            digest.update(chunk)
            size += len(chunk)
    sha = digest.hexdigest()
    storage.get_storage().put_file(
        storage.content_key(prefix, org_id, sha), path, content_type, sha256=sha, size=size
    )
    return sha, size


async def store_file(
    prefix: str,
    org_id: str,
    path: str | Path,
    content_type: str | None = None,
    *,
    db=None,
    filename: str | None = None,
    uploaded_by: str | None = None,
) -> tuple[str, int]:
    """Store an owned file with finite reads; publish only verified bytes.

    Storage verifies the second read's identity before atomic replacement or S3
    multipart completion. The caller owns source cleanup and its DB transaction.
    """
    sha, size = await _file_work(_store_file, prefix, org_id, Path(path), content_type)
    if db is not None:
        from app.services import document_registry

        await document_registry.register(
            db,
            org_id,
            sha256=sha,
            size=size,
            kind=prefix,
            mime=content_type,
            filename=filename,
            uploaded_by=uploaded_by,
        )
    return sha, size


async def load_file(
    prefix: str, org_id: str, sha256: str | None, *, max_bytes: int | None = None
) -> StoredFile | None:
    """Stage complete storage bytes in a private file with bounded memory.

    The caller still verifies identity/admitted size and authorization before
    publishing. A failure removes only this private staging file, never a shared
    object; a successful caller must clean it after delivery or rollback.
    """
    if not sha256:
        return None
    try:
        descriptor, name = tempfile.mkstemp(prefix="bidit-document-", suffix=".tmp")
        os.close(descriptor)
    except OSError as exc:
        raise storage.StorageReadError(
            "Stored document could not be read. Check storage and retry."
        ) from exc
    staged = StoredFile(Path(name), "", 0)
    try:
        actual_sha, size = await _file_work(
            storage.get_storage().get_file,
            storage.content_key(prefix, org_id, sha256),
            staged.path,
            max_bytes=max_bytes,
        )
        return StoredFile(staged.path, actual_sha, size)
    except BaseException:
        staged.cleanup()
        raise


async def load_verified_file(
    prefix: str, org_id: str, sha256: str | None, *, size: int | None
) -> StoredFile | None:
    """Stage an admitted original without materializing it in the caller."""
    if size is not None and size < 0:
        raise OriginalIntegrityError()
    try:
        staged = await load_file(
            prefix, org_id, sha256, max_bytes=max(1, size) if size is not None else None
        )
    except storage.StorageReadTooLarge as exc:
        raise OriginalIntegrityError() from exc
    if staged is not None and (
        (size is not None and staged.size != size) or staged.sha256 != sha256
    ):
        staged.cleanup()
        raise OriginalIntegrityError()
    return staged


class OriginalIntegrityError(AppError):
    """The stored bytes cannot be verified against their admitted identity."""

    def __init__(self) -> None:
        super().__init__(
            "Stored document integrity could not be verified. Restore the original before retrying.",
            code="original_integrity_unverified",
            status=503,
            headers={"Cache-Control": "private, no-store"},
        )


async def store(
    prefix: str,
    org_id: str,
    data: bytes,
    content_type: str | None = None,
    *,
    db=None,
    filename: str | None = None,
    uploaded_by: str | None = None,
) -> tuple[str, int]:
    """Persist bytes; return (sha256, size). Idempotent — same bytes ⇒ same key.

    When a `db` session is passed, the object is also recorded in the document
    registry (Slice 5d) in that session — the caller's own commit persists it. This
    is THE choke point for binary writes, so every upload path registers here."""
    sha = storage.sha256_hex(data)
    key = storage.content_key(prefix, org_id, sha)
    await run_in_threadpool(storage.get_storage().put, key, data, content_type)
    if db is not None:
        from app.services import document_registry

        await document_registry.register(
            db,
            org_id,
            sha256=sha,
            size=len(data),
            kind=prefix,
            mime=content_type,
            filename=filename,
            uploaded_by=uploaded_by,
        )
    return sha, len(data)


async def load(
    prefix: str, org_id: str, sha256: str | None, *, max_bytes: int | None = None
) -> bytes | None:
    """Load bytes for a stored object from object storage. Returns None when
    there is no reference (`sha256` is None); raises `StorageError` when a
    referenced object is missing (an integrity fault, surfaced by `verify`)."""
    if not sha256:
        return None
    key = storage.content_key(prefix, org_id, sha256)
    if max_bytes is None:
        return await run_in_threadpool(storage.get_storage().get, key)
    return await run_in_threadpool(storage.get_storage().get, key, max_bytes=max_bytes)


async def load_capture(prefix: str, org_id: str, sha256: str | None) -> bytes | None:
    """Bound parser input, then verify the original identity before parsing."""
    from app.services import capture_failures, filesec

    try:
        content = await load(prefix, org_id, sha256, max_bytes=filesec.max_bytes())
    except storage.StorageReadTooLarge as exc:
        raise capture_failures.CaptureError(
            capture_failures.STORED_FILE_TOO_LARGE,
            "Stored document exceeds the processing size limit.",
        ) from exc
    if content is not None and await run_in_threadpool(storage.sha256_hex, content) != sha256:
        raise capture_failures.CaptureError(
            capture_failures.STORED_FILE_CHANGED,
            "Stored document does not match the original. Restore the original before retrying.",
        )
    return content


async def load_verified_original(
    prefix: str, org_id: str, sha256: str | None, *, size: int | None
) -> bytes | None:
    """Bound a stored original by its admitted size and verify its identity.

    Downloading an accepted historical original must survive a later reduction
    of the upload limit. Nullable historical metadata retains its original read
    contract; its digest is still verified. Missing and interrupted storage
    reads keep their existing distinct exceptions. The caller owns visibility,
    fresh authorization/source checks after I/O, and any download transaction.
    """
    if size is not None and size < 0:
        raise OriginalIntegrityError()
    try:
        content = await load(
            prefix, org_id, sha256, max_bytes=max(1, size) if size is not None else None
        )
    except storage.StorageReadTooLarge as exc:
        raise OriginalIntegrityError() from exc
    if content is None:
        return None
    if size is not None and len(content) != size:
        raise OriginalIntegrityError()
    if await run_in_threadpool(storage.sha256_hex, content) != sha256:
        raise OriginalIntegrityError()
    return content


async def digest(prefix: str, org_id: str, sha256: str | None) -> str | None:
    """Re-hash a historical original without materializing its bytes in memory.

    The storage backend opens, reads and closes its own source in the worker
    thread. This bounds hashing memory, not total I/O duration or source size.
    """
    if not sha256:
        return None
    key = storage.content_key(prefix, org_id, sha256)
    return await run_in_threadpool(storage.get_storage().digest, key)


async def delete(prefix: str, org_id: str, sha256: str | None) -> None:
    if sha256:
        key = storage.content_key(prefix, org_id, sha256)
        await run_in_threadpool(storage.get_storage().delete, key)


# Canonical document-class prefixes.
RECEIPTS = "receipts"
LOGOS = "logos"
EMAIL_ATTACHMENTS = "email-attachments"
UPLOADS = "uploads"  # UI direct uploads, persisted so the worker can parse off-tier
INVOICE_ATTACHMENTS = "invoice-attachments"  # internal working docs on an invoice (Phase 08)
ISSUED_ATTACHMENTS = "issued-attachments"  # supporting docs on an issued invoice (INV-4)
# WO-AF: the fuel-card statement bytes a review finding points at. Until this
# prefix existed the statement was digested, keyed on everywhere (findings,
# baselines, audit) and then discarded — a finding named a file nobody could
# open. Vaulted through THIS choke point like every other original.
STATEMENTS = "statements"
EXPORTS = "exports"  # WO-AI: a whole-archive zip produced for its owner, one-time link
