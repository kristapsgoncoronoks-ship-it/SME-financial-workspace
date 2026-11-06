# Readiness evidence and remaining decisions — 6 November 2026

These are completed, source-scoped observations, not operating-policy changes.
The three studies bind frozen PR122 `5d4c7ef78ffdcd28eab6545172be06404066cd0d`
and PostgreSQL revision166 `e2f4a6b8d0c3`; they are not new timings of the request-ID
repair or future delivery. All use separate registered owned databases and the
ordinary `backlog_app` role without superuser/bypass-RLS. Clients and engines
were closed; databases remain owned and retained for review, with private
environment files. No provider or production data/action is involved.

| Existing row | Completed observation | Limit and next decision |
|---|---|---|
| PERF-015 | Actual ASGI HTTP read in an exhausted fixture pool returned safe 500 after 30.038 s; an explicit retry returned 200 in 0.278 s. All 122 recorded table hashes and selection remained unchanged. | One refusal/retry, no live browser/TCP or workload distribution. UI 15 s and backend 30 s are distinct. Keep pool/timeout defaults; representative workload/hardware and proxy/browser behavior remain to qualify. |
| DB-021 | One native pre-COMMIT Task cancellation was observed on a separate connection during an active PgSleep query. Explicit rollback, join and connection exit completed in 6.308 ms; marker absent, healthy next use of the same engine, zero checked-out connections and all 122 recorded table hashes unchanged. | The connection was invalidated: this is not proof of reusing that physical connection. No HTTP/worker cancellation, network/PgBouncer, financial command or lost/uncertain COMMIT proof. No global timeout follows. |
| ARCH-010 | One actual supported 1,000-row CSV expense import took 384.118 ms through commit acknowledgement; audit-call→commit 12.139 ms and lock-ack→commit 11.028 ms.62 observer samples saw 5 granted-lock samples and no waits. | No artificial hold/contender, repetition or contention distribution. The client interval is not exact continuous server lock lifetime. No HTTP write, rollback/retry or lost-COMMIT proof; preserve the atomic source/audit transaction. |

The expense import preserves the exact 1,000 rows, source digest, total 1469.95,
command payload/result and pre-existing receipt. Exactly one audit is added;
the resulting two-event chain validates. Only `audit_events`, `expense_commands`,
`expense_import_batches` and `expense_transactions` change as intended; the other
118 table hashes remain. No audit-lock optimization defect was demonstrated.
The cancellation study retains 10+10 pool shape/30 s checkout, no configured
command timeout and local statement/lock/idle-transaction timeouts 0. These are
fixture facts, not recommended production values. Recorded hash scopes remain
those of each ordinary-role fixture, not a production census.

The PERF-015 observation found a separate missing error request ID. The
[request-ID component](UNHANDLED-ERROR-REQUEST-ID-2026-11-06.md) fixes that response
boundary with actual-stack tests. Its 50-case evidence does not relabel the old
HTTP measurement as a repaired-source performance or browser experiment.

## SEC-X-SYS: actual PR121 qualification

PR121 head `9b8e4647adac233ee81bc74505c3c2905d9f294b`, tested merge
`4d7d16cf717bb3af4b390bb8e6b6a75e3080c589`, has the exact 13 required contexts
successful, with raw logs and inventories retained. The existing row advances
to CI-PASS-HUMAN-REVIEW; earlier INVESTIGATE/pending text and qualification
objects remain dated history. This qualifies the implemented Linux/amd64 helper
maintenance, not every future platform or a production release.

All 10 image targets and 20 saved reports are hash-verified. The actual PostgreSQL
runtime image `sha256:8ccae5b533b7b1af07a15814ed93f2afd1d58c88615a7257ed0735cf2230d441`
and builder contain the verified binary/module graph: Go 1.27.3,
gosu v1.20.0+dirty/modifiedtrue, Moby 0.4.1 and official x/sys 0.49.0 with exact
checksums/notices. Binary SHA-256 is
`41df0e8735a2159314aeea96ad817db90792cc2cbba4df7ac25bab4e35f84ee3`.
The new-image fresh-init/graceful same-volume restart and owned cleanup pass;
all six public helper records match. This is no longer just the old-image harness
or the isolated prototype. It does not establish cross-image recovery or full
image reproducibility.

Full unfiltered provider inventory moves from four rows to two: the two
CVE-2026-39824 target metadata rows disappear after the actual module/binary
change, and unrelated complete parsed objects/ratings remain equal. The original
Windows-specific advisory and empty ratings are preserved historically; absence
in the new reports is not a Windows runtime, Linux exploit or universal platform
clearance. No scanner suppression or unrelated rating rewrite occurs.

Separate PR121 cohorts retain scope: backend 6,678 passed/1,019 skipped (7,697 collected),
PostgreSQL migration 302 passed/3 skipped, bootstrap 15, native 839, growth 6 and real browser 16;
frontend 1,322 first-attempt functional and 13 visual; runtime 47 and S3 seven groups.
They overlap and are not summed into a new unique total. Current request-ID
source is not qualified by parent-only CI. At metadata freeze, PR122 still has
11 required successes with backend/aggregate pending; ARCH-006 remains unchanged,
with its separate transport CI and dated renderer evidence intact.

## Inputs and handoff

| Evidence | Receipt SHA-256 |
|---|---|
| `/workspace/artifacts/perf015-org-read-http-2026-11-06/RECEIPT.json` | `4f62556d51a06360d0012f03ef0e9e13afb442fa8796626ce515e55dffdb4291` |
| `/tmp/bidit-db021-cancellation-20261106-5d4c7ef/attempt2/FINAL-RECEIPT.json` | `93e74de6227440a94be4cddfb29123983f32e7d6c7708d733d6d6636c8c14977` |
| `/tmp/bidit-arch010-expense-bulk-20261106/RECEIPT.json` | `d623e30f4d0d4035e497b756d08c19cc44d8e782f9d86f4e3792886057f3822d` |
| `/workspace/artifacts/sec-x-sys-investigation-2026-11-06/hosted-ci/head-9b8e464/final-verification.json` | `03f38bf6ecf2e14a5917b1103faf01514323edae90d6b693aa2c5943499dc48e` |

Read-only extraction at `/tmp/error-request-id-delivery-20261106/measurement-inputs/FACTS.json`
(SHA-256 `59655b67bf7bd7f3c49b71927b3d32423c95b8cfb05eca161ce748e8fd7fbb08`)
verifies those four receipt hashes. Linked manifests/logs and invariant results
retain their producer attribution; this extraction does not repeat database,
image or test execution. No default/index/timeout/pool/audit/provider policy
change is justified. Remaining legitimate workload, transport-fault, browser,
uncertain-commit and deployment decisions are explicit; they are not external
blockers or completed acceptance.
