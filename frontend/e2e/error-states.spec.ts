import { test, expect, type Page } from "@playwright/test";

/**
 * Async-state truth on the money-bearing pages (WO-45 / UX1). Verified defect:
 * 26 of 39 `useQuery` pages never destructured `isError`, so a failed request
 * (500, 403, timeout) rendered pixel-identical to a genuinely empty dataset —
 * a silently-wrong state with no toast and no retry. `QueryState`'s error
 * branch now renders `ErrorState` (`role="alert"`), never `EmptyState`, and
 * the empty/error copy on each page is deliberately DIFFERENT so the two
 * cases read as different facts, over the LIVE app shell with `page.route`
 * mocking — the same deterministic, no-backend pattern as
 * `billing-downgrade-confirm.spec.ts`.
 *
 * Fail-open vs fail-closed (master-context §9/§6): none of this changes
 * permission logic. A 403 renders the SAME error state as a 500 — the UI
 * draws no authorization conclusion either way; the server remains the sole
 * authority on access.
 *
 * Synthetic fixtures only (master context §10): fictional names and figures.
 */

const ORG = { id: "org-1", name: "Test Workspace", status: "active" };
const USER = {
  id: "user-1",
  email: "owner@test.io",
  name: "Owner",
  role: "owner",
  org_id: "org-1",
};

const INVOICE_LIST_EMPTY = { items: [], total: 0, page: 1, page_size: 20 };
const INVOICE_ITEM = {
  id: "inv-1",
  vendor_id: "vendor-1",
  invoice_number: "INV-TEST-1",
  issue_date: "2026-05-01",
  due_date: null,
  currency: "EUR",
  status: "pending",
  subtotal: "100.00",
  tax_amount: "21.00",
  total: "121.00",
  validation_status: "none",
  source_filename: null,
};
const INVOICE_LIST_ONE = { items: [INVOICE_ITEM], total: 1, page: 1, page_size: 20 };

const EXPENSE_REPORTS_EMPTY = { items: [], total: 0 };
const EXPENSE_REPORT = {
  id: "rep-1",
  title: "Berlin conference",
  currency: "EUR",
  status: "submitted",
  submitted_at: "2026-05-01",
  vat_total: "10.00",
  total: "60.00",
  employee_name: "A. Reviewer",
};
const EXPENSE_REPORTS_ONE = { items: [EXPENSE_REPORT], total: 1 };

/** Which failure/response a given (mocked) path returns, keyed by pathname —
 * plus, for the endpoints this suite needs to disambiguate by query string,
 * a query-aware key (see `keyFor`). A value can be a single response or an
 * array consumed in order across repeat calls (the retry test). */
type MockResponse = { status?: number; body?: unknown };
type Overrides = Record<string, MockResponse | MockResponse[]>;

function keyFor(url: URL): string {
  const path = url.pathname.replace(/^.*\/api\/v1/, "");
  if (path === "/invoices" && url.searchParams.get("validation_status")) {
    return `/invoices?validation_status=${url.searchParams.get("validation_status")}`;
  }
  if (path === "/expenses" && url.searchParams.get("mine") === "true") return "/expenses?mine=true";
  if (path === "/expenses" && url.searchParams.get("status") === "submitted") return "/expenses?status=submitted";
  return path;
}

const DEFAULTS: Record<string, unknown> = {
  "/invoices": INVOICE_LIST_EMPTY,
  "/invoices?validation_status=pending": INVOICE_LIST_EMPTY,
  "/invoices?validation_status=flagged": INVOICE_LIST_EMPTY,
  "/expenses?mine=true": EXPENSE_REPORTS_EMPTY,
  "/expenses?status=submitted": EXPENSE_REPORTS_EMPTY,
  "/expenses/summary": {
    my_reimbursable: "0.00",
    reclaimable_vat: "0.00",
    my_submitted: 0,
    pending_approvals: 0,
  },
  "/expenses/transactions": [],
  "/payment-runs": [],
  "/payment-runs/page": { items: [], total: 0 },
  "/payment-runs/payable": [],
  "/settings/validation": { ai_validation_enabled: true, human_validation_enabled: true },
};

