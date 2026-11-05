# Backlog completion — 5 November 2026

The user's instruction is to continue through the actionable backlog. The
[197-row source inventory](BACKLOG-COMPLETION-2026-11-05.json) records each named
item, its original disposition, dependencies and current development state.
It includes historical carry-forwards; it is not 197 equally sized features.
Baseline: `47baca2bc45d7772f54f3bb51ee832408bd4459d` (PR #104). Main remains
unchanged. Work is on `dev/backlog-completion-20261105`.

Nine subsequently identified entries are appended separately, preserving the
197-row baseline inventory. Current schema: 120 tables, 157 revisions, single
head `b2e4f6a8c0d3`; current collection is 6,099 backend cases.

## Current implementation

| Area | Implemented behavior and verification boundary |
| --- | --- |
| Reimbursement cancellation, EX19 | Actor/workspace/key receipt; exact released report/version snapshot; legacy DELETE adapter; strict audit rollback; permission-aware GET recovery. 36 focused backend, 55 native PostgreSQL, 96 browser checks and one connected lost-response browser/PostgreSQL journey pass locally. No new external events or migration. |
| Inbound email | Same-message admission serialized across generic and Mailgun deliveries; extraction publication fenced against confirm/discard; invoice/link/audit commit together. 32 native PostgreSQL checks and 122 portable compatibility/boundary checks pass locally. Original download read bounds and post-I/O access checks are being completed. |
| Jobs and startup | Reclaim updates only the observed stale lease; concurrent FX initialization keeps the serial dated/provenance-bearing set. Eight native races, 57 portable regressions and four actual Uvicorn workers pass. Historical policy rollback types and initial-schema enum cleanup pass SQLite and disposable PostgreSQL roundtrips. No live downgrade. |
| UI | Native project file buttons and table labels; settled/cancelled invoice search; recoverable organization switching; bounded switcher; unobscured planning focus; optional dashboard analytics. Initial combined 136 browser checks pass. Supplier/currency reflow, mobile targets and semantic states are in progress. No visual baseline regeneration. |
| npm build tool | Official integrity-pinned npm 12.2.2 supplies fixed ip-address and retained fixed undici. Twenty-nine guard fixtures and isolated real network/proxy checks pass. Actual Node 22 image qualification and full inventories remain required; glob/minimatch findings remain visible. |
| CI and test isolation | Run/attempt-owned Trivy cache plus explicit database identity receipt; new native tests explicitly wired into CI. Real heap-freeze proof runs in a child process. Typed calendar/notification responses preserve current wire output. Fifty planning/GC/CI-discovery checks pass locally. Actual cache failure controls and delivered-source CI remain pending. |
| Erasure reporting | Retained stored export snapshots are disclosed conservatively with existing purge eligibility and unknown deadlines. Legal hold suspension is explicit; no new deletion policy or lifecycle change. Eleven privacy tests pass locally. |
| AP expression reuse | Cash-position and AP aging use an intentional public SQL expression seam with unchanged money/query semantics. Eight existing regression tests pass locally. |

## Continuing work

Current local evidence now includes the completed planning receipt (20 API,
36 compatibility, 43 tenant, ten native concurrency/RLS, four migration,
69 mocked browser and one connected lost-response journey), the actual
30-photo browser/API/PostgreSQL journey, all seven actual S3 groups, and
the bounded runtime-only image graph. Runtime removal is five test distributions
/ 6,141,952 logical bytes; three preexisting unpinned transitives also refreshed,
so total image-size changes are not attributed solely to removal. No RAM or
latency benefit is claimed. PDF scratch timeout cleanup passes ten controls.

The expense review-cycle fix passes 112 plus 42 portable controls and 21 native
cases. The AP exported-cancellation guard passes 31 portable, four native and
14 browser cases. UI's 273-case union retains one unexplained EX17 setup failure
(272 passes); subsequent confirmations do not establish its cause. First-failure
capture is strengthened without changing retries/timeouts or assertions.

An interrupted pre-finance serial run recorded 2,787 passes, 329 skips and ten
failures. These were README count drift, eight merge-point tests assuming the
old head, and two newly added PG files missing from the CI hand-list. Counts
and wiring are corrected; merge checks now explicitly reach the merge, retain
all unrelated schema/RLS comparisons and restore the additive head. This partial
run is not final source qualification. Delivered-source CI remains required.

The finance/lease follow-up wave now implements all four reproduced gaps below,
with scoped local/native/connected evidence in the
[follow-up report](FINANCE-LEASE-FOLLOWUPS-2026-11-05.md): fresh expense authority,
exclusive AP selection, conserved AP settlement and per-claim job-generation
fencing. It also repairs stale manual retry, finishes the delivery/claim-pack
verified reads, adds complete issued-invoice pagination and read-only rebate
preview. Fresh hosted qualification is required.

PR #105's latest functional and image jobs pass, but one mobile supplier-invoice
visual check fails. Its expected/actual images and original trace are preserved;
no baseline is regenerated. The image resource investigation reproduced a valid
80Mpx upload exhausting a 768MiB process. A separate isolated OCR wave is now
implementing supervised decoding with visible derivative warnings; this is not
production-capacity or customer-extraction acceptance.

Finish the active source and connected tests, then qualify an integrated candidate
using the existing lint, types, migration, PostgreSQL, browser, image, storage and
security gates. Newly delivered source needs all thirteen required hosted CI
contexts; an earlier green revision cannot qualify it. Retain failures and
advisories, including alternate ratings and unscored rows. Continue remaining
ready items from the inventory after each reviewable candidate.

## Decisions and external evidence

RB04/RB05/EX21 remain dependent on [owner decisions §33](../DECISIONS-NEEDED.md).
The pending questions concern full versus partial reimbursement export and
bank-detail artifact retention/legal hold/download roles. No answer is assumed.
Other policy, credentials, provider, legal, accountant, hardware/corpus and human
review dependencies remain explicit in the inventory and Decisions register.
An engineering check does not satisfy those dependencies or authorize deployment.

Per-task logs, failed controls, native receipts and source hashes are retained in
`/workspace/artifacts/backlog-completion-2026-11-05/`; earlier qualified evidence
is preserved unchanged. Final delivered-source receipts will be added here when
available.

The final runtime image `addeda248664` also passes 191 offline smoke cases with five
hash-locked test tools, 59 unchanged runtime distributions and 60 active dependency
requirements. Native OCR/font/PDF/zlib/timezone/ECB XML probes pass. The extra
local image-to-PostgreSQL smoke could not start because the local Docker daemon
ran out of disk; no database test ran in that attempt. Its failed context is
retained. Hosted native/image/visual qualification remains mandatory.
