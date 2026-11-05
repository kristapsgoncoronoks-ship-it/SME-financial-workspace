import { test, expect, type Page, type Route } from "@playwright/test";

/**
 * P2 product set (audit 2026-09-05: FE-004/FE-005, R2-A3).
 *
 *  - FE-004  a list page whose read failed rendered "no rows" — an empty table
 *            that looked like a true answer. The page now says it failed and
 *            offers a retry; `scripts/check-query-errors.mjs` keeps the class
 *            from growing again.
 *  - R2-A3   page-level controls ("New tax code", restore, export…) were gated
 *            on `isAdmin`/`isOwner` role checks, while the API serves a
 *            permission list per identity. A custom role whose served
 *            permissions include `settings.manage` saw no button; a role
 *            without it saw one that always 403'd. Controls now follow
 *            `hasPerm(...)` — the served list wins, the role mirror is the
 *            fallback only.
 *
 * Live app shell, API mocked via page.route. Synthetic fixtures only.
 */

const ORG = { id: "org-1", name: "Haulage Co", status: "active" };

const ISSUER = {
  id: "issuer-1",
  name: "Fictional Freight OÜ",
  is_default: true,
  legal_name: "Fictional Freight OÜ",
  trade_name: null,
  vat_number: null,
  registration_number: "12345678",
  address_line1: "Testivälja 1",
  address_line2: null,
  city: "Tallinn",
  postal_code: "10111",
  country: "EE",
  email: "billing@fictional-freight.test",
  phone: null,
  iban: null,
  bic: null,
  default_currency: "EUR",
  invoice_prefix: "INV",
  credit_note_prefix: "CN",
  next_number: 42,
  payment_terms_days: 14,
  default_penalty_rate: null,
  payment_instructions: null,
  notes: null,
  is_complete: true,
  missing_fields: [],
  has_logo: false,
};

const MODULES = [
  { key: "issuing", name: "Invoice issuing", description: "Issue customer invoices", core: false, enabled: true },
];

const json = (body: unknown, code = 200) => ({
  status: code,
  contentType: "application/json",
  body: JSON.stringify(body),
});

type Handler = (p: string, route: Route) => Promise<void> | void | false;

interface Identity {
  role: string;
  /** The permission list `/auth/me` serves; omit to send none (mirror fallback). */
  permissions?: string[];
}

async function signedIn(page: Page, who: Identity, handler: Handler) {
  await page.addInitScript(() => localStorage.setItem("invoiceiq_token", "e2e-token"));
  const user = { id: "u1", email: "crew@haulage.example", name: "Site Crew", role: who.role, org_id: "org-1", is_platform_admin: false };
  await page.route("**/api/v1/**", async (route: Route) => {
    const p = new URL(route.request().url()).pathname.replace(/^.*\/api\/v1/, "");
    if (p === "/auth/me") {
      return route.fulfill(
        json(who.permissions ? { user, organization: ORG, permissions: who.permissions } : { user, organization: ORG }),
      );
    }
    if (p === "/auth/organizations") return route.fulfill(json([{ org_id: "org-1", name: ORG.name, role: who.role, current: true }]));
    if (p === "/invoices/upload-policy") return route.fulfill(json({ max_bytes: 15728640, max_files: 25, max_batch_bytes: 393216000 }));
    if (p === "/modules") return route.fulfill(json(MODULES));
    if (p === "/dashboard/onboarding") return route.fulfill(json({ steps: [], done_count: 0, complete: true, dismissed: true, can_dismiss: true }));
    const handled = await handler(p, route);
    if (handled === false) return route.fulfill(json([]));
  });
}

// ---------------------------------------------------------------- FE-004

test("FE-004: a failed list read says so and offers a retry instead of showing an empty table", async ({ page }) => {
  let listCalls = 0;
  await signedIn(page, { role: "owner" }, async (p, route) => {
    if (p === "/issuer" && route.request().method() === "GET") return route.fulfill(json(ISSUER));
    if (p === "/issuer/registry") return route.fulfill(json([ISSUER]));
    if (p === "/issued" && route.request().method() === "GET") {
      listCalls++;
      // The paged list does not retry automatically. Its first GET fails;
      // the explicit retry requested by the person succeeds.
      if (listCalls === 1) return route.fulfill(json({ detail: "Internal Server Error" }, 500));
      return route.fulfill(json({ items: [], total: 0 }));
    }
    return false;
  });
  await page.goto("/issue");

  const failed = page.getByText("Couldn’t load the issued invoices");
  await expect(failed).toBeVisible();
  // Before the fix this read as a true answer.
  await expect(page.getByText("No invoices issued yet.")).toHaveCount(0);
  expect(listCalls).toBe(1);

  await page.getByRole("button", { name: /retry|try again/i }).click();
  await expect(page.getByText("No invoices issued yet.")).toBeVisible();
  await expect(failed).toHaveCount(0);
  expect(listCalls).toBe(2);
});

// ---------------------------------------------------------------- R2-A3

const TAX_CODES = [
  { id: "tc-1", code: "STD", name: "Standard rate", rate: "22.00", category: "standard", country: "EE", active: true, version: 1 },
];

