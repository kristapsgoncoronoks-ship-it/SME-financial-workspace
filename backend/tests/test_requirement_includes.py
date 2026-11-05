"""Dependency review must preserve and bound the committed local manifest graph."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "dependency_includes", ROOT / "scripts/dependency_delta.py"
)
dependency = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dependency)


@pytest.mark.parametrize("symlink", [False, True])
def test_base_audit_materializes_nested_requirement_and_constraint_files(tmp_path, symlink):
    (tmp_path / "scripts").mkdir()
    shutil.copyfile(ROOT / "scripts/dependency_delta.py", tmp_path / "scripts/dependency_delta.py")
    files = {
        "backend/requirements.txt": "-r manifests/requirements-runtime.txt\n-r requirements-test.txt\n",
        "backend/requirements-dev.txt": "-r requirements.txt\nruff==0.17.0\n",
        "backend/requirements-test.txt": "pytest==9.1.1\n",
        "backend/manifests/requirements-runtime.txt": "-c ../constraints.txt\nfastapi==0.143.0\n",
        "backend/constraints.txt": "fastapi==0.143.0\n",
        "frontend/package-lock.json": '{"lockfileVersion":3,"packages":{"":{}}}\n',
    }
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    if symlink:
        path = tmp_path / "backend/manifests/requirements-runtime.txt"
        path.unlink()
        path.symlink_to("../requirements-test.txt")
    for args in (
        ["git", "init", "-q"],
        ["git", "-c", "user.name=Synthetic", "-c", "user.email=synthetic@example.com", "add", "."],
        [
            "git",
            "-c",
            "user.name=Synthetic",
            "-c",
            "user.email=synthetic@example.com",
            "commit",
            "-qm",
            "Synthetic manifests",
        ],
    ):
        subprocess.run(args, cwd=tmp_path, check=True, capture_output=True)
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    report = {
        "dependencies": [
            {"name": name, "version": version, "vulns": []}
            for name, version in (("fastapi", "0.143.0"), ("pytest", "9.1.1"), ("ruff", "0.17.0"))
        ]
    }
    (tmp_path / "head.json").write_text(json.dumps(report))
    # The scanner stub only checks that its recursive input files exist; it
    # provides no package/network/security verdict and cannot conceal omissions.
    (tmp_path / "pip_audit.py").write_text(
        "import json,pathlib,shlex,sys\n"
        "seen=set()\n"
        "def read(p):\n"
        " p=pathlib.Path(p)\n"
        " if p in seen:return\n"
        " seen.add(p)\n"
        " for line in p.read_text().splitlines():\n"
        "  tokens=shlex.split(line,comments=True)\n"
        "  if tokens and tokens[0] in ('-r','-c'):read(p.parent/tokens[1])\n"
        "for i,arg in enumerate(sys.argv):\n"
        " if arg=='-r':read(sys.argv[i+1])\n"
        f"print({json.dumps(report)!r})\n"
    )
    result = subprocess.run(
        [
            sys.executable,
            str(tmp_path / "scripts/dependency_delta.py"),
            "--base",
            base,
            "--head-audit",
            "head.json",
            "--output",
            "delta.json",
        ],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    if symlink:
        assert result.returncode != 0 and not (tmp_path / "delta.json").exists()
        assert "non-regular" in result.stderr
        return
    assert result.returncode == 0, result.stdout + result.stderr
    delta = json.loads((tmp_path / "delta.json").read_text())
    assert delta["python_resolved_changes"] == []


@pytest.mark.parametrize(
    "line",
    [
        "-r ../../outside.txt",
        "-r /private.txt",
        "--requirement=https://example.com/r.txt",
        "-r ${HOME}/requirements.txt",
        "-r ~/requirements.txt",
        "-r requirements.in",
        "--index-url https://example.com",
        "-r child.txt extra.txt",
        "--requirement",
    ],
)
def test_requirement_includes_refuse_unsafe_or_unsupported_paths(line):
    files = {"backend/requirements.txt": line.encode(), "backend/requirements-dev.txt": b""}
    read = []

    def source(path):
        read.append(path)
        return files[path]

    with pytest.raises(ValueError):
        dependency.requirement_closure(source)
    assert read == ["backend/requirements.txt"]


@pytest.mark.parametrize("budget", ["cycle", "depth", "files", "bytes", "total", "missing"])
def test_requirement_graph_budgets_and_missing_files_fail_closed(monkeypatch, budget):
    files = {
        "backend/requirements.txt": b"-r child.txt\n",
        "backend/child.txt": b"fastapi==0.143.0\n",
        "backend/requirements-dev.txt": b"ruff==0.17.0\n",
    }
    if budget == "cycle":
        files["backend/child.txt"] = b"-r requirements.txt\n"
    elif budget == "depth":
        monkeypatch.setattr(dependency, "MAX_REQUIREMENT_DEPTH", 1)
    elif budget == "files":
        monkeypatch.setattr(dependency, "MAX_REQUIREMENT_FILES", 1)
    elif budget == "bytes":
        monkeypatch.setattr(dependency, "MAX_REQUIREMENT_FILE_BYTES", 8)
    elif budget == "total":
        monkeypatch.setattr(dependency, "MAX_REQUIREMENT_TOTAL_BYTES", 24)
    else:
        del files["backend/child.txt"]
    with pytest.raises((ValueError, KeyError)):
        dependency.requirement_closure(files.__getitem__)


def test_runtime_and_combined_graphs_preserve_authoritative_package_pins():
    closure = dependency.requirement_closure(lambda name: (ROOT / name).read_bytes())
    definitions = {}
    for content in closure.values():
        for line in content.decode().splitlines():
            line = line.split("#", 1)[0].strip()
            if line and not line.startswith("-"):
                package, version = line.split("==", 1)
                normalized = package.split("[", 1)[0].lower().replace("_", "-")
                assert normalized not in definitions, f"multiple authoritative pins for {package}"
                definitions[normalized] = version
    runtime = (ROOT / "backend/requirements-runtime.txt").read_text().lower()
    assert "pytest" not in runtime and "pytest-asyncio" not in runtime
    for package in ("pyyaml", "httpx", "prometheus-client", "pytesseract", "pypdf", "reportlab"):
        assert package + "==" in runtime
    assert {"pytest", "pytest-asyncio", "pytest-xdist", "execnet"} <= definitions.keys()
