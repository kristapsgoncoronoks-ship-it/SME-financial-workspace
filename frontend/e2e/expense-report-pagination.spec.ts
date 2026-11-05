import { test, expect, type Page } from "@playwright/test";

// Routed synthetic populations qualify browser state, not actual API/DB behavior.
const ORG = { id: "report-pages-org-A", name: "Synthetic report pages", status: "active" };
const USER = { id: "report-pages-user-A", org_id: ORG.id, name: "Synthetic report owner", email: "pages@example.test", role: "owner" };
type Kind = "mine" | "pending";
function report(index: number, kind: Kind = "mine", account = "A") {
  return { id: `${account}-${kind}-${index}`, employee_id: `report-pages-user-${account}`, employee_name: `Synthetic employee ${account}`,
    title: `${account} ${kind} report ${index}`, status: kind === "mine" ? "draft" : "submitted", currency: index === 201 ? "USD" : "EUR",
    total: "123.45", vat_total: "23.45", total_eur: index === 201 ? null : "123.45", submitted_at: kind === "mine" ? null : "2026-11-01T00:00:00Z", created_at: "2026-11-01T00:00:00Z", version: 1 };
}
function held() { let release!: () => void; const wait = new Promise<void>(resolve => { release = resolve; }); return { entered: false, finished: false, release, wait }; }
type Wire = { rows: Record<Kind, ReturnType<typeof report>[]>; reads: { kind: Kind; page: number; size: number; account: string; status: string | null; mine: string | null }[];
  writes: { path: string; method: string; body: unknown; key?: string; user?: string; org?: string }[]; holds: Map<string, ReturnType<typeof held>>; failures: Map<string, number>; empty: Set<string>; malformed: Set<string>; lost: Set<string>; detailReads: string[]; manualReport?: ReturnType<typeof report>; postHold?: ReturnType<typeof held> };
const section = (page: Page, kind: Kind) => page.getByRole("region", { name: kind === "mine" ? "My reports" : "Awaiting my approval", exact: true });
const pages = (page: Page, kind: Kind) => section(page, kind).getByRole("navigation", { name: kind === "mine" ? "My reports pages" : "Awaiting my approval pages", exact: true });

