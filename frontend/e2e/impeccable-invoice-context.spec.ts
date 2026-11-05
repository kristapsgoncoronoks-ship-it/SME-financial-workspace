import { test, expect, type Page } from "@playwright/test";

// Synthetic live-page fixtures. All data comes from one paginated invoice read;
// no supplier/detail request is needed to decide which invoice to open.
const ORG = { id: "org-context", name: "Synthetic Workspace", status: "active" };
const USER = { id: "user-context", email: "owner@example.test", name: "Owner", role: "owner", org_id: ORG.id };

function invoice(overrides: Record<string, unknown> = {}) {
  return {
    id: "invoice-context", vendor_id: "vendor-context", supplier_name: "Synthetic Transport OÜ",
    invoice_number: "INV-CONTEXT-100", issue_date: "2026-06-01", due_date: "2026-06-15",
    currency: "USD", status: "pending", subtotal: "100.00", tax_amount: "21.00", total: "121.00",
    validation_status: "none", workflow_state: "scheduled_for_payment", source_filename: null,
    ...overrides,
  };
}

interface Options {
  items?: Record<string, unknown>[];
  total?: number;
  url?: string;
  onRead?: (url: URL) => void | Promise<void>;
  onBulk?: (body: Record<string, unknown>) => void;
  failure?: { active: boolean };
}

async function open(page: Page, options: Options = {}) {
  const calls: { method: string; path: string }[] = [];
  await page.addInitScript(() => localStorage.setItem("invoiceiq_token", "synthetic-context-token"));
  await page.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname.replace(/^.*\/api\/v1/, "");
    const method = route.request().method();
    calls.push({ method, path });
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (path === "/auth/me") return json({ user: USER, organization: ORG });
    if (path === "/auth/organizations") return json([{ org_id: ORG.id, name: ORG.name, role: USER.role, current: true }]);
    if (path === "/modules") return json([]);
    if (path === "/export/formats") return json([
      { key: "generic", action_id: "ACC01", label: "Generic accounting CSV", enabled: true, contract: "generic-v2", reason: null, setup_steps: [] },
      { key: "xero", action_id: "ACC02", label: "Xero — unconfigured", enabled: false, contract: null, reason: "Xero requires reviewed mappings.", setup_steps: ["Ask your accountant to review Xero setup."] },
      { key: "quickbooks", action_id: "ACC03", label: "QuickBooks — unconfigured", enabled: false, contract: null, reason: "QuickBooks requires reviewed mappings.", setup_steps: ["Ask your accountant to review QuickBooks setup."] },
    ]);
    if (path === "/invoices/bulk-delete") {
      const body = route.request().postDataJSON();
      options.onBulk?.(body);
      return json({ deleted: body.invoice_ids.length, skipped: 0, failed: 0, outcomes: [], deleted_records: [] });
    }
    if (path === "/invoices") {
      await options.onRead?.(url);
      if (options.failure?.active) return json({ detail: "Synthetic read unavailable" }, 500);
      const items = options.items ?? [invoice()];
      return json({ items, total: options.total ?? items.length, page: Number(url.searchParams.get("page")), page_size: 20 });
    }
    if (path === "/invoices/trash") return json({ items: [], total: 0 });
    return json([]);
  });
  await page.goto(options.url ?? "/invoices");
  return calls;
}

const row = (page: Page, number = "INV-CONTEXT-100") => page.getByRole("row").filter({ has: page.getByRole("link", { name: number, exact: true }) });
const visibleText = (page: Page, text: string, number = "INV-CONTEXT-100") => row(page, number).getByText(text, { exact: true }).filter({ visible: true });

