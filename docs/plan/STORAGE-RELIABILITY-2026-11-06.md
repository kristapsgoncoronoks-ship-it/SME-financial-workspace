# Integrity coverage, download recovery and ledger reads — 6 November 2026

Settings now explains which current document references its integrity check
examines. An empty result means there were no supported references to check.
When an internal project original is missing or its adapter raises a plain
read error, Download shows a safe availability message and remains available
for an explicit retry. Ledger integrity reports retain their signed financial
comparisons while replacing per-parent SUM queries with two grouped reads.

These are three separate, bounded changes. They do not expand document
verification, repair source files or financial history, or establish that an
entire workspace is healthy. Fresh delivered-source hosted CI remains pending.

## Components and composition

The reviewed parent is revision 166 at
`cd226a126a4e981f3ec5fa9c978e1a3d38dacdf5`. It includes Explore and the four
residual tenant-parent guards. This delivery adds no model, migration, table,
permission, dependency or default.

| Component | Commit | Preserved scope |
| --- | --- | --- |
| Project original availability | `2312255199011593d0c347382b0d6381ee06abf4` | One route exception boundary and its tests |
| Integrity coverage copy | `e77771dd1c5e2a5bfc7004940f3d5a008e556fd7` | Settings integrity panel and focused browser spec |
| Grouped ledger reads | `4b7f0cef2c037566081bae27896c826c6cb5b6bd` | Two grouped maps/two lookups, tests and frozen legacy function |

All eight component files are byte-identical after composition. Browser
registration `bae4d4e` lists the new spec once; the parent merge preserves all
Explore registrations. Separate native registration `96fc492` lists
`tests/test_integrity_ledger_groups_pg.py` once in the ordinary PostgreSQL
cohort. No portable test was moved into an admin-enabled migration lane.

## Document coverage is explicit

The checker covers current expense receipt pointers, company logos, retained
email attachments and distinct original supplier-invoice upload hashes. Other
document categories and replaced-file version history are outside this check.
The API, checker, storage and authorization behavior remain unchanged.

A root-owned actual ASGI/SQLite characterization at Facts `6ed908d` uploaded
a normal project contract, then removed its object from isolated MemoryStorage.
The verifier reported `checked=0, ok=0, healthy=true, issues=[]` both before and
after; the project row remained visible. This proves the old coverage claim
was too broad. It is not a test requiring that omission to persist, a native
PostgreSQL/provider result, or evidence about production files.

Settings now renders a neutral empty result and limits positive and problem
counts to checked supported references. A new or reused 202 response directs
the user to Background jobs; it does not claim the job is running. Existing
issue details, read-only access, pending behavior and explicit retry remain.

The owner retained nine initial browser failures plus one passing control.
Final source-bound evidence is 22 distinct cases: ten new and twelve incumbent
Settings cases, with no retries. These use controlled API responses on the
actual page. Two screenshot confirmations are not extra acceptance cases.
DOM containment at 390 px with a 32 px root size passes; narrow screenshot
limitations remain recorded, without a full mobile visual acceptance claim.
[UI receipt](/workspace/artifacts/backlog-integrity-coverage-2026-11-06/UI-RECEIPT.json).

## Project original availability keeps error distinctions

Only exact plain `StorageError` escaping the internal project original
download is mapped to the existing response:

```json
{"detail":"Stored document could not be read. Check storage and retry.","code":"storage_read_unavailable"}
```

The status is 503 with `Cache-Control: private, no-store`. The existing
frontend reconstructs JSON blob errors, displays the message and allows
another explicit GET. No UI change or automatic retry was needed.

Interrupted `StorageReadError` already has a global safe handler; that handler
is unchanged. Digest, truncation and oversize retain
`original_integrity_unverified`. Missing metadata, anonymous/foreign access and
fresh post-read permission refusals retain their existing 404/401/403
contracts. Write errors, unknown storage subclasses and programming failures
are not disguised as retryable reads. The shared verified-original loader,
portal, photo and worker behavior remain unchanged.

