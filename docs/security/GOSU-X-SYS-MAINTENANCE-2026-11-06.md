# Gosu x/sys manifest maintenance — 6 November 2026

Status: helper source and additive runtime qualification are composed and locally
checked in an isolated branch based on
frozen PR120 `76d6d7f9babbbf4ef4707f5b1dfdc980d16a693d`. **Actual candidate
PostgreSQL image builds, full inventories and delivered-source CI remain
required.** The existing SEC-X-SYS row stays INVESTIGATE. No current image,
Windows, future-platform or release clearance is claimed.

The privilege-drop helper still uses gosu 1.20 and Moby user 0.4.1 with the
patched Go 1.27.3 compiler. Only its two module manifests change to the official
x/sys 0.49.0 release. Consumed identity, argument, exec, signal and failure
behavior matched the baseline in the bounded prototype. No PostgreSQL binary,
entrypoint, UID/GID, PGDATA, authentication or financial/source data changes.

## Current advisory and publisher facts

Current saved PR120 runtime image `98533750…` and builder `bf5e88b8…` are
linux/amd64 and retain x/sys 0.1.0 / CVE-2026-39824. Both full SBOM records have
`ratings: []`, not a zero or LOW rating. The selected HIGH/CRITICAL audit does
not remove those unscored records. Full raw ratings and historical evidence
remain unchanged.

Official GoDB/CNA record GO-2026-5024 names only
`golang.org/x/sys/windows.NewNTUnicodeString`, Windows, with fixed floor 0.44.0.
The named source is in a Windows-specific file; gosu imports x/sys/unix and
the observed image/build target is Linux. This is a precise source/target
boundary, not evidence that the retained module is fixed or every future
target is clear. No exploit, Windows execution or new govulncheck run occurred.
The official CVE record separately carries CISA ADP CVSS3.1 3.3 LOW; that
attribution is retained without rewriting the captured unscored image rows.

Fresh official gosu latest/tag/master remain 1.20 at
`29b939aa5bd4dc588af765b8248f6e39150d2565`, still requiring x/sys 0.1.0.
There is no published fixed gosu parent. Its security policy does not promise
releases for unused CVE functionality; no upstream exclusion wrapper or
scanner suppression is adopted here. Official x/sys 0.49.0, released
6 October 2026 at `b6b8557e8e2974925aec7a136ba0e323eedc5f43`, is the selected
downstream module. It requires Go 1.26.0; fixed floor 0.44.0 requires Go 1.25.
The existing compiler satisfies both. This is a maintained downstream manifest
build, not a new or publisher-endorsed gosu release.

## Exact source, metadata and notices

The original gosu/fake-git archives, compiler/runtime parent digests and Alpine
security floors remain pinned. Before replacing anything, the build verifies
the original go.mod/go.sum hashes and both source-defined overlay hashes. The
selected readonly graph stays main gosu, unchanged Moby 0.4.1 and x/sys 0.49.0,
with no replace directive or unrelated module upgrade.

| Input | SHA-256 |
| --- | --- |
| Original go.mod | `216dbdc31af7371e368b90a85837c6dcf8aa7139cb449ecceaa0526d91900c3a` |
| Original go.sum | `3f4d244f09dc5009ad68d99b2d99815f48fddf693fad8e734bbe820b315f43e7` |
| Overlay go.mod | `4632cb07b230b1caee8f377dae2f28e5d66757d1867d3d783aa84166620ff4f8` |
| Overlay go.sum | `4c5ca63607e11c08110f8b8326552b4c62090a736ecf8ce03188db600dedc515` |
| Actual new x/sys notice | `911f8f5782931320f5b8d1160a76365b83aea6447ee6c04fa6d5591467db9dad` |

Truthful modified state produces actual main BuildInfo `v1.20.0+dirty`,
release revision `1.20` and `vcs.modified=true`. The upstream commit/archive
and overlay hashes are retained as separate public provenance. The gosu-only
AWK guard now requires that exact dirty identity, Go 1.27.3, both exact linked
dependency versions/checksums, release/time/CGO/Linux fields and the declared
target architecture. It rejects missing/duplicate/contradictory/replacement
records, old x/sys and falsely clean metadata. No other role gains a dirty-source
allowance. Helper component `1fee7a5` left other Dockerfiles, scanners, workflows
and filters unchanged. The final composition adds only the explicit runtime
qualification script/tests and Docker-job step described below; existing gates
and scanner/rating controls remain intact.

BuildKit TARGETARCH is made stage-visible and required. Actual GOARCH must
match it; the amd64 default level remains v1, while non-amd64 targets do not
require an amd64-only record. Synthetic matching arm64 metadata tests the
declaration protocol, not an ARM binary or runtime. Actual qualification here
remains linux/amd64.

