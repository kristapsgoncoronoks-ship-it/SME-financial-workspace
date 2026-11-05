"""Object storage abstraction for document bytes (ADR-0008).

Large binary originals (receipts, logos, email attachments) belong in object
storage, not the primary database — see docs/architecture/adr/0008-object-storage.md.
This module is the swappable seam: a tiny `Storage` protocol with three backends:

  • MemoryStorage — in-process dict; used by tests (no disk, isolated).
  • LocalStorage  — filesystem under a root; the default for dev/single-node.
  • S3Storage     — any S3-compatible service (AWS S3, MinIO); the prod target.

Keys are CONTENT-ADDRESSED (sha256), sharded, and tenant-prefixed, which gives
deduplication and integrity for free. The backend is chosen from settings and
cached per process; tests swap it via `set_storage`.

The methods are synchronous (filesystem/boto3 are blocking); callers on the async
path wrap them via `services.documents` which uses a threadpool.
"""

from __future__ import annotations

import hashlib
import io
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import BinaryIO, Protocol, runtime_checkable

from app.core.config import settings

_READ_CHUNK_BYTES = 64 * 1024
_MULTIPART_BYTES = 8 * 1024 * 1024
log = logging.getLogger("invoiceiq.storage")


class StorageError(Exception):
    """Raised when a stored object cannot be read/written (missing key, backend error)."""


class StorageReadTooLarge(StorageError):
    """The stored object exceeds the caller's processing budget; never truncated."""


class StorageReadError(StorageError):
    """An object could not be read completely; distinct from a missing object."""


class StorageWriteError(StorageError):
    """A file upload could not publish its complete verified original."""


def _copy(source, target, max_bytes: int | None = None) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    while True:
        amount = _READ_CHUNK_BYTES
        if max_bytes is not None:
            amount = min(amount, max_bytes + 1 - size)
        chunk = source.read(amount)
        if not chunk:
            return digest.hexdigest(), size
        size += len(chunk)
        if max_bytes is not None and size > max_bytes:
            raise StorageReadTooLarge("Stored document exceeds the processing size limit.")
        digest.update(chunk)
        target.write(chunk)


def _copy_and_close(source, destination: Path, max_bytes: int | None) -> tuple[str, int]:
    try:
        with destination.open("wb") as target:
            identity = _copy(source, target, max_bytes)
    except BaseException:
        try:
            source.close()
        except OSError:
            pass
        raise
    else:
        source.close()
        return identity


def _verify_file(identity: tuple[str, int], sha256: str, size: int) -> None:
    if identity != (sha256, size):
        raise StorageWriteError("Document changed while preparing its upload. Retry the export.")


@contextmanager
def _source_file(path: Path) -> Iterator[BinaryIO]:
    source = path.open("rb")
    try:
        yield source
    except BaseException:
        try:
            source.close()
        except OSError:
            log.warning("Could not close an interrupted private upload source")
        raise
    else:
        source.close()


def _digest(source) -> str:
    digest = hashlib.sha256()
    while chunk := source.read(_READ_CHUNK_BYTES):
        digest.update(chunk)
    return digest.hexdigest()


def _digest_and_close(source) -> str:
    try:
        digest = _digest(source)
    except BaseException:
        # Preserve the original read/hash failure or cancellation during cleanup.
        try:
            source.close()
        except OSError:
            pass
        raise
    else:
        source.close()  # A failed close invalidates an otherwise successful hash.
        return digest


def _validate_read_limit(max_bytes: int | None) -> None:
    if max_bytes is not None and max_bytes <= 0:
        raise ValueError("Stored-object read limit must be positive.")


def _read(source, max_bytes: int | None) -> bytes:
    if max_bytes is None:
        return source.read()
    data = bytearray()
    while True:
        chunk = source.read(min(_READ_CHUNK_BYTES, max_bytes + 1 - len(data)))
        if not chunk:
            return bytes(data)
        data.extend(chunk)
        if len(data) > max_bytes:
            raise StorageReadTooLarge("Stored document exceeds the processing size limit.")


def _read_and_close(source, max_bytes: int | None) -> bytes:
    try:
        data = _read(source, max_bytes)
    except BaseException:
        # Cleanup must not replace an oversize refusal, cancellation or the
        # original read failure with a secondary I/O error.
        try:
            source.close()
        except OSError:
            pass
        raise
    else:
        source.close()  # A failed close invalidates an otherwise successful read.
        return data


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_key(prefix: str, org_id: str, sha256: str) -> str:
    """Tenant-prefixed, sharded, content-addressed key.

    e.g. ``receipts/<org>/ab/cd/<sha256>`` — the two shard levels keep any single
    directory / prefix from growing unbounded.
    """
    return f"{prefix}/{org_id}/{sha256[:2]}/{sha256[2:4]}/{sha256}"


