# Durable bank-line reconciliation commands — 6 November 2026

Ignore now records a required explanation. Match, Ignore, Unmatch and Restore
all submit the version and exact state the user reviewed, together with a new
command key. A delayed action cannot clear a later match or a later Ignore
cycle. A saved key recovers the original committed outcome; a separate current
read shows what the line looks like now.

This is locally qualified delivery of the reviewed RN07–RN09 command boundary,
with further RN06 progress. Final composed-source checks pass in the scopes
recorded below; fresh delivered-source hosted CI remains pending. RN03 bank-account
attribution, historical source remediation and unrelated backlog decisions remain open. These operational
dispositions do not establish a verified bank balance or change settlement.

## User behavior and recovery

The page sends the reviewed line version, status and association. It does not
fetch a fresh version to silently rebase an old intention. Each new key needs
a real transition:

| Intention | Required reviewed state | Result |
| --- | --- | --- |
| Match | Unmatched, with no association; the chosen cash target remains eligible | Matched to the chosen kind and ID |
| Ignore | Unmatched, with no association and a nonblank explanation | Ignored, with the explanation retained |
| Unmatch | Matched, with the exact reviewed kind and ID | Unmatched; no payment reversal |
| Restore | Ignored, with no association | Unmatched; earlier Ignore evidence remains |

Ignore explanations are trimmed, valid UTF-8 text of at most 1,000 characters.
The browser uses the server's whitespace definition, including NEL and the
information separators. There is no reason taxonomy or new accounting rule.

An unresolved key and its exact intention are saved before sending. Timeout,
abort, malformed replies and read failures keep that reference. Check Outcome
uses GET only. An authorized missing outcome permits an explicit retry with
the same key and body; it does not prove an earlier request is no longer in
flight. A stale conflict keeps the old intention for review, without generating
a replacement key or silently changing its version. Acknowledgement of a
committed result survives a failed current read.

A current version may be newer than the receipt's version. The page keeps
historical acknowledgement separate from current readback. Actor, workspace,
credential generation and component lifetime fence late callbacks and storage
cleanup. Read-only users can inspect permitted history and their own outcomes,
but cannot submit or retry a command.

## API and immutable outcomes

`POST /api/v1/reconciliation/lines/{line_id}/commands` requires an
`Idempotency-Key` and a typed intention with `operation`, strict positive
`expected_version`, coherent `expected` state/association, a Match-only
`target`, and optional `reason`. The normalized payload is capped at 8 KiB. The maximum version is
2,147,483,647; exhaustion
returns a typed 409 before changing the line. Undeclared fields, invalid text
and invalid intentions return a sanitized 422 without echoing source text.

A version, state or association mismatch returns
`reconciliation_line_stale` (409). Reusing a committed actor/workspace/key
with a different normalized intention, operation or line returns
`reconciliation_command_payload_conflict` (409). UUID spelling is normalized
before hashing and comparison.

The original and same-key POST response are the same frozen committed receipt.
Replay verifies the stored outcome before checking today's line or target
eligibility. It therefore acknowledges a past action even after later valid
transitions or target changes. A new Match still runs the existing money,
currency, direction, paid-status and global target one-to-one guards.

| Read | Meaning and audience |
| --- | --- |
| `GET /lines/{line_id}/commands/{key}` | The original actor's outcome in the current workspace; mismatched actor, workspace or line is opaque 404 |
| `GET /lines/{line_id}` | Current safe bank-line fields and persisted version |
| `GET /lines/{line_id}/history` | Authorized transition history, ordered by descending after-version; default 20, maximum 100, with a bounded cursor |

All routes above share the `/api/v1/reconciliation` prefix. Reads require
`PAYMENT_READ`; POST, including replay, requires `PAYMENT_WRITE`. The issuing
module and current tenant authority still apply. History omits other actors'
command keys and full replay payloads.

The receipt exposes the canonical intention's facts through contract version,
line ID, operation, before-version/state, reason and the Match association in
the after-state. Canonical `payload_json` is stored and verified server-side;
there is no second payload object in the response. The browser validates the
captured facts and hash shape; no browser digest recomputation is claimed.
The line snapshot is deliberately the disposition at that action, without a
copy of bank descriptions or amounts. Corrupt stored hashes or identities
return a safe error and never trigger another mutation.

`recorded_at` is fixed inside the transaction. It is recording time, not an
independently measured PostgreSQL commit instant. Durable receipt presence and
its committed status establish that the transaction committed.

## History and the compatibility transition

