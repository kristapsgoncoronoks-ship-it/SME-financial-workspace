#!/usr/bin/env python3
"""Exercise only a locally present PG16 image with disposable owned resources.

No pull/build, existing-volume option, host ports, entrypoint/user/auth override,
or raw Docker/SQL diagnostics. Docker startup capabilities stay at their normal
image/deployment defaults; the actual postgres process must have dropped them.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

LABEL = "io.bidit.postgres-runtime-smoke.owner"
KIND = "io.bidit.postgres-runtime-smoke.kind"
PGDATA = "/var/lib/postgresql/data"
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
CONTAINER_ID = re.compile(r"[0-9a-f]{64}\Z")
IMAGE_FORMAT = "{{json .}}"
CONTAINER_FORMAT = (
    '{"Id":{{json .Id}},"Name":{{json .Name}},"Image":{{json .Image}},'
    '"Labels":{{json .Config.Labels}},"Mounts":{{json .Mounts}},'
    '"Running":{{json .State.Running}},"ExitCode":{{json .State.ExitCode}},'
    '"OOMKilled":{{json .State.OOMKilled}},'
    '"NetworkMode":{{json .HostConfig.NetworkMode}},'
    '"PortBindings":{{json .HostConfig.PortBindings}}}'
)
VOLUME_FORMAT = (
    '{"Name":{{json .Name}},"Labels":{{json .Labels}},'
    '"CreatedAt":{{json .CreatedAt}},"Driver":{{json .Driver}}}'
)
IDENTITY = r"""awk '/^(Uid|Gid|Groups|CapEff|CapPrm):/ { print }' /proc/1/status
printf 'PGDATA '; stat -c '%u %g %a' "$PGDATA"
printf 'PG_VERSION '; cat "$PGDATA/PG_VERSION"
printf 'COMM '; cat /proc/1/comm
"""
SERVER_SQL = """SELECT json_build_object(
 'major',current_setting('server_version_num')::int/10000,
 'version',current_setting('server_version'),
 'system_identifier',system_identifier::text,
 'password_encryption',current_setting('password_encryption'),
 'data_directory',current_setting('data_directory')) FROM pg_control_system();
"""
AUTH_SQL = """SELECT json_build_object('rules',coalesce(json_agg(
 json_build_object('type',type,'address',address,'method',auth_method)
 ORDER BY line_number),'[]'::json)) FROM pg_hba_file_rules WHERE error IS NULL;
"""
SETUP_SQL = r"""\getenv role_password BIDIT_SMOKE_ROLE_PASSWORD
CREATE ROLE bidit_smoke_user LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
 NOREPLICATION NOBYPASSRLS PASSWORD :'role_password';
CREATE SCHEMA runtime_smoke AUTHORIZATION bidit_smoke_user;
"""
MARKER_SQL = r"""\getenv marker BIDIT_SMOKE_MARKER
CREATE TABLE runtime_smoke.marker
 (id integer PRIMARY KEY, amount numeric(14,2) NOT NULL, payload jsonb NOT NULL, value text NOT NULL);
INSERT INTO runtime_smoke.marker VALUES (1,-12.34,'{"kind":"owned-scratch","count":3}'::jsonb,:'marker');
"""
SNAPSHOT_SQL = """SELECT json_build_object(
 'role',current_user,'superuser',rolsuper,'bypassrls',rolbypassrls,'role_oid',oid::text,
 'table_oid','runtime_smoke.marker'::regclass::oid::text,
 'rows',(SELECT json_agg(json_build_object('id',id,'amount',amount::text,'payload',payload,'marker',value) ORDER BY id)
 FROM runtime_smoke.marker)) FROM pg_roles WHERE rolname=current_user;
