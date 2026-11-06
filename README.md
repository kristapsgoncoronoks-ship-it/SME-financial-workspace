# InvoiceIQ — the SME financial workspace

A **multi-tenant SaaS financial workspace for SMEs and accountancy practices**:
supplier-invoice capture and approval (AP), customer invoicing (AR), payments and
settlement, expenses, analytics, exports, and organization/identity administration.
A transport vertical (EU cross-border VAT refunds, Dir. 2008/9/EC) ships as a
plug-in bounded context ([ADR-0023](./docs/architecture/adr/0023-platform-evolution-and-transport-seam.md)),
entitlement-gated and reachable end to end — statement upload through filed claim.

**Stack:** FastAPI + async SQLAlchemy 2.0 + Alembic on PostgreSQL (SQLite for
zero-setup dev/test) · React 19 + Vite + TypeScript + Tailwind SPA · Docker.

**Scale of the codebase (verified against this tree):** 122 database tables
(166 Alembic revisions, single head), 70 model modules, 136 service modules,
50 route modules, 69 SPA pages, 7731 collected backend tests, 10 CI jobs.

Direct non-production PostgreSQL startup creates eleven tenant-safe optional
links (project/issuer, expense/partner/document and inbound/customer/correction) using dialect-specific
development metadata. This convenience does not install migration-only RLS or
repair existing constraints; use Alembic for migrated environments.
[Bootstrap contract and evidence](docs/plan/DEVELOPMENT-POSTGRES-BOOTSTRAP-2026-10-28.md)
and [revision 164 scope](docs/plan/OPTIONAL-TENANT-PARENTS-164-2026-11-06.md).
[Revision 166](docs/plan/RESIDUAL-TENANT-PARENTS-166-2026-11-06.md) also enforces the mandatory
customer-contact tenant pair and records current qualification and remaining scope.

[Storage reliability](docs/plan/STORAGE-RELIABILITY-2026-11-06.md) documents the supported integrity scope, safe project-download retry response and grouped ledger reads, with dated measurement limits.

> **The specification lives in [`docs/`](./docs), not here.**
> [`docs/architecture/adr/`](./docs/architecture/adr/README.md) (30 ADRs) and
> [`docs/product/`](./docs/product) are authoritative; start with
> [`docs/architecture/overview.md`](./docs/architecture/overview.md). The
> user-facing guide is [`docs/MANUAL.md`](./docs/MANUAL.md). This README
> is only the front door.

## What it does

- **AP — supplier invoices**: multi-channel capture (upload, email-in, API) with
  deterministic-first extraction (UBL/CII XML, Factur-X/ZUGFeRD hybrid PDFs, PDF
  text layer, Tesseract OCR, CSV/JSON), an async parse queue, per-field
  provenance/confidence, a human review queue, a 14-state approval workflow with
  priority-ordered approval policies and segregation of duties, and **one**
  validation engine with explicit per-rule `block | advise` policies (ADR-0026).
- **Vendor master under dual control**: changing a vendor's IBAN/tax id is a
  **pending change request a second approver applies** (maker ≠ checker), with
  IBAN mod-97 + BIC validation at every write path and again in the SEPA builder
  (ADR-0025).
- **AR — customer invoicing**: multi-entity issuer registry with per-entity
  gap-free numbering, Art. 226 completeness gate, seller/buyer snapshots at
  issue, credit notes, cancel/void/write-off as distinct events, server-side VAT
  (4 schemes), PDF + EN 16931 CII XML, recurring schedules, dunning ladder,
  partner document gates, penalty invoicing, cash application on an append-only
  ledger.
- **Payments & settlement**: payment runs with **maker ≠ checker ≠ payer**,
  export-once-guarded SEPA pain.001 with a unique `MsgId` per generation and
  surfaced skipped payees; AP/AR settlement ledgers where **status is derived,
  never stored**; bank statement import (CSV/camt.053/MT940/PDF) and advisory
  reconciliation.
- **Expenses**: reports with standard/mileage/per-diem items, receipt capture,
  bank-statement inbox, an 11-rule policy engine, approval chains, reimbursement
  batches (CSV + SEPA).
- **Project lifecycle & profitability**: open project → versioned
  offers/estimates (client-configurable numbering) → contract (uploaded or
  generated from adjustable **document templates** — platform masters a
  workspace copies and owns; unknown placeholders stay visibly unreplaced) →
  invoicing plan tracked against actually-issued → costs from allocated
  supplier invoices (cent-exact % splits), expense links, and manual entries →
  a per-project P&L whose wire states its own basis → **close-freeze** (the
  snapshot commits with the status change; late documents surface as labelled
  adjustments). Industry-neutral by rule — industry nouns appear only in
  examples, never in schema or copy. Design:
  [`docs/design/project-profitability.md`](./docs/design/project-profitability.md).
