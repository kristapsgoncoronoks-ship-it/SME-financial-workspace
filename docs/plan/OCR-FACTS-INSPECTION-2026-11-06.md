# Inspect recorded full-document reading facts — 6 November 2026

The existing capture summary did not expose the persisted per-page and profile
facts. Upload review and email detail now offer collapsed **Reading details**:
current observed facts, bounded history and one explicitly historical detail.
The panel reads existing evidence; opening it never queues a reread, changes
saved values or selects a reader/profile. Manual corrections and selected-region
read/apply remain separate workflows and are not new full-document engine reads.

## Source and contract

Component `0c5c07d` follows optional-parent164 `845420e`. Registration-only
`5dbb7bd` adds the native module and browser spec to ordinary cohorts once.
Local composition `f35dc59` merges corrected Expense115 parent `8719dead`,
including reconciliation165 and bootstrap repair `1998377`. Eleven component
files are byte-identical. Shared `types.ts` differs only by the parent's expense
amount/status and `BankLine.version`; removing those two parent hunks reproduces
all component bytes. No source-only166 sibling is included.

Four additive GETs reuse `ReadingObservationFacts` (128KiB overall):

- `/invoices/captures/{run_id}/reading-observations/current`
- `/invoices/captures/{run_id}/reading-observations/{observation_id}`
- `/email/inbox/{inbound_id}/reading-observations/current`
- `/email/inbox/{inbound_id}/reading-observations/{observation_id}`

The existing two owner-history GETs retain 20 summaries/96KiB and their keyset
cursor. Invoice reads inherit `INVOICE_READ`; inbound reads retain that permission
and the `email_intake` module guard. All queries bind organization and owner.
Missing/foreign and malformed UUID identities refuse opaque404; invalid cursors
keep their existing client errors. No new model, migration, policy or table.
OpenAPI grows from429 to433 paths; the122-table ERD is parent-byte-identical.

Current facts use the same canonical resolver as existing summaries. It checks
the fresh owner state, source digest, upload attempt, failure generation, owner
association and cached canonical summary/digest in one joined SELECT, batching
at most20 eligible owners. No second facts fetch, timestamp or UUID-based newest
inference is used. Historical detail is bound to the requested owner and is
labelled with source/attempt; it never substitutes for an unavailable current
observation. A recording timestamp is not a measured transaction commit instant.

## Read bounds and unavailable records

New facts GETs and the existing facts-history owner checks select scoped owner
metadata/existence instead of loading source draft/preparation blobs. Current
metadata includes status/source/attempt/failure plus a summary hint bounded by
the existing4096-byte allowance. The fresh joined owner hint uses the same SQL
CASE bound. Observation profile/page JSON columns are each projected only within
the existing128KiB raw UTF8 allowance. PostgreSQL uses `octet_length`; SQLite
uses BLOB length. The decoded canonical profile keeps its existing16KiB limit,
and the combined facts DTO keeps its128KiB limit. A valid whitespace-formatted
raw profile above16KiB is still accepted within its prior128KiB allowance.

These are per-row/projection/response limits, not a cumulative history retention
or whole-application memory guarantee. Broader pre-existing upload/extraction
and inbound detail getters still have their inherited full owner-row scope;
this slice does not change their draft/preparation loading. History grows under
the existing publication lifecycle; no purge or retention policy is introduced.

True SQL NULL remains legitimate legacy unknown. Empty, malformed, oversized or
otherwise unverified current facts produce null. An existing unreadable
historical record returns typed409 `reading_observation_unavailable`; it is not
silently omitted or repaired. Oversized values are refused before JSON parsing.
A current oversized hint cannot hide legitimate historical detail/history.

## Interface behavior and truthful fields

Closed panels issue no facts reads. Opening loads current facts and one history
page; selecting a historical entry loads that entry only. Disposable read caches,
actor/organization/owner/revision keys, expected-identity headers and aborts fence
context changes. Empty, pending, failed and read-only states have explicit text;
refresh/recovery are GET-only. No retry policy, timeout, shared styles, screenshot
baseline, dependency or existing reread/apply contract changes.

Per-page native/OCR/failed/timed-out/unknown/not-attempted outcomes, counts,
hashes and actual raster dimensions remain observed facts. Requested and observed
profile/engine are distinguished. Installed language packs never imply selected
language; engine defaults remain unresolved unless recorded. Target DPI is
separate from actual raster size/scale. Counts and a completed reading do not
measure recognition accuracy. Raw extracted text/words are neither added to these
facts nor rendered. There is no new reader, provider, model, configurable profile,
default language/PSM/OEM or automatic reread.

Local upload header/badge and email detail grid/address wrapping retain all
fields/actions and fit390px at32px root text. Before opening the new panel, those
existing surfaces measured596px and951px; scoped fixes reduce both to390px.
Impeccable existing-interface hardening guided the batched desktop/mobile review;
no visual baseline update was used.

## Qualification and remaining acceptance

The immutable component receipt remains
`/workspace/artifacts/backlog-ocr-facts-inspection-2026-11-06/TEST-RECEIPT.json`
(SHA `0b2e10094e09fd6f9af0b8590a577f83c1bd6506989de90a2ac868c1405be226`).
It records42 portable cases (13 new/29 incumbent),63 mocked-browser cases
(17 new/46 incumbent), and32 native cases against164. The portable/browser
sources are unchanged except the explicit unrelated parent type projection, so
those closed cohorts are reused rather than rerun or counted as new165 proof.

Composed-source native qualification upgrades the dedicated owned facts database
164→165 as the ordinary non-superuser/non-bypass role, verifies actual head
`d1e3f5a7c9b2` and FORCE RLS, and passes32 inspection/cache/lease cases in20.34s
with zero failures/errors/skips. Stored synthetic source facts and all scoped
business-table hashes, including a nonempty12.34 receipt, remain unchanged by
reads. Two separate native input-review cases cover malformed owner/observation
paths on both channels, returning404 without business writes. These are API/DB
controls, not an actual customer document/recognizer/browser connected journey.

Preserved failures include empty-string JSON being confused with NULL and an
isolated replay of the exact earlier resolver function transferring a4097-byte
fresh hint after a separate committed replacement. Final native controls prove
bounded projection/no parse/no repair. The replay is explicitly function-scoped,
not a full old-head runtime claim. Independent review receipts remain under
`/workspace/artifacts/ocr-facts-inspection-2026-11-06/` and retain their164 scope.

The composed tree collects7290 backend cases (not a full-suite pass), with
122 tables/165 revisions/70 model modules/136 service modules/50 route modules/
69 pages/10 CI jobs. Full Ruff/format checks1009 files; Mypy checks452 app files.
Frontend lint/build and the460/135kB bundle gate pass at442.8kB raw/129.9kB gzip
across4 assets. Generated/docs/CI-discovery controls and final delivery review
are recorded in the delivery receipt. Fresh delivered-source hosted CI and human
review remain required; prior candidate CI does not qualify this local composition.

All215 ledger identities, order, provenance and unrelated states are preserved;
only existing OCR-PROFILE/OCR-PAGES/OCR-RETRY progress is extended. Customer's
original invoice/expected values, actual corpus accuracy, highlights/raw-text
comparison, deliberate candidate comparison/application, configurable profiles,
live fleet/provider and hardware/commercial acceptance remain open. There is no
source-data repair, production migration, deployment or complete-backlog claim.
Keep existing stored observations for compatible code rollback. Remove the panel
and additive reads if necessary; do not erase evidence or downgrade data to make
an old image appear compatible. This lane performs no production action.
