# uv / rustls maintenance — 3 November 2026

Status: implemented and locally qualified. Fresh hosted CI
for the delivered source and human/Lead review remain required. Earlier candidate
images and preliminary results must not be relabelled as final delivery proof.

Base is qualified draft PR #101, source
`4f2a360ef78d9d6cdcbcf7b04cb8359bf681de3a`. Its core `41650624105` and supply
`41650624091` runs passed all thirteen mandatory contexts on attempt 1.
Main remains `90891d8`. This slice uses branch
`fix/uv-rustls-maintenance-20261103` in the existing checkout. No merge,
deployment, production migration, provider or repository-setting action occurs.

## Confirmed repair

The actual preceding builder executable is uv 0.13.1 with rustls 0.23.45.
Published stable uv 0.13.7 supplies rustls 0.23.46 in the exact copied OCI
executable. Official advisory records identify that stable fix for:

- GHSA-c8m5-c952-6cm4 — medium.
- GHSA-h2v8-394m-p7vv — low.
- GHSA-vp88-798v-h67f — low.
- GHSA-w44r-93j7-qv28 — low.

The change updates the maintained installer parent and current provenance record.
It retains both original MIT/Apache notices, the same Python/Alpine parents,
installer flags, prefix, verified-CA handling and no-Python-download policy.
The installer executes only in the builder. Runtime payload bytes still change:
the rebuilt prefix, native build artifacts and copied provenance require their
own image identities and qualification.

| Binding | Verified value |
| --- | --- |
| Official source commit | `316c827e058c16b71d5c507a20729c2995a7bcce` |
| OCI index | `sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490` |
| linux/amd64 manifest | `sha256:c7db1d63057f268a8b6da8af9077850a60461f98d23db23c676a82089a050c91` |
| Actual OCI `/uv` SHA256 | `92a78f0a49090f868df7396e0ca6aa1af6b90995269ebc2178d361a66caac3c9` |
| Actual executable metadata | uv 0.13.7 / rustls 0.23.46; static x86_64 ELF |

All index, manifest, config and layer content digests were checked. The published
musl archive executable is different: its SHA256 is
`500c0ec54518b2a53380783586b75a79907d937d741ebb04236d90a50ed5ab26`.
Immutable packaging source explains independent builds: Docker omits self-update;
the archive enables it. Their common package versions match, but they are not
byte-equivalent binaries. The OCI hash above binds the executable actually used.

The official index attestation was initially captured as unverified because the
existing CLI lacked the command. A separate supplement then verified it with
task-private official GitHub CLI 2.103.3, whose archive matched the official asset
digest and published checksums. Normal Sigstore/TUF policy verifies the exact
index subject, source commit/repository, publishing workflow, hosted runner and
Rekor timestamp. No custom trust roots or skip flags are used. The original
unverified-at-capture phase remains unchanged. The verified index binds the
digest-checked amd64 manifest/layers; no separate amd64 attestation or reproducible
machine-code equivalence is claimed.

## Related permission correction

A cap-drop probe exposed inherited mode 0644 on the public
`/usr/local/share/licenses` parent. That prevented traversal and made a discovery
glob return an empty map, although all three uv notices were actually retained.
Explicit read-only exports preserve their names, bytes and hashes; the original
empty-map observation remains separate and does not establish missing files.

Both existing chmod commands now include that exact public parent at 0755.
Other file modes, UID 10001, private storage at 0700 and final paired-XML guard
ordering remain unchanged. Final checks must explicitly read the three uv notices
as builder root without capabilities, and Python/Expat provenance as UID 10001.
Parser verification alone does not prove notice readability.

## Qualification and evidence boundaries

The preliminary images build successfully and remove all four target IDs from
full SBOMs, reducing eight IDs / nine role rows to four IDs / five rows. All
remaining substantive findings and provider ratings remain. Complete APK and
application inventories match the qualified baseline. These results are bound to
their original images; final permission-corrected images require a fresh supplement.

The corrected final runtime `ca436bca2d0d5693001fc551f97ed69d16f0cc19635fdf397daab22aeb56ff3a`
and builder `a51ac1fa80332643f03e52081ffa2f97ceb8d03967034d0a4dc45bf96ffd7343`
build successfully. The fresh ten-role/twenty-report final audit passes and the
four target IDs remain absent. Full residual inventory is four IDs / five role
rows, with every rating retained. No APK or application inventory changes occur.
The tool's internal Rust graph does change: old/new auditable package triples
are 563 / 547, with 417 unchanged. Complete component deltas remain reviewable;
version changes are not presented as distinct removed dependencies.

Actual environment comparison records 64 physical installed distributions per
stage, their dependency metadata, 436 application Python files and 77 interpreter/
other native hashes. Requirements are not a complete transitive lock, so source
hashes alone cannot prove graph preservation. Rebuilt XML pair hashes can differ
because of build paths; fixed CAPI/ABI, source/configuration and preserved core
hashes are checked separately.

Twenty-six final comparison checks preserve every complete distribution record,
all 436 application hashes and all 77 original core/native hashes. Six final
native groups pass, including 32 unchanged XML invoice/camt/SEPA/Factur-X cases,
ordinary OCR/PDF, compression/timezone/ECB XML and actual UID 10001 ASGI health,
readiness and graceful SIGTERM 143 with complete shutdown markers. Explicit final
notice/ELF reads prove public parents 0755, private storage 0700 and coherent
system Expat 2.10.0. Owned fixtures and private material are removed.

A tiny owned HTTPS fixture exercises the real copied installer: wrong-CA refusal,
trusted 503 refusal, successful trusted-CA recovery/import and repeat stability.
Failures install no distribution or code and preserve the sentinel; uv creates
an empty lock and empty site directories. Earlier harness assumptions about no
metadata writes are preserved as failed assumptions. Owned resources and private
CA material are removed. No external provider or CVE exploit is invoked.
Those five preliminary TLS groups are reused only through the observed identical
final uv binary and installer invocation; they are not labelled new final runs.
The exact final backend separately passes eleven shared-volume stages and all
six native S3 groups, including refusal/recovery/persistence and cleanup.

Fresh source audits cover 78 Python and 188 npm dependencies with zero advisories.
The initial focused cohort passes 196 existing cases; the final source passes
all 102 image contracts. Final native, storage, image and delivered-source CI
results remain separately attributable. No new
pin-mirroring test, skip, timeout relaxation, vulnerability suppression or gate
policy change is introduced; backend collection remains 5,447.

Full evidence is under `/workspace/artifacts/uv-rustls-maintenance-2026-11-03/`.
The frozen `publisher-review/`, preliminary `inventory-review/`, scoped runtime
and adversarial phases retain their actual source/images. Final results will be
recorded in a separate image supplement and the delivered draft PR/`ci/` receipts.
No speed, RAM, hardware-capacity or CVE-specific resource-exhaustion claim is made.

## Remaining work and rollback

Global npm's three findings and the x/sys finding in two PostgreSQL roles remain
visible, including the alternate-provider HIGH and both unscored rows. No fixed
supported npm parent was verified; preserve existing guarded undici maintenance
and compatibility boundaries. Conditional feature/platform evidence is not risk
acceptance. Owner, release, finance/UI, provider and accountant decisions remain.

Rollback should retain a separately qualified fixed installer, coherent Expat
pair and patched gosu. Restoring an old vulnerable builder is not an automatic
security rollback. There is no schema or stored-data rewrite.

AI assisted research, implementation, tests, documentation and independent
reviews. Those reviews do not supply human/Lead approval. Publication-time
pending markers must not be presented as completed CI; final PR receipts will
bind actual results to the delivered source.