@runtime_checkable
class Storage(Protocol):
    def put(self, key: str, data: bytes, content_type: str | None = None) -> None: ...
    def get(self, key: str, *, max_bytes: int | None = None) -> bytes: ...
    def digest(self, key: str) -> str: ...
    def delete(self, key: str) -> None: ...
    def exists(self, key: str) -> bool: ...
    def put_file(
        self, key: str, source: Path, content_type: str | None = None, *, sha256: str, size: int
    ) -> None: ...
    def get_file(
        self, key: str, destination: Path, *, max_bytes: int | None = None
    ) -> tuple[str, int]: ...


class MemoryStorage:
    """In-process store for tests. Not durable; not for production."""

    def __init__(self) -> None:
        self._data: dict[str, bytes] = {}

    def put(self, key: str, data: bytes, content_type: str | None = None) -> None:
        self._data[key] = bytes(data)

    def get(self, key: str, *, max_bytes: int | None = None) -> bytes:
        _validate_read_limit(max_bytes)
        try:
            data = self._data[key]
        except KeyError as e:
            raise StorageError(f"object not found: {key}") from e
        if max_bytes is not None and len(data) > max_bytes:
            raise StorageReadTooLarge("Stored document exceeds the processing size limit.")
        return data

    def delete(self, key: str) -> None:
        self._data.pop(key, None)

    def digest(self, key: str) -> str:
        try:
            return sha256_hex(self._data[key])
        except KeyError as e:
            raise StorageError(f"object not found: {key}") from e

    def exists(self, key: str) -> bool:
        return key in self._data

    def put_file(
        self, key: str, source: Path, content_type: str | None = None, *, sha256: str, size: int
    ) -> None:
        # This test backend necessarily owns the complete blob. The file API
        # bounds source reads, not the memory needed by an in-memory store.
        try:
            with _source_file(source) as stream, io.BytesIO() as target:
                identity = _copy(stream, target)
                _verify_file(identity, sha256, size)
                self.put(key, target.getvalue(), content_type)
        except OSError as exc:
            raise StorageWriteError(
                "Stored document could not be written. Retry after recovery."
            ) from exc

    def get_file(
        self, key: str, destination: Path, *, max_bytes: int | None = None
    ) -> tuple[str, int]:
        data = self.get(key, max_bytes=max_bytes)
        digest = hashlib.sha256()
        try:
            with destination.open("wb") as target:
                view = memoryview(data)
                for offset in range(0, len(data), _READ_CHUNK_BYTES):
                    chunk = view[offset : offset + _READ_CHUNK_BYTES]
                    digest.update(chunk)
                    target.write(chunk)
        except OSError as exc:
            raise StorageReadError(
                "Stored document could not be read. Check storage and retry."
            ) from exc
        return digest.hexdigest(), len(data)