async function mockApi(page: Page, overrides: Overrides = {}): Promise<void> {
  await page.addInitScript(() => localStorage.setItem("invoiceiq_token", "e2e-token"));
  const callIndex: Record<string, number> = {};

  const json = (body: unknown, status = 200) => ({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });

  await page.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname.replace(/^.*\/api\/v1/, "");

    if (path === "/auth/me") return route.fulfill(json({ user: USER, organization: ORG }));
    if (path === "/auth/organizations") return route.fulfill(json([{ org_id: ORG.id, name: ORG.name, role: USER.role, current: true }]));
    if (path === "/modules") {
      // Expenses.tsx gates its whole page on the "expenses" module; the
      // other pages this spec covers don't check module state.
      return route.fulfill(
        json([
          {
            key: "expenses",
            name: "Employee expenses",
            description: "",
            core: false,
            enabled: true,
            requires_issuer: false,
            ready: true,
          },
        ]),
      );
    }

    const key = keyFor(url);
    const override = overrides[key];
    if (override) {
      const list = Array.isArray(override) ? override : [override];
      const idx = callIndex[key] ?? 0;
      callIndex[key] = idx + 1;
      const chosen = list[Math.min(idx, list.length - 1)];
      if (chosen.status && chosen.status >= 400) {
        return route.fulfill(
          json({ detail: "Mocked failure for this test", code: "mocked_failure" }, chosen.status),
        );
      }
      return route.fulfill(json(chosen.body));
    }

    if (key in DEFAULTS) return route.fulfill(json(DEFAULTS[key]));
    return route.fulfill(json({}));
  });
}

// A failed mocked API call also raises a transient global toast, which is
// its OWN `role="alert"` element — a real, separate mechanism, not part of
// `QueryState`/`ErrorState`. Scoping to the `ErrorState` region's own text
// keeps these assertions about the thing WO-45 actually changed.
function errorState(page: Page, titleSubstring: string) {
  return page.getByRole("alert").filter({ hasText: titleSubstring });
}

test("invoices: a 500 shows an error, never the empty-table copy", async ({ page }) => {
  await mockApi(page, { "/invoices": { status: 500 } });
  await page.goto("/invoices");

  const alert = errorState(page, "Couldn’t load invoices");
  await expect(alert).toBeVisible();
  await expect(page.getByText("No invoices found")).toHaveCount(0);
  await expect(page.getByText("No invoices match these filters")).toHaveCount(0);
});

test("invoices: a genuinely empty result shows the empty copy, never an alert", async ({ page }) => {
  await mockApi(page, { "/invoices": { body: INVOICE_LIST_EMPTY } });
  await page.goto("/invoices");

  await expect(page.getByText("No invoices match these filters")).toBeVisible();
  await expect(errorState(page, "Couldn’t load invoices")).toHaveCount(0);
});

test("invoices: a 403 renders the same error state as a 500 (no authorization conclusion drawn)", async ({
  page,
}) => {
  await mockApi(page, { "/invoices": { status: 403 } });
  await page.goto("/invoices");

  await expect(errorState(page, "Couldn’t load invoices")).toBeVisible();
});

test("invoices: clicking Try again after a 500 then 200 renders the rows", async ({ page }) => {
  // A single always-500 response (rather than a call-count-indexed sequence)
  // avoids depending on exactly how many times React Strict Mode's dev-mode
  // double-invocation fetches on mount — deterministic either way. Once the
  // error is confirmed on screen, a SECOND `page.route` registration for the
  // same pattern takes over (Playwright dispatches the most-recently-added
  // matching handler first), so the retry's request — and only the retry's —
  // gets the success response.
  await mockApi(page, { "/invoices": { status: 500 } });
  await page.goto("/invoices");

  const alert = errorState(page, "Couldn’t load invoices");
  await expect(alert).toBeVisible();

  await mockApi(page, { "/invoices": { body: INVOICE_LIST_ONE } });
  await page.getByRole("button", { name: "Try again" }).click();

  await expect(page.getByText("INV-TEST-1")).toBeVisible();
  await expect(alert).toHaveCount(0);
});

test("review queue: a 500 on the pending region shows an error", async ({ page }) => {
  await mockApi(page, { "/invoices?validation_status=pending": { status: 500 } });
  await page.goto("/review");

  await expect(errorState(page, "Couldn’t load this queue").first()).toBeVisible();
});

test("payment runs: a 500 on the runs list shows an error", async ({ page }) => {
  await mockApi(page, { "/payment-runs/page": { status: 500 } });
  await page.goto("/payment-runs");

  await expect(errorState(page, "Couldn’t load payment runs")).toBeVisible();
});

test("expenses: one failed region shows an alert while the other still renders its rows", async ({
  page,
}) => {
  await mockApi(page, {
    "/expenses?status=submitted": { status: 500 },
    "/expenses?mine=true": { body: EXPENSE_REPORTS_ONE },
  });
  await page.goto("/expenses");

  await expect(errorState(page, "Couldn’t load expense reports")).toBeVisible();
  await expect(page.getByText("Berlin conference")).toBeVisible();
});
