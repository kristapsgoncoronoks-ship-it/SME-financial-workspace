# Engineering backlog

**Current162 follow-up:** [Partner document and webhook tenant integrity](plan/PARTNER-WEBHOOK-TENANT-162-2026-11-05.md)
adds two mandatory CASCADE pairs without changing optional references or money.
118 focused,28 graph/generic and98 composed native checks pass; fresh CI remains
required. Corrected PR109254dd9e now passes all13 contexts. Older paragraphs
below retain their own source context. No main merge or deployment.

## Current160 sibling tenant-parent follow-up

[Expense comment/approval-step parent integrity](plan/EXPENSE-PARENT-TENANT-160-2026-11-05.md)
adds the two mandatory composite report parents, retaining159, its index and
unrelated schema/history.122 focused and83 integration controls pass; fresh
CI remains required. Six dated candidates remain separate. PR107 is now all13
green, with its original browser retry retained and a separate fixture repair.

## Current159 tenant-parent follow-up

[Expense item/report tenant integrity](plan/EXPENSE-PARENT-TENANT-159-2026-11-05.md)
adds one mandatory composite CASCADE parent with global masked preflight and
atomic rollback.77 focused checks pass; broad graph and fresh CI qualification
remain required. Separate160 siblings and six dated candidates are not silently
closed. Earlier dated wave status below retains its original source.

## Active backlog completion — 5 November 2026

[Current implementation and dependencies](plan/BACKLOG-COMPLETION-2026-11-05.md)
and the [197-row baseline plus15 appended findings](plan/BACKLOG-COMPLETION-2026-11-05.json)
are the current entry points. PR1059efcd1c and PR10637b826b passed all13 required
actual PR contexts. [The current OCR/workflow wave](plan/OCR-WORKFLOW-REPAIRS-2026-11-05.md)
has local/native/connected qualification and needs fresh hosted CI. Tenant-link,
parser, recurring-notice and concurrent resource follow-ups continue separately.
Owner/provider/legal/corpus/hardware/release dependencies remain explicit. No
main merge or deployment; older dated statements below are historical.

## Current uv / rustls development — 3 November 2026

[Current implementation and evidence boundaries](security/UV-RUSTLS-MAINTENANCE-2026-11-03.md).
PR #101 source `4f2a360` completed all thirteen mandatory CI contexts on attempt 1;
new source requires its own qualification. The preliminary uv image inventory
removes all four rustls IDs, retains every remaining rating and matches complete
APK/application inventories. Final permission-corrected images also pass fresh
ten-role/twenty-report auditing, native/application and storage qualification;
the signed index attestation verifies in a separate supplement. Four other IDs /
five rows remain with every rating. Delivered-source CI and human review remain
separate from local proof.

| Assignment | Current engineering contract |
| --- | --- |
| uv / rustls parent | Use published uv 0.13.7 with actual OCI rustls 0.23.46, immutable source/index/amd64/binary bindings and unchanged MIT/Apache notices. The OCI and release-archive executables are distinct builds. Preserve installer flags/CA/prefix/no-download behavior, compare actual installed distributions and dependency metadata, and qualify final native/image/storage/CI outcomes. Unverified attestation claims stay explicitly unverified. |
| Public provenance traversal | Inherited `/usr/local/share/licenses` mode 0644 hid existing notices from a cap-drop probe. Set only that public parent to 0755 in both existing chmod seams. Prove exact notice/provenance reads without accepting empty globs; retain existing file modes and private storage 0700. Preserve first-candidate and corrected-image evidence separately. |
| Residual inventory | Keep three npm IDs plus x/sys in two PG roles, all provider ratings, the alternate HIGH and two unscored rows visible. Investigate supported parent or compatible, separately guarded maintenance; never force an internal major, delete metadata or declare unscored safe. |

The preceding candidate's 5,173 backend passes / 274 skips, 949 functional /
13 visual and native PostgreSQL/image/storage checks are completed dated evidence.
They do not qualify this new installer or close owner/release, RB04/RB05/EX21,
EX19 recovery, B00/F10/readiness, UI/finance/provider/accountant acceptance.
Existing cache/footprint proposals remain unapplied. No main merge or deployment.

## Preceding gosu / Expat development — 3 November 2026

[Current repair, actual findings and evidence boundaries](security/GOSU-EXPAT-MAINTENANCE-2026-11-03.md).
PR #100 at c904bc7 completed all mandatory CI on 1 November; its original source and
reports remain. The new helper/Expat candidate must receive its own qualification.

| Assignment | Current engineering contract |
| --- | --- |
| Gosu/Moby helper | Published gosu 1.20 links fixed moby/sys/user 0.4.1. Verify real binary metadata, source/hash/time/license and false-upgrade controls; preserve existing compiler, PG16 initialization, UID 70 and baseline-created volume data. Local source/build/runtime proof is recorded; fresh candidate CI remains separate. |
| Expat OS packages | Fresh scanning finds four new IDs 113351/115506/117610/118370 across three old roles, with six selected HIGH rows. Require official Alpine 2.10.0-r0 at each affected existing seam and inspect full inventory, not only filtered counts. Preserve original blocked run. |
| PYEXPAT-VENDOR-01 | Direct inspection/source review proves the stock Python 3.14.8 bundled 2.8.5 copy is separately unfixed; published 3.14.10 also lacks all four fixes. The implemented same-release/configuration build pairs pyexpat AND ElementTree against fixed system Expat and preserves interpreter/ABI/provenance. Actual paired imports, CAPI/linkage, 32 existing application cases, OCR/PDF, health/shutdown and storage pass locally; fresh delivered-source CI and human review remain separate. An APK-only update or scanner absence is insufficient. Remove this maintained overlay only after a published parent directly proves fixed coherent XML behavior and complete qualification. |

The current local candidate passes 196 focused cases, actual collection of 5,447
backend cases, all ten image roles/twenty reports and the native database, XML,
application, TLS and storage checks. Five targeted IDs disappear from full SBOMs;
eight other IDs/nine rows remain, including one alternate-provider HIGH rating
and two unscored rows. Source/negative-control evidence and the failed intermediate
scan remain preserved. This local status does not replace delivered-commit CI,
human/Lead review or release controls.

The additional measured cache/footprint assignments below remain proposals.
Existing RB04/RB05/EX21 decisions, EX19 recovery, CI-DEV-LOAD-01, B00/F10/readiness,
remaining npm/uv/x/sys inventory and human/provider/accountant acceptance stay open.
No role policy, production migration, merge, deployment or unsupported major is
introduced by this security work.

## Additional measured follow-ups — 1 November 2026

These are logged, unapplied assignments from source `c904bc7`; they do not
qualify a different binary, implement an optimization or accept remaining risk.

| Order / assignment | Finding and engineering prompt |
| --- | --- |
| Next supported security repair: gosu 1.20 | Verify immutable official source `29b939aa5bd4dc588af765b8248f6e39150d2565` / archive SHA256 `2b70da8a5480fafedc16bb1fc91b572f064d03df736cbd91a616af2f1b66ab51`, release timestamp and module/license provenance. The inspected source moves moby/sys/user to 0.4.1 without a required Go/base change; source availability is not a qualified helper binary. Implement the reviewed source/hash/semver/time/license-path and actual binary-metadata guards, add false-upgrade controls, and prove privilege dropping, actual PostgreSQL initialization/restart/existing volumes plus full image/CI evidence. Retain unchanged x/sys 0.1.0 Windows-specific inventory and platform limits; no suppression or automatic vulnerable rollback. |
| CI-IMAGE-CACHE-01 | Saved prior/current hosted gates each download the Trivy database in 20/20 invocations, versus 1/20 in the local run using existing cache support. Use a fresh run/attempt-owned cache directory at the existing audit step, preserving normal refresh checks, all ten targets/twenty reports, database metadata, failure controls and cleanup. No cross-run stale/offline fallback or skip-update flag. Measure actual request/bytes/time/resource differences before claiming a benefit; do not infer identical DB bytes from a shared tag. |
| PERF-RUNTIME-DEV-01 (modest disk-only candidate) | One bounded actual-image probe finds five test distributions (pytest, pytest-asyncio, iniconfig, pluggy, Pygments) totaling 6,141,952 logical bytes (~5.86 MiB). Ruff/mypy/pre-commit are already absent; retain real runtime PyYAML, packaging, HTTPX and Prometheus, OCR/fonts and metadata. Before a minimal runtime/dev requirement split, preserve the documented in-container pytest smoke and one authoritative version per package, teach base-graph auditing/cache keys to read bounded recursive includes safely, and prove source/combined dev graphs and all real image behaviors. No compressed-layer, RAM or latency gain has been measured; keep this lower priority than supported security repairs. |

Detailed source, bytes, hashes and unapplied proposals are in the 1 November
Alpine review artifacts. Fresh complete qualification remains mandatory for any
implementation. The current candidate source stays unchanged while CI runs.

## Follow-up assignments from qualified source 42cb534 — 1 November 2026

These are source-backed assignments, not new risk acceptance. Preserve
current tests/security policy and qualify each future delivered source. Source
CI evidence: https://github.com/kristapsgoncoronoks-ship-it/SME-financial-workspace/actions/runs/41299385021.
The full image inventory retains 24 advisory/role rows across 12 IDs, including
one alternate-vendor HIGH and nine unscored rows. A passing filtered image gate
is not a vulnerability-free inventory; retain every rating and scope condition.

| Assignment | Source-backed finding and implementation prompt |
| --- | --- |
| Alpine maintenance | CVE-2026-107891 musl/musl-utils, CVE-2026-85091 zlib and CVE-2026-94287 libxpm have publisher/index-supported v3.24 x86_64 candidates: 1.2.6-r3, 1.3.2-r1 and 3.5.20-r0. Refresh secdb/APKINDEX and actual affected roles; update only reviewed maintenance packages/parent digests. Distinguish already fixed core musl from older utilities. Verify actual binaries/ABI, non-root/TLS, OCR/PDF and database startup on affected image targets; retain all ten role scans/hashes/storage gates. No production action or unscored-is-safe claim. |
| gosu privilege-drop helper | CVE-2026-61801: published gosu 1.20 source pins fixed moby/sys/user 0.4.1, but its replacement binary is unqualified. Verify source/toolchain/provenance and actual static helper, then prove PostgreSQL init/startup, UID/GID and existing-volume contracts. Retain x/sys CVE-2026-39824 Windows-only applicability evidence for observed linux/amd64; do not suppress its module inventory or assume future platforms. |
| Global npm tools | CVE-2026-111997 ip-address, CVE-2026-118773 glob and CVE-2026-118764 minimatch belong to npm 12.2.1 in frontend-build, not app lock. No supported fixed parent was verified; fixed libraries are ip-address 10.7.4, glob 14.0.2 and minimatch 11.0.1. Refresh parent bundles/ranges/integrity. Prefer a supported parent fixing all relevant copies and reassess the existing guarded undici patch. Do not force internal major versions, edit the wrong app graph, delete metadata after vulnerable use, downgrade for scanner output or change severity policy. Keep GHSA medium/Red Hat HIGH minimatch ratings visible. |
| uv / rustls | GHSA-c8m5-c952-6cm4, GHSA-h2v8-394m-p7vv, GHSA-vp88-798v-h67f and GHSA-w44r-93j7-qv28: fixed rustls 0.23.46 exists; no shipped fixed uv parent was verified (latest inspected 0.13.6 locks 0.23.45). Refresh parent binary provenance and actual features. Preserve conditional custom-debug/Brotli/ECH/key-share evidence without declaring a fix or blanket unreachable code. Bound any synthetic TLS/resource regression and retain all image/security gates. |
| Full EX19 cancellation recovery | Preserve current exported/paid refusal and APPROVE policy. Implement a shared actor/org/key ExpenseCommand cancellation receipt with typed POST and legacy DELETE adapter, optional reason, reviewed versions, membership→batch→ordered report→audit locks, child version bumps and exact frozen released set. Reuse strict audit rollback and GET-only recovery. Guard the new operation in reimbursement lookup and both older expense-command aliases, retaining those guards on rollback after receipts exist. Current cancellation has no external event: no dummy job/webhook or new integration policy. Prove actual original-key/race/role/no-write/browser/PG outcomes. No migration is needed by the inspected model. |
| CI timing and fail-fast | Record current serial duration/result/resource evidence first. Evaluate backend needing existing lint: the original full test step ran 32m24s after lint already failed. Preserve all seven core/four supply result-only contexts and fail required summary for skipped/failed/cancelled/missing results. npm-only auditing is a separate workflow; lint alone does not preflight it. Choose faster failed-head feedback versus added valid-head critical path explicitly. |
| Portable process workers | Historical xdist use is not current qualification. Review a pinned compatible process runner only after isolation/baseline evidence; start bounded workers, not automatic CPU scaling. Preserve fresh per-case DB/storage/FX and real crypto. Prove exact node-ID union/skip parity/no lost coverage and correctly scoped CPU/RSS. Keep native lock observers, resource/performance proofs and browser auth pacing isolated initially; broad same-user native wait predicates could produce false lock proof when parallelized. Do not remove repeated fast-feedback tests without evidence. |
| CI-DEV-LOAD-01 | One patched local run passed 948/949 and hit a global workspace PageLoadError. Exact cause remains unknown; 30 cold-cache cases, ten explicit exact-case executions and subsequent full/local/hosted 949 passes do not identify or erase it. Capture first-attempt module URL/status/cause on recurrence; no assertion/timeout/baseline/automatic-retry relaxation. |

