# Published OCR reading facts — 5 November 2026

Mixed PDFs could select the document's native text and omit a scanned page.
Native job handlers could also publish capture state after another worker had
reclaimed the same job: the generic finalizer checked the lease after the
handler had already committed. Both defects have preserved failing controls.

This slice records the facts of accepted readings, selects text per PDF page,
and fences publication and progress with the actual job claimant. It does not
establish customer extraction accuracy or complete the OCR settings interface.

## Reading and publication

- Preserve the existing 25-character native-text threshold, now measured on
  each page. OCR only the short pages, combine selected readings in source
  order, and retain the existing page, text, raster, process and IPC budgets.
  A required-page failure cannot produce a partial invoice draft.
- Record bounded counts, hashes, geometry and page outcomes. Full raw page/word
  streams remain transient. A completed selected stream and a successful invoice
  interpretation are separate facts.
- Stamp newly admitted jobs with `builtin-local-default-v1`. Preserve that
  known request even when source integrity fails before an adapter starts.
  Legacy missing requests remain unknown. Effective adapter/settings and engine
  execution are reported only from actual observations; engine-default language,
  PSM and OEM are not invented resolved values.
- Preserve the legacy public timeout classification. New page facts distinguish
  an actual observed deadline from native exit, incomplete results and unknown
  pre-execution failures. Entering the engine timeout manager proves a spawned
  process; observing `subprocess.TimeoutExpired` proves its timeout. Temporary
  observation adapters are restored without changing arguments, descriptors,
  timeouts or process cleanup.
- Snapshot the immutable claimant at handler entry. Before publication, lock
  and freshly match job ID, tenant, running state, worker, attempt and lease
  token; then lock/refresh the source. Hold job → source ordering through the
  atomic source/history commit. Independent progress transactions use the same
  guard. Superseded workers publish neither progress nor history.

## Additive schema and read contracts

Revision163 `b9c1e3f5a7d0` follows162 `a8b0d2f4c6e9` and adds:

- `capture_reading_observations`, owned by exactly one same-tenant upload run
  or inbound invoice, with existing-owner CASCADE lifecycles and FORCE RLS;
- two nullable current-summary JSON columns and one inbound owner-pair index;
- no backfill, new generation counter, money, delivery or retention policy.

Published history is append-only through application services; no new general
database immutability policy is claimed. Facts are bounded to128KiB, current
summaries to4KiB, and history to20 summaries/96KiB per response. Cumulative
history still grows with accepted publications; no purge/count policy is added.

Existing upload, extraction-lineage and inbound detail reads gain nullable
`reading_observation`. Two owner-scoped history GETs return bounded summaries:

- `/invoices/captures/{run_id}/reading-observations`
- `/email/inbox/{inbound_id}/reading-observations`

Current-summary JSON is a hint. Reads join the canonical observation and fresh
owner, matching tenant, owner, source, upload generation, state, failure counter
and canonical digest. Malformed, oversized, stale or foreign hints return
unknown without read mutation. Timestamp/UUID ordering is not current authority.
History cursors validate exact version, timestamp and UUID types; invalid input
returns a client error. Reads perform no storage, OCR, provider or queue work.

There is no facts-detail/text endpoint or new frontend panel in this slice.
[The existing OCR plan](OCR-CONTROL-PANEL-2026-10-21.md) remains partial; exact
page/profile inspection needs a separately reviewed bounded facts-detail read
and interface. Earlier manual corrections and region read/apply flows remain
independent of the published reading facts.

The fixed `python -m app.services.capture_reading --local-inventory` CLI reports
only the local installation/configuration under the existing supervisor. It
does not claim document execution, live workers, delivery or fleet readiness.

## Verification and rollback

Source-bound artifacts are under
`/workspace/artifacts/backlog-ninth-wave-2026-11-05/`. Preserved failures include
mixed-page omission, reclaimed-job publication/progress, stale current cache,
malformed cursor, native-exit/timeout attribution and lost requested profile.

At the current local checkpoint:

- 73 focused facts/pages/publication/cache/cursor cases pass;
- 19 SQLite and6 native PostgreSQL schema controls pass, including hidden-tenant
  downgrade refusal and atomic restoration of catalog, rows and prior scope;
- 28 full-chain/generic controls pass with3 explicit SQLite instances of native
  PostgreSQL-only skips; all12 native graph controls execute. Eight historical
  starts/backend retain25 populated table snapshots, with exact new-table and
  additive-column/index projections rather than unrelated drift suppression;
- 15 native claim/cache controls pass; the broader portable OCR, capture, email,
  image and queue cohort passes658 cases. These overlap the focused cases.
- 40 native profiles pass, serially at one CPU and768MiB/1GiB, using the pinned
  Python3.14.8 runtime. Invoice/receipt cells match metadata-disabled output;
  mixed/native/scanned/blank/page-limit, installation-only inventory, timeout,
  cancellation, process-death and pre-execution failure controls pass. Observed
  cgroup peak is451,768,320 bytes, with zero OOM events. This bounded synthetic
  matrix does not establish fleet capacity, customer accuracy or VPS sizing.
- CI-scope Ruff/format checks pass975 files; Mypy passes447 app sources.

Combined-source assembly, connected readback and fresh delivered-source CI
remain pending. Prior-candidate native receipts are retained separately and
do not qualify the final source. The final matrix binds the447 app-file freeze
and is distinct from future combined-source changes. Runtime image
`addeda248664d…` is Python3.14.8; local host checks use Python3.12.14. See the
[runtime provenance erratum](RUNTIME-PROVENANCE-ERRATUM-2026-11-05.md).

Keep additive metadata for a compatible code rollback. Destructive downgrade
locks owner/history tables and refuses any history or current summary, including
hidden tenants, before mutation; it never erases evidence to permit rollback.
An older image whose Alembic graph does not contain163 must not be assumed to
start against that revision through its automatic migration entrypoint.
No production migration, merge, deployment or external configuration is performed.
