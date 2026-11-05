import { test, expect, type Page, type Route } from "@playwright/test";

// Synthetic browser interaction evidence. Server ordering/count/tenant contracts
// are verified independently by the issued pagination API/native regressions.
const ORG = { id: "pagination-org", name: "Synthetic issuing workspace", status: "active" };
const USER = { id: "pagination-owner", org_id: ORG.id, name: "Owner", role: "owner", email: "owner@example.com" };
const PROFILE = { id: "pagination-issuer", legal_name: "Synthetic Seller", is_complete: true, missing_fields: [], default_penalty_rate: null };
const PAGE_SIZE = 100;
function invoice(index: number) {
  return {
    id: `pagination-invoice-${index}`, number: `PAGE-${String(index).padStart(4, "0")}`,
    issuer_id: PROFILE.id, buyer_name: `Synthetic buyer ${index}`, buyer_email: null,
    issue_date: "2026-10-13", due_date: null, currency: "EUR", total: "121.00",
    subtotal: "100.00", tax_total: "21.00", lifecycle: "issued", doc_type: "invoice",
    status: "open", amount_paid: "0.00", credited_total: "0.00", outstanding: "121.00",
    sent_at: null, reminder_count: 0, last_reminder_at: null, vat_scheme: "standard", lines: [],
  };
}
interface Wire {
  rows: ReturnType<typeof invoice>[];
  reads: Array<{ page: number; size: number; query: string }>;
  writes: Array<{ method: string; path: string; key?: string; payload: unknown }>;
  zip: string[];
  failures: Map<number, number>;
  holds: Map<number, { entered: boolean; release: () => void; wait: Promise<void> }>;
  lostCash: boolean;
  cashCommitted: boolean;
  totalOverride?: number;
}
async function setup(page: Page, count = 101, role = "owner"): Promise<Wire> {
  const wire: Wire = { rows: Array.from({ length: count }, (_, index) => invoice(index + 1)), reads: [], writes: [], zip: [], failures: new Map(), holds: new Map(), lostCash: false, cashCommitted: false };
  const json = (body: unknown, status = 200) => ({ status, contentType: "application/json", body: JSON.stringify(body) });
  await page.addInitScript(() => localStorage.setItem("invoiceiq_token", "pagination-synthetic-token"));
  await page.route("**/api/v1/**", async (route: Route) => {
    const req = route.request();
    const url = new URL(req.url());
    const path = url.pathname.replace("/api/v1", "");
    const method = req.method();
    if (["POST", "PATCH", "PUT", "DELETE"].includes(method)) wire.writes.push({ method, path, key: req.headers()["idempotency-key"], payload: req.postDataJSON() });
    if (path === "/auth/me") return route.fulfill(json({ user: { ...USER, role }, organization: ORG }));
    if (path === "/auth/organizations") return route.fulfill(json([{ org_id: ORG.id, name: ORG.name, current: true }]));
    if (path === "/modules") return route.fulfill(json([{ key: "issuing", enabled: true, ready: true, requires_issuer: true }]));
    if (path === "/issuer") return route.fulfill(json(PROFILE));
    if (path === "/issuer/registry" || path === "/issuer/entities") return route.fulfill(json([PROFILE]));
    if (path === "/issued" && method === "GET") {
      const requestedPage = Number(url.searchParams.get("page") ?? "1");
      const size = Number(url.searchParams.get("page_size") ?? "100");
      wire.reads.push({ page: requestedPage, size, query: url.search });
      const hold = wire.holds.get(requestedPage);
      if (hold) { hold.entered = true; await hold.wait; }
      if (wire.failures.has(requestedPage)) return route.fulfill(json({ detail: "Synthetic issued-list read unavailable" }, wire.failures.get(requestedPage)));
      const start = (requestedPage - 1) * size;
      return route.fulfill(json({ items: wire.rows.slice(start, start + size), total: wire.totalOverride ?? wire.rows.length }));
    }
    if (path === "/issued/export.zip") { wire.zip.push(url.search); return route.fulfill({ status: 200, contentType: "application/zip", body: "synthetic archive scope fixture" }); }
    if (path === "/issued/reminders/run") return route.fulfill(json({ sent: 0, skipped_no_email: 0 }));
    if (path === "/issued/pagination-invoice-1/payments" && method === "POST" && wire.lostCash) return route.abort("failed");
    if (path.startsWith("/issued/pagination-invoice-1/payment-commands/")) {
      if (!wire.cashCommitted) return route.fulfill(json({ detail: "No committed command" }, 404));
      return route.fulfill(json({ payment: { id: "pagination-cash-1", amount: "121.00", reversal_of_id: null }, invoice: { ...wire.rows[0], amount_paid: "121.00", outstanding: "0.00", status: "paid" } }));
    }
    if (path === "/issued/recurring" || path === "/partners" || path === "/costing/projects" || path === "/masters/projects" || path === "/tax-codes") return route.fulfill(json([]));
    return route.fulfill(json({ items: [], total: 0 }));
  });
  return wire;
}
function pages(page: Page) { return page.getByRole("navigation", { name: "Issued invoice pages", exact: true }); }
function row(page: Page, number: number) { return page.getByRole("row").filter({ has: page.getByText(`PAGE-${String(number).padStart(4, "0")}`, { exact: true }) }); }

