"""Seeded image/evidence failures must block without hiding later targets."""

import hashlib
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("image_audit", ROOT / "scripts/image_audit.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)

BUILT = {role: "bidit-ci-" + role + ":review" for role in audit.ROLES}
OBJECT_STORE = (
    "rustfs/rustfs:1.0.4@sha256:ed9210d6411d77ffceb70d94b964a3cb7d225983368bc25bebe01e995293dcc2"
)


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    for relative in {value[0] for value in audit.BUILD_OUTPUTS.values()}:
        path = root / relative
        path.parent.mkdir(parents=True)
        stages = [
            stage
            for file, stage, _ in audit.BUILD_OUTPUTS.values()
            if file == relative and stage != "runtime"
        ] + ["runtime"]
        path.write_text(
            "".join(f"FROM synthetic@sha256:{'a' * 64} AS {stage}\n" for stage in stages)
        )
    (root / "docker-compose.yml").write_text(
        'services:\n  db:\n    image: "postgres:16@sha256:' + "c" * 64 + '"\n'
    )
    return root


@pytest.fixture
def fake_docker(tmp_path, monkeypatch):
    path = tmp_path / "bin"
    path.mkdir()
    docker = path / "docker"
    docker.write_text(
        """#!/usr/bin/env python3
import hashlib,json,os,sys
from pathlib import Path
a=sys.argv[1:]; mode=os.environ.get('IMAGE_AUDIT_FAULT','')
if a[0]=='pull':
 sys.exit(1 if mode=='pull' else 0)
if a[:2]==['image','inspect']:
 ref=a[-1]; ident='sha256:'+hashlib.sha256(ref.encode()).hexdigest()
 m={'Id':ident,'Architecture':'amd64','Os':'linux','RepoDigests':[],
    'Config':{'Env':['SYNTHETIC=private-value-do-not-publish']}}
 if mode=='inspect' and ref=='bidit-ci-backend:review':m.pop('Id')
 print(json.dumps([m]));sys.exit(0)
base=next(x[:-9] for x in a if x.endswith(':/reports'))
filename=Path(a[a.index('--output')+1]).name
p=Path(base)/filename
ident=a[-1]; fmt=a[a.index('--format')+1]
first=filename.startswith('backend.')
if mode=='missing' and first:sys.exit(0)
if mode=='malformed' and first:p.write_text('not JSON');sys.exit(0)
if fmt=='cyclonedx':
 bom_id='sha256:'+'0'*64 if mode=='sbom_identity' and first else ident
 p.write_text(json.dumps({'bomFormat':'CycloneDX','components':[{'type':'library','name':'synthetic'}],
  'metadata':{'component':{'type':'container','name':bom_id,'properties':[{'name':'aquasecurity:trivy:ImageID','value':bom_id},{'name':'aquasecurity:trivy:Labels:synthetic','value':'private-value-do-not-publish'}]}}}))
 sys.exit(2 if mode=='sbom' and first else 0)
rows=[]
if mode in ('vulnerable','false_zero') and first:
 rows=[{'VulnerabilityID':'SYNTHETIC-CVE','PkgName':'synthetic','InstalledVersion':'1','Severity':'CRITICAL'}]
if mode=='identity' and first:ident='sha256:'+'0'*64
data={'Metadata':{'ImageID':ident,'OS':{'Family':'synthetic'},'ImageConfig':{'config':{'Env':['private-value-do-not-publish']}}},
      'Results':[{'Class':'os-pkgs','Vulnerabilities':rows}]}
if mode=='unsupported' and first:data['Metadata']['OS']['EOSL']=True
if mode=='bad_results' and first:data['Results']=[None]
p.write_text(json.dumps(data))
sys.exit(2 if mode=='scanner' and first else 1 if mode=='false_nonzero' and first else 0 if mode=='false_zero' else 1 if rows else 0)
"""
    )
    docker.chmod(0o755)
    monkeypatch.setenv("PATH", str(path) + ":" + os.environ["PATH"])


def test_current_inventory_covers_every_deployment_reference_and_build_output():
    plan = audit.inventory(ROOT, BUILT)
    assert plan["errors"] == []
    assert len(plan["targets"]) == len(BUILT) + 1
    stages = [value for value in plan["base_inputs"] if value["instruction"] == "FROM"]
    assert len(stages) == len(BUILT)
    assert {value["covered_by"] for value in plan["base_inputs"]} == set(BUILT)
    external_copy = [value for value in plan["base_inputs"] if value["instruction"] == "COPY"]
    assert len(external_copy) == 1
    assert external_copy[0]["reference"].startswith("ghcr.io/astral-sh/uv:")
    assert external_copy[0]["covered_by"] == "backend-build"
    external = [t for t in plan["targets"] if t["role"] == "service"]
    assert {target["reference"] for target in external} == {OBJECT_STORE}
    assert external[0]["pull"] is True and external[0]["source_pinned"] is True
    expected = set()
    sources = sorted(ROOT.glob("*compose*.y*ml")) + sorted((ROOT / "deploy/k8s").rglob("*.y*ml"))
    for path in sources:
        for document, doc in enumerate(yaml.load_all(path.read_text(), Loader=audit.ComposeLoader)):
            expected.update(
                (str(path.relative_to(ROOT)), document, location)
                for location, _ in audit.image_fields(doc)
            )
    mapped = [
        (source["file"], source["document"], source["location"])
        for target in plan["targets"]
        for source in target["sources"]
    ]
    assert len(mapped) == len(set(mapped))
    assert set(mapped) == expected
    for role in ("postgres", "pgbouncer"):
        target = next(t for t in plan["targets"] if t["id"] == role)
        assert target["sources"] and target["pull"] is False


def test_local_calendar_and_general_consumers_map_to_the_audited_backend(source):
    (source / "compose.calendar.yaml").write_text(
        yaml.safe_dump(
            {
                "services": {
                    name: {"build": "./backend", "image": audit.LOCAL_WORKER_IMAGE}
                    for name in ("worker", "calendar-worker")
                }
            }
        )
    )
    plan = audit.inventory(source, BUILT)
    assert not plan["errors"]
    backend = next(target for target in plan["targets"] if target["id"] == "backend")
    mapped = [item for item in backend["sources"] if item["file"] == "compose.calendar.yaml"]
    assert {item["location"] for item in mapped} == {
        "/services/worker/image",
        "/services/calendar-worker/image",
    }
    assert backend["reference"] == BUILT["backend"] and backend["pull"] is False
    assert all(
        target["reference"] != audit.LOCAL_WORKER_IMAGE
        for target in plan["targets"]
        if target["role"] == "service"
    )


@pytest.mark.parametrize(
    "name,build",
    [
        ("worker", "./frontend"),
        ("calendar-worker", None),
        ("calendar-worker", {"context": "./backend", "dockerfile": "other.Dockerfile"}),
        ("unreviewed-consumer", "./backend"),
    ],
)
def test_local_worker_alias_cannot_hide_changed_build_provenance(source, name, build):
    (source / "compose.calendar.yaml").write_text(
        yaml.safe_dump({"services": {name: {"build": build, "image": audit.LOCAL_WORKER_IMAGE}}})
    )
    plan = audit.inventory(source, BUILT)
    assert any("local worker image" in error for error in plan["errors"])


def test_other_project_worker_templates_still_fail_closed(source):
    (source / "compose.calendar.yaml").write_text(
        yaml.safe_dump(
            {
                "services": {
                    "worker": {
                        "build": "./backend",
                        "image": "${UNREVIEWED_PROJECT:-invoiceiq}-worker",
                    }
                }
            }
        )
    )
    plan = audit.inventory(source, BUILT)
    assert any("unmapped image template" in error for error in plan["errors"])


def test_ci_builds_and_loads_every_audited_stage():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    steps = workflow["jobs"]["docker-build"]["steps"]
    built = {
        s["with"]["tags"]: s["with"] for s in steps if "build-push-action@" in s.get("uses", "")
    }
    for role, reference in BUILT.items():
        assert reference in built, f"missing audited image {reference}"
        assert built[reference]["load"] is True and built[reference]["push"] is False
        dockerfile, stage, kind = audit.BUILD_OUTPUTS[role]
        assert built[reference]["context"] == "./" + str(Path(dockerfile).parent)
        assert built[reference].get("target", "runtime") == stage
        assert kind == ("final" if role in audit.PUBLISHED else "build")


def test_release_publishes_every_final_artifact_and_database_overlay_uses_same_commit():
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    matrix = workflow["jobs"]["images"]["strategy"]["matrix"]["include"]
    assert {item["name"] for item in matrix} == set(audit.PUBLISHED)
    for item in matrix:
        assert item["context"] == "./" + str(Path(audit.BUILD_OUTPUTS[item["name"]][0]).parent)
    # Inspect the YAML node: a plain null must not masquerade as the reset that
    # removes the inherited db build when Compose merges the overlays.
    overlay = yaml.compose((ROOT / "docker-compose.images.yml").read_text())
    services = next(value for key, value in overlay.value if key.value == "services")
    service_nodes = {key.value: value for key, value in services.value}
    db = {key.value: value for key, value in service_nodes["db"].value}
    assert db["build"].tag == "!reset" and db["build"].value == "null"
    assert db["image"].value.startswith("ghcr.io/${GHCR_REPOSITORY:?")
    assert "/postgres:${IMAGE_TAG:?" in db["image"].value


def test_native_s3_qualification_follows_successful_security_audit_and_keeps_evidence():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    steps = workflow["jobs"]["docker-build"]["steps"]
    scan = next(s for s in steps if s.get("id") == "image_audit")
    native = next(s for s in steps if "check_s3_storage.py" in s.get("run", ""))
    assert steps.index(scan) < steps.index(native)
    assert native["if"] == "${{ !cancelled() && steps.image_audit.conclusion == 'success' }}"
    assert native["run"] == (
        "python scripts/check_s3_storage.py bidit-ci-backend:review > image-security/storage-smoke.json"
    )
    upload = next(s for s in steps if s.get("name") == "Image security evidence")
    assert upload["if"] == "always()" and upload["with"]["path"] == "image-security/"


def test_additional_compose_yaml_and_nested_init_container_are_discovered(source):
    (source / "compose.audit.yaml").write_text(
        "services:\n  helper:\n    image: registry.example/helper@sha256:" + "d" * 64 + "\n"
    )
    directory = source / "deploy/k8s/optional"
    directory.mkdir(parents=True)
    (directory / "job.yaml").write_text(
        "spec:\n  template:\n    spec:\n      initContainers:\n"
        "        - name: init\n          image: registry.example/init@sha256:" + "e" * 64 + "\n"
    )
    plan = audit.inventory(source, BUILT)
    assert plan["errors"] == []
    assert len(plan["targets"]) == len(BUILT) + 3
    assert any(
        t["sources"][0]["file"] == "deploy/k8s/optional/job.yaml"
        for t in plan["targets"]
        if t["role"] == "service"
    )


def test_rebuilt_service_sources_map_to_the_same_audited_runtime(source):
    (source / "compose.rebuilds.yaml").write_text(
        "services:\n"
        "  postgres-local:\n    image: bidit-postgres:local\n"
        "  pgbouncer-local:\n    image: bidit-pgbouncer:local\n"
        "  postgres-release:\n    image: ghcr.io/OWNER/REPO/postgres:VERSION\n"
        "  pgbouncer-release:\n    image: ghcr.io/OWNER/REPO/pgbouncer:VERSION\n"
    )
    plan = audit.inventory(source, BUILT)
    assert plan["errors"] == []
    assert len(plan["targets"]) == len(BUILT) + 1
    for role in ("postgres", "pgbouncer"):
        target = next(t for t in plan["targets"] if t["id"] == role)
        assert target["reference"] == BUILT[role]
        assert target["pull"] is False and target["role"] == "final"
        assert {item["template"] for item in target["sources"]} == {
            f"bidit-{role}:local",
            f"ghcr.io/OWNER/REPO/{role}:VERSION",
        }


@pytest.mark.parametrize("fault", ["added", "removed", "duplicate", "mutable", "missing"])
def test_unmapped_or_missing_stage_blocks_without_dropping_any_output(
    source, fake_docker, tmp_path, fault
):
    path = source / "frontend/Dockerfile"
    original = path.read_text()
    if fault == "added":
        path.write_text(f"FROM synthetic@sha256:{'a' * 64} AS extra\n" + original)
    elif fault == "removed":
        path.write_text(
            "\n".join(
                line for line in original.splitlines() if "AS typescript-compiler" not in line
            )
        )
    elif fault == "duplicate":
        path.write_text(original.replace("AS typescript-compiler", "AS build"))
    elif fault == "mutable":
        path.write_text(original.replace("synthetic@sha256:" + "a" * 64, "synthetic:latest", 1))
    else:
        path.unlink()
    out = tmp_path / "reports"
    assert audit.run(source, out, BUILT) == 1
    summary = json.loads((out / "summary.json").read_text())
    assert summary["inventory"]["errors"]
    assert not summary["passed"]
    assert len(summary["results"]) == len(BUILT) + 1
    assert {result["id"] for result in summary["results"]} >= set(BUILT)


def test_unnamed_final_runtime_is_covered_but_unnamed_intermediate_stage_is_not(source):
    path = source / "frontend/Dockerfile"
    path.write_text(path.read_text().replace(" AS runtime", ""))
    plan = audit.inventory(source, BUILT)
    assert plan["errors"] == []
    runtime = next(item for item in plan["base_inputs"] if item["covered_by"] == "frontend")
    assert runtime["stage"] is None
    path.write_text(path.read_text().replace(" AS typescript-compiler", ""))
    assert audit.inventory(source, BUILT)["errors"]


def test_external_copy_input_requires_a_digest_and_records_the_consumer_stage(source):
    path = source / "backend/Dockerfile"
    original = path.read_text()
    copied = f"COPY --from=registry.example/tool@sha256:{'d' * 64} /tool /tool\n"
    lines = original.splitlines(keepends=True)
    path.write_text(lines[0] + copied + "".join(lines[1:]))
    plan = audit.inventory(source, BUILT)
    assert plan["errors"] == []
    external = [item for item in plan["base_inputs"] if item["instruction"] == "COPY"]
    assert external == [
        {
            "file": "backend/Dockerfile",
            "reference": "registry.example/tool@sha256:" + "d" * 64,
            "stage": "build",
            "covered_by": "backend-build",
            "instruction": "COPY",
        }
    ]
    path.write_text(path.read_text().replace("@sha256:" + "d" * 64, ":latest"))
    assert audit.inventory(source, BUILT)["errors"]


def test_wrapped_external_copy_is_not_hidden_from_provenance_or_digest_validation(source):
    path = source / "backend/Dockerfile"
    lines = path.read_text().splitlines(keepends=True)
    copied = (
        "COPY " + "\\" + "\n" + f"    --from=registry.example/tool@sha256:{'d' * 64} /tool /tool\n"
    )
    path.write_text(lines[0] + copied + "".join(lines[1:]))
    plan = audit.inventory(source, BUILT)
    assert plan["errors"] == []
    assert any(
        item["instruction"] == "COPY" and item["covered_by"] == "backend-build"
        for item in plan["base_inputs"]
    )
    path.write_text(path.read_text().replace("@sha256:" + "d" * 64, ":latest"))
    assert audit.inventory(source, BUILT)["errors"]


def test_ci_collects_the_full_inventory_after_another_check_fails():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    steps = workflow["jobs"]["docker-build"]["steps"]
    scans = [s for s in steps if "image_audit.py" in s.get("run", "")]
    assert len(scans) == 1
    assert any(x in scans[0].get("if", "") for x in ("always()", "!cancelled()"))
    assert not scans[0].get("continue-on-error", False)


@pytest.mark.parametrize("failed_role", audit.ROLES)
def test_every_built_output_is_required_even_when_all_other_images_are_clean(
    source, tmp_path, monkeypatch, failed_role
):
    inspected = []

    def result(target, directory):
        inspected.append(target["id"])
        return dict(
            target,
            status="image_error" if target["id"] == failed_role else "clean",
            advisory_rows=0,
        )

    monkeypatch.setattr(audit, "audit_target", result)
    out = tmp_path / "reports"
    assert audit.run(source, out, BUILT) == 1
    summary = json.loads((out / "summary.json").read_text())
    assert len(inspected) == len(BUILT) + 1
    assert set(inspected) >= set(BUILT)
    assert not summary["passed"]
    assert sum(item["status"] != "clean" for item in summary["results"]) == 1


def test_clean_requires_all_targets_and_fresh_identity_evidence(source, fake_docker, tmp_path):
    out = tmp_path / "reports"
    assert audit.run(source, out, BUILT) == 0
    first = json.loads((out / "summary.json").read_text())["run"]
    assert audit.run(source, out, BUILT) == 0
    final = json.loads((out / "summary.json").read_text())
    assert final["passed"] and final["run"] != first
    assert len(final["results"]) == len(BUILT) + 1
    assert all(r["status"] == "clean" for r in final["results"])
    assert all("private-value-do-not-publish" not in p.read_text() for p in out.rglob("*.*"))


@pytest.mark.parametrize(
    "mode",
    [
        "vulnerable",
        "false_zero",
        "scanner",
        "sbom",
        "sbom_identity",
        "missing",
        "malformed",
        "identity",
        "unsupported",
        "bad_results",
        "false_nonzero",
        "inspect",
        "pull",
    ],
)
def test_failure_never_becomes_clean_or_hides_later_images(
    source, fake_docker, tmp_path, monkeypatch, mode
):
    monkeypatch.setenv("IMAGE_AUDIT_FAULT", mode)
    out = tmp_path / "reports"
    assert audit.run(source, out, BUILT) == 1
    final = json.loads((out / "summary.json").read_text())
    assert not final["passed"] and len(final["results"]) == len(BUILT) + 1
    assert any(r["status"] == "clean" for r in final["results"])
    assert any(r["status"] != "clean" for r in final["results"])
    assert all("private-value-do-not-publish" not in p.read_text() for p in out.rglob("*.*"))


def test_mutable_or_unmapped_service_configuration_blocks(source, fake_docker, tmp_path):
    compose = source / "docker-compose.yml"
    compose.write_text("services:\n  db:\n    image: postgres:16\n")
    assert audit.run(source, tmp_path / "mutable", BUILT) == 1
    compose.write_text("services:\n  db:\n    image: ${UNREVIEWED_IMAGE}\n")
    assert audit.run(source, tmp_path / "template", BUILT) == 1


def test_replacement_store_does_not_hide_reintroduced_legacy_images(source, fake_docker, tmp_path):
    (source / "docker-compose.yml").write_text(f"services:\n  store:\n    image: {OBJECT_STORE}\n")
    legacy = {"minio/minio:latest", "minio/mc:latest"}
    (source / "compose.legacy.yaml").write_text(
        "services:\n  legacy-store:\n    image: minio/minio:latest\n"
        "  legacy-client:\n    image: minio/mc:latest\n"
    )
    out = tmp_path / "reports"
    # Even successful synthetic pulls/scans cannot make mutable sources pass;
    # all three external references must remain visible in the final report.
    assert audit.run(source, out, BUILT) == 1
    summary = json.loads((out / "summary.json").read_text())
    external = [result for result in summary["results"] if result["role"] == "service"]
    assert {result["reference"] for result in external} == legacy | {OBJECT_STORE}
    assert len(summary["results"]) == len(BUILT) + 3
    assert all(
        result["source_pinned"] is False for result in external if result["reference"] in legacy
    )
    assert not summary["passed"]


def test_invalid_inventory_cannot_retain_a_previous_passing_summary(source, fake_docker, tmp_path):
    out = tmp_path / "reports"
    assert audit.run(source, out, BUILT) == 0
    duplicate = dict.fromkeys(BUILT, "same:review")
    assert audit.run(source, out, duplicate) == 1
    assert json.loads((out / "summary.json").read_text())["passed"] is False


def test_timeout_is_a_tool_error_without_echoing_submitted_secrets(tmp_path, monkeypatch):
    def timeout(*args, **kwargs):
        raise audit.subprocess.TimeoutExpired("synthetic-secret", 300)

    monkeypatch.setattr(audit.subprocess, "run", timeout)
    log = tmp_path / "timeout.log"
    assert audit.command(["synthetic"], log) == 2
    assert "synthetic-secret" not in log.read_text()


def test_cache_receipt_hashes_observed_bytes_and_does_not_retain_stale_success(tmp_path):
    cache = tmp_path / "cache"
    (cache / "db").mkdir(parents=True)
    database, metadata = cache / "db/trivy.db", cache / "db/metadata.json"
    database.write_bytes(b"synthetic database bytes")
    metadata.write_text('{"Version":2,"NextUpdate":"synthetic-next-update"}\n')
    output = tmp_path / "trivy-db.json"
    audit.write_cache_evidence(cache, output)
    receipt = json.loads(output.read_text())
    assert receipt["status"] == "recorded" and receipt["stable_during_hash"] is True
    assert receipt["database_sha256"] == hashlib.sha256(database.read_bytes()).hexdigest()
    assert receipt["metadata_sha256"] == hashlib.sha256(metadata.read_bytes()).hexdigest()
    assert receipt["metadata"]["NextUpdate"] == "synthetic-next-update"
    metadata.write_text("not JSON")
    audit.write_cache_evidence(cache, output)
    failed = json.loads(output.read_text())
    assert failed["status"] == "unavailable"
    assert "database_sha256" not in failed and "metadata" not in failed


def test_cache_receipt_marks_database_changed_during_hash_unstable(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    (cache / "db").mkdir(parents=True)
    database = cache / "db/trivy.db"
    database.write_bytes(b"before")
    (cache / "db/metadata.json").write_text('{"Version":2}\n')
    sha256 = hashlib.sha256

    class ChangedWhileHashing:
        def __init__(self):
            self.digest = sha256()

        def update(self, block):
            self.digest.update(block)
            database.write_bytes(b"a changed database")

        def hexdigest(self):
            return self.digest.hexdigest()

    monkeypatch.setattr(
        audit.hashlib, "sha256", lambda *args: sha256(*args) if args else ChangedWhileHashing()
    )
    output = tmp_path / "trivy-db.json"
    audit.write_cache_evidence(cache, output)
    receipt = json.loads(output.read_text())
    assert receipt["status"] == "unstable" and receipt["stable_during_hash"] is False


def test_failed_cli_writes_cache_receipt_without_masking_failure(
    source, fake_docker, tmp_path, monkeypatch
):
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setenv("TRIVY_CACHE_DIR", str(cache))
    monkeypatch.setenv("IMAGE_AUDIT_FAULT", "scanner")
    out = tmp_path / "reports"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/image_audit.py"),
            "--root",
            str(source),
            "--output",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    summary = json.loads((out / "summary.json").read_text())
    assert summary["passed"] is False and len(summary["results"]) == len(BUILT) + 1
    receipt = json.loads((out / "trivy-db.json").read_text())
    assert receipt["status"] == "unavailable" and "database_sha256" not in receipt


@pytest.mark.parametrize(
    "audit_exit,cleanup_exit,signal,expected_exit",
    [
        (0, 0, "", 0),
        (7, 0, "", 7),
        (0, 9, "", 1),
        (7, 9, "", 7),
        (0, 0, "TERM", 143),
        (0, 0, "INT", 130),
    ],
)
def test_run_owned_cache_cleanup_preserves_audit_and_cleanup_failures(
    tmp_path, audit_exit, cleanup_exit, signal, expected_exit
):
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    step = next(
        s for s in workflow["jobs"]["docker-build"]["steps"] if s.get("id") == "image_audit"
    )
    binaries = tmp_path / "bin"
    binaries.mkdir()
    (binaries / "python").write_text(
        "#!/bin/bash\nset -eu\n"
        'test -d "$TRIVY_CACHE_DIR"\n'
        'test -z "$(ls -A "$TRIVY_CACHE_DIR")"\n'
        'printf "%s" "$TRIVY_CACHE_DIR" > "$CACHE_TEST_PATH"\n'
        'mkdir "$TRIVY_CACHE_DIR/root-owned-simulation"\n'
        'if [ -n "$CACHE_TEST_SIGNAL" ]; then kill "-$CACHE_TEST_SIGNAL" "$PPID"; fi\n'
        'exit "$CACHE_TEST_AUDIT_EXIT"\n'
    )
    (binaries / "sudo").write_text(
        "#!/bin/bash\nset -eu\n"
        'if [ "$CACHE_TEST_CLEANUP_EXIT" -ne 0 ]; then exit "$CACHE_TEST_CLEANUP_EXIT"; fi\n'
        'exec "$@"\n'
    )
    for binary in binaries.iterdir():
        binary.chmod(0o755)
    runner = tmp_path / "runner"
    runner.mkdir()
    inherited = runner / "unrelated-cache"
    inherited.mkdir()
    sentinel = inherited / "must-remain"
    sentinel.write_text("unrelated")
    path = tmp_path / "created-cache.txt"
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", step["run"]],
        env=dict(
            os.environ,
            PATH=str(binaries) + ":" + os.environ["PATH"],
            RUNNER_TEMP=str(runner),
            GITHUB_RUN_ID="12345",
            GITHUB_RUN_ATTEMPT="2",
            TRIVY_CACHE_DIR=str(inherited),
            CACHE_TEST_PATH=str(path),
            CACHE_TEST_AUDIT_EXIT=str(audit_exit),
            CACHE_TEST_CLEANUP_EXIT=str(cleanup_exit),
            CACHE_TEST_SIGNAL=signal,
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == expected_exit
    created = Path(path.read_text())
    assert created.parent == runner and created.name.startswith("bidit-trivy-12345-2.")
    assert created.exists() is bool(cleanup_exit)
    assert sentinel.read_text() == "unrelated"
    assert ("::error::" in result.stdout) is bool(cleanup_exit)


@pytest.mark.parametrize("mode", ["vulnerable", "pull", "malformed"])
def test_failed_gate_publishes_readable_summary_without_changing_exit(
    source, fake_docker, tmp_path, monkeypatch, mode
):
    out = tmp_path / "reports"
    github_summary = tmp_path / "github-summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(github_summary))
    monkeypatch.setenv("IMAGE_AUDIT_FAULT", mode)
    assert audit.run(source, out, BUILT) == 1
    report = (out / "summary.md").read_text()
    assert "BLOCKED" in report
    assert "final-image-security" in report
    assert "private-value-do-not-publish" not in report
    assert github_summary.read_text() == report
    if mode == "vulnerable":
        assert "SYNTHETIC-CVE" in report and "CRITICAL" in report
    else:
        assert "not scanned" in report


def test_readable_summary_does_not_turn_bad_inventory_green(tmp_path, monkeypatch):
    out = tmp_path / "reports"
    github_summary = tmp_path / "github-summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(github_summary))
    monkeypatch.setattr(audit, "inventory", lambda *args: (_ for _ in ()).throw(ValueError()))
    assert audit.run(tmp_path, out, BUILT) == 1
    assert "BLOCKED" in github_summary.read_text()
    assert json.loads((out / "summary.json").read_text())["passed"] is False


def test_readable_summary_escapes_metadata_and_explicitly_counts_preview_rows():
    row = {
        "Severity": "HIGH",
        "VulnerabilityID": "SYNTHETIC-CVE",
        "PkgName": "[unsafe](https://example.com)|<img>",
        "InstalledVersion": "1",
        "FixedVersion": "",
    }
    summary = {
        "passed": False,
        "inventory": {"errors": []},
        "results": [
            {
                "reference": "https://synthetic-user:synthetic-secret@registry.example/image",
                "role": "build",
                "status": "vulnerable",
                "advisory_rows": 101,
                "advisories": [dict(row) for _ in range(101)],
            }
        ],
    }
    report = audit.readable_summary(summary)
    assert "synthetic-secret" not in report
    assert "<img>" not in report and "[unsafe](" not in report
    assert "&#124;" in report and "&lt;img&gt;" in report
    assert "Showing 100 of 101" in report
    assert "No fix listed" in report
    assert len(summary["results"][0]["advisories"]) == 101


# These are the seven affected outputs from the preserved full image inventories,
# not all Alpine images. Keep package admission independent of scanner severity.
ALPINE_MAINTENANCE = {
    "backend-build": {
        "musl": "1.2.6-r3",
        "musl-utils": "1.2.6-r3",
        "zlib": "1.3.2-r1",
        "libexpat": "2.10.0-r0",
    },
    "backend": {
        "musl": "1.2.6-r3",
        "musl-utils": "1.2.6-r3",
        "zlib": "1.3.2-r1",
        "libexpat": "2.10.0-r0",
    },
    "postgres-build": {"musl": "1.2.6-r3", "musl-utils": "1.2.6-r3", "zlib": "1.3.2-r1"},
    "postgres": {"musl": "1.2.6-r3", "musl-utils": "1.2.6-r3", "zlib": "1.3.2-r1"},
    "pgbouncer-build": {
        "musl": "1.2.6-r3",
        "musl-utils": "1.2.6-r3",
        "libexpat": "2.10.0-r0",
    },
    "pgbouncer": {"musl": "1.2.6-r3", "musl-utils": "1.2.6-r3", "zlib": "1.3.2-r1"},
    "frontend": {
        "musl": "1.2.6-r3",
        "musl-utils": "1.2.6-r3",
        "libxpm": "3.5.20-r0",
        "libexpat": "2.10.0-r0",
    },
}
ALPINE_FIRST_USE = {
    "backend-build": ("uv", "pip", "install"),
    "postgres-build": ("curl",),
    "postgres": ("gosu",),
    "pgbouncer-build": ("curl",),
}


def _stage_shell_commands(text, stage):
    """Read this repository's shell RUN grammar, not arbitrary Docker/shell code.

    Default Docker continuations, --mount, quotes and command separators are
    supported. Admission below only accepts a leading apk add, optionally after
    set -eu, with only the current --no-cache/--upgrade apk options. Conditional
    installs, apk pipelines, simulation and failure recovery cannot qualify.
    COPY/CMD/USER declarations do not execute a workload during the build.
    """
    current = None
    commands = []
    for line in re.sub(r"\\\r?\n[ \t]*", " ", text).splitlines():
        match = re.match(r"^\s*FROM\s+\S+(?:\s+AS\s+(\S+))?\s*$", line, re.I)
        if match:
            current = match[1] or "runtime"
        run = re.match(r"^\s*RUN\s+(.+)$", line, re.I)
        if current != stage or not run:
            continue
        lexer = shlex.shlex(run[1], posix=False, punctuation_chars=";&|")
        lexer.whitespace_split = True
        tokens = list(lexer)
        while tokens and tokens[0].startswith("--mount="):
            tokens.pop(0)
        parts, command = [], []
        for token in tokens:
            if token in {";", "&&", "||", "|", "&"}:
                parts.append((command, token))
                command = []
            else:
                command.append(token)
        parts.append((command, None))
        commands.extend((parts, index) for index in range(len(parts)))
    return commands


def _stable_apk_version(version):
    # Supported maintenance releases use x.y.z-rN; this is deliberately not an
    # implementation of apk's complete version/dependency resolution algorithm.
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)-r(\d+)", version)
    assert match, f"unsupported Alpine maintenance version: {version}"
    return tuple(map(int, match.groups()))


def _assert_alpine_admission(text, role):
    stage = audit.BUILD_OUTPUTS[role][1]
    commands = _stage_shell_commands(text, stage)
    first_use = ALPINE_FIRST_USE.get(role)
    uses = [
        index
        for index, (parts, position) in enumerate(commands)
        if first_use
        and tuple(shlex.split(word)[0] for word in parts[position][0][: len(first_use)])
        == first_use
    ]
    if first_use:
        assert uses, f"{role}: update the contract for its dependency/runtime validation command"
    boundary = min(uses) if uses else len(commands)
    for package, minimum in ALPINE_MAINTENANCE[role].items():
        admitted = False
        for parts, position in commands[:boundary]:
            raw, following = parts[position]
            words = [shlex.split(word)[0] for word in raw]
            if words[:2] != ["apk", "add"]:
                continue
            fail_fast = (
                position == 1 and parts[0][0] == ["set", "-eu"] and parts[0][1] in {";", "&&"}
            )
            if position != 0 and not fail_fast:
                continue
            if any(
                word.startswith("-") and word not in {"--no-cache", "--upgrade"}
                for word in words[2:]
            ):
                continue
            if any(operator in {"||", "&"} for _, operator in parts):
                continue
            if not fail_fast and any(operator == ";" for _, operator in parts):
                continue
            if following == "&&" and any(operator == ";" for _, operator in parts[position + 1 :]):
                continue
            if following not in {None, "&&"} and not (following == ";" and fail_fast):
                continue
            # Explicit floors already force an inherited below-floor package to
            # be replaced. --upgrade is an additional admission-policy control,
            # not evidence that a fixed floor alone would keep a vulnerable one.
            if "--upgrade" not in words:
                continue
            for raw_word, word in zip(raw[2:], words[2:], strict=True):
                constraint = re.fullmatch(re.escape(package) + r"(>=|=)(.+)", word)
                if not constraint:
                    continue
                # An unquoted '>' redirects shell output instead of constraining
                # apk. Both single and double quoted constraints are supported.
                if constraint[1] == ">=" and raw_word[0] not in {"'", '"'}:
                    continue
                if _stable_apk_version(constraint[2]) >= _stable_apk_version(minimum):
                    admitted = True
        assert admitted, f"{role}: require {package}>={minimum} with --upgrade before use"


@pytest.mark.parametrize("role", ALPINE_MAINTENANCE)
def test_affected_alpine_stages_require_maintenance_before_dependency_or_runtime_validation(role):
    relative, _, _ = audit.BUILD_OUTPUTS[role]
    _assert_alpine_admission((ROOT / relative).read_text(), role)


def _maintenance_dockerfile(install=None, *, before="", after="", stage="build"):
    install = install or (
        "apk add --no-cache --upgrade 'musl>=1.2.6-r3' 'musl-utils>=1.2.6-r3' 'zlib>=1.3.2-r1' "
        "'libexpat>=2.10.0-r0'"
    )
    return (
        f"FROM synthetic AS {stage}\n{before}"
        f"RUN --mount=type=secret,id=build_ca {install}\n"
        f"{after}RUN uv pip install --prefix /opt/backend -r requirements.txt\n"
        "FROM synthetic AS runtime\n"
    )


@pytest.mark.parametrize("package", ["musl", "musl-utils", "zlib"])
@pytest.mark.parametrize("mutation", ["missing", "unversioned", "old-revision"])
def test_alpine_admission_rejects_missing_or_inherited_vulnerable_package_constraints(
    package, mutation
):
    text = _maintenance_dockerfile()
    token = f"'{package}>={ALPINE_MAINTENANCE['backend-build'][package]}'"
    replacement = {
        "missing": "",
        "unversioned": package,
        "old-revision": token.replace("-r3", "-r2").replace("-r1", "-r0"),
    }[mutation]
    with pytest.raises(AssertionError, match=re.escape(f"require {package}>=")):
        _assert_alpine_admission(text.replace(token, replacement), "backend-build")


@pytest.mark.parametrize("role", ["backend", "backend-build", "frontend", "pgbouncer-build"])
@pytest.mark.parametrize("mutation", ["missing", "unversioned", "old-release"])
def test_libexpat_admission_refuses_missing_unversioned_or_below_fixed_floor(role, mutation):
    stage = audit.BUILD_OUTPUTS[role][1]
    packages = " ".join(
        f"'{package}>={minimum}'" for package, minimum in ALPINE_MAINTENANCE[role].items()
    )
    text = f"FROM synthetic AS {stage}\nRUN apk add --no-cache --upgrade {packages}\n"
    if role == "pgbouncer-build":
        text += "RUN curl --fail https://example.test/source\n"
    elif role == "backend-build":
        text += "RUN uv pip install -r requirements.txt\n"
    _assert_alpine_admission(text, role)
    token = "'libexpat>=2.10.0-r0'"
    replacement = {
        "missing": "",
        "unversioned": "libexpat",
        "old-release": "'libexpat>=2.9.0-r0'",
    }[mutation]
    with pytest.raises(AssertionError, match="require libexpat>=2.10.0-r0"):
        _assert_alpine_admission(text.replace(token, replacement), role)


def test_alpine_admission_requires_upgrade_policy_even_when_floors_are_present():
    with pytest.raises(AssertionError, match="require musl>="):
        _assert_alpine_admission(
            _maintenance_dockerfile().replace("--upgrade", ""), "backend-build"
        )


@pytest.mark.parametrize(
    "text",
    [
        _maintenance_dockerfile(before="RUN uv pip install -r requirements.txt\n"),
        _maintenance_dockerfile(
            install="uv pip install -r requirements.txt && apk add "
            "--upgrade 'musl>=1.2.6-r3' 'musl-utils>=1.2.6-r3' 'zlib>=1.3.2-r1'"
        ),
        _maintenance_dockerfile(stage="unrelated"),
        _maintenance_dockerfile().replace("RUN --mount", "# RUN --mount"),
        _maintenance_dockerfile(
            install="echo \"apk add --upgrade 'musl>=1.2.6-r3' "
            "'musl-utils>=1.2.6-r3' 'zlib>=1.3.2-r1'\""
        ),
        _maintenance_dockerfile().replace("'musl>=1.2.6-r3'", "musl>=1.2.6-r3"),
        _maintenance_dockerfile().replace("\nRUN uv", " || true\nRUN uv"),
        _maintenance_dockerfile().replace("\nRUN uv", "; true\nRUN uv"),
        _maintenance_dockerfile().replace("\nRUN uv", " && true || true\nRUN uv"),
        _maintenance_dockerfile().replace("\nRUN uv", " && true; true\nRUN uv"),
        _maintenance_dockerfile().replace("apk add", "if true; then apk add"),
        _maintenance_dockerfile().replace("apk add", "apk add --simulate"),
        _maintenance_dockerfile().replace("apk add", "apk add -s"),
        _maintenance_dockerfile().replace("apk add", "apk add --root=/tmp/unrelated"),
        _maintenance_dockerfile()
        .replace("apk add", "set -eu | apk add")
        .replace("\nRUN uv", "; true\nRUN uv"),
        _maintenance_dockerfile()
        .replace("apk add", "set -eu; apk add")
        .replace("\nRUN uv", " && true; true\nRUN uv"),
    ],
    ids=[
        "later-run",
        "same-run-after-use",
        "other-stage",
        "comment",
        "echo",
        "redirect",
        "ignored-failure",
        "semicolon-without-errexit",
        "later-failure-recovery",
        "later-semicolon-without-errexit",
        "conditional-install",
        "simulation",
        "short-simulation",
        "alternate-root",
        "errexit-in-other-pipeline-process",
        "and-list-errexit-exception",
    ],
)
def test_alpine_admission_rejects_wrong_order_decoys_and_fail_open_install(text):
    with pytest.raises(AssertionError):
        _assert_alpine_admission(text, "backend-build")


@pytest.mark.parametrize(
    "install",
    [
        "apk add 'zlib>=1.3.2-r1' --upgrade 'musl-utils>=1.2.6-r3' 'musl>=1.2.6-r3' "
        "'libexpat>=2.10.0-r0'",
        'apk add --upgrade "musl>=1.2.6-r4" "musl-utils>=1.2.6-r4" "zlib>=1.3.2-r2" '
        '"libexpat>=2.10.1-r0"',
        "set -eu; apk add --upgrade 'musl>=1.2.7-r0' 'musl-utils>=1.2.7-r0' 'zlib>=1.3.3-r0' "
        "'libexpat>=2.11.0-r0'; true",
    ],
    ids=["reordered", "newer-revisions-double-quotes", "newer-release-errexit"],
)
def test_alpine_admission_accepts_equivalent_layout_and_stronger_fixed_floors(install):
    _assert_alpine_admission(_maintenance_dockerfile(install), "backend-build")


def test_alpine_admission_treats_copied_files_and_cmd_as_metadata_not_native_execution():
    text = _maintenance_dockerfile(before='COPY requirements.txt .\nCMD ["uv", "pip", "install"]\n')
    _assert_alpine_admission(text, "backend-build")


# Independently checked Alpine v3.24 publisher floors for the four outputs in
# CI43634702510's 14 November package inventory. These strengthen source admission;
# they do not claim a CVE reproduction, runtime compatibility or scanner waiver.
ALPINE_NOV14_MAINTENANCE = {
    "frontend": {"libxslt": "1.1.43-r4"},
    "postgres": {"krb5-libs": "1.22.3-r0", "libxslt": "1.1.43-r4"},
    "pgbouncer-build": {"libpq": "18.7-r0", "postgresql18-client": "18.7-r0"},
    "pgbouncer": {"libpq": "18.7-r0", "postgresql18-client": "18.7-r0"},
}
ALPINE_NOV14_PRIOR = {
    "libxslt": "1.1.43-r3",
    "krb5-libs": "1.22.2-r1",
    "libpq": "18.6-r99",
    "postgresql18-client": "18.6-r99",
}
ALPINE_NOV14_TARGETS = [
    (role, package) for role, packages in ALPINE_NOV14_MAINTENANCE.items() for package in packages
]


def _nov14_apk_version(version):
    # PostgreSQL maintenance is x.y-rN; the reviewed libraries use x.y.z-rN.
    # Keep the incumbent parser untouched and scope this extension to these tests.
    match = re.fullmatch(r"(\d+)\.(\d+)(?:\.(\d+))?-r(\d+)", version)
    assert match, f"unsupported reviewed Alpine maintenance version: {version}"
    major, minor, patch, revision = match.groups()
    return int(major), int(minor), int(patch or 0), int(revision)


def _assert_nov14_alpine_admission(text, role, monkeypatch):
    with monkeypatch.context() as scoped:
        scoped.setitem(
            ALPINE_MAINTENANCE,
            role,
            {**ALPINE_MAINTENANCE[role], **ALPINE_NOV14_MAINTENANCE[role]},
        )
        scoped.setattr(sys.modules[__name__], "_stable_apk_version", _nov14_apk_version)
        _assert_alpine_admission(text, role)


def _nov14_maintenance_dockerfile(role):
    stage = audit.BUILD_OUTPUTS[role][1]
    packages = {**ALPINE_MAINTENANCE[role], **ALPINE_NOV14_MAINTENANCE[role]}
    constraints = " ".join(f"'{name}>={floor}'" for name, floor in packages.items())
    text = f"FROM synthetic AS {stage}\nRUN apk add --no-cache --upgrade {constraints}\n"
    if role == "postgres":
        text += "RUN gosu postgres id\n"
    elif role == "pgbouncer-build":
        text += "RUN curl --fail https://example.test/source\n"
    return text


@pytest.mark.parametrize("role", ALPINE_NOV14_MAINTENANCE)
def test_nov14_affected_alpine_stages_require_fixed_packages_before_use(role, monkeypatch):
    relative, _, _ = audit.BUILD_OUTPUTS[role]
    _assert_nov14_alpine_admission((ROOT / relative).read_text(), role, monkeypatch)


@pytest.mark.parametrize(("role", "package"), ALPINE_NOV14_TARGETS)
@pytest.mark.parametrize("mutation", ["missing", "unversioned", "older-maintenance"])
def test_nov14_admission_refuses_missing_or_vulnerable_constraints(
    role, package, mutation, monkeypatch
):
    text = _nov14_maintenance_dockerfile(role)
    _assert_nov14_alpine_admission(text, role, monkeypatch)
    token = f"'{package}>={ALPINE_NOV14_MAINTENANCE[role][package]}'"
    replacement = {
        "missing": "",
        "unversioned": package,
        "older-maintenance": f"'{package}>={ALPINE_NOV14_PRIOR[package]}'",
    }[mutation]
    with pytest.raises(AssertionError, match=re.escape(f"require {package}>=")):
        _assert_nov14_alpine_admission(text.replace(token, replacement), role, monkeypatch)


@pytest.mark.parametrize("role", ALPINE_NOV14_MAINTENANCE)
def test_nov14_admission_refuses_fixed_packages_in_an_unrelated_stage(role, monkeypatch):
    stage = audit.BUILD_OUTPUTS[role][1]
    old = " ".join(f"'{package}>={floor}'" for package, floor in ALPINE_MAINTENANCE[role].items())
    fixed = " ".join(
        f"'{package}>={floor}'" for package, floor in ALPINE_NOV14_MAINTENANCE[role].items()
    )
    text = f"FROM synthetic AS {stage}\nRUN apk add --no-cache --upgrade {old}\n"
    if role == "postgres":
        text += "RUN gosu postgres id\n"
    elif role == "pgbouncer-build":
        text += "RUN curl --fail https://example.test/source\n"
    text += f"FROM synthetic AS unrelated\nRUN apk add --no-cache --upgrade {fixed}\n"
    first_package = next(iter(ALPINE_NOV14_MAINTENANCE[role]))
    with pytest.raises(AssertionError, match=re.escape(f"require {first_package}>=")):
        _assert_nov14_alpine_admission(text, role, monkeypatch)


@pytest.mark.parametrize("role", ["postgres", "pgbouncer-build"])
def test_nov14_admission_refuses_fixed_packages_after_source_or_runtime_use(role, monkeypatch):
    stage = audit.BUILD_OUTPUTS[role][1]
    old = " ".join(f"'{package}>={floor}'" for package, floor in ALPINE_MAINTENANCE[role].items())
    fixed = " ".join(
        f"'{package}>={floor}'" for package, floor in ALPINE_NOV14_MAINTENANCE[role].items()
    )
    use = "gosu postgres id" if role == "postgres" else "curl --fail https://example.test/source"
    text = (
        f"FROM synthetic AS {stage}\nRUN apk add --no-cache --upgrade {old}\n"
        f"RUN {use}\nRUN apk add --no-cache --upgrade {fixed}\n"
    )
    first_package = next(iter(ALPINE_NOV14_MAINTENANCE[role]))
    with pytest.raises(AssertionError, match=re.escape(f"require {first_package}>=")):
        _assert_nov14_alpine_admission(text, role, monkeypatch)


@pytest.mark.parametrize("role", ALPINE_NOV14_MAINTENANCE)
def test_nov14_admission_accepts_later_maintenance_revisions(role, monkeypatch):
    text = _nov14_maintenance_dockerfile(role)
    for package, floor in ALPINE_NOV14_MAINTENANCE[role].items():
        release, revision = floor.rsplit("-r", 1)
        text = text.replace(f"'{package}>={floor}'", f'"{package}>={release}-r{int(revision) + 1}"')
    _assert_nov14_alpine_admission(text, role, monkeypatch)


def _assert_node_build_zlib_admission(text, monkeypatch):
    # The independent publisher receipt authenticates zlib1.4-r0 in Alpine3.24.
    # This is an added build-tooling boundary, not a changed legacy stage policy.
    commands = _stage_shell_commands(text, "build")
    uses = [
        index
        for index, (parts, position) in enumerate(commands)
        if parts[position][0][:1] == ["npm"]
    ]
    assert uses, "Node builder must retain a direct npm first-use boundary"
    prefix = commands[: min(uses) + 1]

    def before_first_npm(candidate_text, stage):
        assert candidate_text == text and stage == "build"
        # Later shell assignments and quoted codegen programs cannot satisfy a
        # before-first-use floor. Their grammar need not enter legacy normalization.
        return prefix

    with monkeypatch.context() as scoped:
        scoped.setitem(ALPINE_MAINTENANCE, "frontend-build", {"zlib": "1.4-r0"})
        scoped.setitem(ALPINE_FIRST_USE, "frontend-build", ("npm",))
        scoped.setattr(sys.modules[__name__], "_stable_apk_version", _nov14_apk_version)
        scoped.setattr(sys.modules[__name__], "_stage_shell_commands", before_first_npm)
        _assert_alpine_admission(text, "frontend-build")


def _node_build_zlib_dockerfile(install="apk add --no-cache --upgrade 'zlib>=1.4-r0'"):
    return (
        "FROM synthetic AS build\n"
        f"RUN --mount=type=secret,id=build_ca {install}\n"
        "RUN npm pack --ignore-scripts npm@12.2.2\n"
    )


def test_node_build_requires_fixed_zlib_before_first_npm_use(monkeypatch):
    relative, _, _ = audit.BUILD_OUTPUTS["frontend-build"]
    _assert_node_build_zlib_admission((ROOT / relative).read_text(), monkeypatch)


@pytest.mark.parametrize("mutation", ["missing", "unversioned", "older-maintenance"])
def test_node_build_zlib_refuses_missing_or_older_constraints(mutation, monkeypatch):
    text = _node_build_zlib_dockerfile()
    _assert_node_build_zlib_admission(text, monkeypatch)
    replacement = {
        "missing": "",
        "unversioned": "zlib",
        "older-maintenance": "'zlib>=1.3.2-r99'",
    }[mutation]
    with pytest.raises(AssertionError, match="require zlib>=1.4-r0"):
        _assert_node_build_zlib_admission(text.replace("'zlib>=1.4-r0'", replacement), monkeypatch)


def test_node_build_zlib_refuses_runtime_only_floor(monkeypatch):
    text = (
        "FROM synthetic AS build\nRUN npm pack --ignore-scripts npm@12.2.2\n"
        "FROM synthetic AS runtime\nRUN apk add --no-cache --upgrade 'zlib>=1.4-r0'\n"
    )
    with pytest.raises(AssertionError, match="require zlib>=1.4-r0"):
        _assert_node_build_zlib_admission(text, monkeypatch)


def test_node_build_zlib_refuses_floor_after_first_npm_use(monkeypatch):
    text = _node_build_zlib_dockerfile().replace(
        "FROM synthetic AS build\n",
        "FROM synthetic AS build\nRUN npm pack --ignore-scripts npm@12.2.2\n",
    )
    with pytest.raises(AssertionError, match="require zlib>=1.4-r0"):
        _assert_node_build_zlib_admission(text, monkeypatch)


def test_node_build_zlib_accepts_later_reviewed_revision(monkeypatch):
    text = _node_build_zlib_dockerfile('apk add --no-cache --upgrade "zlib>=1.4-r1"')
    _assert_node_build_zlib_admission(text, monkeypatch)
