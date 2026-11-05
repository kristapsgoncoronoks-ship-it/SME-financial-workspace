"""Run pytest in a disposable venv over the unchanged installed runtime graph."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

TEST_ONLY = {"pytest", "pytest-asyncio", "iniconfig", "pluggy", "pygments"}


def distributions() -> dict:
    result = {}
    for dist in importlib.metadata.distributions():
        name = re.sub(r"[-_.]+", "-", dist.metadata["Name"].lower())
        if name in result:
            raise RuntimeError("Smoke environment has duplicate distribution metadata")
        result[name] = {"version": dist.version, "location": str(dist.locate_file(""))}
    return result


def main() -> int:
    if os.getuid() != 10001 or os.getgid() != 10001:
        raise RuntimeError("Smoke must retain the runtime image's non-root UID/GID")
    wheelhouse = Path("/wheelhouse")
    manifest = json.loads((wheelhouse / "manifest.json").read_text())
    if (
        hashlib.sha256(Path("/app/requirements-test.txt").read_bytes()).hexdigest()
        != manifest["test_manifest_sha256"]
    ):
        raise RuntimeError("Wheelhouse does not match this image's test requirements")
    lock = wheelhouse / "test-tools.lock"
    if hashlib.sha256(lock.read_bytes()).hexdigest() != manifest["lock_sha256"]:
        raise RuntimeError("Smoke lock integrity check failed")
    for wheel in manifest["wheels"]:
        path = wheelhouse / wheel["filename"]
        if (
            path.name != wheel["filename"]
            or hashlib.sha256(path.read_bytes()).hexdigest() != wheel["sha256"]
        ):
            raise RuntimeError("Smoke wheel integrity check failed")
    before = distributions()
    if TEST_ONLY & before.keys():
        raise RuntimeError("Production runtime still contains test distributions")
    if before != manifest["runtime_distributions"]:
        raise RuntimeError("Wheelhouse was prepared for a different runtime graph")
    environment = {**os.environ, "UV_NO_CACHE": "1", "UV_PYTHON_DOWNLOADS": "never"}
    with tempfile.TemporaryDirectory(prefix="bidit-runtime-smoke-") as temporary:
        python = str(Path(temporary) / "venv/bin/python")
        subprocess.run(
            [
                "/tmp/smoke-uv",
                "venv",
                "--system-site-packages",
                "--python",
                sys.executable,
                str(Path(temporary) / "venv"),
            ],
            env=environment,
            check=True,
        )
        subprocess.run(
            [
                "/tmp/smoke-uv",
                "pip",
                "install",
                "--python",
                python,
                "--offline",
                "--no-index",
                "--find-links",
                str(wheelhouse),
                "--no-deps",
                "--require-hashes",
                "-r",
                str(lock),
            ],
            env=environment,
            check=True,
        )
        subprocess.run(
            ["/tmp/smoke-uv", "pip", "check", "--python", python], env=environment, check=True
        )
        probe = (
            "import json,runpy; "
            "m=runpy.run_path('/app/scripts/runtime_smoke.py',run_name='smoke_inventory'); "
            "print(json.dumps(m['distributions']()))"
        )
        after = json.loads(subprocess.check_output([python, "-c", probe], text=True))
        if any(after.get(name) != identity for name, identity in before.items()):
            raise RuntimeError("Smoke tooling replaced an installed runtime distribution")
        result = subprocess.run(
            [python, "-m", "pytest", *sys.argv[1:]], env=environment, check=False
        )
        sys.stdout.write(
            "backend-smoke-evidence: "
            + json.dumps(
                {
                    "runtime_distributions_unchanged": True,
                    "runtime_distribution_count": len(before),
                    "test_distributions": sorted(after.keys() - before.keys()),
                    "pytest_exit": result.returncode,
                    "uid": os.getuid(),
                    "gid": os.getgid(),
                    "offline_install": True,
                }
            )
            + "\n"
        )
        sys.stdout.flush()
        return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
