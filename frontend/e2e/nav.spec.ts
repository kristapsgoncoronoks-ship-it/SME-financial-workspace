import { test, expect, type Page, type Locator } from "@playwright/test";

/**
 * The grouped navigation IA (WO-17 / I1.2) over the LIVE app shell, API mocked via
 * `page.route` — deterministic, no backend. Proves the three things ARCH_plan's
 * acceptance criterion names: the nav is grouped, permission-aware (admin/owner
 * flags) and module-aware (add-on modules), plus the shared shell behaviours
 * (`docs/DESIGN_SYSTEM.md`) now reachable from the live app: the org switcher, the
 * mobile drawer and the breadcrumb.
 *
 * Synthetic fixtures only (master context §10): fictional names.
 */

const GROUPS = ["Overview", "Purchases & expenses", "Sales & invoicing", "Reports & planning", "Transport", "Settings & administration"];
const CORE_GROUPS = ["Overview", "Purchases & expenses", "Reports & planning", "Settings & administration"];

const MODULE_KEYS = ["issuing", "email_intake", "budget", "expenses", "transport"];

function modules(enabled: boolean): Record<string, unknown>[] {
  return MODULE_KEYS.map((key) => ({
    key,
    name: key,
    description: key,
    core: false,
    enabled,
    requires_issuer: false,
    ready: true,
  }));
}

// A minimal dashboard payload — nav tests don't care about its content, only that
// the page under the shell renders without erroring (mirrors dashboard.spec.ts's
// REDUCED_DASHBOARD shape).
const DASHBOARD = {
  as_of: "2026-07-27",
  approvals: null,
  captures: { pending: 0, low_confidence_fields: 0 },
  payables: null,
  receivables: null,
  cash: null,
};

// PROD-003 (audit 2026-09-05): the nav is drawn from the permissions the API
// SERVES on `/auth/me`, so each role fixture carries the matrix row the real
// server would send (`app/core/authz.py::ROLE_PERMISSIONS`, verbatim). A
// fixture with NO `permissions` exercises the SPA's fallback mirror instead.
const PERMS: Record<string, string[]> = {
  owner: [
    "archive.read", "audit.read", "billing.manage", "expense.approve", "expense.read", "expense.write",
    "export.run", "invoice.approve", "invoice.delete", "invoice.read", "invoice.restore", "invoice.write",
    "issued.read", "issued.send", "issued.write", "member.manage", "member.read", "payment.read",
    "payment.write", "report.read", "role.assign", "settings.manage", "transport.read", "vat.read",
    "vat.submit", "vat.write",
  ],
  // EMPLOYEE: submits their own expenses, reads invoices. Nothing else.
  user: ["expense.read", "expense.write", "invoice.read"],
  finance_manager: [
    "audit.read", "expense.approve", "expense.read", "expense.write", "export.run", "invoice.approve",
    "invoice.delete", "invoice.read", "invoice.write", "issued.read", "issued.send", "issued.write",
    "payment.read", "payment.write", "report.read", "transport.read", "vat.read", "vat.submit", "vat.write",
  ],
  approver: ["expense.approve", "expense.read", "invoice.approve", "invoice.read", "report.read"],
};

interface MockOpts {
  role: "owner" | "user" | "finance_manager" | "approver";
  orgs: { org_id: string; name: string; role: string; current: boolean }[];
  modulesEnabled: boolean;
  isPlatformAdmin?: boolean;
  orgStatus?: "active" | "suspended";
  /** Override the served permission list (default: the role's matrix row);
   * `null` sends none, so the SPA falls back to its mirror. */
  permissions?: string[] | null;
}

