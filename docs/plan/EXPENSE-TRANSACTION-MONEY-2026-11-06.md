# Historical expense transaction money — 6 November 2026

Historical unavailable amounts could make the expense inbox, candidate search,
selected-report creation, add-from-transaction, matching and import replay
return HTTP 500. The retained synthetic PostgreSQL diagnosis demonstrates the
failure; it does not establish that production records contain these values.

Component `9d9b5c6dd541b9cab57cba583f29ebd80c8bf3cf` repairs these read and
consumption paths. Registration-only commit
`ceccf36b756262c150c2a431d566bb7f0a6748b9` names the new ordinary-RLS and browser
modules. Reviewed parent 165 `3d06b9e0754f8cb72db6fb1af988a8d64cf87878`
(PR114) is now merged at `68cd76853d5095bc4510623516e93dd8feb1be40`.
Current native, static, frontend and docs checks pass as recorded below; fresh
new-head hosted CI remains pending. The source-bound results remain
separate from the new assembled-source results.

## Read and action contract

`ExpenseTransactionOut.amount` is `Decimal | null`, with an explicit
`amount_status` of `valid` or `invalid`. A response copy checks finiteness and
the existing `Numeric(14,2)` storage rounding. Supported finite values retain
their original signs, precision and currency; the adapter never assigns to
the source row. Invalid rows remain visible with unavailable amounts.

Candidate search skips invalid transaction amounts before Decimal arithmetic.
Financial consumption refuses an invalid selected transaction with the existing
HTTP 409 error shape and code `expense_transaction_amount_invalid`. Existing
owner, currency, current-version and receipt checks retain their precedence.
The original locked selection and finite-positive import guards are unchanged.
Refusal precedes report, item, association, audit and command-receipt writes.

The inbox and detail page show unavailable money explicitly and never format
null as zero. Invalid selection cannot enter a sum, report or match. A selected
row that becomes unavailable can be deselected without losing the other rows.
The existing authorized bin action remains available; it preserves the source
amount and financial records. Local table scrolling and a narrow one-column
summary layout contain the page at 390px with a 32px root font. Shared styles,
dependencies and screenshot baselines are unchanged.

## Immutable outcomes

Current list/import-replay reads may expose the new safe read metadata. Existing
`ExpenseCommand.result_json` and raw command-result endpoints remain as-of.
They do not project current unavailable values over an earlier committed
amount or add fields to an old raw receipt. Report replay after a source amount
changes still returns the original report outcome. No stored money or approved
total is normalized or guessed.

## Source-bound qualification

The [component receipt](/workspace/artifacts/backlog-expense-money-2026-11-06/FINAL-RECEIPT.json)
has SHA256 `291cbb590c7e410645c13b04afbd4df3b00df83b397aa3066598f3a0de5fada6`.
It records these final component cohorts, with zero failures, errors or skips:

| Cohort | Passed | Scope |
| --- | ---: | --- |
| Portable | 184 | 34 new read/range controls and 150 incumbents covering import, report and matching contracts |
| Native PostgreSQL | 20 | 12 new controls and 8 R010 incumbents on the ordinary non-superuser/non-bypass role at revision 163 |
| Mocked-data browser | 80 | 6 new controls and 74 incumbents using explicitly routed synthetic data |

Native controls retain complete before/after row hashes for 114 tenant tables,
prove the actual transaction-lock wait, and cover typed refusal, version/currency
precedence, binning and old/new receipt replay. Source fixtures are declared and
removed after retained evidence; the dedicated database readback has zero
organizations and non-null tenant rows. FORCE RLS remains enabled.

Browser evidence includes stale selection, legacy finite responses, a malformed
amount, read-only controls and keyboard table scrolling at enlarged narrow
text. It is not a connected browser/API journey, physical-device or assistive
technology acceptance claim. The Impeccable skill source and failed context
launcher fallback are recorded in the
[UI context receipt](/workspace/artifacts/backlog-expense-money-2026-11-06/skill-context.json).

Full lint, typing, frontend build and the existing bundle budgets pass at the
component source. Artifact-only schema comparison retains 425 paths and changes
only `ExpenseTransactionOut` at that earlier source. Those component counts
are retained with their revision-163 scope; they are not current parent-165
metadata or a new full-suite pass.

## Parent 165 composition

The [preservation receipt](/workspace/artifacts/backlog-expense-money-2026-11-06/parent165-composition/component-preservation.json)
records nine other owned files byte-identical to the component. Shared types
add only parent `BankLine.version`; `ExpenseTransaction` and every other byte
project exactly to the component. The complete parent workflow is retained with
one ordinary-RLS expense module added; frontend registration retains the full
parent/expense union without duplicates. The admin lane retains
`test_reconciliation_command_migration_pg.py` and
`test_reconciliation_command_schema_pg.py`. Ordinary
`test_reconciliation_commands_pg.py` and `test_expense_transaction_amount_pg.py`
each appear once. No portable migration module is added to the admin lane.
These validated integration registrations do not establish a hosted CI result.

The 184 portable and 80 mocked-data browser results are reused against those
preserved source bytes without redundant reruns. They retain their original
component scope and do not become a larger unique-case total when combined
with the new native run.

The dedicated empty expense database was upgraded by its ordinary role through
163 → 164 → 165, ending at `d1e3f5a7c9b2`. Strict Alembic comparison reports zero
diffs; the writer is non-superuser/non-bypass. At this assembled source, all
20 native expense/R010 cases pass in 19.85s with zero failures, errors or skips:
12 new controls and 8 incumbents, with 115 tenant-table snapshots and the new
FK composition exercised. This is new revision-165 proof; the earlier native
receipt remains dated at revision 163.

The current tree has 122 tables, 165 revisions, 70 model modules, 136 service
modules, 50 backend route modules and 69 pages; 7,260 backend cases collect.
Full Ruff/format checks 1,007 files and Mypy checks 452 app files. Frontend
full lint/build pass, and four assets total 442.7 kB raw/129.9 kB gzip within
the 460/135 kB budgets. OpenAPI retains the parent's 429 paths, with only
`ExpenseTransactionOut` changing; the ERD is byte-identical to the parent.
All 51 docs/generated/CI-discovery controls pass in 15.41s with zero failures,
errors or skips. Fresh new-head hosted CI remains pending. Collection is not a
full-suite pass.

[Final composed-source receipt](/workspace/artifacts/backlog-expense-money-2026-11-06/parent165-composition/FINAL-RECEIPT.json).
Proof and documentation-preservation artifacts are under the same
`parent165-composition/` directory.

## Remaining source remediation

The separate `EXPENSE-LEGACY-NONFINITE-READ` ledger finding is partial with local
checks passed and CI pending. Original statements and an authorized recovery
process are still needed for any affected historical record. No production
prevalence, zero substitution, historical normalization, receipt rewrite,
production migration or deployment is claimed. The bank-line finding remains
separate. The inventory preserves 197 original rows plus 18 findings: 215 named
items, not a completion percentage.
