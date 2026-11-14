"""Host CI refuses unverified compiler bytes while preserving npm's contract."""

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "install_ci_typescript", ROOT / "scripts/install_ci_typescript.py"
)
compiler = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compiler)


@pytest.fixture
def output(tmp_path):
    directory = tmp_path / "out"
    provenance = directory / "provenance"
    provenance.mkdir(parents=True)
    (directory / "tsc").write_bytes(b"synthetic-native-compiler")
    digest = hashlib.sha256((directory / "tsc").read_bytes()).hexdigest()
    (provenance / "compiler.sha256").write_text(digest + "  /out/tsc\n")
    (provenance / "source.txt").write_text(
        f"source: https://github.com/microsoft/typescript-go/tree/{compiler.SOURCE}\n"
        f"archive-sha256: {compiler.ARCHIVE_SHA256}\n"
    )
    (provenance / "compiler-build.txt").write_text(
        f"/out/tsc: {compiler.GO_VERSION}\n"
        "\tpath\tgithub.com/microsoft/typescript-go/cmd/tsgo\n"
        f"\tdep\tgolang.org/x/text\t{compiler.TEXT_VERSION}\th1:synthetic\n"
        "\tbuild\tCGO_ENABLED=0\n\tbuild\tGOOS=linux\n"
    )
    for name in (
        "LICENSE",
        "NOTICE.txt",
        "go.mod",
        "go.sum",
        "licenses/go/LICENSE",
        "licenses/modules/golang.org/x/text/LICENSE",
    ):
        path = provenance / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Synthetic notice\n")
    return directory


@pytest.fixture
def frontend(tmp_path):
    root = tmp_path / "frontend"
    package = root / "node_modules/typescript"
    package.mkdir(parents=True)
    (package / "package.json").write_text(json.dumps({"version": compiler.VERSION}))
    for name in ("lib/getExePath.js", "lib/lib.es5.d.ts", "bin/tsc"):
        path = package / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Original npm source: " + name)
    target = root / "node_modules/@typescript/typescript-linux-x64/lib/tsc"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"original-official-native-executable")
    (target.parent / "lib.d.ts").write_text("Original npm standard library\n")
    return root, target


def fake_launcher(monkeypatch, target, *, candidate_version=None):
    calls = []

    def run(*args, **kwargs):
        calls.append(args)
        if args[0] == "node":
            return str(target)
        if args[-1] == "--version":
            return candidate_version or f"Version {compiler.VERSION}"
        raise AssertionError(args)

    monkeypatch.setattr(compiler, "run", run)
    return calls