test("R2-A3: a page control follows the SERVED permissions, not the role name (granted)", async ({ page }) => {
  // Role "user" carries no settings.manage in the mirror; the server says it does.
  await signedIn(page, { role: "user", permissions: ["invoice.read", "settings.manage"] }, async (p, route) => {
    if (p.startsWith("/tax-codes")) return route.fulfill(json(TAX_CODES));
    return false;
  });
  await page.goto("/tax-codes");
  await expect(page.getByText("Standard rate")).toBeVisible();
  await expect(page.getByRole("button", { name: "New tax code" })).toBeVisible();
});

test("R2-A3: a page control follows the SERVED permissions, not the role name (withheld)", async ({ page }) => {
  // Role "admin" carries settings.manage in the mirror; the server says it does NOT.
  await signedIn(page, { role: "admin", permissions: ["invoice.read"] }, async (p, route) => {
    if (p.startsWith("/tax-codes")) return route.fulfill(json(TAX_CODES));
    return false;
  });
  await page.goto("/tax-codes");
  await expect(page.getByText("Standard rate")).toBeVisible();
  await expect(page.getByRole("button", { name: "New tax code" })).toHaveCount(0);
});

// ---------------------------------------------------------------- PROD-004

const usage = (used: number, limit: number) => ({
  plan: "starter",
  invoices_used: used,
  invoice_limit: limit,
  invoices_remaining: Math.max(0, limit - used),
  unlimited: false,
});

/** Drop `names` on the dropzone the way a person drops a folder of scans. */
async function dropFiles(page: Page, names: string[]): Promise<void> {
  const data = await page.evaluateHandle((fileNames) => {
    const dt = new DataTransfer();
    for (const name of fileNames) {
      const body = `description,quantity,unit_price,invoice_number\nDiesel,1,10,${name}\n`;
      dt.items.add(new File([body], name, { type: "text/csv" }));
    }
    return dt;
  }, names);
  await page.getByRole("button", { name: /drag it here/i }).dispatchEvent("drop", { dataTransfer: data });
}

test("PROD-004: the owner is warned before the cap and pointed at Plan & billing", async ({ page }) => {
  await signedIn(page, { role: "owner", permissions: ["invoice.read", "invoice.write", "billing.manage"] }, async (p, route) => {
    if (p === "/access/usage") return route.fulfill(json(usage(48, 50)));
    return false;
  });
  await page.goto("/upload");
  const notice = page.getByRole("status").filter({ hasText: "Close to the monthly invoice limit" });
  await expect(notice).toBeVisible();
  await expect(notice).toContainText("48 of 50 used, 2 remaining");
  await expect(notice.getByRole("link", { name: "Upgrade the plan" })).toHaveAttribute("href", "/billing");
});

test("PROD-004: someone who cannot manage billing is told who can — never 'a platform operator'", async ({ page }) => {
  await signedIn(page, { role: "user", permissions: ["invoice.read", "invoice.write"] }, async (p, route) => {
    if (p === "/access/usage") return route.fulfill(json(usage(50, 50)));
    return false;
  });
  await page.goto("/upload");
  const notice = page.getByRole("status").filter({ hasText: "Monthly invoice limit reached" });
  await expect(notice).toBeVisible();
  await expect(notice).toContainText("Ask your workspace owner to upgrade the plan.");
  await expect(notice.getByRole("link")).toHaveCount(0);
  await expect(page.getByText("platform operator")).toHaveCount(0);
});

test("PROD-004: well under the cap there is no warning at all", async ({ page }) => {
  await signedIn(page, { role: "owner", permissions: ["invoice.read", "invoice.write", "billing.manage"] }, async (p, route) => {
    if (p === "/access/usage") return route.fulfill(json(usage(10, 50)));
    return false;
  });
  await page.goto("/upload");
  await expect(page.getByRole("heading", { level: 1, name: "Upload invoices" })).toBeVisible();
  await expect(page.getByRole("status")).toHaveCount(0);
});

test("PROD-004: a 402 on upload shows the upgrade path next to the refusal", async ({ page }) => {
  await signedIn(page, { role: "owner", permissions: ["invoice.read", "invoice.write", "billing.manage"] }, async (p, route) => {
    // The usage read is stale (the cap was hit by a colleague a moment ago).
    if (p === "/access/usage") return route.fulfill(json(usage(10, 50)));
    if (p === "/invoices/upload/batch" && route.request().method() === "POST") {
      return route.fulfill(json({ detail: "Monthly invoice limit reached (50/50) for your organization's plan." }, 402));
    }
    return false;
  });
  await page.goto("/upload");
  await dropFiles(page, ["depot-june.csv"]);
  await page.getByRole("button", { name: "Upload", exact: true }).click();
  await expect(page.getByRole("alert").filter({ hasText: "Monthly invoice limit reached (50/50)" })).toBeVisible();
  const notice = page.getByRole("status").filter({ hasText: "invoice limit" });
  await expect(notice.getByRole("link", { name: "Upgrade the plan" })).toHaveAttribute("href", "/billing");
});

test("R2-A3: with no served list the role mirror still gates the control (older API)", async ({ page }) => {
  await signedIn(page, { role: "user" }, async (p, route) => {
    if (p.startsWith("/tax-codes")) return route.fulfill(json(TAX_CODES));
    return false;
  });
  await page.goto("/tax-codes");
  await expect(page.getByText("Standard rate")).toBeVisible();
  await expect(page.getByRole("button", { name: "New tax code" })).toHaveCount(0);
});
