#!/usr/bin/env python3
"""Smoke-test already loaded backend/S3 images using owned synthetic resources.

Usage: python scripts/check_s3_storage.py BACKEND_IMAGE
Requires Docker and the repository-pinned PyYAML. Never pulls an image, accepts
an existing volume, changes customer data, or enables the candidate in production.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import yaml

PROBE = r"""
from contextlib import closing
import hashlib
import importlib.metadata
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path
import boto3
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import ClientError
from app.core.storage import S3Storage, StorageError, StorageReadError, StorageReadTooLarge, StorageWriteError, content_key

assert os.getuid() == 10001
bucket = os.environ['STORAGE_S3_BUCKET']
endpoint = os.environ['STORAGE_S3_ENDPOINT_URL']
store = S3Storage(bucket, endpoint_url=endpoint, region='us-east-1', prefix='smoke')
objects = [('tenant-a', bytes(range(256)) * 16384),
           ('tenant-b', b'synthetic binary\x00\xff'), ('tenant-c', b'')]
try:
    for tenant, data in objects:
        sha = hashlib.sha256(data).hexdigest()
        key = content_key('uploads', tenant, sha)
        if sys.argv[1] == 'write':
            store.put(key, data, content_type='application/octet-stream')
        assert store.exists(key) and store.get(key) == data and store.digest(key) == sha
        assert store.get(key, max_bytes=max(1, len(data))) == data
        with tempfile.TemporaryDirectory(prefix='s3-file-copy-') as temporary:
            copied = Path(temporary) / 'copied.bin'
            assert store.get_file(key, copied, max_bytes=max(1, len(data))) == (sha, len(data))
        if len(data) > 1:
            try:
                store.get(key, max_bytes=len(data)-1)
            except StorageReadTooLarge:
                pass
            else:
                raise AssertionError('expected size refusal')
            assert store.get(key, max_bytes=len(data)) == data
            with tempfile.TemporaryDirectory(prefix='s3-file-bound-') as temporary:
                bounded = Path(temporary) / 'bounded.bin'
                try:
                    store.get_file(key, bounded, max_bytes=len(data)-1)
                except StorageReadTooLarge:
                    assert bounded.stat().st_size < len(data)
                else:
                    raise AssertionError('expected file-copy size refusal')
    if sys.argv[1] == 'write':
        with tempfile.TemporaryDirectory(prefix='s3-file-upload-') as temporary:
            source, copied = Path(temporary) / 'source.bin', Path(temporary) / 'copy.bin'
            digest = hashlib.sha256()
            block = bytes(range(256)) * 256
            with source.open('wb') as stream:
                for _ in range(144):
                    stream.write(block)
                    digest.update(block)
            file_sha, file_size = digest.hexdigest(), source.stat().st_size
            assert file_size == 9 * 1024 * 1024
            store.put_file('file-stream', source, 'application/octet-stream', sha256=file_sha, size=file_size)
            assert store.get_file('file-stream', copied, max_bytes=file_size) == (file_sha, file_size)
            try:
                store.put_file('file-stream', source, sha256='0' * 64, size=file_size)
            except StorageWriteError:
                pass
            else:
                raise AssertionError('changed upload identity was accepted')
            assert store.get_file('file-stream', copied, max_bytes=file_size) == (file_sha, file_size)
            real_part = store._client.upload_part
            written = []
            def interrupted_part(**kwargs):
                result = real_part(**kwargs)
                written.append(kwargs['PartNumber'])
                raise RuntimeError('synthetic response lost after actual part upload')
            store._client.upload_part = interrupted_part
            try:
                try:
                    store.put_file('interrupted-file', source, sha256=file_sha, size=file_size)
                except StorageWriteError:
                    pass
                else:
                    raise AssertionError('interrupted multipart upload was accepted')
            finally:
                store._client.upload_part = real_part
            assert written == [1] and not store.exists('interrupted-file')
            assert not store._client.list_multipart_uploads(Bucket=bucket).get('Uploads')
            store.delete('file-stream')
        store.put('delete-me', b'synthetic deletion')
        store.delete('delete-me')
        assert not store.exists('delete-me')
        for method in (store.get, store.digest):
            try:
                method('delete-me')
            except StorageError as exc:
                assert not isinstance(exc, StorageReadError)
                assert str(exc) == 'Stored document is missing.'
            else:
                raise AssertionError('deleted object remains')
        store.delete('delete-me')
        bad = boto3.client('s3', endpoint_url=endpoint, region_name='us-east-1',
                           aws_access_key_id=os.environ['AWS_ACCESS_KEY_ID'],
                           aws_secret_access_key=secrets.token_urlsafe(36))
        unsigned = boto3.client('s3', endpoint_url=endpoint, region_name='us-east-1',
                                config=Config(signature_version=UNSIGNED))
        for denied in (bad, unsigned):
            with closing(denied):
                for method, kwargs in (
                    ('list_objects_v2', {'Bucket': bucket}),
                    ('get_object', {'Bucket': bucket, 'Key': 'smoke/' + content_key(
                        'uploads', objects[0][0], hashlib.sha256(objects[0][1]).hexdigest())}),
                    ('put_object', {'Bucket': bucket, 'Key': 'denied-write', 'Body': b'no'}),
                ):
                    try:
                        getattr(denied, method)(**kwargs)
                    except ClientError as exc:
                        assert exc.response['ResponseMetadata']['HTTPStatusCode'] == 403
                    else:
                        raise AssertionError('unauthorized operation accepted')
    rows = store._client.list_objects_v2(Bucket=bucket)['Contents']
    assert {r['Key'] for r in rows} == {
        'smoke/' + content_key('uploads', tenant, hashlib.sha256(data).hexdigest())
        for tenant, data in objects
    }
    print(json.dumps({'python': sys.version.split()[0], 'uid': os.getuid(),
                      'boto3': importlib.metadata.version('boto3'),
                      'botocore': importlib.metadata.version('botocore')}))
