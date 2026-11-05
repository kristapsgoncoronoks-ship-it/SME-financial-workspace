import { test, expect, type Page, type Route } from "@playwright/test";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

/**
 * The RECOVERY INTELLIGENCE workspace (WO-86) over the LIVE app shell, API
 * mocked via `page.route` — the same deterministic pattern as
 * `vat-claims.spec.ts` and `vat-admin.spec.ts`, no backend involved.
 *
 * What this suite is actually proving:
 *  - the three screens render from the WO-81 / WO-82 / WO-83 / WO-84 wire
 *    shapes, field for field,
 *  - the two figures that must NOT be shown as zero are shown as what they
 *    are: a null `median_days_to_refund` reads "not yet measurable" with its
 *    sample size, and a non-zero `currency_mismatch_claims` gets a notice
 *    explaining the €0.00 contribution,
 *  - the deadline-risk count is NOT rendered as a seventh readiness bucket,
 *  - every refusal code these routes can raise renders its human sentence and
 *    the raw slug NEVER reaches the screen — `overcharge_evidence_drift` most
 *    of all, because it fires in the window between a claim-back freezing its
 *    demand and a rebate merge changing what the same lines audit to,
 *  - the ADVISORY surfaces say so (detection writes nothing; `source_warnings`
 *    blocks nothing) and the BLOCKING refusals say so — §4.19 in the copy,
 *  - the rebate screen states the §3.H close boundary and offers no faked
 *    preview, because the backend has no preview endpoint,
 *  - the claim-back ladder offers exactly the edges `TRANSITIONS` allows,
 *  - money renders from the wire STRING with no float round-trip anywhere —
 *    asserted both on a value a double cannot hold and by a source grep,
 *  - permission-gated visibility (granted/denied pairs), module gating, nav
 *    gating, and loading / empty / error states.
 *
 * Synthetic fixtures only (master context §10): fictional supplier codes,
 * document numbers and figures. No Fleet Fuel bytes, and no literal shaped like
 * a real VAT id, IBAN or registration number.
 */

const ORG = { id: "org-1", name: "Test Workspace", status: "active" };

type Role = "owner" | "finance_manager" | "accountant" | "auditor" | "user" | "user_free";

function user(role: Role) {
  return {
    id: "user-1",
    email: "operator@test.io",
    name: "Test Operator",
    role,
    org_id: "org-1",
    is_platform_admin: false,
  };
}

const TRANSPORT_MODULE = {
  key: "transport",
  name: "Transport & VAT refunds (EU cross-border)",
  description: "",
  core: false,
  enabled: true,
  requires_issuer: false,
  ready: true,
};

// A figure with more significant digits than an IEEE-754 double can hold:
// Number("99999999999999.99") becomes 100000000000000. If any float path
// existed anywhere between the wire and the screen, these digits would not
// survive the round-trip.
const HUGE = "99999999999999.99";

const DASHBOARD = {
  year: 2026,
  currency: "EUR",
  total_claims: 9,
  buckets: [
    { state: "ready", claims: 2, vat_eur: "4210.55" },
    { state: "deadline", claims: 1, vat_eur: "1800.00" },
    { state: "missing", claims: 2, vat_eur: "930.10" },
    { state: "below", claims: 0, vat_eur: "0.00" },
    { state: "submitted", claims: 1, vat_eur: "7200.00" },
    { state: "paid", claims: 1, vat_eur: HUGE },
  ],
  excluded: [
    { reason: "withdrawn", claims: 1 },
    { reason: "rejected", claims: 1 },
  ],
  recovered_eur: HUGE,
  awaiting_eur: "7200.00",
  claimable_eur: "6940.65",
  overcharges_eur: "1250.00",
  deadline_risk_claims: 3,
  currency_mismatch_claims: 0,
  median_days_to_refund: null,
  days_to_refund_sample: 0,
};

const EMPTY_DASHBOARD = {
  ...DASHBOARD,
  total_claims: 0,
  buckets: DASHBOARD.buckets.map((b) => ({ ...b, claims: 0, vat_eur: "0.00" })),
  excluded: [],
  recovered_eur: "0.00",
  awaiting_eur: "0.00",
  claimable_eur: "0.00",
  overcharges_eur: "0.00",
  deadline_risk_claims: 0,
  currency_mismatch_claims: 0,
  median_days_to_refund: null,
  days_to_refund_sample: 0,
};

const AUDIT = {
  period: "2026-05",
  supplier: "CARDNET",
  currency: "EUR",
  price_basis: "NET EUR/L, final — VAT excluded, rebates applied",
  legal_framing:
    "Money the supplier owes — a contractual claim-back, not negotiation evidence",
  breaches: [
    {
      fuel_transaction_id: "ftx-1",
      entity_id: "ent-1",
      supplier: "CARDNET",
      country: "LV",
      station: "Riga North",
      product_group: "diesel",
      txn_date: "2026-05-11",
      qty: "412.500",
      document_currency: "EUR",
      eur_l_doc: "1.4200",
      eur_l_eff: "1.4200",
      flag: "short discount",
      agreed_eur_l: "1.3700",
      actual_eur_l: "1.4200",
      gap_eur_l: "0.0500",
      recover_eur: "20.63",
    },
    {
      fuel_transaction_id: "ftx-2",
      entity_id: "ent-1",
      supplier: "CARDNET",
      country: "PL",
      station: "Poznan West",
      product_group: "diesel",
      txn_date: "2026-05-18",
      qty: "300.000",
      document_currency: "PLN",
      eur_l_doc: "1.5100",
      eur_l_eff: "1.5100",
      flag: "over ceiling",
      agreed_eur_l: "1.4500",
      actual_eur_l: "1.5100",
      gap_eur_l: "0.0600",
      recover_eur: "18.00",
    },
  ],
  recover_eur: "38.63",
  lines_audited: 96,
  lines_without_terms: 4,
  lines_skipped_zero_qty: 1,
  source_warnings: [],
};

const CLEAN_AUDIT = {
  ...AUDIT,
  breaches: [],
  recover_eur: "0.00",
  lines_audited: 96,
  lines_without_terms: 12,
  source_warnings: [],
};

