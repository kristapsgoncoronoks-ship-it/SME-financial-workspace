"""Offline smoke must refuse changed or unexpected installer inputs."""

import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "runtime_smoke", ROOT / "scripts/backend_runtime_smoke.py"
)
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)
RUNTIME_SPEC = importlib.util.spec_from_file_location(
    "smoke_inventory", ROOT / "backend/scripts/runtime_smoke.py"
)
runtime = importlib.util.module_from_spec(RUNTIME_SPEC)
RUNTIME_SPEC.loader.exec_module(runtime)


@pytest.mark.parametrize("change", [None, "wheel", "lock", "extra", "path"])
def test_offline_smoke_refuses_corrupt_or_unreviewed_inputs(tmp_path, change):
    wheel = tmp_path / "synthetic-1-py3-none-any.whl"
    wheel.write_bytes(b"synthetic wheel identity, not an installable distribution")
    lock = tmp_path / "test-tools.lock"
    lock.write_text("synthetic==1 --hash=sha256:synthetic\n")
    manifest = {
        "wheels": [
            {"filename": wheel.name, "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()}
        ],
        "lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
    }
    if change == "wheel":
        wheel.write_bytes(b"changed wheel")
    elif change == "lock":
        lock.write_text("changed lock")
    elif change == "extra":
        (tmp_path / "unreviewed-1-py3-none-any.whl").write_bytes(b"unreviewed")
    elif change == "path":
        manifest["wheels"][0]["filename"] = "../" + wheel.name
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    if change is None:
        assert smoke.verify_wheelhouse(tmp_path) == manifest
    else:
        with pytest.raises(ValueError):
            smoke.verify_wheelhouse(tmp_path)


def test_smoke_inventory_refuses_a_venv_shadow_of_a_runtime_distribution(monkeypatch):
    installed = [
        SimpleNamespace(
            metadata={"Name": name}, version="1", locate_file=lambda _p, root=root: root
        )
        for name, root in (
            ("Packaging", "/tmp/venv/site-packages"),
            ("packaging", "/usr/local/site-packages"),
        )
    ]
    monkeypatch.setattr(runtime.importlib.metadata, "distributions", lambda: installed)
    with pytest.raises(RuntimeError, match="duplicate"):
        runtime.distributions()