async function mockApi(page: Page, opts: MockOpts): Promise<void> {
  await page.addInitScript(() => localStorage.setItem("invoiceiq_token", "e2e-token"));

  const user = {
    id: "user-1",
    email: "owner@test.io",
    name: "Test User",
    role: opts.role,
    org_id: "org-1",
    is_platform_admin: opts.isPlatformAdmin ?? false,
  };
  const org = { id: "org-1", name: "Test Workspace", status: opts.orgStatus ?? "active" };

  const json = (body: unknown, status = 200) => ({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });

  await page.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname.replace(/^.*\/api\/v1/, "");

    if (path === "/auth/me") {
      const permissions = opts.permissions === undefined ? PERMS[opts.role] : opts.permissions;
      return route.fulfill(
        json(permissions === null ? { user, organization: org } : { user, organization: org, permissions }),
      );
    }
    if (path === "/auth/organizations") return route.fulfill(json(opts.orgs));
    if (path.startsWith("/auth/switch-org/")) return route.fulfill(json({ ok: true }));
    if (path === "/modules") return route.fulfill(json(modules(opts.modulesEnabled)));
    if (path === "/billing") {
      // Suspension still renders billing. A generic [] makes that page throw,
      // so navigation assertions used to race an unrelated error boundary.
      const plan = { key: "free", name: "Free", seats: 5, price_eur: 0,
        modules: [], trial: false, purchasable: true, archive_retention_years: 3 };
      return route.fulfill(json({ plan, status: org.status, seats_used: 1,
        seats_limit: 5, available_plans: [plan], billing_enabled: false,
        billing_provider: "none", has_subscription: false }));
    }
    if (path === "/dashboard") return route.fulfill(json(DASHBOARD));
    if (path === "/invoices") return route.fulfill(json({ items: [], total: 0, page: 1, page_size: 20 }));
    if (path === "/issued") return route.fulfill(json({ items: [], total: 0, page: 1, page_size: 20 }));
    if (path === "/expenses/nav-report") return route.fulfill(json({ detail: "Synthetic unavailable report" }, 404));
    if (path.startsWith("/analytics/")) return route.fulfill(json([]));
    return route.fulfill(json([]));
  });
}

const OWNER_SINGLE_ORG = [{ org_id: "org-1", name: "Test Workspace", role: "owner", current: true }];
const OWNER_MULTI_ORG = [
  { org_id: "org-1", name: "Test Workspace", role: "owner", current: true },
  { org_id: "org-2", name: "Second Workspace", role: "owner", current: false },
];

async function openAvailableGroups(nav: Locator, expectedGroups = GROUPS) {
  await expect(nav).toBeVisible();
  // Module data arrives after identity. Wait for this fixture's complete group
  // inventory, then address headings by name rather than shifting nth indices.
  await expect(nav.getByRole("button")).toHaveText(expectedGroups);
  for (const title of expectedGroups) {
    const section = nav.getByRole("button", { name: title, exact: true });
    if (await section.getAttribute("aria-expanded") === "false") await section.click();
    await expect(section).toHaveAttribute("aria-expanded", "true");
  }
}

test("owner sees every group with a representative item per group, every module enabled", async ({ page }) => {
  await mockApi(page, { role: "owner", orgs: OWNER_SINGLE_ORG, modulesEnabled: true });
  await page.goto("/");

  const nav = page.getByRole("navigation", { name: "Primary" });
  for (const g of GROUPS) await expect(nav.getByText(g, { exact: true })).toBeVisible();
  await openAvailableGroups(nav);

  await expect(nav.getByRole("link", { name: "Supplier invoices" })).toHaveAttribute("href", "/invoices");
  await expect(nav.getByRole("link", { name: "Issued invoices" })).toHaveAttribute("href", "/issue");
  await expect(nav.getByRole("link", { name: "Explore" })).toHaveAttribute("href", "/explore");
  await expect(nav.getByRole("link", { name: "Access" })).toHaveAttribute("href", "/access");
  await expect(nav.getByRole("link", { name: "Audit log" })).toHaveAttribute("href", "/audit");
});

test("platform admin gets a Platform link in Workspace", async ({ page }) => {
  await mockApi(page, { role: "owner", orgs: OWNER_SINGLE_ORG, modulesEnabled: true, isPlatformAdmin: true });
  await page.goto("/");
  const nav = page.getByRole("navigation", { name: "Primary" });
  await openAvailableGroups(nav);
  await expect(nav.getByRole("link", { name: "Platform" })).toHaveAttribute("href", "/platform");
});

