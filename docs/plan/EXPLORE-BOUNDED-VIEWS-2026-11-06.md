# Explore bounded views and downloads — 6 November 2026

Explore's Table view renders a table and exposes every returned group through
local pages of 100. The earlier fallback rendered a chart, and the table sliced
results at 200 despite an API limit of up to 1,000. Scope is now explicit in
JSON and all download formats. The subsequent
[native-currency and file-contract transition](EXPLORE-NATIVE-CURRENCY-2026-11-06.md)
requires one selected currency for money and contract 2 for money files.

## Bounded result contract

The request default of 100 and range of 1–1,000 groups are unchanged. The ordered
grouped query fetches one extra row, at most 1,001, to establish truncation.
JSON adds typed `scope: bounded_groups`, `limit`, `returned_rows` and `truncated`.
All formats have matching `X-Explore-*` scope headers. There is no count-all
query. These bounds apply to returned groups, not the database's underlying
scan/grouping work, invoice count or complete filtered population.

The UI offers 100/500/1,000 group choices, states the actual returned/truncated
scope and suggests narrower filters at the maximum. Table keeps filters and
local position across visualization changes, with at most 100 data rows in the
DOM. Complete high-cardinality legends occupy a separate keyboard-scrollable
region. Loading/error states disable exports and omit stale values; a pending
download guards repeated activation and retains the selected query. Enlarged
narrow tables remain horizontally keyboard-scrollable without widening the
document. Existing session and generation fences remain.

Downloads rerun current filtered data. They are not immutable snapshots or
complete filtered exports. The accepted `REPORT_READ` contract is preserved.
JSON measure labels and numeric strings remain stable; money's explicit
contract-2 header/filename transition is documented in the linked plan. Generic
writers, sanitization, shared styles and dependencies are unchanged.

## Source-scoped qualification

Original component `58a3d075243bb413a1d1896767c775c2c2ecc261` has a dated
[receipt](/workspace/artifacts/backlog-explore-2026-11-06/TEST-RECEIPT.json)
for 29 portable, one native-at-163 and nine mocked-browser cases. Its first
composition at `11e30bd` retains its separate native-at-165/static receipt.
Those historical results retain their source and precondition scope.

Currency component `a3ef5a2` requalifies the existing workflows with the explicit
new currency/version preconditions, retaining every numeric/filter/permission/
scope assertion. Its 78 portable, three native and 21 mocked-browser cases
include the original bounded cases; the counts are not added again as larger
unique coverage. Separate actual connected API/browser evidence uses the exact
declared three-invoice fixture and preserves financial facts. The linked plan
separates mocked UI, native PostgreSQL and connected SQLite proof.

Perf parent `18714df` is merged at `81738da`. The bounded seven source files and
its Explore type project exactly after reversing only the qualified currency
change. The shared type file retains every incoming Facts addition. Fresh
composed static/build and 51 docs controls pass. OpenAPI retains 433 paths,
ERD remains parent-identical and 7,351 backend cases collect; collection is not
a full-suite pass. Fresh new-head hosted CI remains pending.

## Remaining scope

BE-018, PERF-010 and FE-016 remain partial because other list/selection/filter/
export consumers are assessed independently. Complete filtered exports,
immutable snapshots and secondary ordering for equal-value ties remain open.
At the original bounded stage currency semantics were unchanged; the strict
native-currency repair now refuses mixed-unit money cuts and labels selected
units. It introduces no conversion or source-accuracy claim. ARCH-006 and all
other incoming ledger progress are preserved exactly.