The Alpine candidate is implemented in this branch. Nine actual builds, ten role
scans and runtime/storage checks pass locally, removing three IDs / 13 role rows
from full SBOMs. Nine IDs / 11 rows remain with all ratings preserved, including
one alternate HIGH and two unscored rows. Fresh delivered-head hosted CI and
human/Lead review remain separate from local qualification. [Scope, qualification
and rollback](security/ALPINE-IMAGE-MAINTENANCE-2026-11-01.md). Other rows remain
open assignments; no unsupported parent or internal major override is applied.
Keep RB04 retention/access/replacement decisions, reviewed RB05/EX22 allocation,
EX21, EX16/report/UI queue, B00/F10/readiness, human/Lead and accountant/provider
acceptance separate. No new role, mandatory reason, bank profile, merger or
deployment is authorized by these assignments.

## Fresh dependency advisories — PR #99 qualification completed

The first PR #99 source `026b7fe` fails pip-audit/dependency review on seven
new pypdf advisories and one follow-redirects advisory (two affected npm package
rows). Preserve those red receipts. [Minimal patch and evidence](security/CI-DEPENDENCY-REPAIR-2026-11-01.md)
updates pypdf 6.21.2 → 6.21.4 and follow-redirects 1.16.1 → 1.16.2 inside the
existing dependency range, with no graph expansion, audit waiver or gate change.
Repaired source `42cb534` completed core/supply CI successfully (runs
41299385021 / 41299385016): 5,030 backend / 274 skips, 249 ordinary PG,
37 migration/parity / three SQLite-only skips, 14 bootstrap, 10 live journeys,
949 retry-free functional and 13 visual cases. All ten image identities and
twenty report hashes match, with zero selected HIGH/CRITICAL gate rows;
24 lower/unscored/alternate-rating rows remain tracked above. This supersedes
the earlier pending notes, without qualifying this new Alpine source or closing
human/Lead review, owner decisions or production release controls.

**CI-DEV-LOAD-01 (open observation):** the first patched-dependency full local
run passed 948/949; unchanged suspended-workspace navigation encountered a
global workspace `PageLoadError`. Its original module/transport cause was not
captured. Thirty instrumented cold-cache navigation/recovery cases and ten
explicit executions of the exact scenario pass without retries; three deliberate
failure controls validate the cause instrumentation. This does not establish a
Billing defect, optimizer cause or dependency regression. Preserve the failed
attempt and capture module URL/status/cause if it recurs; no assertion, timeout,
baseline or automatic-retry relaxation is justified. Subsequent traced local and hosted runs each passed all 949 functional cases.
These do not identify the original cause; keep the observation open.

## EX17 direct-terminal reimbursement boundary — 1 November 2026

[Implementation, action prompts, evidence and rollback](security/REIMBURSEMENT-TERMINAL-BOUNDARY-2026-11-01.md).
Close the report-decision shortcut for both `mark_reimbursed` and legacy
`reimburse`; preserve existing access/version/self-decision guards and approved
queue progression. The canonical payout command remains responsible for payment
receipts, maker-checker and transactional audit/outbox. Replace terminal buttons
with an actual Reimbursements link and explanatory guidance. Legacy reimbursed
reports stay readable without invented receipts. PR #99 at `42cb534` completed
all required hosted gates as recorded above; human/Lead review remains open.

PR #98's full hosted qualification is now verified at `eb02cd2`: 4,982 backend
passes / 266 skips, 241 ordinary PG, 942 functional and 13 visual browser passes,
37 migration/parity passes / three SQLite-only skips and 14 bootstrap passes.
Ten image identities/twenty saved hashes match; HIGH/CRITICAL gates are clear,
with lower/unscored findings retained. Its earlier pending notes are historical.

**RB04 review:** use dedicated reimbursement artifacts and existing command/job/
storage mechanics after the retention/download/replacement contract is reviewed.
[Source findings, proposed design and acceptance prerequisites](reviews/REIMBURSEMENT-ARTIFACT-FOUNDATION-REVIEW-2026-11-01.md).
Workspace/archive single-use email redemption and expiry are not that contract.
Freeze sources under membership → batch → ordered report locks, verify stored
bytes before ready, and never regenerate from mutable source during recovery.
Do not claim generic orphan GC exists: the current storage protocol has no
listing/sweep facility. Worker rollback also means assigning failed then raising
does not persist an artifact state. Keep these acceptance checks in RB04.
Decisions §33 explicitly leaves EX17 unblocked. RB04 → reviewed RB05/EX22 → EX21,
full EX19 recovery, B00/F10/readiness and provider/accountant acceptance stay open.

**UI-EXPENSE-AUTH-01 (source observation):** report-page progression and the
report decision route both require the designated expense-approver flag, whereas
the general reimbursement permission also maps business roles. Review whether
the intended capability/copy should align across these workflows. This is not
established as a new regression or authorization bypass; do not loosen roles or
self-decision controls as part of EX17.

## ACC02/ACC03 current integration candidate — report follow-up

[Reviewed composition, source preservation, error repair and evidence](reviews/ACCOUNTING-ADAPTER-INTEGRATION-2026-10-29.md).
Combine existing PR #96 with qualified PR #97 rather than rebuilding its guards
or reverting current security repairs. Public/internal adapter refusal, typed
capability metadata and accessible blocked-action help are integrated. Preserve
current responsive wrappers and typed organization fixtures; regenerate the
single additive OpenAPI response contract with all 417 paths intact.

**ACC-COMPOSE-ERROR-01:** actual browser controls reproduced raw server/parser
detail in the global toast alongside the safe catalogue error. The catalogue now
uses the existing per-query handled-error mechanism; immediate regression checks
prevent waiting out toast expiry. Global query behavior and generic CSV remain
unchanged. Known key/action IDs are validated before rendering provider help.

The affected legacy layout fixture now supplies valid read metadata instead of
an empty catalogue; every prior assertion remains. Four Schedule screenshots
write to per-test outputs, preserving historical review evidence and assertions.
Original interrupted/failed attempts remain recorded, not relabelled passes.
New source still needs fresh delivered-head CI and human review; no provider is
accepted or enabled. Prior PR #97 completed all required CI on its own source.

Next financial work remains RB04 immutable artifacts → RB05/EX22 reviewed
whole-batch/child contract → EX17 bypass closure → EX21 worksheet. Full EX19,
retention/disposition decisions, B00/F10 and accountant/provider acceptance stay
open. **UI-ACC-HEADER-01 (nonblocking refinement):** expanded setup disclosures
can grow the invoice action header and wrap adjacent controls. Consider a
dedicated helper region while keeping visual/keyboard order, visible blocker
reasons and current generic preview; do not describe this candidate as a broad
invoice-page redesign or an accessibility certification. Keep the existing
UI-PLANNING-FOCUS-01 and context/reflow follow-ups in their ordered queue.

## Daily-report reconciliation — report dated 29 October 2026

[Input identity, source findings, final CI and candidate boundaries](reviews/DAILY-REPORT-RECONCILIATION-2026-10-29.md).
The report preserves the earlier 5,851-line import and adds 498 lines, including
EX21/EX22 and revised ACC02/ACC03. PR #95's complete exact-head core/supply
qualification is now verified; prior pending-CI notes are historical. New report
recommendations are proposed work, not executed commands or owner-policy approval.

| Order | Task / current prompt | Remaining acceptance |
| --- | --- | --- |
| Parallel release controls | B00 / F10 / readiness | Main remains unprotected with no rulesets/effective rules; latest history run fails. Obtain owner decisions, classify history without exposing matches, and verify served revision. Green feature CI does not close these. |
| Candidate reconciliation | ACC02/ACC03 r2, existing PR #96 | Independently review and compose its eight changed files onto the current qualified stack. Preserve existing security fixes, current UI layout/contrast and browser coverage. Regenerate additive OpenAPI; do not copy its older snapshot or reimplement its guards. Qualify a new combined candidate; no unreviewed main merge. |
| 1 | O05-RB04 / `O05-RB04-P01 r1` | Persist immutable export identity/bytes/hash/source and recover the same artifact after a lost response. Reuse existing receipt/job/storage infrastructure; prove actual saved-file and PostgreSQL outcomes. |
| 2 | O05-RB05 / `O05-EX22-P01 r1` | Resolve the acknowledged partial-SEPA versus whole-batch payment contradiction. Proposed first-release contract is all-ready/all-included; review the policy boundary in Decisions §33 before changing the supported partial option. Payment instructions never establish bank settlement. |
| 3 | `R010-A-EX17-P01 r1` | Close report-level terminal reimbursement that bypasses the batch's payment receipt and maker-checker evidence. Reuse canonical locks/commands; preserve unrelated approval actions. |
| 4 | `O05-EX21-P01 r1` | Produce a labelled, versioned payout worksheet from the frozen artifact, with exact amounts/provenance, formula safety and identical re-download. No unapproved bank/payroll import claim. |

[Complete refreshed engineering assignments](plan/REIMBURSEMENT-EXPORT-RELIABILITY-2026-10-29.md).
There is no standalone `O05-RB05-P01` prompt in this input; EX22 is its assignment.
Do not reimplement EX09, EX18, EX20, minimum EX19, EX16 eligible-query continuity,
comparison parity or development bootstrap without a new regression. Full EX19,
the broader report lifecycle, bank/accountant acceptance, optional
DB-PG-CONTRACT-02 and existing UI/finance work remain tracked below. The report's
83 semantic actions are a catalogue count, not whole-app runtime coverage.

## DB-PG-BOOTSTRAP-01 development candidate — 28 October 2026

[Contract, evidence, maintenance prompt and rollback](plan/DEVELOPMENT-POSTGRES-BOOTSTRAP-2026-10-28.md).
Use validated PostgreSQL metadata only when new tables are created by direct
non-production startup. Preserve tenant IDs when optional project/issuer links
detach. Check original constraint/element policy before copying; leave SQLite
portable, production Alembic-owned and existing generic constraints visible.
New native/bootstrap and portable/startup regression cohorts pass; fresh
delivered-head CI and human review remain separate. No migration, data repair,
new dependency or tenant-policy exemption is added.