test("an employee sees only what the API serves an employee — no dead links", async ({ page }) => {
  await mockApi(page, { role: "user", orgs: OWNER_SINGLE_ORG, modulesEnabled: true });
  await page.goto("/");
  const nav = page.getByRole("navigation", { name: "Primary" });
  await openAvailableGroups(nav, CORE_GROUPS);

  // Anchors first (items the EMPLOYEE row serves) — they prove the nav actually
  // rendered for this role, so the absences below cannot pass vacuously against
  // a blank shell (check-e2e.mjs).
  await expect(nav.getByRole("link", { name: "Supplier invoices" })).toBeVisible();
  await expect(nav.getByRole("link", { name: "Expenses", exact: true })).toBeVisible();
  await expect(nav.getByRole("link", { name: "Cost objects" })).toBeVisible(); // invoice.read

  // PROD-003: before this, "Team", "Issued invoices" and "Upload" were listed for an
  // employee and answered 403 — member.read, issued.read and invoice.write are
  // not in the EMPLOYEE row.
  for (const label of [
    "Team",
    "Issued invoices",
    "Upload",
    "Payment runs",
    "Access",
    "Audit log",
    "Tax codes",
    "Currencies",
    "Documents",
    "Payment reminders",
    "Expense policy",
    "Explore",
  ]) {
    await expect(nav.getByRole("link", { name: label, exact: true, includeHidden: true })).toHaveCount(0);
  }
});

test("a finance manager sees the finance surfaces the ladder used to hide", async ({ page }) => {
  await mockApi(page, { role: "finance_manager", orgs: OWNER_SINGLE_ORG, modulesEnabled: true });
  await page.goto("/");
  const nav = page.getByRole("navigation", { name: "Primary" });
  await expect(nav.locator('a[href="/issue"]')).toHaveCount(1);
  await openAvailableGroups(nav);
  // Served by the FINANCE_MANAGER row; hidden by the old `owner`/`admin` flags.
  await expect(nav.getByRole("link", { name: "Audit log" })).toBeVisible();
  await expect(nav.getByRole("link", { name: "Reimbursements" })).toBeVisible();
  await expect(nav.getByRole("link", { name: "Payment runs" })).toBeVisible();
  await expect(nav.getByRole("link", { name: "Issued invoices", exact: true })).toBeVisible();
  // Not served: no settings.manage, member.read or billing.manage in that row.
  for (const label of ["Team", "Settings", "Billing", "Payment reminders", "Automation", "Tax codes"]) {
    await expect(nav.getByRole("link", { name: label, exact: true, includeHidden: true })).toHaveCount(0);
  }
});

test("an approver sees the approval surfaces and nothing that would 403", async ({ page }) => {
  await mockApi(page, { role: "approver", orgs: OWNER_SINGLE_ORG, modulesEnabled: true });
  await page.goto("/");
  const nav = page.getByRole("navigation", { name: "Primary" });
  await openAvailableGroups(nav, CORE_GROUPS);
  await expect(nav.getByRole("link", { name: "Review" })).toBeVisible();
  await expect(nav.getByRole("link", { name: "Reimbursements" })).toBeVisible();
  await expect(nav.getByRole("link", { name: "Explore" })).toBeVisible(); // report.read
  // Suppliers stays: /vendors is invoice.read, which an approver holds.
  for (const label of ["Upload", "Issued invoices", "Payment runs", "Team", "Audit log"]) {
    await expect(nav.getByRole("link", { name: label, exact: true, includeHidden: true })).toHaveCount(0);
  }
});

test("the SERVED list wins over the stored role, and the mirror covers a response without one", async ({
  page,
}) => {
  // Stored role "user" but the server says this person holds audit.read (say,
  // a matrix change deployed before the SPA's mirror caught up): the nav
  // follows the server.
  await mockApi(page, {
    role: "user",
    orgs: OWNER_SINGLE_ORG,
    modulesEnabled: true,
    permissions: ["invoice.read", "audit.read"],
  });
  await page.goto("/");
  const nav = page.getByRole("navigation", { name: "Primary" });
  await openAvailableGroups(nav, CORE_GROUPS);
  await expect(nav.getByRole("link", { name: "Audit log" })).toBeVisible();
  await expect(nav.getByRole("link", { name: "Expenses", exact: true, includeHidden: true })).toHaveCount(0);

  // No `permissions` on the wire at all (an older API): the mirror row for the
  // stored role renders, so an owner still gets a full nav.
  await mockApi(page, { role: "owner", orgs: OWNER_SINGLE_ORG, modulesEnabled: true, permissions: null });
  await page.goto("/");
  await openAvailableGroups(page.getByRole("navigation", { name: "Primary" }));
  await expect(nav.getByRole("link", { name: "Access" })).toBeVisible();
  await expect(nav.getByRole("link", { name: "Billing" })).toBeVisible();
});

