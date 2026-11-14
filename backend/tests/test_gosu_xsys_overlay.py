"""Exercise the actual overlay verifier and checksum/copy admission seam.

Historical fixture files are actual task-local Go1.27.3 prototype output, with
ONLY their binary-path headers normalized to /out/gosu. Current protocol controls
derive a synthetic Go1.27.4 header; they do not claim a new binary or image build.
Shell controls execute only the extracted four-check/two-copy seam in an isolated
temporary tree: no downloads, Go compiler or Docker operation.
"""

import re
import shlex
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CONTEXT = ROOT / "deploy/postgres"
GUARD = CONTEXT / "verify-gosu-buildinfo.awk"
DOCKERFILE = CONTEXT / "Dockerfile"
FIXTURES = Path(__file__).parent / "fixtures"
HISTORICAL_CANDIDATE = (FIXTURES / "gosu_xsys_candidate_buildinfo.txt").read_text()
CANDIDATE = HISTORICAL_CANDIDATE.replace("/out/gosu: go1.27.3\n", "/out/gosu: go1.27.4\n", 1)
BASELINE = (FIXTURES / "gosu_xsys_baseline_buildinfo.txt").read_text()
RECORDS = {
    "header": "/out/gosu:",
    "path": "path\tgithub.com/tianon/gosu",
    "main": "mod\tgithub.com/tianon/gosu",
    "moby": "dep\tgithub.com/moby/sys/user",
    "xsys": "dep\tgolang.org/x/sys",
    "CGO": "build\tCGO_ENABLED=",
    "GOOS": "build\tGOOS=",
    "GOARCH": "build\tGOARCH=",
    "GOAMD64": "build\tGOAMD64=",
    "revision": "build\tvcs.revision=",
    "time": "build\tvcs.time=",
    "modified": "build\tvcs.modified=",
}

ORIGINAL_MOD = b"module github.com/tianon/gosu\n\ngo 1.20\n\nrequire (\n\tgithub.com/moby/sys/user v0.4.1\n\tgolang.org/x/sys v0.1.0\n)\n"
ORIGINAL_SUM = b"github.com/moby/sys/user v0.4.1 h1:RgjRlaDKi/Xmyrz4t8lyzXT6v2ooFeO/7xtchmhVWE0=\ngithub.com/moby/sys/user v0.4.1/go.mod h1:E9QsW5WRe1kUAf7kW8hXKwu1uhsZEAdPLYHYSDudF4Y=\ngolang.org/x/sys v0.1.0 h1:kunALQeHf1/185U1i0GOB/fy1IPRDDpuoOOqRReG57U=\ngolang.org/x/sys v0.1.0/go.mod h1:oPkhp1MJrh7nUepCBck5+mAzfO9JrbApNNgaTdGDITg=\n"
LICENSE_NOTICE = b'Copyright 2009 The Go Authors.\n\nRedistribution and use in source and binary forms, with or without\nmodification, are permitted provided that the following conditions are\nmet:\n\n   * Redistributions of source code must retain the above copyright\nnotice, this list of conditions and the following disclaimer.\n   * Redistributions in binary form must reproduce the above\ncopyright notice, this list of conditions and the following disclaimer\nin the documentation and/or other materials provided with the\ndistribution.\n   * Neither the name of Google LLC nor the names of its\ncontributors may be used to endorse or promote products derived from\nthis software without specific prior written permission.\n\nTHIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS\n"AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT\nLIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR\nA PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT\nOWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,\nSPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT\nLIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,\nDATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY\nTHEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT\n(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE\nOF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.\n'

EXPECTED_CHECKS = {
    "/src/go.mod": "216dbdc31af7371e368b90a85837c6dcf8aa7139cb449ecceaa0526d91900c3a",
    "/src/go.sum": "3f4d244f09dc5009ad68d99b2d99815f48fddf693fad8e734bbe820b315f43e7",
    "/tmp/gosu-go.mod": "4632cb07b230b1caee8f377dae2f28e5d66757d1867d3d783aa84166620ff4f8",
    "/tmp/gosu-go.sum": "4c5ca63607e11c08110f8b8326552b4c62090a736ecf8ce03188db600dedc515",
}


def check(metadata, expected_goarch="amd64"):
    command = ["awk"]
    if expected_goarch is not None:
        command += ["-v", "expected_goarch=" + expected_goarch]
    return subprocess.run(
        [*command, "-f", str(GUARD)],
        input=metadata,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )


@pytest.mark.parametrize("reordered", [False, True])
def test_synthetic_current_toolchain_modified_candidate_is_admitted(reordered):
    metadata = CANDIDATE
    if reordered:
        lines = metadata.splitlines()
        metadata = "\n".join([lines[0], *reversed(lines[1:])]) + "\n"
        metadata += "\tdep\texample.test/unrelated\tv2.0.0\th1:unrelated\n"
    result = check(metadata)
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""


