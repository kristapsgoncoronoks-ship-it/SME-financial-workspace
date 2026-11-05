"""The Kubernetes reference manifests boot the application as committed
(ARCH-002 / OPS-011, audit 2026-09-05).

The audit found a pod started from `deploy/k8s/` refused to boot: the Secret
example carried two keys while the production validator demands more, and the
storage default (`local`) pointed at a directory under a read-only root
filesystem. Nothing in CI ever rendered the manifests. These tests read every
manifest and prove, at the configuration level:

- every `secretKeyRef` any manifest reads exists in the Secret example;
- the ConfigMap + Secret pair, given to the application's own `Settings`, passes
  the production validator (the boot-time refusal) and raises none of its
  warnings;
- a backend with a read-only root filesystem does not store documents in it;
- every image reference follows one placeholder convention, so the documented
  `sed` cutover reaches all of them;
- every Deployment has a liveness probe (the worker's landed in R6).

`kubectl` is not on the test machine; what a live cluster adds (RBAC, storage
classes, the ingress class) is outside this proof and stays in the runbook.
"""

from __future__ import annotations

import os
import pathlib
from unittest import mock

import pytest
import yaml

from app.core.config import Settings

REPO = pathlib.Path(__file__).resolve().parents[2]
K8S = REPO / "deploy" / "k8s"


def _docs() -> list[tuple[str, dict]]:
    out = []
    for path in sorted(K8S.glob("*.yaml")):
        for doc in yaml.safe_load_all(path.read_text()):
            if doc:
                out.append((path.name, doc))
    return out


def _of_kind(kind: str) -> list[tuple[str, dict]]:
    return [(n, d) for n, d in _docs() if d.get("kind") == kind]


def _config_map() -> dict[str, str]:
    ((name, cm),) = [
        (n, d) for n, d in _of_kind("ConfigMap") if d["metadata"]["name"] == "invoiceiq-config"
    ]
    return dict(cm["data"])


def _secret_example() -> dict[str, str]:
    ((name, sec),) = [
        (n, d) for n, d in _of_kind("Secret") if d["metadata"]["name"] == "invoiceiq-secrets"
    ]
    return dict(sec["stringData"])


def _containers():
    for name, doc in _docs():
        spec = doc.get("spec", {})
        tpl = spec.get("template") or spec.get("jobTemplate", {}).get("spec", {}).get("template")
        if not tpl:
            continue
        for c in tpl["spec"].get("containers", []):
            yield name, doc, c


def test_every_secret_key_a_manifest_reads_exists_in_the_secret_example():
    provided = set(_secret_example())
    missing = []
    for name, _doc, c in _containers():
        for env in c.get("env") or []:
            ref = (env.get("valueFrom") or {}).get("secretKeyRef")
            if ref and ref["name"] == "invoiceiq-secrets" and ref["key"] not in provided:
                missing.append(f"{name}: {ref['key']}")
    assert missing == [], missing


# Keys the pair carries for readers OTHER than Settings: the image's shell CMD
# expands WEB_CONCURRENCY into `uvicorn --workers` (Dockerfile), the AWS SDK
# reads the two AWS_* names natively, pgbouncer reads DB_USER/DB_PASSWORD.
_NOT_SETTINGS = {
    "web_concurrency",
    "aws_access_key_id",
    "aws_secret_access_key",
    "db_user",
    "db_password",
}


def test_the_configmap_and_secret_pass_the_production_validator_and_its_warnings():
    """The same validator that refuses a misconfigured pod, fed the committed
    example, in an EMPTY process environment (nothing from the test machine or
    CI may leak in). A placeholder value is fine — the validator checks shape
    (not the dev default, not SQLite, no '*' origin, a secret present), which
    is what the audit's pod failed on. Every key must also be one the
    application (or another container) reads: Settings ignores unknown
    variables, so a misspelt key in the ConfigMap would otherwise pass here
    while the pod silently ran on the default."""
    env = {**_config_map(), **_secret_example()}
    kwargs = {k.lower(): v for k, v in env.items()}
    unknown = set(kwargs) - set(Settings.model_fields) - _NOT_SETTINGS
    assert unknown == set(), f"keys nothing reads: {sorted(unknown)}"
    with mock.patch.dict(os.environ, {}, clear=True):
        settings = Settings(_env_file=None, **kwargs)  # type: ignore[call-arg]
    assert settings.environment == "production"
    assert settings.production_warnings() == [], settings.production_warnings()
    assert settings.storage_backend == "s3"


