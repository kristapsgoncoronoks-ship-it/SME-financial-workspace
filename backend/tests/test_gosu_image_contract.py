"""Exercise the shipped Go BuildInfo guard and fail-fast Docker command wiring."""

import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "deploy/postgres/Dockerfile"
GUARD = ROOT / "deploy/postgres/verify-gosu-buildinfo.awk"
GUARD_TARGET = "/usr/local/share/gosu/verify-buildinfo.awk"
CAPTURE_TARGET = "/out/gosu-buildinfo.txt"
MOBY = "\tdep\tgithub.com/moby/sys/user\tv0.4.1\th1:RgjRlaDKi/Xmyrz4t8lyzXT6v2ooFeO/7xtchmhVWE0=\n"

# Public upstream hashes and realistic Go output shape; this is deliberately a
# synthetic parser control, not a claim that a replacement binary was built.
PUBLISHED_BUILDINFO = (
    "/out/gosu: go1.27.3\n"
    "\tpath\tgithub.com/tianon/gosu\n"
    "\tmod\tgithub.com/tianon/gosu\tv1.20.0\t\n"
    + MOBY
    + "\tdep\tgolang.org/x/sys\tv0.1.0\th1:kunALQeHf1/185U1i0GOB/fy1IPRDDpuoOOqRReG57U=\n"
    "\tbuild\t-buildmode=exe\n"
    "\tbuild\tCGO_ENABLED=0\n"
    "\tbuild\tGOARCH=amd64\n"
    "\tbuild\tGOOS=linux\n"
    "\tbuild\tvcs=git\n"
    "\tbuild\tvcs.revision=1.20\n"
    "\tbuild\tvcs.time=2026-10-25T09:43:55Z\n"
    "\tbuild\tvcs.modified=false\n"
)
# Synthetic current-toolchain protocol control: the historical header above
# remains negative evidence. This derived Go1.27.4 header is not an actual build.
VALID_BUILDINFO = (
    PUBLISHED_BUILDINFO.replace("\tv1.20.0\t", "\tv1.20.0+dirty\t")
    .replace("/out/gosu: go1.27.3\n", "/out/gosu: go1.27.4\n", 1)
    .replace(
        "v0.1.0\th1:kunALQeHf1/185U1i0GOB/fy1IPRDDpuoOOqRReG57U=",
        "v0.49.0\th1:XbzkgYJHdqh/8m2Uu0W/dQv8nktxx4BFHp1M0gROTXA=",
    )
    .replace("vcs.modified=false", "vcs.modified=true")
    .replace("GOARCH=amd64\n", "GOARCH=amd64\n\tbuild\tGOAMD64=v1\n")
)


@pytest.fixture(scope="module")
def guard():
    # A missing/syntactically broken awk must not masquerade as rejection of
    # invalid metadata. Positives require exit 0; semantic refusals require 1.
    assert GUARD.is_file(), "the actual image guard must exist"
    return GUARD


def _check(guard, metadata):
    return subprocess.run(
        ["awk", "-v", "expected_goarch=amd64", "-f", str(guard)],
        input=metadata,
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )


@pytest.mark.parametrize(
    "metadata",
    [
        VALID_BUILDINFO,
        "\n".join([VALID_BUILDINFO.splitlines()[0], *reversed(VALID_BUILDINFO.splitlines()[1:])])
        + "\n\tdep\texample.test/unrelated\tv2.0.0\th1:unrelated\n"
        + "\tbuild\t-unrelated-setting=retained\n",
    ],
    ids=["reviewed-overlay-output-shape", "reordered-with-unrelated-fields"],
)
def test_buildinfo_guard_accepts_expected_linked_module_and_build_records(guard, metadata):
    result = _check(guard, metadata)
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""


