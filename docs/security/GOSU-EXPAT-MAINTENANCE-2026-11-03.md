# Gosu and complete Expat maintenance — 3 November 2026

Status: implemented and locally qualified. The final ten-role image audit passes.
Fresh hosted CI for the delivered commit
and human/Lead review remain required. The failed intermediate inventory remains
separate from the repaired candidate.

Base is qualified draft PR #100, source
`c904bc77829a73a23eab7d520145f89a4d0e834b`. Main is unchanged at `90891d8`.
Development uses branch `fix/gosu-helper-maintenance-20261103` in the existing
checkout. No merge, deployment, repository setting, provider action or production
migration is authorized by this report.

## Confirmed findings

| Finding | Source evidence and supported repair |
| --- | --- |
| CVE-2026-61801 | Published gosu 1.20 moves linked `moby/sys/user` from 0.1.0 to fixed 0.4.1. Rebuild the verified release with the existing patched Go compiler. |
| CVE-2026-113351, CVE-2026-115506, CVE-2026-117610, CVE-2026-118370 | Fresh full image auditing finds all four Expat IDs in backend runtime, frontend runtime and PgBouncer build. Official Alpine v3.24 secdb, x86_64 APKINDEX and authoritative APKBUILD assign all four to fixed `libexpat 2.10.0-r0`. |
| Bundled Python Expat | The current Python 3.14.8 extension reports Expat 2.8.5 and has no dynamic Expat mapping. Immutable CPython release-source review confirms the four fixes are absent in 3.14.8, 3.14.9 and published 3.14.10. An OS-only update or decorative newer parent does not repair that separate copy. |

The initial new-head inventory is genuinely blocked: twelve IDs / twenty-one
advisory-role rows, including six selected HIGH rows. Gosu removes its old ID
from two PostgreSQL roles, but newly published Expat adds four IDs / twelve
rows in unchanged roles. The new findings are not established gosu regressions.
The original failed summary, twenty reports and hashes remain preserved.

## Gosu source and real binary identity

The source commit is `29b939aa5bd4dc588af765b8248f6e39150d2565`; archive SHA-256 is
`2b70da8a5480fafedc16bb1fc91b572f064d03df736cbd91a616af2f1b66ab51`.
Official release metadata, archive bytes and module ZIP/.mod content hashes were
independently refreshed. The source commit is unsigned; no signature claim is
made. Gosu and Moby remain Apache-2.0. The current Go/compiler/base/fake-git pins,
CGO-disabled build, source verification and PostgreSQL runtime contract remain.

The build captures successful `go version -m` output before parsing it under
`set -eu`. A build-only POSIX awk guard checks the real linked Moby dependency,
main module, release revision/time and build settings, refusing missing,
duplicate, replaced or contradictory relevant records. It does not authenticate
arbitrary hostile text; pinned source/checksums and Go module verification are
separate layers. Upstream-style VCS revision is release tag `1.20`, not a falsely
stamped source commit. Successful BuildInfo and matching module licenses are
retained with the delivered helper.

## Expat OS and Python boundary

The three affected existing APK seams require `libexpat>=2.10.0-r0`; the Python
builder also requires matching fixed `libexpat` and `expat-dev`. The MIT
license and `libexpat.so.1` SONAME remain. The authoritative Alpine GitLab API,
binary index and secdb agree; a lagging GitHub mirror and blocked web retrievals
are recorded as discrepancies rather than substituted for current evidence.

For Python, official `--with-system-expat` configuration supports a coherent
build of both `pyexpat` and `_elementtree`. ElementTree checks the exact compiled
Expat major/minor/micro against its capsule: replacing only `pyexpat.so` is unsafe.
The implemented build uses the same released Python 3.14.8 source, verified
against the parent image's `PYTHON_SHA256`, original configure/compiler/ABI
settings and only the two generated module targets. It dynamically uses fixed
Alpine Expat without replacing the interpreter or unrelated stdlib modules.

The helper validates the source checksum, interpreter version, SOABI, extension
suffix and ABI macros before building only the two generated module targets.
An isolated process verifies the staged pair before replacement and the installed
pair afterward. Build failures propagate; failed installation restores the original
files and fails the Docker layer. Each file replacement is atomic, not a live
two-file filesystem transaction. Hashes prove the interpreter, shared Python
library and every unrelated native stdlib extension remain unchanged.

Both image stages use the fixed pair. After every runtime COPY and `USER appuser`,
the final image build verifies actual imports, the accelerated ElementTree CAPI,
the fixed compiled/runtime versions and a loaded system library. Compatible newer
runtime micro releases are accepted. Original source, configuration, licenses,
module hashes and verification outcomes remain in the image provenance directory.

