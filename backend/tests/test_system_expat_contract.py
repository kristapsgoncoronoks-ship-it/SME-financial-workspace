"""Exercise paired XML-extension verification and guarded replacement behavior."""

import ctypes
import hashlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import sysconfig
import xml.etree.ElementTree as real_etree
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "backend/scripts"


def _load(name):
    path = SCRIPTS / (name + ".py")
    assert path.is_file(), "exercise the actual production helper"
    spec = importlib.util.spec_from_file_location(name + "_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def verifier():
    return _load("verify_system_expat")


@pytest.fixture(scope="module")
def builder():
    return _load("build_system_expat")


@pytest.mark.parametrize(
    "runtime,compiled",
    [((2, 10, 0), (2, 10, 0)), ((2, 10, 1), (2, 10, 0)), ((2, 11, 0), (2, 10, 0))],
    ids=["matching-fixed-pair", "newer-runtime-micro", "newer-runtime-minor-same-major"],
)
def test_version_guard_accepts_fixed_compile_pair_with_compatible_runtime(
    verifier, runtime, compiled
):
    verifier.validate_versions(runtime, compiled, compiled)


@pytest.mark.parametrize(
    "runtime,capi,elementtree",
    [
        ((2, 9, 0), (2, 9, 0), (2, 9, 0)),
        ((2, 10, 0), (2, 9, 0), (2, 9, 0)),
        ((2, 9, 0), (2, 10, 0), (2, 10, 0)),
        ((2, 10, 0), (2, 10, 0), (2, 9, 0)),
        ((2, 10, 1), (2, 10, 0), (2, 10, 1)),
        ((2, 10, 1), (2, 10, 1), (2, 10, 0)),
        ((3, 0, 0), (2, 10, 0), (2, 10, 0)),
        ((3, 0, 0), (3, 0, 0), (3, 0, 0)),
        ((2, 10, 0), (2, 10, 1), (2, 10, 1)),
        ((2, 10, 99), (2, 11, 0), (2, 11, 0)),
        ((2, 10), (2, 10, 0), (2, 10, 0)),
        ((2, 10, True), (2, 10, 0), (2, 10, 0)),
    ],
    ids=[
        "bundled-below-floor",
        "new-os-library-old-compiled-clients",
        "old-runtime",
        "old-elementtree",
        "different-elementtree-micro",
        "different-capi-micro",
        "different-runtime-major",
        "unsupported-compile-major",
        "older-runtime-micro",
        "older-runtime-minor",
        "truncated-version",
        "noninteger-version-part",
    ],
)
def test_version_guard_refuses_below_floor_incoherent_or_incompatible_metadata(
    verifier, runtime, capi, elementtree
):
    with pytest.raises(ValueError):
        verifier.validate_versions(runtime, capi, elementtree)


@pytest.fixture
def paired_probe(tmp_path, monkeypatch, verifier):
    directory = tmp_path / "paired-extensions"
    directory.mkdir()
    for name in ("pyexpat", "_elementtree"):
        (directory / (name + ".synthetic.so")).write_bytes(b"unit metadata fixture; not an ELF")

    class Parser:
        version = "Expat 2.10.0"

    expat = SimpleNamespace(
        __file__=str(directory / "pyexpat.synthetic.so"), version_info=(2, 10, 0)
    )
    accelerator = SimpleNamespace(
        __file__=str(directory / "_elementtree.synthetic.so"),
        Element=real_etree.Element,
        XMLParser=Parser,
    )
    etree = SimpleNamespace(
        Element=accelerator.Element,
        XMLParser=accelerator.XMLParser,
        fromstring=real_etree.fromstring,
    )
    library = tmp_path / "libexpat.so.1.0.0"
    state = SimpleNamespace(
        directory=directory,
        expat=expat,
        accelerator=accelerator,
        etree=etree,
        capi={"magic": "pyexpat.expat_CAPI 1.1", "size": 24, "version": (2, 10, 0)},
        libraries={library: (2, 10, 0)},
    )
    modules = {"pyexpat": expat, "_elementtree": accelerator, "xml.etree.ElementTree": etree}
    # Replace this helper's import boundary, never global importlib behavior.
    monkeypatch.setattr(verifier, "importlib", SimpleNamespace(import_module=modules.__getitem__))
    monkeypatch.setattr(verifier, "_capi_info", lambda module: state.capi)
    monkeypatch.setattr(verifier, "_loaded_libraries", lambda: list(state.libraries))
    monkeypatch.setattr(verifier, "_library_version", state.libraries.__getitem__)
    return state


@pytest.mark.parametrize("runtime", [(2, 10, 0), (2, 10, 1)])
def test_real_probe_logic_accepts_expected_pair_and_newer_dynamic_micro(
    verifier, paired_probe, runtime
):
    state = paired_probe
    state.expat.version_info = runtime
    state.libraries.update(dict.fromkeys(state.libraries, runtime))
    result = verifier.probe(state.directory)
    assert result["status"] == "passed"
    assert result["runtime_version"] == runtime
    assert result["capi"]["version"] == result["elementtree_compile_version"] == (2, 10, 0)
    assert set(result["modules"]) == {"pyexpat", "_elementtree"}


@pytest.mark.parametrize(
    "fault",
    [
        "old-bundled-pair",
        "one-sided-elementtree",
        "mixed-directory",
        "element-fallback",
        "parser-fallback",
        "malformed-elementtree-version",
        "no-dynamic-library",
        "wrong-dynamic-library",
        "second-old-dynamic-library",
        "broken-ordinary-xml",
    ],
)
def test_real_probe_logic_refuses_partial_false_or_incompatible_pair(verifier, paired_probe, fault):
    state = paired_probe
    if fault == "old-bundled-pair":
        state.expat.version_info = (2, 9, 0)
        state.capi["version"] = (2, 9, 0)
        state.accelerator.XMLParser.version = "Expat 2.9.0"
    elif fault == "one-sided-elementtree":
        state.accelerator.XMLParser.version = "Expat 2.9.0"
    elif fault == "mixed-directory":
        state.accelerator.__file__ = str(state.directory.parent / "_elementtree.synthetic.so")
    elif fault == "element-fallback":
        state.etree.Element = object()
    elif fault == "parser-fallback":
        state.etree.XMLParser = object()
    elif fault == "malformed-elementtree-version":
        state.accelerator.XMLParser.version = "unknown expat"
    elif fault == "no-dynamic-library":
        state.libraries.clear()
    elif fault == "wrong-dynamic-library":
        state.libraries.update(dict.fromkeys(state.libraries, (2, 9, 0)))
    elif fault == "second-old-dynamic-library":
        state.libraries[state.directory / "libexpat.so.1.9.0"] = (2, 9, 0)
    elif fault == "broken-ordinary-xml":
        state.etree.fromstring = lambda text: [SimpleNamespace(text="wrong", attrib={})]
    with pytest.raises(ValueError):
        verifier.probe(state.directory)


def test_actual_capi_reader_refuses_a_non_capsule(verifier):
    with pytest.raises(ValueError):
        verifier._capi_info(SimpleNamespace(expat_CAPI=object()))


def test_actual_dynamic_loader_refuses_an_absent_library(verifier, tmp_path):
    with pytest.raises(OSError):
        verifier._library_version(tmp_path / "absent-libexpat.so.1")


@pytest.mark.parametrize(
    "failure", [ImportError, OSError, ValueError, TypeError, AttributeError, ctypes.ArgumentError]
)
def test_cli_refuses_import_abi_or_loader_failure_without_success_or_raw_details(
    verifier, monkeypatch, capsys, failure
):
    monkeypatch.setattr(sys, "argv", ["verify_system_expat.py"])

    def fail(directory):
        raise failure("private synthetic loader or ABI detail")

    monkeypatch.setattr(verifier, "probe", fail)
    assert verifier.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "System Expat XML extension verification failed.\n"


def test_cli_selects_staged_pair_before_probe_and_publishes_only_verified_status(
    verifier, monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(sys, "argv", ["verify_system_expat.py", "--directory", str(tmp_path)])
    monkeypatch.setattr(sys, "path", list(sys.path))
    seen = []

    def probe(directory):
        seen.append((directory, sys.path[0]))
        return {"status": "passed", "synthetic": True}

    monkeypatch.setattr(verifier, "probe", probe)
    assert verifier.main() == 0
    assert seen == [(tmp_path, str(tmp_path.resolve()))]
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"status": "passed", "synthetic": True}
    assert captured.err == ""


def test_source_download_checks_the_actual_bytes_before_returning(builder, monkeypatch, tmp_path):
    payload = b"synthetic archive bytes; no source code executed"
    seen = []

    def opened(url, timeout):
        seen.append((url, timeout))
        return io.BytesIO(payload)

    monkeypatch.setattr(builder, "urllib", SimpleNamespace(request=SimpleNamespace(urlopen=opened)))
    destination = tmp_path / "source.tar.xz"
    expected = hashlib.sha256(payload).hexdigest()
    assert builder.download_source("3.14.8", expected, destination).endswith(
        "/3.14.8/Python-3.14.8.tar.xz"
    )
    assert destination.read_bytes() == payload and len(seen) == 1
    with pytest.raises(ValueError, match="checksum"):
        builder.download_source("3.14.8", "0" * 64, destination)


@pytest.mark.parametrize(
    "version,checksum", [("3.15.0", "0" * 64), ("3.14.8+modified", "0" * 64), ("3.14.8", "bad")]
)
def test_unsupported_source_identity_is_refused_before_network(
    builder, monkeypatch, tmp_path, version, checksum
):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid source metadata must not trigger a download")

    monkeypatch.setattr(
        builder, "urllib", SimpleNamespace(request=SimpleNamespace(urlopen=forbidden))
    )
    with pytest.raises(ValueError):
        builder.download_source(version, checksum, tmp_path / "source.tar.xz")


@pytest.mark.parametrize("changed", [None, "Py_DEBUG", "Py_GIL_DISABLED", "SIZEOF_VOID_P"])
def test_real_abi_comparison_accepts_identical_and_refuses_changed_flags(
    builder, tmp_path, changed
):
    original = tmp_path / "original.h"
    rebuilt = tmp_path / "rebuilt.h"
    text = "#define Py_ENABLE_SHARED 1\n#define SIZEOF_VOID_P 8\n#define Py_DEBUG 0\n"
    original.write_text(text)
    rebuilt.write_text(text if changed is None else text + f"#define {changed} 1\n")
    if changed is None:
        result = builder.check_abi(original, rebuilt)
        assert all(row["before"] == row["after"] for row in result.values())
    else:
        with pytest.raises(ValueError, match="ABI"):
            builder.check_abi(original, rebuilt)


@pytest.mark.parametrize(
    "exit_code,body",
    [
        (0, '{"status":"passed"}'),
        (23, '{"status":"passed"}'),
        (0, '{"status":"failed"}'),
        (0, "not JSON"),
    ],
)
def test_fresh_child_verification_requires_successful_process_and_valid_result(
    builder, tmp_path, exit_code, body
):
    verifier = tmp_path / "synthetic_verifier.py"
    verifier.write_text(f"import sys\nprint({body!r})\nsys.exit({exit_code})\n")
    if exit_code:
        with pytest.raises(subprocess.CalledProcessError) as caught:
            builder.verify_pair(verifier, tmp_path)
        assert caught.value.returncode == exit_code
    elif body != '{"status":"passed"}':
        with pytest.raises(ValueError):
            builder.verify_pair(verifier, tmp_path)
    else:
        assert builder.verify_pair(verifier, tmp_path) == {"status": "passed"}


def test_verification_uses_a_fresh_isolated_no_bytecode_child(builder, tmp_path):
    verifier = tmp_path / "child_settings.py"
    verifier.write_text(
        "import sys,json\nprint(json.dumps({'status':'passed','isolated':sys.flags.isolated,"
        "'no_bytecode':sys.flags.dont_write_bytecode,'argv':sys.argv[1:]}))\n"
    )
    result = builder.verify_pair(verifier, tmp_path)
    assert result["isolated"] == result["no_bytecode"] == 1
    assert result["argv"] == ["--directory", str(tmp_path)]


@pytest.mark.parametrize(
    "exit_code", [0, 23], ids=["only-two-targets", "compiler-failure-propagates"]
)
def test_real_build_boundary_uses_only_two_targets_and_requires_make_success(
    builder, tmp_path, exit_code
):
    tools = tmp_path / "tools"
    tools.mkdir()
    observed = tmp_path / "make-observed.json"
    executable = tools / "make"
    executable.write_text(
        "#!/usr/bin/env python3\nimport json,os,sys\nfrom pathlib import Path\n"
        "Path(os.environ['EXPAT_TEST_MAKE_OBSERVATION']).write_text("
        "json.dumps({'argv':sys.argv[1:],'cwd':os.getcwd()}))\n"
        'print(\'{"status":"passed"}\')\n'
        "sys.exit(int(os.environ['EXPAT_TEST_MAKE_EXIT']))\n"
    )
    executable.chmod(0o755)
    source = tmp_path / "source"
    source.mkdir()
    suffix = sysconfig.get_config_var("EXT_SUFFIX")
    targets = ["Modules/" + name + suffix for name in ("pyexpat", "_elementtree")]
    env = {
        **os.environ,
        "PATH": str(tools) + os.pathsep + os.environ["PATH"],
        "EXPAT_TEST_MAKE_OBSERVATION": str(observed),
        "EXPAT_TEST_MAKE_EXIT": str(exit_code),
    }
    if exit_code:
        with pytest.raises(subprocess.CalledProcessError) as caught:
            builder.build_module_pair(source, suffix, env)
        assert caught.value.returncode == exit_code
    else:
        assert builder.build_module_pair(source, suffix, env) == targets
    assert json.loads(observed.read_text()) == {"argv": ["-j2", *targets], "cwd": str(source)}


@pytest.fixture
def pair_files(tmp_path):
    staged, destination = tmp_path / "staged", tmp_path / "installed"
    staged.mkdir()
    destination.mkdir()
    names = [name + sysconfig.get_config_var("EXT_SUFFIX") for name in ("pyexpat", "_elementtree")]
    for index, name in enumerate(names):
        (staged / name).write_bytes(("new-" + name).encode())
        (destination / name).write_bytes(("old-" + name).encode())
        (destination / name).chmod((0o640, 0o750)[index])
    return staged, destination, names


def _snapshot(directory):
    return {p.name: (p.read_bytes(), stat.S_IMODE(p.stat().st_mode)) for p in directory.iterdir()}


def test_both_modules_are_verified_before_replacement_and_original_modes_are_preserved(
    builder, monkeypatch, pair_files
):
    staged, destination, names = pair_files
    before = _snapshot(destination)
    seen = []

    def verify(verifier, directory=None):
        seen.append(directory)
        if directory is not None:
            assert directory == staged and _snapshot(destination) == before
        return {"status": "passed"}

    monkeypatch.setattr(builder, "verify_pair", verify)
    assert builder.install_pair(staged, destination, Path("synthetic-verifier")) == {
        "status": "passed"
    }
    assert seen == [staged, None]
    assert {name: (destination / name).read_bytes() for name in names} == {
        name: (staged / name).read_bytes() for name in names
    }
    assert {name: stat.S_IMODE((destination / name).stat().st_mode) for name in names} == {
        name: before[name][1] for name in names
    }


@pytest.mark.parametrize("fault", ["one-sided", "wrong-abi-tag", "extra-module"])
def test_partial_wrong_tag_or_extra_staged_modules_are_refused_before_verification(
    builder, monkeypatch, pair_files, fault
):
    staged, destination, names = pair_files
    before = _snapshot(destination)
    if fault == "one-sided":
        (staged / names[1]).unlink()
    elif fault == "wrong-abi-tag":
        (staged / names[0]).rename(staged / "pyexpat.wrong-abi.so")
    else:
        (staged / "unexpected.so").write_bytes(b"extra")

    def forbidden(*args, **kwargs):
        pytest.fail("wrong staged pair must fail before any verification or replacement")

    monkeypatch.setattr(builder, "verify_pair", forbidden)
    with pytest.raises(ValueError):
        builder.install_pair(staged, destination, Path("synthetic-verifier"))
    assert _snapshot(destination) == before


@pytest.mark.parametrize("phase", ["staged", "installed"])
def test_failed_pair_verification_never_returns_success_and_preserves_originals(
    builder, monkeypatch, pair_files, phase
):
    staged, destination, names = pair_files
    before = _snapshot(destination)

    def verify(verifier, directory=None):
        if (phase == "staged") == (directory is not None):
            raise ImportError("synthetic capsule-version mismatch")
        return {"status": "passed"}

    monkeypatch.setattr(builder, "verify_pair", verify)
    with pytest.raises(ImportError):
        builder.install_pair(staged, destination, Path("synthetic-verifier"))
    assert _snapshot(destination) == before


def test_second_file_replace_failure_rolls_back_both_and_propagates(
    builder, monkeypatch, pair_files
):
    staged, destination, names = pair_files
    before = _snapshot(destination)
    monkeypatch.setattr(builder, "verify_pair", lambda *args: {"status": "passed"})

    def replace(source, target):
        if Path(source).name == names[1] + ".new":
            raise OSError("synthetic second-file replacement failure")
        os.replace(source, target)

    monkeypatch.setattr(builder, "os", SimpleNamespace(replace=replace))
    with pytest.raises(OSError):
        builder.install_pair(staged, destination, Path("synthetic-verifier"))
    assert _snapshot(destination) == before


def test_other_interpreter_source_version_is_refused_before_build_io(builder, monkeypatch):
    different = ".".join(map(str, (*sys.version_info[:2], sys.version_info.micro + 1)))
    monkeypatch.setenv("PYTHON_VERSION", different)
    monkeypatch.setenv("PYTHON_SHA256", "0" * 64)
    with pytest.raises(ValueError, match="Source version must match"):
        builder.main()