class LocalStorage:
    """Filesystem-backed storage under a root directory (dev / single node)."""

    def __init__(self, root: str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # Keys never contain '..'; they are derived from sha256 + org id. Containment
        # is checked via Path.is_relative_to (a true path-boundary comparison), not a
        # bare string prefix: a bare `startswith` would wrongly admit a sibling
        # directory whose name happens to extend the root's, e.g. root=/data/store
        # would accept /data/store-evil/secret (R10).
        root = self.root.resolve()
        p = (self.root / key).resolve()
        if p != root and not p.is_relative_to(root):
            raise StorageError(f"illegal key path: {key}")
        return p

    def put(self, key: str, data: bytes, content_type: str | None = None) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        # A shared `<key>.tmp` lets simultaneous content-addressed uploads steal
        # each other's staging file. Each writer owns a private sibling instead.
        tmp: Path | None = None
        try:
            with NamedTemporaryFile(
                dir=p.parent, prefix=".upload-", suffix=".tmp", delete=False
            ) as staged:
                tmp = Path(staged.name)
                staged.write(data)
            tmp.replace(p)  # atomic within the same filesystem
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)

    def get(self, key: str, *, max_bytes: int | None = None) -> bytes:
        _validate_read_limit(max_bytes)
        try:
            source = self._path(key).open("rb")
        except FileNotFoundError as e:
            raise StorageError("Stored document is missing.") from e
        except OSError as e:
            raise StorageReadError(
                "Stored document could not be read. Check storage and retry."
            ) from e
        try:
            return _read_and_close(source, max_bytes)
        except OSError as e:
            # A FileNotFoundError after opening is an interrupted read/close,
            # not evidence that the referenced object was absent at lookup.
            raise StorageReadError(
                "Stored document could not be read. Check storage and retry."
            ) from e

    def put_file(
        self, key: str, source: Path, content_type: str | None = None, *, sha256: str, size: int
    ) -> None:
        path = self._path(key)
        staged_path: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(
                dir=path.parent, prefix=".upload-", suffix=".tmp", delete=False
            ) as staged:
                staged_path = Path(staged.name)
                with _source_file(source) as stream:
                    identity = _copy(stream, staged)
                _verify_file(identity, sha256, size)
            staged_path.replace(path)
        except OSError as exc:
            raise StorageWriteError(
                "Stored document could not be written. Retry after recovery."
            ) from exc
        finally:
            if staged_path is not None:
                try:
                    staged_path.unlink(missing_ok=True)
                except OSError:
                    log.warning("Could not remove a private upload staging file")

    def get_file(
        self, key: str, destination: Path, *, max_bytes: int | None = None
    ) -> tuple[str, int]:
        _validate_read_limit(max_bytes)
        try:
            stream = self._path(key).open("rb")
        except FileNotFoundError as exc:
            raise StorageError("Stored document is missing.") from exc
        except OSError as exc:
            raise StorageReadError(
                "Stored document could not be read. Check storage and retry."
            ) from exc
        try:
            return _copy_and_close(stream, destination, max_bytes)
        except OSError as exc:
            raise StorageReadError(
                "Stored document could not be read. Check storage and retry."
            ) from exc

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def digest(self, key: str) -> str:
        try:
            source = self._path(key).open("rb")
        except FileNotFoundError as e:
            raise StorageError("Stored document is missing.") from e
        except OSError as e:
            raise StorageReadError(
                "Stored document could not be read. Check storage and retry."
            ) from e
        try:
            return _digest_and_close(source)
        except OSError as e:
            # Once opened, a disappearing file or failed close is a read error.
            raise StorageReadError(
                "Stored document could not be read. Check storage and retry."
            ) from e

    def exists(self, key: str) -> bool:
        return self._path(key).exists()


