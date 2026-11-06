# Project document generation: bounded rendering — 6 November 2026

Generate keeps its existing document and financial behavior while the pure text
substitution and PDF stage runs off the event loop. A busy process returns HTTP
503 with `project_document_render_busy` before this attempt attaches a document,
writes storage or records audit. Its message says this attempt saved no document
and asks the user to wait and try again. There is no automatic retry, durable
command key or promise about an earlier attempt whose response was lost.

The component is `c6557b11a8f139237df81135706988a0d2752ad4`, based on frozen
PR121 `9b8e4647adac233ee81bc74505c3c2905d9f294b`, composed locally at
`2b4878fe393d3618488678722859955061ec49ef`. Only
`backend/app/services/doc_templates.py` and its focused test module change.
The accepted source projection preserves the existing context, templates,
permissions, token rules, PDF renderer, filename, attachment, storage, audit and
commit implementation. No frontend, route, dependency, schema or CI gate changes.

## Admission and completion

One nonblocking slot per process follows the physical concurrent Future through
completion. The worker receives only detached title, body and a copied string
context in a fresh empty execution Context. ORM, AsyncSession, identity, storage,
audit and DML remain on the request. Declared web workers each have their own slot;
this is not a global capacity limit.

Caller cancellation propagates without attachment, while admitted physical work
finishes naturally and retains the slot. Repeated cancellation, loop closure,
late exceptions and submission/startup failures have focused controls. Physical
completion consumes late exceptions and releases admission; submission failures
retire and drain the affected executor rather than retaining detached inputs.
Explicitly cancelling a server request Task does not prove that a real browser
disconnect automatically cancels it. No thread hard kill or CPU deadline is added.

No new text, time, output, RAM or global policy limit is introduced. Stored bodies
and buffered output retain their existing size behavior; a hung render can retain
its slot. Other inline paths, transport postparse work, DB-021 and ARCH-010 remain
open. ARCH-006 retains its partial status and prior qualifications.

## Source-bound evidence

The owner cohort passed **80 cases**: 15 new and 65 incumbent, with **18 existing
PostgreSQL-gated skips**. Those skips are not native qualification. The final test
adds one strengthened invocation with one overlapping pass; this is not 81 unique
cases. The original cohort test SHA `e391b96d…` and its exact one-call projection
to final SHA `231e4b53…` remain in the receipts. Tests cover real off-loop rendering,
deterministic text/byte parity under test-only fixed PDF metadata, cancellation,
thread startup/submission failures, detached inputs and real ASGI/SQLite busy,
cancelled and successful attachment paths. Original inline-source failure and a
naive offload failure are preserved separately.

The before/after TCP experiment uses the same synthetic 640-paragraph body
(215,038 body and 215,678 rendered UTF-8 bytes), real ReportLab rendering and
asynchronous I/O stubs. Each side has one warm call and three sampled calls.
Every output has 60 pages and retains all markers, known/unknown token behavior
and escaped literals. Actual PDF metadata varies; production PDF bytes are not
claimed identical.

| Median of three sampled calls | Before inline | After offload |
|---|---:|---:|
| Concurrent health | 415.800 ms | 118.019 ms |
| Render wall | 429.241 ms | 476.396 ms |
| Render thread CPU | 427.444 ms | 470.730 ms |

Health ran before rendering ended in 3/3 after calls versus 0/3 before. Baseline
health ranged 388.472–448.242 ms; after ranged 86.724–142.996 ms. Rendering was not
faster. This Python 3.12.14/ReportLab 5.0.1 shared-host experiment has five affinity
CPUs under a four-CPU/32 GiB quota. It does not qualify production auth, database,
storage, native PostgreSQL, an SLO, worst-case body cost, throughput or capacity.
No provider was used; owned threads and TCP port 47771 were cleaned up.

The delivery collects **7,712 backend cases**, not a full-suite pass. Full
Ruff/format checks 1,025 files and Mypy checks 452 app files. The unchanged
OpenAPI/ERD, CI registrations and documentation are checked by the existing
51-control docs/generated/discovery cohort, which passes in 15.37 seconds. No
frontend, native, focused or full
suite is repeated locally; the existing component evidence remains source-bound.
Fresh delivered-source hosted CI and human review are pending.

## Historical process-worker clarification

The third-wave packet already contains a successful full serial/two-worker pair
at `4fd44b8bed97e91cf75ac12649d1f42a580ff2d3` on **Python 3.12.14**. Each side
collected 6,185 cases, passed 5,597 and skipped the same 588, with exact node,
setup/call/teardown and skip-reason parity, no failures, retries or worker restarts.
Whole-process wall was 2,816.609 s (46.9 minutes) serial and 1,347.952 s (22.5
minutes) with two workers. Source/environment/package records match; two-core
affinity was fixed, serial ran first and warm caches remained for the second run.
Sampled aggregate RSS was 4.381 versus 4.957 GiB and double-counts shared pages.
This one ordered pair is not a fleet or memory-capacity comparison.

CI-PROCESS-WORKERS stays QUALIFICATION-PREPARED for current-source qualification;
its original prepared text is retained as history. The old pair does not qualify
current 7,712 cases, hosted Python 3.11, native database parallelism or adoption in
CI. Another current pair is estimated at 85–90 minutes and deliberately deferred
by priority. PR120's actual whole backend job took 35.7 minutes versus PostgreSQL's
41.8, so faster local portable pytest alone has no demonstrated pipeline benefit.
These are whole-job spans, not pure pytest timings or a claim of external blockage.

## Evidence and preservation

- Owner final receipt: `/tmp/bidit-project-document-rendering-proof-20261106/FINAL-RECEIPT.json`, SHA-256 `2038b97711d621d15824e473936085a02c4ede7950191ab22d958a8d11ed3a7c`.
- OID-bound peer receipt: `independent-review/receipt.json` in that packet, SHA-256 `2484494b6ba024e7c9b067ae4efaff666ef779d486c2a031fa1fb4750247ef28`. The earlier SHA `84ac8a78…` is preserved as `receipt-precommit.json`; it is not the current receipt hash.
- Original measurement receipt: `/tmp/bidit-arch006-project-pdf-20261106-1sxnb9tw/FINAL-RECEIPT.json`, SHA-256 `784f39a1e1ea5c305c9e4c3e10a478d29f600bb2bb428332e291aa694047b85c`.
- Historical worker packet: `/workspace/artifacts/backlog-third-wave-2026-11-05/process-workers/`; `pair-verification.json` SHA-256 `57dbc8f6a69bf4f87f5ec5798bf6b457c655ba89e397aaeb5fed4ecd0f2f26a1`.
- Historical extraction: `/tmp/project-document-rendering-delivery-20261106/worker-history/FACTS.json`, SHA-256 `68342b2dce5b9c52abdc94ca325561d7bfadcecfc5189e0185e012760a75fd96`.

All 219 ledger identities, inventory/provenance, statuses and prior evidence remain.
Only ARCH-006 progress and CI-PROCESS-WORKERS historical clarification are appended;
217 unrelated complete rows retain their exact JSON bytes. No production action,
financial policy, current worker adoption or release clearance is claimed.