finally:
    store._client.close()
"""

READ_FAILURE_PROBE = r"""
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from botocore.exceptions import ClientError, EndpointConnectionError
from app.core.storage import S3Storage, StorageReadError, content_key

mode = sys.argv[1]
assert mode in {'denied', 'offline'} and os.getuid() == 10001
endpoint = os.environ['STORAGE_S3_ENDPOINT_URL'] if mode == 'denied' else 'http://127.0.0.1:1'
store = S3Storage(os.environ['STORAGE_S3_BUCKET'], endpoint_url=endpoint,
                  region='us-east-1', prefix='smoke')
key = content_key('uploads', 'tenant-a', hashlib.sha256(bytes(range(256)) * 16384).hexdigest())
try:
    with tempfile.TemporaryDirectory(prefix='s3-file-unavailable-') as temporary:
        for method in (store.get, store.digest, lambda key: store.get_file(key, Path(temporary) / 'copy')):
            try:
                method(key)
            except StorageReadError as exc:
                assert str(exc) == 'Stored document could not be read. Check storage and retry.'
                if mode == 'denied':
                    assert isinstance(exc.__cause__, ClientError)
                    assert exc.__cause__.response['ResponseMetadata']['HTTPStatusCode'] == 403
                else:
                    assert isinstance(exc.__cause__, EndpointConnectionError)
            else:
                raise AssertionError('unavailable object was accepted')
    print(json.dumps({'check': mode}))
finally:
    store._client.close()