Existing lines receive version 1 as a concurrency baseline. No earlier action,
reason, actor or time is reconstructed. An existing ignored line can therefore
have no recorded command history. Restore preserves an earlier recorded Ignore;
a later Ignore creates a new version and explanation rather than rewriting it.

The old `POST .../match`, `POST .../ignore` and `DELETE .../match` routes now
return actionable 428 after the existing permission, module and opaque line
checks. Even a missing old Match body cannot invoke an unversioned write.
The delivered UI uses the canonical endpoint. External clients must migrate;
there is no compatibility adapter, inferred Restore or automatic current
version.

After the core runtime qualification, root added `deprecated=True` and the
428 response documentation to those three route decorators. This is an
OpenAPI metadata change, with no handler, body or admission-control change.
Its route-file hash differs from the original component snapshot. The final
composition checks include that documented delta; the old route hash is not
claimed to match.

## Atomicity, data and schema

One transaction locks the current actor's Membership, then the source line and
concrete Match target, before taking the tenant audit lock. Live session,
workspace, role and module checks run again after waits with no pending domain
changes. The identity refresh does not touch or commit Session; the source is
reloaded using captured IDs after expiration. This is authorization at final
admission, not a promise that revocation after that point cancels a commit.

The transaction increments the persisted integer once and appends one
source-owned receipt/history row and one hash-chained audit event. A rejected
request or same-key replay adds none. Audit absence or failure, SQL failure and
precommit cancellation roll back line, counter, receipt and audit together.
A uniqueness failure is rolled back before a fresh authorized outcome lookup.
Known command SQL errors are sanitized so bound source payloads do not reach
generic exception logs.

A lost COMMIT response can follow a real commit. Recovery checks the durable
key; it does not promise rollback after transport loss. Portable controls
exercise both precommit and postcommit loss, and the native controls separately
prove a real failed PostgreSQL transaction after domain SQL.

Revision `d1e3f5a7c9b2` (165) follows actual revision `c0d2e4f6a8b1` (164).
It adds the bounded integer version, an explicit `(org_id, id)` bank-line
unique index and `reconciliation_commands`. The new table has the same-org
line FK, a global `User.id` actor FK, scoped uniqueness/history indexes and
actual ENABLE/FORCE RLS plus its tenant-registry declaration. A user's default
organization is not current membership authority. Actor RESTRICT and
organization/source-line CASCADE retain the reviewed lifecycle conventions;
no new retention schedule or delete API is introduced.

Global masked preflight refuses contradictory historical state and unexpected
named objects without repairing data. SQLite uses safe direct DDL with foreign
keys enabled; the existing bank-line table is not rebuilt. Raw amounts,
balances, currency and source identity remain unchanged, including historical
unavailable money. Cash-target types retain their existing finite/rounding
rules, without imposing the bank source's `Numeric(14,2)` bound on them.

Operational rollback keeps the additive schema and receipts. Reconciliation
writers must be paused or rolled forward; old unversioned writers must remain
disabled. Destructive downgrade refuses any command/history or any line
advanced beyond version 1. It cannot erase reasons or reset the ABA fence.
A pristine baseline-only roundtrip removes only the owned additions.

## Source-scoped qualification

Backend implementation `a568aa4c577f40a83ac257f689162cfabe587240` and the native
proof follow-up `e0d50093ebf27175a6a4d9f81a82d29ca4b70304` have **220 distinct
passing pytest nodes** in the component inventory, with no duplicated nodes
inside that inventory:

| Component | Passing cases |
| --- | --- |
| Portable commands, incumbent financial/currency/legacy checks and boundaries | 127 |
| Schema/migration and parity controls | 53: 38 portable, 13 native and 2 parity |
| Incumbent native legacy-money and line/target schedules | 20: 11 legacy-money and 9 line/target |
| Native command authority, contention, replay, ABA and failed-transaction controls | 20: original 17 plus appended 3 |

The last native proof is 17 passes in 31.86s plus 3 focused passes in 9.25s,
with the prior 17 AST bodies, fixtures and helpers unchanged. That component
receipt is not one combined 20-case execution. The later composed 43-case RLS
cohort executes all 20 command-native cases together. Real PostgreSQL blocking
PIDs and query/lock observations cover distinct-actor line/target waits, same-actor key queues,
role/offboarding order and actual switch/logout while waiting. Actual SQLSTATE
22012 after pending line/command/audit SQL proves full rollback and a healthy
explicit same-key retry. Both native ABA paths preserve the later state.