@pytest.mark.parametrize(
    "metadata",
    [
        PUBLISHED_BUILDINFO,
        VALID_BUILDINFO.replace("go1.27.4", "go1.27.3", 1),
        VALID_BUILDINFO.replace("v0.4.1", "v0.1.0"),
        VALID_BUILDINFO.replace(MOBY, ""),
        VALID_BUILDINFO + MOBY,
        VALID_BUILDINFO + MOBY.replace("v0.4.1", "v0.1.0"),
        VALID_BUILDINFO.replace(MOBY, MOBY + "\t=>\texample.test/old-user\tv0.1.0\n"),
        VALID_BUILDINFO.replace("\tpath\tgithub.com/tianon/gosu", "\tpath\texample.test/gosu"),
        VALID_BUILDINFO.replace("\tv1.20.0", "\tv1.19.0"),
        VALID_BUILDINFO + "\tpath\tgithub.com/tianon/gosu\n",
        VALID_BUILDINFO.replace("\tbuild\tvcs.revision=1.20\n", ""),
        VALID_BUILDINFO.replace("vcs.revision=1.20", "vcs.revision=1.19"),
        VALID_BUILDINFO.replace("2026-10-25T09:43:55Z", "2025-09-23T19:09:38Z"),
        VALID_BUILDINFO.replace("vcs.modified=true", "vcs.modified=false"),
        VALID_BUILDINFO.replace("GOOS=linux", "GOOS=windows"),
        VALID_BUILDINFO.replace("CGO_ENABLED=0", "CGO_ENABLED=1"),
        VALID_BUILDINFO + "\tbuild\tCGO_ENABLED=0\n",
        "\n".join(VALID_BUILDINFO.splitlines()[:3]) + "\n",
        VALID_BUILDINFO.replace(MOBY, "\tdep\tgithub.com/moby/sys/user\tv0.4.1\n"),
        VALID_BUILDINFO.replace("\tv1.20.0+dirty\t\n", "\tv1.20.0+dirty\th1:present\textra\n"),
        "\n".join(VALID_BUILDINFO.splitlines()[1:]) + "\n",
        "",
        VALID_BUILDINFO.replace(MOBY, "# " + MOBY.lstrip()),
        VALID_BUILDINFO.replace("\tbuild\tvcs.time=", "\tnote\tvcs.time="),
    ],
    ids=[
        "published-clean-release-is-not-overlay",
        "previous-toolchain-with-correct-overlay-is-not-current",
        "old-linked-dependency-with-new-main-version",
        "missing-linked-dependency",
        "duplicate-linked-dependency",
        "contradictory-linked-dependency",
        "replaced-linked-dependency",
        "wrong-main-path",
        "old-main-version",
        "duplicate-main-path",
        "missing-revision",
        "wrong-revision",
        "wrong-time",
        "falsely-clean-overlay",
        "wrong-platform",
        "cgo-reenabled",
        "duplicate-build-setting",
        "truncated-output",
        "missing-dependency-checksum-field",
        "malformed-main-module-record",
        "missing-header",
        "empty-output",
        "dependency-comment-decoy",
        "required-setting-in-wrong-record",
    ],
)
def test_buildinfo_guard_rejects_false_upgrade_and_invalid_evidence(guard, metadata):
    result = _check(guard, metadata)
    assert result.returncode == 1, result
    assert result.stdout == result.stderr == ""


def _metadata_fragment(text):
    """Extract only the two fixed commands, without interpreting general shell."""
    joined = re.sub(r"\\\r?\n[ \t]*", " ", text)
    run = next(
        line
        for line in joined.splitlines()
        if line.startswith("RUN ") and "go build -mod=readonly" in line
    )
    assert re.match(r"RUN (?:--mount=\S+\s+)*set -eu;", run)
    match = re.search(
        r"go version -m /out/gosu\s*>\s*/out/gosu-buildinfo\.txt;\s*"
        r'awk -v expected_goarch="\$TARGETARCH" -f '
        r"/usr/local/share/gosu/verify-buildinfo\.awk /out/gosu-buildinfo\.txt;",
        run,
    )
    assert match, "capture must succeed before the actual awk guard executes"
    assert match.end() < run.index("/out/gosu --version"), "verify before executing the helper"
    return match.group(0)


def _admit_source(text):
    assert re.findall(r"^FROM (\S+) AS (\S+)$", text, re.M) == [
        (
            "golang:1.27.4-alpine3.24@sha256:0a5f7fe796599ff487605987432e7619ae45beeb2fba4c2ce0b907879932aebf",
            "build",
        ),
        (
            "postgres:16.16-alpine3.24@sha256:0adbf25a9e96d38823bb813f3ac0ed41fadb665c88f1df7fe6dbfb060cbdc106",
            "runtime",
        ),
    ]
    for required in (
        "https://codeload.github.com/tianon/gosu/tar.gz/29b939aa5bd4dc588af765b8248f6e39150d2565",
        "2b70da8a5480fafedc16bb1fc91b572f064d03df736cbd91a616af2f1b66ab51  /tmp/gosu.tar.gz",
        "https://codeload.github.com/tianon/fake-git/tar.gz/aaa875961611764a86a21cf80c250929a7786179",
        "4eebe35bfc2842398d325c09167faa597cd4c29f7049ae239b13822d20dfbf88  /tmp/fake-git.tar.gz",
        "FAKEGIT_GO_SEMVER=v1.20.0",
        "FAKEGIT_GO_REVISION=1.20",
        "FAKEGIT_GO_TIMESTAMP=1792921435",
        "FAKEGIT_GO_MODIFIED=",
        "ARG TARGETARCH",
        "${TARGETARCH:?BuildKit TARGETARCH is required for gosu provenance}",
        "CGO_ENABLED=0 GOTOOLCHAIN=local go build -mod=readonly",
        "COPY --chmod=0644 verify-gosu-buildinfo.awk " + GUARD_TARGET,
        "cp /go/pkg/mod/github.com/moby/sys/user@v0.4.1/LICENSE",
        "cp /go/pkg/mod/golang.org/x/sys@v0.49.0/LICENSE",
        "cp /out/gosu-buildinfo.txt /out/licenses/gosu-buildinfo.txt",
        "'musl>=1.2.6-r3' 'musl-utils>=1.2.6-r3' 'zlib>=1.3.2-r1'",
    ):
        assert required in text, f"preserve reviewed source/build contract: {required}"
    assert text.index("COPY --chmod=0644 verify-gosu-buildinfo.awk") < text.index("go build")
    return _metadata_fragment(text)