- **Transport VAT recovery** (entitlement-gated vertical): fuel-card statement
  ingest behind one parser contract (Eurowag, E100, Q8, DKV, TFC, Moeve, BP —
  each network's money model in its own parser, and the network is detected
  from the file rather than asserted by the uploader), a nine-rule capture gate + a
  human-typed tie-out, monthly close, claim build with frozen-at-submit lines
  and VAT base, Art. 17 minimums / deadlines / document gates / adjustable
  checklist, decisions incl. partial rejection at the frozen fee rate,
  overcharge claim-backs with audited ignore, per-country supplier
  registrations, and a canonical query registry the tests forbid forking.
- **Field service & clients**: shared Board/Calendar work planning, revision-aware
  rescheduling, optional durable email notices and Google/Outlook calendar export
  (operator configuration and user consent required), plus polling ICS feeds,
  client arrival notices (per-org lead time, quiet hours), mobile
  job photos into project documents, CRM-light notes/timeline/offer kanban,
  and a magic-link client portal (offer decisions, invoices, shared docs).
- **Automation rules** (admin): trigger → conditions → ordered actions over
  work that already exists (offer gone quiet, invoice overdue, work accepted,
  visits done, customer dormant); immutable published versions with revert,
  dry-run, per-record fire-once with cooldown/every-sweep policies, a
  per-sweep throttle, and a full run log.
- **Supplier cost analytics + agreed prices**: per supplier-and-item price
  history with change detection; validity-windowed agreed price lists matched
  at capture, an Overcharges worklist with the damage priced out, and an
  optional hard block at submit.
- **Recoverability**: deletes go through a 30-day recycle bin across the
  product (invoices with a consent ceremony and purge-to-archive; expense
  reports and inbox transactions, recurring schedules and attachments in a
  generic bin); an invoice backing a filed VAT claim refuses deletion
  outright; a daily audited purge empties both bins.
- **Guided onboarding**: a derived getting-started checklist on the dashboard
  (company profile → modules → team → first customer → first invoice),
  computed from existing rows, org-wide dismissible by an admin.
- **Money correctness**: `Decimal` ROUND_HALF_UP everywhere, server-recomputed
  totals, **one FX convention** (ECB units-per-EUR, divide; `fx_source` a closed
  enum; `unknown` → NULL, never a guess; no cross-currency sums without a
  recorded conversion) — ADR-0010/0026.
- **Analytics & exports**: KPI dashboards, a self-service Explore pivot,
  supplier benchmark, budget, AR reports, FX-vs-ECB markup; CSV exports
  (formula-injection-safe), scoped generic-v2 accounting CSV previews (Xero/QuickBooks remain unconfigured pending reviewed importer mappings),
  audit export.
- **Organization & identity**: multi-org membership with org switching,
  invitations, four stored role tiers resolving to an 8-role × 20-permission
  deny-by-default matrix enforced **structurally on every router** with
  both-direction CI coverage (ADR-0024); per-request org-status enforcement with
  session revocation; OIDC SSO (PKCE S256), SCIM 2.0 Users, retention + legal
  hold, GDPR erasure, data-residency pinning, plans/entitlements/usage metering,
  and Stripe/EveryPay billing behind a provider seam (code-complete, not live).
- **Tenant isolation, three layers, tested**: per-query `org_id` filters + an ORM
  `do_orm_execute` guard over a 98-model registry + Postgres `FORCE ROW LEVEL
  SECURITY`; CI asserts RLS/model set-equality (`tests/test_rls.py`), behavioural
  isolation per table over the real query path (`tests/test_tenancy_parity.py`),
  and opaque 404s on cross-tenant ids.
- **Audit**: every mutating operation writes a hash-chained, append-only,
  per-tenant-sequenced audit event in the same transaction (ADR-0012).
- **AI policy before any AI**: with default settings the system runs end to end
  with **zero external calls** — CI-enforced with the network blocked at the
  socket layer (`tests/test_ai_policy.py`, ADR-0027).

## Quick start

```bash
# Docker (Postgres + API + SPA)
docker compose up --build      # web: :8080  api: :8000/docs
docker compose exec backend python -m app.seed   # demo login: demo@invoiceiq.app / demo1234

# Local, no Docker (backend defaults to SQLite)
make install                   # backend venv + frontend deps
cd backend && . .venv/bin/activate && python -m app.seed && uvicorn app.main:app --reload
cd frontend && npm run dev     # http://localhost:5173
```

## Commands (from the Makefile)