The overlapping root graph cohort collects 31: 28 pass, 3 are explicit
SQLite-only skips, and all 12 native graph cases execute. Three tenant route
probes cover own outcomes/history, same keys across organizations and foreign
404 opacity. These and the 36 final mocked-browser controls are separate,
overlapping evidence; they are not added to 220 as a larger unique total.

The frontend's 36 controls comprise 35 bank cases plus one incumbent FE004
case, without retries. Lint/build and the unchanged 460/135 kB budgets pass at
442.7 kB raw and 129.9 kB gzip in that source scope. Controlled guard-disabled
reds are retained for malformed receipt IDs/dates, Unicode whitespace and
corrupt saved-key types.

### Actual connected browser, HTTP and restricted PostgreSQL

At frontend source `d75afdb`, actual Chromium imported a synthetic statement
through the UI and submitted one Ignore with a reason to the isolated API.
The harness fetched the real server response after commit, then deliberately
aborted its delivery to the browser. It also aborted the first outcome lookup
before upstream delivery and the first current read. These are labelled
browser transport faults, not a server outage or a database COMMIT-transport
experiment.

Reload and own-key GET recovered the committed receipt without a second POST.
The confirmed key survived the failed current read, and another GET completed
readback/history. Separate authoritative SQL snapshots on the owned database
proved one command, one version advance from 1 to 2, and one new command audit.
Across 18 monitored tables, financial rows, including a nonempty cash-control
receipt, stayed unchanged. Only the line disposition, version and update
metadata, plus the required new command/audit entries, changed. Original source
projection, raw amount/balance/currency and FORCE RLS were preserved. Stored request/result hashes and the audit's reason hash verify;
audit metadata contains no raw reason or bank source fields.

The final frontend `6d9216a` adds one saved-key string-type guard after that
journey. Two corrupt-storage controls first reproduced the unwanted POST and
then passed with the guard; final mocked-browser/static checks cover this
bounded delta. The connected journey is not claimed as a byte-identical rerun
of that later frontend. The API at 8016 and Vite at 5193 were stopped by their
owners. The isolated database and private fixture records remain owned for
review; no production database or default API at 8000 was used.

| Evidence | Location |
| --- | --- |
| Component receipt and exact 220-node inventory | [Final receipt](/workspace/artifacts/bank-line-commands-2026-11-06/FINAL-RECEIPT.json), [node inventory](/workspace/artifacts/bank-line-commands-2026-11-06/QUALIFIED-NODE-INVENTORY.json) |
| Native source and matrix boundaries | [Native receipt](/workspace/artifacts/bank-line-commands-2026-11-06/schema/NATIVE-BEHAVIOR-RECEIPT.json), [matrix](/workspace/artifacts/bank-line-commands-2026-11-06/schema/NATIVE-BEHAVIOR-MATRIX.json) |
| Actual connected SQL proof | [SQL verification](/workspace/artifacts/bank-line-commands-2026-11-06/connected/sql-verification.json) |
| Browser source and final saved-key delta | [Frontend receipt](/workspace/artifacts/backlog-reconciliation-commands-ui-2026-11-06/FOLLOWUP-RECEIPT.json) |
| Root graph qualification | [Graph receipt](/workspace/artifacts/backlog-reconciliation-command-2026-11-06/root-graph/FINAL-RECEIPT.json) |

## Composed-source checks and remaining work

The current tree collects 7,214 backend cases. Base metadata has 122 application
tables, with 165 Alembic revisions, 70 model modules and 135 service modules.
Collection is not a full-suite pass. The composed source passes **432 portable
cases in 254.58s**, including all 110 tenant-parity, 38 schema and 51
docs/generated/CI-discovery controls. Its **43-case RLS cohort passes in 48.35s**:
42 database cases and one registry control, including all 20 command-native
cases executed together. These scopes overlap the 220 component cases and
other receipts; no larger unique total is inferred.

Full Ruff/format checks 1,004 files and Mypy checks 451 app files. Frontend
lint/build and the bundle gate pass at 442.7 kB raw/129.9 kB gzip against
460/135 kB budgets. OpenAPI is regenerated with 429 paths and the ERD has
122 application tables. Fresh delivered-source hosted CI remains pending.
These local checks are not a release or complete-backlog acceptance claim.

All 214 ledger identities, order, titles, source references and original
inventory classifications remain. Only RN06–RN09 progress/status is updated
for this delivery. RN06 retains its partial account-attribution boundary;
RN03, historical source remediation, bank account/entity/control-total policy,
owner/provider/accountant acceptance and unrelated work remain open. There is
no cash settlement, reversal, classification, provider, production migration,
main merge or deployment in this delivery.
