"""Protocol controls for the disposable probe; these never contact Docker.

The separate native-image receipt proves the image. This fake verifies command
boundaries, evidence rejection and ownership-aware cleanup, including uncertain
daemon outcomes that are difficult to induce safely against a real daemon.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import importlib.util
import json
import signal
import stat
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/check_postgres_runtime.py"
PUBLIC_HELPER_PATHS = (
    "/usr/share/licenses/gosu/gosu-buildinfo.txt",
    "/usr/share/licenses/gosu/gosu-source-provenance.txt",
    "/usr/share/licenses/gosu/gosu-go.mod",
    "/usr/share/licenses/gosu/gosu-go.sum",
    "/usr/share/licenses/gosu/golang-x-sys-BSD-3-Clause",
    "/usr/local/bin/gosu",
)


@pytest.fixture
def smoke():
    spec = importlib.util.spec_from_file_location("postgres_runtime_smoke_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeDocker:
    """An allowlisted Docker protocol, not an unconditional-success runner."""

    image_id = "sha256:" + "a" * 64
    cid = "b" * 64

    def __init__(self, module, fault=None):
        self.module = module
        self.fault = fault
        self.calls = []
        self.volume = None
        self.container = None
        self.env = {}
        self.env_path = None
        self.private_modes = None
        self.starts = 0
        self.now = 0.0
        self.removed = []
        self.sql_calls = []
        self.on_command = None
        self.hidden_inspects = 0
        self.metadata_calls = 0

    def clock(self):
        return self.now

    def pause(self, seconds):
        assert seconds == 0.25
        self.now += 30  # Advance only the injected harness clock, with no real sleep.

    @staticmethod
    def labels(args):
        return dict(args[i + 1].split("=", 1) for i, arg in enumerate(args) if arg == "--label")

    def reply(self, args, value="", *, code=0, error=""):
        if not isinstance(value, str):
            value = json.dumps(value)
        return subprocess.CompletedProcess(args, code, value, error)

    def diagnostic(self):
        return "RAW_DOCKER_DIAGNOSTIC " + self.env.get("POSTGRES_PASSWORD", "private-fixture")

    def __call__(self, args, **kwargs):
        assert args[0] == "docker"
        assert set(kwargs) == {"input", "text", "capture_output", "timeout", "check"}
        assert kwargs["text"] is True and kwargs["capture_output"] is True
        assert kwargs["check"] is False and 0 < kwargs["timeout"] <= 20
        self.calls.append(args)
        command = args[1:]
        if self.on_command is not None:
            self.on_command(command)
        if command[:2] == ["image", "inspect"]:
            assert command == [
                "image",
                "inspect",
                "--format",
                self.module.IMAGE_FORMAT,
                "local-pg:trial",
            ]
            if self.fault == "image-unavailable":
                return self.reply(args, code=1, error=self.diagnostic())
            return self.reply(
                args,
                {
                    "Id": self.image_id,
                    "Os": "linux",
                    "Config": {
                        "Entrypoint": ["other-entrypoint"]
                        if self.fault == "image-entrypoint"
                        else ["docker-entrypoint.sh"],
                        "Cmd": ["sh"] if self.fault == "image-command" else ["postgres"],
                        "User": "70" if self.fault == "image-user" else "",
                        "StopSignal": "SIGINT",
                        "Volumes": {self.module.PGDATA: {}},
                        "Env": ["PG_MAJOR=16", "PGDATA=" + self.module.PGDATA],
                    },
                },
            )
        if command[0] in {"container", "volume"} and command[1] == "inspect":
            kind, _, flag, template, name = command
            assert flag == "--format"
            assert template == getattr(self.module, kind.upper() + "_FORMAT")
            value = self.container if kind == "container" else self.volume
            if value is None:
                return self.reply(args, code=1, error=f"Error: No such {kind}: {name}")
            if self.fault == f"uncertain-{kind}-hidden" and self.hidden_inspects == 0:
                self.hidden_inspects += 1
                return self.reply(args, code=1, error=f"Error: No such {kind}: {name}")
            assert name in {value["Name"].lstrip("/"), self.cid}
            value = json.loads(json.dumps(value))
            if kind == "container":
                if self.fault == "foreign-label":
                    value["Labels"][self.module.LABEL] = "another-owner"
                elif self.fault == "malformed-mount":
                    value["Mounts"] = [None]
                elif self.fault == "changed-cid" and self.starts >= 2:
                    value["Id"] = "c" * 64
            elif self.fault == "foreign-volume-label":
                value["Labels"][self.module.LABEL] = "another-owner"
            elif self.fault == "replaced-volume" and self.starts >= 2:
                value["CreatedAt"] = "2026-11-06T01:00:00Z"
            return self.reply(args, value)
        if command[:2] == ["volume", "create"]:
            assert len(command) == 7
            self.volume = {
                "Name": command[-1],
                "Driver": "local",
                "Labels": self.labels(command),
                "CreatedAt": "2026-11-06T00:00:00Z",
            }
            if self.fault in {"uncertain-volume-create", "uncertain-volume-hidden"}:
                raise subprocess.TimeoutExpired(args, kwargs["timeout"], stderr=self.diagnostic())
            return self.reply(args, self.volume["Name"])
        if command[0] == "create":
            assert self.volume is not None
            self.env_path = Path(command[command.index("--env-file") + 1])
            self.private_modes = (
                stat.S_IMODE(self.env_path.parent.stat().st_mode),
                stat.S_IMODE(self.env_path.stat().st_mode),
            )
            self.env = dict(line.split("=", 1) for line in self.env_path.read_text().splitlines())
            name = command[command.index("--name") + 1]
            self.container = {
                "Id": self.cid,
                "Name": "/" + name,
                "Image": self.image_id,
                "Labels": self.labels(command),
                "Mounts": [
                    {
                        "Type": "volume",
                        "Name": self.volume["Name"],
                        "Destination": self.module.PGDATA,
                        "RW": True,
                    }
                ],
                "Running": False,
                "ExitCode": 0,
                "OOMKilled": False,
                "NetworkMode": "none",
                "PortBindings": {},
            }
            if self.fault in {"uncertain-container-create", "uncertain-container-hidden"}:
                raise subprocess.TimeoutExpired(args, kwargs["timeout"], stderr=self.diagnostic())
            if self.fault == "interrupted-create":
                raise self.module.Interrupted()
            if self.fault == "wrong-create-cid":
                return self.reply(args, "c" * 64)
            return self.reply(args, self.cid + "\n")
        if command[0] == "start":
            assert command == ["start", self.cid]
            self.starts += 1
            self.container["Running"] = True
            return self.reply(args, self.cid)
        if command[0] == "stop":
            assert command == ["stop", "--time", "10", self.cid]
            self.container["Running"] = False
            return self.reply(args, self.cid)
        if command[0] == "exec":
            if command == ["exec", self.cid, "sh", "-ec", self.module.HELPER_METADATA_PROBE]:
                self.metadata_calls += 1
                return self.reply(args, self.helper_metadata())
            if command == ["exec", self.cid, "sh", "-ec", self.module.IDENTITY]:
                identity = (
                    "Uid: 70 70 70 70\nGid: 70 70 70 70\nGroups: 70\n"
                    "CapEff: 0000000000000000\nCapPrm: 0000000000000000\n"
                    "PGDATA 70 70 700\nPG_VERSION 16\nCOMM postgres\n"
                )
                if self.fault == "server-uid":
                    identity = identity.replace("Uid: 70 70 70 70", "Uid: 0 0 0 0")
                if self.fault == "server-capabilities":
                    identity = identity.replace(
                        "CapPrm: 0000000000000000", "CapPrm: 0000000000000001"
                    )
                if self.fault == "server-groups":
                    identity = identity.replace("Groups: 70", "Groups: 70 0")
                if self.fault == "pgdata-mode":
                    identity = identity.replace("PGDATA 70 70 700", "PGDATA 70 70 755")
                return self.reply(args, identity)
            assert command[:5] == ["exec", "-i", self.cid, "sh", "-ec"]
            assert len(command) == 6
            shell = command[-1]
            assert "--host=127.0.0.1" in shell and "--set=ON_ERROR_STOP=1" in shell
            assert "--no-password" in shell and "PGCONNECT_TIMEOUT=2" in shell
            source = kwargs["input"]
            self.sql_calls.append((shell, source))
            ordinary = "--username=bidit_smoke_user" in shell
            wrong = "bidit_smoke_deliberately_wrong" in shell
            if wrong:
                assert ordinary and source == "SELECT 1;"
                return self.reply(
                    args,
                    code=0
                    if self.fault == "wrong-password-accepted"
                    or (self.fault == "restart-auth-outcome" and self.starts == 2)
                    else 2,
                    error=self.diagnostic(),
                )
            if source == "SELECT 1;":
                if self.fault == "readiness-timeout":
                    return self.reply(args, code=2, error=self.diagnostic())
                return self.reply(args, "1\n")
            if source == self.module.SERVER_SQL:
                return self.reply(
                    args,
                    {
                        "major": 16,
                        "version": "16.11",
                        "system_identifier": "123456789"
                        if not (self.fault == "restart-system-id" and self.starts == 2)
                        else "999",
                        "password_encryption": "scram-sha-256",
                        "data_directory": self.module.PGDATA,
                    },
                )
            if source == self.module.AUTH_SQL:
                return self.reply(args, self.auth_observation())
            if source == self.module.SETUP_SQL:
                assert not ordinary
                assert "NOSUPERUSER" in source and "NOBYPASSRLS" in source
                return self.reply(args)
            if source == self.module.MARKER_SQL:
                assert ordinary and "numeric(14,2)" in source and "jsonb" in source
                return self.reply(args)
            assert source == self.module.SNAPSHOT_SQL and ordinary
            return self.reply(
                args,
                {
                    "role": "bidit_smoke_user",
                    "superuser": self.fault == "ordinary-superuser",
                    "bypassrls": False,
                    "role_oid": "16385",
                    "table_oid": "16390"
                    if not (self.fault == "restart-table-oid" and self.starts == 2)
                    else "999",
                    "rows": [
                        {
                            "id": 1,
                            "amount": "12.34" if self.fault == "marker" else "-12.34",
                            "payload": {"kind": "owned-scratch", "count": 3},
                            "marker": self.env["BIDIT_SMOKE_MARKER"],
                        }
                    ],
                },
            )
        if command[0] == "rm":
            assert command == ["rm", "--force", self.cid]
            if self.fault == "container-cleanup":
                return self.reply(args, code=1, error=self.diagnostic())
            self.removed.append(("container", self.cid))
            self.container = None
            return self.reply(args)
        if command[:2] == ["volume", "rm"]:
            assert command == ["volume", "rm", self.volume["Name"]]
            if self.fault == "volume-cleanup":
                return self.reply(args, code=1, error=self.diagnostic())
            self.removed.append(("volume", self.volume["Name"]))
            self.volume = None
            return self.reply(args)
        raise AssertionError(f"Unexpected fake-Docker command: {command[0]}")

    def auth_observation(self):
        method = "trust" if self.fault == "wrong-password-accepted" else "scram-sha-256"
        if self.fault == "restart-hba" and self.starts == 2:
            method = "md5"
        return {
            "rules": [
                {"type": "local", "address": None, "method": "trust"},
                {"type": "host", "address": "127.0.0.1", "method": method},
                {"type": "host", "address": "all", "method": "scram-sha-256"},
            ]
        }

    def helper_metadata(self):
        if self.fault == "helper-response-too-large":
            return "X" * 65537
        lines = []
        for index, path in enumerate(PUBLIC_HELPER_PATHS):
            kind = "text" if index < 2 else "file"
            raw = f"public fixture metadata {index}\n".encode()
            status, size = "available", len(raw)
            value = (
                base64.b64encode(raw).decode()
                if kind == "text"
                else hashlib.sha256(raw).hexdigest()
            )
            if self.fault == "helper-unavailable":
                status, size, value = "unavailable", 0, "-"
            elif index == 0:
                if self.fault == "helper-oversized":
                    status, size, value = "oversized", 16385, "-"
                elif self.fault == "helper-unknown-path":
                    path = "/unapproved/path"
                elif self.fault == "helper-invalid-utf8":
                    size, value = 1, base64.b64encode(b"\xff").decode()
                elif self.fault == "helper-invalid-base64":
                    value = "$"
                elif self.fault == "helper-wrong-size":
                    size += 1
                elif self.fault == "helper-private-echo":
                    raw = self.env["POSTGRES_PASSWORD"].encode()
                    size, value = len(raw), base64.b64encode(raw).decode()
            elif index == 5 and self.fault == "helper-invalid-digest":
                value = "g" * 64
            lines.append(f"{kind}\t{path}\t{status}\t{size}\t{value}\n")
        return "".join(lines)


def run_probe(smoke, fault=None):
    fake = FakeDocker(smoke, fault)
    result = smoke.Probe("local-pg:trial", runner=fake, clock=fake.clock, pause=fake.pause).run()
    # Keep failure diagnostics/private material out of the published JSON even
    # when the injected daemon error includes a fixture password.
    serialized = json.dumps(result)
    assert "RAW_DOCKER_DIAGNOSTIC" not in serialized
    assert all(value not in serialized for key, value in fake.env.items() if "PASSWORD" in key)
    assert result["cleanup"]["private_environment_removed"] is True
    if fake.env_path is not None:
        assert not fake.env_path.exists() and not fake.env_path.parent.exists()
    return fake, result


def test_fresh_default_image_restart_and_private_owned_cleanup(smoke):
    fake, result = run_probe(smoke)
    assert result["status"] == "passed" and "failure_phase" not in result
    assert result["image_id"] == fake.image_id
    assert [check["name"] for check in result["checks"]] == [
        "fresh-init-and-ordinary-role",
        "same-container-volume-graceful-restart",
    ]
    assert result["checks"][0]["identity"] == {
        "uid": 70,
        "gid": 70,
        "groups": [70],
        "CapEff": 0,
        "CapPrm": 0,
        "PGDATA_owner": "70:70",
        "PGDATA_mode": "700",
        "PG_VERSION": "16",
    }
    assert result["checks"][0]["wrong_password_accepted"] is False
    assert result["checks"][0]["ordinary_role"] == "bidit_smoke_user"
    assert result["checks"][0]["auth_observation"] == fake.auth_observation()
    assert smoke.PUBLIC_TEXT_FILES + smoke.PUBLIC_HASH_FILES == PUBLIC_HELPER_PATHS
    metadata = result["public_helper_metadata"]
    assert metadata["verification"] == "reported-only-not-helper-clearance"
    assert fake.metadata_calls == 1
    for index, record in enumerate(metadata["files"]):
        raw = f"public fixture metadata {index}\n".encode()
        expected = {
            "path": PUBLIC_HELPER_PATHS[index],
            "status": "available",
            "size_bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
        if index < 2:
            expected["text"] = raw.decode()
        assert record == expected
    assert len(metadata["files"]) == 6
    assert result["checks"][1] == {
        "name": "same-container-volume-graceful-restart",
        "same_system_identifier": True,
        "same_role_and_table_OIDs": True,
        "same_signed_numeric_JSON_marker": True,
    }
    assert fake.starts == 2 and fake.private_modes == (0o700, 0o600)
    create = next(args[1:] for args in fake.calls if args[1] == "create")
    assert create[-1] == fake.image_id and "--pull=never" in create
    assert create[create.index("--network") + 1] == "none"
    assert create[create.index("--mount") + 1] == (
        "type=volume,src=" + result["resources"]["volume_name"] + ",dst=" + smoke.PGDATA
    )
    assert not any(
        arg == option or arg.startswith(option + "=")
        for arg in create
        for option in ("--user", "--entrypoint", "--publish", "-p", "--privileged", "--cap-add")
    )
    assert set(fake.env) == {
        "POSTGRES_USER",
        "POSTGRES_DB",
        "POSTGRES_PASSWORD",
        "BIDIT_SMOKE_ROLE_PASSWORD",
        "BIDIT_SMOKE_MARKER",
    }
    assert "POSTGRES_HOST_AUTH_METHOD" not in fake.env
    assert fake.removed == [("container", fake.cid), ("volume", result["resources"]["volume_name"])]
    assert result["cleanup"]["verified"] is True
    assert fake.container is fake.volume is None


def test_unavailable_image_never_creates_resources(smoke):
    fake, result = run_probe(smoke, "image-unavailable")
    assert result["failure_phase"] == "image-inspection"
    assert len(fake.calls) == 1
    assert result["cleanup"]["container"] == result["cleanup"]["volume"] == "not-created"
    assert fake.env_path is None and fake.removed == []


@pytest.mark.parametrize("fault", ["image-entrypoint", "image-command", "image-user"])
def test_nondefault_image_contract_refuses_before_creation(smoke, fault):
    fake, result = run_probe(smoke, fault)
    assert result["failure_phase"] == "image-contract"
    assert len(fake.calls) == 1 and fake.env_path is None and fake.removed == []


@pytest.mark.parametrize(
    ("fault", "phase"),
    [
        ("server-uid", "server-identity"),
        ("server-capabilities", "server-identity"),
        ("server-groups", "server-identity"),
        ("pgdata-mode", "server-identity"),
        ("ordinary-superuser", "ordinary-role-marker"),
        ("marker", "ordinary-role-marker"),
        ("restart-system-id", "restart-persistence"),
        ("restart-table-oid", "restart-persistence"),
        ("restart-hba", "restart-auth-observation"),
        ("restart-auth-outcome", "restart-auth-observation"),
        ("readiness-timeout", "server-readiness"),
    ],
)
def test_rejects_runtime_evidence_and_reaps_only_owned_resources(smoke, fault, phase):
    fake, result = run_probe(smoke, fault)
    assert result["status"] == "failed" and result["failure_phase"] == phase
    assert result["cleanup"]["verified"] is True
    assert [kind for kind, _ in fake.removed] == ["container", "volume"]
    assert len(fake.calls) < 40


def test_default_loopback_trust_is_observed_without_changing_auth_policy(smoke):
    fake, result = run_probe(smoke, "wrong-password-accepted")
    assert result["status"] == "passed" and fake.starts == 2
    observation = result["checks"][0]
    assert observation["wrong_password_accepted"] is True
    assert observation["auth_observation"] == fake.auth_observation()
    assert observation["ordinary_role"] == "bidit_smoke_user"
    assert len([source for _, source in fake.sql_calls if source == smoke.AUTH_SQL]) == 2
    assert len([shell for shell, _ in fake.sql_calls if "deliberately_wrong" in shell]) == 2
    assert "POSTGRES_HOST_AUTH_METHOD" not in fake.env


@pytest.mark.parametrize("fault", ["helper-unavailable", "helper-oversized"])
def test_public_helper_unavailable_or_oversized_metadata_is_reported_without_clearance(
    smoke, fault
):
    _, result = run_probe(smoke, fault)
    assert result["status"] == "passed"
    metadata = result["public_helper_metadata"]
    assert metadata["verification"] == "reported-only-not-helper-clearance"
    assert [record["path"] for record in metadata["files"]] == list(PUBLIC_HELPER_PATHS)
    if fault == "helper-unavailable":
        assert metadata["files"] == [
            {"path": path, "status": "unavailable", "size_bytes": 0} for path in PUBLIC_HELPER_PATHS
        ]
    else:
        assert metadata["files"][0] == {
            "path": PUBLIC_HELPER_PATHS[0],
            "status": "oversized",
            "size_bytes": 16385,
        }


@pytest.mark.parametrize(
    "fault",
    [
        "helper-unknown-path",
        "helper-invalid-utf8",
        "helper-invalid-base64",
        "helper-wrong-size",
        "helper-invalid-digest",
        "helper-response-too-large",
    ],
)
def test_public_helper_rejects_unapproved_or_invalid_bounded_evidence(smoke, fault):
    _, result = run_probe(smoke, fault)
    assert result["status"] == "failed" and result["failure_phase"] == "public-helper-metadata"
    assert result["cleanup"]["verified"] is True
    assert "public_helper_metadata" not in result


def test_public_helper_cannot_echo_generated_private_credentials_into_json(smoke):
    _, result = run_probe(smoke, "helper-private-echo")
    assert result["status"] == "failed" and result["failure_phase"] == "private-data-in-evidence"
    assert result["cleanup"] == {"verified": True, "private_environment_removed": True}


@pytest.mark.parametrize(
    ("fault", "phase", "removed"),
    [
        ("uncertain-volume-create", "volume-creation", ["volume"]),
        ("uncertain-container-create", "container-creation", ["container", "volume"]),
        ("interrupted-create", "interrupted", ["container", "volume"]),
        ("wrong-create-cid", "container-ownership", ["container", "volume"]),
    ],
)
def test_uncertain_creation_recovers_exact_names_and_owner_labels(smoke, fault, phase, removed):
    fake, result = run_probe(smoke, fault)
    assert result["failure_phase"] == phase and result["status"] == "failed"
    assert result["cleanup"]["verified"] is True
    assert [kind for kind, _ in fake.removed] == removed
    assert fake.starts == 0
    assert not any(args[1:3] in (["container", "ls"], ["volume", "ls"]) for args in fake.calls)
    assert all("prune" not in args for args in fake.calls)


@pytest.mark.parametrize("kind", ["container", "volume"])
def test_uncertain_create_first_absence_does_not_claim_confirmed_cleanup(smoke, kind):
    fake, result = run_probe(smoke, f"uncertain-{kind}-hidden")
    assert result["status"] == "failed" and result["failure_phase"] == f"{kind}-creation"
    assert result["cleanup"]["verified"] is False
    assert result["cleanup"][kind] == "creation-unsettled-not-observed"
    assert fake.hidden_inspects == 1 and fake.removed == []
    # First lookup cannot prove that the timed-out daemon request has finished.
    # The fake retains the pending resource, which would be visible on a later
    # lookup. In particular, never delete its mounted volume on an early 404.
    assert fake.volume is not None
    assert (fake.container is not None) == (kind == "container")


@pytest.mark.parametrize("fault", ["foreign-label", "changed-cid", "malformed-mount"])
def test_unverified_container_identity_never_deletes_container_or_volume(smoke, fault):
    fake, result = run_probe(smoke, fault)
    assert result["status"] == "failed"
    assert result["cleanup"]["verified"] is False
    assert fake.removed == []
    assert fake.container is not None and fake.volume is not None


@pytest.mark.parametrize("fault", ["foreign-volume-label", "replaced-volume"])
def test_unverified_volume_provenance_is_never_deleted(smoke, fault):
    fake, result = run_probe(smoke, fault)
    assert result["status"] == "failed" and result["cleanup"]["verified"] is False
    assert fake.volume is not None
    assert all(kind != "volume" for kind, _ in fake.removed)
    assert fake.container is None


@pytest.mark.parametrize("fault", ["container-cleanup", "volume-cleanup"])
def test_cleanup_failure_overrides_success_and_does_not_leak_diagnostics(smoke, fault):
    fake, result = run_probe(smoke, fault)
    assert len(result["checks"]) == 2  # Runtime checks passed; cleanup still governs success.
    assert result["status"] == "failed" and result["failure_phase"] == "cleanup"
    assert result["cleanup"]["verified"] is False
    if fault == "container-cleanup":
        assert fake.removed == [] and fake.volume is not None
    else:
        assert fake.removed == [("container", fake.cid)]


@pytest.mark.parametrize(
    "argv", [[], ["a", "b"], ["--help"], ["--volume=existing"], ["image\nref"]]
)
def test_cli_rejects_unsupported_arguments_without_daemon_access(smoke, monkeypatch, capsys, argv):
    def unexpected_probe(_image):
        pytest.fail("invalid arguments must not instantiate a Docker probe")

    monkeypatch.setattr(smoke, "Probe", unexpected_probe)
    assert smoke.main(argv) == 2
    output = capsys.readouterr()
    assert output.err == ""
    assert json.loads(output.out)["failure_phase"] == "arguments"


@pytest.mark.parametrize("fault", [None, "marker"])
def test_cli_emits_one_sanitized_json_and_restores_signal_handlers(
    smoke, monkeypatch, capsys, fault
):
    probe_class = smoke.Probe
    fake = FakeDocker(smoke, fault)
    monkeypatch.setattr(
        smoke,
        "Probe",
        lambda image: probe_class(image, runner=fake, clock=fake.clock, pause=fake.pause),
    )
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    assert smoke.main(["local-pg:trial"]) == (0 if fault is None else 1)
    assert previous == {sig: signal.getsignal(sig) for sig in previous}
    output = capsys.readouterr()
    assert output.err == "" and len(output.out.splitlines()) == 1
    result = json.loads(output.out)
    assert result["status"] == ("passed" if fault is None else "failed")
    assert "RAW_DOCKER_DIAGNOSTIC" not in output.out
    assert all(value not in output.out for key, value in fake.env.items() if "PASSWORD" in key)
    assert result["cleanup"]["private_environment_removed"] is True
    assert not fake.env_path.parent.exists()


@pytest.mark.parametrize(
    ("phase", "fault", "first_signal"),
    [
        ("work", None, signal.SIGTERM),
        ("container", None, signal.SIGTERM),
        ("volume", None, signal.SIGTERM),
        ("private", None, signal.SIGTERM),
        ("work", None, signal.SIGINT),
        ("private", None, signal.SIGINT),
        ("container", "marker", signal.SIGTERM),
        ("volume", "marker", signal.SIGTERM),
        ("private", "marker", signal.SIGTERM),
    ],
)
@pytest.mark.parametrize("deliveries", [1, 3])
def test_first_signal_during_work_or_each_cleanup_phase_finishes_owned_teardown(
    smoke, monkeypatch, capsys, phase, fault, first_signal, deliveries
):
    probe_class = smoke.Probe
    fake = FakeDocker(smoke, fault)
    delivered = 0

    def deliver():
        nonlocal delivered
        delivered += 1
        signum = first_signal if delivered == 1 else signal.SIGINT
        handler = signal.getsignal(signum)
        if callable(handler):
            handler(signum, None)
        else:
            assert handler == signal.SIG_IGN

    def interrupt_at_command(command):
        wanted = {"work": "start", "container": "rm", "volume": "volume"}
        matches = command[0] == wanted.get(phase) and (
            phase != "volume" or command[:2] == ["volume", "rm"]
        )
        # A work signal raises into the probe; later signals reach its finally
        # boundary, where they must not interrupt ownership-aware cleanup.
        repeat_after_work = phase == "work" and delivered and command[0] == "rm"
        if matches or repeat_after_work:
            while delivered < deliveries:
                deliver()

    fake.on_command = interrupt_at_command
    real_temporary_directory = smoke.tempfile.TemporaryDirectory
    recover_private = []

    def temporary_directory(*args, **kwargs):
        private = real_temporary_directory(*args, **kwargs)
        original_cleanup = private.cleanup
        recover_private.append(original_cleanup)

        def cleanup():
            if phase == "private":
                while delivered < deliveries:
                    deliver()
            original_cleanup()

        private.cleanup = cleanup
        return private

    monkeypatch.setattr(smoke.tempfile, "TemporaryDirectory", temporary_directory)
    monkeypatch.setattr(
        smoke,
        "Probe",
        lambda image: probe_class(image, runner=fake, clock=fake.clock, pause=fake.pause),
    )
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        assert smoke.main(["local-pg:trial"]) == 1
        output = capsys.readouterr()
        assert output.err == "" and len(output.out.splitlines()) == 1
        result = json.loads(output.out)
        assert result["status"] == "failed"
        assert result["failure_phase"] == ("ordinary-role-marker" if fault else "interrupted")
        assert result["interruption_observed"] is True
        assert result["cleanup"]["verified"] is True
        assert result["cleanup"]["private_environment_removed"] is True
        assert fake.container is fake.volume is None
        assert delivered == deliveries
        assert not fake.env_path.exists() and not fake.env_path.parent.exists()
        assert "RAW_DOCKER_DIAGNOSTIC" not in output.out
        assert all(value not in output.out for key, value in fake.env.items() if "PASSWORD" in key)
    finally:
        assert previous == {sig: signal.getsignal(sig) for sig in previous}
        # The pre-fix failure must not leave this test's actual private directory
        # behind. This recovery is separate from the probe's required cleanup.
        for cleanup in recover_private:
            cleanup()


def test_first_signal_at_cleanup_entry_owns_private_cleanup_before_reporting_success(
    smoke, monkeypatch, capsys
):
    probe_class = smoke.Probe
    fake = FakeDocker(smoke)
    run_source = ast.parse(SCRIPT.read_text())
    probe_node = next(
        node for node in run_source.body if isinstance(node, ast.ClassDef) and node.name == "Probe"
    )
    run_node = next(
        node for node in probe_node.body if isinstance(node, ast.FunctionDef) and node.name == "run"
    )
    cleanup_try = next(
        node for node in run_node.body if isinstance(node, ast.Try) and node.finalbody
    )
    cleanup_entry_line = cleanup_try.finalbody[0].lineno
    real_temporary_directory = smoke.tempfile.TemporaryDirectory
    private_owners = []
    explicit_cleanup = []

    def temporary_directory(*args, **kwargs):
        private = real_temporary_directory(*args, **kwargs)
        original_cleanup = private.cleanup
        private_owners.append((private, original_cleanup))

        def cleanup():
            explicit_cleanup.append(private.name)
            original_cleanup()

        private.cleanup = cleanup
        return private

    monkeypatch.setattr(smoke.tempfile, "TemporaryDirectory", temporary_directory)
    monkeypatch.setattr(
        smoke,
        "Probe",
        lambda image: probe_class(image, runner=fake, clock=fake.clock, pause=fake.pause),
    )
    delivered = False

    def trace(frame, event, _argument):
        nonlocal delivered
        if (
            frame.f_code is probe_class.run.__code__
            and event == "line"
            and frame.f_lineno == cleanup_entry_line
            and not delivered
        ):
            delivered = True
            handler = signal.getsignal(signal.SIGTERM)
            assert callable(handler)
            handler(signal.SIGTERM, None)
        return trace

    previous_trace = sys.gettrace()
    previous_signals = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        sys.settrace(trace)
        assert smoke.main(["local-pg:trial"]) == 1
        sys.settrace(previous_trace)
        output = capsys.readouterr()
        assert output.err == "" and len(output.out.splitlines()) == 1
        result = json.loads(output.out)
        assert delivered and result["failure_phase"] == "interrupted"
        assert result["interruption_observed"] is True
        assert result["cleanup"]["verified"] is True
        # Retain the owned object as instrumentation: Python garbage collection
        # must not substitute for the probe's verified private-file cleanup.
        assert explicit_cleanup == [private_owners[0][0].name]
        assert result["cleanup"]["private_environment_removed"] is True
        assert not fake.env_path.parent.exists()
        assert fake.container is fake.volume is None
    finally:
        sys.settrace(previous_trace)
        assert previous_signals == {sig: signal.getsignal(sig) for sig in previous_signals}
        for _, cleanup in private_owners:
            cleanup()