const CLAIMS = [
  {
    id: "ocl-1",
    supplier: "CARDNET",
    period: "2026-05",
    status: "claimed",
    detected_eur: HUGE,
    lines_count: 2,
    recovered_eur: null,
    note: null,
    currency: "EUR",
    created_at: "2026-06-02T09:00:00Z",
  },
  {
    id: "ocl-2",
    supplier: "TOLLWAY",
    period: "2026-04",
    status: "recovered",
    detected_eur: "410.00",
    lines_count: 5,
    recovered_eur: "410.00",
    note: "credited in full",
    currency: "EUR",
    created_at: "2026-05-02T09:00:00Z",
  },
  {
    id: "ocl-3",
    supplier: "FUELPASS",
    period: "2026-03",
    status: "detected",
    detected_eur: "77.20",
    lines_count: 1,
    recovered_eur: null,
    note: null,
    currency: "EUR",
    created_at: "2026-04-02T09:00:00Z",
  },
];

const TOTAL = { year: null, currency: "EUR", recovered_eur: "410.00" };

const TERMS = [
  {
    id: "trm-1",
    supplier: "CARDNET",
    country: "LV",
    station_like: "",
    product_group: "diesel",
    expected_discount_eur_l: "0.0500",
    max_net_eur_l: null,
    active: true,
  },
  {
    id: "trm-2",
    supplier: "TOLLWAY",
    country: "PL",
    station_like: "Poznan%",
    product_group: "diesel",
    expected_discount_eur_l: null,
    max_net_eur_l: "1.4500",
    active: false,
  },
];

const REBATES = [
  {
    id: "reb-1",
    supplier: "CARDNET",
    country: "LV",
    period: "2026-05",
    source_ref: "RB-2026-0518",
    source_party: "Rebate Partner Nord",
    rebate_date: "2026-06-10",
    currency: "EUR",
    amount_local: "1200.00",
    amount_eur: "1200.00",
    fx_rate: null,
    fx_ecb_rate: null,
    fx_ecb_date: null,
    fx_source: "eur",
    note: null,
  },
  {
    id: "reb-2",
    supplier: "CARDNET",
    country: "PL",
    period: "2026-05",
    source_ref: "RB-2026-0519",
    source_party: "Rebate Partner Nord",
    rebate_date: "2026-06-10",
    currency: "PLN",
    amount_local: "8600.00",
    amount_eur: "2000.00",
    fx_rate: "4.3000",
    fx_ecb_rate: "4.3000",
    fx_ecb_date: "2026-06-10",
    fx_source: "ecb",
    note: null,
  },
];

// WO-AH — open claims with an R12 action_deadline inside the horizon, soonest
// first, the overdue one flagged rather than dropped.
const DUE_SOON = {
  days: 14,
  today: "2026-09-06",
  overdue_claims: 1,
  items: [
    {
      claim_id: "claim-overdue",
      entity_id: "ent-1",
      refund_country: "LV",
      ref_period: "2026-Q1",
      status: "submitted",
      status_code: "2B",
      action_deadline: "2026-09-01",
      days_left: -5,
      overdue: true,
      vat_eur: "1234.50",
    },
    {
      claim_id: "claim-soon",
      entity_id: "ent-1",
      refund_country: "PL",
      ref_period: "2026-Q2",
      status: "draft",
      status_code: "2B",
      action_deadline: "2026-09-15",
      days_left: 9,
      overdue: false,
      vat_eur: null,
    },
  ],
};

interface MockOpts {
  role?: Role;
  moduleEnabled?: boolean;
  dashboard?: unknown;
  dueSoon?: unknown;
  dashboardStatus?: number;
  dashboardBody?: unknown;
  dashboardDelayMs?: number;
  audit?: unknown;
  claims?: unknown;
  terms?: unknown;
  rebates?: unknown;
  rebatesStatus?: number;
  /** Refusals keyed by a path fragment, applied to the matching request. */
  refuse?: Record<string, { status: number; code: string; detail: string; methods?: string[] }>;
  /** Collects every mutating body/URL, keyed by the matched path fragment. */
  captured?: Record<string, { method: string; url: string; body: unknown }[]>;
}

const PATHS = ["recovery-dashboard", "overcharges/audit", "overcharges", "contract-terms", "rebates"];

async function mockApi(page: Page, opts: MockOpts = {}): Promise<void> {
  const {
    role = "owner",
    moduleEnabled = true,
    dashboard = DASHBOARD,
    dueSoon = DUE_SOON,
    dashboardStatus,
    dashboardBody,
    dashboardDelayMs = 0,
    audit = AUDIT,
    claims = CLAIMS,
    terms = TERMS,
    rebates = REBATES,
    rebatesStatus,
    refuse = {},
    captured,
  } = opts;

  await page.addInitScript(() => localStorage.setItem("invoiceiq_token", "e2e-token"));

  const json = (body: unknown, status = 200) => ({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });

  await page.route("**/api/v1/**", async (route: Route) => {
    const url = new URL(route.request().url());
    const path = url.pathname.replace(/^.*\/api\/v1/, "");
    const method = route.request().method();

    if (path === "/auth/me") return route.fulfill(json({ user: user(role), organization: ORG }));
    if (path === "/auth/organizations") return route.fulfill(json([{ org_id: ORG.id, name: ORG.name, current: true }]));
    if (path === "/modules")
      return route.fulfill(json([{ ...TRANSPORT_MODULE, enabled: moduleEnabled }]));

    if (captured && method !== "GET") {
      const key = PATHS.find((p) => path.includes(p)) ?? path;
      (captured[key] ??= []).push({
        method,
        url: url.pathname + url.search,
        body: method === "DELETE" ? null : route.request().postDataJSON(),
      });
    }

    for (const [fragment, r] of Object.entries(refuse)) {
      const methods = r.methods ?? ["POST", "PUT", "DELETE"];
      if (path.includes(fragment) && methods.includes(method)) {
        return route.fulfill(json({ detail: r.detail, code: r.code }, r.status));
      }
    }

    if (path === "/transport/recovery-dashboard/due-soon") return route.fulfill(json(dueSoon));
    if (path.startsWith("/transport/recovery-dashboard")) {
      if (dashboardDelayMs) await new Promise((res) => setTimeout(res, dashboardDelayMs));
      if (dashboardStatus && dashboardStatus >= 400)
        return route.fulfill(
          json(dashboardBody ?? { detail: "boom", code: "mocked_failure" }, dashboardStatus),
        );
      return route.fulfill(json(dashboard));
    }
    if (path.startsWith("/transport/overcharges/audit")) return route.fulfill(json(audit));
    if (path === "/transport/overcharges/total") return route.fulfill(json(TOTAL));
    if (path.startsWith("/transport/overcharges")) {
      if (method === "GET" && path === "/transport/overcharges") return route.fulfill(json(claims));
      if (path.endsWith("/packet") || path.endsWith("/letter"))
        return route.fulfill({
          status: 200,
          contentType: "application/octet-stream",
          body: "synthetic-artifact-bytes",
        });
      return route.fulfill(json(CLAIMS[0]));
    }
    if (path.startsWith("/transport/contract-terms")) {
      if (method === "DELETE") return route.fulfill(json({ removed: true }));
      return route.fulfill(json(method === "GET" ? terms : TERMS[0]));
    }
    if (path.startsWith("/transport/rebates")) {
      if (method === "GET") {
        if (rebatesStatus && rebatesStatus >= 400)
          return route.fulfill(json({ detail: "boom", code: "mocked_failure" }, rebatesStatus));
        return route.fulfill(json(rebates));
      }
      return route.fulfill(json(REBATES[0]));
    }

    return route.fulfill(json({}));
  });
}

