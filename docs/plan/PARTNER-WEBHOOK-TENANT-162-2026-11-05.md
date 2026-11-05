# Partner document and webhook parent integrity — 5 November 2026

Revisions161 `f7a9c1e3b5d8` and162 `a8b0d2f4c6e9` follow160. Partner documents
and webhook deliveries now reference their mandatory parents by tenant and ID,
retaining CASCADE. Two full `(org_id,id)` unique parent indexes support these
constraints. Existing optional partner references, workflow/authority, money,
webhook idempotency, jobs and retention are unchanged. No webhook is sent by tests.

The old database admitted an own-tenant child referencing an invisible foreign
parent. Rollback-only failure-first insert/update controls prove the integrity
gap; no public parent-data exposure is claimed. New insert/reference/org updates
and parent moves refuse immediately. Valid cascades preserve the other tenant.

## Migration safety

Each migration checks global missing/wrong-tenant history with masked counts
before DDL. Ordered parent/child locks exclude writers; FORCE RLS remains enabled
under the existing empty-org maintenance context. Old FK actions/flags and target
indexes are validated. DDL and postchecks share rollback ownership. Unsupported
history or constraint variants refuse; no rows are normalized or deleted.

Dedicated SQLite migrations refuse foreign_keys=ON and never toggle it. A real
transaction and reflected child-only copies preserve unrelated names/options,
checks/defaults, partial webhook idempotency indexes, optional references and
downstream issued-invoice rows. Injected late faults prove catalogue/data rollback
in both directions. Originally unnamed target SQLite FKs return with explicit
canonical names on downgrade; this known physical distinction is recorded without
tolerating unrelated drift. Code rollback may retain the added constraints.

## Qualification

118 distinct focused cases pass:17 model,19 SQLite migration,38 native PostgreSQL
and44 existing partner/webhook workflow cases. Both strict SQLite and PostgreSQL
Alembic comparison are clean. Native controls include actual blocked-writer
observation, FORCE RLS, scoped refusal, masked preflight, drift and late rollback.

Full graph/generic integration passes28 cases plus three explicitly SQLite-only
skips. All12 PostgreSQL graph cases run; eight historical starts per backend
retain25 populated table snapshots, optional links, full schema/RLS/policies and
the original financial assertions. New index/FK deltas are projected explicitly.

The composed current159/current160/new162 native, historical159 sandbox,
bootstrap and metadata cohort passes98 cases. The historical159 correction is
inherited: its two tests use actual159 private databases and never remove an
index needed by later constraints. The proven browser fixture ordering correction
is inherited as well. Prior red runs remain evidence.

This tree has120 tables,162 revisions and6,330 backend cases. Full CI-scope
Ruff/format, Mypy444 sources and generated/docs/discovery controls are checked.
Fresh delivered-source CI remains required. Five dated mandatory links are now
covered; four dated optional links and additional current candidates remain
separate. No main/PR merge, production migration, deployment or external settings.

Evidence: `/workspace/artifacts/backlog-seventh-wave-2026-11-05/db010-next/` and
sibling `integration/`. Older sealed receipts and failed setup/test fixtures are
retained with their source and scope; counts are named suites, not release proof.
