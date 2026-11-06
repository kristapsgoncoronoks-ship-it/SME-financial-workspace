# Transport statement parser availability — 6 November 2026

Transport estimates and statement uploads keep the request loop available while
their existing fuel-card parser runs. Another parser request receives an explicit
retryable 503, while the admitted request still completes its original workflow
and 200 response. Statement uploads distinguish retained original bytes from
transactions that this busy attempt did not register.

This is partial ARCH-006 progress. Frozen component `404b1c3` is composed with
Facts parent `6ed908d24509b6ba348a96015e50436ab0ccf7a6` at local merge
`c6d78d5540847a1d95d3834e3f4c0f2496543ef9`. All four app files and the test
module retain their qualified hashes. The parent remains revision 165; no
schema, dependency, frontend or transaction boundary is introduced by this slice.
The actual timing comparison remains its dated Python 3.12/PostgreSQL 164 proof,
not a new performance result on the Facts composition.

## Behavior

Transport estimates and statement ingestion now await the existing pure
filename/immutable-bytes parser in the application's existing thread pool.
The event loop can serve other requests while that parser runs. The request
still returns its original 200 response only after its existing workflow is
complete; it does not become a background job or return 202.

One process-local slot admits one of these asynchronous parses. Another caller
receives an immediate 503 with code `statement_reading_busy`, a string `detail`
and `retryable: true`. There is no parser admission queue, automatic retry,
Retry-After value, new executor or global timeout. The slot covers parsing,
not the entire subsequent workflow or all application CPU/memory capacity.

An estimate does not store the file or write domain facts. Statement upload
retains its existing vault-before-parse ordering: a busy attempt has already
stored the exact content-addressed original. Its response adds
`original_stored: true` and explains that this attempt did not register
transactions and that the user can retry the same file explicitly. It does
not create a catalog entry, refusal finding, import or audit event, advertise a
retrieval URL, or delete the retained bytes. Successful explicit retry completes
the existing registration/review/audit workflow and preserves natural-key
idempotency and the existing response count.

Only the pure parser receives thread arguments. Session access, FX resolution,
capture review, domain writes, baseline learning, audit and commit remain on
the request loop at their original positions. All seven parser networks and
the synchronous registry API retain their money, warning and refusal behavior.

## Cancellation ownership

A thread-safe `concurrent.futures.Future` distinguishes pending submission from
physical parser execution. The worker atomically claims it with
`set_running_or_notify_cancel`; its single completion callback releases the
slot. Pending cancellation releases admission once, and a late canceled
callable cannot parse or release a successor's slot.

Cancellation after parsing starts waits for physical completion even if the
inner AnyIO/asyncio Task has already been canceled. Repeated cancellation does
not release admission early. The wrapper consumes completion and inner-task
exceptions before propagating cancellation. Failed task submission closes the
unused coroutine and releases only pending admission. Public busy/cancellation
handling adds no raw filename, bank source or worker exception to the busy
message.

Independent read-only review found no blocking issue at the five observed
source hashes. It rechecked the old parser class/function ASTs and both caller
ASTs after normalizing only the awaited adapter call. Two retained synchronous
re-extraction helpers have no production callers in `backend/app`; source
inspection did not establish a request-path bypass. They are not presented as
newly offloaded HTTP paths.

## Measured local evidence

The owner used the same 4,889,023-byte, 50,000-row synthetic Eurowag CSV through
actual TCP HTTP on a single asyncio Uvicorn worker and ordinary, non-superuser,
non-BYPASSRLS PostgreSQL 164. Complete JSON responses match baseline exactly.
The observer called the original parser without fake CPU, waits, replacement
responses, provider or decoder work; its logging/hash costs were not subtracted.

| Observation, two large requests | Baseline | Candidate |
| --- | --- | --- |
| First queued loop callback | 0.4450 / 0.5443 s | 0.00336 / 0.00330 s |
| Maximum competing `/health` delay | 0.4418 / 0.5315 s | 0.06860 / 0.06062 s |
| Total estimate request | 0.4883 / 0.5780 s | 0.5244 / 0.4952 s |
| Maximum API-process RSS high water | 253,760 KiB | 256,820 KiB |

The evidence demonstrates better availability during this parser stage, not
faster total requests or a fixed memory/capacity bound. Postparse EUR aggregation
still produced about 61–69 ms of observed competing-health delay. Host
Python 3.12 observations do not qualify the production image, cgroup, fleet,
VPS size, concurrent capacity or an SLA.