Two actual baseline requests failed with HTTP 500 for a missing object and a
plain adapter read error; the interrupted-read control already passed. The
generic 500 body was redacted. The original exception escaped into server
logs, rather than exposing a raw object key or path in the HTTP response.

Source-bound qualification passes 32 ASGI/SQLite cases in 27.86 s: fourteen
new route tests and eighteen unchanged read-bound cases. It proves safe
refusal, restored retry with exact original bytes, access/integrity/error
distinctions and unchanged scoped source, registry, audit and financial rows.
Eighteen native variants were deselected and are not newly qualified. This
narrow route fix does not certify all download boundaries or restore files.
[Component receipt](/workspace/artifacts/project-document-availability-2026-11-06/FINAL-RECEIPT.json).

## Ledger statement count, semantics and measurement

The service keeps its original cache SELECTs, iteration and issue order,
messages, tenant/source/bin predicates, signed Decimal SUM/q2 behavior and
missing-sum `0.00` formatting. Receipt-less payments affect AR without becoming
allocations; negative entries and reversal pairs still offset. The AP grouped
block and caller-owned transaction remain intact. No source locks, explicit
flush/commit/rollback, isolation or cash-validation policy is added.

Populated reports use six business SELECTs plus one tenant setup statement,
or seven driver commands. Empty reports use four plus one, or five. Only the
number of statements is bounded. Returned/grouped rows, total work and map
memory remain proportional to the population. READ COMMITTED is still a
best-effort observation: cache and aggregate reads may see different committed
states. Grouping changes observation timing without promising a whole-report
consistent snapshot.

The old function produced 19 and 132 business reads in two failure-first
controls against the six-read bound, while report equality already held.
Final component proof is 33 distinct portable cases: six new and 27 incumbent.
A strengthened caller-transaction case was rerun once; it remains part of the
same 33. Four historical ordinary-role native cases at revision 165 retain
their date and source scope. The frozen legacy function and source projection
pin the exact comparison; they are not a full old-head application replay.
[Report and limits](/workspace/artifacts/integrity-ledger-groups-2026-11-06/REPORT.md).

Eight new service calls reused the retained 400/1600 fixtures: one warmup and
three measured calls per scale. Reports remain 832/832 and 3328/3328 with no
issues; all 116 scoped table hashes match. Driver commands change from
437/1733 to seven. New local wall medians are 15.55/49.88 ms, compared with
historical 346.95/1179.32 ms observations. These shared-host samples do not
isolate contention or prove a causal speedup, p95, SLA, production maximum or
fleet/hardware capacity. Driver wall and process CPU overlap and are not added.
No timeout, index, storage, settlement or retention rule follows.

## Fresh composed checks

An independently created owned database, `backlog_reliability166`, was empty
before migration. Application role `backlog_app` has superuser, bypass-RLS and
create-database false. Normal application-role upgrade reaches actual head
`e2f4a6b8d0c3`; Alembic check reports no drift. All four grouped-ledger native
cases pass in 4.48 s with strict source-current head, ENABLE/FORCE RLS, report
equality, unchanged hashes, caller-transaction identity and query counts.
This is a new 166 execution, not four additional distinct cases beyond the
historical component cohort.

Full Ruff/format checks 1,022 files; Mypy checks 452 application files. Full
backend collection finds 7,577 cases; collection is not a full-suite pass.
The tree retains 122 tables, 166 revisions, 70 model modules, 136 services,
50 route modules, 69 pages and ten CI jobs. Regenerated OpenAPI remains 433
paths and the ERD is parent-identical. Registered frontend lint/build pass;
the first-load bundle is 442.9 kB raw/129.9 kB gzip across four assets, within
the unchanged 460/135 kB budgets. The initial bundle command used a nonexistent
npm alias; the existing direct script then passed, with the command error
retained. It was not an application/build failure.

All 51 final docs/generated/CI-discovery controls pass in 14.49 s, with zero
failures, errors or skips. Existing 22 browser, 32 route and 33 portable proofs
are reused against exact source hashes, not rerun or summed into one release
total. No new native download, provider or production result is claimed.

