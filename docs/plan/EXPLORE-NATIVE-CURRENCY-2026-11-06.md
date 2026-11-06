# Explore native currency and file contract 2 — 6 November 2026

Explore now requires an explicit native currency for Net spend, Tax and Gross
spend. It previously summed document-currency line amounts across currencies
and displayed USD rows with a EUR symbol. The normal API and connected browser
diagnosis is retained; it establishes a synthetic defect, not production
prevalence. This repair introduces no FX conversion or allocation policy.

Component `a3ef5a239279ca57159b384c851922ca99e619c5`, registration
`5bed8ad24e0d6de43dc184df82c6097733d4dab1` and the earlier bounded-view change
are composed with Perf parent `18714dfa5f35242a4aa9d0388326bbcf6218ee4a`
at `81738da31f57021134e894a52e4bc320243d4d69`. Local qualification and fresh
composed checks are recorded below. New-head hosted CI remains pending.

## Intentional client transition

A money JSON request must include a user-selected native unit, for example:

```text
GET /api/v1/analytics/explore?measure=net&dim=category&currency=USD
```

`currency` accepts exactly three ASCII letters and normalizes them to uppercase.
There is no EUR default, single-currency inference query, registry preference,
FX lookup or source rewrite. A shape-valid unrecognized code can return an
empty cut; it is not silently changed to a known unit. The existing currency
predicate applies before aggregation, so a concurrent other-currency insert
cannot enter the selected-unit sum. One bounded aggregate remains.

JSON retains its measure labels and adds required `currency` and
`currency_basis` metadata. Money returns the effective code and `native`;
quantity/count return null and `not_applicable`. HTTP responses add
`X-Explore-Currency-Basis` and, for money, `X-Explore-Currency`. Missing money
currency returns typed HTTP 422 `explore_currency_required`; malformed currency
returns `explore_currency_invalid`. The messages explain how to correct the
request. These are intentional new preconditions for old money URLs.

Money files additionally require an explicit `export_contract=2`:

```text
GET /api/v1/analytics/explore?measure=net&dim=category&currency=USD&format=csv&export_contract=2
```

Contract 1 remains the default, and money CSV/XLSX/PDF requests using it refuse
with typed HTTP 422 `explore_export_contract_required`. There is no legacy
money-download escape path. Contract 2 uses a monetary header such as
`Net spend (USD)`, a filename such as `explore-USD-up-to-100-groups.csv`, and
the unit/basis headers plus `X-Explore-Export-Contract: 2`. Numeric strings,
column order, formula sanitization and generic writer bodies are unchanged;
only the declared monetary header label changes. An export-only result copy
keeps JSON measure labels stable.

Clients should add the selected currency to money reads, consume its effective
unit, and opt money downloads into contract 2. File consumers must accept the
declared header/filename transition. Quantity and count queries still work
without currency, and their contract-1 file cells, headers and filenames remain
compatible. This is a client/API transition, with no database migration.

All formats retain the accepted `REPORT_READ` authority, including read-only
members without `EXPORT_RUN`. WO-21/WO-77 and the
[permission review](/workspace/artifacts/explore-download-permissions-2026-11-06/REVIEW.md)
distinguish report renderings from accounting exports; that policy is unchanged.

The UI starts with a blank Native currency field. Money waits for an explicit
valid selection; quantity/count can use all currencies when it is blank. The
effective unit is passed to existing table, tooltip and axis formatters. Empty
and typed-refusal states offer currency/filter correction and GET-only retry.
Clearing, invalidating or changing currency hides stale money and disables
downloads; returned unit/basis metadata must match the current selection.
No shared formatter, style, dependency or screenshot baseline is rewritten.

## What the native basis means

Invoice creation stores line amounts and native invoice totals separately from
`total_eur`, rate and FX provenance. Explore still uses the same line-level
net/tax/gross expressions. `currency_basis: native` describes aggregation units;
it does not verify source amounts, OCR accuracy or the authenticity of stored
FX rates. Stored originals, monetary facts and FX snapshots remain unchanged.
Converted-currency reporting is outside this contract.

The three normal synthetic invoices are USD 100 net at a stated rate of 2,
EUR 50 net, and USD 200 net at a stated rate of 4, each with 10% tax. Each stores
a separate EUR gross snapshot of 55. The corrected USD cut is net 300/tax 30/
gross 330; the EUR cut is 50/5/55. The former unfiltered 350/35/385 money sums
now refuse until a currency is selected. Quantity and count remain 3.000/3.
The browser shows USD with its USD unit, and all three file formats carry it.
The synthetic stated rates are diagnostic source inputs, not market-rate claims.