async function setup(page: Page, options: { mine?: number; pending?: number; manager?: boolean; url?: string; module?: boolean; scopeChange?: "account" | "org" } = {}) {
  const wire: Wire = { rows: { mine: Array.from({ length: options.mine ?? 201 }, (_, i) => report(i + 1)), pending: Array.from({ length: options.pending ?? 101 }, (_, i) => report(i + 1, "pending")) }, reads: [], writes: [], holds: new Map(), failures: new Map(), empty: new Set(), malformed: new Set(), lost: new Set(), detailReads: [] };
  await page.addInitScript(() => { if (!sessionStorage.getItem("report-pages-seeded")) { localStorage.setItem("invoiceiq_token", "report-pages-A"); sessionStorage.setItem("report-pages-seeded", "1"); } });
  await page.route("**/api/v1/**", async route => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname.replace("/api/v1", "");
    const account = request.headers().authorization === "Bearer report-pages-B" ? "B" : "A";
    const org = account === "A" ? ORG : { ...ORG, id: "report-pages-org-B", name: "Replacement report workspace" };
    const user = account === "A" ? USER : { ...USER, id: options.scopeChange === "org" ? USER.id : "report-pages-user-B", org_id: org.id };
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (request.method() !== "GET") wire.writes.push({ path, method: request.method(), body: request.postDataJSON(), key: request.headers()["idempotency-key"], user: request.headers()["x-expected-user-id"], org: request.headers()["x-expected-org-id"] });
    if (path === "/auth/me") return json({ user, organization: org, permissions: ["expense.read", "expense.write", ...(options.manager === false ? [] : ["expense.approve"])] });
    if (path === "/auth/organizations") return json([{ org_id: org.id, name: org.name, current: true }]);
    if (path === "/modules") return json([{ key: "expenses", name: "Expenses", enabled: options.module !== false, ready: true, requires_issuer: false }]);
    if (path === "/expenses/summary") return json({ my_reimbursable: "0", reclaimable_vat: "0", my_submitted: 0, pending_approvals: wire.rows.pending.length });
    if (path === "/expenses/transactions") return json([{ id: "selected-transaction", version: 7, import_batch_id: "synthetic-import", currency_source: "statement", txn_date: "2026-11-01", description: "Synthetic card train", merchant: "Synthetic rail", amount: "10.25", currency: "EUR", direction: "debit", source: "bank_statement", status: "available" }]);
    if (path === "/expenses" && request.method() === "POST") {
      if (wire.postHold) { wire.postHold.entered = true; await wire.postHold.wait; }
      const manual = Array.isArray((request.postDataJSON() as Record<string, unknown>).items);
      const saved = manual
        ? { ...report(202), title: "Synthetic manual page report", total: "12.34", vat_total: "2.34", total_eur: "12.34" }
        : { ...report(202), title: "Synthetic selected report", total: "10.25", vat_total: "0.00", total_eur: "10.25" };
      if (manual) wire.manualReport = saved;
      wire.rows.mine.unshift(saved);
      return json(saved, 201);
    }
    if (wire.manualReport && path === `/expenses/${wire.manualReport.id}`) {
      wire.detailReads.push(path);
      return json(wire.manualReport);
    }
    if (path === "/expenses" && request.method() === "GET") {
      const kind: Kind = url.searchParams.get("mine") === "true" ? "mine" : "pending", currentPage = Number(url.searchParams.get("page") ?? 1), size = Number(url.searchParams.get("page_size") ?? 100), key = `${account}:${kind}:${currentPage}`;
      wire.reads.push({ kind, page: currentPage, size, account, status: url.searchParams.get("status"), mine: url.searchParams.get("mine") });
      const hold = wire.holds.get(key); if (hold) { hold.entered = true; await hold.wait; }
      const reply = async (body: unknown, status = 200) => { try { return await json(body, status); } finally { if (hold) hold.finished = true; } };
      if (wire.lost.has(key)) return route.abort("failed");
      if (wire.failures.has(key)) return reply({ detail: "Synthetic report page unavailable" }, wire.failures.get(key));
      const rows = account === "A" ? wire.rows[kind] : (kind === "mine" ? [report(1, kind, "B"), report(2, kind, "B")] : []);
      const items = wire.empty.has(key) ? [] : rows.slice((currentPage - 1) * size, currentPage * size);
      return reply({ items, total: wire.malformed.has(key) ? "unknown" : rows.length });
    }
    return json([]);
  });
  await page.goto(options.url ?? "/expenses?context=preserved");
  await expect(page.getByRole("heading", { level: 1, name: "Expenses", exact: true })).toBeVisible();
  return wire;
}

test("201 own reports and101 approval reports are independently reachable with exact bounded reads", async ({ page }) => {
  const wire = await setup(page);
  await expect(pages(page, "mine")).toContainText("Showing 1–100 of 201");
  await expect(pages(page, "pending")).toContainText("Showing 1–100 of 101");
  await pages(page, "mine").getByRole("button", { name: "Next page", exact: true }).click();
  await expect(pages(page, "mine")).toContainText("Showing 101–200 of 201");
  await expect(section(page, "mine").getByRole("link", { name: "A mine report 101", exact: true })).toHaveAttribute("href", "/expenses/A-mine-101");
  await expect(pages(page, "pending")).toContainText("Showing 1–100 of 101");
  await pages(page, "pending").getByRole("button", { name: "Next page", exact: true }).click();
  await expect(section(page, "pending").getByRole("link", { name: "A pending report 101", exact: true })).toBeVisible();
  await expect(pages(page, "pending")).toContainText("Showing 101–101 of 101");
  await pages(page, "mine").getByRole("button", { name: "Next page", exact: true }).click();
  await expect(pages(page, "mine")).toContainText("Showing 201–201 of 201");
  await expect(section(page, "mine").getByRole("link", { name: "A mine report 201", exact: true })).toBeVisible();
  await expect(section(page, "mine").getByRole("cell", { name: "US$123.45", exact: true })).toBeVisible();
  await expect(page).toHaveURL(/context=preserved&my_page=3&approval_page=2/);
  expect(wire.reads.map(({ kind, page }) => [kind, page]).sort()).toEqual([["mine", 1], ["mine", 2], ["mine", 3], ["pending", 1], ["pending", 2]].sort());
  expect(wire.reads.every(read => read.size === 100 && (read.kind === "mine" ? read.mine === "true" && read.status === null : read.status === "submitted" && read.mine === null))).toBe(true);
  expect(wire.writes).toEqual([]);
});

