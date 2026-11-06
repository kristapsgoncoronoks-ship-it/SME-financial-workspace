# Traceable unexpected errors — 6 November 2026

An unexpected API failure now returns the same `X-Request-ID` used in its error
log. Operators can correlate the user's generic error response without exposing
internal exception text. Status 500 and the existing `{detail, code}` body remain.
Normal and handled responses retain exactly one ID under the existing supplied
or generated policy, including empty-header generation, spaces and UTF-8 values.

Component `17ffa19cbe4242d6f637e35c12a25003684fd595` is based on frozen PR122
`5d4c7ef78ffdcd28eab6545172be06404066cd0d`. RequestContextMiddleware persists its
already-resolved ID in per-request ASGI state before entering application work.
Starlette's outer ServerErrorMiddleware can then recover it after the ContextVar
has reset; no ambient context is reused. The response uses the existing raw UTF-8
header encoding rather than imposing a new Latin-1 conversion.

When middleware never resolved an ID, a handler-only fixture or header decode
failure falls back to the existing supplied-or-generated policy. An invalid
UTF-8 raw header in this fallback chooses a fresh ID and preserves safe JSON;
normal middleware acceptance does not change. This ID cannot be correlated with
a log ID that was never selected. Absent/non-string/empty state is handled; this
is not a repair for arbitrary invalid ASGI objects or forged internal strings.

Only `backend/app/core/observability.py`, `backend/app/main.py` and the new focused
test module change. Logging, redaction, reset, the exact JSON500 constructor and
all unrelated module AST nodes are preserved. No body field, cache/security
policy, provider, timeout, pool, audit, financial, schema, dependency, framework
or workflow change is added; exceptions still propagate normally.

## Evidence and next action

The original HTTP read study observed pool exhaustion followed by retry recovery
with all 122 table hashes unchanged, but the safe500 omitted its promised ID.
That dated measurement is separate from this component's actual FastAPI tests.
The initial stack tests recorded six missing-header failures and six passing
normal/handled controls. Review then found a separate working-candidate fallback
regression for invalid UTF-8 bytes; its first failure is also retained.

Final qualification passes **50 distinct cases in 22.32 seconds**, comprising
16 new controls and 34 incumbent foundation/download/read-error controls, with
no failures, errors or skips. Tests use real FastAPI/ServerErrorMiddleware,
`ASGITransport(raise_app_exceptions=False)`, structured emitted logs, overlapping
requests and a same-task passive unwind observer. They cover supplied/generated,
empty/spaces/UTF-8 IDs, exactly one header, malformed incoming state, isolation,
context reset, safe body and unresolved-header fallback. Scoped Ruff/format
passes three paths and Mypy passes two application files. Independent review
accepts the final three committed hashes. These cases are not rerun in delivery.

The delivered tree collects **7,728 backend cases**, not a full-suite execution.
Full Ruff/format checks **1,026 files** and Mypy checks **452 app files**. The
existing docs/generated/discovery cohort passes all 51 controls in 16.34 seconds,
verifying unchanged API/ERD and registrations. No unchanged frontend, native or full backend suite is run
locally. Fresh delivered-source hosted CI and human review remain pending.

The ledger appends only finding `ERROR-UNHANDLED-REQUEST-ID` as item 220:
197 original rows plus 23 differently sized findings, not a completion percentage.
All219 parent identities/order/inventory/provenance and earlier evidence remain.
The separate [readiness evidence](READINESS-EVIDENCE-2026-11-06.md) records only
the approved measurements and parent CI qualification; it does not choose new
operating defaults or confer current performance/capacity acceptance.

## Source and receipts

| Path | SHA-256 |
|---|---|
| `backend/app/core/observability.py` | `baaf7d39fc3e2819b2d378c8fcb49d0a139484f4c6df470437f8e17cc08f2236` |
| `backend/app/main.py` | `3f494b09b5c9eedc524ee9c0a847b446f46017ff9149617e0071776584026113` |
| `backend/tests/test_unhandled_request_id.py` | `9f9b7d4f7f4ed861fac78caf0a8a468a1e9f4df21e4b00b287544dab8a517e0a` |

- Commit-bound component: `/tmp/error-request-id-20261106/FINAL-COMPONENT-RECEIPT-17ffa19.json`, SHA-256 `244f7af276a7b322ff2cfeb4314cb0a7dec1b6fd46cb1895dd635a752c86325c`.
- Peer review: `/tmp/error-request-id-20261106/independent-review/REVIEW-17ffa19.json`, SHA-256 `c94b0b53f44dd21237d53bcff0269885636a151f21e2d56a2972e0acf2268137`.
- Original red: `/workspace/artifacts/unhandled-request-id-repair-2026-11-06/RED-RECEIPT-5d4c7ef.json`, SHA-256 `946fa12b28bcb6917fe8707d516fa08ef61fad69aaa3801d1ebbd1562158d15d`.
- Review-found regression: `RED-FALLBACK-UTF8-v3.json` in the same packet, SHA-256 `442531c51169b547781efc6bf5fb9839288748990dea3c43611773d058610800`.

All prior hash-addressed receipts remain immutable; final delivery uses a new
receipt filename. Root reviews and publishes the draft. No production operation,
provider call, main merge or deployment is performed.