async function openDashboard(page: Page, opts: MockOpts = {}) {
  await mockApi(page, opts);
  await page.goto("/recovery");
}

async function openOvercharges(page: Page, tab: string, opts: MockOpts = {}) {
  await mockApi(page, opts);
  await page.goto("/overcharges");
  if (tab !== "Detection") await page.getByRole("tab", { name: tab }).click();
}

async function openRebates(page: Page, opts: MockOpts = {}) {
  await mockApi(page, opts);
  await page.goto("/rebates");
}

// ---------------------------------------------------------------------------
// The cash-recovery dashboard (WO-81 / R38)
// ---------------------------------------------------------------------------

test("dashboard: renders all six buckets in the service's order with counts and euros", async ({
  page,
}) => {
  await openDashboard(page);

  await expect(page.getByRole("heading", { name: "Cash recovery" })).toBeVisible();
  const states = await page.locator("table td span.font-mono").allTextContents();
  expect(states).toEqual(["ready", "deadline", "missing", "below", "submitted", "paid"]);

  await expect(page.getByRole("cell", { name: "€4,210.55", exact: true })).toBeVisible();
  await expect(page.getByRole("cell", { name: "€930.10", exact: true })).toBeVisible();
  // The empty bucket is still a row — "nothing below minimum" and "the
  // dashboard forgot" must never look the same.
  await expect(page.getByText("Below the country minimum")).toBeVisible();
  await expect(page.getByRole("cell", { name: "€0.00", exact: true })).toBeVisible();
});

test("dashboard: the north-star euros render exactly as the wire string", async ({ page }) => {
  await openDashboard(page);

  // 99999999999999.99 survives only if nothing parsed it as a double.
  await expect(page.getByText("€99,999,999,999,999.99").first()).toBeVisible();
  await expect(page.getByText("€7,200.00").first()).toBeVisible();
  await expect(page.getByText("€6,940.65")).toBeVisible();
  await expect(page.getByText("€100,000,000,000,000.00")).toHaveCount(0);
});

test("dashboard: overcharges is a separate cash stream, outside the VAT totals", async ({
  page,
}) => {
  await openDashboard(page);

  await expect(page.getByText("Beside the VAT: what suppliers owe us")).toBeVisible();
  await expect(page.getByText("€1,250.00")).toBeVisible();
  await expect(
    page.getByText("deliberately kept out of the VAT totals above", { exact: false }),
  ).toBeVisible();
});

test("dashboard: deadline risk is shown outside the six buckets", async ({ page }) => {
  await openDashboard(page);

  await expect(page.getByText("Deadline risk")).toBeVisible();
  await expect(page.getByText("Unfiled claims within 60 days", { exact: false })).toBeVisible();
  // Still exactly six rows in the readiness table — no seventh bucket.
  await expect(page.locator("table td span.font-mono")).toHaveCount(6);
  await expect(
    page.getByText("deliberately not a seventh bucket", { exact: false }),
  ).toBeVisible();
});

test("dashboard: the excluded block names withdrawn and rejected with their counts", async ({
  page,
}) => {
  await openDashboard(page);

  await expect(page.getByText("Not in any readiness state")).toBeVisible();
  await expect(page.getByText("Withdrawn — the filing was pulled back", { exact: false })).toBeVisible();
  await expect(page.getByText("Rejected — the refunding member state", { exact: false })).toBeVisible();
});

test("dashboard: a null median renders 'not yet measurable' with its sample size, never 0", async ({
  page,
}) => {
  await openDashboard(page);

  await expect(page.getByText("Not yet measurable")).toBeVisible();
  await expect(
    page.getByText("So far 0 claims carry both dates in 2026", { exact: false }),
  ).toBeVisible();
  // `exact` matters: "60 days" appears in the deadline-risk copy, and a
  // substring match there would hide the real assertion — that the median card
  // never renders the figure "0 days".
  await expect(page.getByText("0 days", { exact: true })).toHaveCount(0);
});

test("dashboard: a measurable median renders the days figure and its sample", async ({ page }) => {
  await openDashboard(page, {
    dashboard: { ...DASHBOARD, median_days_to_refund: "128.5", days_to_refund_sample: 4 },
  });

  await expect(page.getByText("128.5 days")).toBeVisible();
  await expect(page.getByText("over 4 refunded claims of 2026", { exact: false })).toBeVisible();
  await expect(page.getByText("Not yet measurable")).toHaveCount(0);
});

test("dashboard: currency_mismatch_claims explains the €0.00 contribution", async ({ page }) => {
  await openDashboard(page, { dashboard: { ...DASHBOARD, currency_mismatch_claims: 2 } });

  await expect(page.getByText("2 draft claims span more than one currency")).toBeVisible();
  await expect(
    page.getByText("rather than being summed across currencies", { exact: false }),
  ).toBeVisible();
  await expect(page.getByText("That is why a total here can look short", { exact: false })).toBeVisible();
});