test("paging preserves unsaved manual fields, bank import fields and selected transactions", async ({ page }) => {
  const wire = await setup(page);
  await page.getByLabel("Statement account reference", { exact: true }).fill("Synthetic operating card");
  await page.getByLabel("Currency if missing from the statement", { exact: true }).fill("EUR");
  await page.getByRole("checkbox", { name: "Select Synthetic card train", exact: true }).check();
  await page.getByRole("button", { name: "+ New expense report (manual)", exact: true }).click();
  await page.getByLabel("Title", { exact: true }).fill("Unsaved report kept while browsing");
  await page.getByLabel("Item 1 description", { exact: true }).fill("Synthetic unsaved expense");
  await page.getByLabel("Item 1 amount", { exact: true }).fill("12.34");
  await pages(page, "mine").getByRole("button", { name: "Next page", exact: true }).click();
  await expect(pages(page, "mine")).toContainText("Showing 101–200 of 201");
  await pages(page, "pending").getByRole("button", { name: "Next page", exact: true }).click();
  await expect(pages(page, "pending")).toContainText("Showing 101–101 of 101");
  await expect(page.getByLabel("Title", { exact: true })).toHaveValue("Unsaved report kept while browsing");
  await expect(page.getByLabel("Item 1 description", { exact: true })).toHaveValue("Synthetic unsaved expense");
  await expect(page.getByLabel("Item 1 amount", { exact: true })).toHaveValue("12.34");
  await expect(page.getByLabel("Statement account reference", { exact: true })).toHaveValue("Synthetic operating card");
  await expect(page.getByLabel("Currency if missing from the statement", { exact: true })).toHaveValue("EUR");
  await expect(page.getByRole("checkbox", { name: "Select Synthetic card train", exact: true })).toBeChecked();
  expect(wire.writes).toEqual([]);
});

test("paging while a selected-report command is pending preserves its single key scope and exact body", async ({ page }) => {
  const wire = await setup(page), hold = held(); wire.postHold = hold;
  await page.getByRole("checkbox", { name: "Select Synthetic card train", exact: true }).check();
  await page.getByLabel("Report title", { exact: true }).fill("Synthetic selected report");
  try {
    await page.getByRole("button", { name: "Create report from 1 · €10.25", exact: true }).click();
    await expect.poll(() => hold.entered).toBe(true);
    await pages(page, "mine").getByRole("button", { name: "Next page", exact: true }).click();
    await expect(pages(page, "mine")).toContainText("Showing 101–200 of 201");
    await pages(page, "pending").getByRole("button", { name: "Next page", exact: true }).click();
    await expect(pages(page, "pending")).toContainText("Showing 101–101 of 101");
    await expect(page.getByRole("button", { name: "Creating…", exact: true })).toBeDisabled();
    expect(wire.writes).toHaveLength(1);
    expect(wire.writes[0]).toMatchObject({ path: "/expenses", method: "POST", user: USER.id, org: ORG.id, body: {
      title: "Synthetic selected report", currency: "EUR", transaction_ids: ["selected-transaction"], transaction_versions: { "selected-transaction": 7 },
    } });
    expect(wire.writes[0].key).toMatch(/^[0-9a-f-]{36}$/);
    hold.release();
    await expect(page.getByRole("status").filter({ hasText: "Report saved: Synthetic selected report (EUR 10.25)." })).toBeVisible();
    await expect(pages(page, "mine")).toContainText("Showing 101–200 of 202");
    await expect(page.getByRole("checkbox", { name: "Select Synthetic card train", exact: true })).not.toBeChecked();
    expect(wire.writes).toHaveLength(1);
    await expect(page).toHaveURL(/context=preserved&my_page=2&approval_page=2/);
  } finally { hold.release(); }
});

