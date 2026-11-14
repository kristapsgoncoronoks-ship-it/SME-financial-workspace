# Official compatible glob maintenance — 14 November 2026

SEC-NPM-RESIDUAL remains partial. Official npm12.2.2 is still the latest parent and bundles glob13.0.6. The publisher supplied glob13.0.7 on its v13-legacy channel with an abort/callback settlement backport. All five npm consumers already declare compatible13.x ranges. The frontend build applies this leaf before application npm ci; host setup-node/Make/e2e toolchains do not adopt it.

## Source and preservation

The original npm archive SHA/SRI and six protected package/launcher guards remain. A stock-only preflight verifies the original20-file glob and all1,923 non-glob regular parent files before mutation. A bounded fixed-format reader authenticates the leaf archive before decompression/extraction, permits only65 regular package-relative files, checks the complete manifest/exports/dependencies/engine/license and stages a same-filesystem swap. Final ownership, all five consumer resolutions, actual default/raw CJS/ESM entries and the unchanged raw closure are checked. The legitimate nested minipass3.3.6 remains; extra copies/shadows/links are refused.

Only the canonical glob directory changes. A failed restoration retains the original backup for recovery and fails the build. Final installed validation/API probes precede provenance; a killed proof child cannot leave the build waiting indefinitely. Its15-second watchdog is a build-proof control, not a new application timeout. Provenance distinguishes the authentic npm parent from the separately maintained official leaf overlay and copies the exact BlueOak-1.0.0 LICENSE.md bytes.

Official leaf: https://registry.npmjs.org/glob/-/glob-13.0.7.tgz

- SHA256: `58342994ba61c09d9c27365d0930195c5583fe31e80c1683544805cebc065fd4`
- SRI: `sha512-wfsFgXQ8A3SEI9XGf7Nl3Y9l7ffSrGh7SUCT7vU/2CzYPep9kjxAn1S40F9OPKxAMGCVl7HtNlJhrCJUeUq83g==`
- 65 files/1,607,352 bytes; manifest `dd7bc734619392667f4eea7b183b0445016711326111b91fff07b111abe7dea8`.
- License SHA256: `a49c9ba464796f65b59fca3f1e6ca40912df1e859f575383223f7ec6c5baae09`.
- Parent non-glob manifest: `ff1cc2933960f3f8799a0c8fd52cde4e7438202397bd2c834d2809a107e3dd3d`.

The separate publisher investigation verified registry signatures and tagged source correspondence. The image verifier claims pinned hash/SRI and installed-file correspondence; it does not claim an independently verified publisher build attestation.

## Development evidence and remaining qualification

On supported host Node24.19.0,71 existing-module guard tests pass with0 skips, preserving all29 incumbent assertion bodies. Controls cover forged versions/ownership/ranges/exports/extra files/notices, malformed archives, pre-read size refusal and failed rollback preservation. Four original helpers are unchanged.

A separate actual authenticated-package comparison uses a finite six-directory/six-file fixture: old13.0.6 performs six directory reads after first-read abort;13.0.7 performs one. All four default/raw CJS/ESM representations preserve the abort reason, drain pending callbacks and recover six ordinary matches. Ordinary sync/async/stream reads agree. These overlapping observations are not added to the71 unique test nodes and are not a denial-of-service or capacity benchmark.

A task-local extracted npm prefix passes the installed CLI proof, unchanged loopback SOCKS IPv4/IPv6/undici/package-install helper, and a three-workspace npm CLI read. All1,923 non-glob files remain exact. This is not yet an actual package-manager global installation or Alpine qualification. Independent source review found and verified corrections for bounded archive allocation, backup retention on restoration failure and proof-child termination.

Next: actual pinned Node22.23.4 Alpine global installation, final image inventories/SBOM/all scanner ratings, and all required checks at the delivered revision. Historical parent PR123 passed13 required contexts, including6,712 backend pass/1,019 skip. Its unavailable raw image artifact is not reused as fresh candidate evidence.

## Advisory boundary

GHSA-3g3m-2fjr-6pw4 / CVE-2026-118773 still lists the affected range through14.0.1 and first fixed14.0.2, covering the published legacy backport. Preserve every resulting scanner row/rating. Source/behavior evidence does not authorize a waiver, false-positive classification or a clean-scan claim. Minimatch10.2.5 / GHSA-g3hv-75wg-m249 / CVE-2026-118764 remains;10.2.6 does not supply its fix. No incompatible major, metadata deletion, advisory suppression or gate change is made.

No application dependency/lock, financial rule, tenant schema, browser baseline, runtime provider action, main merge or deployment is included. The separately requested full security audit is frozen to parent5b15e4e and explicitly incomplete because its mandatory skill validators are unavailable; this development qualification does not complete that audit.
