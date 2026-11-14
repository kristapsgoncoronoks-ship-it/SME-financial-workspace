# Supported image security maintenance — 14 November 2026

Source components and bounded local development checks pass. Final composed images, full CI and human/Lead review remain pending. The separate security-audit run is frozen at `5b15e4e` and explicitly incomplete because its mandatory validators are unavailable. This maintenance does not complete that audit or establish production release clearance.

## Trigger and counting boundaries

PR126 head `bacc497` completed **11/13 required checks**. Backend: 6,712 passed/1,019 skipped; PostgreSQL and 1,322 first-attempt browser plus 13 visual checks passed. The selected image gate correctly rejected **174 HIGH package/advisory rows across four roles, covering 57 IDs**. Ten image identities and all twenty SBOM/audit reports remain available. Postgate custom PostgreSQL/S3 checks were skipped.

Full CycloneDX evidence separately retains 141 provider objects/81 IDs and 281 rating entries: 163 medium, 110 high, four critical and four low, plus four empty-rating Go objects. These have different counting grains from package/advisory rows and must not be summed. They are publisher/scanner metadata, not independently established application exploitability or severity assessments. Alternate ratings and unscored objects remain visible.

## Supported source corrections

| Component and roles | Correction | Boundary |
| --- | --- | --- |
| Frontend nginx and PostgreSQL runtime | `libxslt>=1.1.43-r4` | Existing Alpine 3.24 channel and library family |
| PostgreSQL runtime | `krb5-libs>=1.22.3-r0` | Library maintenance; no new Kerberos service |
| PgBouncer build/runtime | `libpq>=18.7-r0`, `postgresql18-client>=18.7-r0` | Existing 18.x clients; PgBouncer 1.26.1 source/checksum retained |
| Compiler/gosu builders and linked binaries | Official Go 1.27.4 image | GO-2026-6859/CVE-2026-97054; strict source/module/license guards retained |
| Node builder | `zlib>=1.4-r0` before npm | CVE-2026-4738; normal APK TLS/signature checks retained |
| PostgreSQL server | Official 16.16 Alpine 3.24 image | Same-major security release, distinct from18.x client findings |

Alpine secdb, authenticated indexes/APKs and official APKBUILDs agree. Four index signatures/eight selected APK signatures, plus both architecture-specific zlib APKs, verify against official keys. License, dependencies, build contracts and SONAME families remain compatible. These facts are not exhaustive ABI/runtime proof. Targeted `apk add --upgrade` can update resolver dependencies too; there is no claim that only named packages change. No broad `apk upgrade` or unsupported repository is introduced.

Go parent: `golang:1.27.4-alpine3.24@sha256:0a5f7fe796599ff487605987432e7619ae45beeb2fba4c2ce0b907879932aebf`; its `-alpine` alias has the same index. Gosu remains 1.20 with truthful modified-module metadata, x/sys 0.49.0 and moby/sys/user 0.4.1. TypeScript remains 7.0.2 at its original source/checksum, with x/text 0.41.0 and retained notices. Host CI verifies actual Go 1.27.4 provenance before replacing only the selected native compiler. Historical 1.27.3 fixtures stay byte-exact; current header-only protocol controls are explicitly synthetic.

PostgreSQL parent: `postgres:16.16-alpine3.24@sha256:0adbf25a9e96d38823bb813f3ac0ed41fadb665c88f1df7fe6dbfb060cbdc106`. Official config/recipe comparison changes PG_VERSION/PG_SHA256 while retaining entrypoint scripts, UID/GID 70 and the PG16 contract. Underlying OS layers change; whole-image equality is not claimed. Upstream 16.16 fixes 43 security records affecting16.15 (37 core/three contrib/three client), overlapping other records rather than adding a unique total. C-extension and selected index/reindex release guidance requires operator assessment before production. No production extension, data or index operation is performed.

## Local development evidence