The uploaded patch's development-bootstrap idea is implemented independently;
its clone-before-validation gap is covered by negative controls. DB-PG-PARITY-01's
original comparison correction remains byte-identical. Preceding PR #93/#94
core/supply workflows completed successfully at `bb3559ad` / `fc3a85bf`; their
earlier pending-CI statements below are historical. Their results do not qualify
this new source. DB-PG-CONTRACT-02 remains optional catalog/wrong-action breadth
for the migrated comparison suite; bootstrap catalog cases do not close that
separate scope. Keep UI-PLANNING-FOCUS-01, full EX19/EX21–22/EX17, B00 owner
decisions, F10/readiness and accountant/provider acceptance open.

## B00-INT-01 current integration candidate — 28 October 2026

[Reconciliation, qualification, rollback and action prompts](reviews/CURRENT-DEVELOPMENT-INTEGRATION-2026-10-28.md).
Combine current planning with the finance/OCR/UI/security stack, preserve both
tenant/worker/test registries, and join unchanged e5/a9 histories using graph-only
f0. The candidate has 155 revisions/one head, 119 tables, 929 functional browser
cases and strict comparison-only PostgreSQL parity correction. Ordinary native
PG, migration starting-state, drift/deletion, generated-contract and static
cohorts are recorded individually; no coverage totals are invented.

DB-PG-PARITY-01 also has a minimal draft PR #93 based on #87: original eight
comparison operations reproduce, corrected full comparison is clean, and all
four actual scoped DELETE contracts preserve tenants. No original constraints,
models, history or financial data are repaired. CI and human review remain
separate; no release, production migration or unreviewed main merge is claimed.

New inherited follow-up **UI-PLANNING-FOCUS-01**: native reverse-Tab can leave
New assignment behind the sticky header after calendar scrolling, reproduced on
both planning and candidate. Preserve the paired DOM/keyboard evidence and add
a scoped visibility regression before repair. The full-page screenshot's initial
missing-button appearance is separately explained by capture scroll state.
Keep B00 governance, F10/readiness, full EX19/EX21–22/EX17 and accountant acceptance
open. Green feature heads do not qualify this newly combined source.

## B00-CI-01 completion-check candidate — 28 October 2026

[Behavior, maintenance prompts, evidence and rollback](security/CI-REQUIRED-COMPLETION-2026-10-28.md).
Add PR-only `ci-required` and `supply-chain-required` with same-workflow seven/four
dependencies, `always()`, empty token permissions and a strict complete-inventory/
exact-success check. Existing contexts, child jobs, permissions, baseline writer
and main deployment remain unchanged. Actual committed inline commands are tested
against successful/non-success/missing/malformed fixtures and an accept-skipped
mutant. No application runtime, dependency, schema or data behavior changes.

Fresh actual PR-triggered success of both new names and human/Lead review remain
required. This candidate does not configure required settings or prove native
enforcement/cancellation scheduling. Retain all eleven incumbent contexts before
any separately authorized settings add qualified summary names. Uploaded payloads
stay byte-identical proposals. B00-GH decisions, B00-INT-01, B00-CI-03, F10/readiness
and retained finance/UI work remain open.

Failed #81/#88–90 logs/reports are now triaged. PR #88 uniquely mismatches
Playwright 1.64.1 with both 1.64.0 images, stopping guards before application
tests. The other blockers largely inherit old-main security repairs already
present in this development stack; #89's pypdf 6.21.1 is below its reported fix.
Keep update acceptance/paired-image repair and fresh final integration separate;
do not duplicate superseded security fixes or weaken gates. Parent PR #87's final
CI artifacts are independently verified, including all 901 retry-free browser
cases and ten image identities/twenty report hashes; the new source needs its
own eligible CI.

## B00 branch-policy intake — 28 October 2026

[Uploaded policy/configuration review, exact prompts and evidence](reviews/BRANCH-PROTECTION-INTAKE-2026-10-28.md).
Fresh main branch/rules reads establish protection off and zero configured or
effective rules. Detailed classic-protection access is separately 403. The
proposed eleven app-bound checks plus independent-review overlay require an
owner-selected reviewer/solo/recovery model; they do not replace section 23's
older proposal by implication. B00 / OPS-GOV-001 stays open.

| Task | State / acceptance |
| --- | --- |
| B00-GH-01 | Owner/admin: select operating model and approve scoped protection policy; preserve stronger controls; no activation in this intake. |
| B00-INT-01 | Engineering: refresh current #76/#87 integration with justified #80 reconciliation; unmodified graph has two heads. Qualify a single reconciled graph and final combination before integration. |
| B00-CI-01 | Engineering: add same-workflow `ci-required` / `supply-chain-required`, with exact-success checks and skipped/failed/cancelled controls; preserve all eleven incumbent contexts. Do not require names before qualified PR runs exist. |
| B00-CI-03 | CI queue: inspect actual failed #81/#88–90 logs at their exact heads; fix root causes individually without audit/test/severity waivers. Snapshot failures are not newly diagnosed vulnerabilities. |
| B00-GH-02/03/04 | Owner/admin: verify Actions/environment settings, name eligible code owners and rehearse actual enforcement/recovery after authorization. |
| B00-CI-02 | Later: bounded cross-PR acceptance gate if the human dependency ledger proves insufficient. |

PR #87 at `5926b84` now has successful completed core/supply workflows, all eleven
eligible contexts. This updates its pending-workflow observation below; no fresh
independent full artifact verification is claimed by this intake. Latest main
PII-history run `40621373374` still fails; occurrence classification/counts were
not re-evaluated. Keep F10/readiness, human review and the existing finance/UI
queue open. No settings, workflow, runtime, merge or deployment changes.

## EX20 operator-record command candidate — 27 October 2026