"""


class SmokeFailure(Exception):
    """A fixed phase name, never raw Docker/provider diagnostics."""


def main(image: str) -> int:
    prefix = "bidit-s3-smoke-" + uuid.uuid4().hex
    containers: set[str] = set()
    volumes: list[str] = []
    network_created = False
    result: dict = {"status": "failed", "checks": [], "images": {}}

    def docker(args: list[str], *, source: str | None = None, timeout: int = 60):
        return subprocess.run(
            ["docker", *args],
            input=source,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def checked(args: list[str], phase: str) -> str:
        response = docker(args)
        if response.returncode:
            raise SmokeFailure(phase)
        return response.stdout.strip()

    def require(condition: bool, phase: str) -> None:
        if not condition:
            raise SmokeFailure(phase)

    def immutable(reference: str) -> str:
        value = checked(["image", "inspect", reference, "--format", "{{.Id}}"], "image-inspection")
        require(re.fullmatch(r"sha256:[0-9a-f]{64}", value) is not None, "image-identity")
        return value

    def passed(name: str) -> None:
        result["checks"].append(name)

    try:
        require(bool(image) and not image.startswith("-"), "backend-image-argument")
        compose = yaml.safe_load(
            (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text()
        )
        reference = compose["services"]["minio"]["image"]
        require(
            isinstance(reference, str)
            and re.fullmatch(r"rustfs/rustfs:[\w.-]+@sha256:[0-9a-f]{64}", reference) is not None,
            "compose-server-digest",
        )
        backend, server = immutable(image), immutable(reference)
        result["images"] = {"backend": backend, "server": server, "server_reference": reference}
        with tempfile.TemporaryDirectory(prefix=prefix) as temporary:
            root = Path(temporary)
            access, secret = secrets.token_hex(12), secrets.token_urlsafe(36)

            def environment(name: str, values: dict[str, str]) -> str:
                path = root / name
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "w") as stream:
                    stream.write("".join(f"{k}={v}\n" for k, v in values.items()))
                return str(path)

            server_env = environment(
                "server.env",
                {
                    "RUSTFS_ACCESS_KEY": access,
                    "RUSTFS_SECRET_KEY": secret,
                    "RUSTFS_VOLUMES": "/data",
                    "RUSTFS_ADDRESS": "0.0.0.0:9000",
                    "RUSTFS_CONSOLE_ENABLE": "false",
                    "RUSTFS_OBS_LOGGER_LEVEL": "warn",
                },
            )
            values = {
                "ENVIRONMENT": "development",
                "AWS_ACCESS_KEY_ID": access,
                "AWS_SECRET_ACCESS_KEY": secret,
                "AWS_EC2_METADATA_DISABLED": "true",
                "AWS_MAX_ATTEMPTS": "1",
                "STORAGE_S3_ENDPOINT_URL": "http://store:9000",
                "STORAGE_S3_BUCKET": "qualification-documents",
                "STORAGE_S3_REGION": "us-east-1",
            }
            client_env = environment("client.env", values)
            wrong_env = environment(
                "wrong.env", {**values, "AWS_SECRET_ACCESS_KEY": secrets.token_urlsafe(36)}
            )

            def client(
                args: list[str],
                *,
                env: str = client_env,
                isolated: bool = False,
                production: bool = False,
                mount: Path | None = None,
                source: str | None = None,
            ):
                name = prefix + "-" + uuid.uuid4().hex[:8]
                containers.add(name)
                options = [
                    "run",
                    "--rm",
                    "--name",
                    name,
                    "--interactive",
                    "--network",
                    "none" if isolated else prefix,
                    "--read-only",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges:true",
                    "--cpus",
                    "1",
                    "--memory",
                    "384m",
                    "--tmpfs",
                    "/tmp:rw,nosuid,nodev,size=32m,uid=10001,gid=10001",
                    "--env-file",
                    env,
                ]
                if production:
                    options += ["--env", "ENVIRONMENT=production"]
                if mount is not None:
                    options += ["--mount", f"type=bind,src={mount},dst=/legacy-minio,readonly"]
                response = docker([*options, backend, "python", *args], source=source)
                if response.returncode in (0, 1):
                    containers.remove(name)  # --rm completed; interrupted runs remain owned.
                return response

            for action in ("initialize", "check-legacy"):
                response = client(
                    ["-m", "app.storage_bootstrap", action], isolated=True, production=True
                )
                require(
                    response.returncode == 1
                    and response.stdout == ""
                    and "development-only" in response.stderr
                    and "Traceback" not in response.stderr,
                    "production-refusal",
                )
            passed("both-production-actions-refuse-without-network")
            for hidden in (False, True):
                directory = root / ("hidden" if hidden else "empty")
                directory.mkdir(mode=0o755)
                directory.chmod(0o755)
                if hidden:
                    (directory / ".minio.sys").write_bytes(b"synthetic legacy state")
                response = client(
                    ["-m", "app.storage_bootstrap", "check-legacy"], isolated=True, mount=directory
                )
                require(
                    response.returncode == int(hidden)
                    and "Traceback" not in response.stderr
                    and (
                        response.stdout == "" and "replacement is refused" in response.stderr
                        if hidden
                        else response.stdout == "Legacy store is empty.\n"
                    ),
                    "legacy-readonly-guard",
                )
                if hidden:
                    require(
                        (directory / ".minio.sys").read_bytes() == b"synthetic legacy state",
                        "legacy-preservation",
                    )
            passed("empty-and-hidden-readonly-legacy-guard")
            # Register intended UUID resources before Docker can create them:
            # a client timeout does not prove the daemon rejected creation.
            network_created = True
            checked(["network", "create", prefix], "network-creation")
            volume = prefix + "-data"
            volumes.append(volume)
            checked(["volume", "create", volume], "volume-creation")

            def start() -> str:
                name = prefix + "-" + uuid.uuid4().hex[:8]
                containers.add(name)
                checked(
                    [
                        "run",
                        "-d",
                        "--name",
                        name,
                        "--network",
                        prefix,
                        "--network-alias",
                        "store",
                        "--user",
                        "10001:10001",
                        "--read-only",
                        "--cap-drop",
                        "ALL",
                        "--security-opt",
                        "no-new-privileges:true",
                        "--cpus",
                        "1",
                        "--memory",
                        "512m",
                        "--env-file",
                        server_env,
                        "--tmpfs",
                        "/tmp:rw,nosuid,nodev,size=32m,uid=10001,gid=10001",
                        "--tmpfs",
                        "/logs:rw,nosuid,nodev,size=16m,uid=10001,gid=10001",
                        "--mount",
                        f"type=volume,src={volume},dst=/data",
                        server,
                    ],
                    "server-start",
                )
                for _ in range(40):
                    if (
                        docker(
                            ["exec", name, "curl", "-fsS", "http://127.0.0.1:9000/health"],
                            timeout=5,
                        ).returncode
                        == 0
                    ):
                        return name
                    time.sleep(1)
                raise SmokeFailure("server-readiness")

            def initialize(env: str = client_env, refused: bool = False) -> None:
                response = client(["-m", "app.storage_bootstrap", "initialize"], env=env)
                require(
                    response.returncode == int(refused)
                    and "Traceback" not in response.stderr
                    and (
                        response.stdout == "" and "could not be verified" in response.stderr
                        if refused
                        else response.stdout == "S3 bucket ready.\n" and response.stderr == ""
                    ),
                    "bucket-bootstrap",
                )

            def probe(stage: str) -> dict:
                response = client(["-", stage], source=PROBE)
                require(response.returncode == 0 and response.stderr == "", "shipped-sdk-contract")
                metadata = json.loads(response.stdout)
                require(
                    set(metadata) == {"uid", "python", "boto3", "botocore"}
                    and metadata.get("uid") == 10001
                    and all(
                        re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", metadata[k])
                        for k in ("python", "boto3", "botocore")
                    ),
                    "shipped-runtime-identity",
                )
                return metadata

            first = start()
            initialize()
            initialize()
            initialize(wrong_env, refused=True)
            passed("cold-repeated-and-wrong-auth-bootstrap")
            result["runtime"] = probe("write")
            passed("shipped-file-copy-multipart-identity-and-abort")
            for mode, env in (("denied", wrong_env), ("offline", client_env)):
                response = client(["-", mode], env=env, source=READ_FAILURE_PROBE)
                require(
                    response.returncode == 0
                    and response.stderr == ""
                    and response.stdout == json.dumps({"check": mode}) + "\n",
                    "shipped-read-error-classification",
                )
            require(probe("read") == result["runtime"], "read-error-recovery")
            passed("shipped-read-errors-distinct-from-missing-and-recover")
            initialize()
            require(probe("read") == result["runtime"], "bootstrap-preserves-objects")
            passed("shipped-sdk-bytes-digest-limits-recovery-delete-and-auth")
            checked(["stop", "--time", "10", first], "server-stop")
            checked(["rm", first], "server-removal")
            containers.remove(first)
            start()
            require(probe("read") == result["runtime"], "restart-persistence")
            passed("new-server-container-same-volume-preserves-exact-objects")
        result["status"] = "passed"
    except BaseException as exc:
        result["error"] = str(exc) if isinstance(exc, SmokeFailure) else type(exc).__name__
    finally:
        cleanup = True
        for name in containers:
            try:
                cleanup &= docker(["rm", "-f", name], timeout=30).returncode == 0
            except Exception:
                cleanup = False
        for volume in reversed(volumes):
            try:
                cleanup &= docker(["volume", "rm", volume], timeout=30).returncode == 0
            except Exception:
                cleanup = False
        if network_created:
            try:
                cleanup &= docker(["network", "rm", prefix], timeout=30).returncode == 0
            except Exception:
                cleanup = False
        result["cleanup"] = cleanup
        if not cleanup:
            result["status"] = "failed"
        sys.stdout.write(json.dumps(result) + "\n")
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.stdout.write(
            json.dumps({"status": "failed", "error": "usage: check_s3_storage.py BACKEND_IMAGE"})
            + "\n"
        )
        sys.exit(1)
    sys.exit(main(sys.argv[1]))