test("all 101 issued invoices are reachable through bounded server pages", async ({ page }) => {
  const wire = await setup(page);
  await page.goto("/issue");
  await expect(page.getByRole("heading", { name: "Issued (101)", exact: true })).toBeVisible();
  await expect(row(page, 1)).toBeVisible();
  await expect(row(page, 100)).toHaveCount(1);
  await expect(row(page, 101)).toHaveCount(0);
  await expect(pages(page).getByRole("button", { name: "Previous page", exact: true })).toBeDisabled();
  await pages(page).getByRole("button", { name: "Next page", exact: true }).click();
  await expect(row(page, 101)).toBeVisible();
  await expect(row(page, 1)).toHaveCount(0);
  await expect(pages(page)).toContainText("Showing 101–101 of 101");
  await expect(pages(page)).toContainText("Page 2 of 2");
  await expect(pages(page).getByRole("button", { name: "Next page", exact: true })).toBeDisabled();
  expect(wire.reads.map(({ page: value, size }) => [value, size])).toEqual([[1, PAGE_SIZE], [2, PAGE_SIZE]]);
  expect(wire.reads.every(({ query }) => new URLSearchParams(query).get("page_size") === String(PAGE_SIZE))).toBe(true);
  expect(wire.writes).toEqual([]);
});

test("page URL survives reload and browser history", async ({ page }) => {
  const wire = await setup(page);
  await page.goto("/issue?context=kept");
  await pages(page).getByRole("button", { name: "Next page", exact: true }).click();
  await expect(page).toHaveURL(/context=kept.*page=2/);
  await expect(row(page, 101)).toBeVisible();
  await page.reload();
  await expect(row(page, 101)).toBeVisible();
  expect(wire.reads.at(-1)).toMatchObject({ page: 2, size: PAGE_SIZE });
  await pages(page).getByRole("button", { name: "Previous page", exact: true }).click();
  await expect(row(page, 1)).toBeVisible();
  await page.goBack();
  await expect(row(page, 101)).toBeVisible();
  await page.goForward();
  await expect(row(page, 1)).toBeVisible();
  expect(wire.writes).toEqual([]);
});

test("paging preserves independent ZIP filters, archive population and global reminders", async ({ page }) => {
  const wire = await setup(page);
  await page.goto("/issue");
  await page.getByLabel("ZIP period from", { exact: true }).fill("2026-01-01");
  await page.getByLabel("ZIP period to", { exact: true }).fill("2026-10-31");
  await page.getByLabel("ZIP status", { exact: true }).selectOption("open");
  await pages(page).getByRole("button", { name: "Next page", exact: true }).click();
  await expect(row(page, 101)).toBeVisible();
  await expect(page.getByLabel("ZIP period from", { exact: true })).toHaveValue("2026-01-01");
  await expect(page.getByLabel("ZIP period to", { exact: true })).toHaveValue("2026-10-31");
  await expect(page.getByLabel("ZIP status", { exact: true })).toHaveValue("open");
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download PDFs (ZIP)", exact: true }).click();
  await download;
  expect(wire.zip).toEqual(["?from=2026-01-01&to=2026-10-31&status=open"]);
  expect(wire.reads.every(({ query }) => !/[?&](from|to|status)=/.test(query))).toBe(true);
  await expect(page.getByText(/ZIP covers numbered documents across the current workspace’s issuers/)).toBeVisible();
  await page.getByRole("button", { name: "Remind all overdue", exact: true }).click();
  await expect(page.getByText("Sent 0 reminders", { exact: true })).toBeVisible();
  expect(wire.writes).toEqual([{ method: "POST", path: "/issued/reminders/run", payload: null, key: undefined }]);
});

