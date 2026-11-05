# Document registry navigation — 5 November 2026

The registry previously fetched and rendered every document metadata row. A
203-record synthetic browser fixture produced203 table rows and a52,259-byte
initial response. Selecting one kind removed the other kind from the dropdown;
the390px mobile page expanded to434px. These are measured source-specific
observations, not production population or device benchmarks.

## Behavior

Additive `GET /documents/page` returns `items`, a current filtered `total` and
tenant-wide `kinds`. Row retrieval defaults to100 and refuses limits outside1–200
or negative offsets. Ordering uses descending creation time and ID so tied
timestamps do not reorder between unchanged pages. Count, rows and distinct
kinds use three scoped reads. The helper explicitly disables autoflush and
does not commit its caller's work. Existing admin authorization and tenant
isolation remain authoritative.

The browser uses100-row pages with URL-backed kind/page and preserves unrelated
URL context. Changing kind starts at page1 and keeps the other available choices.
Loading, failed, empty and concurrently empty pages are distinct. Manual recovery
repeats only the requested GET; there are no automatic retries or writes. Fresh
successful counts clamp stale page URLs without treating a failed read as empty.

The metadata table scrolls inside a named keyboard-focusable region on narrow
screens. Existing desktop columns, source descriptions and download ownership
remain intact. Mobile controls retain44px targets;320px and enlarged text checks
pass. No visual regression baseline is changed.

The old array endpoint and service remain compatible. They are not silently
capped; older external clients can still request the entire population. Counts
and offset pages describe current reads rather than an immutable export snapshot.
Other list endpoints, selection and export contracts remain separate work.

## Evidence

- Seventeen portable checks, including six existing registry regressions, pass.
  Four native PostgreSQL checks pass with actual non-superuser/non-bypass
  credentials, FORCE RLS,203 records, tied IDs, tenant-opacity and unchanged
  registry/invoice/expense/job/audit snapshots. Existing concurrent registry
  writers still preserve their callers' transactions.
- Twenty-seven browser cases pass with two workers/retries0, including six
  existing master-data cases. They cover complete traversal, facets, history,
  bounded GET recovery, invalid/stale URLs, pending controls, narrow screens and
  enlarged text. These use declared mocks and do not prove database outcomes.
- In the same203-record mock, the initial page response is25,686 bytes and100
  rows.390px document width is390px. This establishes bounded payload/rendering
  for that fixture, not latency, Core Web Vitals or production capacity.
- Frontend types/lint/build pass; first-load bundle is442.5kB raw/129.8kB gzip
  within existing460/135 budgets. OpenAPI and native-test discovery are updated.
  No dependency, model, migration, retention or financial command changes.

The branch has158 revisions and6,198 collected backend cases. Fresh full
delivered-source CI and combined actual browser/API/PostgreSQL readback remain
required. No main/PR merge, production migration, deployment, external settings
or complete-backlog/release claim is made.

Artifacts: `/workspace/artifacts/backlog-eighth-wave-2026-11-05/document-pagination/`.
Original setup failures are recorded separately: the preserved browser cache
needed an explicit path, and the old wrapped label required its existing partial
locator. Final assertions and application contracts were not relaxed.
