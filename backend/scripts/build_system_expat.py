"""Rebuild the two XML extensions from this pinned interpreter's released source."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ABI_KEYS = (
    "Py_DEBUG",
    "Py_GIL_DISABLED",
    "Py_TRACE_REFS",
    "WITH_PYMALLOC",
    "WITH_MIMALLOC",
    "Py_ENABLE_SHARED",
    "SIZEOF_VOID_P",
    "SIZEOF_LONG",
    "SIZEOF_SIZE_T",
    "SIZEOF_WCHAR_T",
    "ALIGNOF_LONG",
    "ALIGNOF_SIZE_T",
    "PYLONG_BITS_IN_DIGIT",
)
MAKE_KEYS = (
    "SOABI",
    "EXT_SUFFIX",
    "CC",
    "PY_CFLAGS",
    "PY_CFLAGS_NODIST",
    "CCSHARED",
    "BLDSHARED",
    "LDFLAGS",
    "MODULE_PYEXPAT_CFLAGS",
    "MODULE_PYEXPAT_LDFLAGS",
    "MODULE_PYEXPAT_DEPS",
    "MODULE__ELEMENTTREE_CFLAGS",
    "MODULE__ELEMENTTREE_LDFLAGS",
    "MODULE__ELEMENTTREE_DEPS",
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def download_source(version: str, checksum: str, destination: Path) -> str:
    if not re.fullmatch(r"3\.14\.\d+", version) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise ValueError("Unsupported pinned Python source metadata")
    url = f"https://www.python.org/ftp/python/{version}/Python-{version}.tar.xz"
    size = 0
    with urllib.request.urlopen(url, timeout=90) as response, destination.open("wb") as target:
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > 64 * 1024 * 1024:
                raise ValueError("Python source archive exceeds the bounded size")
            target.write(chunk)
    if digest(destination) != checksum:
        raise ValueError("Python source archive checksum does not match the pinned base")
    return url


def check_abi(original: Path, rebuilt: Path) -> dict:
    def macros(path):
        return dict(re.findall(r"^#define\s+(\w+)\s+(.+)$", path.read_text(), re.M))

    before, after = macros(original), macros(rebuilt)
    compared = {key: {"before": before.get(key), "after": after.get(key)} for key in ABI_KEYS}
    if any(row["before"] != row["after"] for row in compared.values()):
        raise ValueError("Rebuilt XML extension ABI does not match this interpreter")
    return compared


def build_module_pair(source: Path, suffix: str, env: dict) -> list[str]:
    targets = ["Modules/" + name + suffix for name in ("pyexpat", "_elementtree")]
    subprocess.run(["make", "-j2", *targets], cwd=source, env=env, check=True, timeout=240)
    return targets


def verify_pair(verifier: Path, directory: Path | None = None) -> dict:
    command = [sys.executable, "-I", "-B", str(verifier)]
    if directory is not None:
        command.extend(["--directory", str(directory)])
    result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=30)
    observed = json.loads(result.stdout)
    if observed.get("status") != "passed":
        raise ValueError("XML extension verification did not complete")
    return observed


def install_pair(staged: Path, destination: Path, verifier: Path) -> dict:
    """Verify first; replace each file atomically within one transactional RUN."""
    suffix = sysconfig.get_config_var("EXT_SUFFIX")
    names = ("pyexpat" + suffix, "_elementtree" + suffix)
    if {p.name for p in staged.iterdir()} != set(names):
        raise ValueError("Staging directory must contain exactly the two XML extensions")
    verify_pair(verifier, staged)
    with tempfile.TemporaryDirectory(prefix=".system-expat-", dir=destination) as temporary:
        backup = Path(temporary)
        for name in names:
            shutil.copy2(destination / name, backup / name)
            shutil.copy2(staged / name, backup / (name + ".new"))
            (backup / (name + ".new")).chmod(stat.S_IMODE((destination / name).stat().st_mode))
        try:
            for name in names:
                os.replace(backup / (name + ".new"), destination / name)
            return verify_pair(verifier)
        except BaseException:
            for name in names:
                os.replace(backup / name, destination / name)
            raise


def main() -> None:
    version, checksum = os.environ["PYTHON_VERSION"], os.environ["PYTHON_SHA256"]
    if version != ".".join(map(str, sys.version_info[:3])):
        raise ValueError("Source version must match the running pinned interpreter")
    original = {
        key: sysconfig.get_config_var(key)
        for key in (
            "SOABI",
            "EXT_SUFFIX",
            "CONFIG_ARGS",
            "CONFIGURE_CFLAGS",
            "LDFLAGS",
            "LIBDIR",
            "LDLIBRARY",
        )
    }
    destination = Path(sysconfig.get_config_var("DESTSHARED"))
    suffix = original["EXT_SUFFIX"]
    names = ("pyexpat" + suffix, "_elementtree" + suffix)
    untouched = {
        path: digest(path)
        for path in (
            Path(sys.executable).resolve(),
            Path(original["LIBDIR"]) / original["LDLIBRARY"],
            *(p for p in destination.glob("*.so") if p.name not in names),
        )
    }
    original_pair = {name: digest(destination / name) for name in names}
    verifier = Path(__file__).with_name("verify_system_expat.py")
    output = Path("/opt/python-system-expat")
    modules = output / "lib-dynload"
    provenance = output / "provenance"
    modules.mkdir(parents=True, exist_ok=True)
    provenance.mkdir(parents=True, exist_ok=True)
    Path("/usr/src").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="python-system-expat-", dir="/usr/src") as temporary:
        temporary = Path(temporary)
        archive = temporary / "source.tar.xz"
        url = download_source(version, checksum, archive)
        with tarfile.open(archive) as source:
            source.extractall(temporary, filter="data")
        source = temporary / ("Python-" + version)
        args = [
            str(source / "configure"),
            *shlex.split(original["CONFIG_ARGS"]),
            "--with-system-expat",
        ]
        env = dict(os.environ)
        env["LDFLAGS"] = original["LDFLAGS"] or ""
        if original["CONFIGURE_CFLAGS"]:
            env["CFLAGS"] = original["CONFIGURE_CFLAGS"]
        else:
            env.pop("CFLAGS", None)
        subprocess.run(args, cwd=source, env=env, check=True, timeout=240)
        inspection = source / "inspect-module-config.mk"
        inspection.write_text(
            ".PHONY: inspect-module-config\ninspect-module-config:\n"
            + "".join(f"\t$(info {key}=$({key}))\n" for key in MAKE_KEYS)
            + "\t@:\n"
        )
        inspected = subprocess.run(
            [
                "make",
                "--no-print-directory",
                "-s",
                "-f",
                "Makefile",
                "-f",
                str(inspection),
                "inspect-module-config",
            ],
            cwd=source,
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        configured = dict(line.split("=", 1) for line in inspected.stdout.splitlines())
        if any(configured[key] != original[key] for key in ("SOABI", "EXT_SUFFIX")):
            raise ValueError("Configured XML extension tag does not match the interpreter")
        abi = check_abi(Path(sysconfig.get_config_h_filename()), source / "pyconfig.h")
        targets = build_module_pair(source, suffix, env)
        for name in names:
            shutil.copy2(source / "Modules" / name, modules / name)
        observed = install_pair(modules, destination, verifier)
        preserved_after = {path: digest(path) for path in untouched}
        if preserved_after != untouched:
            raise ValueError("The interpreter or another stdlib extension changed")
        shutil.copy2(source / "LICENSE", provenance / "CPython-LICENSE")
        metadata = {
            "python": version,
            "source_url": url,
            "source_sha256": checksum,
            "original_configuration": original,
            "configure_arguments": args[1:],
            "configured_module_settings": configured,
            "ABI_comparison": abi,
            "make_targets": targets,
            "staged_module_sha256": {name: digest(modules / name) for name in names},
            "original_pair_sha256": original_pair,
            "installed_pair_sha256": {name: digest(destination / name) for name in names},
            "preserved_native_sha256_before": {
                str(path): value for path, value in untouched.items()
            },
            "preserved_native_sha256_after": {
                str(path): value for path, value in preserved_after.items()
            },
            "preserved_native_files_unchanged": True,
            "verification": observed,
            "install_semantics": "Each file atomically replaced after paired verification; failed RUN is never committed",
        }
        (provenance / "build.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n"
        )


if __name__ == "__main__":
    main()