for (const lostRead of [false, true]) test(`manual page2 save ${lostRead ? "with a lost list read offers GET-only recovery" : "refreshes the active page and count"}`, async ({ page }, info) => {
  const wire = await setup(page, { url: "/expenses?my_page=2&approval_page=2&context=preserved" });
  await expect(pages(page, "mine")).toContainText("Showing 101–200 of 201");
  await expect(pages(page, "pending")).toContainText("Showing 101–101 of 101");
  await page.getByLabel("Statement account reference", { exact: true }).fill("Synthetic retained import account");
  await page.getByLabel("Currency if missing from the statement", { exact: true }).fill("USD");
  await page.getByRole("checkbox", { name: "Select Synthetic card train", exact: true }).check();
  await page.getByLabel("Report title", { exact: true }).fill("Unsaved selected-report title");
  await page.getByRole("button", { name: "+ New expense report (manual)", exact: true }).click();
  await page.getByLabel("Title", { exact: true }).fill("Synthetic manual page report");
  await page.getByLabel("Item 1 spend date", { exact: true }).fill("2026-11-01");
  await page.getByLabel("Item 1 description", { exact: true }).fill("Synthetic manual train fare");
  await page.getByLabel("Item 1 amount", { exact: true }).fill("12.34");
  await page.getByLabel("Item 1 VAT", { exact: true }).fill("2.34");
  if (lostRead) wire.lost.add("A:mine:2");
  const ready = page.getByText("The current report and totals have loaded. Saving only created a draft; submission and payment are separate actions.", { exact: true });
  try {
    expect(wire.reads.filter(read => read.kind === "mine")).toHaveLength(1);
    await page.getByRole("button", { name: "Save draft", exact: true }).click();
    await expect(page.getByRole("link", { name: "Open saved draft", exact: true })).toHaveAttribute("href", "/expenses/A-mine-202");
    expect(wire.writes).toHaveLength(1);
    expect(wire.writes[0]).toMatchObject({ path: "/expenses", method: "POST", user: USER.id, org: ORG.id, body: {
      title: "Synthetic manual page report", currency: "EUR", items: [{ spend_date: "2026-11-01", category: "travel", description: "Synthetic manual train fare", amount: "12.34", vat_amount: "2.34", payment_method: "personal", comment: "" }],
    } });
    expect(wire.writes[0].key).toMatch(/^[0-9a-f-]{36}$/);
    const originalWrite = JSON.stringify(wire.writes[0]);
    if (lostRead) {
      await expect(page.getByRole("button", { name: "Retry loading saved report", exact: true })).toBeVisible();
      await expect(page.getByRole("alert").filter({ hasText: "The manual draft was saved, but its current report or totals could not load." })).toBeVisible();
      await expect(ready).toHaveCount(0);
      await expect(pages(page, "mine")).toContainText("Page 2 could not be loaded.");
      expect(wire.rows.mine).toHaveLength(202);
      expect(wire.reads.filter(read => read.kind === "mine").length).toBeGreaterThan(1);
      const failedReads = wire.reads.length;
      wire.lost.delete("A:mine:2");
      await page.getByRole("button", { name: "Retry loading saved report", exact: true }).click();
      await expect.poll(() => wire.reads.length).toBeGreaterThan(failedReads);
    }
    await expect(pages(page, "mine")).toContainText("Showing 101–200 of 202");
    await expect(ready).toBeVisible();
    expect(wire.reads.filter(read => read.kind === "mine").length).toBeGreaterThan(1);
    expect(wire.reads.every(read => read.page === 2)).toBe(true);
    expect(wire.reads.filter(read => read.kind === "pending")).toHaveLength(1);
    expect(wire.detailReads.length).toBe(lostRead ? 2 : 1);
    expect(wire.writes).toHaveLength(1);
    expect(JSON.stringify(wire.writes[0])).toBe(originalWrite);
    await expect(page.getByLabel("Report title", { exact: true })).toHaveValue("Unsaved selected-report title");
    await expect(page.getByLabel("Statement account reference", { exact: true })).toHaveValue("Synthetic retained import account");
    await expect(page.getByLabel("Currency if missing from the statement", { exact: true })).toHaveValue("USD");
    await expect(page.getByRole("checkbox", { name: "Select Synthetic card train", exact: true })).toBeChecked();
    await expect(page).toHaveURL(/my_page=2&approval_page=2&context=preserved/);
  } finally {
    await info.attach("manual-page-readback-wire", { body: JSON.stringify({ reads: wire.reads, writes: wire.writes, detailReads: wire.detailReads, total: wire.rows.mine.length, readyVisible: await ready.isVisible() }, null, 2), contentType: "application/json" });
  }
});

