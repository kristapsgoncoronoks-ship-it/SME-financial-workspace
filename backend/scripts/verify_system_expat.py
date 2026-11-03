"""Verify the paired stdlib XML extensions against the supported system Expat."""

from __future__ import annotations

import argparse
import ctypes
import importlib
import json
import re
import sys
import sysconfig
from pathlib import Path

MINIMUM_EXPAT = (2, 10, 0)


def validate_versions(runtime: tuple, capi: tuple, elementtree: tuple) -> None:
    """Keep both compiled clients coherent; allow compatible runtime updates."""
    for version in (runtime, capi, elementtree):
        if (
            not isinstance(version, tuple)
            or len(version) != 3
            or any(type(part) is not int or part < 0 for part in version)
        ):
            raise ValueError("Invalid Expat version metadata")
    if capi < MINIMUM_EXPAT or capi[0] != MINIMUM_EXPAT[0] or elementtree != capi:
        raise ValueError("The compiled XML extension pair is incompatible")
    if runtime[0] != capi[0] or runtime < capi:
        raise ValueError("The shared Expat runtime is incompatible")


def _capi_info(expat) -> dict:
    # This is the prefix declared by the same pinned CPython Include/pyexpat.h.
    # Importing accelerated _elementtree separately checks its full CAPI size.
    class Header(ctypes.Structure):
        _fields_ = [
            ("magic", ctypes.c_char_p),
            ("size", ctypes.c_int),
            ("major", ctypes.c_int),
            ("minor", ctypes.c_int),
            ("micro", ctypes.c_int),
        ]

    pointer = ctypes.pythonapi.PyCapsule_GetPointer
    pointer.argtypes = (ctypes.py_object, ctypes.c_char_p)
    pointer.restype = ctypes.c_void_p
    address = pointer(expat.expat_CAPI, b"pyexpat.expat_CAPI")
    if not address:
        raise ValueError("Missing pyexpat CAPI")
    header = Header.from_address(address)
    if header.magic != b"pyexpat.expat_CAPI 1.1" or header.size < ctypes.sizeof(Header):
        raise ValueError("Unsupported pyexpat CAPI")
    return {
        "magic": header.magic.decode(),
        "size": header.size,
        "version": (header.major, header.minor, header.micro),
    }


def _loaded_libraries() -> list[Path]:
    paths = {
        line.split()[-1]
        for line in Path("/proc/self/maps").read_text().splitlines()
        if line.split() and re.fullmatch(r"libexpat\.so\.1(?:\.\d+)*", Path(line.split()[-1]).name)
    }
    return [Path(path) for path in sorted(paths)]


def _library_version(path: Path) -> tuple[int, int, int]:
    class Version(ctypes.Structure):
        _fields_ = [("major", ctypes.c_int), ("minor", ctypes.c_int), ("micro", ctypes.c_int)]

    library = ctypes.CDLL(str(path))
    function = library.XML_ExpatVersionInfo
    function.argtypes = ()
    function.restype = Version
    version = function()
    return (version.major, version.minor, version.micro)


def probe(expected_directory: Path | None = None) -> dict:
    expat = importlib.import_module("pyexpat")
    accelerator = importlib.import_module("_elementtree")
    etree = importlib.import_module("xml.etree.ElementTree")
    expected_directory = (
        expected_directory or Path(sysconfig.get_config_var("DESTSHARED"))
    ).resolve()
    files = {
        name: Path(module.__file__).resolve()
        for name, module in (("pyexpat", expat), ("_elementtree", accelerator))
    }
    if any(path.parent != expected_directory for path in files.values()):
        raise ValueError("XML extensions do not come from the expected paired directory")
    if etree.Element is not accelerator.Element or etree.XMLParser is not accelerator.XMLParser:
        raise ValueError("The stdlib XML accelerator is not selected")
    capi = _capi_info(expat)
    # The accelerated CPython getter uses XML_*_VERSION compile-time macros.
    match = re.fullmatch(r"Expat (\d+)\.(\d+)\.(\d+)", etree.XMLParser().version)
    if not match:
        raise ValueError("Invalid accelerated ElementTree version")
    compiled = tuple(map(int, match.groups()))
    runtime = tuple(expat.version_info)
    validate_versions(runtime, tuple(capi["version"]), compiled)
    libraries = _loaded_libraries()
    if not libraries or any(_library_version(path) != runtime for path in libraries):
        raise ValueError("A matching dynamic system Expat library is not loaded")
    sample = etree.fromstring("<root><item currency='EUR'>caf\u00e9 &amp; tea</item></root>")
    if sample[0].text != "caf\u00e9 & tea" or sample[0].attrib != {"currency": "EUR"}:
        raise ValueError("Ordinary XML compatibility check failed")
    return {
        "status": "passed",
        "python": sys.version.split()[0],
        "modules": {name: str(path) for name, path in files.items()},
        "runtime_version": runtime,
        "capi": capi,
        "elementtree_compile_version": compiled,
        "dynamic_libraries": [str(path) for path in libraries],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path)
    args = parser.parse_args()
    if args.directory:
        sys.path.insert(0, str(args.directory.resolve()))
    try:
        result = probe(args.directory)
    except (ImportError, OSError, ValueError, TypeError, AttributeError, ctypes.ArgumentError):
        sys.stderr.write("System Expat XML extension verification failed.\n")
        return 1
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
