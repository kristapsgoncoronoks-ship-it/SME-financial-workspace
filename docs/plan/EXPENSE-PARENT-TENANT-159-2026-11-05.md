# Expense item parent integrity — 5 November 2026

Revision159 `d5e7f9a1b3c6`, following `c3f5a7b9d1e4`, replaces the expense
item's report-only foreign key with `(org_id, report_id)` referencing
`expense_reports(org_id, id)`. The parent stays mandatory and retains CASCADE.
A unique report-pair index supports this constraint. Optional transaction links,
financial amounts, workflow authority, audit and retention policy are unchanged.

The old model admitted a wrong-tenant report reference. Failure-first controls
establish that defect; the correction prevents explicit insert/update violations
while preserving valid report/item behavior. This is one of the dated DB-010
links, rather than a blanket completion of the schema review.

## Migration and rollback

Global masked preflight counts refuse missing or wrong-tenant historical
parents before DDL. PostgreSQL uses ordered write exclusion in the existing
unscoped maintenance context while retaining FORCE RLS. Replacement and
postvalidation share a savepoint. Dedicated SQLite migration connections must
have foreign-key enforcement off before entry; the migration never toggles that
setting and owns a real transaction. Existing violations refuse migration.
No history is repaired or deleted automatically.

Populated migration and injected-fault controls preserve rows, money, optional
links, unrelated schema and RLS policy. Downgrade restores the single-column
CASCADE relationship. On SQLite the original unnamed report foreign key returns
with canonical name `expense_items_report_id_fkey`; logical rollback is verified,
but physically identical unnamed DDL is not claimed. This distinction is recorded
in the sibling review's `TEST-PHYSICAL-NAMES.json` and is not a general drift waiver.

## Qualification and limits

The focused source passed77 distinct controls:14 portable/model/migration,
11 native PostgreSQL and52 existing expense compatibility checks. Native checks
treat SQLAlchemy warnings as errors; strict Alembic comparison reports no new
upgrade operations. All tests use isolated synthetic data. The full THIRD parent
has now been integrated locally.83 applicable graph/parity/bootstrap checks pass;
74 ran after the final test freeze and nine SQLite/static checks used an
AST-equivalent formatting predecessor. One existing SQLite-only RLS variant is
explicitly skipped by the native selector; no actual native case skipped.
Ruff/format in the full CI scope, Mypy444 sources and51 generated/discovery/docs
contracts pass. Fresh delivered-source CI remains required. The
integrated tree collects6,210 backend cases and has159 revisions/120 tables.

The two sibling comment/approval-step links in revision160 have their own
isolated122-check receipt and remain a separate branch. Six dated candidates
remain, including optional SET NULL relationships that must preserve the
non-null tenant identifier. No historical cleanup, main/PR merge, production
migration, deployment or external settings change is authorized by this repair.

Focused evidence:
`/workspace/artifacts/backlog-followups-2026-11-05/db010-implementation/`.
Integration evidence:
`/workspace/artifacts/backlog-fourth-wave-2026-11-05/`.