- Four-role admission:146 passed, including 35 new controls; unchanged source produces four expected floor failures. Node zlib adds seven controls, giving 153 passed; unchanged Node source fails its new admission. Original helpers/tables/assertions remain. These are source-admission tests, not CVE exploit reproductions.
- Go/installer/BuildInfo: 120 passed, seven added. Old toolchains fail before execution/install; synthetic headers do not claim actual new binaries. Independent source review preserves historical data and original controls.
- The current tree collects 7,780 backend cases in 17.34 seconds, not a full-suite execution. Full Ruff/format 1,026 files and Mypy 452 source files pass. Schema, financial rules, permissions and application source are unchanged.
- Actual custom PostgreSQL builds with 16.16 and Go 1.27.4 gosu. An isolated one-shot fixture initializes 16.15, stops gracefully, copies 975 quiescent files to a new owned volume, then verifies 16.16 retains three exact numeric/JSON rows, OIDs/system identifier, ordinary role, pgcrypto, HBA and password-encryption settings. A fourth ordinary-role write survives graceful restart. The old clone stays byte-identical. UID/GID 70, zero effective/permitted process capabilities and PGDATA70:70/700 hold. Default loopback trust accepts an incorrect password; no password-enforcement improvement is claimed. Actual helper bytes/full Go graph match retained metadata and notices. Eight owned containers, two volumes and the private fixture are removed.

The PostgreSQL proof is a synthetic same-major transition, not production data/extension/index/fleet/capacity or downgrade assurance. Actual execution is linux/amd64; ARM package/manifest availability is metadata, not runtime qualification. Actual PgBouncer 1.26.1/psql 18.7/libpq and client18.7-r0 now pass a fresh owned transaction-pool fixture: an ordinary role writes/reads exact123.4500, an incorrect credential is refused with exit2/no SQL output, and valid reconnect returns124.0000. Both services run as UID/GID70 with zero capabilities and stop gracefully; owned containers, volume, internal network and private files are removed. A first fixture stopped at an inventory-format assertion after startup/admin setup; it did not complete the transaction/auth sequence. Exact same-image metadata established that apk verbose output was the fixture assumption error. The corrected new fixture preserves that failure and uses strict installed-package records, with no assertion relaxation. Frontend runtime qualification and final all-image scans remain in progress. Fresh final-source hosted CI is mandatory; passing ancestor checks do not qualify this composition.

## Retained findings and storage boundary

Glob 13.0.7 remains covered by advisory metadata. Minimatch 10.2.5 retains medium and alternate high ratings; incompatible majors are not forced. Old RustFS 1.0.4 critical zlib ratings remain preserved in the baseline records.

The development Compose pin now selects `rustfs/rustfs:1.0.5@sha256:b81d16a90ee25bd9511280127ca1471788c6e874cf0f0da759645f46b2dc1d74` after separate local qualification. Only the image literal and matching inventory-test fixture change; production refusal, legacy MinIO protection, volumes, credentials, commands and cutover gates stay unchanged. Hostinger's local-storage mode is unaffected.

- Actual package inspection finds zlib 1.4-r0. The image's entrypoint/bootstrap changes are acknowledged rather than treated as identical configuration.
- First-attempt fresh-candidate checks pass all seven incumbent S3 groups. A separate fresh-owned 1.0.4-to-1.0.5 fixture passes old data reads, new writes, multipart abort, delete/auth/read-error behavior and another new-container restart. Its nine overlapping groups are not summed with the first seven as sixteen unique tests. Exact shipped probes and all prior assertions remain; only scratch selection, internal-network and ownership adaptations were made. All owned resources and private dummy credentials are removed.
- A separate exact-image Docker-archive scan uses the repository-pinned Trivy 0.76.0, a freshly refreshed database and unchanged selected HIGH/CRITICAL policy. It reports zero selected rows and zero full CycloneDX vulnerability objects/ratings across 38 components/37 detected OS packages. Language files detected: zero; native Rust/crate coverage and exhaustive vulnerability absence are not established. Image/config digest binding and every raw report hash are retained. The observed scanner OS version is3.24.2 while package inspection identifies an updated alpine-release package; neither observation is silently rewritten.

The upstream mixed-version DELETE503 caveat and full-upgrade-before-topology/decommission guidance remain. These fixtures do not qualify fleet upgrades, downtime, retention policy or downgrade compatibility. Production activation and automatic cutover remain refused. Fresh full ten-image CI remains required; the single-image archive scan is not substituted for it. Complete remaining inventories and all provider ratings must be reported after final qualification. No waiver or application-wide vulnerability-free claim is made.

## Review and operations

Separate commits: `5bc855eb` (four-role floors), `b1d17aaf` (Go), `7192381e` (Node zlib) and `1cb68c84` (PostgreSQL16.16). Independent source review accepted the nine-file composition. Runtime receipts preserve setup failures and corrections; cleanup uncertainty and replay controls were corrected before fixture execution. Future rollback must retain security fixes or use another qualified safe image; automatic vulnerable downgrades are not proposed. Main merge, deployment and production-data operations remain outside this work.