"""
PUBLIC_TEXT_FILES = (
    "/usr/share/licenses/gosu/gosu-buildinfo.txt",
    "/usr/share/licenses/gosu/gosu-source-provenance.txt",
)
PUBLIC_HASH_FILES = (
    "/usr/share/licenses/gosu/gosu-go.mod",
    "/usr/share/licenses/gosu/gosu-go.sum",
    "/usr/share/licenses/gosu/golang-x-sys-BSD-3-Clause",
    "/usr/local/bin/gosu",
)
HELPER_METADATA_PROBE = r"""set -eu
public_text() {
    path="$1"
    if [ ! -f "$path" ] || [ -L "$path" ] || [ ! -r "$path" ]; then
        printf 'text\t%s\tunavailable\t0\t-\n' "$path"
        return
    fi
    size=$(wc -c < "$path")
    if [ "$size" -gt 16384 ]; then
        printf 'text\t%s\toversized\t%s\t-\n' "$path" "$size"
        return
    fi
    printf 'text\t%s\tavailable\t%s\t' "$path" "$size"
    head -c 16385 "$path" | base64 | tr -d '\n'
    printf '\n'
}
public_hash() {
    path="$1"
    if [ ! -f "$path" ] || [ -L "$path" ] || [ ! -r "$path" ]; then
        printf 'file\t%s\tunavailable\t0\t-\n' "$path"
        return
    fi
    size=$(wc -c < "$path")
    digest=$(sha256sum "$path")
    printf 'file\t%s\tavailable\t%s\t%s\n' "$path" "$size" "${digest%% *}"
}
""" + "\n".join(
    ["public_text " + path for path in PUBLIC_TEXT_FILES]
    + ["public_hash " + path for path in PUBLIC_HASH_FILES]
)


class SmokeFailure(Exception):
    """Phase only: subprocess output/arguments may contain private fixture data."""


class Interrupted(Exception):
    pass


class Probe:
    def __init__(self, image, *, runner=None, clock=None, pause=None):
        self.runner = runner or subprocess.run
        self.clock = clock or time.monotonic
        self.pause = pause or time.sleep
        self.reference = image
        self.token = uuid.uuid4().hex
        self.container = "bidit-pg-runtime-" + self.token
        self.volume = self.container + "-data"
        self.image_id = None
        self.cid = None
        self.volume_created_at = None
        self.container_attempted = False
        self.volume_attempted = False
        self.container_acknowledged = False
        self.volume_acknowledged = False
        self.cleaning = False
        self.private_directory = None
        self.private_values = []
        self.result = {
            "status": "failed",
            "scope": "functional-runtime-smoke; helper metadata is reported, not security clearance",
            "checks": [],
            "image_id": None,
            "resources": {"container_name": self.container, "volume_name": self.volume},
            "cleanup": {},
        }

    def docker(self, args, phase, *, source=None, timeout=20, check=True):
        try:
            reply = self.runner(
                ["docker", *args],
                input=source,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except (Interrupted, KeyboardInterrupt):
            raise
        except Exception:  # noqa: BLE001 - never expose Docker diagnostics or private input.
            raise SmokeFailure(phase) from None
        if check and reply.returncode:
            raise SmokeFailure(phase)
        return reply

    def inspect(self, kind, name, *, missing=False):
        template = CONTAINER_FORMAT if kind == "container" else VOLUME_FORMAT
        reply = self.docker(
            [kind, "inspect", "--format", template, name],
            kind + "-inspection",
            check=False,
        )
        if reply.returncode:
            # Unknown daemon failure is not authoritative absence.
            if (
                missing
                and "no such " + kind in reply.stderr.lower()
                and name in reply.stderr
            ):
                return None
            raise SmokeFailure(kind + "-inspection")
        try:
            value = json.loads(reply.stdout)
        except (ValueError, TypeError):
            raise SmokeFailure(kind + "-inspection") from None
        if not isinstance(value, dict):
            raise SmokeFailure(kind + "-inspection")
        return value

    def owned_container(self, value):
        mounts = value.get("Mounts")
        labels = value.get("Labels")
        return (
            isinstance(value.get("Id"), str)
            and CONTAINER_ID.fullmatch(value["Id"]) is not None
            and value.get("Name") == "/" + self.container
            and value.get("Image") == self.image_id
            and isinstance(labels, dict)
            and labels.get(LABEL) == self.token
            and labels.get(KIND) == "container"
            and value.get("NetworkMode") == "none"
            and value.get("PortBindings") in (None, {})
            and isinstance(mounts, list)
            and len(mounts) == 1
            and isinstance(mounts[0], dict)
            and mounts[0].get("Type") == "volume"
            and mounts[0].get("Name") == self.volume
            and mounts[0].get("Destination") == PGDATA
            and mounts[0].get("RW") is True
            and (self.cid is None or value.get("Id") == self.cid)
        )

    def owned_volume(self, value):
        return (
            value.get("Name") == self.volume
            and value.get("Driver") == "local"
            and value.get("Labels") == {LABEL: self.token, KIND: "volume"}
            and isinstance(value.get("CreatedAt"), str)
            and bool(value["CreatedAt"])
            and (
                self.volume_created_at is None
                or value["CreatedAt"] == self.volume_created_at
            )
        )

    def sql(self, source, *, ordinary=False, wrong=False, check=True):
        password = "BIDIT_SMOKE_ROLE_PASSWORD" if ordinary else "POSTGRES_PASSWORD"
        role = "bidit_smoke_user" if ordinary else "bidit_fixture_admin"
        expression = (
            "bidit_smoke_deliberately_wrong" if wrong else '"$' + password + '"'
        )
        command = (
            f"PGPASSWORD={expression} PGCONNECT_TIMEOUT=2 exec psql -X --no-password "
            f"--host=127.0.0.1 --username={role} --dbname=bidit_runtime "
            "--set=ON_ERROR_STOP=1 --tuples-only --no-align --quiet"
        )
        return self.docker(
            ["exec", "-i", self.cid, "sh", "-ec", command],
            "sql",
            source=source,
            timeout=10,
            check=check,
        )

    def ready(self):
        deadline = (
            self.clock() + 75
        )  # Harness budget, not a PostgreSQL/application default.
        while self.clock() < deadline:
            reply = self.sql("SELECT 1;", check=False)
            if reply.returncode == 0 and reply.stdout.strip() == "1":
                return
            value = self.inspect("container", self.cid)
            if not self.owned_container(value) or value.get("Running") is not True:
                raise SmokeFailure("server-readiness")
            self.pause(0.25)
        raise SmokeFailure("server-readiness")

    def identity(self):
        reply = self.docker(
            ["exec", self.cid, "sh", "-ec", IDENTITY], "server-identity"
        )
        try:
            parts = {
                line.split()[0].rstrip(":"): line.split()[1:]
                for line in reply.stdout.splitlines()
            }
            valid = (
                parts["Uid"] == parts["Gid"] == ["70"] * 4
                and set(parts["Groups"]) == {"70"}
                and int(parts["CapEff"][0], 16) == int(parts["CapPrm"][0], 16) == 0
                and parts["PGDATA"] == ["70", "70", "700"]
                and parts["PG_VERSION"] == ["16"]
                and parts["COMM"] == ["postgres"]
            )
        except (KeyError, ValueError, IndexError):
            valid = False
        if not valid:
            raise SmokeFailure("server-identity")
        return {
            "uid": 70,
            "gid": 70,
            "groups": [70],
            "CapEff": 0,
            "CapPrm": 0,
            "PGDATA_owner": "70:70",
            "PGDATA_mode": "700",
            "PG_VERSION": "16",
        }

    def sql_json(self, statement, *, ordinary=False):
        try:
            value = json.loads(self.sql(statement, ordinary=ordinary).stdout)
        except (ValueError, TypeError):
            raise SmokeFailure("sql-evidence") from None
        if not isinstance(value, dict):
            raise SmokeFailure("sql-evidence")
        return value

    def helper_metadata(self):
        reply = self.docker(
            ["exec", self.cid, "sh", "-ec", HELPER_METADATA_PROBE],
            "public-helper-metadata",
        )
        if len(reply.stdout.encode()) > 65536:
            raise SmokeFailure("public-helper-metadata")
        records = {}
        expected = set(PUBLIC_TEXT_FILES + PUBLIC_HASH_FILES)
        try:
            for line in reply.stdout.splitlines():
                kind, path, status, size, value = line.split("\t")
                if (
                    path not in expected
                    or path in records
                    or kind != ("text" if path in PUBLIC_TEXT_FILES else "file")
                ):
                    raise ValueError()
                count = int(size)
                if count < 0 or status not in {"available", "unavailable", "oversized"}:
                    raise ValueError()
                record = {"path": path, "status": status, "size_bytes": count}
                if status == "available":
                    if kind == "text":
                        raw = base64.b64decode(value, validate=True)
                        if count > 16384 or len(raw) != count:
                            raise ValueError()
                        record["text"] = raw.decode("utf-8")
                        record["sha256"] = hashlib.sha256(raw).hexdigest()
                    else:
                        if not re.fullmatch(r"[0-9a-f]{64}", value):
                            raise ValueError()
                        record["sha256"] = value
                elif (
                    value != "-"
                    or (status == "unavailable" and count != 0)
                    or (status == "oversized" and (kind != "text" or count <= 16384))
                ):
                    raise ValueError()
                records[path] = record
            if set(records) != expected:
                raise ValueError()
        except (ValueError, TypeError, UnicodeError):
            raise SmokeFailure("public-helper-metadata") from None
        return {
            "files": [records[path] for path in PUBLIC_TEXT_FILES + PUBLIC_HASH_FILES],
            "verification": "reported-only-not-helper-clearance",
        }

    def cleanup(self):
        # Recover uncertain creation only by the exact planned name, then verify
        # identity/owner before deleting by CID. Never list/prune/rm by a pattern.
        container_safe = not self.container_attempted
        if self.container_attempted:
            try:
                value = self.inspect("container", self.container, missing=True)
                if value is None:
                    self.result["cleanup"]["container"] = (
                        "absent"
                        if self.container_acknowledged
                        else "creation-unsettled-not-observed"
                    )
                    container_safe = self.container_acknowledged
                elif self.owned_container(value):
                    self.cid = value["Id"]
                    self.docker(["rm", "--force", self.cid], "container-cleanup")
                    if self.inspect("container", self.cid, missing=True) is not None:
                        raise SmokeFailure("container-cleanup")
                    self.result["cleanup"]["container"] = "removed-owned-CID"
                    container_safe = True
                else:
                    self.result["cleanup"]["container"] = "refused-unverified-owner"
            except SmokeFailure:
                self.result["cleanup"]["container"] = "unverified-or-failed"
        else:
            self.result["cleanup"]["container"] = "not-created"
        if self.volume_attempted:
            try:
                value = self.inspect("volume", self.volume, missing=True)
                if value is None:
                    self.result["cleanup"]["volume"] = (
                        "absent"
                        if self.volume_acknowledged
                        else "creation-unsettled-not-observed"
                    )
                elif container_safe and self.owned_volume(value):
                    self.docker(["volume", "rm", self.volume], "volume-cleanup")
                    if self.inspect("volume", self.volume, missing=True) is not None:
                        raise SmokeFailure("volume-cleanup")
                    self.result["cleanup"]["volume"] = "removed-owned-fresh-volume"
                else:
                    self.result["cleanup"]["volume"] = (
                        "refused-unverified-owner-or-container"
                    )
            except SmokeFailure:
                self.result["cleanup"]["volume"] = "unverified-or-failed"
        else:
            self.result["cleanup"]["volume"] = "not-created"
        good = {
            "not-created",
            "absent",
            "removed-owned-CID",
            "removed-owned-fresh-volume",
        }
        self.result["cleanup"]["verified"] = all(
            self.result["cleanup"][key] in good for key in ("container", "volume")
        )

    def run(self):
        try:
            image = json.loads(
                self.docker(
                    ["image", "inspect", "--format", IMAGE_FORMAT, self.reference],
                    "image-inspection",
                ).stdout
            )
            config = image.get("Config", {})
            values = dict(x.split("=", 1) for x in config.get("Env", []) if "=" in x)
            self.image_id = image.get("Id")
            if not (
                isinstance(self.image_id, str)
                and IMAGE_ID.fullmatch(self.image_id)
                and image.get("Os") == "linux"
                and config.get("Entrypoint") == ["docker-entrypoint.sh"]
                and config.get("Cmd") == ["postgres"]
                and config.get("User", "") in ("", "0", "0:0")
                and config.get("StopSignal") == "SIGINT"
                and config.get("Volumes") == {PGDATA: {}}
                and values.get("PGDATA") == PGDATA
                and values.get("PG_MAJOR") == "16"
            ):
                raise SmokeFailure("image-contract")
            self.result["image_id"] = self.image_id
            for kind, name in [("container", self.container), ("volume", self.volume)]:
                if self.inspect(kind, name, missing=True) is not None:
                    raise SmokeFailure("fresh-resource-required")
            self.private_directory = tempfile.TemporaryDirectory(
                prefix="bidit-pg-runtime-private-"
            )
            directory = Path(self.private_directory.name)
            directory.chmod(0o700)
            env_file = directory / "postgres.env"
            marker = uuid.uuid4().hex
            self.private_values = [secrets.token_urlsafe(32), secrets.token_urlsafe(32)]
            with os.fdopen(
                os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
            ) as stream:
                stream.write(
                    "POSTGRES_USER=bidit_fixture_admin\nPOSTGRES_DB=bidit_runtime\nPOSTGRES_PASSWORD="
                    + self.private_values[0]
                    + "\nBIDIT_SMOKE_ROLE_PASSWORD="
                    + self.private_values[1]
                    + "\nBIDIT_SMOKE_MARKER="
                    + marker
                    + "\n"
                )
            if stat.S_IMODE(env_file.stat().st_mode) != 0o600:
                raise SmokeFailure("private-environment")
            self.volume_attempted = True
            self.docker(
                [
                    "volume",
                    "create",
                    "--label",
                    LABEL + "=" + self.token,
                    "--label",
                    KIND + "=volume",
                    self.volume,
                ],
                "volume-creation",
            )
            self.volume_acknowledged = True
            volume = self.inspect("volume", self.volume)
            if not self.owned_volume(volume):
                raise SmokeFailure("volume-ownership")
            self.volume_created_at = volume["CreatedAt"]
            self.container_attempted = True
            created = self.docker(
                [
                    "create",
                    "--pull=never",
                    "--name",
                    self.container,
                    "--label",
                    LABEL + "=" + self.token,
                    "--label",
                    KIND + "=container",
                    "--network",
                    "none",
                    "--env-file",
                    str(env_file),
                    "--memory",
                    "512m",
                    "--pids-limit",
                    "128",
                    "--mount",
                    "type=volume,src=" + self.volume + ",dst=" + PGDATA,
                    self.image_id,
                ],
                "container-creation",
            ).stdout.strip()
            self.container_acknowledged = True
            if not CONTAINER_ID.fullmatch(created):
                raise SmokeFailure("container-identity")
            value = self.inspect("container", self.container)
            if not self.owned_container(value) or value.get("Id") != created:
                raise SmokeFailure("container-ownership")
            self.cid = created
            self.result["resources"]["container_id"] = self.cid
            self.docker(["start", self.cid], "server-start")
            self.ready()
            identity = self.identity()
            server = self.sql_json(SERVER_SQL)
            if (
                set(server)
                != {
                    "major",
                    "version",
                    "system_identifier",
                    "password_encryption",
                    "data_directory",
                }
                or type(server.get("major")) is not int
                or server.get("major") != 16
                or not re.fullmatch(r"16\.[0-9]+", str(server.get("version", "")))
                or server.get("password_encryption") != "scram-sha-256"
                or server.get("data_directory") != PGDATA
                or not re.fullmatch(r"[0-9]+", str(server.get("system_identifier", "")))
            ):
                raise SmokeFailure("server-contract")
            self.result["public_helper_metadata"] = self.helper_metadata()
            self.sql(SETUP_SQL)
            self.sql(MARKER_SQL, ordinary=True)
            before = self.sql_json(SNAPSHOT_SQL, ordinary=True)
            expected_row = {
                "id": 1,
                "amount": "-12.34",
                "payload": {"kind": "owned-scratch", "count": 3},
                "marker": marker,
            }
            if not (
                before.get("role") == "bidit_smoke_user"
                and before.get("superuser") is False
                and before.get("bypassrls") is False
                and before.get("rows") == [expected_row]
                and all(
                    re.fullmatch(r"[0-9]+", str(before.get(k, "")))
                    for k in ["role_oid", "table_oid"]
                )
            ):
                raise SmokeFailure("ordinary-role-marker")
            auth = self.sql_json(AUTH_SQL)
            if (
                set(auth) != {"rules"}
                or not isinstance(auth["rules"], list)
                or not all(
                    isinstance(row, dict)
                    and set(row) == {"type", "address", "method"}
                    and isinstance(row["type"], str)
                    and isinstance(row["method"], str)
                    and (row["address"] is None or isinstance(row["address"], str))
                    for row in auth["rules"]
                )
            ):
                raise SmokeFailure("auth-observation")
            wrong_password_accepted = (
                self.sql("SELECT 1;", ordinary=True, wrong=True, check=False).returncode
                == 0
            )
            self.result["checks"].append(
                {
                    "name": "fresh-init-and-ordinary-role",
                    "server": server,
                    "identity": identity,
                    "role_oid": before["role_oid"],
                    "table_oid": before["table_oid"],
                    "ordinary_role": before["role"],
                    "auth_observation": auth,
                    "wrong_password_accepted": wrong_password_accepted,
                }
            )
            self.docker(["stop", "--time", "10", self.cid], "graceful-stop", timeout=20)
            stopped = self.inspect("container", self.cid)
            if (
                not self.owned_container(stopped)
                or stopped.get("Running") is not False
                or stopped.get("ExitCode") != 0
                or stopped.get("OOMKilled") is not False
            ):
                raise SmokeFailure("graceful-stop")
            self.docker(["start", self.cid], "server-restart")
            self.ready()
            if (
                self.identity() != identity
                or self.sql_json(SERVER_SQL) != server
                or self.sql_json(SNAPSHOT_SQL, ordinary=True) != before
            ):
                raise SmokeFailure("restart-persistence")
            if (
                self.sql_json(AUTH_SQL) != auth
                or (
                    self.sql(
                        "SELECT 1;", ordinary=True, wrong=True, check=False
                    ).returncode
                    == 0
                )
                != wrong_password_accepted
            ):
                raise SmokeFailure("restart-auth-observation")
            if not self.owned_container(
                self.inspect("container", self.cid)
            ) or not self.owned_volume(self.inspect("volume", self.volume)):
                raise SmokeFailure("restart-resource-identity")
            self.result["checks"].append(
                {
                    "name": "same-container-volume-graceful-restart",
                    "same_system_identifier": True,
                    "same_role_and_table_OIDs": True,
                    "same_signed_numeric_JSON_marker": True,
                }
            )
            self.result["status"] = "passed"
        except (Interrupted, KeyboardInterrupt):
            self.result.setdefault("failure_phase", "interrupted")
            self.result["interruption_observed"] = True
        except SmokeFailure as exc:
            self.result["failure_phase"] = str(exc)
        except Exception:  # noqa: BLE001 - only typed phases may leave the private fixture boundary.
            self.result["failure_phase"] = "invalid-evidence-or-internal-error"
        finally:
            self.finish_cleanup()
        return self.public_result()

    def finish_cleanup(self):
        """Shared, repeatable teardown also covers interruption at finally entry."""
        self.cleaning = True
        try:
            self.cleanup()
        except (Interrupted, KeyboardInterrupt):
            self.result.setdefault("failure_phase", "interrupted")
            self.result["interruption_observed"] = True
            self.result["cleanup"]["verified"] = False
        except Exception:  # noqa: BLE001 - cleanup diagnostics may contain private fixture input.
            self.result["cleanup"]["verified"] = False
            self.result.setdefault("failure_phase", "cleanup")
        finally:
            if self.private_directory is not None:
                try:
                    self.private_directory.cleanup()
                    self.result["cleanup"]["private_environment_removed"] = not Path(
                        self.private_directory.name
                    ).exists()
                except (Interrupted, KeyboardInterrupt):
                    self.result.setdefault("failure_phase", "interrupted")
                    self.result["interruption_observed"] = True
                    self.result["cleanup"]["private_environment_removed"] = False
                except Exception:  # noqa: BLE001 - keep private cleanup failures typed and non-public.
                    self.result["cleanup"]["private_environment_removed"] = False
            else:
                self.result["cleanup"]["private_environment_removed"] = True
            if (
                not self.result["cleanup"].get("verified")
                or not self.result["cleanup"]["private_environment_removed"]
            ):
                self.result["status"] = "failed"
                self.result.setdefault("failure_phase", "cleanup")
        if self.result.get("failure_phase"):
            self.result["status"] = "failed"

    def public_result(self):
        """The fallback path must apply the same credential-output fence."""
        if any(value in json.dumps(self.result) for value in self.private_values):
            self.result = {
                "status": "failed",
                "failure_phase": "private-data-in-evidence",
                "cleanup": {
                    "verified": bool(self.result["cleanup"].get("verified")),
                    "private_environment_removed": bool(
                        self.result["cleanup"].get("private_environment_removed")
                    ),
                },
            }
        return self.result


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if (
        len(args) != 1
        or args[0].startswith("-")
        or not re.fullmatch(r"[A-Za-z0-9_./:@-]+", args[0])
    ):
        print(
            json.dumps(
                {
                    "status": "failed",
                    "failure_phase": "arguments",
                    "usage": "check_postgres_runtime.py LOCAL_IMAGE",
                }
            )
        )
        return 2
    previous = {}
    probe = Probe(args[0])

    def interrupted(_signum, _frame):
        # Prevent repeated cancellation from interrupting strict finally cleanup.
        for sig in previous:
            signal.signal(sig, signal.SIG_IGN)
        probe.result["interruption_observed"] = True
        probe.result.setdefault("failure_phase", "interrupted")
        probe.result["status"] = "failed"
        if probe.cleaning:
            return
        raise Interrupted()

    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, interrupted)
        result = probe.run()
    except (Interrupted, KeyboardInterrupt):
        # Before run() starts there are no private files/resources. After it
        # returns, retain its already verified (or failed) teardown evidence.
        if not probe.cleaning:
            probe.finish_cleanup()
        probe.result["status"] = "failed"
        probe.result.setdefault("failure_phase", "interrupted")
        probe.result["interruption_observed"] = True
        result = probe.result
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    result = probe.public_result()
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