for (const count of [101, 0]) {
  test(`a successful out-of-range page clamps to the last page after the list shrinks to ${count}`, async ({ page }) => {
    const wire = await setup(page, 201);
    await page.goto("/issue?page=3&context=kept");
    await expect(row(page, 201)).toBeVisible();
    wire.rows = wire.rows.slice(0, count);
    await page.reload();
    const target = count ? 2 : 1;
    await expect(page).toHaveURL(new RegExp(`page=${target}&context=kept`));
    await expect(pages(page)).toContainText(`Page ${target} of ${target}`);
    if (count) {
      await expect(row(page, 101)).toBeVisible();
      await expect(page.getByText("No invoices issued yet.", { exact: true })).toHaveCount(0);
    } else {
      await expect(page.getByText("No invoices issued yet.", { exact: true })).toBeVisible();
      await expect(pages(page)).toContainText("Showing 0 of 0");
    }
    expect(wire.reads.at(-1)).toMatchObject({ page: target, size: PAGE_SIZE });
    expect(wire.writes).toEqual([]);
  });
}

for (const raw of ["0", "-1", "abc", "1.5", "9999999999999999999999999"]) {
  test(`an invalid page ${raw} reads the first bounded page`, async ({ page }) => {
    const wire = await setup(page);
    await page.goto(`/issue?page=${raw}&context=kept`);
    await expect(row(page, 1)).toBeVisible();
    await expect(pages(page)).toContainText("Page 1 of 2");
    expect(wire.reads.every(({ page: value, size }) => value === 1 && size === PAGE_SIZE)).toBe(true);
    expect(wire.writes).toEqual([]);
  });
}

for (const status of [403, 503]) {
  test(`page-two read ${status} keeps its URL and retries only that bounded GET`, async ({ page }) => {
    const wire = await setup(page);
    wire.failures.set(2, status);
    await page.goto("/issue?page=2");
    await expect(page.getByText("Couldn’t load the issued invoices", { exact: true })).toBeVisible();
    await expect(page).toHaveURL(/page=2/);
    await expect(page.getByRole("heading", { name: "Issued (0)", exact: true })).toHaveCount(0);
    await expect(page.getByText("No invoices issued yet.", { exact: true })).toHaveCount(0);
    expect(wire.reads.every(({ page: value, size }) => value === 2 && size === PAGE_SIZE)).toBe(true);
    wire.failures.delete(2);
    await page.getByRole("button", { name: "Try again", exact: true }).click();
    await expect(row(page, 101)).toBeVisible();
    await expect(pages(page)).toContainText("Page 2 of 2");
    expect(wire.reads.every(({ page: value }) => value === 2)).toBe(true);
    expect(wire.writes).toEqual([]);
  });
}

test("a late page-two response cannot replace page one after browser back", async ({ page }) => {
  const wire = await setup(page);
  let release = () => {};
  const wait = new Promise<void>((resolve) => { release = resolve; });
  const hold = { entered: false, release, wait };
  wire.holds.set(2, hold);
  try {
    await page.goto("/issue");
    await pages(page).getByRole("button", { name: "Next page", exact: true }).click();
    await expect.poll(() => hold.entered).toBe(true);
    await expect(row(page, 1)).toHaveCount(0);
    await page.goBack();
    await expect(row(page, 1)).toBeVisible();
    release();
    await expect(pages(page)).toContainText("Page 1 of 2");
    await expect(row(page, 101)).toHaveCount(0);
    expect(wire.writes).toEqual([]);
  } finally { release(); }
});

