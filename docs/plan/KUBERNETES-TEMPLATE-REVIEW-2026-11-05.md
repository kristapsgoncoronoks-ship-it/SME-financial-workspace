# Kubernetes template review — 5 November 2026

Fresh Trivy0.76.0 checks reproduce all32 original candidates. Explicit runtime
roles, group IDs and no-escalation/read-only/capability/seccomp settings repair18.
The14 remaining raw rows are retained: two numeric-setting false positives, four
vendor UID/GID heuristics, seven deployment registry placeholders and one
cluster-specific privileged-port acceptance item. No check is suppressed.

The checks bundle digest is `sha256:b591e9190cff4aa4e385b1a25bccfd9ecf178d3c77fd194e1410b7ce7e1df058`.
The first direct-network policy update failed and fell back to embedded rules;
that report remains dated evidence. The subsequent normal proxy/CA-supported
update succeeded, and the before/after reports use that downloaded bundle.

The migration Job now uses the existing image UID/GID10001, runtime seccomp,
no capabilities/escalation, read-only root and one writable `/tmp` volume. A
real final backend runtime plus frozen source overlay applied all158 revisions
and passed strict Alembic comparison against a fresh non-superuser PostgreSQL
database at512MiB/0.5CPU. Production Settings shape was used with private
synthetic credentials; no external provider was contacted. This is a disposable
fixture, not permission to migrate production.

NGINX keeps its existing UID/GID101 and gets explicit no-escalation/capability
and read-only boundaries, with `/tmp` and `/var/cache/nginx` writable through
group101 volumes. The exact serve-stage fixture (declared local asset-copy
substitution) serves all91 expected asset hashes, passes health, has zero effective
capabilities, NoNewPrivs1 and seccomp2 at128MiB/0.2CPU. A second run uses root-owned
group101 volumes to test the required group-write behavior. The first fixture
copied private host-build file modes and returned403; its logs are preserved.
Correcting only fixture copy modes leaves asset bytes unchanged. This fixture
does not replace the complete final-image build/audit gates or Kubernetes
admission/network/storage acceptance.

Backend and worker pods explicitly declare the already verified image GID10001.
Worker-lane comments now match the existing `email.extract,upload.extract`
commands; receipt scans and some reports still use the web tier. No new capacity
budget, automatic cleanup, network policy, provider or production setting is
introduced. Temporary-space/inode/fleet sizing remains open.

All15 manifest/validator contracts pass locally. Fresh same-source hosted CI
is still required. Full evidence and all source/probe hashes are retained at
`/workspace/artifacts/backlog-third-wave-2026-11-05/k8s-refresh/`.

| Original row | Template | Check | Severity | Disposition |
| --- | --- | --- | --- | --- |
| 1 | `10-config.yaml` | `KSV-01010` | MEDIUM | REVIEWED-FALSE-POSITIVE |
| 2 | `20-migrate-job.yaml` | `KSV-0001` | MEDIUM | REPAIRED-NATIVE-CHECKS-PASS |
| 3 | `20-migrate-job.yaml` | `KSV-0003` | LOW | REPAIRED-NATIVE-CHECKS-PASS |
| 4 | `20-migrate-job.yaml` | `KSV-0004` | LOW | REPAIRED-NATIVE-CHECKS-PASS |
| 5 | `20-migrate-job.yaml` | `KSV-0014` | HIGH | REPAIRED-NATIVE-CHECKS-PASS |
| 6 | `20-migrate-job.yaml` | `KSV-0021` | LOW | REPAIRED-NATIVE-CHECKS-PASS |
| 7 | `20-migrate-job.yaml` | `KSV-0030` | LOW | REPAIRED-NATIVE-CHECKS-PASS |
| 8 | `20-migrate-job.yaml` | `KSV-0104` | MEDIUM | REPAIRED-NATIVE-CHECKS-PASS |
| 9 | `20-migrate-job.yaml` | `KSV-0106` | LOW | REPAIRED-NATIVE-CHECKS-PASS |
| 10 | `20-migrate-job.yaml` | `KSV-0125` | MEDIUM | DEPLOYMENT-SUBSTITUTION-REQUIRED |
| 11 | `25-pgbouncer.yaml` | `KSV-0020` | LOW | REVIEWED-VENDOR-ROLE-CONTRACT |
| 12 | `25-pgbouncer.yaml` | `KSV-0021` | LOW | REVIEWED-VENDOR-ROLE-CONTRACT |
| 13 | `25-pgbouncer.yaml` | `KSV-01010` | MEDIUM | REVIEWED-FALSE-POSITIVE |
| 14 | `25-pgbouncer.yaml` | `KSV-0125` | MEDIUM | DEPLOYMENT-SUBSTITUTION-REQUIRED |
| 15 | `30-backend.yaml` | `KSV-0021` | LOW | REPAIRED-NATIVE-CHECKS-PASS |
| 16 | `30-backend.yaml` | `KSV-0125` | MEDIUM | DEPLOYMENT-SUBSTITUTION-REQUIRED |
| 17 | `35-worker.yaml` | `KSV-0021` | LOW | REPAIRED-NATIVE-CHECKS-PASS |
| 18 | `35-worker.yaml` | `KSV-0125` | MEDIUM | DEPLOYMENT-SUBSTITUTION-REQUIRED |
| 19 | `36-worker-lanes.yaml` | `KSV-0021` | LOW | REPAIRED-NATIVE-CHECKS-PASS |
| 20 | `36-worker-lanes.yaml` | `KSV-0021` | LOW | REPAIRED-NATIVE-CHECKS-PASS |
| 21 | `36-worker-lanes.yaml` | `KSV-0125` | MEDIUM | DEPLOYMENT-SUBSTITUTION-REQUIRED |
| 22 | `36-worker-lanes.yaml` | `KSV-0125` | MEDIUM | DEPLOYMENT-SUBSTITUTION-REQUIRED |
| 23 | `40-frontend.yaml` | `KSV-0001` | MEDIUM | REPAIRED-NATIVE-CHECKS-PASS |
| 24 | `40-frontend.yaml` | `KSV-0003` | LOW | REPAIRED-NATIVE-CHECKS-PASS |
| 25 | `40-frontend.yaml` | `KSV-0004` | LOW | REPAIRED-NATIVE-CHECKS-PASS |
| 26 | `40-frontend.yaml` | `KSV-0012` | MEDIUM | REPAIRED-NATIVE-CHECKS-PASS |
| 27 | `40-frontend.yaml` | `KSV-0014` | HIGH | REPAIRED-NATIVE-CHECKS-PASS |
| 28 | `40-frontend.yaml` | `KSV-0020` | LOW | REVIEWED-VENDOR-ROLE-CONTRACT |
| 29 | `40-frontend.yaml` | `KSV-0021` | LOW | REVIEWED-VENDOR-ROLE-CONTRACT |
| 30 | `40-frontend.yaml` | `KSV-0106` | LOW | REPAIRED-NATIVE-CHECKS-PASS |
| 31 | `40-frontend.yaml` | `KSV-0117` | MEDIUM | CLUSTER-NETWORK-ACCEPTANCE-REQUIRED |
| 32 | `40-frontend.yaml` | `KSV-0125` | MEDIUM | DEPLOYMENT-SUBSTITUTION-REQUIRED |
