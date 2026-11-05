# Expense report child integrity — 5 November 2026

Revision160 `e6f8a0b2c4d7` follows159 `d5e7f9a1b3c6`. Expense comments and
approval steps now reference reports using `(org_id, report_id)`. Both parents
remain mandatory with CASCADE. The supporting report-pair unique index from159
is validated and reused without parent/index DDL. No optional link changes.

The old metadata admitted cross-tenant child insert/update references. Four
failure-first controls establish the gap. Corrected model and actual PostgreSQL
controls refuse those references, preserve valid cascades and keep other tenants
isolated. Existing approval/review authority and financial commands are unchanged.

## Migration and rollback

Both child histories are globally checked with masked missing/wrong-tenant counts
before any DDL. PostgreSQL excludes writes in report/comment/step order under
the existing unscoped maintenance context; FORCE RLS and all policies remain.
Both original FK contracts are validated before replacement. The two changes
and postvalidation share one savepoint. Injected second-replacement/postcheck
faults prove exact data/catalog rollback in both directions.

SQLite refuses foreign-key-enabled migration connections and never toggles that
setting. It owns an actual transaction and names only the reflected target FK
for batch replacement. Rows, optional references, unrelated organization FK
names/options and parent/item/transaction schemas remain intact. No history is
repaired or deleted automatically.

Downgrade restores both report-only CASCADE relationships. Originally unnamed
SQLite report FKs return with explicit canonical names; logical rollback is
verified, not physically identical unnamed DDL. The previously recorded159
report-FK name distinction remains. These facts are recorded in
`TEST-PHYSICAL-NAMES.json`, without tolerating unrelated drift.

## Qualification

Focused controls pass122 distinct cases with zero skips:26 portable/model/SQLite
migration,20 native PostgreSQL,52 existing portable expense regressions and24
native authority/review cases. Strict Alembic comparison reports no operations;
SQLAlchemy warning-as-error controls remain enabled in the named cohorts.

Final integration passes83 graph/parity/bootstrap checks:10
SQLite/static,12 PostgreSQL graph,32 portable parity and29 native parity/bootstrap,
with zero skips/errors/failures and unchanged before/after backend Python/config
hashes. All unrelated schema attributes, row/link snapshots and tenant policies
remain asserted. The inherited WO8 fixture repair supplies its missing synthetic
employee without changing financial or rollback assertions. Earlier failed
fixture and snapshot-scope runs remain preserved.

The tree has160 revisions/120 tables and collects6,256 backend cases. Full
CI-scope Ruff/format and Mypy444 sources pass; native-test discovery is updated.
Fresh delivered-source CI remains required. The three repaired dated links do
not close the six separately assessed candidates, including optional SET NULL
links that must retain non-null tenant IDs.

No main/PR merge, historical cleanup, production migration, deployment or
external setting change. Focused evidence:
`/workspace/artifacts/backlog-followups-2026-11-05/db010-children160/`;
integration evidence: sibling `db010-graph-qualification/` and
`/workspace/artifacts/backlog-fifth-wave-2026-11-05/`.

The first hosted head9a47efa exposed two historical159 fixture failures against
valid160 dependencies. [The isolated159 correction](HISTORICAL-159-TEST-ISOLATION-2026-11-05.md)
retains all integrity assertions and passes31 native/40 discovery controls.
Fresh repaired-head CI remains required.