test("dashboard: no mismatch notice when the count is zero", async ({ page }) => {
  await openDashboard(page);

  // Anchor first: prove the page rendered, so the absence below cannot be
  // true vacuously against a blank document (check-e2e.mjs).
  await expect(page.getByRole("heading", { name: "Cash recovery" })).toBeVisible();
  await expect(page.getByText("span more than one currency", { exact: false })).toHaveCount(0);
});

test("due soon: WO-AH — deadlines list soonest first, the overdue one flagged and linked", async ({
  page,
}) => {
  await mockApi(page);
  await page.goto("/recovery");

  const list = page.getByRole("list", { name: "Claims due soon" });
  const rows = list.getByRole("listitem");
  await expect(rows).toHaveCount(2);
  await expect(rows.nth(0)).toContainText("LV · 2026-Q1");
  await expect(rows.nth(0)).toContainText("5 days overdue");
  await expect(rows.nth(0)).toContainText("2026-09-01");
  await expect(rows.nth(0).getByRole("link")).toHaveAttribute("href", "/vat-claims/claim-overdue");
  await expect(rows.nth(1)).toContainText("PL · 2026-Q2");
  await expect(rows.nth(1)).toContainText("9 days left");
  await expect(page.getByRole("status").filter({ hasText: "1 deadline already passed" })).toBeVisible();
});

test("due soon: WO-AH — no deadlines is said in words, never as a blank card", async ({
  page,
}) => {
  await mockApi(page, { dueSoon: { days: 14, today: "2026-09-06", overdue_claims: 0, items: [] } });
  await page.goto("/recovery");
  await expect(page.getByText("No open claim has an action deadline in the next 14 days.")).toBeVisible();
});

test("dashboard: an empty year renders zero-state copy, not an error", async ({ page }) => {
  await openDashboard(page, { dashboard: EMPTY_DASHBOARD });

  await expect(page.getByText("No refund claims in 2026 yet")).toBeVisible();
  await expect(page.getByRole("alert")).toHaveCount(0);
  // The six buckets are still rendered — zeroes, not an absence.
  await expect(page.locator("table td span.font-mono")).toHaveCount(6);
});

test("dashboard: a 500 renders the error state, never the zero-state copy", async ({ page }) => {
  await openDashboard(page, { dashboardStatus: 500 });

  await expect(
    page.getByRole("alert").filter({ hasText: "Couldn’t load the recovery dashboard" }),
  ).toBeVisible();
  await expect(page.getByText("No refund claims in 2026 yet")).toHaveCount(0);
});

test("dashboard: a loading state renders before the API resolves", async ({ page }) => {
  await openDashboard(page, { dashboardDelayMs: 1200 });

  // e2e-anchor-exempt: the pre-render ABSENCE is the subject — this test
  // asserts the content is missing WHILE loading, then arrives; the trailing
  // positive assertion is what fails if the page never renders at all.
  await expect(page.getByText("Not in any readiness state")).toHaveCount(0);
  await expect(page.getByText("Not in any readiness state")).toBeVisible({ timeout: 10_000 });
});

test("dashboard: invalid_year renders its sentence, not the slug", async ({ page }) => {
  await openDashboard(page, {
    dashboardStatus: 422,
    dashboardBody: { detail: "'12' is not a valid refund year", code: "invalid_year" },
  });

  await expect(page.getByText("That isn’t a refund year this service can report on")).toBeVisible();
  await expect(page.getByText("invalid_year")).toHaveCount(0);
});

// ---------------------------------------------------------------------------
// Overcharges — detection (WO-82, R41/R53)
// ---------------------------------------------------------------------------

test("overcharges: detection renders each breach with its flag, gap, litres and euro", async ({
  page,
}) => {
  await openOvercharges(page, "Detection");

  await expect(page.getByRole("cell", { name: "Riga North" })).toBeVisible();
  await expect(page.getByText("short discount")).toBeVisible();
  await expect(page.getByText("over ceiling")).toBeVisible();
  await expect(page.getByRole("cell", { name: "412.500" })).toBeVisible();
  await expect(page.getByRole("cell", { name: "0.0500" })).toBeVisible();
  await expect(page.getByRole("cell", { name: "€20.63" })).toBeVisible();
  await expect(page.getByText("€38.63")).toBeVisible();
});

test("overcharges: the price basis and legal framing render verbatim", async ({ page }) => {
  await openOvercharges(page, "Detection");

  await expect(
    page.getByText("NET EUR/L, final — VAT excluded, rebates applied"),
  ).toBeVisible();
  await expect(
    page.getByText(
      "Money the supplier owes — a contractual claim-back, not negotiation evidence",
    ),
  ).toBeVisible();
});

test("overcharges: detection says it writes nothing and gates nothing", async ({ page }) => {
  await openOvercharges(page, "Detection");

  await expect(
    page.getByText("changes no figure, blocks no claim and halts no close", { exact: false }),
  ).toBeVisible();
});

test("overcharges: source_warnings render as an advisory notice that blocks nothing", async ({
  page,
}) => {
  await openOvercharges(page, "Detection", {
    audit: { ...AUDIT, source_warnings: ["CARDNET/LV", "CARDNET/PL"] },
  });

  await expect(
    page.getByText("Some suppliers are being priced at list because a rebate document is missing"),
  ).toBeVisible();
  await expect(
    page.getByText("This is a warning only — it blocks nothing", { exact: false }),
  ).toBeVisible();
  await expect(
    page.getByRole("status").getByRole("link", { name: "off-invoice rebates" }),
  ).toBeVisible();
});

test("overcharges: a period with no configured term is not reported as a clean supplier", async ({
  page,
}) => {
  await openOvercharges(page, "Detection", { audit: CLEAN_AUDIT });

  await expect(
    page.getByText("“We have not told the system what was agreed” is not", { exact: false }),
  ).toBeVisible();
});