for (const status of [403, 503]) test(`a failed page${status} remains scoped and explicit recovery only reads that page`, async ({ page }) => {
  const wire = await setup(page); wire.failures.set("A:mine:2", status);
  await pages(page, "mine").getByRole("button", { name: "Next page", exact: true }).click();
  await expect(pages(page, "mine")).toContainText("Page 2 could not be loaded.");
  await expect(section(page, "mine").getByRole("alert")).toContainText("Couldn’t load expense reports");
  await expect(section(page, "mine").getByText("No expense reports yet", { exact: true })).toHaveCount(0);
  await expect(pages(page, "pending")).toContainText("Showing 1–100 of 101");
  await expect(page).toHaveURL(/context=preserved&my_page=2/);
  const writes = wire.writes.length; wire.failures.delete("A:mine:2");
  await section(page, "mine").getByRole("button", { name: "Try again", exact: true }).click();
  await expect(pages(page, "mine")).toContainText("Showing 101–200 of 201");
  expect(wire.writes.length).toBe(writes);
  expect(wire.reads.filter(read => read.kind === "mine").every(read => read.page === 1 || read.page === 2)).toBe(true);
});

test("a late page response cannot replace a restored first page after browser back", async ({ page }) => {
  const wire = await setup(page), hold = held(); wire.holds.set("A:mine:2", hold);
  try {
    await pages(page, "mine").getByRole("button", { name: "Next page", exact: true }).click();
    await expect.poll(() => hold.entered).toBe(true);
    await expect(pages(page, "mine").getByRole("button", { name: "Next page", exact: true })).toBeDisabled();
    await page.goBack();
    await expect(pages(page, "mine")).toContainText("Showing 1–100 of 201");
    hold.release();
    await expect.poll(() => hold.finished).toBe(true);
    await expect(section(page, "mine").getByRole("link", { name: "A mine report 1", exact: true })).toBeVisible();
    await expect(section(page, "mine").getByRole("link", { name: "A mine report 101", exact: true })).toHaveCount(0);
    await expect(page).toHaveURL(/expenses\?context=preserved$/);
    expect(wire.writes).toEqual([]);
  } finally { hold.release(); }
});

test("shrinking a population replaces an out-of-range URL and loads the remaining last page", async ({ page }) => {
  const wire = await setup(page, { url: "/expenses?my_page=3&approval_page=2&context=preserved" });
  await expect(pages(page, "mine")).toContainText("Showing 201–201 of 201");
  wire.rows.mine.length = 101;
  await page.reload();
  await expect(pages(page, "mine")).toContainText("Showing 101–101 of 101");
  await expect(page).toHaveURL(/my_page=2&approval_page=2&context=preserved/);
  await expect(section(page, "mine").getByText("No expense reports yet", { exact: true })).toHaveCount(0);
  await expect(pages(page, "pending")).toContainText("Showing 101–101 of 101");
  expect(wire.writes).toEqual([]);
});

for (const raw of ["0", "-1", "1.5", "bad", "999999999999999999999999"]) test(`invalid own-report page ${raw} reads and normalizes only page1`, async ({ page }) => {
  const wire = await setup(page, { url: `/expenses?my_page=${raw}&approval_page=2&context=preserved` });
  await expect(pages(page, "mine")).toContainText("Showing 1–100 of 201");
  await expect(page).toHaveURL(/my_page=1&approval_page=2&context=preserved/);
  expect(wire.reads.filter(read => read.kind === "mine").map(read => read.page)).toEqual([1]);
  expect(wire.writes).toEqual([]);
});