test("disabled modules remove their items and the now-empty Receivables group disappears", async ({ page }) => {
  await mockApi(page, { role: "owner", orgs: OWNER_SINGLE_ORG, modulesEnabled: false });
  await page.goto("/");
  const nav = page.getByRole("navigation", { name: "Primary" });

  // Anchors first: Payables and Insights survive (they have non-module items),
  // proving the nav rendered before any absence is asserted (check-e2e.mjs).
  await expect(nav.getByText("Purchases & expenses", { exact: true })).toBeVisible();
  await expect(nav.getByText("Reports & planning", { exact: true })).toBeVisible();

  for (const label of [
    "Issued invoices",
    "Customers",
    "Receipts",
    "Reconciliation",
    "Invoice reports",
    "Partners",
    "Payment reminders",
    "Email intake",
    "Budget",
    "Expenses",
    "Expense policy",
  ]) {
    await expect(nav.getByRole("link", { name: label, exact: true, includeHidden: true })).toHaveCount(0);
  }
  // Every Receivables item was issuing-gated, so the group heading itself is gone.
  await expect(nav.getByText("Sales & invoicing", { exact: true })).toHaveCount(0);
});

test("org switcher: a single org renders static text", async ({ page }) => {
  await mockApi(page, { role: "owner", orgs: OWNER_SINGLE_ORG, modulesEnabled: true });
  await page.goto("/");
  await expect(page.getByText("Test Workspace")).toBeVisible();
  await expect(page.getByRole("button", { name: "Switch organization" })).toHaveCount(0);
});

test("org switcher: multiple orgs render a working picker that posts the switch", async ({ page }) => {
  await mockApi(page, { role: "owner", orgs: OWNER_MULTI_ORG, modulesEnabled: true });
  await page.goto("/");

  const trigger = page.getByRole("button", { name: "Switch organization" });
  await expect(trigger).toBeVisible();
  await trigger.click();
  const menu = page.getByRole("menu", { name: "Switch organization" });
  await expect(menu).toBeVisible();

  const switchRequest = page.waitForRequest((r) => r.url().includes("/auth/switch-org/org-2"));
  await menu.getByRole("menuitem", { name: /Second Workspace/ }).click();
  await switchRequest;
});

test("mobile drawer opens via the hamburger and contains the same grouped nav", async ({ page }) => {
  await page.setViewportSize({ width: 500, height: 800 });
  await mockApi(page, { role: "owner", orgs: OWNER_SINGLE_ORG, modulesEnabled: true });
  await page.goto("/");

  await page.getByRole("button", { name: "Open navigation menu" }).click();
  const drawerNav = page.getByRole("dialog", { name: "Menu" }).getByRole("navigation", { name: "Primary" });
  for (const g of GROUPS) await expect(drawerNav.getByText(g, { exact: true })).toBeVisible();
  await openAvailableGroups(drawerNav);
  await expect(drawerNav.getByRole("link", { name: "Supplier invoices" })).toBeVisible();
});

test("breadcrumb reflects the current page", async ({ page }) => {
  await mockApi(page, { role: "owner", orgs: OWNER_SINGLE_ORG, modulesEnabled: true });
  await page.goto("/invoices");
  await expect(page.getByRole("navigation", { name: "Breadcrumb" })).toContainText("Supplier invoices");
});

// ---------------------------------------------------------------------------
// TW-P2-01 (reference integration R4): quick navigation is a view over the
// SAME filtered nav the sidebar renders — it can reach what the sidebar shows
// and nothing else. Security regression: a role without Upload in the sidebar
// must not discover it through the palette.