test("overcharges: opening a claim-back posts the audited supplier and period", async ({ page }) => {
  const captured: Record<string, { method: string; url: string; body: unknown }[]> = {};
  await openOvercharges(page, "Detection", { captured });

  await page.getByRole("button", { name: "Open a claim-back" }).click();

  await expect.poll(() => (captured["overcharges"] ?? []).length).toBe(1);
  expect(captured["overcharges"][0].method).toBe("POST");
  expect(captured["overcharges"][0].body).toEqual({ supplier: "CARDNET", period: "2026-05" });
});

test("overcharges: no_overcharge_detected renders its sentence, not the slug", async ({ page }) => {
  await openOvercharges(page, "Detection", {
    refuse: {
      overcharges: {
        status: 422,
        code: "no_overcharge_detected",
        detail: "The contract audit finds no breach for CARDNET in 2026-05 today",
      },
    },
  });

  await page.getByRole("button", { name: "Open a claim-back" }).click();

  await expect(page.getByText("The audit finds no breach for this supplier and period")).toBeVisible();
  await expect(page.getByText("no_overcharge_detected")).toHaveCount(0);
});

// ---------------------------------------------------------------------------
// Overcharges — the claim-back lifecycle and the two artifacts (WO-82/WO-83)
// ---------------------------------------------------------------------------

test("overcharges: the ladder offers only the transitions legal from the state", async ({
  page,
}) => {
  await openOvercharges(page, "Claim-backs");

  const claimed = page.getByRole("row", { name: /CARDNET/ });
  await expect(claimed.getByRole("button", { name: "recovered" })).toBeVisible();
  await expect(claimed.getByRole("button", { name: "rejected" })).toBeVisible();
  await expect(claimed.getByRole("button", { name: "written off" })).toBeVisible();
  await expect(claimed.getByRole("button", { name: "packaged" })).toHaveCount(0);

  const detected = page.getByRole("row", { name: /FUELPASS/ });
  await expect(detected.getByRole("button", { name: "packaged" })).toBeVisible();
  await expect(detected.getByRole("button", { name: "recovered" })).toHaveCount(0);
});

test("overcharges: a terminal claim-back offers no transition", async ({ page }) => {
  await openOvercharges(page, "Claim-backs");

  const recovered = page.getByRole("row", { name: /TOLLWAY/ });
  await expect(recovered.getByText("Closed — no further step.")).toBeVisible();
  await expect(recovered.getByRole("button", { name: "rejected" })).toHaveCount(0);
});

test("overcharges: advancing to recovered posts the typed amount as the wire string", async ({
  page,
}) => {
  const captured: Record<string, { method: string; url: string; body: unknown }[]> = {};
  await openOvercharges(page, "Claim-backs", { captured });

  await page.getByRole("row", { name: /CARDNET/ }).getByRole("button", { name: "recovered" }).click();
  await page.getByLabel("Amount the supplier credited").fill("1234.56");
  await page.getByRole("button", { name: "Confirm" }).click();

  await expect.poll(() => (captured["overcharges"] ?? []).length).toBe(1);
  expect(captured["overcharges"][0].url).toContain("/transport/overcharges/ocl-1/advance");
  expect(captured["overcharges"][0].body).toEqual({
    to_status: "recovered",
    recovered_eur: "1234.56",
  });
});

test("overcharges: a non-recovered move records no amount", async ({ page }) => {
  const captured: Record<string, { method: string; url: string; body: unknown }[]> = {};
  await openOvercharges(page, "Claim-backs", { captured });

  await page.getByRole("row", { name: /FUELPASS/ }).getByRole("button", { name: "packaged" }).click();
  await expect(
    page.getByText("only a recovered claim-back books cash", { exact: false }),
  ).toBeVisible();
  await expect(page.getByLabel("Amount the supplier credited")).toHaveCount(0);
  await page.getByRole("button", { name: "Confirm" }).click();

  await expect.poll(() => (captured["overcharges"] ?? []).length).toBe(1);
  expect(captured["overcharges"][0].body).toEqual({ to_status: "packaged" });
});

test("overcharges: the demanded euro survives as the wire string", async ({ page }) => {
  await openOvercharges(page, "Claim-backs");

  await expect(page.getByRole("cell", { name: "€99,999,999,999,999.99" })).toBeVisible();
  await expect(page.getByText("€100,000,000,000,000.00")).toHaveCount(0);
});

test("overcharges: booked cash is labelled distinctly from detected exposure", async ({ page }) => {
  await openOvercharges(page, "Claim-backs");

  await expect(page.getByText("Booked cash recovered from suppliers")).toBeVisible();
  await expect(
    page.getByText("not the exposure a contract audit detects", { exact: false }),
  ).toBeVisible();
});

test("overcharges: overcharge_transition_invalid renders its sentence, not the slug", async ({
  page,
}) => {
  await openOvercharges(page, "Claim-backs", {
    refuse: {
      overcharges: {
        status: 409,
        code: "overcharge_transition_invalid",
        detail: "A claim-back in 'claimed' cannot move to 'packaged'",
      },
    },
  });

  await page.getByRole("row", { name: /FUELPASS/ }).getByRole("button", { name: "packaged" }).click();
  await page.getByRole("button", { name: "Confirm" }).click();

  await expect(
    page.getByText("That isn’t a step this claim-back can take from where it is"),
  ).toBeVisible();
  await expect(page.getByText("Nothing was changed", { exact: false })).toBeVisible();
  await expect(page.getByText("overcharge_transition_invalid")).toHaveCount(0);
});

test("overcharges: recovered_amount_invalid renders its sentence", async ({ page }) => {
  await openOvercharges(page, "Claim-backs", {
    refuse: {
      overcharges: {
        status: 422,
        code: "recovered_amount_invalid",
        detail: "The recovered amount exceeds the detected euro",
      },
    },
  });

  await page.getByRole("row", { name: /CARDNET/ }).getByRole("button", { name: "recovered" }).click();
  await page.getByLabel("Amount the supplier credited").fill("999999.00");
  await page.getByRole("button", { name: "Confirm" }).click();

  await expect(page.getByText("That recovered amount can’t be booked")).toBeVisible();
  await expect(page.getByText("recovered_amount_invalid")).toHaveCount(0);
});

