#!/usr/bin/env python3
"""Audit application build/final images and every Compose/Kubernetes service image.

Requires Docker and the repository's pinned PyYAML. Never starts service images.
Every scanner invocation uses the inspected immutable image ID. Missing images,
unmapped templates, unpinned services, invalid evidence and tool errors fail closed.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import yaml

SCANNER = "aquasec/trivy@sha256:2416d1f74045b57274df1a340eee6b510cacd73c66a6ccb8da092b117eb7d222"
SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")
# Each Dockerfile stage is a distinct required output. A security rebuild of a
# service is audited just like the application, including its compiler stage.
# Inventory rejects added/removed stages until their outputs are loaded in CI.
BUILD_OUTPUTS = {
    "backend": ("backend/Dockerfile", "runtime", "final"),
    "frontend": ("frontend/Dockerfile", "runtime", "final"),
    "postgres": ("deploy/postgres/Dockerfile", "runtime", "final"),
    "pgbouncer": ("deploy/pgbouncer/Dockerfile", "runtime", "final"),
    "backend-build": ("backend/Dockerfile", "build", "build"),
    "frontend-build": ("frontend/Dockerfile", "build", "build"),
    "frontend-compiler-build": ("frontend/Dockerfile", "typescript-compiler", "build"),
    "postgres-build": ("deploy/postgres/Dockerfile", "build", "build"),
    "pgbouncer-build": ("deploy/pgbouncer/Dockerfile", "build", "build"),
}
ROLES = tuple(BUILD_OUTPUTS)
PUBLISHED = tuple(role for role, (_, _, kind) in BUILD_OUTPUTS.items() if kind == "final")
LOCAL_SERVICES = {f"bidit-{role}:local": role for role in ("postgres", "pgbouncer")}
# The optional calendar lane reuses Compose's existing project-worker tag. It
# is covered by the audited backend runtime only for these reviewed definitions
# with the same backend build context; arbitrary image templates still fail.
LOCAL_WORKER_IMAGE = "${COMPOSE_PROJECT_NAME:-invoiceiq}-worker"
LOCAL_SERVICES[LOCAL_WORKER_IMAGE] = "backend"


class ComposeLoader(yaml.SafeLoader):
    pass


def compose_tag(loader, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


for tag in ("!reset", "!override"):
    ComposeLoader.add_constructor(tag, compose_tag)


def image_fields(value, location=""):
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{location}/{key}"
            if key == "image":
                if not isinstance(child, str) or not child:
                    raise ValueError(f"invalid image at {path}")
                yield path, child
            else:
                yield from image_fields(child, path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from image_fields(child, f"{location}/{index}")


def inventory(root: Path, built: dict[str, str]) -> dict:
    if set(built) != set(ROLES) or len(set(built.values())) != len(ROLES):
        raise ValueError("a distinct built image reference is required for every audited stage")
    targets = [
        {
            "id": role,
            "reference": built[role],
            "role": BUILD_OUTPUTS[role][2],
            "pull": False,
            "sources": [],
        }
        for role in ROLES
    ]
    bases, errors = [], []
    for relative in dict.fromkeys(value[0] for value in BUILD_OUTPUTS.values()):
        path = root / relative
        try:
            stages = []
            # Docker instructions may span lines. Join Docker's default escape
            # continuations before checking FROM/COPY so a wrapped external
            # source cannot disappear from the provenance or pinning checks.
            lines = re.sub(r"\\\r?\n[ \t]*", " ", path.read_text()).splitlines()
            for line in lines:
                if re.match(r"^\s*FROM\s", line, re.IGNORECASE):
                    match = re.fullmatch(
                        r"FROM (\S+)(?: AS ([\w-]+))?", line.strip(), re.IGNORECASE
                    )
                    if not match or not re.search(r"@sha256:[0-9a-f]{64}$", match[1]):
                        raise ValueError("unsupported/unpinned FROM")
                    stages.append((match[1], match[2]))
            expected = {
                stage: role for role, (file, stage, _) in BUILD_OUTPUTS.items() if file == relative
            }
            # Docker's unnamed final stage is the default runtime output. An
            # unnamed intermediate stage cannot be selected and fails closed.
            names = [
                stage or ("runtime" if i == len(stages) - 1 else None)
                for i, (_, stage) in enumerate(stages)
            ]
            if (
                set(names) != set(expected)
                or len(names) != len(expected)
                or not names
                or names[-1] != "runtime"
            ):
                raise ValueError("build stages changed; update audited build outputs")
            for (reference, stage), name in zip(stages, names, strict=True):
                bases.append(
                    {
                        "file": relative,
                        "reference": reference,
                        "stage": stage,
                        "covered_by": expected[name],
                        "instruction": "FROM",
                    }
                )
            current = -1
            for line in lines:
                if re.match(r"^\s*FROM\s", line, re.IGNORECASE):
                    current += 1
                if re.match(r"^\s*COPY\s", line, re.IGNORECASE):
                    copied = re.search(r"(?:^|\s)--from=(\S+)", line)
                    if not copied:
                        continue
                    reference = copied[1]
                    if reference in names or (
                        reference.isdecimal() and int(reference) < len(stages)
                    ):
                        continue
                    if current < 0 or not re.search(r"@sha256:[0-9a-f]{64}$", reference):
                        raise ValueError("unsupported/unpinned external COPY input")
                    bases.append(
                        {
                            "file": relative,
                            "reference": reference,
                            "stage": stages[current][1],
                            "covered_by": expected[names[current]],
                            "instruction": "COPY",
                        }
                    )
        except (OSError, ValueError) as exc:
            errors.append(f"{relative}: {exc}")
    sources = sorted(root.glob("*compose*.y*ml")) + sorted((root / "deploy/k8s").rglob("*.y*ml"))
    services = {}
    for path in sources:
        try:
            docs = yaml.load_all(path.read_text(), Loader=ComposeLoader)
            for document, doc in enumerate(docs):
                for location, reference in image_fields(doc):
                    source = {
                        "file": str(path.relative_to(root)),
                        "document": document,
                        "location": location,
                    }
                    if reference == LOCAL_WORKER_IMAGE:
                        if location not in (
                            "/services/worker/image",
                            "/services/calendar-worker/image",
                        ):
                            raise ValueError(
                                "local worker image has an unreviewed service location"
                            )
                        service = doc["services"][location.split("/")[2]]
                        if service.get("build") != "./backend":
                            raise ValueError(
                                "local worker image requires the backend build context"
                            )
                    app = re.fullmatch(
                        r"ghcr\.io/(?:OWNER/REPO|\$\{GHCR_REPOSITORY:\?[^}]+\})/("
                        + "|".join(PUBLISHED)
                        + r"):"
                        r"(?:VERSION|\$\{IMAGE_TAG:\?[^}]+\})",
                        reference,
                    )
                    mapped = app[1] if app else LOCAL_SERVICES.get(reference)
                    if mapped:
                        targets[ROLES.index(mapped)]["sources"].append(
                            dict(source, template=reference)
                        )
                    else:
                        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._/:@-]*", reference):
                            raise ValueError(f"unmapped image template at {location}")
                        services.setdefault(reference, []).append(source)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            errors.append(f"{path.relative_to(root)}: {exc}")
    if not sources or not (services or any(target["sources"] for target in targets)):
        errors.append("no deployment service image inventory found")
    for reference, locations in sorted(services.items()):
        targets.append(
            {
                "id": "service-" + hashlib.sha256(reference.encode()).hexdigest()[:16],
                "reference": reference,
                "role": "service",
                "pull": True,
                "source_pinned": bool(re.search(r"@sha256:[0-9a-f]{64}$", reference)),
                "sources": locations,
            }
        )
    return {"targets": targets, "base_inputs": bases, "errors": errors}


def redact(value: str) -> str:
    value = re.sub(r"eyJ[A-Za-z0-9_.-]+", "<redacted-token>", value)
    return re.sub(r"(https?://)[^/\s:@]+:[^/\s@]+@", r"\1<redacted>@", value)


def command(args: list[str], log: Path) -> int:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=300, check=False)
        log.write_text(redact(result.stdout + result.stderr))
        return result.returncode
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.write_text(redact(f"{type(exc).__name__}: image audit command could not complete"))
        return 2


def audit_target(target: dict, directory: Path) -> dict:
    result = dict(target, status="image_error")
    prefix = directory / target["id"]
    if target["pull"]:
        rc = command(
            ["docker", "pull", "--platform", "linux/amd64", target["reference"]],
            prefix.with_suffix(".pull.log"),
        )
        result["pull_exit"] = rc
        if rc:
            result["status"] = "unavailable"
            return result
    inspect = prefix.with_suffix(".inspect.json")
    try:
        inspected = subprocess.run(
            ["docker", "image", "inspect", target["reference"]],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if inspected.returncode:
            raise ValueError("image inspection failed")
        # Full image config stays in memory; Env never enters the artifact.
        (metadata,) = json.loads(inspected.stdout)
        image_id = metadata["Id"]
        if not SHA.fullmatch(image_id) or (
            metadata["Os"],
            metadata["Architecture"],
        ) != ("linux", "amd64"):
            raise ValueError("invalid image identity/platform")
        result.update(
            image_id=image_id,
            repository_digests=metadata.get("RepoDigests", []),
            platform="linux/amd64",
        )
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        subprocess.TimeoutExpired,
    ):
        inspect.write_text('{"status":"image inspection failed"}\n')
        return result
    # Keep only identity metadata in the artifact, never a container's environment.
    inspect.write_text(
        json.dumps({k: result[k] for k in ("image_id", "repository_digests", "platform")})
    )
    args = [
        "docker",
        "run",
        "--rm",
        "-v",
        "/var/run/docker.sock:/var/run/docker.sock",
    ]
    for variable, mount in (
        ("TRIVY_CA_BUNDLE", "/etc/ssl/certs/ca-certificates.crt:ro"),
        ("TRIVY_CACHE_DIR", "/root/.cache/trivy"),
    ):
        if os.environ.get(variable):
            args.extend(["-v", f"{Path(os.environ[variable]).resolve()}:{mount}"])
    sbom = prefix.with_suffix(".cdx.json")
    audit = prefix.with_suffix(".audit.json")
    result["status"] = "scanner_error"
    try:
        bom, data = scanner_reports(args, image_id, prefix, result)
        if (
            bom.get("bomFormat") != "CycloneDX"
            or not isinstance(bom.get("components"), list)
            or not bom["components"]
        ):
            raise ValueError("missing SBOM components")
        component = bom["metadata"]["component"]
        if component.get("type") != "container" or component.get("name") != image_id:
            raise ValueError("mismatched SBOM identity")
        if not any(
            p.get("name") == "aquasecurity:trivy:ImageID" and p.get("value") == image_id
            for p in component.get("properties", [])
        ):
            raise ValueError("missing SBOM image identity")
        if data["Metadata"]["ImageID"] != image_id or not data.get("Results"):
            raise ValueError("missing/mismatched audit identity")
        if not data["Metadata"].get("OS", {}).get("Family") or data["Metadata"]["OS"].get("EOSL"):
            raise ValueError("missing/unsupported OS coverage")
        if not any(r.get("Class") == "os-pkgs" for r in data["Results"]):
            raise ValueError("missing OS package audit")
        rows = [v for r in data["Results"] for v in r.get("Vulnerabilities", [])]
        for row in rows:
            if not all(row.get(k) for k in ("VulnerabilityID", "PkgName", "InstalledVersion")):
                raise ValueError("incomplete advisory evidence")
            if row.get("Severity") not in ("HIGH", "CRITICAL"):
                raise ValueError("unexpected severity evidence")
        expected = 1 if rows else 0
        if result["sbom_exit"] != 0 or result["audit_exit"] != expected:
            raise ValueError("scanner exit does not match evidence")
        result.update(
            status="vulnerable" if rows else "clean",
            advisory_rows=len(rows),
            advisory_ids=sorted({v["VulnerabilityID"] for v in rows}),
            advisories=[
                {
                    key: row.get(key, "")
                    for key in (
                        "VulnerabilityID",
                        "PkgName",
                        "InstalledVersion",
                        "FixedVersion",
                        "Severity",
                    )
                }
                for row in rows
            ],
            evidence={p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (sbom, audit)},
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return result


def scanner_reports(args: list[str], image_id: str, prefix: Path, result: dict) -> tuple:
    # ImageConfig includes Env/history. Raw scanner JSON never enters the public
    # artifact tree, even if an interrupted run cannot finish sanitizing it.
    with tempfile.TemporaryDirectory(prefix="bidit-image-audit-") as temporary:
        private = Path(temporary)
        common = [
            *args,
            "-v",
            f"{private}:/reports",
            SCANNER,
            "image",
            "--image-src",
            "docker",
            "--scanners",
            "vuln",
        ]
        for name, fmt, extra in (
            ("sbom", "cyclonedx", []),
            ("audit", "json", ["--severity", "HIGH,CRITICAL", "--exit-code", "1"]),
        ):
            filename = prefix.with_suffix(".cdx.json" if name == "sbom" else ".audit.json").name
            result[name + "_exit"] = command(
                [
                    *common,
                    *extra,
                    "--format",
                    fmt,
                    "--output",
                    f"/reports/{filename}",
                    image_id,
                ],
                prefix.with_suffix(f".{name}.log"),
            )
        raw_sbom = private / prefix.with_suffix(".cdx.json").name
        raw_audit = private / prefix.with_suffix(".audit.json").name
        result["raw_report_sha256"] = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (raw_sbom, raw_audit)
            if p.exists()
        }
        bom, data = json.loads(raw_sbom.read_text()), json.loads(raw_audit.read_text())
    metadata = data["Metadata"]
    data["Metadata"] = {
        k: metadata[k]
        for k in (
            "ImageID",
            "OS",
            "Size",
            "RepoTags",
            "RepoDigests",
            "DiffIDs",
            "Reference",
        )
        if k in metadata
    }
    component = bom["metadata"]["component"]
    component["properties"] = [
        p
        for p in component.get("properties", [])
        if not p.get("name", "").startswith("aquasecurity:trivy:Labels:")
    ]
    prefix.with_suffix(".cdx.json").write_text(json.dumps(bom, indent=2) + "\n")
    prefix.with_suffix(".audit.json").write_text(json.dumps(data, indent=2) + "\n")
    result["evidence_sanitized"] = ["ImageConfig", "image labels"]
    return bom, data


def write_cache_evidence(cache: Path, output: Path) -> None:
    """Record the observed final DB, without implying every scan used these bytes."""
    receipt = {
        "scanner": SCANNER,
        "github_run_id": os.environ.get("GITHUB_RUN_ID"),
        "github_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "status": "unavailable",
        "scope": "post-audit cache observation; not an in-process per-scan DB attestation",
    }
    database, metadata = cache / "db/trivy.db", cache / "db/metadata.json"
    try:
        before = (database.stat(), metadata.stat())
        metadata_bytes = metadata.read_bytes()
        details = json.loads(metadata_bytes)
        if not isinstance(details, dict):
            raise TypeError("invalid DB metadata")
        digest = hashlib.sha256()
        with database.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        after = (database.stat(), metadata.stat())
        stable = all(
            (a.st_dev, a.st_ino, a.st_size, a.st_mtime_ns, a.st_ctime_ns)
            == (b.st_dev, b.st_ino, b.st_size, b.st_mtime_ns, b.st_ctime_ns)
            for a, b in zip(before, after, strict=True)
        )
        receipt.update(
            status="recorded" if stable else "unstable",
            stable_during_hash=stable,
            database_bytes=before[0].st_size,
            database_sha256=digest.hexdigest(),
            metadata_sha256=hashlib.sha256(metadata_bytes).hexdigest(),
            metadata={
                key: details[key]
                for key in ("Version", "NextUpdate", "UpdatedAt", "DownloadedAt")
                if key in details
            },
        )
    except (OSError, ValueError, TypeError):
        # A failed download/corrupt cache must never inherit a previous receipt.
        receipt["error"] = "database or metadata could not be recorded"
    output.write_text(json.dumps(receipt, indent=2) + "\n")


def readable_summary(summary: dict) -> str:
    def cell(value) -> str:
        value = html.escape(redact(str(value))).replace("\n", " ").replace("\r", " ")
        for old, new in (
            ("|", "&#124;"),
            ("`", "&#96;"),
            ("[", "&#91;"),
            ("]", "&#93;"),
        ):
            value = value.replace(old, new)
        return value

    lines = [
        "## Image security gate: " + ("PASS" if summary.get("passed") else "BLOCKED"),
        "",
        (
            "Build, final application and deployment service images are all required. "
            "Unavailable or unverified images are not clean scans."
        ),
        "",
        "| Image | Role | Result | HIGH / CRITICAL advisory rows |",
        "| --- | --- | --- | --- |",
    ]
    for result in summary.get("results", []):
        status = result["status"]
        count = str(result["advisory_rows"]) if status in {"clean", "vulnerable"} else "not scanned"
        if not result.get("source_pinned", True):
            status += " (unpinned source)"
        lines.append(
            "| "
            + " | ".join(map(cell, [result["reference"], result["role"], status, count]))
            + " |"
        )
    for error in summary.get("inventory", {}).get("errors", []):
        lines.append("\nInventory problem: " + cell(error))
    for result in summary.get("results", []):
        rows = result.get("advisories", [])
        if not rows:
            continue
        lines.extend(
            [
                "",
                "### " + cell(result["reference"]),
                "",
                "| Severity | Advisory | Package | Installed | Fixed version reported by scanner |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for row in rows[:100]:
            fields = [
                row[key]
                for key in (
                    "Severity",
                    "VulnerabilityID",
                    "PkgName",
                    "InstalledVersion",
                )
            ]
            fields.append(row.get("FixedVersion") or "No fix listed")
            lines.append("| " + " | ".join(map(cell, fields)) + " |")
        if len(rows) > 100:
            lines.append(
                f"\nShowing 100 of {len(rows)} rows for this image; the artifact retains all rows."
            )
    lines.extend(
        [
            "",
            (
                "Download the **final-image-security** artifact for complete JSON/SBOM evidence "
                "and sanitized pull/scanner logs. Inspect unavailable image references, registry access "
                "and supported vendor fixes; this report does not waive findings or change the gate."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def write_report(summary: dict, output: Path, *, github: bool = False) -> None:
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    report = readable_summary(summary)
    (output / "summary.md").write_text(report)
    if github and os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as sink:
            sink.write(report)


def run(root: Path, output: Path, built: dict[str, str]) -> int:
    output.mkdir(parents=True, exist_ok=True)
    directory = output / uuid.uuid4().hex
    directory.mkdir()
    summary = {
        "scope": "application build/final and Compose/Kubernetes deployment images",
        "run": directory.name,
        "scanner": SCANNER,
        "inventory": {"targets": [], "errors": ["inventory not completed"]},
        "results": [],
        "passed": False,
    }
    # Write a non-passing marker before I/O so interrupted runs cannot retain green.
    write_report(summary, output)
    try:
        plan = inventory(root, built)
    except ValueError:
        write_report(summary, output, github=True)
        return 1
    summary["inventory"] = plan
    for target in plan["targets"]:
        result = audit_target(target, directory)
        summary["results"].append(result)
        write_report(summary, output)
        sys.stdout.write(f"image-audit: {target['reference']}: {result['status']}\n")
        sys.stdout.flush()
    summary["passed"] = (
        bool(summary["results"])
        and not plan["errors"]
        and all(r["status"] == "clean" and r.get("source_pinned", True) for r in summary["results"])
    )
    write_report(summary, output, github=True)
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, default=Path("image-security"))
    for role in ROLES:
        parser.add_argument("--" + role, default="bidit-ci-" + role + ":review")
    args = parser.parse_args()
    try:
        audit_exit = run(
            args.root,
            args.output,
            {r: getattr(args, r.replace("-", "_")) for r in ROLES},
        )
    finally:
        if os.environ.get("TRIVY_CACHE_DIR"):
            write_cache_evidence(Path(os.environ["TRIVY_CACHE_DIR"]), args.output / "trivy-db.json")
    raise SystemExit(audit_exit)