test("quick navigation opens with Ctrl+K and navigates using the same filtered destinations", async ({ page }) => {
  await mockApi(page, { role: "owner", orgs: OWNER_SINGLE_ORG, modulesEnabled: true });
  await page.goto("/");
  await openAvailableGroups(page.getByRole("navigation", { name: "Primary" }));
  // The shortcut listener mounts with the shell; press only once it is there.
  await expect(page.getByRole("navigation", { name: "Primary" }).getByRole("link", { name: "Supplier invoices" })).toBeVisible();

  await page.keyboard.press("Control+K");
  const palette = page.getByRole("dialog", { name: "Go to page" });
  await expect(palette).toBeVisible();

  // Keyboard ONLY from here (R4 review A-1/Q-1: the first draft passed only
  // because `fill()` re-focused the input the focus trap had taken away).
  const query = palette.getByRole("combobox", { name: "Page name" });
  await expect(query).toBeFocused();
  await page.keyboard.type("Supplier invoices");
  await expect(query).toHaveValue("Supplier invoices");
  await expect(palette.getByRole("option", { name: /Supplier invoices/ }).first()).toBeVisible();
  await page.keyboard.press("Enter");

  await expect(page).toHaveURL(/\/invoices$/);
  await expect(palette).toBeHidden();
});

test("go-to-page lists the platform item exactly when the sidebar does", async ({ page }) => {
  await mockApi(page, { role: "owner", orgs: OWNER_SINGLE_ORG, modulesEnabled: true, isPlatformAdmin: true });
  await page.goto("/");
  const sidebar = page.getByRole("navigation", { name: "Primary" });
  await openAvailableGroups(sidebar);
  await expect(sidebar.getByRole("link", { name: "Platform" })).toBeVisible();

  await page.getByRole("button", { name: "Go to page" }).click();
  const palette = page.getByRole("dialog", { name: "Go to page" });
  await palette.getByRole("combobox", { name: "Page name" }).fill("Platform");
  await expect(palette.getByRole("option", { name: /Platform/ })).toBeVisible();
});

test("quick navigation cannot discover a destination removed by served permissions", async ({ page }) => {
  await mockApi(page, { role: "user", orgs: OWNER_SINGLE_ORG, modulesEnabled: true });
  await page.goto("/");
  // The sidebar is the reference: Invoices is there for this role, Upload is not.
  const sidebar = page.getByRole("navigation", { name: "Primary" });
  await openAvailableGroups(sidebar, CORE_GROUPS);
  await expect(sidebar.getByRole("link", { name: "Supplier invoices" })).toBeVisible();
  await expect(sidebar.getByRole("link", { name: "Upload", includeHidden: true })).toHaveCount(0);

  await page.getByRole("button", { name: "Go to page" }).click();
  const palette = page.getByRole("dialog", { name: "Go to page" });
  const query = palette.getByRole("combobox", { name: "Page name" });
  await query.fill("Upload");

  await expect(palette.getByRole("status")).toContainText("No page matches");
  await expect(palette.getByRole("option", { name: /Upload/ })).toHaveCount(0);
});