// The important refusal: a rebate merged after the claim-back froze its demand.
test("overcharges: overcharge_evidence_drift explains the rebate/freeze race and the fix", async ({
  page,
}) => {
  await openOvercharges(page, "Claim-backs", {
    refuse: {
      packet: {
        status: 409,
        code: "overcharge_evidence_drift",
        detail:
          "The claim-back demands 500.00 EUR but its evidence now totals 380.00 EUR",
        methods: ["GET"],
      },
    },
  });

  await page.getByRole("row", { name: /CARDNET/ }).getByRole("button", { name: "Evidence packet" }).click();

  await expect(
    page.getByText("The frozen demand no longer matches what its own evidence lines add up to"),
  ).toBeVisible();
  await expect(
    page.getByText("a recorded off-invoice rebate has been merged", { exact: false }),
  ).toBeVisible();
  await expect(
    page.getByText("No packet and no letter are produced", { exact: false }),
  ).toBeVisible();
  await expect(page.getByText("overcharge_evidence_drift")).toHaveCount(0);
});

test("overcharges: overcharge_claim_closed says the evidence packet stays available", async ({
  page,
}) => {
  await openOvercharges(page, "Claim-backs", {
    refuse: {
      letter: {
        status: 409,
        code: "overcharge_claim_closed",
        detail: "This claim-back is 'recovered'",
        methods: ["GET"],
      },
    },
  });

  await page.getByRole("row", { name: /TOLLWAY/ }).getByRole("button", { name: "Claim letter" }).click();

  await expect(
    page.getByText("This claim-back is closed, so no new payment demand goes out"),
  ).toBeVisible();
  await expect(
    page.getByText("The evidence packet is still available", { exact: false }),
  ).toBeVisible();
  await expect(page.getByText("overcharge_claim_closed")).toHaveCount(0);
});

test("overcharges: issuer_profile_incomplete names the missing letterhead fields", async ({
  page,
}) => {
  await openOvercharges(page, "Claim-backs", {
    refuse: {
      letter: {
        status: 409,
        code: "issuer_profile_incomplete",
        detail: "your issuer company is missing: address_line1, vat_number",
        methods: ["GET"],
      },
    },
  });

  await page.getByRole("row", { name: /CARDNET/ }).getByRole("button", { name: "Claim letter" }).click();

  await expect(
    page.getByText("The demand letter goes out on your own letterhead, and it’s incomplete"),
  ).toBeVisible();
  await expect(page.getByText("address_line1, vat_number", { exact: false })).toBeVisible();
  await expect(page.getByText("issuer_profile_incomplete")).toHaveCount(0);
});

test("overcharges: pdf_renderer_unavailable points at the packet instead", async ({ page }) => {
  await openOvercharges(page, "Claim-backs", {
    refuse: {
      letter: {
        status: 503,
        code: "pdf_renderer_unavailable",
        detail: "wkhtmltopdf is not installed",
        methods: ["GET"],
      },
    },
  });

  await page.getByRole("row", { name: /CARDNET/ }).getByRole("button", { name: "Claim letter" }).click();

  await expect(page.getByText("The PDF renderer isn’t available on this server")).toBeVisible();
  await expect(
    page.getByText("The Excel evidence packet still downloads", { exact: false }),
  ).toBeVisible();
  await expect(page.getByText("pdf_renderer_unavailable")).toHaveCount(0);
});

test("overcharges: an empty worklist is a first run, not an error", async ({ page }) => {
  await openOvercharges(page, "Claim-backs", { claims: [] });

  await expect(page.getByText("No claim-backs opened yet")).toBeVisible();
  await expect(page.getByRole("alert")).toHaveCount(0);
});

// ---------------------------------------------------------------------------
// Overcharges — contract terms (WO-82, §2.5's two term types)
// ---------------------------------------------------------------------------

test("overcharges: terms render both €/L figures and their active state", async ({ page }) => {
  await openOvercharges(page, "Contract terms");

  await expect(page.getByRole("cell", { name: "0.0500" })).toBeVisible();
  await expect(page.getByRole("cell", { name: "1.4500" })).toBeVisible();
  await expect(page.getByRole("cell", { name: "every station" })).toBeVisible();
  await expect(page.getByText("Inactive")).toBeVisible();
});

test("overcharges: a term posts both €/L figures as typed strings", async ({ page }) => {
  const captured: Record<string, { method: string; url: string; body: unknown }[]> = {};
  await openOvercharges(page, "Contract terms", { captured });

  await page.getByLabel("Supplier").fill("CARDNET");
  await page.getByLabel("Country").fill("ee");
  await page.getByLabel("Product group").fill("diesel");
  await page.getByLabel("Expected discount €/L").fill("0.0425");
  await page.getByRole("button", { name: "Save term" }).click();

  await expect.poll(() => (captured["contract-terms"] ?? []).length).toBe(1);
  expect(captured["contract-terms"][0].method).toBe("PUT");
  expect(captured["contract-terms"][0].body).toEqual({
    supplier: "CARDNET",
    country: "EE",
    product_group: "diesel",
    station_like: "",
    expected_discount_eur_l: "0.0425",
    max_net_eur_l: null,
    active: true,
  });
});

test("overcharges: term_has_no_figure renders its sentence, not the slug", async ({ page }) => {
  await openOvercharges(page, "Contract terms", {
    refuse: {
      "contract-terms": {
        status: 422,
        code: "term_has_no_figure",
        detail: "A term needs an expected discount or a net ceiling",
      },
    },
  });

  await page.getByRole("row", { name: /CARDNET/ }).getByRole("button", { name: "Deactivate" }).click();

  await expect(page.getByText("An agreed term needs at least one figure")).toBeVisible();
  await expect(page.getByText("term_has_no_figure")).toHaveCount(0);
});

test("overcharges: invalid_term_rate renders its sentence, not the slug", async ({ page }) => {
  await openOvercharges(page, "Contract terms", {
    refuse: {
      "contract-terms": {
        status: 422,
        code: "invalid_term_rate",
        detail: "The expected discount must be greater than zero (€/L)",
      },
    },
  });

  await page.getByRole("row", { name: /CARDNET/ }).getByRole("button", { name: "Deactivate" }).click();

  await expect(page.getByText("That €/L figure is outside the range this service accepts")).toBeVisible();
  await expect(page.getByText("invalid_term_rate")).toHaveCount(0);
});

