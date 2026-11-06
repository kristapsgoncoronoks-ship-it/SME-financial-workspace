# Bank imports and reconciliation — 6 November 2026

The initial CSV/current-line serialization delivery was published as PR112
`ebf0a84`. Phase3 `c9e63dbfb889f75460a10e7eb53dcf48c6c0bbea` and legacy-read
`f847e3dc09ae095c549784dc435dbed39d3b52d4` are now merged at
`fee10422eacdd79a2858b58321af4bbaa1cd96cf`. Combined local checks pass as
recorded below; fresh new-head hosted CI remains pending.
RN03/RN06 remain partial; RN07–RN09 durable intent, reason and versioned history
remain open. Safe legacy bank reads/actions/UI are delivered locally;
historical remediation and source verification remain open. No production-data
repair, deployment or completed-backlog claim follows from these changes.

## Phase3 shared public parse validation

Every supported-format public parse result now validates finite amount and
optional balance against the current `Numeric(14,2)` storage boundary. Known
`DecimalException` failures become the existing HTTP 422 before import writes.
The guard leaves valid returned values, sign, currency, rounding and `_num`
unchanged; it does not widen storage precision or alter CAMT's existing
unparseable-amount omission policy.

The [phase3 source receipt](/workspace/artifacts/backlog-bank-nonfinite-2026-11-06/phase3/TEST-RECEIPT.json)
has SHA256 `578dd6cbb586a38f1846071551838c02f48e7332e1e95e9bf7b1d5ed827ab960`.
Its **195 distinct passing cases** comprise 37 new portable, 11 new native,
3 incumbent native and 144 incumbent portable cases. This is source-scoped
qualification of phase3, not a new combined-head or hosted-CI result. New PDF
checks use pure text or labelled extraction seams; they make no recognizer,
native-capacity or broader PDF acceptance claim.

## Safe legacy bank reads and matching

`BankLineOut.amount` is now `Decimal | null` with `amount_status` of `valid` or
`invalid`. Its balance is `Decimal | null` with `balance_status` of `valid`,
`invalid` or `unprovided`. Valid money stays unchanged; unavailable money is
not zero. Reads preserve stored facts.

Suggestions and Confirm refuse invalid bank or cash-target money with existing
HTTP 409 `{detail: string, code: string}` responses, using
`bank_line_amount_invalid` or `reconciliation_target_amount_invalid`. These
refusals write no stored facts. The UI explicitly marks unavailable amounts for
manual review and offers no Match/Suggest for a bad amount. Ignore and Restore
retain their existing command contracts. The page contains the recovery copy
at 390px with a 32px root font; shared styles and visual baselines are unchanged.

The [legacy source receipt](/workspace/artifacts/backlog-fourteenth-wave-2026-11-06/legacy-bank-read/receipt.json)
has SHA256 `9715812caee9c580bede2d76e8faf44895ec09d11d7ed2d225c4720a855894a5`.
It records 68 portable cases (19 new and 49 incumbent), 11 native cases and
8 **mocked-data browser** cases (5 new and 3 incumbent currency checks).
All nine legacy source hashes and all three phase3 hashes match after merge;
the browser results apply to the unchanged frontend sources. They do not
establish a live browser/API journey. These source cohorts overlap other
receipts and are not added as unique combined coverage.

OpenAPI retains 425 paths and changes only `BankLineOut`; models, migrations
and ERD remain unchanged. There are 134 service modules.

## Combined local qualification

### Projects navigation correction from the initial CI run

The initial `ebf0a84` browser run failed after navigation from project photos
to Projects. Its mock returned a paginated object for the PnL summary endpoint,
whose actual contract is an array. The corrected fixture supplies the array and
waits for the loaded destination before the original checks. Independently,
the valid destination overflowed to 725px at a 390px viewport with a 32px root
font. Wrapping its local tabs/filter and adding a labelled, keyboard-scrollable
table region keeps every column available and the document at 390px.