test("the unfiltered worklist shows supplier, due date, workflow and source-currency total without detail lookups", async ({ page }) => {
  const calls = await open(page);
  await expect(visibleText(page, "Synthetic Transport OÜ")).toBeVisible();
  await expect(visibleText(page, "15 Jun 2026")).toBeVisible();
  await expect(visibleText(page, "Scheduled for payment")).toBeVisible();
  await expect(visibleText(page, "US$121.00")).toBeVisible();
  await expect(visibleText(page, "USD")).toBeVisible();
  await expect(row(page).getByText(/Issued 01 Jun 2026/)).toBeVisible();
  await expect(row(page).getByText("pending", { exact: true })).toBeVisible();
  await expect(row(page).getByText(/Tax.*US\$21\.00/).filter({ visible: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "INV-CONTEXT-100", exact: true })).toHaveAttribute("href", "/invoices/invoice-context");
  expect(calls.filter((call) => call.path.startsWith("/vendors") || /^\/invoices\//.test(call.path))).toEqual([]);
  expect(calls.filter((call) => call.path === "/invoices")).toHaveLength(1);
});

test("missing supplier, due date and workflow remain explicit without inventing Draft", async ({ page }) => {
  await open(page, { items: [invoice({ supplier_name: null, due_date: null, workflow_state: null })] });
  await expect(visibleText(page, "Supplier unavailable")).toBeVisible();
  await expect(row(page).getByText("Not set", { exact: true }).filter({ visible: true })).toHaveCount(2);
  await expect(row(page).getByText("Draft", { exact: true })).toHaveCount(0);
  await expect(visibleText(page, "US$121.00")).toBeVisible();
});

test("an older invoice list without the additive supplier field remains usable", async ({ page }) => {
  const legacy: Record<string, unknown> = invoice();
  delete legacy.supplier_name;
  await open(page, { items: [legacy] });
  await expect(visibleText(page, "Supplier unavailable")).toBeVisible();
  await expect(visibleText(page, "15 Jun 2026")).toBeVisible();
  await expect(visibleText(page, "Scheduled for payment")).toBeVisible();
  await expect(visibleText(page, "US$121.00")).toBeVisible();
});

for (const width of [320, 390]) {
  test(`all payable context and long supplier names remain readable at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 });
    const supplier = `Synthetic ${"LongSupplierName".repeat(12)} OÜ`;
    const number = `INV-${"LONG-INVOICE-REFERENCE".repeat(8)}`;
    await open(page, { items: [invoice({ supplier_name: supplier, invoice_number: number, total: "123456789012.34" })] });
    for (const text of [supplier, number, "15 Jun 2026", "Scheduled for payment", "US$123,456,789,012.34", "USD"]) {
      const fact = visibleText(page, text, number);
      await expect(fact).toBeVisible();
      const box = await fact.boundingBox();
      expect(box).not.toBeNull();
      expect(box!.x).toBeGreaterThanOrEqual(0);
      expect(box!.x + box!.width).toBeLessThanOrEqual(width + 1);
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    await expect(page.getByRole("link", { name: number, exact: true })).toHaveCount(1);
    await page.getByRole("link", { name: number, exact: true }).focus();
    await expect(page.getByRole("link", { name: number, exact: true })).toBeFocused();
  });
}

test("enlarged text keeps invoice context and existing export actions within the mobile page", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.addInitScript(() => document.addEventListener("DOMContentLoaded", () => { document.documentElement.style.fontSize = "32px"; }));
  await open(page);
  for (const text of ["Synthetic Transport OÜ", "15 Jun 2026", "Scheduled for payment", "US$121.00"]) {
    const fact = visibleText(page, text);
    await expect(fact).toBeVisible();
    const box = await fact.boundingBox();
    expect(box!.x + box!.width).toBeLessThanOrEqual(391);
  }
  await expect(page.getByRole("button", { name: "Accounting CSV", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Xero — unconfigured", exact: true })).toBeDisabled();
  await expect(page.getByRole("button", { name: "QuickBooks — unconfigured", exact: true })).toBeDisabled();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  for (const [text, word] of [["Scheduled for payment", "Scheduled"], ["Scheduled for payment", "payment"], ["US$121.00", "US$121.00"]]) {
    const lines = await visibleText(page, text).evaluate((element, word) => {
      const node = element.firstChild!;
      const start = node.textContent!.indexOf(word);
      const range = document.createRange();
      range.setStart(node, start);
      range.setEnd(node, start + word.length);
      return range.getClientRects().length;
    }, word);
    expect(lines, `${word} should not split across short text fragments`).toBe(1);
  }
});

test("intermediate-width export actions do not cover the page title", async ({ page }) => {
  await page.setViewportSize({ width: 900, height: 900 });
  await open(page);
  await expect(visibleText(page, "Synthetic Transport OÜ")).toBeVisible();
  const title = await page.getByRole("heading", { level: 1, name: "Invoices", exact: true }).boundingBox();
  const action = await page.getByRole("button", { name: "Accounting CSV", exact: true }).boundingBox();
  expect(title).not.toBeNull();
  expect(action).not.toBeNull();
  const separated = action!.x >= title!.x + title!.width || action!.y >= title!.y + title!.height;
  expect(separated).toBe(true);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(900);
});

test("list filters, twenty-row pagination and workflow-filter removal keep their original request scope", async ({ page }) => {
  const reads: URL[] = [];
  await open(page, { url: "/invoices?status=overdue&q=INV-KEEP&workflow_state=in_approval", total: 41, onRead: (url) => { reads.push(url); } });
  await expect(visibleText(page, "Synthetic Transport OÜ")).toBeVisible();
  expect(reads[0].searchParams.get("page_size")).toBe("20");
  expect(reads[0].searchParams.get("status")).toBe("overdue");
  expect(reads[0].searchParams.get("q")).toBe("INV-KEEP");
  expect(reads[0].searchParams.get("workflow_state")).toBe("in_approval");
  await page.getByRole("button", { name: "Next", exact: true }).click();
  await expect.poll(() => reads.at(-1)?.searchParams.get("page")).toBe("2");
  await page.getByRole("button", { name: "Clear workflow filter", exact: true }).click();
  await expect.poll(() => reads.at(-1)?.searchParams.has("workflow_state")).toBe(false);
  expect(reads.at(-1)!.searchParams.get("status")).toBe("overdue");
  expect(reads.at(-1)!.searchParams.get("q")).toBe("INV-KEEP");
  expect(reads.at(-1)!.searchParams.get("page")).toBe("1");
  await expect(visibleText(page, "Scheduled for payment")).toBeVisible();
});

test("number search settles once, applies Enter and clear immediately, and retains filter/page/export scope", async ({ page }) => {
  await page.clock.install();
  const reads: URL[] = [];
  await open(page, { url: "/invoices?status=overdue&q=INV-KEEP&workflow_state=in_approval", total: 41, onRead: (url) => { reads.push(url); } });
  await expect(visibleText(page, "Synthetic Transport OÜ")).toBeVisible();
  await page.getByRole("button", { name: "Next", exact: true }).click();
  await expect.poll(() => reads.at(-1)?.searchParams.get("page")).toBe("2");
  const count = reads.length;
  const search = page.getByRole("textbox", { name: "Search number", exact: true });
  for (const value of ["I", "IN", "INV"]) {
    await search.fill(value);
    await page.clock.runFor(100);
  }
  expect(reads).toHaveLength(count);
  await page.clock.runFor(149);
  expect(reads).toHaveLength(count);
  await page.clock.runFor(1);
  await expect.poll(() => reads.length).toBe(count + 1);
  expect(Object.fromEntries(reads.at(-1)!.searchParams)).toEqual({ page: "1", page_size: "20", status: "overdue", q: "INV", workflow_state: "in_approval" });
  await search.fill("INV-ENTER");
  await search.press("Enter");
  await expect.poll(() => reads.at(-1)?.searchParams.get("q")).toBe("INV-ENTER");
  await search.fill("");
  await expect.poll(() => reads.at(-1)?.searchParams.has("q")).toBe(false);
  await search.fill("INV-FILTER");
  await page.getByRole("combobox", { name: "Status", exact: true }).selectOption("paid");
  await expect.poll(() => reads.at(-1)?.searchParams.get("status")).toBe("paid");
  expect(reads.at(-1)!.searchParams.get("q")).toBe("INV-FILTER");
  await page.getByRole("button", { name: "Clear workflow filter", exact: true }).click();
  await expect.poll(() => reads.at(-1)?.searchParams.has("workflow_state")).toBe(false);
  expect(Object.fromEntries(reads.at(-1)!.searchParams)).toEqual({ page: "1", page_size: "20", status: "paid", q: "INV-FILTER" });
  // Local filters retain their existing URL-initialized behavior; removing the
  // workflow chip edits only that URL parameter.
  await expect(page).toHaveURL(/\/invoices\?status=overdue&q=INV-KEEP$/);
  await page.getByRole("button", { name: "Accounting CSV", exact: true }).click();
  await expect(page.getByRole("region", { name: "Accounting export preview" })).toContainText("List filters: status paid; number INV-FILTER; workflow any.");
  const finalCount = reads.length;
  await page.clock.runFor(500);
  expect(reads).toHaveLength(finalCount);
});

test("superseding or leaving a held invoice search aborts its read without an error toast", async ({ page }, testInfo) => {
  let release = () => {};
  const held = new Promise<void>((resolve) => { release = resolve; });
  const reads: string[] = [];
  const aborted: string[] = [];
  page.on("requestfailed", (request) => {
    if (new URL(request.url()).pathname === "/api/v1/invoices") aborted.push(new URL(request.url()).searchParams.get("q") ?? "");
  });
  await open(page, { onRead: async (url) => {
    const q = url.searchParams.get("q") ?? "";
    reads.push(q);
    if (q.startsWith("HELD")) await held;
  } });
  await expect(visibleText(page, "Synthetic Transport OÜ")).toBeVisible();
  const search = page.getByRole("textbox", { name: "Search number", exact: true });
  await search.fill("HELD-OLD"); await search.press("Enter");
  await expect.poll(() => reads.includes("HELD-OLD")).toBe(true);
  await search.fill("CURRENT"); await search.press("Enter");
  await expect.poll(() => aborted.includes("HELD-OLD")).toBe(true);
  await expect(visibleText(page, "Synthetic Transport OÜ")).toBeVisible();
  await search.fill("HELD-LEAVE"); await search.press("Enter");
  await expect.poll(() => reads.includes("HELD-LEAVE")).toBe(true);
  await page.getByRole("link", { name: "Deleted", exact: true }).click();
  await expect(page).toHaveURL(/\/invoices\/trash$/);
  await expect.poll(() => aborted.includes("HELD-LEAVE")).toBe(true);
  release();
  await expect(page.getByRole("alert")).toHaveCount(0);
  await testInfo.attach("invoice-search-requests", { body: JSON.stringify({ reads, aborted }), contentType: "application/json" });
});

test("responsive rows keep one draft checkbox and the explicit agreed-count delete contract", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  let posted: Record<string, unknown> | null = null;
  await open(page, { items: [invoice({ workflow_state: "draft" }), invoice({ id: "approved-context", invoice_number: "INV-APPROVED", workflow_state: "approved" })], onBulk: (body) => { posted = body; } });
  await expect(page.getByRole("checkbox", { name: "Select INV-CONTEXT-100", exact: true })).toHaveCount(1);
  await expect(page.getByRole("checkbox", { name: "Select INV-APPROVED", exact: true })).toHaveCount(0);
  await page.getByRole("checkbox", { name: "Select INV-CONTEXT-100", exact: true }).check();
  await expect(page.getByText("1 selected", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Delete", exact: true }).click();
  await expect.poll(() => posted).not.toBeNull();
  expect(posted).toEqual({ invoice_ids: ["invoice-context"], agreed_count: 1, selection: "explicit" });
});

test("a failed list read stays distinct from missing invoice context and recovers with GET only", async ({ page }) => {
  const failure = { active: true };
  const calls = await open(page, { failure });
  await expect(page.getByText("Couldn’t load invoices", { exact: true })).toBeVisible();
  await expect(page.getByText("Supplier unavailable", { exact: true })).toHaveCount(0);
  failure.active = false;
  await page.getByRole("button", { name: "Try again", exact: true }).click();
  await expect(visibleText(page, "Synthetic Transport OÜ")).toBeVisible();
  expect(calls.filter((call) => call.method !== "GET")).toEqual([]);
});