def test_actual_unmodified_old_dependency_build_is_refused():
    result = check(BASELINE)
    assert result.returncode == 1, result.stderr
    assert result.stdout == result.stderr == ""


def test_historical_correct_overlay_with_previous_toolchain_is_refused():
    assert HISTORICAL_CANDIDATE.startswith("/out/gosu: go1.27.3\n")
    assert CANDIDATE.startswith("/out/gosu: go1.27.4\n")
    result = check(HISTORICAL_CANDIDATE)
    assert result.returncode == 1, result.stderr
    assert result.stdout == result.stderr == ""


@pytest.mark.parametrize("record", RECORDS)
@pytest.mark.parametrize("mutation", ["missing", "duplicate"])
def test_every_required_record_is_single_and_present(record, mutation):
    matching = [
        line
        for line in CANDIDATE.splitlines(keepends=True)
        if line.strip().startswith(RECORDS[record])
    ]
    assert len(matching) == 1
    line = matching[0]
    metadata = CANDIDATE.replace(line, "") if mutation == "missing" else CANDIDATE + line
    result = check(metadata)
    assert result.returncode == 1, result.stderr
    assert result.stdout == result.stderr == ""


@pytest.mark.parametrize(
    "old,new",
    [
        ("go1.27.4", "go1.27.3"),
        ("go1.27.4", "go1.27.2"),
        ("v1.20.0+dirty", "v1.20.0"),
        ("vcs.modified=true", "vcs.modified=false"),
        ("GOARCH=amd64", "GOARCH=arm64"),
        ("GOAMD64=v1", "GOAMD64=v3"),
        ("GOOS=linux", "GOOS=windows"),
        ("CGO_ENABLED=0", "CGO_ENABLED=1"),
        ("vcs.revision=1.20", "vcs.revision=1.19"),
        ("vcs.time=2026-10-25T09:43:55Z", "vcs.time=2026-10-26T09:43:55Z"),
        ("v0.49.0", "v0.1.0"),
        ("v0.4.1", "v0.1.0"),
        ("h1:XbzkgYJHdqh/8m2Uu0W/dQv8nktxx4BFHp1M0gROTXA=", "h1:wrong"),
        ("h1:RgjRlaDKi/Xmyrz4t8lyzXT6v2ooFeO/7xtchmhVWE0=", "h1:wrong"),
    ],
)
def test_contradictory_identity_platform_and_checksum_are_refused(old, new):
    assert old in CANDIDATE
    result = check(CANDIDATE.replace(old, new))
    assert result.returncode == 1, result.stderr
    assert result.stdout == result.stderr == ""


def test_replacements_are_refused_even_with_all_expected_records():
    result = check(CANDIDATE + "\t=>\texample.test/override\tv0.49.0\n")
    assert result.returncode == 1, result.stderr
    assert result.stdout == result.stderr == ""


@pytest.mark.parametrize("expected_goarch", [None, "", "arm-64", "a.*"])
def test_guard_requires_an_explicit_valid_build_target(expected_goarch):
    metadata = CANDIDATE
    if expected_goarch:
        # Align the forged record so a mismatch cannot hide target validation.
        metadata = metadata.replace("GOARCH=amd64", "GOARCH=" + expected_goarch)
        metadata = metadata.replace("\tbuild\tGOAMD64=v1\n", "")
    result = check(metadata, expected_goarch)
    assert result.returncode == 1, result.stderr
    assert result.stdout == result.stderr == ""


def test_matching_non_amd64_declaration_is_protocol_consistent():
    # Synthetic BuildInfo protocol control only; no ARM build/runtime claim.
    metadata = CANDIDATE.replace("GOARCH=amd64", "GOARCH=arm64")
    metadata = metadata.replace("\tbuild\tGOAMD64=v1\n", "")
    result = check(metadata, "arm64")
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""


def test_non_amd64_declaration_rejects_an_amd64_setting():
    metadata = CANDIDATE.replace("GOARCH=amd64", "GOARCH=arm64")
    result = check(metadata, "arm64")
    assert result.returncode == 1, result.stderr
    assert result.stdout == result.stderr == ""