test("overcharges: a lapsed contract is deactivated rather than deleted", async ({ page }) => {
  await openOvercharges(page, "Contract terms");

  await expect(
    page.getByText("Deactivate a contract that simply lapsed", { exact: false }),
  ).toBeVisible();
});

// ---------------------------------------------------------------------------
// Off-invoice rebates (WO-84, G4.2/R50)
// ---------------------------------------------------------------------------

test("rebates: recorded documents render with their FX provenance and server-resolved euro", async ({
  page,
}) => {
  await openRebates(page);

  await expect(page.getByRole("cell", { name: "RB-2026-0518" })).toBeVisible();
  await expect(page.getByRole("cell", { name: "Rebate Partner Nord" }).first()).toBeVisible();
  await expect(page.getByRole("cell", { name: "PLN 8,600.00" })).toBeVisible();
  await expect(page.getByRole("cell", { name: "€2,000.00" })).toBeVisible();
  await expect(page.getByText("Converted at the ECB reference rate", { exact: false })).toBeVisible();
  await expect(page.getByText("4.3000 on 2026-06-10", { exact: false })).toBeVisible();
});

test("rebates: the form never asks for a EUR amount", async ({ page }) => {
  await openRebates(page);

  await expect(page.getByLabel("Amount on the document")).toBeVisible();
  await expect(page.getByLabel("Amount in EUR")).toHaveCount(0);
  await expect(page.getByText("There is no euro field", { exact: false })).toBeVisible();
});

test("rebates: preview is read-only and the close retains the write boundary", async ({ page }) => {
  await openRebates(page);

  await expect(
    page.getByText("Recording a rebate does not change any figure until the close runs"),
  ).toBeVisible();
  await expect(
    page.getByText("read-only preview below shows the current calculation", { exact: false }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "Preview rebate effect", exact: true })).toBeVisible();
  // Preview reads the canonical plan; the page cannot apply or merge it.
  await expect(page.getByRole("button", { name: /merge|apply/i })).toHaveCount(0);
});

test("rebates: recording posts the document, never a converted total", async ({ page }) => {
  const captured: Record<string, { method: string; url: string; body: unknown }[]> = {};
  await openRebates(page, { captured });

  await page.getByLabel("Supplier").fill("CARDNET");
  await page.getByLabel("Country").fill("lv");
  await page.getByLabel("Period adjusted").fill("2026-05");
  await page.getByLabel("Document date").fill("2026-06-10");
  await page.getByLabel("Document number").fill("RB-2026-0520");
  await page.getByLabel("Issued by").fill("Rebate Partner Nord");
  await page.getByLabel("Currency").fill("pln");
  await page.getByLabel("Amount on the document").fill("8600.00");
  await page.getByRole("button", { name: "Record rebate" }).click();

  await expect.poll(() => (captured["rebates"] ?? []).length).toBe(1);
  const body = captured["rebates"][0].body as Record<string, unknown>;
  expect(body).toEqual({
    supplier: "CARDNET",
    country: "LV",
    period: "2026-05",
    source_ref: "RB-2026-0520",
    source_party: "Rebate Partner Nord",
    rebate_date: "2026-06-10",
    currency: "PLN",
    amount_local: "8600.00",
  });
  expect(body).not.toHaveProperty("amount_eur");
});

test("rebates: a successful record says the effect applies at the next close", async ({ page }) => {
  await openRebates(page);

  await page.getByLabel("Supplier").fill("CARDNET");
  await page.getByLabel("Country").fill("lv");
  await page.getByLabel("Period adjusted").fill("2026-05");
  await page.getByLabel("Document date").fill("2026-06-10");
  await page.getByLabel("Document number").fill("RB-2026-0520");
  await page.getByLabel("Issued by").fill("Rebate Partner Nord");
  await page.getByLabel("Currency").fill("eur");
  await page.getByLabel("Amount on the document").fill("1200.00");
  await page.getByRole("button", { name: "Record rebate" }).click();

  await expect(page.getByText("No transaction figure has changed yet", { exact: false })).toBeVisible();
  await expect(page.getByText("applies at the next close for 2026-05", { exact: false })).toBeVisible();
});

test("rebates: fx_rate_unavailable says nothing was recorded", async ({ page }) => {
  await openRebates(page, {
    refuse: {
      rebates: {
        status: 422,
        code: "fx_rate_unavailable",
        detail: "No ECB rate is available for PLN on 2026-06-10",
      },
    },
  });

  await page.getByLabel("Supplier").fill("CARDNET");
  await page.getByLabel("Country").fill("lv");
  await page.getByLabel("Period adjusted").fill("2026-05");
  await page.getByLabel("Document date").fill("2026-06-10");
  await page.getByLabel("Document number").fill("RB-2026-0520");
  await page.getByLabel("Issued by").fill("Rebate Partner Nord");
  await page.getByLabel("Currency").fill("pln");
  await page.getByLabel("Amount on the document").fill("8600.00");
  await page.getByRole("button", { name: "Record rebate" }).click();

  await expect(
    page.getByText("nothing was recorded", { exact: false }),
  ).toBeVisible();
  await expect(
    page.getByText("no row was written and no figure changed", { exact: false }),
  ).toBeVisible();
  await expect(page.getByText("fx_rate_unavailable")).toHaveCount(0);
});

test("rebates: rebate_source_required renders its sentence, not the slug", async ({ page }) => {
  await openRebates(page, {
    refuse: {
      rebates: {
        status: 422,
        code: "rebate_source_required",
        detail: "A rebate needs both a document number and an issuer",
      },
    },
  });

  await page.getByLabel("Supplier").fill("CARDNET");
  await page.getByLabel("Country").fill("lv");
  await page.getByLabel("Period adjusted").fill("2026-05");
  await page.getByLabel("Document date").fill("2026-06-10");
  await page.getByLabel("Document number").fill("RB-2026-0520");
  await page.getByLabel("Issued by").fill("Rebate Partner Nord");
  await page.getByLabel("Currency").fill("eur");
  await page.getByLabel("Amount on the document").fill("1200.00");
  await page.getByRole("button", { name: "Record rebate" }).click();

  await expect(page.getByText("A rebate needs the document it came from")).toBeVisible();
  await expect(page.getByText("rebate_source_required")).toHaveCount(0);
});

