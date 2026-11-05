# Finance and worker follow-ups — 5 November 2026

This reviewable wave follows draft PR #105. Main remains unchanged; no merge,
deployment, external setting change or production migration is part of this work.
The parent wave retains a failed mobile supplier-invoice visual check, so neither
that wave nor these additional local checks constitutes release clearance.

## Changes and evidence

| Finding | Result | Scoped local qualification |
| --- | --- | --- |
| Fresh expense authority | Decision and reassignment use the locked membership's designated approver flag and permissions, before report locking. Existing role policy is preserved. | 27 native PostgreSQL and 85 portable cases. The original real team-API revocation failure is retained. |
| Overlapping AP selection | Invoice IDs are unique, locked in a deterministic order and re-read before a new run can claim them. | Create/settlement combined union: 80 distinct cases. Reproduced two successful conflicting creations and orphaned run before repair. |
| AP settlement conservation | Manual and payment-run settlements serialize through fresh ordered invoice locks; existing cumulative/correction semantics remain. | Included in the 80-case union. Original ledger/cache divergence failures retained. |
| Late job generation | A nullable per-claim random UUID fences heartbeat, reclaim and terminal writes. The original winning claim is captured before commit and survives handler rollback. Losing finalization rolls back pending handler facts. | 81 native, 89 portable and five migration cases (175 combined). Previously committed handler effects still rely on domain idempotency. |
| Stale manual job retry | Compare the observed status, attempt and random lease before flushing; an outdated retry receives the existing conflict response. | 19 native controls, included in the 175-case union. Attempt/token/no-autoflush omission controls fail as intended. |
| Delivery source truth | Fresh locked message and current invoice eligibility; verified attachment hash/admitted bound and fresh post-read metadata. | 35 portable and eight native cases; real void-during-read and stale delivery-status failures retained. No actual SMTP sent. |
| Claim evidence ZIP | Stage one verified source at a time into a private owned file. Revalidate current VAT permission, module, frozen claim and source metadata before finite streaming; clean up on cancellation/error. | 66 portable and four native cases. Byte builder retained only as a cleanup-owning service compatibility adapter. No read audit or financial write. |
| Issued invoice pagination | Navigate the existing bounded API population with exact totals, stable ordering and tenant/user-scoped query identity. ZIP filters remain independent. | 47 backend, 41 browser and one real 105-invoice browser/API/PostgreSQL journey. |
| Rebate preview | Shared Decimal planner provides a read-only whole-scope summary and bounded allocation pages. Close independently re-plans current records. | 249 distinct cases: 127 portable, 11 native, 110 browser and one connected 105-row no-write journey. |

Counts above describe each named suite; overlapping suites are not added into a
claimed application-wide total. First failures, synthetic fixture setup and source
hashes are retained with their exact qualification receipts under
`/workspace/artifacts/backlog-followups-2026-11-05/`.

The connected rebate fixture first received the real commercial-module refusal;
only its synthetic organization's declared module prerequisite was enabled.
It covers two entities/two countries, exact 100+5 paging and a 1.05 EUR allocation.
All thirteen scoped database tables match the browser baseline after preview,
paging, reload and real refusal. A separate intervening login creates its normal
authentication audit event; that event is not hidden as a no-write preview claim.

Preview limits are 20,000 fuel rows, 5,000 rebate documents, 20,000 historical
source groups and 20,000 active supplier/country pairs. A cap-plus-one overflow
refuses the whole preview; it never presents a partial financial total. Four
bounded financial SELECTs avoid per-group round trips. These interactive limits
do not truncate monthly close. Existing cross-entity line-sequence ties now use
the stable transaction ID as the secondary key, which can determine the cent
recipient for a previously undefined tie. No historical totals are backfilled;
locked claims, approved fee snapshots and historical rows are tested unchanged.

## Migration and rollout boundary

The single new head `b2e4f6a8c0d3` follows `8d1f3a5c7e90` (157 revisions,
120 tables). It adds only nullable `jobs.lease_token`; historical rows remain
NULL. Existing jobs, foreign keys and tenant/RLS policies remain intact.

Before relying on UUID fencing, drain old worker binaries: they cannot obey a
column their code does not know. For code rollback, keep the additive column.
The destructive schema downgrade refuses any populated token and checks all
tenants under write exclusion. Disposable SQLite/PostgreSQL upgrade, preservation,
rollback and interruption controls pass; no production downgrade was attempted.

Broad composition checks explicitly preserve the historical planning head and
compare the exact later chain. They remove only the expected planning table/policy
and nullable lease column/NULL value for the historical comparison. Unrelated
columns, indexes, foreign keys, RLS settings, roles and rows remain strict. Real
extra-column and changed-policy-role controls are rejected. Twenty-four checks
pass across the scoped composition runs, with expected native/SQLite-only skips;
these overlap nine historical/lease cases above.

## Remaining work and qualification

Generated OpenAPI/ERD, explicit native CI discovery and README counts are updated.
The integrated tree collects 6,099 backend cases. Fresh delivered-source lint,
types, SQLite migrations/parity, PostgreSQL, functional/visual browsers, bundles,
all ten image targets/twenty scan reports and supply-chain checks are still
required. Previous green source cannot qualify this revision. No security waiver,
assertion relaxation, extra retry, timeout increase or visual baseline update is
introduced.

The isolated next wave is addressing source-backed large-image OCR exhaustion;
shared temporary-space/inode/fleet limits, PDF aggregate memory, missing private
invoice corpus, external provider credentials and owner policy/retention decisions
remain open. The unavailable original 83-action report is not reconstructed as
an invented completion claim.