Qualified source `3685125a36e55433191c2ebbe20460d8e7d20e04` passes all 64
photo, project PnL, context and reflow browser cases without retries, plus lint,
build and bundle checks. The two merged file hashes match that source receipt.
Original photo assertions, geometry thresholds, timeouts and visual baselines
are unchanged. This local mocked-browser evidence does not establish the exact
cause of the 686px width in CI's error state or qualify a fresh hosted run.
[Sealed receipt](/workspace/artifacts/backlog-project-photos-2026-11-06/TEST-RECEIPT.json),
SHA256 `9648235c990160183bc8dfe0b1c862beaa136fc7a52691d9beabd4cc017b9655`.

At merged source `fee1042`, the combined bank cohorts pass **230 portable
cases (91.81s)** and **34 actual PostgreSQL cases (22.52s)**. The **51
docs/generated/CI-discovery controls pass (14.60s)**. All three cohorts have
zero failures, errors or skips. Full Ruff/format checks 986 files and Mypy
checks 448 app files; frontend full lint and build pass. The four-asset bundle
passes at 442.5 kB raw/129.8 kB gzip against the 460/135 kB budgets. The tree collects
6,724 backend cases, which is a collection count rather than a full-suite pass.
The eight source-scoped mocked-data browser checks above are reused because
the merged frontend hashes match; no new live browser/API proof is claimed.
Fresh new-head hosted CI remains pending.
[Final follow-up receipt](/workspace/artifacts/backlog-bank-delivery-2026-11-06/followup-phase3-legacy/FINAL-RECEIPT.json).

## Initial delivered behavior

CSV imports report skipped date/no-nonzero-amount rows with exact aggregate
counts and at most 20 safe row details, without returning raw source fields.
The shared reader refuses nonfinite numbers in either split debit/credit cell
and selected amounts that cannot fit the existing `Numeric(14,2)` columns.
The existing bank and expense import routes return HTTP 422 before import facts
are written. Finite debit precedence and the existing Decimal rounding remain:
`999999999999.994` rounds to the supported maximum `999999999999.99`, while
`999999999999.995` is refused. Phase3 adds the shared public result validation
described above while retaining the existing format parsers and number parser.

Match, Ignore and Unmatch/Restore now refresh and lock the tenant-scoped bank
line before reading its current state; Match then takes the existing target
lock. A later action cannot overwrite a current association or match an ignored
line without the existing Unmatch/Restore transition. Validated exact replays,
including equivalent UUID spellings, perform no new line write. Existing target
uniqueness, eligibility, money/currency, permission and module rules remain.
Row locking serializes current state; a later valid Unmatch/Restore can still
change that state.

## Evidence and limits

The retained [30-case baseline receipt](/workspace/artifacts/backlog-bank-nonfinite-2026-11-06/baseline53c996e/TEST-RECEIPT.json)
records actual FastAPI ASGI and PostgreSQL behavior at frozen source commit
`53c996e43eb3c2e55753bdc1bfc627b50d992abe`. Its receipt SHA256 is
`cf4b6c431aad92bbc6c8f48d243eb98987de1bb02653c2ba5c35e4eab6febc1e`;
that hash identifies the receipt, not a source commit.
Split debit or credit quiet NaN returned **POST 201**, committed one statement
and one NaN bank line, then returned **GET 500** on line readback. No tested
POST 500 committed a bank line. The other tested nonfinite/overflow failures
committed no bank rows; nine finite controls returned POST 201/GET 200. All
18 monitored nonbank table hashes stayed unchanged. These were owned synthetic
fixtures, retained in raw receipts before their scoped cleanup; they do not
establish that production or other historical records contain NaN.

The separate [16-case non-CSV baseline receipt](/workspace/artifacts/backlog-bank-nonfinite-2026-11-06/noncsv53c996e/TEST-RECEIPT.json)
records actual CAMT/MT940 API behavior at the same frozen `53c996e` source;
SHA256 `79608ae9a309bcd591ec47ad3eed05ee7bb1b425bd19084a6d117150c59af6c2`.
CAMT quiet NaN returned POST 201, committed a bank line and then returned GET 500.
No observed POST 500 committed a bank row. Its PDF evidence was pure text only,
without a PDF API/decoder claim. This synthetic diagnosis does not establish
production data or repair historical records.