test("an employee pages their own reports without fetching the approval population", async ({ page }) => {
  const wire = await setup(page, { manager: false });
  await expect(pages(page, "mine")).toContainText("Showing 1–100 of 201");
  await expect(section(page, "pending")).toHaveCount(0);
  await pages(page, "mine").getByRole("button", { name: "Next page", exact: true }).click();
  await expect(pages(page, "mine")).toContainText("Showing 101–200 of 201");
  expect(wire.reads.every(read => read.kind === "mine")).toBe(true);
  expect(wire.writes).toEqual([]);
});

test("an empty workspace is distinct from an empty page with a positive count", async ({ page }) => {
  const wire = await setup(page, { mine: 0, pending: 0 });
  await expect(section(page, "mine").getByText("No expense reports yet", { exact: true })).toBeVisible();
  await expect(section(page, "pending").getByText("Nothing awaiting your approval", { exact: true })).toBeVisible();
  await expect(pages(page, "mine").getByRole("button", { name: "Next page", exact: true })).toBeDisabled();
  expect(wire.writes).toEqual([]);
});

test("an empty current page with a positive count offers bounded read recovery", async ({ page }) => {
  const wire = await setup(page); wire.empty.add("A:mine:2");
  await pages(page, "mine").getByRole("button", { name: "Next page", exact: true }).click();
  await expect(section(page, "mine").getByText("No reports on this page", { exact: true })).toBeVisible();
  await expect(pages(page, "mine")).toContainText("Showing 0 of 201");
  await expect(section(page, "mine").getByText("No expense reports yet", { exact: true })).toHaveCount(0);
  wire.empty.delete("A:mine:2");
  await section(page, "mine").getByRole("button", { name: "Refresh reports", exact: true }).click();
  await expect(pages(page, "mine")).toContainText("Showing 101–200 of 201");
  expect(wire.writes).toEqual([]);
});

test("an invalid page count is a read error and cannot invent an empty workspace", async ({ page }) => {
  const wire = await setup(page); wire.malformed.add("A:mine:2");
  await pages(page, "mine").getByRole("button", { name: "Next page", exact: true }).click();
  await expect(pages(page, "mine")).toContainText("Page 2 could not be loaded.");
  await expect(section(page, "mine").getByRole("alert")).toContainText("Couldn’t load expense reports");
  await expect(section(page, "mine").getByText("No expense reports yet", { exact: true })).toHaveCount(0);
  wire.malformed.delete("A:mine:2");
  await section(page, "mine").getByRole("button", { name: "Try again", exact: true }).click();
  await expect(pages(page, "mine")).toContainText("Showing 101–200 of 201");
  expect(wire.writes).toEqual([]);
});

test("a module-disabled page does not start either report read", async ({ page }) => {
  const wire = await setup(page, { module: false });
  await expect(page.getByText("The employee expenses module isn't active.", { exact: false })).toBeVisible();
  await expect(section(page, "mine")).toHaveCount(0);
  expect(wire.reads).toEqual([]); expect(wire.writes).toEqual([]);
});

test("URL context and router state survive report navigation", async ({ page }) => {
  await page.addInitScript(() => history.replaceState({ ...history.state, usr: { source: "synthetic-report-context", returnTo: "/expenses" } }, ""));
  const wire = await setup(page);
  await pages(page, "mine").getByRole("button", { name: "Next page", exact: true }).click();
  await expect(pages(page, "mine")).toContainText("Showing 101–200 of 201");
  expect(await page.evaluate(() => history.state.usr)).toEqual({ source: "synthetic-report-context", returnTo: "/expenses" });
  await expect(page).toHaveURL(/context=preserved&my_page=2/);
  expect(wire.writes).toEqual([]);
});