The Compose development stack uses a pinned RustFS S3 candidate with a separate
volume. It refuses any data/metadata in the original MinIO volume. Use a fresh
development database; an empty mounted legacy volume does not verify an existing
database's source store. The bundled production overlay explicitly refuses
startup pending [provider/cutover review](docs/security/CI-STORAGE-REPLACEMENT-2026-10-19.md).
The independent Hostinger local-storage configuration is unchanged.

```bash
make test         # backend: python -m pytest -q
make lint         # ruff check + ruff format --check
make fmt          # ruff auto-fix + format
make typecheck    # mypy app/core   (CI runs the stricter `mypy app`)
make check        # lint + typecheck + test
make migrate      # alembic upgrade head
make migration m="add x"       # autogenerate a revision (then READ + EDIT it)
make openapi      # regenerate docs/api/openapi.json (CI fails if it drifts)
make build        # frontend: tsc --noEmit && vite build
make up / down / logs          # docker compose
```

Production images install the runtime dependency graph. The existing combined
requirements entry point still supports local/CI pytest; the
[disposable in-container smoke](docs/development-runtime-smoke.md) runs test tools
offline over the unchanged installed runtime packages.

**CI is authoritative and stricter than the Makefile shortcuts.** The 10 jobs in
[.github/workflows/ci.yml](./.github/workflows/ci.yml): `pii-scan` (WO-6
quarantine gate), `lint`, `backend` (SQLite suite), `postgres` (real-Postgres RLS
+ concurrency, `NOSUPERUSER` role), `frontend` (typecheck + build), `frontend-e2e`
(Playwright design-system smoke **and, since WO-Y, the visual-regression
snapshots** — whose baselines are produced by, and belong to, that container:
`npm run test:vr` on a dev machine is expected to fail, because font rendering
moves 12,000–19,000 pixels per snapshot between environments), `vr-baselines` (dispatch-only: regenerates those snapshots in the
same container that checks them, publishes them to the working branch it was
dispatched from, and REFUSES to run on the default branch — a job that could
rewrite a gate's own reference on `main` would let anyone bless a regression
by re-running a workflow), `docker-build`, `ci-required` (PR-only completion check:
all seven mandatory CI jobs must explicitly succeed), `deploy`.
The separate supply-chain workflow keeps its four security checks and adds
`supply-chain-required`, which also requires every child to succeed on PRs.
These completion jobs have no checkout, external action or token permissions.
They do not activate branch-protection settings or change production deployment.
Reproduce locally:

```bash
cd backend && . .venv/bin/activate
ruff check app tests && ruff format --check app tests
mypy app
test "$(alembic heads | wc -l)" -eq 1 && alembic upgrade head && alembic check
python -m pytest -q
cd ../frontend && npm run build
```

## Engineering rules (the short version)

- Layering `models → core → services → api` is machine-enforced
  (`tests/test_boundaries.py`). **All business logic in services**; routes are
  thin controllers.
- Every route declares its permission structurally or sits on the reasoned
  `PUBLIC_ROUTES` allow-list — CI asserts coverage in both directions
  (`tests/test_authz_coverage.py`).
- New tenant-scoped tables ship their RLS policy **in the same migration**, join
  `TENANT_MODELS`, and are automatically in scope for the tenancy-parity suite.
- Errors are `{"detail", "code"}` + `X-Request-ID`; services raise `AppError`,
  never `HTTPException`.
- No float in money paths. No unaudited financial mutation. No skipped tests.

Full rules: [`docs/architecture/engineering-rules.md`](./docs/architecture/engineering-rules.md).
Deployment (TLS, Cloudflare, Hostinger): [`docs/DEPLOYMENT.md`](./docs/DEPLOYMENT.md),
[`docs/DEPLOY-TLS.md`](./docs/DEPLOY-TLS.md). Milestone gate: [`docs/M0-exit-gate.md`](./docs/M0-exit-gate.md).

### Performance

The read paths are measured, not assumed. `make perf PERF_URL=<postgres-url>`
drives the real app against a dataset it scales itself; `make perf-shape`
quadruples that dataset and fails any endpoint that slowed by more than its
declared ceiling — a **growth ratio**, so the verdict means the same thing on a
laptop and on a CI runner. That is why it can run in CI at all: the `postgres`
job executes it on every push (~17s). The recorded baseline, the machine it came from, and
what is deliberately not covered (concurrency, write paths, cold caches) are in
[`docs/perf/BASELINE-2026-08-27.md`](./docs/perf/BASELINE-2026-08-27.md).

## License

GNU General Public License v3.0 or later — see [LICENSE](./LICENSE).
(SPDX: `GPL-3.0-or-later`, declared in `backend/pyproject.toml` and
`frontend/package.json`.)