test("rebates: an empty registry explains the consequence of not recording", async ({ page }) => {
  await openRebates(page, { rebates: [] });

  await expect(page.getByText("No rebate documents recorded")).toBeVisible();
  await expect(
    page.getByText("could ask for money already paid", { exact: false }),
  ).toBeVisible();
  await expect(page.getByRole("alert")).toHaveCount(0);
});

test("rebates: a 500 renders the error state, never the empty copy", async ({ page }) => {
  await openRebates(page, { rebatesStatus: 500 });

  await expect(
    page.getByRole("alert").filter({ hasText: "Couldn’t load the recorded rebates" }),
  ).toBeVisible();
  await expect(page.getByText("No rebate documents recorded")).toHaveCount(0);
});

// ---------------------------------------------------------------------------
// Permissions, module and nav gating
// ---------------------------------------------------------------------------

test("perm: a read-only role sees every figure and no mutating control", async ({ page }) => {
  await openOvercharges(page, "Detection", { role: "user_free" });

  await expect(page.getByRole("cell", { name: "Riga North" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Open a claim-back" })).toHaveCount(0);

  await page.getByRole("tab", { name: "Contract terms" }).click();
  await expect(page.getByRole("cell", { name: "0.0500" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Save term" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Deactivate" })).toHaveCount(0);

  await page.getByRole("tab", { name: "Claim-backs" }).click();
  await expect(page.getByRole("button", { name: "recovered" })).toHaveCount(0);

  await mockApi(page, { role: "user_free" });
  await page.goto("/rebates");
  await expect(page.getByRole("cell", { name: "RB-2026-0518" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Record rebate" })).toHaveCount(0);
});

test("perm: artifact downloads stay visible to a read-only role — they are reads", async ({
  page,
}) => {
  await openOvercharges(page, "Claim-backs", { role: "user_free" });

  await expect(
    page.getByRole("row", { name: /CARDNET/ }).getByRole("button", { name: "Evidence packet" }),
  ).toBeVisible();
  await expect(
    page.getByRole("row", { name: /CARDNET/ }).getByRole("button", { name: "Claim letter" }),
  ).toBeVisible();
});

test("perm: an accountant (VAT_WRITE) sees every mutating control", async ({ page }) => {
  await openOvercharges(page, "Detection", { role: "accountant" });
  await expect(page.getByRole("button", { name: "Open a claim-back" })).toBeVisible();

  await page.getByRole("tab", { name: "Contract terms" }).click();
  await expect(page.getByRole("button", { name: "Save term" })).toBeVisible();

  await page.getByRole("tab", { name: "Claim-backs" }).click();
  await expect(
    page.getByRole("row", { name: /CARDNET/ }).getByRole("button", { name: "recovered" }),
  ).toBeVisible();

  await mockApi(page, { role: "accountant" });
  await page.goto("/rebates");
  await expect(page.getByRole("button", { name: "Record rebate" })).toBeVisible();
});

test("perm: a server 403 on a read still renders through the refusal path", async ({ page }) => {
  await openDashboard(page, {
    dashboardStatus: 403,
    dashboardBody: {
      detail: "The Transport & VAT refunds module is not activated.",
      code: "module_not_enabled",
    },
  });

  await expect(
    page.getByText("The Transport & VAT refunds module isn’t active for this workspace"),
  ).toBeVisible();
  await expect(page.getByText("module_not_enabled")).toHaveCount(0);
});

test("module: transport off renders the module notice on all three pages", async ({ page }) => {
  await mockApi(page, { moduleEnabled: false });

  await page.goto("/recovery");
  await expect(page.getByText("isn't active", { exact: false })).toBeVisible();
  await expect(page.getByText("Readiness")).toHaveCount(0);

  await page.goto("/overcharges");
  await expect(page.getByText("isn't active", { exact: false })).toBeVisible();
  await expect(page.getByRole("tab", { name: "Detection" })).toHaveCount(0);

  await page.goto("/rebates");
  await expect(page.getByText("isn't active", { exact: false })).toBeVisible();
  await expect(page.getByRole("button", { name: "Record rebate" })).toHaveCount(0);
});

test("nav: the three analytics entries are permission- and module-gated", async ({ page }) => {
  await mockApi(page, { role: "auditor" });
  await page.goto("/recovery");
  const nav = page.getByRole("navigation");
  await expect(nav.getByRole("link", { name: "Cash recovery" })).toBeVisible();
  await expect(nav.getByRole("link", { name: "Supplier overcharges" })).toBeVisible();
  await expect(nav.getByRole("link", { name: "Off-invoice rebates" })).toBeVisible();

  // EMPLOYEE holds neither VAT_READ nor TRANSPORT_READ.
  await mockApi(page, { role: "user" });
  await page.goto("/recovery");
  await expect(
    page.getByRole("navigation").getByRole("link", { name: "Cash recovery" }),
  ).toHaveCount(0);

  await mockApi(page, { moduleEnabled: false });
  await page.goto("/recovery");
  await expect(
    page.getByRole("navigation").getByRole("link", { name: "Supplier overcharges" }),
  ).toHaveCount(0);
});

// ---------------------------------------------------------------------------
// Money discipline — proven structurally, not only by rendering
// ---------------------------------------------------------------------------

test("money: no page in this slice performs float arithmetic on an amount", () => {
  const here = dirname(fileURLToPath(import.meta.url));
  const files = [
    "src/pages/RecoveryDashboard.tsx",
    "src/pages/Overcharges.tsx",
    "src/pages/Rebates.tsx",
    "src/lib/transportRecovery.ts",
  ];
  // `parseFloat`/`Number(`/`toFixed` are the three ways a decimal string
  // becomes an IEEE-754 double in this codebase. None of them may appear: every
  // figure these screens show is the server's own string, rendered by
  // `decimalMoney`, which does string surgery and no arithmetic.
  const banned = [/parseFloat/, /\bNumber\s*\(/, /toFixed/, /\bMath\./];
  for (const rel of files) {
    const src = readFileSync(join(here, "..", rel), "utf8");
    for (const pattern of banned) {
      expect(pattern.test(src), `${rel} must not contain ${pattern}`).toBe(false);
    }
  }
});
