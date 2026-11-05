#!/usr/bin/env python3
"""Resolved Python + locked npm dependency delta for PR review (no hosted API).

Head vulnerability enforcement stays in pip-audit/npm audit. The base may have
findings; only a valid scanner JSON result with findings explains its exit 1.
Service/configuration failures are never treated as a clean graph. No license
allow/deny policy was configured in the former action; npm declarations are
reported as metadata, not legal certification. Python license review is manual.
"""

from __future__ import annotations

import argparse
import json
import posixpath
import re
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENT_ROOTS = ("backend/requirements.txt", "backend/requirements-dev.txt")
MAX_REQUIREMENT_FILES = 32
MAX_REQUIREMENT_DEPTH = 8
MAX_REQUIREMENT_FILE_BYTES = 64 * 1024
MAX_REQUIREMENT_TOTAL_BYTES = 256 * 1024


def requirement_closure(read: Callable[[str], bytes]) -> dict[str, bytes]:
    """Read the bounded local .txt graph pip will resolve, preserving its layout."""
    files: dict[str, bytes] = {}
    total = 0

    def visit(path: str, ancestors: tuple[str, ...]) -> None:
        nonlocal total
        if path in ancestors:
            raise ValueError("Cyclic requirement include")
        if path in files:
            return
        if (
            len(ancestors) >= MAX_REQUIREMENT_DEPTH
            or len(files) >= MAX_REQUIREMENT_FILES
        ):
            raise ValueError("Requirement include graph exceeds its file/depth budget")
        content = read(path)
        total += len(content)
        if (
            len(content) > MAX_REQUIREMENT_FILE_BYTES
            or total > MAX_REQUIREMENT_TOTAL_BYTES
        ):
            raise ValueError("Requirement include graph exceeds its byte budget")
        text = content.decode("utf-8")
        if "\x00" in text:
            raise ValueError("Invalid requirement manifest")
        files[path] = content
        for line in re.sub(r"\\\r?\n", "", text).splitlines():
            tokens = shlex.split(line, comments=True)
            if not tokens or not tokens[0].startswith("-"):
                continue
            option, *arguments = tokens
            if option in ("-r", "--requirement", "-c", "--constraint"):
                if len(arguments) != 1:
                    raise ValueError("Invalid requirement include")
                include = arguments[0]
            elif (
                option.startswith(("--requirement=", "--constraint=")) and not arguments
            ):
                include = option.split("=", 1)[1]
            elif option.startswith(("-r", "-c")) and not arguments:
                include = option[2:]
            else:
                raise ValueError("Unsupported requirement manifest option")
            if not re.fullmatch(
                r"[A-Za-z0-9_./ -]+\.txt", include
            ) or include.startswith("/"):
                raise ValueError("Requirement includes must be local .txt files")
            target = posixpath.normpath(
                posixpath.join(posixpath.dirname(path), include)
            )
            if not target.startswith("backend/"):
                raise ValueError("Requirement include escapes backend")
            visit(target, (*ancestors, path))

    for root in REQUIREMENT_ROOTS:
        visit(root, ())
    return files


def python_graph(report: dict) -> dict:
    dependencies = report.get("dependencies")
    if not isinstance(dependencies, list) or not dependencies:
        raise ValueError("Audit did not return a resolved dependency graph")
    graph = {}
    for dependency in dependencies:
        if not all(key in dependency for key in ("name", "version", "vulns")):
            raise ValueError("Unresolved/skipped dependency in audit graph")
        name = re.sub(r"[-_.]+", "-", dependency["name"].lower())
        graph[name] = {
            "version": dependency["version"],
            "advisories": sorted(v["id"] for v in dependency["vulns"]),
        }
    return graph


def delta(base: dict, head: dict) -> list:
    return [
        {"dependency": key, "base": base.get(key), "head": head.get(key)}
        for key in sorted(base.keys() | head.keys())
        if base.get(key) != head.get(key)
    ]


def npm_graph(lock: dict) -> dict:
    if not isinstance(lock.get("packages"), dict) or lock.get("lockfileVersion") != 3:
        raise ValueError("Expected a complete npm v3 lock graph")
    return {
        key: {
            "version": value["version"],
            "dev": value.get("dev", False),
            "license": value.get("license", "UNDECLARED"),
        }
        for key, value in lock["packages"].items()
        if key and "version" in value
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head-audit", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.base):
        parser.error("base must be a full commit SHA")

    def base_file(path: str) -> bytes:
        return subprocess.check_output(["git", "show", f"{args.base}:{path}"], cwd=ROOT)

    def base_requirement(path: str) -> bytes:
        entry = subprocess.check_output(
            ["git", "ls-tree", args.base, "--", path], cwd=ROOT, text=True
        )
        metadata = entry.split("\t", 1)[0].split()
        if (
            len(metadata) != 3
            or metadata[0] not in ("100644", "100755")
            or metadata[1] != "blob"
        ):
            raise ValueError("Missing or non-regular base requirement file")
        size = int(
            subprocess.check_output(["git", "cat-file", "-s", metadata[2]], cwd=ROOT)
        )
        if size > MAX_REQUIREMENT_FILE_BYTES:
            raise ValueError("Base requirement file exceeds its byte budget")
        return base_file(path)

    with tempfile.TemporaryDirectory(prefix="dependency-base-") as folder:
        for name, content in requirement_closure(base_requirement).items():
            path = Path(folder) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        manifests = [
            value
            for name in REQUIREMENT_ROOTS
            for value in ("-r", str(Path(folder) / name))
        ]
        audit = subprocess.run(
            [sys.executable, "-m", "pip_audit", *manifests, "--format", "json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if audit.returncode not in (0, 1):
            raise RuntimeError(
                "Base graph audit failed; dependency review is unavailable"
            )
        base = python_graph(json.loads(audit.stdout))
        if audit.returncode == 1 and not any(
            value["advisories"] for value in base.values()
        ):
            raise RuntimeError("Base scanner failed without advisory evidence")
    head = python_graph(json.loads(args.head_audit.read_text()))
    base_npm = npm_graph(json.loads(base_file("frontend/package-lock.json")))
    head_npm = npm_graph(json.loads((ROOT / "frontend/package-lock.json").read_text()))
    report = {
        "base_sha": args.base,
        "head_sha": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "python_resolved_changes": delta(base, head),
        "npm_locked_changes": delta(base_npm, head_npm),
        "license_policy": "No allow/deny policy configured; npm declarations are metadata; Python licensing needs manual review.",
        "python_counts": {"base": len(base), "head": len(head)},
        "npm_counts": {"base": len(base_npm), "head": len(head_npm)},
    }
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(
        f"Dependency delta: {len(report['python_resolved_changes'])} Python / {len(report['npm_locked_changes'])} npm changes"
    )


if __name__ == "__main__":
    main()