test("settled paging does not steal focus from a draft field edited while the read was pending", async ({ page }) => {
  const wire = await setup(page), hold = held(); wire.holds.set("A:mine:2", hold);
  await page.getByRole("button", { name: "+ New expense report (manual)", exact: true }).click();
  try {
    const next = pages(page, "mine").getByRole("button", { name: "Next page", exact: true });
    await next.focus(); await page.keyboard.press("Enter");
    await expect.poll(() => hold.entered).toBe(true);
    const title = page.getByLabel("Title", { exact: true });
    await title.fill("Draft focus belongs to the operator");
    hold.release();
    await expect(pages(page, "mine")).toContainText("Showing 101–200 of 201");
    await expect(title).toBeFocused();
    await expect(title).toHaveValue("Draft focus belongs to the operator");
    expect(wire.writes).toEqual([]);
  } finally { hold.release(); }
});

test("the bounded page read aborts on its deadline while the other table stays usable", async ({ page }) => {
  const wire = await setup(page), hold = held(); wire.holds.set("A:mine:2", hold);
  try {
    const failed = page.waitForEvent("requestfailed", { predicate: request => new URL(request.url()).searchParams.get("mine") === "true" && new URL(request.url()).searchParams.get("page") === "2" });
    await pages(page, "mine").getByRole("button", { name: "Next page", exact: true }).click();
    await expect.poll(() => hold.entered).toBe(true);
    await failed;
    wire.holds.delete("A:mine:2"); wire.failures.set("A:mine:2", 403);
    await expect(pages(page, "mine")).toContainText("Page 2 could not be loaded.");
    await expect(pages(page, "pending")).toContainText("Showing 1–100 of 101");
    expect(wire.writes).toEqual([]);
  } finally { hold.release(); }
});

for (const scopeChange of ["account", "org"] as const) test(`a late report page cannot enter the replacement ${scopeChange} scope`, async ({ page }) => {
  const wire = await setup(page, { scopeChange }), hold = held(); wire.holds.set("A:mine:2", hold);
  try {
    await pages(page, "mine").getByRole("button", { name: "Next page", exact: true }).click();
    await expect.poll(() => hold.entered).toBe(true);
    await page.evaluate(() => { const oldValue = localStorage.getItem("invoiceiq_token"); localStorage.setItem("invoiceiq_token", "report-pages-B"); window.dispatchEvent(new StorageEvent("storage", { key: "invoiceiq_token", oldValue, newValue: "report-pages-B", storageArea: localStorage })); });
    await expect(section(page, "mine").getByRole("link", { name: "B mine report 1", exact: true })).toBeVisible();
    hold.release();
    await expect.poll(() => hold.finished).toBe(true);
    await expect(pages(page, "mine")).toContainText("Showing 1–2 of 2");
    await expect(section(page, "mine").getByRole("link", { name: /A mine report/ })).toHaveCount(0);
    expect(wire.writes).toEqual([]);
  } finally { hold.release(); }
});

for (const [width, font] of [[320, 16], [390, 32], [1280, 16]]) test(`report paging stays operable at ${width}px and root text ${font}px`, async ({ page }) => {
  await page.setViewportSize({ width, height: 900 });
  const wire = await setup(page);
  await page.addStyleTag({ content: `html { font-size: ${font}px !important; }` });
  const next = pages(page, "mine").getByRole("button", { name: "Next page", exact: true });
  await next.focus(); await page.keyboard.press("Enter");
  await expect(pages(page, "mine")).toContainText("Showing 101–200 of 201");
  await expect(next).toBeFocused();
  const previous = pages(page, "mine").getByRole("button", { name: "Previous page", exact: true });
  await previous.focus(); await page.keyboard.press("Enter");
  await expect(pages(page, "mine")).toContainText("Showing 1–100 of 201");
  await expect(next).toBeFocused();
  const bounds = await pages(page, "mine").evaluate(element => {
    const box = element.getBoundingClientRect(); return { width: box.width, scroll: element.scrollWidth, viewport: innerWidth, left: box.left, right: box.right };
  });
  expect(bounds.left).toBeGreaterThanOrEqual(0); expect(bounds.right).toBeLessThanOrEqual(bounds.viewport);
  expect(bounds.scroll).toBeLessThanOrEqual(Math.ceil(bounds.width));
  await section(page, "mine").screenshot({ path: test.info().outputPath(`reports-${width}-root${font}.png`) });
  expect(wire.writes).toEqual([]);
});