## Six existing rows retain partial, dated measurements

These observations bind Facts `6ed908d`, revision 165 and local ordinary-role
PostgreSQL 16.15. They are not new revision-166 runtime benchmarks. Current
pool 10+10, checkout 30 s, recycle 1800 s and pre-ping defaults remain; no
asyncpg command timeout is set. Local statement/lock/idle-transaction limits
were zero; deployed settings are unknown. Export/worker deadlines are separate.

| Existing ID | Partial evidence | Next acceptance |
| --- | --- | --- |
| ARCH-010 | Same-tenant waits about 277/261 ms include an injected 250 ms audit-lock hold; rollback and chain checks pass | Measure legitimate append-to-commit lifetime before moving preparation; retain atomic audit/source writes |
| PERF-015 | A labeled 1+0 fixture retains the 30 s timeout; queued/running cancellation releases ownership and subsequent SELECT succeeds | Measure intended deployment checkout/hold distributions and HTTP/UI recovery before choosing deadlines |
| QA-008 | Twelve fresh commands, twelve replays and two refusals retain exact effects/source hashes, with at most six in flight | Repeat representative workloads, examine variance and agree stable budgets before any broader gate |
| DB-021 | Seven services at 400/1600 scale yield 56 successful samples with statement/service/stream/lock spans | Define supported data/history/size/deployment envelope and qualify cancellation, rollback, connection-loss and uncertain-commit recovery |
| DB-015 | Twenty-four template plans plus ten overlapping literal plans establish no index candidate | Identify a measured query/selectivity problem and compare plans, service/write cost and rollback before DDL |
| CAP-STORAGE | 690 typed edges map to 48 purpose/tenant/digest keys, including 40 historical edges, without storage I/O or deletion | Agree reference/unknown-coverage/restore semantics; compare only an authorized supplied manifest read-only before separate GC policy |

DB-021 and DB-015 share one study, not independent sample populations. The
initial adapter error and resumed samples remain recorded; they are not a
product failure or a 57th success. Process memory samples are not combined
into a whole-run peak. Reference manifests prove neither byte existence nor
deletability; a synthetic unseen candidate is not an actual orphan. The
12-row readiness appendix is triage, not twelve findings or completed work.
[Sealed measurement handoff](/workspace/artifacts/backlog-reliability-measurement-handoff-2026-11-06/PLAN-DRAFT.md).

## Future architecture appendix: calendar attempt/session actor

A separate reviewed proposal at Facts `6ed908d`/schema 165 records six
rolled-back native diagnostics. Current SQL permits an attempt actor/session
owner mismatch and a hidden-child cascade when the wrong owner's session is
deleted. Normal connect and both callback phases already enforce actor,
session, organization and freshness; no normal API bypass or production
exploit is demonstrated.

An actor-bound `(user_id, session_id)` pair may be viable independently of
mutable selected `Session.org_id`. Connection owner/provider/generation,
manual actor reassignment, history and account lifecycle remain separate
review questions. All callback checks must stay. This appendix adds no finding
ID, changes no DB-010 row/status and authorizes no revision-167 implementation.
[Reviewable proposal](/workspace/artifacts/calendar-session-actor-design-review-2026-11-06/PROPOSAL.md).

## Ledger preservation and delivery limits

All 216 parent identities/order/titles/sources/statuses/dependencies and
inventory statuses remain. DB-010's complete row and parent qualification
history/provenance are preserved. Six existing rows gain only dated
qualification and next-text additions; the other 210 complete raw row blocks
remain exact. Three distinct findings append as 217–219: 197 original entries
plus 22 differently sized findings, without a completion percentage.

The original baseline/date/scope/limits and all earlier qualification text
remain. Historical data/source recovery, broader verification, reference/GC
policy, legitimate capacity/deadline work and unrelated backlog stay open.
Fresh new-head hosted CI and root review precede publication. No production
migration, provider operation, source repair, main/PR merge or deployment is
claimed. Revert these application/copy changes and registrations together if
needed; this delivery has no schema/data downgrade or automatic repair step.