Initial source-bound qualification is retained separately:

| Source slice | Passing evidence | Receipt |
| --- | --- | --- |
| CSV `c1c822d` | 133 portable cases and 3 actual PostgreSQL cases; scoped Ruff/format/Mypy | [CSV receipt](/workspace/artifacts/backlog-thirteenth-wave-2026-11-06/bank-csv-decimal-boundary/receipt.json) |
| Line serialization `edf6d6e` | 66 cases: 57 portable and 9 actual PostgreSQL; source-first failures and UUID replay repair retained | [Reconciliation receipt](/workspace/artifacts/backlog-fourteenth-wave-2026-11-05/reconciliation/TEST-RECEIPT.json) |

These cohorts overlap and are not a combined unique-case total. Their database,
role and schema162 labels describe those historical test runs, not a current
schema baseline or production qualification. Native callers used ordinary
non-superuser/non-bypass roles with tenant RLS retained. At initial delivery `ebf0a84` (PR112), the combined portable
union collected 163 distinct cases across nine modules: the prior CSV 133 and
reconciliation 57 share 27 cases. That initial source passed these 163 portable
cases (64.91s) and all 12 native cases (5.33s, zero skips) on the dedicated
schema163 PostgreSQL fixture. The 51 docs controls pass (11.67s), full Ruff/format
checks 981 files and Mypy checks 447 app files. That dated tree collected 6,646
backend cases; collection is not a full-suite pass. Native CI registration is
included in that initial delivery; those results do not qualify this follow-up
head. [Initial combined delivery receipt](/workspace/artifacts/backlog-bank-delivery-2026-11-06/FINAL-RECEIPT.json).
The unrelated runtime GUI profile implements none of this bank work.

## Remaining work

- **RN03:** bank account/control attribution, durable import history/readback
  and broader supported-format acceptance remain open. The import is not bank
  balance verification.
- **RN06:** current-state races are repaired; stale intent/version, audit/history
  and unknown-currency account attribution remain open.
- **RN07:** ignoring a line still needs the reviewed reason/audit and durable
  command contract.
- **RN08:** Undo match still needs expected-version intent, durable receipts,
  stale-conflict recovery and history; row locks do not prevent delayed intent
  or ABA from clearing a newer association.
- **RN09:** Restore still needs distinct replay-safe intent, versioned
  reason/history and recovery.
- **BANK-LEGACY-NONFINITE-READ (PARTIAL-LOCAL-CHECKS-CI-PENDING):** the safe
  bank-read, matching-refusal and UI portion is delivered locally. Historical
  remediation and source verification remain open. Preserve source/evidence
  before any authorized repair and first establish affected owned records
  through bounded read-only inspection; no amount rewrite, zero substitution,
  production-data existence or historical repair is asserted.
- **Separate expense candidate read:** actual legacy `ExpenseTransaction` NaN
  still makes `GET /expenses/{report}/items/{item}/match-candidates` return 500.
  The [source observation](/workspace/artifacts/backlog-fourteenth-wave-2026-11-06/legacy-bank-read/separate-expense-candidate.json)
  (SHA256 `045d5f2872c0ad56e58ba281093388f8fedc866568516ee2cf1848057cdc6e2c`)
  diagnoses a separate issue. This bank repair does not fix or qualify it, and
  that observation is excluded from bank acceptance counts.

The [execution ledger](BACKLOG-COMPLETION-2026-11-05.json) preserves the prior
213 rows, including every ID, title, source reference and order. The separately
appended finding makes **214 named rows: 197 original + 17 findings**.
Those differently sized items are not a completion percentage. Owner, legal,
provider, corpus, hardware and release dependencies retain their individual
boundaries. Docs preservation receipts are under
`/workspace/artifacts/backlog-bank-delivery-2026-11-06/docs/`; follow-up
preservation is under
`/workspace/artifacts/backlog-bank-delivery-2026-11-06/followup-phase3-legacy/docs/`.