This final nonroot gate exposed a real integration failure: Docker's explicit
0644 COPY mode also created the public helper directory without traversal bits.
The failed build is preserved. Only the two public helper/license directories
receive 0755 in the corrected build; private application storage remains 0700.

This pair becomes a maintained build boundary. A future Python parent update must
verify its released source, embedded Expat fixes, ABI and actual XML behavior;
remove the overlay only when a separately qualified published parent supplies the
fixed coherent pair. A floating upstream branch or a higher Python version alone
does not meet that condition. The existing parent digest is deliberately retained.

## Executed evidence and remaining qualification

Gosu's two images build and nine grouped runtime checks pass: both named/numeric
UID/GID 70, absent-user refusal, PostgreSQL binary/entrypoint preservation, fresh
initialization, and exact data/role/permission recovery from a volume initialized
by the preceding image. All owned fixtures and temporary credentials are removed.

The final focused cohort passes 196 cases: the preserved ninety image-audit cases,
twelve additive Expat image controls, thirty-five Gosu controls and fifty-nine
system-Expat controls. Full backend collection is 5,447. Original assertions and
parser functions remain; valid APK fixtures gain the required fixed package.
Old metadata, a spoofed main version with the old dependency, masked producer
failure, a weakened Expat floor, an overstrict micro check, ignored make failure
and rollback reported as success are separate negative controls. Python and npm
application audits report 78/188 dependencies with zero advisories.

The final scan produces all twenty reports for ten immutable image identities,
with no selected HIGH/CRITICAL findings. The original blocked scan is retained;
neither a passing filter nor disappearance from an SBOM establishes the bundled
Python repair. Actual build, CAPI/linkage and application evidence supply that
separate proof. Final inventory and runtime receipts remain part of qualification.

Full SBOMs retain eight advisory IDs / nine role rows. All supplied ratings remain:
six medium entries, three low entries, one alternate-provider HIGH rating and two
unscored rows (ratings can overlap a row). The five targeted IDs disappear from
the full inventory, not just the filter. Compared with the qualified baseline,
three existing `libexpat` packages move from 2.9.0 to 2.10.0; the Python builder
adds `expat`, `expat-dev`, `libexpat` and `pkgconf`, with no APK removals. The
separately bundled Python copy is assessed through native evidence above.

Both final backend stages pass the real installed-pair verifier and original
interpreter/native hash checks. As UID 10001, the runtime passes ordinary OCR/PDF,
compression, timezone and ECB XML checks plus 32 unchanged repository cases for
XML invoices, camt bank statements, SEPA and Factur-X. Real default-entrypoint
startup passes liveness/readiness and graceful lifespan shutdown. The initial
harness incorrectly required exit 0: unchanged pinned Uvicorn re-raises SIGTERM,
so tini correctly returns 143 after all shutdown markers. The failed assumption
is preserved; focused state/log/source evidence establishes the actual contract
without changing application behavior or repository assertions.

The final frontend passes nonroot/read-only HTTPS, static and proxy behavior with
verified certificate/hostname, UID 101, HSTS/CSP and the real production nginx
configuration. A root-only synthetic key is refused and group-readable 0440 is
accepted. Its preflight fixture was corrected to read APK's installed database
offline after a display-format assumption failed. The exact final backend also
passes all eleven shared-volume stages and six native S3 groups, including cold
initialization, refusal, recovery and persistence. Owned fixtures/credentials are
removed. These local synthetic checks do not establish external provider or
accountant acceptance.

Evidence is under `/workspace/artifacts/gosu-helper-maintenance-2026-11-03/`.
`publisher-review/`, `expat-publisher-review/`, the blocked `inventory-review/`,
`tests/`, `tests-system-expat/`, `tests-system-expat-final-source/`, `runtime/`
and scoped adversarial receipts retain their actual source and evidence scope.
`python-system-expat-feasibility/` is prototype evidence; final product evidence
uses separately identified final images. No CVE-specific exploit or resource
exhaustion reproducer was executed. No new speed, RAM or production-capacity claim
is supported by these compatibility/security checks.

## Review and rollback

Keep the remaining npm, uv/rustls and x/sys inventory, all supplied ratings and
owner/release decisions visible. Do not infer a vulnerability-free release from
a selected-severity gate or apply an unreviewed major parent change.

A compatible application rollback should retain the patched helper and fixed,
coherent XML libraries, or use a separately qualified image meeting those
conditions. Automatically restoring the captured vulnerable helper/bundled XML
is not a security rollback. No schema/data rewrite is introduced.

AI assisted implementation, research, tests and independent review. Agents do
not supply human/Lead approval under the AI Engineering Policy. Fresh CI results
for the delivered source will be recorded in its PR and `ci/` receipts; pending
publication-time markers must never be presented as completed qualification.
