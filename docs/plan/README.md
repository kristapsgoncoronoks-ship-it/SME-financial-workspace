# Execution hub — how to run Plan A or Plan B

Current Explore delivery: [Bounded tables](EXPLORE-BOUNDED-VIEWS-2026-11-06.md) and
[native currency/file contract 2](EXPLORE-NATIVE-CURRENCY-2026-11-06.md). Money now requires a selected
native unit; version-1 money files refuse, while quantity/count and report-read
authority remain. Source-bound component/runtime proof and fresh composed
static/generated/docs checks are recorded separately; hosted CI is pending.
All parent progress including ARCH-006 remains, with one distinct currency
finding appended as 216. Complete exports, snapshots and tie ordering remain open.

Current transport availability: [One owned parser and explicit busy recovery](TRANSPORT-PARSER-AVAILABILITY-2026-11-06.md).
The Facts composition preserves all five component hashes and keeps request-owned
FX, review, audit and writes. ARCH-006 remains partial; residual postparse and
other renderer work, DB-021 and ARCH-010 remain open. Component/164 measurements
and final composition checks are kept separate; fresh hosted CI is required.

**Preceding recorded reading inspection — 6 November (source-scoped):** [Full-document facts and bounded history](OCR-FACTS-INSPECTION-2026-11-06.md)
adds read-only upload/email details with four GETs, canonical current authority
and explicit historical source/attempt labels. Requested versus observed
settings, unresolved defaults, installed packs and target DPI remain distinct
from measured execution/geometry. Manual and region edits remain separate;
no raw text, reader/profile/default or accuracy claim is added. Component42
portable/63 mocked-browser evidence stays source-bound; new composed165 proof
passes32 native inspection/cache/lease cases (20.34s), with ordinary-role/FORCE
RLS and unchanged source/business facts. Two separate native input controls
refuse malformed UUID paths safely. That composition collects 7,290 cases, not a full-suite
pass; static/frontend checks and442.8/129.9kB bundle gate pass. OpenAPI has433
paths, ERD122 tables/165 revisions unchanged. All215 ledger rows and unrelated
states remain; fresh delivered-source CI, customer corpus and human acceptance
are open. The earlier164 receipts retain their dated scope.

Current expense-money follow-up: [Historical unavailable transaction amounts](EXPENSE-TRANSACTION-MONEY-2026-11-06.md).
Expense reads, candidate search and consumption now retain unavailable amounts
safely, with typed refusals before writes and immutable raw command receipts.
Parent 165 is composed at `68cd768`; new native/static/frontend results and
retained source-bound portable/mock-browser evidence are separated in the plan.
All 51 docs/generated/CI-discovery checks pass (15.41s); fresh new-head hosted
CI remains pending. The distinct expense finding makes 215 ledger
rows while preserving all 214 complete parent rows; source remediation remains open.

Current reconciliation delivery: [Durable versioned bank-line commands](DURABLE-RECONCILIATION-COMMANDS-2026-11-06.md).
Ignore records a reason; all actions use reviewed versions and keys, with own
immutable outcomes, separate current reads and bounded history. Old write aliases
return 428. Local component and actual connected recovery proof is recorded;
composed checks pass 432 portable and 43 RLS cases, with static/generated
checks passing. These overlap the component evidence. Fresh delivered-source
hosted CI remains pending. Remaining account attribution, historical remediation and unrelated decisions stay open.

Current tenant-integrity follow-up: [Optional tenant parents — revision164](OPTIONAL-TENANT-PARENTS-164-2026-11-06.md).
All nine original DB-010 candidates are repaired through159–164; current customer,
inbound and session references retain their separate review boundaries. Component
evidence and fresh delivered-source CI limits are recorded in the plan.

Current reading foundation: [Published reading facts](OCR-READING-FACTS-2026-11-05.md).

Current bounded follow-up: [Expense report navigation](EXPENSE-REPORT-PAGINATION-2026-11-05.md),
tracked as partial list-contract progress in the
[execution inventory](BACKLOG-COMPLETION-2026-11-05.json).

Everything needed to execute either plan lives under this folder. **Decide with
`PLAN_A_vs_PLAN_B.md`, then follow ONE of the runbooks below.**

```
docs/plan/
├── README.md                    ← you are here (the map)
├── PLAN_A_vs_PLAN_B.md          ← 1-page decision sheet (OPEN — owner decides)
├── PROMPTS.md                   ← index stub (content split into the folders below)
├── shared/                      ← used by BOTH plans
│   ├── 00_MASTER_CONTEXT.md     ← Part A: prepend to EVERY work session
│   ├── WORK_ORDER_TEMPLATE.md   ← Part C: generate future work orders
│   ├── REVIEW_PROMPTS.md        ← Part D: Security/QA/DB/Perf/FinTech/UX reviews
│   ├── VAT_HARVEST.md           ← Part E: transport vertical (prepend to Epic-G/M5 work)
│   ├── DECOMMISSION_FLEET_FUEL.md ← Part F: archive+delete runbook (per repo)
│   └── specs/
│       ├── BA_bidit.md          ← platform spec: capabilities, invariants, §3 AR engine
│       └── BA_fleet_fuel.md     ← VAT/transport spec: R1–R76 + case-law rules
├── plan-a/                      ← EVOLVE Bid_it (this repo)
│   ├── ARCH_plan.md             ← architecture, gap analysis, M0–M6, risks, TODO board
│   └── wo/WO-01.md … WO-10.md   ← ten ready work orders (M0 debt sprint)
└── plan-b/                      ← GREENFIELD (new repo, both old repos deleted)
    ├── GREENFIELD_plan.md       ← stack/database/scalability decisions, M0–M6, §9 ordering
    └── wo/G-01.md … G-10.md     ← ten ready work orders (foundation + AR engine)
```

## Execute Plan A (evolve Bid_it)
1. Session recipe: paste `shared/00_MASTER_CONTEXT.md`, then ONE file from `plan-a/wo/`
   (start `WO-01.md`). One work order per session, one concern per PR.
2. Order: WO-1 → WO-2 → WO-3 …; WO-4/5/6/7/8/10 may run in parallel after WO-1
   (see each order's Depends line and `plan-a/wo/00_PART_B_INTRO.md`).
3. After each order: run the matching `shared/REVIEW_PROMPTS.md` role in a FRESH session.
4. Fleet Fuel deletion: run `shared/DECOMMISSION_FLEET_FUEL.md` once (WO-6 Step 1 first).
5. After WO-10 (M0 exit gate): continue with `plan-a/ARCH_plan.md` M1 tasks via
   `shared/WORK_ORDER_TEMPLATE.md`.

## Execute Plan B (greenfield)
1. Owner: name + create the new repository.
2. Follow `plan-b/GREENFIELD_plan.md` §9 ORDER STRICTLY: new repo → copy this whole
   `docs/plan/` tree into it → PII deny-list while old repos readable → archive BOTH
   repos (Part F, twice) → delete both.
3. Session recipe: paste `shared/00_MASTER_CONTEXT.md` + ONE file from `plan-b/wo/`
   (start `G-01.md` — it also regenerates the master context's facts appendix for the
   new repo). G-orders run strictly in sequence G-1 → G-10.
4. Reviews after each order as in Plan A. After G-10: M2 onward per
   `GREENFIELD_plan.md` §7 via the template.

## Rules that apply to both
- The 20 invariants in `shared/00_MASTER_CONTEXT.md` are non-negotiable.
- PII quarantine (`shared/VAT_HARVEST.md` E.0) binds ALL copies/archives of the old
  repos, forever.
- Never claim done without pasted command output (definition of done, Part A).
