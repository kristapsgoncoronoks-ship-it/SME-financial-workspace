# Explore bounded views and downloads — 6 November 2026

Explore now renders Table as a table and exposes every returned group through
local pages of 100. The former table sliced results at 200, while the API could
return 1,000 groups. JSON and all three download formats now disclose the same
bounded scope instead of implying that a capped cut is the complete population.

Qualified component `58a3d075243bb413a1d1896767c775c2c2ecc261` and registration
`646fe6bfbb17453e5c622766e5fe197f3059b4ab` are composed with Expense parent
`e0e00882bfb8b4475bb1534d873692255e7d77fc` at merge
`e6bcbbe888e87ca031b32aa83a9bc4ae71345950`. This is a partial repair of
BE-018, PERF-010 and FE-016. Fresh hosted CI remains pending.

## Scope and compatibility

The existing request default of 100 and range of 1–1,000 groups are unchanged.
The ordered grouped query fetches one additional group, at most 1,001, to
establish truncation. There is no count-all query. JSON adds typed `scope`,
`limit`, `returned_rows` and `truncated` fields; `scope` is `bounded_groups`.
JSON, CSV, XLSX and PDF carry corresponding `X-Explore-*` response headers.
Filenames identify `explore-up-to-N-groups.EXT`. Existing measures, filters,
dimension order, decimal strings, file cells and formula protection remain.

These limits bound returned groups, not the database's underlying scan or
grouping work, invoice count, complete filtered population or fleet capacity.
The UI offers 100, 500 and 1,000 group choices, shows the actual returned and
truncated scope, and suggests narrower filters at the maximum. Downloads rerun
the selected query against current data; they are not immutable snapshots of
the displayed result. Complete filtered exports remain separate work.

Explore downloads retain `REPORT_READ`. The accepted WO-21/WO-77 contract and
existing read-only permission tests distinguish these report renderings from
accounting exports governed by `EXPORT_RUN`. The
[permission review](/workspace/artifacts/explore-download-permissions-2026-11-06/REVIEW.md)
records actual ASGI role probes; it found no authorization defect. No new
permission policy is introduced here.

Table pagination preserves the filters and selected page across visualization
switches, with at most 100 data rows in the DOM. A separate keyboard-scrollable
legend preserves all high-cardinality labels without blocking downloads.
Loading/error states do not expose stale results or allow exports. A pending
download prevents repeated activation, retains the selected query and provides
explicit failure/retry feedback. Existing session and generation fences remain.
At 390px with a 32px root font, a labelled keyboard-scrollable table region
reaches every column without widening the document. Shared styles and visual
baselines are unchanged.

## Source-bound component evidence

The [component receipt](/workspace/artifacts/backlog-explore-2026-11-06/TEST-RECEIPT.json)
retains the original 29 portable actual-ASGI/SQLite and serializer cases,
one native PostgreSQL case at revision 163, and nine mocked-data browser
controls. All passed with zero skips; the browser run used no retries and
retained the existing timeout. Before-change failures and intermediate harness
failures remain recorded separately in that receipt and its contract.

Seven component files remain byte-identical. Shared types retain the parent
`BankLine.version` and nullable/status `ExpenseTransaction` interfaces; replacing
only those parent interfaces projects the whole file exactly to the qualified
Explore component. The 29 portable and nine mocked-browser results are reused
against this source binding without redundant reruns. They do not establish a
connected browser/API journey or a larger combined unique-case total.

## Parent 165 composition

The dedicated empty Explore fixture database was upgraded from 163 through
164 to 165 (`d1e3f5a7c9b2`) by the ordinary application role, with superuser and
bypass-RLS both false. Strict Alembic comparison reports no new operations.
The single native Explore case passes at revision 165 in 3.31s, with zero
failures, errors or skips. It makes nine actual ASGI requests and nine grouped
PostgreSQL queries over 1,001 own groups plus one foreign group, with a maximum
fetch of 1,001. Complete row hashes for 14 relevant tables per tenant remain
unchanged. The local nine-request interval is about 0.44s, not a production
capacity claim. The fixture removes only its two synthetic organizations;
readback confirms zero organizations and zero non-null tenant rows across
115 tenant tables. The revision-163 receipt remains unchanged, and these two
runs are not summed as two distinct cases.

The composed tree collects 7,274 backend cases; collection is not a full-suite
result. It has 122 tables, 165 revisions, 70 model modules, 136 service modules,
50 backend route modules, 69 pages and 10 CI jobs. Full Ruff/format checks
1,009 files, Mypy checks 452 app files, and full frontend lint/build pass.
Four first-load assets total 442.8 kB raw/129.9 kB gzip, within the unchanged
460/135 kB budgets. Regenerated OpenAPI retains all 429 paths and changes only
the Explore response documentation with three typed response schemas; the
122-table ERD is byte-identical to the parent. All 51 docs/generated/CI-discovery
controls pass against the final delivery metadata.

The complete parent workflow is retained with one ordinary-RLS Explore module
added. Parent admin migration/schema controls, ordinary reconciliation commands
and Expense controls remain registered once; frontend specs form the exact
parent union plus Explore once. Dependencies, lockfiles, gates and fixtures are
unchanged. Fresh new-head hosted CI remains pending.

[Final composed receipt](/workspace/artifacts/backlog-explore-2026-11-06/parent165-composition/FINAL-RECEIPT.json)
and adjacent preservation, database and check artifacts bind the delivery.

## Remaining work

Complete filtered exports and a durable immutable export snapshot remain open.
Existing equal-value sort ties have no new secondary order, and reruns can
choose a different tied cut. Existing mixed-currency aggregation and default
display formatting remain unchanged; this repair makes no conversion or
economic-basis claim. Other list/selection/filter/export consumers retain their
individual scopes. No provider, decoder, migration, financial write or new
archive population policy is introduced.

The ledger retains all 215 IDs, order, titles and denominator provenance.
Only existing BE-018, PERF-010 and FE-016 progress is updated; their prior
evidence is retained. All 212 unrelated rows remain exact. There is no new
finding, completion percentage or backlog-wide closure claim.