test("quick navigation is not offered while the workspace is suspended", async ({ page }) => {
  await mockApi(page, { role: "owner", orgs: OWNER_SINGLE_ORG, modulesEnabled: true, orgStatus: "suspended" });
  await page.goto("/billing");
  await expect(page.getByRole("heading", { name: "Plan & billing" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Current plan", exact: true })).toBeVisible();
  await expect(page.getByRole("alert").filter({ hasText: "Update the plan or payment details on Plan & billing" })).toContainText("suspended");
  await expect(page.getByText("This page hit an error", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Go to page" })).toHaveCount(0);
  await page.keyboard.press("Control+K");
  await expect(page.getByRole("dialog", { name: "Go to page" })).toHaveCount(0);
});

test("daily menu starts quiet while every authorized destination stays reachable", async ({ page }) => {
  await mockApi(page, { role: "owner", orgs: OWNER_SINGLE_ORG, modulesEnabled: true, isPlatformAdmin: true });
  await page.goto("/");
  const nav = page.getByRole("navigation", { name: "Primary" });
  for (const title of GROUPS) {
    const section = nav.getByRole("button", { name: title, exact: true });
    await expect(section).toBeVisible();
    await expect(section).toHaveAttribute("aria-expanded", title === "Overview" ? "true" : "false");
  }
  await expect(nav.getByRole("link")).toHaveCount(2);
  // Hidden by disclosure is not removed by permission: retain the whole route
  // inventory, including optional transport and operator navigation.
  await expect(nav.locator("a")).toHaveCount(51);
  await expect(nav.getByRole("link", { name: "Dashboard", exact: true })).toHaveAttribute("href", "/");
  await openAvailableGroups(nav);
  await expect(nav.getByRole("link")).toHaveCount(51);
  await expect(nav.getByRole("link", { name: "Issued invoices", exact: true })).toHaveAttribute("href", "/issue");
  await expect(nav.getByRole("link", { name: "Processing failures", exact: true })).toHaveAttribute("href", "/captures/failures");
  await expect(nav.getByRole("link", { name: "Payment reminders", exact: true })).toHaveAttribute("href", "/dunning");
  await expect(nav.getByRole("link", { name: "Exchange rates", exact: true })).toHaveAttribute("href", "/fx");
});

test("current section disclosure can really close and reopen with the keyboard", async ({ page }) => {
  await mockApi(page, { role: "owner", orgs: OWNER_SINGLE_ORG, modulesEnabled: true });
  await page.goto("/");
  const nav = page.getByRole("navigation", { name: "Primary" });
  const overview = nav.getByRole("button", { name: "Overview", exact: true });
  await expect(overview).toHaveAttribute("aria-expanded", "true");
  await overview.focus();
  await page.keyboard.press("Enter");
  await expect(overview).toHaveAttribute("aria-expanded", "false");
  await expect(nav.getByRole("link", { name: "Dashboard", exact: true })).toHaveCount(0);
  await page.keyboard.press("Space");
  await expect(overview).toHaveAttribute("aria-expanded", "true");
  await expect(nav.getByRole("link", { name: "Dashboard", exact: true })).toBeVisible();
});

test("purchase section orders upload, review and supplier invoices first", async ({ page }) => {
  await mockApi(page, { role: "owner", orgs: OWNER_SINGLE_ORG, modulesEnabled: true });
  await page.goto("/");
  const nav = page.getByRole("navigation", { name: "Primary" });
  const purchases = nav.getByRole("button", { name: "Purchases & expenses", exact: true });
  await purchases.click();
  const orderedLinks = await purchases.evaluate((button) => {
    const list = document.getElementById(button.getAttribute("aria-controls")!);
    return Array.from(list!.querySelectorAll("a")).map((link) => link.getAttribute("href"));
  });
  expect(orderedLinks.slice(0, 3)).toEqual(["/upload", "/review", "/invoices"]);
  await expect(nav.getByRole("link", { name: "Upload", exact: true })).toBeVisible();
});

test("quick navigation opens folded sections without accumulating automatic expansion", async ({ page }) => {
  await mockApi(page, { role: "owner", orgs: OWNER_SINGLE_ORG, modulesEnabled: true, isPlatformAdmin: true });
  await page.goto("/");
  const nav = page.getByRole("navigation", { name: "Primary" });
  const settings = nav.getByRole("button", { name: "Settings & administration", exact: true });
  await expect(settings).toHaveAttribute("aria-expanded", "false");
  await page.getByRole("button", { name: "Go to page", exact: true }).click();
  let palette = page.getByRole("dialog", { name: "Go to page" });
  await palette.getByRole("combobox", { name: "Page name" }).fill("Platform");
  await palette.getByRole("option", { name: /Platform/ }).click();
  await expect(page).toHaveURL(/\/platform$/);
  await expect(settings).toHaveAttribute("aria-expanded", "true");
  await expect(nav.getByRole("link", { name: "Platform", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Go to page", exact: true }).click();
  palette = page.getByRole("dialog", { name: "Go to page" });
  await palette.getByRole("combobox", { name: "Page name" }).fill("Supplier invoices");
  await palette.getByRole("option", { name: /Supplier invoices/ }).click();
  await expect(page).toHaveURL(/\/invoices$/);
  await expect(settings).toHaveAttribute("aria-expanded", "false");
  await expect(nav.getByRole("button", { name: "Purchases & expenses", exact: true })).toHaveAttribute("aria-expanded", "true");
});

test("direct expense detail exposes its section despite exact-only list highlighting", async ({ page }) => {
  await mockApi(page, { role: "owner", orgs: OWNER_SINGLE_ORG, modulesEnabled: true });
  await page.goto("/expenses/nav-report");
  const nav = page.getByRole("navigation", { name: "Primary" });
  await expect(nav.getByRole("button", { name: "Purchases & expenses", exact: true })).toHaveAttribute("aria-expanded", "true");
  await expect(nav.getByRole("link", { name: "Expenses", exact: true })).toBeVisible();
  await expect(nav.getByRole("link", { name: "Expenses", exact: true })).not.toHaveAttribute("aria-current", "page");
  await expect(nav.getByRole("button", { name: "Settings & administration", exact: true })).toHaveAttribute("aria-expanded", "false");
});

test("mobile and desktop share disclosure choices and controlled regions have unique IDs", async ({ page }) => {
  await mockApi(page, { role: "owner", orgs: OWNER_SINGLE_ORG, modulesEnabled: true });
  await page.goto("/");
  const desktopNav = page.locator("aside").getByRole("navigation", { name: "Primary" });
  await desktopNav.getByRole("button", { name: "Settings & administration", exact: true }).click();
  await page.setViewportSize({ width: 500, height: 800 });
  await page.getByRole("button", { name: "Open navigation menu", exact: true }).click();
  const drawerNav = page.getByRole("dialog", { name: "Menu" }).getByRole("navigation", { name: "Primary" });
  const mobileSettings = drawerNav.getByRole("button", { name: "Settings & administration", exact: true });
  await expect(mobileSettings).toHaveAttribute("aria-expanded", "true");
  await expect(drawerNav.getByRole("link", { name: "Settings", exact: true })).toBeVisible();
  const controls = await page.locator('nav[aria-label="Primary"] button[aria-controls]').evaluateAll((buttons) =>
    buttons.map((button) => button.getAttribute("aria-controls")),
  );
  expect(new Set(controls).size).toBe(controls.length);
  for (const id of controls) expect(await page.locator(`[id="${id}"]`).count()).toBe(1);
  await mobileSettings.click();
  await expect(drawerNav.getByRole("link", { name: "Settings", exact: true })).toHaveCount(0);
  await page.keyboard.press("Escape");
  await page.setViewportSize({ width: 1280, height: 800 });
  await expect(desktopNav.getByRole("button", { name: "Settings & administration", exact: true })).toHaveAttribute("aria-expanded", "false");
});

test("mobile destination click closes the drawer and opens the current section", async ({ page }) => {
  await page.setViewportSize({ width: 500, height: 800 });
  await mockApi(page, { role: "owner", orgs: OWNER_SINGLE_ORG, modulesEnabled: true });
  await page.goto("/");
  await page.getByRole("button", { name: "Open navigation menu", exact: true }).click();
  const drawer = page.getByRole("dialog", { name: "Menu" });
  const nav = drawer.getByRole("navigation", { name: "Primary" });
  await nav.getByRole("button", { name: "Purchases & expenses", exact: true }).click();
  await nav.getByRole("link", { name: "Supplier invoices", exact: true }).click();
  await expect(page).toHaveURL(/\/invoices$/);
  await expect(drawer).toHaveCount(0);
  await page.getByRole("button", { name: "Open navigation menu", exact: true }).click();
  await expect(nav.getByRole("button", { name: "Purchases & expenses", exact: true })).toHaveAttribute("aria-expanded", "true");
  await expect(nav.getByRole("link", { name: "Supplier invoices", exact: true })).toHaveAttribute("aria-current", "page");
});

// ---------------------------------------------------------------------------
// WO-45 (UX1): `PageHeader` is now the sole page-title mechanism on the eight
// money-bearing pages it touched — each renders exactly ONE `<h1>` (a bare
// `getByRole("heading", { level: 1 })` count), collapsing the duplicate
// `<h1>`s `Expenses.tsx`/`IssuedReports.tsx` used to render across their two
// branches (module-inactive/gated vs the live page).
// ---------------------------------------------------------------------------

const INVOICE_ITEM_H1 = {
  id: "inv-1",
  vendor_id: "vendor-1",
  invoice_number: "INV-H1-TEST",
  issue_date: "2026-05-01",
  due_date: null,
  currency: "EUR",
  status: "pending",
  subtotal: "100.00",
  tax_amount: "21.00",
  total: "121.00",
  validation_status: "none",
  source_filename: null,
  cost_center: null,
  department: null,
  project: null,
  vehicle: null,
  property_ref: null,
};

async function mockApiForOneH1(page: Page): Promise<void> {
  await page.addInitScript(() => localStorage.setItem("invoiceiq_token", "e2e-token"));
  const user = { id: "user-1", email: "owner@test.io", name: "Owner", role: "owner", org_id: "org-1" };
  const org = { id: "org-1", name: "Test Workspace", status: "active" };

  const json = (body: unknown, status = 200) => ({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });

  await page.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname.replace(/^.*\/api\/v1/, "");

    if (path === "/auth/me") return route.fulfill(json({ user, organization: org }));
    if (path === "/auth/organizations") return route.fulfill(json([{ org_id: org.id, name: org.name, current: true }]));
    if (path === "/modules") {
      return route.fulfill(
        json([
          { key: "expenses", name: "Expenses", description: "", core: false, enabled: true, requires_issuer: false, ready: true },
          { key: "issuing", name: "Issuing", description: "", core: false, enabled: true, requires_issuer: false, ready: true },
        ]),
      );
    }
    if (path === "/invoices" && !url.searchParams.get("validation_status")) {
      return route.fulfill(json({ items: [INVOICE_ITEM_H1], total: 1, page: 1, page_size: 20 }));
    }
    if (path === "/invoices" && url.searchParams.get("validation_status")) {
      return route.fulfill(json({ items: [], total: 0, page: 1, page_size: 100 }));
    }
    if (path === "/invoices/inv-1") return route.fulfill(json({ ...INVOICE_ITEM_H1, vendor_name: "Fictional Vendor", notes: null, line_items: [], validation_findings: [], validated_by: null, validated_at: null, workflow_state: null, amount_paid: "0.00", paid_date: null, outstanding: "121.00" }));
    if (path === "/invoices/inv-1/payments") return route.fulfill(json([]));
    if (path === "/expenses/summary") return route.fulfill(json({ my_reimbursable: "0.00", reclaimable_vat: "0.00", my_submitted: 0, pending_approvals: 0 }));
    if (path === "/expenses" || path === "/expenses/transactions") return route.fulfill(json(path === "/expenses" ? { items: [], total: 0 } : []));
    if (path === "/payment-runs/page") return route.fulfill(json({ items: [], total: 0 }));
    if (path === "/payment-runs" || path === "/payment-runs/payable") return route.fulfill(json([]));
    if (path === "/analytics/cash-position") {
      return route.fulfill(
        json({
          currency: "EUR",
          receivables: { outstanding: "0.00", overdue: "0.00", avg_days_to_pay: null, aging: [] },
          payables: { outstanding: "0.00", overdue: "0.00", scheduled: 0, count: 0, in_run: 0 },
          reconciliation: { unmatched: 0, matched: 0, ignored: 0, unmatched_amount: "0.00" },
          net_position: "0.00",
        }),
      );
    }
    if (path === "/analytics/ap-aging") {
      return route.fulfill(
        json({ currency: "EUR", due_soon_count: 0, due_soon_amount: "0.00", overdue_count: 0, overdue_amount: "0.00", other_currencies: [], items: [] }),
      );
    }
    if (path === "/analytics/cash-flow") return route.fulfill(json([]));
    if (path === "/issued/reports/summary") {
      return route.fulfill(
        json({ count: 0, currency: "EUR", net: "0.00", vat: "0.00", gross: "0.00", collected: "0.00", outstanding: "0.00", series: [] }),
      );
    }
    if (path === "/receipts") return route.fulfill(json([]));
    if (path === "/settings/validation") return route.fulfill(json({ ai_validation_enabled: true, human_validation_enabled: true }));

    return route.fulfill(json({}));
  });
}

test("every audited page renders exactly one h1", async ({ page }) => {
  await mockApiForOneH1(page);

  const routes = [
    "/invoices",
    "/expenses",
    "/payment-runs",
    "/cash-position",
    "/issue/reports",
    "/receipts",
    "/review",
    "/invoices/inv-1",
  ];
  for (const route of routes) {
    await page.goto(route);
    await expect(page.getByRole("heading", { level: 1 }), `route ${route}`).toHaveCount(1);
  }
});
