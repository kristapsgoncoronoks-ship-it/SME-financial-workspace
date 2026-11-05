"""Prepare verified pure-Python test wheels, then smoke an installer-free image.

Preparation uses ordinary verified HTTPS on the host. Runtime execution disables
networking and installs only new test distributions into a disposable venv.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import uuid
import zipfile
from email.parser import BytesParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEST_ONLY = {"pytest", "pytest-asyncio", "iniconfig", "pluggy", "pygments"}
SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")
SMOKE_TMPFS = "/tmp:uid=10001,gid=10001,mode=0700,size=1g,exec"


def image_id(reference: str) -> str:
    value = subprocess.check_output(
        ["docker", "image", "inspect", "--format", "{{.Id}}", reference], text=True
    ).strip()
    if not SHA.fullmatch(value):
        raise ValueError("Invalid inspected image identity")
    return value


def verify_wheelhouse(folder: Path) -> dict:
    manifest = json.loads((folder / "manifest.json").read_text())
    expected = set()
    for wheel in manifest["wheels"]:
        filename = wheel["filename"]
        if Path(filename).name != filename or not filename.endswith("-none-any.whl"):
            raise ValueError("Only verified pure-Python wheels are supported")
        expected.add(filename)
        if (
            hashlib.sha256((folder / filename).read_bytes()).hexdigest()
            != wheel["sha256"]
        ):
            raise ValueError("Smoke wheel integrity check failed")
    if {path.name for path in folder.glob("*.whl")} != expected:
        raise ValueError("Unexpected smoke wheel")
    if (
        hashlib.sha256((folder / "test-tools.lock").read_bytes()).hexdigest()
        != manifest["lock_sha256"]
    ):
        raise ValueError("Smoke lock integrity check failed")
    return manifest


def prepare(image: str, folder: Path) -> None:
    identity = image_id(image)
    probe = (
        "import json,runpy; "
        "m=runpy.run_path('/app/scripts/runtime_smoke.py',run_name='smoke_inventory'); "
        "print(json.dumps(m['distributions']()))"
    )
    installed = json.loads(
        subprocess.check_output(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--entrypoint",
                "python",
                identity,
                "-c",
                probe,
            ],
            text=True,
        )
    )
    if TEST_ONLY & installed.keys():
        raise ValueError("Prepare from the rebuilt production image without test tools")
    folder.mkdir(parents=True, exist_ok=True)
    if any(folder.iterdir()):
        raise ValueError("Prepare requires a new empty wheelhouse directory")
    folder.chmod(0o755)
    with tempfile.TemporaryDirectory(prefix="backend-smoke-resolution-") as temporary:
        constraints = Path(temporary) / "runtime-constraints.txt"
        constraints.write_text(
            "".join(
                f"{name}=={item['version']}\n"
                for name, item in sorted(installed.items())
            )
        )
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "download",
                "--disable-pip-version-check",
                "--no-cache-dir",
                "--only-binary=:all:",
                "--platform",
                "any",
                "--python-version",
                "3.14",
                "--dest",
                str(folder),
                "-c",
                str(constraints),
                "-r",
                str(ROOT / "backend/requirements-test.txt"),
            ],
            check=True,
        )
    wheels, lock = [], []
    for path in sorted(folder.glob("*.whl")):
        if not path.name.endswith("-none-any.whl"):
            raise ValueError("Smoke tools must be pure-Python wheels")
        with zipfile.ZipFile(path) as archive:
            metadata = [
                name
                for name in archive.namelist()
                if name.endswith(".dist-info/METADATA")
            ]
            if len(metadata) != 1:
                raise ValueError("Invalid wheel metadata")
            details = BytesParser().parsebytes(archive.read(metadata[0]))
        name = re.sub(r"[-_.]+", "-", details["Name"].lower())
        version = details["Version"]
        metadata_url = f"https://pypi.org/pypi/{name}/{version}/json"
        with urllib.request.urlopen(metadata_url, timeout=30) as response:
            release = json.load(response)
        published = next(
            (item for item in release["urls"] if item["filename"] == path.name), None
        )
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if published is None or published["digests"]["sha256"] != digest:
            raise ValueError(
                "Downloaded wheel does not match the published PyPI SHA256"
            )
        wheels.append(
            {
                "filename": path.name,
                "name": name,
                "version": version,
                "sha256": digest,
                "metadata_url": metadata_url,
            }
        )
        if name not in installed:
            lock.append(f"{name}=={version} --hash=sha256:{digest}\n")
        path.chmod(0o644)
    (folder / "test-tools.lock").write_text("".join(lock))
    (folder / "test-tools.lock").chmod(0o644)
    manifest = {
        "runtime_image_id": identity,
        "runtime_distributions": installed,
        "wheels": wheels,
        "test_manifest_sha256": hashlib.sha256(
            (ROOT / "backend/requirements-test.txt").read_bytes()
        ).hexdigest(),
        "lock_sha256": hashlib.sha256(
            (folder / "test-tools.lock").read_bytes()
        ).hexdigest(),
    }
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (folder / "manifest.json").chmod(0o644)
    verify_wheelhouse(folder)


def run(
    image: str,
    builder: str,
    folder: Path,
    pytest_args: list[str],
    *,
    network: str = "none",
    env_file: Path | None = None,
) -> int:
    manifest = verify_wheelhouse(folder)
    identity, builder_id = image_id(image), image_id(builder)
    if identity != manifest["runtime_image_id"]:
        raise ValueError("Wheelhouse belongs to a different runtime image")
    with tempfile.TemporaryDirectory(prefix="backend-smoke-uv-") as temporary:
        tests = Path(temporary) / "tests"
        shutil.copytree(
            ROOT / "backend/tests",
            tests,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
        )
        staged_hashes = {}
        for path in sorted(tests.rglob("*")):
            path.chmod(0o755 if path.is_dir() else 0o644)
            if path.is_file():
                relative = path.relative_to(tests)
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                if (
                    digest
                    != hashlib.sha256(
                        (ROOT / "backend/tests" / relative).read_bytes()
                    ).hexdigest()
                ):
                    raise ValueError("Test source changed during smoke staging")
                staged_hashes[str(relative)] = digest
        tests.chmod(0o755)
        name = "bidit-smoke-installer-" + uuid.uuid4().hex
        created = False
        try:
            subprocess.run(
                [
                    "docker",
                    "create",
                    "--name",
                    name,
                    "--entrypoint",
                    "/bin/true",
                    builder_id,
                ],
                check=True,
                stdout=subprocess.DEVNULL,
            )
            created = True
            uv = Path(temporary) / "uv"
            subprocess.run(
                ["docker", "cp", f"{name}:/usr/local/bin/uv", str(uv)], check=True
            )
            uv.chmod(0o755)
            print(
                json.dumps(
                    {
                        "runtime_image_id": identity,
                        "builder_image_id": builder_id,
                        "uv_sha256": hashlib.sha256(uv.read_bytes()).hexdigest(),
                        "private_scratch_tmpfs": SMOKE_TMPFS,
                        "test_file_count": len(staged_hashes),
                        "test_tree_sha256": hashlib.sha256(
                            json.dumps(staged_hashes, sort_keys=True).encode()
                        ).hexdigest(),
                    }
                ),
                flush=True,
            )
            command = [
                "docker",
                "run",
                "--rm",
                "--tmpfs",
                SMOKE_TMPFS,
                "--network",
                network,
                "-v",
                f"{uv}:/tmp/smoke-uv:ro",
                "-v",
                f"{folder.resolve()}:/wheelhouse:ro",
                "-v",
                f"{tests}:/app/tests:ro",
                "--entrypoint",
                "python",
                identity,
                "/app/scripts/runtime_smoke.py",
                *pytest_args,
            ]
            if env_file is not None:
                command[2:2] = ["--env-file", str(env_file.resolve())]
            result = subprocess.run(command, check=False)
            return result.returncode
        finally:
            if created:
                subprocess.run(
                    ["docker", "rm", "-f", name], check=True, stdout=subprocess.DEVNULL
                )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run"))
    parser.add_argument("--image", default="bidit-ci-backend:review")
    parser.add_argument("--builder-image", default="bidit-ci-backend-build:review")
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument(
        "--network",
        default="none",
        help="Use an explicit isolated DB network for native PostgreSQL tests",
    )
    parser.add_argument(
        "--env-file", type=Path, help="Private synthetic/disposable test configuration"
    )
    args, remaining = parser.parse_known_args()
    if remaining[:1] == ["--"]:
        remaining = remaining[1:]
    if args.command == "prepare":
        if remaining:
            parser.error("prepare does not accept pytest arguments")
        prepare(args.image, args.wheelhouse)
    else:
        raise SystemExit(
            run(
                args.image,
                args.builder_image,
                args.wheelhouse,
                remaining or ["tests/test_fx.py", "tests/test_jobs.py", "-q"],
                network=args.network,
                env_file=args.env_file,
            )
        )