def test_install_replaces_only_native_bytes_and_retains_all_provenance(
    output, frontend, monkeypatch
):
    root, target = frontend
    original = {
        path: path.read_bytes()
        for path in (root / "node_modules/typescript").rglob("*")
        if path.is_file()
    }
    calls = fake_launcher(monkeypatch, target)
    result = compiler.install(output, root)
    assert target.read_bytes() == (output / "tsc").read_bytes()
    assert result["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert all(path.read_bytes() == value for path, value in original.items())
    retained = root / "node_modules/.bidit-typescript-provenance"
    assert all(
        (retained / path.relative_to(output / "provenance")).read_bytes() == path.read_bytes()
        for path in (output / "provenance").rglob("*")
        if path.is_file()
    )
    assert calls[-1] == (str(root / "node_modules/.bin/tsc"), "--version")
    assert not list(target.parent.glob(".bidit-tsc-*"))


@pytest.mark.parametrize(
    "fault",
    [
        "checksum",
        "source",
        "toolchain",
        "unicode",
        "source_notice",
        "go_notice",
        "module_notice",
        "symlink",
    ],
)
def test_bad_provenance_never_executes_or_replaces_existing_compiler(
    output, frontend, monkeypatch, fault
):
    root, target = frontend
    provenance = output / "provenance"
    if fault == "checksum":
        (output / "tsc").write_bytes(b"changed-after-hashing")
    elif fault == "source":
        (provenance / "source.txt").write_text("unqualified source\n")
    elif fault in ("toolchain", "unicode"):
        path = provenance / "compiler-build.txt"
        old = compiler.GO_VERSION if fault == "toolchain" else compiler.TEXT_VERSION
        path.write_text(path.read_text().replace(old, "unqualified"))
    elif fault == "symlink":
        (output / "extra").symlink_to(target)
    else:
        name = {
            "source_notice": "NOTICE.txt",
            "go_notice": "licenses/go/LICENSE",
            "module_notice": "licenses/modules/golang.org/x/text/LICENSE",
        }[fault]
        (provenance / name).unlink()
    calls = fake_launcher(monkeypatch, target)
    before = target.read_bytes()
    with pytest.raises(ValueError):
        compiler.install(output, root)
    assert target.read_bytes() == before
    assert calls == []


@pytest.mark.parametrize("old_toolchain", ["go1.27.3", "go1.27.2", "go1.28.0"])
def test_unqualified_toolchain_refuses_before_execution_or_installation(
    output, frontend, monkeypatch, old_toolchain
):
    root, target = frontend
    metadata = output / "provenance/compiler-build.txt"
    assert metadata.read_text().startswith("/out/tsc: go1.27.4\n")
    metadata.write_text(metadata.read_text().replace("go1.27.4", old_toolchain, 1))
    calls = fake_launcher(monkeypatch, target)
    before = target.read_bytes()
    with pytest.raises(ValueError, match="unexpected Go toolchain"):
        compiler.install(output, root)
    assert target.read_bytes() == before
    assert calls == []
    assert not (root / "node_modules/.bidit-typescript-provenance").exists()


def test_synthetic_current_toolchain_output_is_admitted_with_all_notices(output):
    # Parser protocol evidence only; output contains no actual compiler binary.
    assert compiler.GO_VERSION == "go1.27.4"
    assert (output / "provenance/compiler-build.txt").read_text().startswith("/out/tsc: go1.27.4\n")
    assert (
        compiler.validate_output(output)
        == hashlib.sha256((output / "tsc").read_bytes()).hexdigest()
    )


def test_npm_version_skew_is_refused_without_resolving_launcher(output, frontend, monkeypatch):
    root, target = frontend
    (root / "node_modules/typescript/package.json").write_text('{"version":"unqualified"}')
    calls = fake_launcher(monkeypatch, target)
    before = target.read_bytes()
    with pytest.raises(ValueError, match="npm TypeScript version"):
        compiler.install(output, root)
    assert target.read_bytes() == before
    assert calls == []


def test_launcher_target_escape_is_refused(output, frontend, monkeypatch, tmp_path):
    root, _ = frontend
    external = tmp_path / "external-compiler"
    external.write_bytes(b"outside-node-modules")
    fake_launcher(monkeypatch, external)
    with pytest.raises(ValueError, match="in node_modules"):
        compiler.install(output, root)
    assert external.read_bytes() == b"outside-node-modules"


def test_binary_version_skew_is_refused_before_installation(output, frontend, monkeypatch):
    root, target = frontend
    fake_launcher(monkeypatch, target, candidate_version="Version unqualified")
    before = target.read_bytes()
    with pytest.raises(ValueError, match="unexpected version"):
        compiler.install(output, root)
    assert target.read_bytes() == before


def test_candidate_runs_with_adjacent_libraries_before_atomic_replacement(
    output, frontend, monkeypatch
):
    root, target = frontend
    original = target.read_bytes()
    calls = fake_launcher(monkeypatch, target)
    wrapped_run = compiler.run

    def run(*args, **kwargs):
        if args[-1] == "--version" and Path(args[0]).parent == target.parent:
            assert target.read_bytes() == original
            assert Path(args[0]).read_bytes() == (output / "tsc").read_bytes()
        return wrapped_run(*args, **kwargs)

    monkeypatch.setattr(compiler, "run", run)
    compiler.install(output, root)
    candidate = Path(calls[1][0])
    assert candidate != target
    assert candidate.parent == target.parent
    assert (candidate.parent / "lib.d.ts").is_file()
    assert calls[1][-1] == "--version"


def test_missing_adjacent_standard_library_refuses_before_replacement(
    output, frontend, monkeypatch
):
    root, target = frontend
    (target.parent / "lib.d.ts").unlink()
    calls = fake_launcher(monkeypatch, target)
    before = target.read_bytes()
    with pytest.raises(ValueError, match="standard library is missing"):
        compiler.install(output, root)
    assert target.read_bytes() == before
    assert len(calls) == 1


def test_identical_read_only_notices_allow_repeatable_install(output, frontend, monkeypatch):
    root, target = frontend
    fake_launcher(monkeypatch, target)
    first = compiler.install(output, root)
    retained = root / "node_modules/.bidit-typescript-provenance"
    for path in retained.rglob("*"):
        if path.is_file():
            path.chmod(0o444)
    assert compiler.install(output, root) == first
    assert target.read_bytes() == (output / "tsc").read_bytes()


def test_differing_existing_provenance_refuses_before_replacement(output, frontend, monkeypatch):
    root, target = frontend
    fake_launcher(monkeypatch, target)
    compiler.install(output, root)
    retained = root / "node_modules/.bidit-typescript-provenance/source.txt"
    retained.write_text("unexpected previous compiler\n")
    target.write_bytes(b"pre-existing executable remains until provenance repaired")
    before = target.read_bytes()
    with pytest.raises(ValueError, match="run npm ci"):
        compiler.install(output, root)
    assert target.read_bytes() == before
    assert retained.read_text() == "unexpected previous compiler\n"


@pytest.mark.parametrize("nested", [False, True])
def test_retained_provenance_symlink_cannot_write_outside_node_modules(
    output, frontend, monkeypatch, tmp_path, nested
):
    root, target = frontend
    retained = root / "node_modules/.bidit-typescript-provenance"
    outside = tmp_path / "outside"
    outside.mkdir()
    if nested:
        retained.mkdir()
        (retained / "licenses").symlink_to(outside, target_is_directory=True)
    else:
        retained.symlink_to(outside, target_is_directory=True)
    fake_launcher(monkeypatch, target)
    before = target.read_bytes()
    with pytest.raises(ValueError, match="must not contain symlinks"):
        compiler.install(output, root)
    assert target.read_bytes() == before
    assert not list(outside.iterdir())


@pytest.mark.parametrize("cleanup_failure", [False, True])
def test_docker_copy_failure_removes_owned_container_without_installing(
    tmp_path, monkeypatch, cleanup_failure
):
    calls = []

    def run(*args, **kwargs):
        calls.append(args)
        if args[:2] == ("docker", "build"):
            Path(args[args.index("--iidfile") + 1]).write_text("sha256:" + "a" * 64)
        elif args[:2] == ("docker", "cp"):
            raise subprocess.CalledProcessError(17, args)
        elif args[:2] == ("docker", "rm") and cleanup_failure:
            raise subprocess.CalledProcessError(18, args)
        return ""

    monkeypatch.setattr(compiler, "run", run)
    monkeypatch.setattr(compiler, "install", lambda *_: pytest.fail("must not install"))
    with pytest.raises(subprocess.CalledProcessError) as error:
        compiler.build_and_install(tmp_path)
    assert error.value.returncode == 17
    assert calls[-1][:3] == ("docker", "rm", "--force")
    created = next(call for call in calls if call[:2] == ("docker", "create"))
    assert calls[-1][-1] == created[created.index("--name") + 1]


def test_ci_installs_rebuild_before_host_build_and_preserves_docker_identity():
    document = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    commands = [step.get("run", "") for step in document["jobs"]["frontend"]["steps"]]
    assert commands.index("npm ci") < commands.index("python3 ../scripts/install_ci_typescript.py")
    assert commands.index("python3 ../scripts/install_ci_typescript.py") < next(
        i for i, command in enumerate(commands) if command.startswith("npm run build")
    )
    dockerfile = (ROOT / "frontend/Dockerfile").read_text()
    assert compiler.SOURCE in dockerfile and compiler.ARCHIVE_SHA256 in dockerfile
    assert f"golang:{compiler.GO_VERSION.removeprefix('go')}-alpine@sha256:" in dockerfile
    assert f"golang.org/x/text@{compiler.TEXT_VERSION}" in dockerfile


def test_cloud_build_options_retain_tls_and_do_not_put_proxy_values_in_argv(tmp_path, monkeypatch):
    ca = tmp_path / "public-ca.crt"
    ca.write_text("Synthetic public certificate\n")
    monkeypatch.setenv("HTTPS_PROXY", "http://synthetic-private-proxy.invalid:1234")
    calls = []

    def run(*args, **kwargs):
        calls.append(args)
        raise subprocess.CalledProcessError(9, args)

    monkeypatch.setattr(compiler, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        compiler.build_and_install(tmp_path, ca, "host")
    args = calls[0]
    assert args[args.index("--network") + 1] == "host"
    assert args[args.index("--secret") + 1] == f"id=build_ca,src={ca}"
    assert "HTTPS_PROXY" in args
    assert not any("synthetic-private-proxy" in value for value in args)
    assert "--insecure" not in args


def test_missing_ca_refuses_without_executing_docker(tmp_path, monkeypatch):
    monkeypatch.setattr(compiler, "run", lambda *_args, **_kwargs: pytest.fail("must not build"))
    with pytest.raises(ValueError, match="existing file"):
        compiler.build_and_install(tmp_path, tmp_path / "missing-ca")