def test_a_read_only_backend_does_not_store_documents_on_its_root_filesystem():
    """A guard, not a proof of the committed state: with `s3` in the ConfigMap the
    body is skipped on purpose. It fires when someone flips the ConfigMap back to
    `local` without mounting the path (the audit's pod) — the seeded flip turns
    it red together with the validator test."""
    cm = _config_map()
    for name, doc, c in _containers():
        if doc["metadata"]["name"] not in ("backend", "worker", "worker-extract", "worker-general"):
            continue
        ro = (c.get("securityContext") or {}).get("readOnlyRootFilesystem")
        if ro and cm.get("STORAGE_BACKEND", "local") == "local":
            mounts = {m["mountPath"] for m in c.get("volumeMounts") or []}
            local_path = cm.get("STORAGE_LOCAL_PATH", "/app/var/storage")
            assert any(local_path.startswith(m) for m in mounts), (
                f"{name}: read-only root filesystem with local storage at {local_path} and no mount"
            )


def test_every_image_follows_the_one_placeholder_convention():
    """`ghcr.io/OWNER/REPO/<name>:VERSION` everywhere, so the runbook's single
    `sed` (OWNER/REPO/VERSION → your registry path and tag) reaches every pod,
    the migrate Job and patched optional PgBouncer artifact included. External
    service images must use explicit versions rather than latest."""
    seen = set()
    for name, _doc, c in _containers():
        image = c["image"]
        if image.startswith("ghcr.io/"):
            assert image.startswith("ghcr.io/OWNER/REPO/") and image.endswith(":VERSION"), (
                f"{name}: {image}"
            )
            seen.add(image.rsplit("/", 1)[-1].split(":")[0])
        else:
            assert ":" in image and not image.endswith(":latest"), f"{name}: unpinned {image}"
    assert seen == {"backend", "frontend", "pgbouncer"}


def test_every_deployment_has_a_liveness_probe():
    for name, doc in _of_kind("Deployment"):
        for c in doc["spec"]["template"]["spec"]["containers"]:
            assert c.get("livenessProbe"), f"{name}: {doc['metadata']['name']} has no livenessProbe"


def test_the_migrate_job_runs_the_same_image_as_the_backend_and_only_migrates():
    ((_name, job),) = _of_kind("Job")
    c = job["spec"]["template"]["spec"]["containers"][0]
    assert c["command"] == ["alembic", "upgrade", "head"]
    assert c["image"] == "ghcr.io/OWNER/REPO/backend:VERSION"
    assert job["spec"]["template"]["spec"]["restartPolicy"] == "Never"


def test_migration_and_frontend_enforce_the_qualified_runtime_boundaries():
    """Native fixture proofs cover these roles and writable paths; not a cluster certificate."""
    for name, doc, container in _containers():
        workload = doc["metadata"]["name"]
        if workload not in {"invoiceiq-migrate", "frontend"}:
            continue
        pod = doc["spec"]["template"]["spec"]
        role = pod["securityContext"]
        expected_id = 101 if workload == "frontend" else 10001
        assert role["runAsNonRoot"] is True, name
        assert role["runAsUser"] == role["runAsGroup"] == expected_id, name
        assert role["seccompProfile"] == {"type": "RuntimeDefault"}, name
        boundary = container["securityContext"]
        assert boundary["allowPrivilegeEscalation"] is False, name
        assert boundary["readOnlyRootFilesystem"] is True, name
        assert boundary["capabilities"] == {"drop": ["ALL"]}, name
        paths = {mount["mountPath"] for mount in container["volumeMounts"]}
        assert paths == ({"/tmp", "/var/cache/nginx"} if workload == "frontend" else {"/tmp"})
        assert all("emptyDir" in volume for volume in pod["volumes"]), name
        if workload == "frontend":
            assert role["fsGroup"] == 101  # group-write access to fresh node-owned volumes


def test_python_pods_declare_the_existing_image_group():
    for name, doc, _container in _containers():
        if doc["metadata"]["name"] not in {
            "invoiceiq-migrate",
            "backend",
            "worker",
            "worker-extract",
            "worker-general",
        }:
            continue
        role = doc["spec"]["template"]["spec"]["securityContext"]
        assert role["runAsUser"] == role["runAsGroup"] == 10001, name


@pytest.mark.parametrize("key", sorted(_secret_example()))
def test_the_runbook_command_creates_every_required_secret_key(key):
    """docs/DEPLOYMENT.md's `kubectl create secret` command must name every key
    the example carries — parametrised over the example itself, so a key added
    to the manifest without the runbook line fails here — or a reader following
    the runbook boots a pod the validator refuses."""
    text = (REPO / "docs" / "DEPLOYMENT.md").read_text()
    assert f"--from-literal={key}=" in text, f"DEPLOYMENT.md's kubectl command lacks {key}"