All five notices and BuildInfo remain public. The actual 0.49.0 BSD-3-Clause
notice is checksum-verified before copying; its bytes differ from 0.1.0,
including Google Inc.→Google LLC. Public notice modes are explicit. Retaining
the license category alone would not justify copying old text.

## Go directive and runtime compatibility

Modern Go requires the root directive to be at least its dependencies' minimum,
so go.mod moves from 1.20 to 1.26.0. With the same compiler this removes 25
legacy DefaultGODEBUG overrides. Two explicit overrides remain unchanged.
Nineteen changed flags belong to absent packages and two os flags are Windows
paths. Four runtime facilities can differ: initial container-aware GOMAXPROCS,
automatic updates, Linux anonymous-map labels and panic(nil) semantics.
Gosu explicitly calls GOMAXPROCS(1), disabling later updates, but that does not
erase startup or mapping differences. No legacy debug defaults are forced.

Executable lookup's ErrDot setting is not one of those changes: both directives
postdate Go 1.19. Two focused paired controls show bare `id` works under the
declared absolute PATH and bare `true` is refused under PATH=. in /bin, with
identical results in both binaries. The same-source pinned-base fixture has
no GODEBUG override; sanitized current image records do not include an Env dump.
The helper's compiled defaults are not an injected environment setting for the
native PostgreSQL child after Exec. User-supplied environments stay separate.
[Complete dated mapping](/workspace/artifacts/sec-x-sys-investigation-2026-11-06/prototype/GODEBUG-ASSESSMENT.md).

## Executed proof and retained failures

The official Go 1.27.3 linux/amd64 archive matches its publisher SHA-256.
All source/cache/compiler changes are task-local under /tmp; only go.mod and
go.sum differ from the verified gosu archive. Official module authentication
from an empty go.sum reproduces the canonical four entries. Both baseline and
candidate compile offline with GOTOOLCHAIN=local, CGO=0, readonly modules and
bounded parallelism; real BuildInfo identifies the exact old/new graphs.

The prototype uses an existing immutable Alpine image `2c1d5e2a…` with only the
public binary bound readonly, network none, readonly root, root startup and
only SETUID/SETGID capabilities. Every declared image VOLUME is fresh tiny
tmpfs; no DB entrypoint, live resource, host user file or DB/PV is accessed.
Fifteen original plus two PATH contracts each match baseline/candidate output
and exit codes: 34 accepted observations, not 34 unique contracts. This covers
UID/GID 70, supplementary groups, unknown numeric IDs, HOME, zero effective
caps after drop, PID1/TERM, argument fidelity, exit propagation and refusals.
All owned fixture containers are removed. It is not a newly built image or
PostgreSQL initialization/existing-volume proof.

Source tests preserve all 15 original assertion ASTs and explicit current-fixture
transition. The old clean release and real baseline BuildInfo remain negative
evidence. Failure-first focused tests record 9 failures/38 passes before the
features; final target-aware coverage is 54 passing nodes. A separate 147-node cohort contains the preserved 35 Gosu controls with explicit
current-fixture transition, one additive old-release refusal and 111 byte-identical
image-audit cases. All 201 final source-contract node IDs are inventoried. These are local contract checks, not an application/release
suite or an addition to prototype observations.

The first incumbent run caught the target-parameter check preceding the APK
install. Only Docker command order was corrected: APK remains immediately after
set-e, and the original parser/assertions remain unchanged. Its 146-pass/one-fail
log is retained. Prototype fixture permission, CID synchronization and lazy
checksum-auth harness corrections are also retained without calling them
product incompatibilities or extra passing cases. Initial 47-case red and
later 54-case target refinements keep their distinct source scope.

Ruff/format checks for both changed tests pass. All 51 docs/generated/CI-discovery
controls pass in 14.24 s, with zero failures, errors or skips. Collection is 7,632 cases,
not a full-suite execution. App/model/migration/frontend sources remain exact
to PR120; API/schema and the 166 migration head do not change.
[Prototype receipt](/workspace/artifacts/sec-x-sys-investigation-2026-11-06/prototype/PROTOTYPE-RECEIPT.json) and
[final-source checks](/workspace/artifacts/sec-x-sys-investigation-2026-11-06/prototype/implementation/FINAL-SOURCE-LOCAL-CHECKS.json).

## All declared build contexts

Both CI PostgreSQL roles and Release's postgres matrix use `./deploy/postgres`.
Local Compose and Hostinger declare that same directory. README/Makefile and
Hostinger/deployment-runbook manual build commands delegate to those Compose
files, including the migrate stack. The image-only overlay deliberately removes
build and uses published images. Generic manual CA-secret guidance does not
declare an alternate PostgreSQL context.

