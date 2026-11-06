# Optional tenant parents — revision 164

Four optional relationships could point at a parent in another company. Their
existing single-column foreign keys checked parent existence, but not tenant
ownership. Revision 164 `c0d2e4f6a8b1` adds the tenant guard while retaining each
existing optional-reference detach behavior. The delivery composes optional product
`1e17168`, fullgraph164 `cbee`, corrected PR111 `df2c344` and dated bank
qualification `5550c5f` at integration source `cecb2fd`.

| Optional reference | Parent |
| --- | --- |
| expense_transactions.item_id | expense_items |
| issued_invoices.partner_id | partners |
| recurring_invoices.partner_id | partners |
| vendor_change_requests.source_document_id | documents |

The old single-column SET NULL constraints remain. New immediate composite
guards use PostgreSQL's column-specific SET NULL on the optional ID; SQLite
uses NO ACTION alongside its existing single-column action. Both native action
orders preserve the non-null organization ID and clear only the permitted link.
No deferred enforcement window, trigger, financial value or legal snapshot is
introduced or changed. Two full pair indexes support items/documents; the
partner pair index from 161 is reused.

## Schema comparison, migration and history

Original portable metadata is validated before cloning, including resolved
column identities, ordered targets, nullability and constraint/element options.
Native comparison validates exact installed FK actions, names, target columns,
set-column attribute numbers, validation/deferral flags and full immediate
indexes. Only the checked PostgreSQL clone receives the intended dialect action
and the one established issued-partner physical name. Schema-qualified metadata
is supported. No Alembic object filter or unrelated drift suppression is added;
the four older project/issuer column-specific policies remain unchanged.

Global masked preflight rejects missing/wrong-company history before DDL.
Ordered locks, FORCE RLS and atomic rollback preserve source data and unrelated
schema. SQLite uses a real owned transaction, refuses FK-on rebuilds and never
toggles enforcement. Reflected copies preserve original physical constraints,
checks, defaults, partial indexes and incoming issued-invoice relationships.
Published 163 reading history/current summaries survive the 164 round-trip.

Two inherited test groups require their actual historical schema. The previous
159 isolation fix remains intact. The 20 direct 161/162 migration cases now use
fresh UUID databases pinned at 162, while 18 current partner/webhook DML/RLS/
cascade/lock cases remain on the current head. All 38 case bodies and decorators
are preserved, with only the explicit historical-fixture separation. The
[AST inventory](/workspace/artifacts/backlog-eleventh-wave-2026-11-05/optional-parent/native-actual/HISTORICAL162-AST-INVENTORY.json)
records the five historical functions/20 cases and four current functions/18
cases; there are no test filters or assertion waivers. The 159 org-update test
isolates the newly enforced incoming transaction FK only inside its failing savepoint, retaining its exact
old constraint assertion and full before/after restoration check.

## Qualified component evidence

The [source case inventory](/workspace/artifacts/backlog-eleventh-wave-2026-11-05/optional-parent/QUALIFIED-CASE-INVENTORY.json)
records 637 distinct applicable passing checks before final delivery assembly
across the documented portable, native catalogue/bootstrap, current/historical DML, workflow and full
graph cohorts. The full graph retains 28 passes/3 explicit SQLite instances of
PostgreSQL-only skips; all 12 native graph cases execute. Inapplicable portable
native-only coverage was separately executed in the 32-case native cohort.
No skip or constraint assertion was weakened. The
[fullgraph164 receipt](/workspace/artifacts/backlog-eleventh-wave-2026-11-06/fullgraph164/FINAL-RECEIPT.json)
records its exact source, unchanged 163 reading fixtures and nine product hashes.
These are component/source receipts, not an added count of repeated runs.

After composition, the parent-qualified ordinary-RLS and four Bank native
modules pass 37 cases (24.47s). This cohort overlaps component evidence and is not
added to 637 as distinct coverage. The assembled source has 121 tables,
164 revisions, 134 service modules, 69 models, 50 backend route modules, 69 pages and 10 CI
jobs; 7,094 backend cases collect. Full Ruff/format checks 993 files and Mypy
checks 448 app files. All 51 docs/generated/CI-discovery controls pass in
12.55s with zero failures, errors or skips. Collection is not a full-suite pass.

All 21 owner-commit hashes match after composition and 1,450 source/test/config/
generated inputs are frozen at `cecb2fd`. Generated OpenAPI and ERD match Bank555
byte-for-byte. The model changes are the reviewed tenant constraints, rather
than new model/table/API inventories. Delivery metadata receipts are under
`/workspace/artifacts/backlog-eleventh-wave-2026-11-06/delivery-metadata/`.

CI registration preserves the ordinary/admin split. The admin migration lane adds
only `test_partner_webhook_parent_migration_pg.py`,
`test_optional_parent_migration_pg.py` and `test_optional_parent_tenant_pg.py`;
the ordinary PostgreSQL lane adds only `test_optional_parent_metadata_pg.py`.
Portable `test_optional_parent_migration.py` stays in the full backend suite
without duplication. Existing paths and filters are preserved. These validated
registrations do not establish a hosted CI pass.

The corrected 163 tenant-registry declaration is included from PR111. Its sole
constant change leaves all other migration AST/DDL identical; the actual RLS
policy and strict coverage guard are both retained. Fresh delivered-source CI
remains required; parent green results do not qualify this source automatically.

## Remaining scope

All nine original dated tenant-parent candidates are repaired through 159–164.
The [source-only residual inventory](/workspace/artifacts/backlog-eleventh-wave-2026-11-05/optional-parent/MODEL-PRESERVATION-AND-RESIDUALS.json)
separately identifies five current links:

- `calendar_oauth_attempts.session_id`
- `customer_contacts.customer_id`
- `inbound_invoices.invoice_id`
- `issued_invoices.corrected_invoice_id`
- `issued_invoices.customer_id`

These are review candidates from model metadata, not newly qualified native
vulnerabilities or repairs. `Session.user_id` also requires its own session and
membership contract: users are global and the active session organization can
change, so a naive tenant-pair constraint is not an established solution.
Historical Bank555 evidence remains source-bound; neither parent green results
nor the 637 component cases qualify every new composition automatically.

The execution ledger preserves all 214 existing IDs, titles, source references,
original inventory statuses and order; only DB-010 progress is refreshed. No
organization zeroing/nulling, history rewrite, production migration, main/PR
merge, deployment, provider action or external setting change is performed.