test("an unresolved saved cash intent survives paging without another payment write", async ({ page }) => {
  const wire = await setup(page);
  wire.lostCash = true;
  await page.goto("/issue");
  await row(page, 1).getByRole("button", { name: "Mark paid", exact: true }).click();
  await expect(row(page, 1).getByText(/Request outcome is unknown/)).toBeVisible();
  const saved = await page.evaluate(() => Object.entries(sessionStorage).filter(([key]) => key.includes("/issued/pagination-invoice-1/cash-command")));
  expect(saved).toHaveLength(1);
  await pages(page).getByRole("button", { name: "Next page", exact: true }).click();
  await expect(row(page, 101)).toBeVisible();
  await pages(page).getByRole("button", { name: "Previous page", exact: true }).click();
  await expect(row(page, 1).getByRole("button", { name: "Check previous payment", exact: true })).toBeVisible();
  expect(await page.evaluate(() => Object.entries(sessionStorage).filter(([key]) => key.includes("/issued/pagination-invoice-1/cash-command")))).toEqual(saved);
  expect(wire.writes).toHaveLength(1);
  expect(wire.writes[0].path).toBe("/issued/pagination-invoice-1/payments");
  await row(page, 1).getByRole("button", { name: "Check previous payment", exact: true }).click();
  await expect(row(page, 1).getByText(/No committed payment was found/)).toBeVisible();
  expect(wire.writes).toHaveLength(1);
});

test("an auditor can page through invoices without gaining payment or issuing permission", async ({ page }) => {
  const wire = await setup(page, 101, "auditor");
  await page.goto("/issue?page=2");
  await expect(row(page, 101)).toBeVisible();
  await expect(row(page, 101).getByRole("button", { name: "Mark paid", exact: true })).toBeDisabled();
  await expect(row(page, 101).getByRole("button", { name: "Credit", exact: true })).toBeDisabled();
  await expect(pages(page).getByRole("button", { name: "Previous page", exact: true })).toBeEnabled();
  expect(wire.writes).toEqual([]);
});

test("an empty current page with a positive count can refresh and clamp without claiming the workspace is empty", async ({ page }) => {
  const wire = await setup(page, 100);
  wire.totalOverride = 101;
  await page.goto("/issue?page=2");
  await expect(pages(page)).toContainText("Showing 0 of 101");
  await expect(page.getByText("No invoices issued yet.", { exact: true })).toHaveCount(0);
  await expect(page.getByText("This page has no invoices.", { exact: false })).toBeVisible();
  delete wire.totalOverride;
  await page.getByRole("button", { name: "Refresh this page", exact: true }).click();
  await expect(page).toHaveURL(/page=1/);
  await expect(row(page, 1)).toBeVisible();
  await expect(pages(page)).toContainText("Showing 1–100 of 100");
  expect(wire.writes).toEqual([]);
});

test("pagination wraps at390px with enlarged text and preserves keyboard navigation", async ({ page }, testInfo) => {
  const wire = await setup(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/issue?page=2");
  await expect(row(page, 101)).toBeVisible();
  await page.evaluate(() => { document.documentElement.style.fontSize = "32px"; });
  const nav = pages(page);
  await nav.scrollIntoViewIfNeeded();
  const bounds = await nav.boundingBox();
  expect(bounds).not.toBeNull();
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(390);
  await nav.screenshot({ path: testInfo.outputPath("pagination-390-text32.png") });
  const previous = nav.getByRole("button", { name: "Previous page", exact: true });
  await previous.focus();
  await expect(previous).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(row(page, 1)).toBeVisible();
  expect(wire.writes).toEqual([]);
});

test("paging and reload preserve the existing project final-invoice prefill context", async ({ page }) => {
  const wire = await setup(page);
  await page.goto("/issue");
  await expect(pages(page)).toContainText("Page 1 of 2");
  await page.evaluate(() => {
    history.replaceState({ ...history.state, usr: { finalInvoice: {
      project_id: "synthetic-won-project", lines: [{ description: "Reviewed project remainder", quantity: "1", unit_price: "123.45" }],
    } } }, "", location.href);
  });
  await page.reload();
  await expect(page.getByLabel("Line 1 description", { exact: true })).toHaveValue("Reviewed project remainder");
  await pages(page).getByRole("button", { name: "Next page", exact: true }).click();
  await expect(row(page, 101)).toBeVisible();
  await page.reload();
  await expect(row(page, 101)).toBeVisible();
  await expect(page.getByLabel("Line 1 description", { exact: true })).toHaveValue("Reviewed project remainder");
  await expect(page.getByLabel("Line 1 unit price", { exact: true })).toHaveValue("123.45");
  wire.rows = wire.rows.slice(0, 100);
  await page.reload();
  await expect(pages(page)).toContainText("Page 1 of 1");
  await expect(page.getByLabel("Line 1 description", { exact: true })).toHaveValue("Reviewed project remainder");
  await expect(page.getByLabel("Line 1 unit price", { exact: true })).toHaveValue("123.45");
  expect(wire.writes).toEqual([]);
});