Owner success, anonymous 401, employee 403 and a separate module-disabled
organization's 403 were observed. All 115 table entries per organization across
three declared synthetic identities stayed unchanged for the measured estimate
journeys. Owned fixture organizations and API processes were cleaned up after
raw snapshots. No production data or external provider was used.

## Component qualification and limits

The source cohort passes 312 portable cases: 301 inherited cases and 11 focused
controls. The final 11-case focused run follows a test-import-order-only
correction; it is a subset, not 323 distinct cases. These cover all seven
networks, ingestion/FX/review, exact storage outcomes, public busy responses,
explicit retry, direct inner cancellation and delayed canceled submission.
Mypy checks four app files; Ruff/format checks five files. No independent test
rerun or performance measurement is claimed by this read-only review.

Final composition checks are separated below; fresh delivered-source hosted CI
remains pending. The source/response/body and database snapshots apply
to their recorded source, rather than silently qualifying later Facts/schema
or other component changes.

ARCH-006 remains partial. Residual EUR aggregation, postparse review/resolution
work and source-confirmed project-template, claim-workbook, overcharge and
excise renderers need bounded investigation and qualification. Existing OCR,
PDF and ZIP worker paths are not newly classified as inline defects.

DB-021 remains unqualified: no legitimate archive/integrity/growth-query timeout
or rollback study occurred, and `/health` has no database I/O. No
`command_timeout` value is inferred. ARCH-010 remains unqualified: the measured
estimate endpoint has no audit lock, and no advisory-lock scope, hash-chain
order or transaction atomicity was changed. No financial rule, settlement,
job, dependency, retention, provider setting, production migration, main merge
or deployment is delivered here.

## Facts-parent composition

The composition collects **7,301 backend cases**; collection is not a full-suite
execution. Full Ruff/format checks **1,010 files** and Mypy checks **452 app
files**, all passing. Frontend lint/build and the unchanged 460/135 kB bundle
gate pass at **442.8 kB raw / 129.9 kB gzip** across four assets. The host uses
Node 24.19.0/npm 11.9.0 with the qualified same-lock dependency directory;
no package install, global tool change or production image qualification is
inferred. The complete frontend tree is identical to the Facts parent.

Regenerated OpenAPI retains **433 paths** and is byte-identical to Facts;
the **122-table** ERD is also parent-identical. There are **165 revisions**,
70 model modules, 136 service modules, 50 route modules, 69 pages and 10 CI jobs.
All 51 docs/generated/CI-discovery controls pass in 15.52s. The 11 focused
adapter cancellation/admission/busy-retry controls also pass in 3.30s on this
parent. They overlap the component proof and are not added to its 312 as a
larger unique total.
The 312 component tests and PostgreSQL 164 TCP comparison are reused only at
their matching five source hashes. Parent Facts native/browser receipts retain
their own scope; no new native or browser execution is claimed here.
Fresh delivered-source hosted CI remains pending.

Only the existing ARCH-006 ledger row changes progress/status. All 215 IDs,
order, titles, source references, original inventory classifications and
unrelated complete rows are preserved from Facts. No new finding is appended;
DB-021 and ARCH-010 remain unchanged and unqualified by this work. The 197
original plus 18 findings denominator is not a completion percentage.

## Evidence

- [Owner component receipt](/workspace/artifacts/backlog-inline-availability-2026-11-06/TEST-RECEIPT.json) and [qualified component scope](/workspace/artifacts/backlog-inline-availability-2026-11-06/QUALIFICATION.md).
- [Actual local comparison](/workspace/artifacts/backlog-inline-availability-2026-11-06/comparison.json), with matching baseline/candidate response and event files.
- [Independent source review](/workspace/artifacts/backlog-inline-availability-2026-11-06/independent-security-review/REVIEW.json).
- [Facts composition](/workspace/artifacts/backlog-inline-availability-2026-11-06/facts-composition/POST-MERGE.json) and [generated preservation](/workspace/artifacts/backlog-inline-availability-2026-11-06/facts-composition/GENERATED-PRESERVATION.json).
- [Parent Facts receipt](/workspace/artifacts/backlog-ocr-facts-inspection-2026-11-06/delivery/FINAL-RECEIPT.json).

This local delivery does not push a branch, open or merge a PR, deploy, change
provider settings or establish a complete ARCH-006/DB-021/ARCH-010 outcome.
