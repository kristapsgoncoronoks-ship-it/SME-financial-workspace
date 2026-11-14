#!/usr/bin/env python3
"""Use the Docker security rebuild for host CI without changing the npm package.

Build the existing pinned Docker stage, validate its identity and notices before
execution, then replace only the native executable selected by npm's launcher.
The retained provenance is not a signature or a separate vulnerability scan.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

VERSION = "7.0.2"
GO_VERSION = "go1.27.4"
TEXT_VERSION = "v0.41.0"
SOURCE = "2bd066d87f5bafd315be9f40889d0a60b9e58e0b"
ARCHIVE_SHA256 = "5ccb47dbb3f68cd0da58b71e6f445eee36a50bd3e9f9f330cc23a97b88500119"
ROOT = Path(__file__).resolve().parents[1]


def run(*args: str, cwd: Path | None = None, capture: bool = False) -> str:
    result = subprocess.run(
        args,
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
    )
    return result.stdout.strip() if capture else ""


def validate_output(output: Path) -> str:
    """Validate bytes and pinned build provenance before executing any compiler."""
    for path in output.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError("Compiler output contains an unsupported file type")
    binary = output / "tsc"
    provenance = output / "provenance"
    expected_source = (
        f"source: https://github.com/microsoft/typescript-go/tree/{SOURCE}\n"
        f"archive-sha256: {ARCHIVE_SHA256}\n"
    )
    if (provenance / "source.txt").read_text() != expected_source:
        raise ValueError("Compiler source identity does not match the qualified release")
    manifest = (provenance / "compiler.sha256").read_text()
    match = re.fullmatch(r"([0-9a-f]{64})  /out/tsc\n", manifest)
    actual = hashlib.sha256(binary.read_bytes()).hexdigest()
    if not match or match[1] != actual:
        raise ValueError("Compiler checksum does not match its build manifest")
    build = (provenance / "compiler-build.txt").read_text()
    if not build.startswith(f"/out/tsc: {GO_VERSION}\n"):
        raise ValueError("Compiler was built with an unexpected Go toolchain")
    required = (
        "\tpath\tgithub.com/microsoft/typescript-go/cmd/tsgo\n",
        "\tbuild\tCGO_ENABLED=0\n",
        "\tbuild\tGOOS=linux\n",
    )
    if not all(value in build for value in required):
        raise ValueError("Compiler build identity is incomplete")
    modules = dict(re.findall(r"^\tdep\t([^\t\n]+)\t([^\t\n]+)", build, re.MULTILINE))
    if modules.get("golang.org/x/text") != TEXT_VERSION:
        raise ValueError("Compiler Unicode dependency does not match the security rebuild")
    notices = [provenance / name for name in ("LICENSE", "NOTICE.txt", "go.mod", "go.sum")]
    notices.append(provenance / "licenses/go/LICENSE")
    for module in modules:
        if module.startswith("/") or ".." in module.split("/"):
            raise ValueError("Invalid linked module path")
        directory = provenance / "licenses/modules" / module
        candidates = list(directory.rglob("*")) if directory.is_dir() else []
        if not any(path.is_file() and path.stat().st_size for path in candidates):
            raise ValueError("A linked compiler module has no license notices")
    if not all(path.is_file() and path.stat().st_size for path in notices):
        raise ValueError("Compiler source and Go license notices must be retained")
    return actual


def install(output: Path, frontend: Path) -> dict[str, str]:
    digest = validate_output(output)
    package = json.loads((frontend / "node_modules/typescript/package.json").read_text())
    if package.get("version") != VERSION:
        raise ValueError("npm TypeScript version differs from the qualified rebuild")
    target = Path(
        run(
            "node",
            "--input-type=module",
            "-e",
            'import getExePath from "./node_modules/typescript/lib/getExePath.js"; '
            "console.log(getExePath());",
            cwd=frontend,
            capture=True,
        )
    )
    node_modules = (frontend / "node_modules").resolve()
    if (
        target.is_symlink()
        or not target.is_file()
        or not target.resolve().is_relative_to(node_modules)
    ):
        raise ValueError("npm compiler target must be an existing regular file in node_modules")
    if not (target.parent / "lib.d.ts").is_file():
        raise ValueError("npm's adjacent TypeScript standard library is missing")
    binary = output / "tsc"
    retained = node_modules / ".bidit-typescript-provenance"
    if retained.is_symlink() or any(path.is_symlink() for path in retained.rglob("*")):
        raise ValueError("Retained compiler provenance must not contain symlinks")
    # Rename on the destination filesystem so an interruption cannot install
    # a partial executable. npm's launcher and standard libraries are untouched.
    fd, filename = tempfile.mkstemp(prefix=".bidit-tsc-", dir=target.parent)
    temporary = Path(filename)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(binary.read_bytes())
        temporary.chmod(0o755)
        if hashlib.sha256(temporary.read_bytes()).hexdigest() != digest:
            raise ValueError("Staged compiler checksum changed")
        # The noembed build resolves its standard libraries relative to its
        # executable. Verify the staged candidate beside npm's existing libs.
        if run(str(temporary), "--version", capture=True) != f"Version {VERSION}":
            raise ValueError("Rebuilt compiler reports an unexpected version")
        if retained.exists():
            # Go module notices can be read-only. An identical rerun needs no
            # writes; unexpected/stale provenance requires a clean npm install.
            def contents(directory: Path) -> dict[str, bytes]:
                return {
                    str(path.relative_to(directory)): path.read_bytes()
                    for path in directory.rglob("*")
                    if path.is_file()
                }

            if contents(retained) != contents(output / "provenance"):
                raise ValueError(
                    "Retained compiler provenance differs; run npm ci before installing"
                )
        else:
            shutil.copytree(output / "provenance", retained)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
        raise ValueError("Installed compiler checksum changed")
    if (
        run(str(frontend / "node_modules/.bin/tsc"), "--version", capture=True)
        != f"Version {VERSION}"
    ):
        raise ValueError("npm launcher does not execute the qualified compiler version")
    return {"version": VERSION, "go": GO_VERSION, "sha256": digest, "source": SOURCE}


def build_and_install(
    root: Path, build_ca: Path | None = None, build_network: str = "default"
) -> dict[str, str]:
    frontend = root / "frontend"
    with tempfile.TemporaryDirectory(prefix="bidit-ci-typescript-") as directory:
        temporary = Path(directory)
        iidfile = temporary / "image-id"
        command = [
            "docker",
            "build",
            "--target",
            "typescript-compiler",
            "--network",
            build_network,
            "--iidfile",
            str(iidfile),
            "-f",
            str(frontend / "Dockerfile"),
        ]
        if build_ca is not None:
            if not build_ca.is_file():
                raise ValueError("The optional build CA must be an existing file")
            command.extend(("--secret", f"id=build_ca,src={build_ca.resolve()}"))
        # Docker's predefined proxy args are excluded from image history. Pass
        # names only so configured cloud proxies work without logging values.
        for proxy in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"):
            if os.environ.get(proxy):
                command.extend(("--build-arg", proxy))
        run(*command, str(frontend))
        image_id = iidfile.read_text().strip()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            raise ValueError("Docker did not report an immutable compiler image ID")
        name = "bidit-ci-tsc-" + uuid.uuid4().hex
        run(
            "docker",
            "create",
            "--name",
            name,
            "--network",
            "none",
            image_id,
            capture=True,
        )
        try:
            run("docker", "cp", name + ":/out", str(temporary / "out"))
        except BaseException as primary:
            try:
                run("docker", "rm", "--force", name, capture=True)
            except (OSError, subprocess.CalledProcessError):
                primary.add_note("Owned compiler container cleanup also failed")
            raise
        else:
            run("docker", "rm", "--force", name, capture=True)
        result = install(temporary / "out", frontend)
        result["image_id"] = image_id
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-ca", type=Path)
    parser.add_argument("--build-network", choices=("default", "host"), default="default")
    arguments = parser.parse_args()
    sys.stdout.write(
        json.dumps(
            build_and_install(ROOT, arguments.build_ca, arguments.build_network),
            sort_keys=True,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
