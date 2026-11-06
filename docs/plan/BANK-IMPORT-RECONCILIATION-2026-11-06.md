# Bank CSV imports and reconciliation — 6 November 2026

The current delivery combines CSV row accounting and numeric validation
`c1c822d` (including `53c996e`) with current-line reconciliation serialization
`edf6d6e` on corrected PR111 source `df2c344`. RN03 and RN06 remain partial;
RN07–RN09 durable intent, reason and versioned history remain open. Combined
source qualification passes locally; fresh hosted CI remains pending. No completed-backlog,
production-data repair or deployment claim follows from these changes.

## Delivered behavior

CSV imports report skipped date/no-nonzero-amount rows with exact aggregate
counts and at most 20 safe row details, without returning raw source fields.
The shared reader refuses nonfinite numbers in either split debit/credit cell
and selected amounts that cannot fit the existing `Numeric(14,2)` columns.
The existing bank and expense import routes return HTTP 422 before import facts
are written. Finite debit precedence and the existing Decimal rounding remain:
`999999999999.994` rounds to the supported maximum `999999999999.99`, while
`999999999999.995` is refused. Other formats and the shared number parser retain
their existing contracts.

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

Source-bound qualification is retained separately:

| Source slice | Passing evidence | Receipt |
| --- | --- | --- |
| CSV `c1c822d` | 133 portable cases and 3 actual PostgreSQL cases; scoped Ruff/format/Mypy | [CSV receipt](/workspace/artifacts/backlog-thirteenth-wave-2026-11-06/bank-csv-decimal-boundary/receipt.json) |
| Line serialization `edf6d6e` | 66 cases: 57 portable and 9 actual PostgreSQL; source-first failures and UUID replay repair retained | [Reconciliation receipt](/workspace/artifacts/backlog-fourteenth-wave-2026-11-05/reconciliation/TEST-RECEIPT.json) |

These cohorts overlap and are not a combined unique-case total. Their database,
role and schema162 labels describe those historical test runs, not a current
schema baseline or production qualification. Native callers used ordinary
non-superuser/non-bypass roles with tenant RLS retained. The combined portable
union collects 163 distinct cases across nine modules: the prior CSV 133 and
reconciliation 57 share 27 cases. The combined source passes these 163 portable
cases (64.91s) and all 12 native cases (5.33s, zero skips) on the dedicated
schema163 PostgreSQL fixture. The 51 docs controls pass (11.67s), full Ruff/format
checks 981 files and Mypy checks 447 app files. The current tree collects 6,646
backend cases; collection is not a full-suite pass. Native CI registration is
included; fresh hosted CI remains pending. [Combined delivery receipt](/workspace/artifacts/backlog-bank-delivery-2026-11-06/FINAL-RECEIPT.json).
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
- **BANK-LEGACY-NONFINITE-READ:** future CSV rejection does not repair historical
  stored NaN or its read failure. Define a read/recovery contract and preserve
  source/evidence before any authorized repair. First establish affected owned
  records through bounded read-only inspection; do not infer production data,
  coerce NaN to zero or claim that cleanup of synthetic fixtures repaired history.

The [execution ledger](BACKLOG-COMPLETION-2026-11-05.json) preserves the prior
213 rows, including every ID, title, source reference and order. The separately
appended READY finding makes **214 named rows: 197 original + 17 findings**.
Those differently sized items are not a completion percentage. Owner, legal,
provider, corpus, hardware and release dependencies retain their individual
boundaries. Docs preservation receipts are under
`/workspace/artifacts/backlog-bank-delivery-2026-11-06/docs/`.