[Result, each action's maintenance prompt, evidence, index and rollback](plan/REIMBURSEMENT-PAYMENT-COMMAND-2026-10-27.md).
Implement immutable reviewed total/report-set checks, membership/batch/report
locks before audit, shared payment receipts/status/replay, strict audit/outbox and
operator-recorded versus legacy-unknown evidence. The UI retains keyed uncertainty,
excludes conflicting actions, owns confirmations and verifies readback. Optional
reference/method and existing maker-checker stay; bank confirmation remains unknown.
Local 117 API / 30 payout PG / two membership PG / 36 compatibility / 61 focused
and 901 full browser checks pass, with actual connected acceptance. Migration
`a9c3e7f1b5d0` adds one non-unique read index; native empty/representative/invalid-
index/downgrade probes preserve all financial rows and bounded three-query pages.
Fresh hosted qualification and human/Lead acceptance remain required.

| Finding | Bounded state / follow-up |
| --- | --- |
| EX20 changed totals and unlocked report transition | Old app accepts changed EUR 0.31 against stored EUR 0.30; candidate refuses and serializes actual HTTP report/authority races without duplicate effects. |
| Membership/audit cycle | Native create-versus-team-PATCH reproduces deadlock/500; target membership now locks before audit, same schedule completes 201/200 with policy unchanged. |
| Historical receipt versions/evidence | Missing versions remain null; legacy payment basis stays unknown; no invented confirmation, backfill or hashed audit rewrite. |
| DB-PG-PARITY-01 | Eight global PostgreSQL FK metadata operations also occur on untouched parent: targeted SET NULL(column) versus generic ORM. New-index delta and SQLite parity pass separately; full PG parity is not clean. Copy-ready follow-up in delivery record; do not hide drift or change FK policy. |

Full EX19 command/reason/version recovery, EX21/22 artifacts, EX17 closure and the
retained finance/UI/release queue remain open. Decision 33 required business fields
and provider/accountant/production acceptance remain separate. Safe rollback must
retain payment-receipt APPROVE checks in both legacy expense lookup aliases.

Repaired parent PR #86 now has complete green eligible CI/supply at `2c51a4a`:
4,573 backend / 218 PG / 872 retry-free browser / 13 visual and ten verified clean
HIGH/CRITICAL image targets. Preserve original f430 retry evidence below.

## CI-EX18-01 deterministic loading check — 27 October 2026

[Confirmed hosted flake, bounded repair and regression prompt](security/CI-EX18-LOADING-SELECTOR-2026-10-27.md).
PR #86's original CI and supply workflows succeeded: 4,573 backend / 218 PG /
13 ordinary visual checks, ten image identities/twenty report hashes verified,
zero reported HIGH/CRITICAL. Browser evidence is **871 first-attempt passes plus
one flaky retry success**; the earlier local 872 pass is a separate invocation.
Replace the broad loading locator with exact independent statuses, and hold both
organization/eligible requests to reproduce the collision. Existing 18 focused
cases and ten repetitions pass without retries; original-selector control fails.
No product, assertion, gate, retry policy or case count changes. All 872 functional
browser cases pass without retries/skips/flakes; fresh hosted status is in the
linked repair receipt. EX20 remains the next
product contract, with full EX19 and other finance/release work still open.

## EX18 recoverable creation candidate — 27 October 2026

[Implementation, each action's maintenance prompt, qualification and rollback](plan/REIMBURSEMENT-CREATE-COMMAND-2026-10-27.md).
The exact observed selection creates one locked, versioned batch with an atomic
shared command receipt/audit/keyed intent, or no writes. Explicit-key replay and
authorized lookup return its original result; the UI preserves uncertainty until
receipt/current-state/list readback, with GET-only check and explicit same-key
retry. Both eligible states, exact EUR arithmetic, bounded pages, constant-query
batch counts and legacy implicit-list refusal are implemented. Old receipt aliases
also enforce approver permission for this new operation. Local qualification:
79 API / 18 PG / 54 focused browser plus actual 102-report connected acceptance.
All 872 functional browser cases pass without retries/skips/flakes. Fresh hosted
status is in the linked receipt; human/Lead review
remains separate.

O05-UI01 body containment is verified at 390px/32px text with native local table
scroll; remaining device/320px/shared-shell/accessibility coverage is not closed.
No schema/FK migration, historical repair, new payment policy or provider action.
Full EX19 receipts/reason/version/recovery remain open after the minimum guard;
EX20, EX21/22, EX17/16 and the wider ordered finance/UI queue retain their prompts.

Preceding PR #85 is fully green at `262d4b4`: 4,542 backend, 208 PG, 854 browser,
13 ordinary visual; ten images/twenty exported hashes verified, zero reported
HIGH/CRITICAL. Preserve its historical pending notes below. Green draft checks
do not close B00/F10/readiness, human review or production/accountant acceptance.

## EX19 minimum recorded-export guard candidate — 27 October 2026

[Action contract, actual qualification, preflight and rollback limits](security/REIMBURSEMENT-CANCEL-GUARD-2026-10-27.md).
The candidate blocks cancellation on any nonzero counter/non-null export timestamp
or message ID, preserving report links and financial/audit/outbox state. Native
disabled/help UI, same-batch interlocks, bounded status reads and explicit GET-only
refresh are implemented. Local focused/actual acceptance and all 854 functional
browser cases pass; full fresh hosted CI and human/Lead review remain required.

This closes only the minimum predicate/UI boundary in O05-RB02, not the full EX19
action. Durable command receipts/keyed recovery, reason/version requirements,
financial write timeout/navigation outcomes and historical disposition remain
open. The read-only preflight flags uncertainty and never relinks or pays records.
Keep RB01/EX18 recoverable creation/eligible reads and RB03/EX20 evidence on the
shared design; existing roles, maker-checker and optional-reference policy stay.
Inherited **O05-UI01** whole-page enlarged-text overflow is separately prompted in
the delivery record; only the changed payout card fits in this slice.

## Daily-report reconciliation and finance priority — 27 October 2026

[Latest report intake, source corrections, final PR qualification and release controls](reviews/DAILY-REPORT-RECONCILIATION-2026-10-27.md).
The uploaded report's final 27 October entry supersedes its 26 October opening
summary. PRs #82/#83 now pass their complete eligible CI and supply-chain jobs;
both remain drafts awaiting human/Lead review and integration. Their old pending
backend records remain historical. The ten-target image scans are clean with
all twenty exported hashes verified per run; no gate is weakened.

| Order | Contract | State / next outcome |
| --- | --- | --- |
| Release controls | B00/F10/readiness | Open: no effective main rules/protection; current failed PII-history scan requires occurrence classification; served revision/readiness unverified. Green PR jobs do not close these controls. |
| 1 | O05-RB02 / EX19 Cancel | Source-confirmed exported cancellation releases report links. Implement the minimum locked export guard and honest blocked help; keep full version/reason/receipt/recovery separately explicit. |
| 2 | O05-RB01 / EX18 Create | Plan deterministic report locks/versions, exact unique selection/metadata, durable receipt/status and bounded eligible/batch reads. Two-actor failure window is not a reproduced production duplicate payment. |
| 3 | O05-RB03 / EX20 Record payment | Preserve existing maker-checker and evidence; distinguish internal assertion from bank confirmation and add command recovery/per-batch exclusion. New mandatory reference fields require section 33's policy choice. |
| 4 | EX21 CSV / EX22 SEPA | Catalogue distinct artifact/download/re-export/skipped-payee/recovery/interlock contracts before complete payout acceptance. |
| Continue | EX17/EX16/EX12–EX15/EX11, RN03/RN06/RN07–RN09/ACC01 | Keep direct-terminal closure and payable-queue continuity open; reuse shared report-cycle/receipt boundaries before connected accountant acceptance. |

[Copy-ready action prompts and acceptance](plan/REIMBURSEMENT-RELIABILITY-2026-10-27.md).
The report's 81 semantic records are not a runtime denominator; new EX18–20 are
source-inspected and zero of three newly runtime-verified. No report instruction
is automatically executed. Preserve existing EX09/manual-recovery implementation
and UI follow-ups CTX-02-A/B/C and CTX-03/04/05 instead of duplicating completed work.

## CTX-02 project-body reflow candidate — 26 October 2026

[Implementation, action contracts, evidence and individual follow-up prompts](design/PROJECT-BODY-REFLOW-2026-10-26.md).
Keep all profitability/evidence text and controls within narrow/enlarged pages;
use named focusable local table scrolling while preserving figures, query/write
contracts and original actions. The bounded candidate passes focused behavioral
and independent source/render/keyboard checks on the final PR #82 security parent
`f100c20`. All 840 local functional cases pass without retries; static/build/bundle
checks pass. Thirteen local golden mismatches also occur on the unchanged control
with byte-identical actual PNG pairs; no visual-suite pass is claimed. Fresh
hosted qualification and human/Lead review remain required; no release is claimed.

New inherited findings remain open with individual prompts in that report:
**CTX-02-A** native keyboard access to both file pickers; **CTX-02-B** column-header
relationships for four tables; **CTX-02-C** meaningful currency/row labels for
adjustment amounts. A suspected photo overflow did not reproduce and is not a
confirmed defect. Retain CTX-03 switch-write outcome recovery, CTX-04 picker
containment and CTX-05 Suppliers baseline/DOM investigation as separate work.

## Embedded Node client follow-up — 26 October 2026

The first repair's hosted image scan is clean, but Node 22.23.3 embeds a second
affected client. [Confirmed finding, narrow repair and maintenance prompt](security/NODE-CORE-FOLLOWUP-2026-10-26.md).
The candidate pins official 22.23.4 in the builder and three CI inputs, retains
the separately verified npm patch, and corrects six ambiguous VAT-test labels.
Native identity/build/ordinary-HTTP/scan checks pass; require fresh complete
hosted CI and human/Lead review before integration. No security gate is waived.

## Image-security gate repair — 26 October 2026

[Original findings, patch provenance, validation and rollback](security/IMAGE-GATE-REPAIR-2026-10-26.md).
PR #82's original image job failed with ten HIGH rows across three advisories;
the gate worked correctly. Require fixed Alpine Brotli/curl packages in affected
runtime/build stages, apply the verified supported undici maintenance patch in
npm's build tooling, and pin official RustFS 1.0.4 without altering storage/user
settings. Existing 92 image/compiler/supply contracts pass; five native scan
categories, actual health/Node 22 compatibility and synthetic old-to-new storage
checks pass. Clean fresh combined CI and human/Lead review remain required.

Keep this ahead of CTX-02/03/04/05. Do not hide targets, widen severity rules,
silence advisories or call an unavailable image a clean scan. A metadata-only
package check and old-to-new read do not establish production rollback safety.

## Organization-list recovery — 26 October 2026

[CTX-01 action contracts, qualification and follow-up prompts](design/ORG-SWITCHER-RECOVERY-2026-10-26.md).
The candidate is based on PR #79 at `47b28e4`: retain verified company context,
explain membership-list loading/errors, provide explicit rate-limit-aware GET
retry, and preserve account/session isolation. It changes no server limiter,
permission, financial value, provider or migration. All 829 local browser cases,
real enabled-limiter/session-isolation acceptance and static/build/bundle gates
pass. Fresh hosted CI and human/Lead review remain required before integration.

Keep **CTX-02** project-body enlarged-text reflow and **CTX-03** existing explicit
switch-write failure/uncertain-outcome handling as separate prompted tasks.
The latter is a source-review finding, not evidence of a wrong-company write or
tenant disclosure. **CTX-04** records paired ready-picker popup clipping;
**CTX-05** records Suppliers full-page capture overflow, requiring same-state
baseline/DOM measurements before repair claims. Both have their own prompts.
Wider audit and release work remain open below.

## Invoice and project context — 23 October 2026

[IMP-09/10 implementation, action prompts and qualification](design/IMPECCABLE-CONTEXT-FIXES-2026-10-23.md).
The new candidate is stacked on PR #78 at `97178b2`: current supplier/due/workflow
context, source-currency invoice totals, responsive worklist facts and permitted
project parent/Projects-tab return. Browser-history draft context and pending
mutation protections pass focused tests and independent review. Actual
browser/API/PostgreSQL invoice/project journeys and two-company isolation also
pass. All 809 local browser tests pass without retries, alongside backend
compatibility and static/build/bundle gates. [Completed hosted qualification](design/IMPECCABLE-PR79-HOSTED-2026-10-26.md)
adds 4,535 backend passes / 184 skips, 202 PostgreSQL cases, 809 functional and
13 ordinary visual checks, and ten clean image targets. Human/Lead review and
main integration remain required. These are 23 October results, not fresh
26 October vulnerability-feed evidence or clearance of the new recovery code.
No migration, dependency or financial-calculation change is introduced.

Two bounded follow-ups are logged with evidence and prompts in the delivery
record: **CTX-01 (P2)** truthful organization-list read failure/retry after an
actual 429; **CTX-02 (P2, existing)** project-body enlarged-text reflow. The former
does not establish tenant exposure, and the latter is not complete page reflow.
Keep the existing limiter and identity/cache safeguards while implementing them.

[Preceding PR #78 completed hosted evidence](design/IMPECCABLE-PR78-HOSTED-2026-10-23.md)
verifies all eligible core/supply-chain jobs, including ordinary visual checks and
ten image targets with zero HIGH/CRITICAL rows. Those scans ran on 22 October;
they are not a fresh 23 October feed or clearance of this next candidate.
Main integration, human/Lead review, planning's sibling migrations, customer
invoice acceptance and deployment remain separate. Preserve the wider queue below.

## Impeccable priority repair candidate — 22 October 2026

[Implementation, action contracts and qualification boundaries](design/IMPECCABLE-PRIORITY-FIXES-2026-10-22.md).
IMP-01 through IMP-06 and IMP-17 are implemented in a candidate stacked on selected-region OCR PR #77:
truthful source-currency labels, dashboard read recovery, readable shared
controls/captions and visible focus, responsive tab strips/dialogs and contained
Automation tables. Qualification belongs to this candidate, not main or Hostinger.
The historical audit below remains intact. Shared semantic roles are only the
foundation of IMP-15; broad status consolidation and full-page enlarged-text
reflow remain open. The Currencies dialog fits at 200% text, while its background
page still needs reflow work.

The fresh branch audit also found `pypdf` 6.21.0 advisories; the minimal secure
6.21.2 pin and its independent PDF compatibility/audit evidence are tracked in
the repair record. No advisory is ignored. A single Claim status lazy-import
failure in the first broad local browser run remains an investigation event:
capture its original cause if it recurs, rather than calling it a repaired
financial or permission defect. Final local qualification passes 781 browser
checks, 136 PDF compatibility tests and build/bundle gates; fresh requirements
auditing reports zero known vulnerabilities. The wider release gates and
human/Lead review remain required.

CI follow-up: online workflow security found seven stale setup-python major-tag
annotations. All 23 floating-major annotations across four workflows now name
verified exact releases, with action SHAs, parsed YAML and runtime behavior
unchanged. Online security validation reproduces the original failure and
passes the corrected annotations; see the repair record for evidence.

## Impeccable design audit — 22 October 2026

[Audit, evidence and individual development prompts](design/IMPECCABLE-AUDIT-2026-10-22.md)
review current main `90891d8d`, with pending PRs #75/#76/#77 identified separately.
Expert rubric score: **11/20**. Seventeen current-main findings: seven P1,
ten P2; no P0 established in the reviewed scope. This is an audit, not a UI
repair, release clearance or WCAG certification.

Work through these groups one at a time; preserve existing financial behavior,
permissions, review identity and performance gates:

| Order | Findings | Outcome / development direction |
| --- | --- | --- |
| 1 | IMP-01 | Show the reviewed source currency beside capture confirmation; no implicit EUR or FX. |
| 2 | IMP-02, IMP-03 | Readable meaningful text/action colors and visible keyboard focus in shared controls. |
| 3 | IMP-04, IMP-05, IMP-17 | Contained mobile tabs/tables and reachable enlarged-text modal controls. |
| 4 | IMP-06 | Explicit, recoverable Dashboard Next actions read failures. |
| Integration checks | IMP-07, IMP-08, IMP-16 | Schedule team-failure recovery and calendar feedback already addressed in pending #76; task-based menu grouping already addressed in #75. Qualify those implementations rather than duplicate them. |
| 5 | IMP-09, IMP-10 | Invoice supplier/due/workflow context and correct project navigation parent. |
| 6 | IMP-11, IMP-12, IMP-13 | Bounded photo previews, optional lazy analytics and settled/cancelled invoice search. Measure requests/bytes. |
| 7 | IMP-14, IMP-15 | Mobile hit-area policy and shared semantic state roles, preserving compact desktop workflows. |
| Final check | All repaired items | `/impeccable polish`, then repeat the audit on the same source/state/viewport fixtures. |

All 17 findings have evidence, impact, acceptance criteria and a development
prompt in the report. Broader security/release work and existing backlog items
remain open. The audit itself changed no application code or Hostinger deployment.


## Previous candidate and qualification snapshots

**Current candidate — selected-region OCR review, 21 October 2026:** the user
approved visual selection and rereading. The candidate is based on OCR/navigation
PR #75 at `0bbeb26e`; it is separate from the work-order planning candidate.
The original invoice remains unavailable. [Delivery record and action prompts](reviews/OCR-REGION-REVIEW-2026-10-21.md).

| Work / finding | Current status | Next acceptance / prompt |
| --- | --- | --- |
| OCR-REGION-01 through 05 | Selection, original-quality bounded reads, comparison, explicit field/table apply and command recovery implemented | Final focused backend 93, PostgreSQL 24, runtime 66 and full functional browser 747 pass; populations overlap. [Receipt](reviews/OCR-REGION-DELIVERY-2026-10-21.json) distinguishes final checks from preceding full-backend evidence. Hosted CI and customer acceptance remain required. |
| OCR-MEMORY-01 correction learning race | Actual parallel PostgreSQL region applications reproduced a unique-constraint error; atomic shared memory upsert/increment repaired and regression green | Keep observation/correction counts atomic across captures and exclude bank instructions; verify fresh hosted PG cohort. |
| OCR-CONFIRM-01 stale final invoice confirmation | Repaired with saved-review base checks, canonical submitted-draft comparison and authority refresh after lock wait | PostgreSQL and actual stale-tab browser/API checks pass. Missing base is mandatory after region apply; legacy captures without apply retain optional-token compatibility. Preserve response-token and explicit reload behavior. |
| OCR-LOCK-01 admission deadlock | Three actual PostgreSQL failure-first interleavings reproduced deadlocks; admission now uses compatible `FOR NO KEY UPDATE` | Append, manual review and worker publication controls pass while organization admission stays serialized. Keep these tests in hosted PostgreSQL CI. |
| Go build-tool advisory CVE-2026-111943 | Inherited Go 1.27.2 pins replaced with 1.27.3; qualified native TypeScript compiler rebuilt | Parallel planning candidate's four affected images scan clean against current feed. This OCR candidate still needs its own hosted image gates. |
| OCR-TABLE-01 whole-document spurious rows | Native synthetic vertical-table baseline produced two spurious initial rows; no invoice created from that fixture | Preserve the fixture, inspect word/column geometry and add missing/extra-row assertions before changing parser heuristics. Region selection/appending is qualified recovery, not whole-page accuracy acceptance. |
| OCR-ACCEPT / PROFILE / PAGES / broader RETRY / PADDLE / VLM | Remain open; selected-region part of RETRY now implemented | Follow the [ordered OCR plan](plan/OCR-CONTROL-PANEL-2026-10-21.md). No new provider/model is installed by this slice. |

No merge or deployment is included in this local qualification. Planning PR #76
and this branch have sibling migrations from PR #75; combined integration must
resolve their graph and rerun qualification. Existing live version and the
broader backlog remain separate.

**Earlier candidate — OCR and navigation, 21 October 2026:** the customer reports

**Current planning candidate — 21 October 2026:** clearer Board/Calendar views,
revision-aware edits and explicit email controls are implemented alongside durable
Google/Outlook outbound export. Local qualification and live provider activation
remain distinct. [Workflow, setup, evidence and ordered prompts](reviews/WORK-ORDER-PLANNING-2026-10-21.md).

| Work | Current status | Next acceptance |
| --- | --- | --- |
| PLAN-BOARD / EDIT / DATES | Implemented; targeted browser checks | Shared records/filters, legal transitions, stale revision/readback recovery and DST-safe all-day dates; hosted checks pending. |
| PLAN-DELIVERY / CALENDAR | Implemented; local mocked-provider and PostgreSQL qualification | Owner app registration, per-user consent and actual Google/Microsoft/email acceptance remain required. Outbound only; no phone-refresh SLA. |
| PLAN-CI-CONTRACT | Passed hosted backend/lint at 7216f951 | Additive OpenAPI snapshot and central response-budget policy; two hosted failures reproduced, 144 focused checks pass. [Follow-up](reviews/WORK-ORDER-PLANNING-2026-10-21.md#hosted-ci-follow-up). |
| PLAN-CI-GO1273 | Locally repaired; fresh hosted qualification pending | Go1.27.3 replaces inherited vulnerable toolchains; four affected outputs scan clean, 76 contracts and native frontend/PostgreSQL checks pass. [Evidence](security/CI-GO1273-2026-10-21.md). |
| PLAN-API-DOCS | Open follow-up | Add typed response models for calendar/notification list/retry endpoints and document callback303 accurately. Preserve runtime serializers and permission boundaries. |
| PLAN-ACCEPT | Next activation step | Run connected provider pilot after qualified deployment; never post secrets in chat. |
| PLAN-CREATE | Open | Durable create command/status lookup; current uncertain save is locked for GET review, not automatically posted again. |
| PLAN-LIFECYCLE / INBOUND / CAPACITY / RETENTION | Ordered follow-ups | Standalone unscheduled work orders, reviewed two-way ownership, measured VPS queue latency and optional remote cleanup. |

**Current candidate — OCR and navigation, 21 October 2026:** the customer reports
only the supplier name was read correctly and requests clearer menus plus an OCR
control panel. Bounded parser repairs, accessible menu grouping and the existing
capture review/failure control surface are locally qualified against main
`90891d8d`. [Implementation and evidence](reviews/OCR-AND-NAVIGATION-2026-10-21.md).

| Work | Current status | Next acceptance |
| --- | --- | --- |
| OCR percentage/currency/localized labels and default provenance | Implemented; 135 focused backend cases pass | Customer invoice regression still requires the actual source. Localized label parsing is not Latvian OCR language qualification. |
| Grouped navigation | Locally qualified; hosted checks pending | Keep all 50 route/permission/module tuples, genuine menu reachability and account/organization-scoped expansion. Project-detail parent alias remains open. |
| OCR-CP01 through CP04 control panel | Locally qualified; hosted checks pending | Truthful review/failure counts, read-only refresh, existing source/correction actions and qualified method scores. No all-processing or accuracy claim. |
| Hosted dependency findings | Repaired and locally qualified; fresh hosted checks pending | Pillow 13: 207 compatibility passes and zero Python advisories; follow-redirects 1.16.1: zero npm advisories, lint/build/bundle pass. Gates unchanged. |
| OCR-ACCEPT / PROFILE / PAGES / RETRY / PADDLE / VLM | Ordered follow-ups | [Pinned upstream research and complete action prompts](plan/OCR-CONTROL-PANEL-2026-10-21.md). Worker-reported language inventory and immutable attempt settings precede engine/profile selectors. |

The user invoice is unavailable; this candidate does not establish that its
extraction failure is fixed. No new OCR/model dependency or production change is
included. Historical findings and the wider backlog below remain separate.

**Current development — manual expense draft completion, 20 October 2026:**
EX09 and EX05-MANUAL are implemented in the isolated candidate based on main
`e5b101756c83f1668925e44e667bc0ae72f564d4`. The repair adds exact cent amounts,
one frozen save intention and a validated, scoped outcome lookup. Known saves
stay distinct from failures to load their current report; retries use the original
key and inputs only while that frozen intention remains available in memory.
[Implementation and qualification](security/R010-MANUAL-DRAFT-2026-10-20.md); [action contracts](plan/R010-MANUAL-DRAFT-2026-10-20.md).
The bounded manual slice is locally qualified: 44 helper, 43 mocked UI and
three fresh actual browser/API/PostgreSQL cases pass. Fresh hosted full
qualification is pending; local full-suite/visual success is not claimed.
No backend product source, schema, dependency, monetary rounding contract or
visual baseline is changed. The complete R010 parent and broader backlog remain open.

**Completed preceding CI batch:** [PR #70](https://github.com/kristapsgoncoronoks-ship-it/SME-financial-workspace/pull/70)
merged normally into main `e5b10175`, including dependency PRs #68/#69.
[Fresh main CI](https://github.com/kristapsgoncoronoks-ship-it/SME-financial-workspace/actions/runs/39439209083)
passed all seven required jobs, both eligible supply-chain jobs and all four
release jobs; the deployment job passed. Backend: 4,421 passed / 156 skipped;
browser: 620 + 13 unchanged visual checks. Ten image targets had zero
HIGH/CRITICAL rows; all 20 report hashes and six native storage groups were
verified. These results qualify that preceding main, not the new manual repair.
The deployment public-health URL is unset; live entrypoint, served revision and
public readiness remain unverified. Historical PII classification, SSH trust,
qualified deployment revision/cancellation, provider/cutover and the original
working copy's separate UI/auth/directory/AP edits remain open or excluded.

## Active bounded manual-expense slice

| Contract | Implementation state | Required closeout |
| --- | --- | --- |
| WF-14 / EX09 Save draft | Exact amounts, frozen keyed intention, known commit/readback separation and original-key retry locally qualified | 44 helper, 43 UI and three actual manual cases pass; fresh hosted full qualification remains pending. Legacy rounding and atomic server semantics are unchanged. |
| WF-14 / EX05-MANUAL outcome lookup | Typed receipt validation and GET-only scoped recovery locally qualified | Two actual postcommit recovery activations return one ID with unchanged independent PG counts; fresh hosted full qualification remains pending. |
| Manual command identity boundary | Token/generation guard added around async hashing and recovery | Final helper/session controls pass. Synthetic stale-dispatch reproduction is not a cross-company mutation; the server identity fence remains authoritative. Hosted qualification pending. |
| Manual form navigation lifetime | Save captures a controller and unmount aborts helper/Axios work; Cancel does not abort server work | Final navigation regression passes; held obsolete form does not dispatch, and after-dispatch abort retains its reference. Earlier broad browser results remain intermediate; hosted qualification pending. |
| Manual status lookup lifetime | Separate lookup controller plus optional shared status-helper signal guards dispatch and metadata clear | Final status-navigation regression and independent abort control pass; an old response retains current-form evidence. Hosted qualification pending. |

Other manual-editor controls, submission/approval/reimbursement, receipt handling
and remaining R010 contracts keep their separate acceptance scope. This slice
adds no new release gate or provider/retention decision.

## Prior publication-time snapshots

**Current follow-up candidate — 20 October 2026:** configurable database
backups and independent Playwright job-pin validation are implemented and
locally qualified. The isolated candidate also includes qualified dependency
PRs #68/#69; their separate earlier checks do not qualify this combined head.
[Backup identity, real PostgreSQL restore evidence and limits](security/CI-CUSTOM-DB-BACKUP-2026-10-20.md);
[own-job validation, failure-first controls and scope](security/CI-PLAYWRIGHT-OWN-PIN-2026-10-20.md). The new candidate
collects 4,577 backend cases; 118 focused checks pass. Full hosted qualification
remains pending. Existing main `d772a366` passed all seven required CI jobs,
release, eligible supply-chain and deployment jobs; public readiness and the
live entrypoint/served revision remain unverified. Historical PII classification,
trusted SSH identity, exact deployment revision/cancellation and other workflow
items remain open. The original workspace's uncommitted UI/auth/directory/AP
changes are not part of this isolated candidate.

## Earlier review and qualification snapshots

**20 October rollout follow-up — CI-20-VPS-QUIESCE:** the capture migrations'
writer-stop preflight was missing from the unchanged Hostinger deploy script.
Build first, stop/verify all API/extract writers, verify quiesced backups, then
start prepared images. Preparation recovery resumes existing containers only;
post-start failures never automatically revive old queue consumers. Twenty
executable Bash regressions pass; fresh hosted qualification/main integration
remain pending. [Repair, evidence and operational boundaries](security/CI-VPS-WRITER-QUIESCE-2026-10-20.md).
Hostinger's existing local storage does not require the separate S3 provider
choice. The current deployment variable is unknown, not assumed disabled.

**20 October CI integration:** [scan, findings and repair evidence](security/CI-SCAN-AND-REPAIR-2026-10-20.md) records the expense branch's older image baseline, the no-op migration join, stronger PostgreSQL execution coverage, independent audit evidence and verified host compiler. Native functional checks pass; exact-head CI and Lead review remain required. Historical PII classification stays owner-dependent. Earlier uncommitted UI/password/directory changes in the original checkout are separate.

## Current findings and work order — 19 October 2026

The [current TODO entry point](../TODO.md) links to this queue. The [known findings register](reviews/FINDINGS-REGISTER-2026-10-17.md) consolidates
confirmed findings, suspected defects, verification gaps, rejected image candidates,
owner decisions and implemented repairs. It preserves all 33 original scan-time
backend advisory IDs and links their exact package/version evidence. Work through its queue
one item at a time; blocked inputs stay visible while the next unblocked item proceeds.
Completed baseline `0d81c0f7d244c911d739a25db2ef49a6ca9f50c2` passed all seven
required jobs in [CI 39256160223](https://github.com/kristapsgoncoronoks-ship-it/SME-financial-workspace/actions/runs/39256160223)
and [supply-chain 39256160225](https://github.com/kristapsgoncoronoks-ship-it/SME-financial-workspace/actions/runs/39256160225):
**4,248 backend passes / 122 existing skips**, **140 PostgreSQL checks plus six
growth checks**, and **620 browser / 13 visual passes**.
The [19 October image repairs](security/CI-IMAGE-REPAIR-2026-10-19.md) and
[RustFS development replacement](security/CI-STORAGE-REPLACEMENT-2026-10-19.md)
now have hosted evidence for **ten images with zero HIGH/CRITICAL findings** and
all six native storage groups passing with cleanup confirmed; all 20 SBOM/audit
hashes were verified. This baseline includes the local/S3 read repairs. Export
redemption and artifact-integrity repairs below are implemented and locally
qualified; their exact-head CI remains pending. Human review remains open. Native denied/offline checks
cover real 403/client-loopback refusal and recovery, not stopped-provider or
midstream fault qualification.
Production bootstrap explicitly refuses pending provider/cutover approval.
The earlier [47-row
inventory](security/SYS-04-IMAGE-COVERAGE-2026-10-17.md) is preserved as historical
evidence. Host CI's original TypeScript executable remains a separate tooling
exposure, CI-HOST-TSC; the Docker compiler rebuild does not cover that path.

**SYS-01 OIDC destination validation is implemented, pending CI/human review:**
HTTPS/connect-time public-address pinning, issuer binding, no proxy/redirect bypass
and sanitized failures; [repair evidence](security/SYS-01-OIDC-DESTINATIONS-2026-10-17.md).
**SYS-02 Compose readiness is implemented, pending CI/human review:** Python probes
preserve database readiness and timings; six real renders and native-image
failure/recovery/dependency checks passed. [Evidence](security/SYS-02-COMPOSE-READINESS-2026-10-17.md).
**SYS-03 fresh-volume permissions is implemented, pending CI/human review:** private
UID/GID 10001 mount target, real-image CI gate and native API/worker/refusal/restart
[evidence](security/SYS-03-STORAGE-VOLUME-2026-10-17.md). Existing nonempty ownership
is preserved; incompatible legacy volumes need an operator-reviewed migration.
**SYS-04 runtime repair/inventory engineering is implemented, pending CI/review:**
qualified Alpine Python/OCR runtime, patched origin libexpat, pinned existing PG16
digest and fail-closed build/final/manifest-service scanning. The subsequent build
and PostgreSQL repairs and PgBouncer1.26.1 qualification are implemented; the
complete ten-image gate passed on the cited baseline. Production provider/cutover
approval remains [Decisions §29](DECISIONS-NEEDED.md#29-residual-nativebuild-image-findings-v05b-2026-10-16).
**CAP-RETRY is implemented, pending CI/human review:** durable capture generations,
keyed retry jobs, atomic result/evidence publication and fenced progress preserve
newer attempts and reviewed/saved data. [Repair and evidence](plan/CAP-RETRY-ATTEMPT-FENCING-2026-10-18.md).
**CAP-COMMAND is implemented, pending CI/human review:** durable per-file receipts,
payload-bound replay before quota, authorized status lookup and account/workspace
browser recovery after response loss or refresh. Purging a capture retains its
receipt. [Repair and evidence](plan/CAP-COMMAND-DURABLE-ADMISSION-2026-10-18.md).
**CAP-RESOURCE handler repair is implemented, pending CI/human review:** all eleven
remaining whole-file upload reads now use purpose-bound cap+1 reads and close on
refusal/failure/cancellation; native API/proxy/browser recovery passed.
[Repair, measurements and remaining limits](security/CAP-RESOURCE-HANDLER-BOUNDS-2026-10-18.md).
**CAP-INGRESS API controls are implemented, pending CI/human review:** pre-parser
wire/header/file-count bounds, two upload slots per worker, deployment defaults
matching the existing 20 MiB proxy cap and browser batch packing/retry recovery.
[Native disk-spool, capacity and workflow evidence](security/CAP-INGRESS-BOUNDARIES-2026-10-18.md).
**SYS-10 is implemented, pending exact-head CI/human review:** development upload
handshakes retain known refusals, use separate upstream connections and report
transport-only outcomes explicitly. Real-Vite protocol/TLS and native browser/API/
worker recovery passed. [Repair/evidence](security/SYS-10-DEV-UPLOAD-PROXY-2026-10-18.md).
**CAP-CAPACITY ingress repair is implemented, pending CI/human review:** body-read
deadlines, shared origin upload admission and streaming, with measured four-worker
and authorized near-envelope decode evidence. **CAP-REGISTRY**, the shared-document
race found during that run, is repaired with caller-owned PostgreSQL/SQLite
transaction evidence. [Repair and limits](security/CAP-CAPACITY-INGRESS-2026-10-18.md).
**CAP-DISK-FULL is implemented, pending CI/human review:** real exhausted staging
now reports a scoped 503 with actionable guidance. Bounded local staging/object
faults, cleanup, preserved originals and browser same-key recovery passed.
[Repair and limits](security/CAP-DISK-FULL-2026-10-18.md).
**CAP-OBJECT-READ is implemented, pending CI/human review:** bounded worker source
reads, S3 body closure, visible size failures and real two-worker text/PDF/OCR
recovery passed. [Repair and measured limits](security/CAP-OBJECT-READ-2026-10-18.md).
**CAP-WORKER-SOURCE-HASH is implemented, pending CI/human review:** verified
bounded originals, explained mismatch failures and original restoration passed on
both channels. [Repair](security/CAP-WORKER-SOURCE-HASH-2026-10-18.md).
**CI failure sweep:** all 1,039 visible runs and current branch failures were
inspected directly. Readable image-gate summaries are implemented; vendor/history
blockers remain. [Inventory and repair](security/CI-FAILURE-SWEEP-2026-10-18.md).
**CI-DEPS-SQLA21 and PERF-HARNESS-STARTUP are implemented, pending CI/Lead review:**
2.1.4 result/type compatibility and stronger nested tenant scoping pass PostgreSQL
checks; benchmark startup now runs once even in an already partially frozen
embedding runtime, preserving the original growth limits.
[Qualification, failing regressions and paired measurements](security/CI-DEPS-SQLA21-2026-10-18.md).
**CI-DEPS-BACKEND-REST is integrated, pending exact-head CI/Lead review:** the other
nine backend updates preserve sampled money behavior and same-database command
replay across the shipped-image upgrade. The prepared OpenAPI change removes
only 412 Decimal patterns; server validation remains authoritative.
[Semantic diff, financial action checks and runtime qualification](security/CI-DEPS-BACKEND-REST-2026-10-18.md).
**CI-DEPS-FRONTEND and page-module recovery are implemented, pending CI/Lead review:**
the exact PR #61 lock and matching Playwright 1.63 images pass the unchanged bundle
limits at 434.3/128.2 kB. Rejected shell/page/public portal downloads have actual
reload recovery. [603 broad / 22 final focused browser checks, compiled-origin
and audit evidence](security/CI-DEPS-FRONTEND-2026-10-18.md).
**UI-PORTAL-READ-TRUTH is implemented, pending new-head CI/Lead review:**
invalid/refused links have contact guidance; transient read failures have a finite,
pending-guarded GET retry, Retry-After cooldown and sanitized public error text.
Offer-decision POSTs are not retried by that recovery. All 620 browser cases and
20 focused portal cases pass; native PostgreSQL proves committed-read recovery
and one actual decision audit/plan. [Evidence and review limits](security/UI-PORTAL-READ-TRUTH-2026-10-19.md).
**SYS-04-ORIGIN-LIBX11 is implemented, pending new-head CI/Lead review:** the
refreshed DB found HIGH CVE-2026-88806 in Alpine origin libx11 1.8.13-r0. The
runtime now requires 1.8.14-r0; its rebuilt image scans clean and passes that native
flow. [Rejected image, fixed package and proof](security/UI-PORTAL-READ-TRUTH-2026-10-19.md#fresh-origin-finding-and-package-repair).
The complete ten-image build/service gate passed on the cited 0d81c0f baseline;
the subsequent export work needs its own CI qualification and human review.
**CAP-VERIFY-STREAM / CAP-PREVIEW-READ are implemented, pending new-head CI/Lead review:**
integrity hashing uses fixed 64 KiB reads without rejecting supported historical
files; generated page downloads enforce their existing 512 KiB budget and explain
oversized storage failure. [113 focused, 131 required PostgreSQL and native corruption/restoration/worker evidence](security/CAP-READ-BOUNDS-2026-10-19.md).
Native Uvicorn peaks at 182.55 MiB under a 256 MiB cgroup; file cache reaches the
ceiling with reclaim events and no OOM. This is a bounded profile, not fleet headroom.
**CAP-ISSUED-ATTACHMENT-READ is implemented, pending new-head CI/Lead review:**
issued attachments bound reads by their recorded admission size, preserve historical
and legacy-empty downloads, and explain oversized replacement failures without a
partial attachment. Exact restoration recovers the download; existing post-I/O
authorization/visibility checks remain. [Local/S3 regressions, PostgreSQL revocation and runtime evidence](security/CAP-ISSUED-ATTACHMENT-READ-2026-10-19.md).
Historical head 584d2ad6 passed all functional/supply-chain CI; its dated expanded
image gate recorded 47 advisory rows and three unavailable references. That
snapshot is preserved; the later 0d81c0f ten-image pass is recorded above and
does not qualify the subsequent export work or clear human review.
**CAP-LOCAL-READ-ERROR is implemented, pending new-head CI/Lead review:**
local open/read/close filesystem failures are sanitized `StorageReadError` responses,
distinct from actual missing objects. Failed local export reads abort builds and
downloads; recovery preserves one-time links and successful-download audit truth.
Cleanup preserves primary refusal/interruption errors. [Fault reproduction, recovery and qualification](security/CAP-LOCAL-READ-ERROR-2026-10-19.md).
**CAP-DIGEST-ERROR-CLASS is implemented with CI passed, pending Lead review:**
local digest lookup remains distinct from post-open filesystem errors, which now
produce sanitized retry findings. Cleanup preserves the primary read/hash failure
or cancellation when a secondary filesystem close fails. Fixed 64 KiB reads and
historical-file support remain. The adapter baseline had **14 failed / 21 passed**;
the HTTP baseline had **3 failed / 2 passed**. **40 new cases** now accompany the
**194 focused passes / 80 warnings**. [Reproduction, repair and limits](security/CAP-DIGEST-ERROR-2026-10-19.md).
The local repair is included in the cited green 0d81c0f baseline.
**CAP-S3-READ-ERROR-CLASS is implemented with CI passed, pending Lead review:**
known missing lookup retains its static response; provider, malformed-body,
read/truncation and close failures become sanitized `StorageReadError` outcomes.
S3 get/digest preserve primary interruptions through secondary I/O cleanup
errors, and export builds/downloads preserve complete artifacts and recovery state.
The **55-case adapter baseline had 34 failed / 21 passed**; four denied-export
cases failed while two real-missing controls passed; the corrected root HTTP
baseline had **4 failed / 4 passed**. **All 77 new cases** and the **260-case focused
selection passed / 112 warnings**. All six native checker groups passed on the
rebuilt image with cleanup confirmed, including actual adapter wrong-credential
403/client-loopback `EndpointConnectionError` and subsequent normal-read recovery.
Included in the cited green 0d81c0f CI; stopped-provider/midstream fault qualification
and Lead review remain separate. [Reproduction, repair and qualification limits](security/CAP-S3-READ-ERROR-2026-10-19.md).
**CAP-EXPORT-REDEEM and CAP-EXPORT-ARTIFACT-INTEGRITY are implemented and locally
qualified, pending exact-head CI/Lead review:** PostgreSQL review reproduced
duplicate HTTP 200 redemption and HTTP 200 downloads of corrupt stored artifacts.
[Repair and qualification](security/EXPORT-REDEMPTION-AND-INTEGRITY-2026-10-19.md).
The shared token fix also affects email verification/password reset: PostgreSQL
controls pass for single-use locking, expiry after lock waits and caller-owned
rollback. Production rejects SQLite; its development concurrency remains unproven.
Export-row serialization and size/SHA refusal precede download/audit publication.
The multi-archive `LIMIT 1` regression passes; the exact no-limit negative control
fails its row-lock assertion as intended.
**23 new artifact tests and 265 existing recovery tests passed**; **30 new
PostgreSQL cases** comprise 12 export HTTP and 18 shared-auth cases. The complete
required PostgreSQL selection passed **170 cases in 78.83 s**, with **six growth
checks in 11.97 s**. Static controls passed **121 cases / 22 warnings**; full Ruff
check/format covers **843 files**, mypy passes **422 sources**, and **4,423 tests
were collected**, not all executed in this local qualification.
Native testing passed **108 cases across three fresh 1 CPU / 384 MiB containers**,
with **422 application hashes and 422 fixture hashes verified**, no reported OOM
and cleanup confirmed. The initial combined 108-case process exited **137 for an
unknown reason** and is preserved; these grouped passes do not establish minimum
hardware acceptance or explain the first process exit.
Legacy unknown sizes retain unbounded reads with SHA verification rather than a
new admission limit; this does not close capacity work. Archive's newest-ready
selection is retained without a token/artifact binding migration. Purge races and
post-commit lost-response/re-download policy remain separate.
**After the current slice:** the remaining 15 CAP-CAPACITY original/download/render/delivery/export
reads, worst-case decoding, shared disk and fleet/headroom work in order. The resource finding is
not globally closed. The [original 18-call inventory](security/CAP-OBJECT-READ-2026-10-18.md#19-october-follow-up-remaining-stored-read-inventory)
is preserved; remaining runtime reproduction and compatible per-purpose contracts
are in the new evidence. SYS-11 startup seed warnings and CAP-EMAIL-REPLAY's untested
same-message concurrency are logged in the register.
The register has one ordered queue and per-item prompts. Kubernetes candidates and PG schema-drift tooling
are also logged by the scan. New follow-ups CAP-REVIEW-VERSION (stale completed
drafts), SYS-08 (historical PostgreSQL downgrade portability) and SYS-09 (browser
repeatability) are in the register. SYS-01–SYS-04 are subsequent repairs; nothing is release-cleared.
Older row labels below retain implementation-time wording; use the register for
current status. The earlier ~93% row-count estimate is not release readiness or
product-vision progress.

**Deployment direction discussed on 18 October:** Kubernetes can support
multi-server scheduling and rolling deployment. For a single-server pilot,
the existing Compose path is simpler and lighter. SYS-05 retains Kubernetes
manifest qualification, alongside image, storage, backup and release acceptance.

The single, controlled list of **build work and tech-debt that is unblocked** — i.e.
things an engineer can pick up now without waiting on anyone. It is deliberately
scoped to complement the two registers that already exist, not duplicate them:

| Register | What lives there |
|---|---|
| **This file** | Unblocked engineering work + tech-debt, prioritised Now / Next / Later. |
| [`DECISIONS-NEEDED.md`](DECISIONS-NEEDED.md) | Work **built to a boundary** that can't finish without an external **decision, credential, or infra** (SSO/SCIM/SAML live, billing go-live, ERP exporters, residency, prod KEK, public API GA, SOC 2 / ISO). |
| [`product/product-requirements.md` §10](product/product-requirements.md) | **Product** priorities (Must / Should / Could / Later) — the *what to sell*, not the *what to build next*. |

An item leaves this file when it ships (move it to **Recently shipped**) or when it
turns out to need a decision (move it to `DECISIONS-NEEDED.md`). Keep each item to
one line of *what* + one of *why* + a size and a source, so the list stays scannable.

Size: **S** ≈ <½ day · **M** ≈ 1–3 days · **L** ≈ >3 days. Every item must land with
tests and keep CI green.

---

## Security remediation — Now, merge blockers

Queued from the [2026-10-16 vulnerability analysis and fix plan](security/VULNERABILITY-ANALYSIS-AND-FIX-PLAN-2026-10-16.md).
**V01a and V01b are implemented locally, pending CI/review**; V02/V03, V04, V05a, V06a and V07a are implemented locally. V05b's ten-image 0d81c0f qualification is recorded above; provider/Lead review remains and nothing is release-cleared. [V01a evidence](security/V01A-PYPDF-UPGRADE-2026-10-16.md). Clean isolated
candidate audits alone are not implemented fixes. This sequence takes priority over routine feature expansion.
Engineering prompts, advisory evidence and full acceptance checks remain in the
linked plan; these rows track the work without duplicating the findings register.

| # | Item / completion criterion | Why / dependencies | Size | Source |
|---|---|---|---|---|
| V01a · P0 | **Upgrade pypdf narrowly to audited 6.21.0 — implemented locally.** 203 PDF/intake/output regressions and Python 3.11/3.14 library smoke checks passed; fresh audit has no pypdf findings. [Evidence](security/V01A-PYPDF-UPGRADE-2026-10-16.md); JWT repair is recorded below. Current image-gate baseline evidence is recorded above; human/provider release gates remain. | First repair: eight observed advisories; untrusted uploads reach affected parsing. Keep unrelated backend upgrades out. | S | Plan §1 / V01 |
| V01b · P0 | **Bound untrusted PDF parsing in a killable process — implemented locally.** Input/decoded-output, attachment-count, CPU/memory/wall limits, progress IPC and explicit persisted failure/recovery; [evidence](security/V01B-PDF-PROCESS-2026-10-16.md). | Continue V01a. Existing async/thread deadlines and the separate preview renderer do not bound attachment extraction. | M | Plan §1 / V01 |
| V02/V03 · P1 | **Replace python-jose with PyJWT[crypto] 2.15.1 and require OIDC claims — implemented locally.** Missing-claim regressions reproduced first; legacy HS256/rotation, RSA-only OIDC, strict claims/JWKS and production key policy verified. Fresh runtime/dev audit has zero findings; obsolete ECDSA ignore removed. [Evidence](security/V02-V03-JWT-REPAIR-2026-10-16.md). | Next auth repair; no patched jose release listed, and missing-claim acceptance was reproduced. Include production key/algorithm policy; no new audit exceptions. | M | Plan §2 / V02–V03 |
| V05a · P0/P1 | **Replace the frontend NGINX base — implemented locally, pending CI/review.** Maintained digest, non-root origin, TLS/proxy/static/document/header/upload-limit smoke passed; final image has zero high/critical findings. [Evidence](security/V05-IMAGES-2026-10-16.md). | Can run alongside auth work. Base-image findings require deployment/architecture assessment; candidate base scan alone is not runtime sign-off. | S | Plan §3 / V05 |
| V04 · P1 | **Update only the source-map-js lock resolution to 1.2.2 — implemented locally.** Full npm audit clean; install/lint/build/bundle passed. Current application-head CI: 581 browser and 13 visual checks passed; final combined gate/review remains required. [Evidence](security/V04-FRONTEND-LOCK-2026-10-16.md). | Independent small repair; six high package entries share one build-tool advisory. Do not take the whole frontend upgrade group or use audit-fix force. | S | Plan §4 / V04 |
| V05b · P1 | **0d81c0f passed seven required CI jobs and the ten-image gate.** [RustFS candidate](security/CI-STORAGE-REPLACEMENT-2026-10-19.md) replaces both unavailable images; all six baseline native storage groups passed with cleanup confirmed. Legacy-data refusal and production-start refusal remain. Previous 47-row snapshot is preserved, no waiver. | The subsequent export work needs its own exact-head CI. Lead review and production provider/cutover approval under Decisions §29 remain. Host TypeScript tooling exposure is separate. | M | Plan §3 / V05 |
| V06a · P1 | **Restore functioning dependency coverage in CI — implemented locally, control review pending; current application-head CI passed.** Full Python/npm graphs, resolved/locked base-head deltas and six vulnerable/fixed/error fixtures. [Coverage and evidence](security/V06-SUPPORTED-DEPENDENCY-REVIEW-2026-10-16.md); no hosted entitlement required. | Engineering is unblocked. Hosted feature availability/settings are tracked separately in [Decisions §28](DECISIONS-NEEDED.md); retain severity, transitive/dev scope and workflow/pin controls. | M | Plan §5 / V06 |
| V07a · P1 | **Prepare private history-PII triage — implemented locally.** 872 masked rows / 196 unresolved blobs across 19 fetched/local refs; private inventory and specific cleanup proposal prepared. [Evidence](security/V07-PRIVATE-TRIAGE-PREPARATION-2026-10-16.md). Named verification/denylist/history action remain Decisions §24; no automatic synthetic approval. | Engineering preparation is unblocked. Named verification, the owner-held denylist and any shared-history rewrite remain in [Decisions §24](DECISIONS-NEEDED.md). A clean current tree does not resolve history. | M | Plan §6 / V07 |

**Next:** current-head verification and review; V05b residual findings remain
blocked under Decisions §29, V06a control review under §28, and V07 cleanup under
§24. The report-driven E16/Function Studio/R014–R016 repairs and bounded pilot are
implemented below; complete current-source verification/review while preserving
these release blockers. Re-scan candidate versions before implementation; recorded clean scans
are dated evidence, not a guarantee about future releases or deployed images.

**Exit / merge gate:** the exact reviewed head must pass fresh dependency,
workflow, backend, PostgreSQL, browser, bundle and final-image checks. Test the
CI/local/Docker runtime matrix specified by the plan. Record explicit PII-history
triage and any evidence-based residual image findings. Keep main merge blocked
until required controls pass; no audit skips, blanket exclusions or budget rises.
External dependencies remain visible in the decision register, not marked done.

## Review repairs — after security verification

Source through R019 is merged via [PR #62](https://github.com/kristapsgoncoronoks-ship-it/SME-financial-workspace/pull/62)
into main `7608c1e`. R020/R021 remain the follow-up draft candidate. Merged source
does not mean release clearance: the evidence/review labels below retain their
security and external-acceptance limits.

Reconciled the two newly uploaded ECC/Function Studio reports against current source;
[evidence, source hashes and per-item prompts](reviews/RECONCILIATION-2026-10-16.md).
These rows track implemented repairs and pending verification/review, not completed release claims.
The reports’ E16/V labels do not replace the security V01–V07 IDs above.
[Combined application verification and remaining gates](reviews/FINAL-VERIFICATION-2026-10-16.md).

| Existing ID | Work / completion criterion | Priority / size |
|---|---|---|
| E16-03 | **Implemented locally, pending CI/review:** shared EUR ledger projection, provenance/incomplete states, guarded plan/final billing and preserved frozen/archive history. EUR190 and source download verified through real browser/API/PG; [evidence](plan/E16-PROJECT-CURRENCY-2026-10-16.md). | P1 / M |
| R014 / INV18 / E16-02 | **Implemented locally, pending CI/review:** complete-or-refused ZIP, filters before 500/501 bound, manifest/hashes, byte caps and refreshed authorization. Real browser/PDF/PG evidence: [repair record](plan/R014-COMPLETE-INVOICE-ZIP-2026-10-16.md). | P1 / M |
| FP-P01 / FS16-01/02 | **Implemented locally, pending CI/review:** immutable version-pinned execution, typed/bounded validation, revision conflicts and explicit legacy mismatch preflight. [Repair evidence](plan/FP-P01-PUBLISHED-RULES-2026-10-16.md). | P1 / M |
| R008-A-AU04 / FS16-03 | **Implemented locally, pending CI/review:** lossless raw fallback for string IDs/comma-list members/nested conditions; pending/readback and stale-edit preservation. [Evidence](plan/FP-P01-PUBLISHED-RULES-2026-10-16.md). | P1 / M |
| R008-A-AU06 / FS16-04 | **Implemented locally, pending CI/review:** named draft/published preview basis, shared suppression/cooldown/cap decisions, bounded proposals and zero action writes. [Evidence](plan/FS16-04-TRUTHFUL-PREVIEW-2026-10-16.md). | P1 / M |
| FP-P02 / FS16-05 | **Engineering implemented locally, pending CI/review:** durable version-pinned children/native mail jobs, truthful states and partial recovery. [Evidence/limits](plan/FP-P02-DURABLE-AUTOMATION-2026-10-16.md); historical/uncertain resend policy remains [decision 30](DECISIONS-NEEDED.md#30-historical-automation-and-uncertain-smtp-evidence-fs16-05). | P1 / L |
| R015 / INV20–21 / E16-04 | **Implemented locally, pending CI/review:** bounded multipart/file reads, offloaded fail-closed scan, durable upload/status lookup and per-file download recovery. PG race and real browser byte/hash [evidence](plan/R015-ATTACHMENT-COMMANDS-2026-10-16.md). | P2 / M |
| R016 / pilot finding | **Implemented locally, pending CI/review:** keyed legal credit issuance/status lookup and reviewed-balance conflicts; partial/full retries converge without duplicate notes; historical audit links require [decision 32](DECISIONS-NEEDED.md#32-historical-credit-audit-document-links-r016). [Evidence and prompt](plan/R016-CREDIT-COMMANDS-2026-10-16.md). | P1 legal-document gate / M |
| E16-05 | **Bounded engineering pilot passed, pending review/external acceptance:** real capture/approval/bank-match/CSV journey, isolated data restore plus new upload/worker check, enforced 2-CPU/1,920-MiB server ceilings. [Measured evidence and limits](plan/E16-05-CONTROLLED-PILOT-2026-10-16.md); no production sizing or accountant-acceptance claim. | P1 pilot gate / M |
| R017 / intake follow-up | **Implemented locally, pending CI/review:** cap+1 capture reads, explicit storage refusal/per-file retry, ordered same-name source recovery and isolated local staging files. [Evidence and prompt](plan/R017-CAPTURE-ADMISSION-2026-10-17.md). | P1 resource/recovery / M |
| R018 / intake transaction | **Implemented locally, pending CI/review:** per-file registry/capture/job/usage commit together; failure rolls back the current file and preserves earlier accepted batch files. Separate-connection PostgreSQL worker visibility and retry [evidence/prompt](plan/R018-ATOMIC-CAPTURE-ADMISSION-2026-10-17.md). | P1 consistency/recovery / M |
| R019 / intake quota | **Implemented locally, pending CI/review:** final per-workspace PostgreSQL admission lock and fresh plan/policy/usage check prevent concurrent last-slot oversubscription; rollback releases capacity and other workspaces progress. Single/batch refusal and native-worker [evidence/prompt](plan/R019-CAPTURE-QUOTA-CONCURRENCY-2026-10-17.md). | P1 concurrency / M |
| R020 / intake access | **Implemented locally, pending CI/review:** pinned per-file identity refresh after scan/lock waits and storage; committed revocations/switches refuse new capture/job/usage without releasing the quota lock or exposing duplicate details. Earlier accepted batch work and registry metadata survive; [evidence/prompt](plan/R020-CAPTURE-ACCESS-REFRESH-2026-10-17.md). | P1 authorization / M |
| R021 / capture downloads | **Implemented locally, pending CI/review:** original/page reads refresh access and recheck visibility after storage; source SHA-256/registry size and current page identity must verify before response. Private/no-store originals and explicit corruption refusal; [evidence/prompt](plan/R021-CAPTURE-DOWNLOAD-TRUTH-2026-10-17.md). | P1 privacy/integrity / M |

## Review implementation sequence

The user-supplied development review prioritized these repairs after the N1
supplier-identifier slice. They are implemented and tested locally; this is not
a shipped-release status.

- [R011: invoice commands and delivery truth](plan/R011-INVOICE-COMMANDS-2026-10-13.md).
- [R002: scoped generic accountant preview and download](plan/R002-ACCOUNTING-PREVIEW-2026-10-13.md); provider adapters remain blocked on reviewed mappings/templates.
- [R012: replay-safe direct cash and explained correction](plan/R012-CASH-COMMANDS-2026-10-13.md).
- [N4: bounded capture page previews](plan/N4-CAPTURE-PAGES-2026-10-14.md).
- [Review repair: reconciliation source-currency display and CSV retention](plan/RECON-CURRENCY-2026-10-14.md).
- [Remaining ECC security repairs](plan/ECC-SECURITY-2026-10-15.md): session tenancy, stale-tab refusal, calendar offboarding and browser-bound one-use OIDC; implemented locally, pending CI/review.
- [ECC account-cache isolation](plan/ECC-IDENTITY-CACHE-2026-10-15.md): fresh client ownership per identity transition and quarantine of late responses; follow-on backend repairs are linked above.
- [N1: captured versus calculated line tax/gross](plan/N1-LINE-AMOUNTS-2026-10-13.md), continuing the intake task; bank-account controls remain separate.

## Now — pick these up next (small, high-value, unblocked)

| # | Item | Why | Size | Source |
|---|------|-----|------|--------|
| N1 | **Capture the remaining invoice fields**: ~~supplier registration no. + VAT~~ implemented locally (2026-10-13; BT-30/BT-31 document snapshots, structured/export capture, provenance, guarded review; [evidence](DEVELOPMENT-REVIEW-2026-10-13.md), pending CI/review); **supplier IBAN document snapshot** implemented locally (2026-10-16; qualified CSV/JSON and UBL/CII payee capture, guarded review, masked detail/audits, no learned bank suggestions or vendor/payment updates; [evidence](plan/N1-SUPPLIER-IBAN-2026-10-16.md), pending CI/review); ~~per-line **tax amount** + **line gross**~~ implemented locally (captured/calculated snapshots, guarded review and provenance; [evidence](plan/N1-LINE-AMOUNTS-2026-10-13.md), pending CI/review). <br>~~PO number~~ ✅ **SHIPPED** (2026-09-13) — `invoices.po_reference`, EN-16931 **BT-13**, read from UBL `cac:OrderReference/cbc:ID` and CII `BuyerOrderReferencedDocument/IssuerAssignedID`, plus the CSV/JSON spellings in `parser.PO_REFERENCE_KEYS`; provenance row, list + detail shapes, editable and clearable in review (`tests/test_n1_po_reference.py`). <br>⚠ **IBAN sub-item carries a constraint**: PRD forbids logging secrets/IBANs and requires bank data masked in analytics — that field is not a plain additive column. | The intake slice captures only a subset; PRD §5A lists these. Additive model + parser + schema + provenance. | M | Intake slice (commit `ab52df4`) deferral |
| N4 | **Page thumbnails for captures.** Implemented locally (2026-10-14; bounded isolated rendering, tenant-scoped JPEG storage/serving, page navigation and original fallback; [evidence](plan/N4-CAPTURE-PAGES-2026-10-14.md), pending CI/review). | Reviewers need to see the source page next to the extracted draft; today there's nothing to show. | M | Intake slice deferral |

## Next — valuable, a bit larger

| # | Item | Why | Size | Source |
|---|------|-----|------|--------|
| X1 | ~~**Multi-file / batch upload.**~~ ✅ **SHIPPED** (WO-X) — `POST /invoices/upload/batch` with a visible per-request file cap and per-file reasons; FE multi-select in `e2e/upload-batch.spec.ts`. | — | — | verified 2026-09-13 |
| X2 | ~~**Upload progress.**~~ ✅ **SHIPPED** (WO-X) — `services/capture_progress.py` stages + percent on the capture run, reset per attempt so a re-parse reports phases again; `e2e/capture-progress.spec.ts`. | — | — | verified 2026-09-13 |
| X3 | ~~**SSO client secret → keyvault.**~~ ✅ **SHIPPED** — `core/keyvault.py` (AES-256-GCM, AAD-bound, `kv1.` format, ADR-0016); sealed on write in `sso_config.py`, unsealed at use in `oidc.py`; `models/sso.py:17` now documents sealed-at-rest and the TODO marker is gone. | — | — | verified 2026-08-27 |
| X4 | ~~**Visual-regression in CI.**~~ ✅ **SHIPPED** (WO-Y) — VR runs as a real CI gate in the containerised browser, with a `vr-baselines` workflow to regenerate baselines inside the same container. | — | — | verified 2026-09-13 |

## Later — larger or lower-priority (unblocked)

| # | Item | Why | Size | Source |
|---|------|-----|------|--------|
| L1 | **External extraction provider — moved to [Decisions §31](DECISIONS-NEEDED.md#31-external-extraction-provider-l1).** Existing deterministic/OCR provider and confidence plumbing remain available. | Selecting a paid processor, data handling terms, region and credentials requires an owner decision before implementation; this is not unblocked engineering. | L / blocked | Provider interface (commit `ab52df4`) |
| L2 | **Header multi-rate VAT breakdown on received invoices.** Implemented locally (2026-10-15): saved-line rate groups, exact line-rounding sums, missing-history and header-mismatch guidance in detail/review; [evidence](plan/L2-RECEIVED-VAT-2026-10-15.md), pending CI/review. | Multi-rate suppliers; PRD §5D. | M | Intake gap analysis |

> **Blocked on you** (not in this file): enterprise SSO/SCIM/SAML live-testing, billing
> go-live, DATEV/SAF-T exporters, data residency, production KEK, public API GA, and
> SOC 2 / ISO — all tracked with "what I need from you" in
> [`DECISIONS-NEEDED.md`](DECISIONS-NEEDED.md).

---

## Recently shipped (context — not backlog)

- **The supplier's purchase-order reference survives arrival** (N1, part): `IssuedInvoice`
  had carried EN-16931 **BT-13** since the issuing module shipped, but the received
  `Invoice` had no order reference at all — `einvoice.py` genuinely parses inbound UBL
  and CII and read straight past the element, so a supplier could send the field, in the
  standard, in the place the standard puts it, and the number was discarded. Nothing
  downstream could match a supplier invoice to a purchase order. Now captured from UBL
  `cac:OrderReference/cbc:ID` (read from that element ONLY — `cbc:ID` is everywhere in a
  UBL document and a looser search files the invoice number as its own PO), from CII
  `BuyerOrderReferencedDocument/IssuerAssignedID`, and from the CSV/JSON spellings in
  `parser.PO_REFERENCE_KEYS`; absent stays absent (`missing` provenance, never
  `defaulted` — there is no sensible default for someone else's order number). Same
  column width as the issued side (60) so a reference round-trips through either half of
  the product, trimmed at capture rather than failing the intake. On the list shape as
  well as the detail, because an approver matches while scanning the queue, and
  clearable in PATCH via `model_fields_set` so a mis-captured number is not permanent.
  Migration `d3e4f5a6b7c8` (additive, nullable, symmetric downgrade).
  18 tests (`tests/test_n1_po_reference.py`) + 2 e2e (`e2e/invoice-po-reference.spec.ts`).
- **Supplier-invoice intake slice** (`ab52df4`): pluggable extraction providers, honest
  per-field provenance (confidence / original / normalized / reviewed / low-confidence),
  JPEG/PNG OCR intake, same- vs cross-supplier duplicate detection, human-review queue +
  manual re-extract, corrupt-file hardening. 12 scenario tests.
- **One upload size cap** (N3, WO-94): `filesec.max_bytes(purpose=None)` is the single
  definition of the limit and `too_large_message()` renders the sentence from the same
  number, so no caller can quote a figure it does not enforce. Seven hard-coded caps in
  six route modules are gone — three duplicated `settings.max_upload_mb` (so raising it
  did nothing on the primary capture endpoint), two are now clamped purpose policy
  (`PURPOSE_MB`: receipt 5 MB, logo 2 MB), and two 25 MB `_ATTACH_MAX` constants were
  DEAD, since `reject_active_content` already capped those paths at 15 MB. An AST scan
  over the whole `app/` package refuses a second cap, with seeded-violation self-tests
  (`tests/test_wo94_upload_cap.py`).
- **Integrity-cover the original uploads** (N2): `verify_documents` now sweeps the
  `uploads` prefix over `extraction_runs.source_sha256`, so the stored original
  supplier-invoice bytes (the legal record) are re-hashed alongside receipts / logos /
  email-attachments — silent loss or corruption of an original is now a finding.
- **Frontend design system + shell + gallery** (`256da1a`), with Playwright e2e smoke
  gated in CI (`419edfb`).
- **Identity / authz**: 8-role matrix + guard migration (`3f09d5f`), multi-org membership,
  sessions, cross-tenant isolation proof.
- **CI health**: Postgres boolean-default migration fix (`0708c16`), Node-24 actions bump.
