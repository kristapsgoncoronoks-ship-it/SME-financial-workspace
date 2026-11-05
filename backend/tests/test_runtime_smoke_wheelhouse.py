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


@pytest.mark.parametrize("fault", [None, "missing", "incompatible", "malformed", "version"])
def test_visible_dependency_check_covers_system_packages_and_refuses_bad_metadata(
    monkeypatch, fault
):
    installed = [
        SimpleNamespace(
            metadata={"Name": "parent"},
            version="1",
            locate_file=lambda _p: "/tmp/venv/site-packages",
            requires=["child>=2", "optional; extra == 'test'", "other; python_version < '0'"],
        ),
        SimpleNamespace(
            metadata={"Name": "child"},
            version="2",
            locate_file=lambda _p: "/usr/local/site-packages",
            requires=[],
        ),
    ]
    if fault == "missing":
        installed.pop()
    elif fault == "incompatible":
        installed[1].version = "1"
    elif fault == "malformed":
        installed[0].requires = ["not a valid requirement ???"]
    elif fault == "version":
        installed[1].version = "invalid version"
    monkeypatch.setattr(runtime.importlib.metadata, "distributions", lambda: installed)
    if fault is None:
        result = runtime.check_dependencies()
        assert result["checked_requirements"] == 1
        assert result["distributions"]["child"]["location"] == "/usr/local/site-packages"
    else:
        with pytest.raises(RuntimeError):
            runtime.check_dependencies()
