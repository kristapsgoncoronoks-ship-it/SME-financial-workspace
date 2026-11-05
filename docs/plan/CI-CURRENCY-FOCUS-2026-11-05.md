# Currency focus clearance — 5 November 2026

PR #105 source `b0d4ef3` passed 1,070 functional browser cases and failed one
narrow currency-page case on both configured attempts. Chromium 156 exposed
fractional table width beyond an integer scroll extent, clipping an action edge.
The image, frontend and security jobs passed; broader source qualification was
still pending when this correction was prepared.

The repair gives mobile action cells four CSS pixels of trailing space and
matching scroll margin. Enlarged text scales this clearance; desktop values stay
zero. The same 13 responsive cases pass on pinned Chromium build 1248. A paired
Arial geometry probe reproduces the old clipping and verifies full visibility
afterward. Assertions, viewports, timeouts, retries and visual baselines are unchanged.

Build and bundle budgets pass at 442.3 kB raw / 129.8 kB gzip. Logs, original
failure, native bounds, hashes and the exact patch are retained under
`/workspace/artifacts/backlog-completion-2026-11-05/ci-currency-focus/`.
Delivered-source full CI is required again; this correction does not close the
separately tracked finance, lease, capacity or owner-decision backlog.