There is no context .dockerignore or Dockerfile-specific ignore, and both
canonical files are present and not Git-ignored. Existing relative COPY operands
remain relative to that dedicated context. No caller migration, arbitrary root
context support or manual TARGETARCH argument is added: BuildKit already supplies
its automatic target, and RUN secret mounts already required BuildKit.

## Remaining acceptance and rollback

No local image pull/build/scan was performed. Fresh actual PostgreSQL builder
and runtime images must confirm exact compiler/module/provenance/notice hashes,
UID70, process behavior, entrypoint/CMD/SIGINT/PGDATA/auth and Moby contracts,
fresh init and exact existing-volume recovery. Full immutable-role inventories
must retain every advisory/provider rating and show actual dependency replacement;
a passing selected-severity filter is insufficient. All required delivered-source
CI and root review remain pending.

Current PR120 receipts stay frozen. Do not suppress old rows, remove metadata
to manufacture a clean scan, infer Windows/future-platform clearance or report a
current Linux exploit. Source recovery, financial policy, global timeouts/indexes
and production deployment are unchanged. A rollback must use a separately
qualified helper; restoring old module metadata alone is not a security repair.

## Composed runtime qualification and final metadata

Helper component `1fee7a558debb283f729b2d8503967af8d399f1f` composes with the
accepted runtime component `1e9c232a7807e38bbba343f2c8b398fd3459dda0` at
`16fabc8`. All eight reviewed helper source/test files remain byte-identical.
The runtime component adds only `scripts/check_postgres_runtime.py`, its new
portable safety test and one CI Docker-job step. After successful image audit
and before the unchanged S3 check, CI invokes the actual newly built
`bidit-ci-postgres:review` and writes `image-security/postgres-runtime-smoke.json`,
included in the existing always-uploaded image evidence. The stock PostgreSQL
application job does not supply this custom-image proof. Ten CI jobs remain;
no required context, threshold, image scan or severity control is removed.

The owner-qualified 65 fake-Docker controls and static checks bind the exact
merged source; they were not rerun here. Its separate 111 incumbent image-audit
cases overlap the 111 already included in the helper's 147-case cohort, so they
are not added again. The previous 201 helper contracts, 17 prototype pairs and
all earlier failed/checkpoint runs retain their original scope.

The local functional result uses the old immutable `2c1d5e2a…` image, not the
new module recipe. Fresh initialization and same-container/same-volume graceful
restart preserve system identifier, ordinary-role/table OIDs and the signed
numeric/JSON marker. PID1 is UID/GID 70 with zero effective/permitted capabilities;
PGDATA is 70:70/mode 700. Startup follows the declared image/entrypoint defaults,
not the helper-only restricted-capability prototype. Both owned container and
fresh volume and the private environment are verified removed. This is not a
cross-image baseline-volume transition or new-helper qualification.

Authentication is observational: inherited local/loopback HBA rules use trust,
and the deliberately wrong password is accepted in that fixture. The reported
scram-sha-256 password-encryption setting does not prove password enforcement.
The same facts must persist after restart; no HBA/auth policy is changed.

Six fixed public helper paths report hashes/sizes/status, with at most two
16-KiB UTF-8 text fields. The 64-KiB output check is post-capture validation,
not a general memory bound. The generic old-image smoke may pass while the
three new overlay files are unavailable. Its own label says reported metadata
is not helper clearance. The actual new-image collector must require expected
manifests, notice/buildinfo/provenance and binary identity, joined to the new
immutable role IDs/full SBOMs; old-image success is insufficient.
[Runtime receipt](/workspace/artifacts/postgres-runtime-smoke-2026-11-06/FINAL-RECEIPT.json).

The composed tree collects 7,697 cases, not a full-suite execution. App,
frontend, models/migrations and generated API/ERD remain unchanged. The 147/54
and 51-check figures above are dated helper-component qualification; final
composed docs controls are recorded in the delivery receipt. No new local
candidate image build, pull, scan or runtime cohort was executed here. Actual
new-image/PG proof and all required hosted CI remain pending.

A separate root-reviewed [hosted component reconciliation](../plan/BACKLOG-HOSTED-QUALIFICATION-2026-11-06.md)
records 17 CI-PASS-HUMAN-REVIEW and 19 PARTIAL-CI-PASS-HUMAN-REVIEW statuses using
specific sealed PR 115–120 sources/logs, not ancestry. Those 36 updates preserve
the old next text/qualifications and all identity/inventory/provenance fields.
SEC-X-SYS is excluded from that qualification and separately gains only this
runtime-progress note; it remains INVESTIGATE. The other 182 complete raw rows
remain exact in this combined delivery. There are still 219 IDs and no new
finding, workflow/policy completion or current image/platform clearance.
