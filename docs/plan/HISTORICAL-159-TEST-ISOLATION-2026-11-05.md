# Historical159 native test isolation — 5 November 2026

PR109 source `9a47efa` correctly failed two native migration tests. They called
revision159's downgrade directly against current revision160, where the comment
and approval-step foreign keys legitimately depend on159's report-pair index.
PostgreSQL refused that index drop. The constraints were functioning as intended.
The failed job,252.99-second cohort and original error remain evidence.

Move only the two historical migration cases into a separate admin-enabled
cohort. It creates a fresh UUID-named database owned by the non-superuser app
role, runs the actual Alembic upgrade to159 and verifies the recorded marker,
item pair/index and pre160 sibling schema. The unchanged rollback-only family
setup then exercises the historical operations. Full catalogue assertions
strengthen the original row/FK/preflight controls. Cleanup drops only that
fixture-owned database; the current application database stays at160.

The nine current-head tenant/model/RLS cases remain in the ordinary native
cohort. Both moved case bodies preserve their existing assertions; no application,
model, migration, CASCADE, integrity or rollback policy is changed. The new file
is explicitly registered in CI's existing step with both app and disposable-
database admin URLs. Missing credentials never count as native qualification.

Final source passes31 affected native cases with zero skips: nine current-head,
two at actual159, and20 current160 sibling cases. All40 discovery controls pass;
Ruff/format and whitespace checks pass. No fixture databases remain. Fresh full
delivered-source CI is required; the first failed head is not rewritten green.

The independently reproduced Duplicate recovery fixture race is also corrected
in the follow-up source: wait for the current page's failed read before changing
the mocked server to success. Assertions, retries and timeouts remain unchanged;
its25-case union and eight scheduling repetitions are recorded separately.

Evidence:
`/workspace/artifacts/backlog-fifth-wave-2026-11-05/pr109-historical159/` and
`/workspace/artifacts/backlog-sixth-wave-2026-11-05/ci-duplicate-recovery/`.