def test_postgres_source_preserves_reviewed_pins_licenses_floors_and_guard_order():
    _admit_source(DOCKERFILE.read_text())


@pytest.mark.parametrize(
    "old,new",
    [
        ("set -eu;", "set -u;"),
        ("/out/gosu-buildinfo.txt; ", "/out/gosu-buildinfo.txt || true; "),
        (
            "go version -m /out/gosu > /out/gosu-buildinfo.txt;",
            "go version -m /out/gosu | tee /out/gosu-buildinfo.txt;",
        ),
        ("user@v0.4.1/LICENSE", "user@v0.1.0/LICENSE"),
    ],
    ids=["no-errexit", "ignored-capture-failure", "pipeline-capture", "stale-module-license"],
)
def test_source_admission_rejects_unsafe_capture_or_stale_license(old, new):
    text = re.sub(r"\\\r?\n[ \t]*", " ", DOCKERFILE.read_text())
    assert old in text
    with pytest.raises(AssertionError):
        _admit_source(text.replace(old, new))


@pytest.mark.parametrize("stage", ["build", "runtime"])
def test_parent_pin_in_comment_cannot_admit_a_changed_actual_parent(stage):
    text = DOCKERFILE.read_text()
    original = next(line for line in text.splitlines() if line.endswith(" AS " + stage))
    changed = "# " + original + "\nFROM unreviewed:mutable AS " + stage
    with pytest.raises(AssertionError):
        _admit_source(text.replace(original, changed))


def test_capture_and_guard_moved_after_helper_execution_are_refused():
    text = re.sub(r"\\\r?\n[ \t]*", " ", DOCKERFILE.read_text())
    fragment = _metadata_fragment(text)
    text = text.replace(fragment, "", 1).replace(
        "/out/gosu --version;", "/out/gosu --version; " + fragment, 1
    )
    with pytest.raises(AssertionError, match="verify before executing the helper"):
        _admit_source(text)


@pytest.mark.parametrize(
    "go_exit,metadata,expected",
    [
        (0, VALID_BUILDINFO, 0),
        (19, VALID_BUILDINFO, 19),
        (0, VALID_BUILDINFO.replace("v0.4.1", "v0.1.0"), 1),
    ],
    ids=[
        "valid-capture-and-guard",
        "failed-go-with-valid-looking-output",
        "false-upgrade-captured",
    ],
)
def test_actual_docker_capture_guard_fragment_propagates_failures(
    tmp_path, guard, go_exit, metadata, expected
):
    fragment = _admit_source(DOCKERFILE.read_text())
    capture = tmp_path / "captured-buildinfo.txt"
    supplied = tmp_path / "supplied-buildinfo.txt"
    supplied.write_text(metadata)
    executable = tmp_path / "go"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import os,sys\n"
        "from pathlib import Path\n"
        "sys.stdout.write(Path(os.environ['GOSU_TEST_INPUT']).read_text())\n"
        "sys.exit(int(os.environ['GOSU_TEST_GO_EXIT']))\n"
    )
    executable.chmod(0o755)
    fragment = fragment.replace(CAPTURE_TARGET, shlex.quote(str(capture))).replace(
        GUARD_TARGET, shlex.quote(str(guard))
    )
    marker = tmp_path / "downstream-executed"
    env = {
        **os.environ,
        "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
        "GOSU_TEST_INPUT": str(supplied),
        "GOSU_TEST_GO_EXIT": str(go_exit),
        "TARGETARCH": "amd64",
    }
    result = subprocess.run(
        ["sh", "-eu", "-c", fragment + " : > " + shlex.quote(str(marker))],
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == expected, result
    assert capture.read_text() == metadata, "the fake go actually emitted the supplied evidence"
    assert marker.exists() is (expected == 0)