class S3Storage:
    """S3-compatible object storage (AWS S3, MinIO). boto3 imported lazily so
    dev/test installs without it still work."""

    def __init__(
        self,
        bucket: str,
        *,
        endpoint_url: str | None = None,
        region: str | None = None,
        prefix: str = "",
    ) -> None:
        try:
            import boto3  # noqa: PLC0415 — lazy: only needed for the s3 backend
        except ImportError as e:  # pragma: no cover
            raise StorageError("boto3 is required for the s3 storage backend") from e
        self._bucket = bucket
        self._prefix = prefix.strip("/")
        # Credentials come from the standard AWS env/instance chain.
        self._client = boto3.client("s3", endpoint_url=endpoint_url, region_name=region)

    def _k(self, key: str) -> str:
        return f"{self._prefix}/{key}" if self._prefix else key

    def put(self, key: str, data: bytes, content_type: str | None = None) -> None:
        extra = {"ContentType": content_type} if content_type else {}
        self._client.put_object(Bucket=self._bucket, Key=self._k(key), Body=data, **extra)

    def put_file(
        self, key: str, source: Path, content_type: str | None = None, *, sha256: str, size: int
    ) -> None:
        upload_id = None
        target = {"Bucket": self._bucket, "Key": self._k(key)}
        extra = {"ContentType": content_type} if content_type else {}
        try:
            with _source_file(source) as stream:
                digest, count = hashlib.sha256(), 0
                parts: list[dict[str, object]] = []
                while True:
                    part = bytearray()
                    while len(part) < _MULTIPART_BYTES:
                        chunk = stream.read(min(_READ_CHUNK_BYTES, _MULTIPART_BYTES - len(part)))
                        if not chunk:
                            break
                        part.extend(chunk)
                        digest.update(chunk)
                        count += len(chunk)
                    if not part:
                        break
                    if upload_id is None:
                        upload_id = self._client.create_multipart_upload(**target, **extra)[
                            "UploadId"
                        ]
                    number = len(parts) + 1
                    response = self._client.upload_part(
                        **target, UploadId=upload_id, PartNumber=number, Body=bytes(part)
                    )
                    parts.append({"PartNumber": number, "ETag": response["ETag"]})
                _verify_file((digest.hexdigest(), count), sha256, size)
                if upload_id is None:
                    self._client.put_object(**target, Body=b"", **extra)
                else:
                    self._client.complete_multipart_upload(
                        **target, UploadId=upload_id, MultipartUpload={"Parts": parts}
                    )
        except BaseException as exc:
            if upload_id is not None:
                try:
                    self._client.abort_multipart_upload(**target, UploadId=upload_id)
                except Exception:
                    # Keep the primary interruption. Provider qualification also
                    # inspects unfinished uploads; no shared object is deleted.
                    log.warning("Could not abort an incomplete document multipart upload")
            if isinstance(exc, (StorageWriteError, KeyboardInterrupt, SystemExit)):
                raise
            if not isinstance(exc, Exception):
                raise
            raise StorageWriteError(
                "Stored document could not be written. Retry after recovery."
            ) from exc

    def get_file(
        self, key: str, destination: Path, *, max_bytes: int | None = None
    ) -> tuple[str, int]:
        _validate_read_limit(max_bytes)
        from botocore.exceptions import ClientError

        try:
            response = self._client.get_object(Bucket=self._bucket, Key=self._k(key))
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "404", "NotFound"}:
                raise StorageError("Stored document is missing.") from exc
            raise StorageReadError(
                "Stored document could not be read. Check storage and retry."
            ) from exc
        except Exception as exc:
            raise StorageReadError(
                "Stored document could not be read. Check storage and retry."
            ) from exc
        try:
            return _copy_and_close(response["Body"], destination, max_bytes)
        except StorageReadTooLarge:
            raise
        except Exception as exc:
            raise StorageReadError(
                "Stored document could not be read. Check storage and retry."
            ) from exc

    def get(self, key: str, *, max_bytes: int | None = None) -> bytes:
        _validate_read_limit(max_bytes)
        from botocore.exceptions import ClientError  # noqa: PLC0415 — optional S3 dependency

        try:
            resp = self._client.get_object(Bucket=self._bucket, Key=self._k(key))
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in {"NoSuchKey", "404", "NotFound"}:
                raise StorageError("Stored document is missing.") from e
            raise StorageReadError(
                "Stored document could not be read. Check storage and retry."
            ) from e
        except Exception as e:  # noqa: BLE001 — sanitize provider diagnostics
            raise StorageReadError(
                "Stored document could not be read. Check storage and retry."
            ) from e
        try:
            return _read_and_close(resp["Body"], max_bytes)
        except StorageReadTooLarge:
            raise
        except Exception as e:  # noqa: BLE001 — failed or malformed reads cannot return bytes
            raise StorageReadError(
                "Stored document could not be read. Check storage and retry."
            ) from e

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self._bucket, Key=self._k(key))

    def digest(self, key: str) -> str:
        # Lookup failures and failures after opening a response are separate:
        # interrupted/truncated reads must never become a partial healthy hash.
        from botocore.exceptions import ClientError  # noqa: PLC0415 — optional S3 dependency

        try:
            resp = self._client.get_object(Bucket=self._bucket, Key=self._k(key))
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in {"NoSuchKey", "404", "NotFound"}:
                raise StorageError("Stored document is missing.") from e
            raise StorageReadError(
                "Stored document could not be read. Check storage and retry."
            ) from e
        except Exception as e:  # noqa: BLE001 — sanitize provider diagnostics
            raise StorageReadError(
                "Stored document could not be read. Check storage and retry."
            ) from e
        try:
            return _digest_and_close(resp["Body"])
        except Exception as e:  # noqa: BLE001 — close/read errors invalidate the entire hash
            raise StorageReadError(
                "Stored document could not be read. Check storage and retry."
            ) from e

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=self._k(key))
            return True
        except Exception:  # noqa: BLE001
            return False


# --- process-wide singleton (swappable in tests) -------------------------------

_storage: Storage | None = None


def _build_from_settings() -> Storage:
    backend = (settings.storage_backend or "local").lower()
    if backend == "s3":
        return S3Storage(
            settings.storage_s3_bucket,
            endpoint_url=settings.storage_s3_endpoint_url or None,
            region=settings.storage_s3_region or None,
            prefix=settings.storage_s3_prefix or "",
        )
    if backend == "memory":
        return MemoryStorage()
    return LocalStorage(settings.storage_local_path)


def get_storage() -> Storage:
    global _storage
    if _storage is None:
        _storage = _build_from_settings()
    return _storage


def set_storage(storage: Storage) -> None:
    """Override the backend (tests)."""
    global _storage
    _storage = storage


def reset_storage() -> None:
    """Clear the cached backend so the next `get_storage` rebuilds from settings."""
    global _storage
    _storage = None