## Qualification and source binding

The [component receipt](/workspace/artifacts/explore-money-currency-basis-2026-11-06/implementation/FINAL-RECEIPT.json)
records zero failures, errors or skips in these cohorts:

| Cohort | Passed | Scope |
| --- | ---: | --- |
| Portable | 78 | Original 29 bounded/serializer, 34 new currency and 15 nearby dimension/off-loop controls |
| PostgreSQL | 3 | Original bounded case and two new ordinary-role controls at revision 165 |
| Mocked-data browser | 21 | 12 new, eight original Explore workflows and the unchanged exact FE004 catalogue-failure case; zero retries/flakes |

Native proof covers foreign opacity, all formats and unchanged complete
15-table hashes per tenant. A declared gate immediately before the real
aggregate lets an actual second-session EUR POST commit first; the selected
USD sum stays 100 while a count sees both rows. This is a controlled application
scheduling boundary, not a claimed PostgreSQL lock wait. Cleanup confirms zero
organizations and non-null rows across 115 tenant tables; the role is neither
superuser nor bypass-RLS, with actual head `d1e3f5a7c9b2`.

Separate connected Chromium → Vite → actual FastAPI/own metadata-created SQLite
uses the exact three invoices without interception. It records 31 direct API
observations (24 success, seven typed refusals), six UI states, seven browser
JSON reads and three actual file downloads. Every file is parsed; complete
15-table source/financial snapshots match before/after each of two read phases.
These observations are not another test-suite total or native-PG proof. The
owned runtimes/cache are stopped/removed. Narrow 390px/root-32 renders and
source-booleans are measured scope, not full accessibility, production capacity
or source-accuracy acceptance.

All 114 existing backend assertions and 48 existing browser assertion lines
remain, with only approved query/header/filename preconditions changed.
Thirteen currency component files are byte-identical after composition. Shared
types retain the exact Explore interface and incoming parent Facts additions;
substituting the parent Explore interface reproduces the whole parent file.
The bounded component also projects exactly after reversing only the qualified
currency change. Invoice creation, models, FX, generic writers and the shared
formatter retain their qualified behavior. Component runtime cohorts are
reused against this source binding; they are not blindly rerun or expanded.

Fresh composed checks pass full Ruff/format over 1,014 files, Mypy over 452 app
files, frontend lint/build and the unchanged 460/135 kB budget at 442.9 kB raw/
129.9 kB gzip across four assets. The tree collects 7,351 backend cases, with
122 tables, 165 revisions, 70 model modules, 136 service modules, 50 route
modules, 69 pages and 10 CI jobs. Collection is not a full-suite result.
Regenerated OpenAPI keeps 433 paths; only Explore gains its typed response
and version/precondition documentation. The ERD is parent-identical. All
51 docs/generated/CI-discovery controls pass; fresh hosted CI remains pending.

[Final composed receipt](/workspace/artifacts/explore-money-currency-basis-2026-11-06/final-composition/FINAL-RECEIPT.json).
The ledger preserves all 215 parent identities, ARCH-006 and unrelated progress,
updates only existing bounded-view progress and appends the distinct currency
finding as 216 (197 original plus 19 findings). This is not a completion
percentage. Complete filtered exports, immutable snapshots and equal-value
tie order remain separate work; the broader backlog is not declared closed.

## R15 caller follow-up — 6 November 2026

PR118 at `ae3925e` exposed a missed runtime caller: `explore_by_vendor` omitted
the required currency and received HTTP 422 before emitting `shape.json`.
The perf seed stores every invoice and its line amount in EUR, so its request
now explicitly selects `currency=EUR`, retaining the complete seeded population.
The scenario label, budgets, thresholds, 12 repetitions, two warmups and factor
four are unchanged. All nine existing perf/seed controls pass, including the
actual ordinary-role PostgreSQL shape gate at 100 → 400 rows: the result is
emitted and parsed, and Explore's observed p95 grows 1.78× against its unchanged
9× ceiling. This local smoke does not replace hosted CI at its larger scale.
The original failed CI log and before/after evidence remain in the
[follow-up receipt](/workspace/artifacts/explore-perf-scenario-2026-11-06/FINAL-RECEIPT.json).
