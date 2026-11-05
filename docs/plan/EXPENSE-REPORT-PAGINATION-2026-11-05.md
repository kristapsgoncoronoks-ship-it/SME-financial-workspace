# Expense report navigation — 5 November 2026

The Expenses page previously displayed the API total while loading only the
first 100 reports. A failure-first browser fixture had 201 own reports and 101
submitted reports, but each table exposed only 100 detail links and no pager.
This slice makes both populations reachable without changing the existing API
or financial commands.

## Behavior and compatibility

“My reports” and “Awaiting my approval” have independent 100-row pages using the
existing `GET /expenses` page/page_size envelope, default 100 and maximum 500.
The existing mine/status/employee/tenant/permission filters remain authoritative.
The only backend read correction adds ascending report ID after descending
creation time so unchanged tied populations have deterministic boundaries.
There is no model, migration, index, write or response-shape change.

`my_page` and `approval_page` preserve unrelated URL parameters and router state.
Query identity includes actor, organization, session, page and size. Obsolete
reads receive cancellation signals and page reads have a 15-second deadline;
the existing global GET retry policy is inherited. Invalid pages normalize,
observed successful count shrink clamps a stale URL, and failed reads remain
distinct from empty pages and empty populations.

Bank import fields, selected transactions and unrelated manual/selected-report
drafts survive browsing. Existing detail links, currencies, role/module gates,
command keys, expected identity, payloads, receipt checks and amount calculations
are preserved. Keyboard paging restores current-scope intent after a pending
read without stealing focus from an edited field. Tables retain local named
scroll regions at 320px and 390px with enlarged text. No visual baseline changes.

## Manual-create readback correction

Integration review caught an old exact `[expenses,mine]` invalidation that no
longer matched the new paginated keys. Both retained red cases created report 202
with one successful scoped POST and a successful detail GET, but sent no list
refetch, left the count at 201 and claimed current data had loaded. A lost-list
control was missed because the list GET was absent.

NewReport now reads the current cache identity and awaits invalidation of the
current actor/organization/session's mine-page prefix. The active page refetches;
inactive current-scope pages become stale. A lost list GET keeps the saved draft
acknowledged and offers explicit GET-only recovery without repeating the POST.
The two-line read-only adaptation supersedes the earlier whole-NewReport byte
preservation claim. Financial mutation handlers and shared command/calculation
helpers remain unchanged, as do all prior focused test assertions.

## Evidence and current tree

- 81 unique pinned Chromium 156 cases pass first attempts with no skips/retries:
  27 pagination cases plus 54 incumbent manual/error/receipt cases. The two new
  page 2 save cases prove actual refetch/count 202 and lost-list GET recovery with
  one unchanged scoped POST, preserved import fields, bank selection and an
  unsaved selected-report title. Browser populations are declared synthetic routes.
- 18 portable API cases pass: seven new paging controls and 11 expense incumbents.
- Two actual PostgreSQL cases at revision 158 pass through real FastAPI ASGI
  requests using a non-superuser/non-bypass application role. Reversed insertion
  with tied timestamps traverses 201 own and 101 approval reports exactly at sizes
  37, 100 and 500. Employee and foreign-tenant boundaries hold; 13 financial,
  receipt, document, audit and job table snapshots remain unchanged. These backend
  suites predate the frontend-only readback correction, which changes none of
  their source.
- Full frontend lint/TypeScript pass. Integration Ruff/format and Mypy (444 files)
  pass; the frontend build is 442.5 kB raw/129.8 kB gzip within existing 460/135
  budgets.
- The current integrated tree collects 6,273 backend cases, with 158 revisions,
  120 tables and one Alembic head `c3f5a7b9d1e4`. Collection is not a full-suite pass.
  Fresh delivered-source CI remains required.

The base includes the sixth-wave parser, shared native-image admission,
recurring-create outcome and capture-review epoch fixes, plus document registry
pagination. Their source-specific evidence and limitations remain separate;
this slice does not requalify all OCR, capacity, workflow or registry behavior.
In particular, native overlay runtime is Python 3.14.8 and host portable/API
qualification is Python 3.12.14, as recorded in the
[runtime erratum](RUNTIME-PROVENANCE-ERRATUM-2026-11-05.md).

BE-018/PERF-010/PERF-014/FE-016 remain partial. Issued/AP histories, document
registry and these report tables do not close other selection/filter/export
contracts. Explore's limited grouped export and the separately inventoried
VAT/receipt-allocation/review/email/audit consumers need individual reviewed
bounds and completeness semantics. No arbitrary cap or complete export is added.
No main merge, push, production migration, deployment, provider/settings action
or complete-backlog/release acceptance is part of this slice.

Evidence: `/workspace/artifacts/backlog-tenth-wave-2026-11-05/expense-pagination/`.
`receipt.json` and `final-source-hashes.json` bind the corrected product/test
source. `historical-pre-readback/` preserves the original 79-case seal;
`readback-red.json` and its traces/screenshots retain the integration regression.
The initial currency-oracle/focus observations and the first post-fix Vite
connection-refused run remain separately recorded. No assertion, retry, timeout,
security gate or dependency was relaxed.