def overlay_fragment():
    source = DOCKERFILE.read_text().replace("\\\n", " ")
    pattern = r"echo\s+(['\"])([0-9a-f]{64}) +(/src/go\.(?:mod|sum)|/tmp/gosu-go\.(?:mod|sum))\1\s*\|\s*sha256sum\s+-c\s+-"
    checks = list(re.finditer(pattern, source))
    assert len(checks) == 4, "all four immutable source/overlay inputs must be authenticated"
    assert {m.group(3): m.group(2) for m in checks} == EXPECTED_CHECKS
    copies = [
        re.search(r"\bcp +/tmp/gosu-go\." + ext + r" +/src/go\." + ext, source)
        for ext in ["mod", "sum"]
    ]
    assert all(copies), "both authenticated manifests must replace the source inputs"
    first_copy, last_copy = copies
    assert all(m.end() < first_copy.start() for m in checks)
    assert first_copy.end() < last_copy.start() < source.index("go build")
    fragment = source[checks[0].start() : last_copy.end()]
    statements = [s.strip() for s in fragment.split(";") if s.strip()]
    assert len(statements) == 6, "execute only four checks and two copies, no other build operation"
    for statement in statements[:4]:
        assert re.fullmatch(pattern, statement)
    assert statements[4:] == ["cp /tmp/gosu-go.mod /src/go.mod", "cp /tmp/gosu-go.sum /src/go.sum"]
    return fragment


def overlay_input(target):
    for line in DOCKERFILE.read_text().splitlines():
        if not line.startswith("COPY "):
            continue
        tokens = shlex.split(line)
        if tokens[-1] == target:
            source = CONTEXT / tokens[-2]
            assert source.is_file()
            return source.read_bytes()
    raise AssertionError("missing immutable overlay COPY for " + target)


@pytest.mark.parametrize("tamper", [None, *EXPECTED_CHECKS])
def test_checksum_admission_is_fail_closed_before_any_source_copy(tmp_path, tamper):
    fragment = overlay_fragment()
    payloads = {
        "/src/go.mod": ORIGINAL_MOD,
        "/src/go.sum": ORIGINAL_SUM,
        "/tmp/gosu-go.mod": overlay_input("/tmp/gosu-go.mod"),
        "/tmp/gosu-go.sum": overlay_input("/tmp/gosu-go.sum"),
    }
    paths = {}
    for logical, contents in payloads.items():
        local = tmp_path / logical.lstrip("/")
        local.parent.mkdir(exist_ok=True, parents=True)
        local.write_bytes(contents + (b"corrupt\n" if logical == tamper else b""))
        paths[logical] = local
    before = {key: paths[key].read_bytes() for key in ["/src/go.mod", "/src/go.sum"]}
    for logical, local in sorted(paths.items(), key=lambda item: -len(item[0])):
        fragment = fragment.replace(logical, shlex.quote(str(local)))
    result = subprocess.run(
        ["sh", "-eu", "-c", fragment], capture_output=True, text=True, timeout=5, check=False
    )
    if tamper:
        assert result.returncode != 0
        assert {key: paths[key].read_bytes() for key in before} == before
    else:
        assert result.returncode == 0, result.stderr
        assert paths["/src/go.mod"].read_bytes() == payloads["/tmp/gosu-go.mod"]
        assert paths["/src/go.sum"].read_bytes() == payloads["/tmp/gosu-go.sum"]


@pytest.mark.parametrize("tamper", [False, True])
def test_source_and_actual_new_license_notice_are_authenticated(tmp_path, tamper):
    overlay_fragment()
    source = DOCKERFILE.read_text().replace("\\\n", " ")
    notice = "/go/pkg/mod/golang.org/x/sys@v0.49.0/LICENSE"
    digest = "911f8f5782931320f5b8d1160a76365b83aea6447ee6c04fa6d5591467db9dad"
    check_pattern = (
        r"echo\s+(['\"])" + digest + r" +" + re.escape(notice) + r"\1\s*\|\s*sha256sum\s+-c\s+-"
    )
    checks = list(re.finditer(check_pattern, source))
    copies = list(
        re.finditer(
            r"\bcp +" + re.escape(notice) + r" +/out/licenses/golang-x-sys-BSD-3-Clause", source
        )
    )
    assert len(checks) == len(copies) == 1
    assert checks[0].end() < copies[0].start()
    assert "@v0.1.0/LICENSE" not in source
    local = tmp_path / "LICENSE"
    copied = tmp_path / "installed-notice"
    local.write_bytes(LICENSE_NOTICE + (b"altered\n" if tamper else b""))
    copied.write_bytes(b"previous notice")
    fragment = checks[0].group() + "; " + copies[0].group()
    fragment = fragment.replace(notice, shlex.quote(str(local)))
    fragment = fragment.replace("/out/licenses/golang-x-sys-BSD-3-Clause", shlex.quote(str(copied)))
    result = subprocess.run(
        ["sh", "-eu", "-c", fragment], capture_output=True, text=True, timeout=5, check=False
    )
    if tamper:
        assert result.returncode != 0
        assert copied.read_bytes() == b"previous notice"
    else:
        assert result.returncode == 0, result.stderr
        assert copied.read_bytes() == LICENSE_NOTICE
