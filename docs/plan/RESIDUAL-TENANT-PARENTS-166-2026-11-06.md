# Residual tenant parents — revision 166

Four remaining DB-010 references now enforce organization membership at the
database boundary. Actual revision-165 characterization permitted foreign
references and cross-organization delete effects in 36 direct SQL schedules.
This establishes missing database defense, not admission through normal HTTP
handlers or production prevalence. Existing application admission remains.

Component `207acc0f011a0fc714b3b394dbe1f17f3cfd4eed`, graph/CI integration
`3dea2c0f685b28f3eed5f2a4294bd0a72b30ece4` and the separate inherited-fixture
correction `3745d84` compose with accepted Explore parent
`ae3925e1a98cd9d05c7c79ea8f29b156433eea1a` at `67f8f30`.
Fresh hosted CI is pending; no production migration or deployment is claimed.

| Reference | Revision-166 behavior |
| --- | --- |
| customer_contacts.customer_id | Mandatory organization/customer pair; original CASCADE retained |
| inbound_invoices.invoice_id | Optional organization/invoice pair; original single-column SET NULL retained |
| issued_invoices.customer_id | Optional organization/customer pair; original single-column SET NULL retained |
| issued_invoices.corrected_invoice_id | Optional organization/original-invoice pair; original single-column SET NULL retained |

The immediate optional pair is NO ACTION in Base/SQLite and PostgreSQL
column-specific SET NULL on the optional ID. Both PostgreSQL action creation
orders retain organization IDs and all other source, financial and legal fields.
The original optional singles and their physical names remain installed. The
mandatory SQLite downgrade restores its original unnamed single-column FK.
Existing named UNIQUE constraints on customers, received invoices and issued
invoices are reused: no parent/index DDL, new financial bounds, source repair,
inferred history, deferred invalid-row window or trigger is introduced.
Same-organization self-reference/cycle behavior and permitted deletes remain.

## Migration and schema comparison

Revision `e2f4a6b8d0c3` follows real 165 `d1e3f5a7c9b2`. Its frozen checks use
ordered write exclusion and masked global missing/wrong-company counts, with
all four replacements and postvalidation under one savepoint. A nonempty
tenant-scoped PostgreSQL connection refuses before locks or DDL; its exact GUC
is retained. The explicit unscoped maintenance path preserves None/empty GUCs
on success or failure. FORCE RLS stays installed; no RLS or scope bypass occurs.

SQLite refuses FK-on rebuild entry before DDL and never toggles enforcement.
An owned real transaction and complete reflection preserve exact physical
names, incoming and self references, all rows, checks, indexes and defaults.
Whole foreign-key checks run before and after. Populated downgrade/round-trip
and late-fourth-replacement/postvalidation failures preserve source rows,
published reading facts/summaries and downstream issued relationships.

The strict metadata helper now explicitly represents the three named UNIQUE
targets, checking ordered resolved columns, nullability, exact names/options
and native backing-index validity/readiness/immediacy. The prior eight special
deletion policies retain their exact semantics. Only the checked PostgreSQL
clone receives dialect actions and known legacy names; source metadata is
unchanged. Generic Alembic comparison still reports unrelated drift.

An actual NOT VALID mandatory contact FK escaped ordinary reflection with no
Alembic diff. The narrow source/native contact check now validates its precise
columns, target, actions, validation and deferral flags, including qualified
metadata. This is not a blanket validation claim for unrelated legacy FKs.
[Reflection limitation](/workspace/artifacts/db010-residual-link-review-2026-11-06/REFLECTION-LIMITATION.md).

## Qualification and historical boundaries

The [component receipt](/workspace/artifacts/db010-residual-link-review-2026-11-06/IMPLEMENTATION-RECEIPT.json)
records 497 distinct passing nodes across separately executed cohorts:
67 portable model/migration, 239 metadata, 67 then 13 then eight new native,
96 incumbent native parity/catalog, five unchanged HTTP and two parity cases.
The initial native 67 bind unchanged model/migration sources and the earlier
helper; later comparator controls qualify the final helper. These are dated
source bindings, not one 497-case run or an enlarged total after repeats.

The graph retains all 25 nodes and eight starting-head schedules plus all six
companion migration cases: 28 pass and three explicit SQLite instances of
PostgreSQL-only contracts skip. All 12 native graph paths execute. The exact
four-FK/UQ projection validates 166 before invoking unchanged 165 and earlier
projections; unknown FK attributes and unrelated drift remain visible. All
original data/schema/RLS assertions remain, with the approved extra customers,
contacts, vendors, received/inbound and same-company credit-note fixtures.
[Graph context and receipts](/workspace/artifacts/db010-residual-link-review-2026-11-06/graph-ci/COMMIT-AND-QUALIFICATION.json).

Pinned 159/162/164/165 migration fixtures remain actual historical databases,
markers and catalogs. Current DML/global comparison follows current head.
One incumbent issued-partner test now validly detaches only its synthetic
credit-note references inside its existing rollback savepoint, isolating the
older guard without disabling any constraint. All prior assertion ASTs and
34 native cases pass; full row/catalog rollback checks remain.
[Fixture receipt](/workspace/artifacts/db010-residual-link-review-2026-11-06/graph-ci/OLD-OPTIONAL-FIXTURE-RECEIPT.json).

After composition, 30 native parity/bootstrap checks pass in 52.61s, including
the accepted parent namespace repair. Owned head166 upgrade/check succeeds
under the non-superuser, non-bypass application role. All 14 component file
hashes, graph source and graph-consumed models/core/migrations/test helpers are
unchanged after merge, so their qualified cohorts are retained without broad
repeats. The two new native modules are registered in the existing admin-enabled
UUID-database CI stage; administration is only for owned database create/drop.

Fresh full Ruff/format checks 1,019 files and Mypy checks 452 application files.
The tree collects 7,553 backend cases, with 122 tables, 166 revisions, 70 model
modules, 136 services, 50 route modules, 69 pages and 10 CI jobs. Collection is
not a full-suite execution. Regenerated OpenAPI remains 433 paths and the ERD
is parent-identical. All 51 docs/generated/CI-discovery controls pass.
All 327 tracked frontend files are byte-identical to Explore parent: its lint,
build and 442.9/129.9 kB raw/gzip bundle proof within 460/135 kB are reused.

All 216 ledger identities and 215 unrelated complete rows remain exact; only
DB-010 gains this bounded progress. No finding is appended. Earlier evidence,
including Explore and ARCH-006, retains its original scope. DB-010 stays partial:
calendar OAuth/mutable Session/global User/membership semantics and intentional
cross-organization subscription relationships require their separate contracts.
No naive user/session organization pair, customer-data remediation, broad
relationship-completeness or full-backlog completion claim follows.

[Final composition receipt](/workspace/artifacts/db010-residual-link-review-2026-11-06/final-composition/FINAL-RECEIPT.json).
