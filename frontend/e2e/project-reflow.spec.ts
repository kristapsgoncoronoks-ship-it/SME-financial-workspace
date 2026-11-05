import { expect, test, type Locator, type Page, type TestInfo } from "@playwright/test";
import type { ProjectPnl } from "../src/lib/types";

// Synthetic API responses exercise the real routed Project page and shell.
// Bounds and keyboard access are measured in Chromium; these are not device,
// provider, database, or complete WCAG conformance tests.
const PROJECT = { id: "project-reflow", code: "REFLOW-23", name: "Project with a long descriptive contract name", status: "active", version: 1, customer_id: null };
const LONG_LABEL = "SyntheticUnbrokenProjectEvidenceLabel".repeat(3);
const LONG_MONEY = "123456789012345678.90";
const COST_LABEL = `${LONG_LABEL}-manual-cost`;
const DOCUMENT = `${LONG_LABEL}-signed-contract.pdf`;
const OFFER_TITLE = `${LONG_LABEL}-offer`;
const PLAN_LABEL = `${LONG_LABEL}-instalment`;
const SOURCE_NUMBER = `${LONG_LABEL}-source`;
const PNL: ProjectPnl = {
  project_id: PROJECT.id, code: PROJECT.code, name: PROJECT.name, status: "active",
  revenue: LONG_MONEY, credited: "12345678.90", costs: "9876543210987654.32",
  invoice_costs: "8765432109876543.21", expense_costs: "123456789012345.67", manual_costs: "987654321098765.44",
  profit: "113580245801358024.58", margin_pct: "92.00000001", basis: "net_eur_live",
  basis_version: 2, reporting_currency: "EUR", complete: true, issues: [], fx_trace: [], legacy_figures: {},
  adjustments_complete: true, adjustments: {}, pnl_frozen_at: null, estimated_revenue: "234567890123456789.01",
  accepted_at: null, accepted_by: null, acceptance_document_id: null, acceptance_note: null,
};
type Write = { method: string; path: string; body: unknown };
type Reply = { status: number; body: unknown };
type Fixture = {
  pnl?: Partial<ProjectPnl>;
  readPnl?: () => Promise<Reply>;
  write?: (request: Write) => Promise<Reply>;
  source?: () => Promise<Reply>;
};
const observed = new WeakMap<Page, { errors: string[]; writes: Write[]; expected: Write[] }>();

test.beforeEach(async ({ page }) => {
  observed.set(page, { errors: [], writes: [], expected: [] });
  page.on("pageerror", (error) => observed.get(page)!.errors.push(error.message));
});
test.afterEach(async ({ page }, testInfo) => {
  if (testInfo.status !== testInfo.expectedStatus) await evidence(page, testInfo, "failure");
  const state = observed.get(page)!;
  expect(state.errors, "the rendered journey must not conceal a page crash").toEqual([]);
  expect(state.writes, "only the explicitly submitted action may write").toEqual(state.expected);
});

function held() {
  let release!: () => void;
  const promise = new Promise<void>((resolve) => { release = resolve; });
  return { promise, release };
}

async function fixture(page: Page, options: Fixture = {}) {
  await page.addInitScript(() => localStorage.setItem("invoiceiq_token", "synthetic-project-reflow-token"));
  const org = { id: "org-reflow", name: "Synthetic reflow workspace", status: "active" };
  const user = { id: "user-reflow", email: "reader@example.test", name: "Reader", role: "owner", org_id: org.id, is_platform_admin: false };
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname.replace(/^.*\/api\/v1/, "");
    const method = route.request().method();
    const fulfill = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (method !== "GET") {
      const request = { method, path, body: route.request().postDataJSON() };
      observed.get(page)!.writes.push(request);
      const reply = options.write ? await options.write(request) : { status: 500, body: { detail: "Unexpected synthetic write" } };
      return fulfill(reply.body, reply.status);
    }
    if (path === "/auth/me") return fulfill({ user, organization: org, permissions: ["invoice.read", "settings.manage"] });
    // Membership rows use org_id/name/current, as returned by the real endpoint.
    if (path === "/auth/organizations") return fulfill([{ org_id: org.id, name: org.name, role: user.role, current: true }]);
    if (path === "/modules") return fulfill([{ key: "issuing", enabled: true, available: true, core: false, name: "Issuing", ready: true, requires_issuer: true }]);
    if (path === `/masters/projects/${PROJECT.id}/pnl`) {
      if (options.readPnl) { const reply = await options.readPnl(); return fulfill(reply.body, reply.status); }
      return fulfill({ ...PNL, ...options.pnl });
    }
    if (path === `/masters/projects/${PROJECT.id}/cost-entries`) return fulfill([{ id: "cost-reflow", label: COST_LABEL, category: "equipment", amount: LONG_MONEY, currency: "EUR", entry_date: null, note: null, created_by: user.email, created_at: "2026-10-01T09:00:00Z" }]);
    if (path === `/masters/projects/${PROJECT.id}/documents`) return fulfill([{ id: "document-reflow", filename: DOCUMENT, kind: "contract", content_type: "application/pdf", uploaded_by: user.email, created_at: "2026-10-01T09:00:00Z", shared_with_customer: false }]);
    if (path === `/masters/projects/${PROJECT.id}/offers`) return fulfill([{ id: "offer-reflow", number: "OFFER-23", version: 1, title: OFFER_TITLE, status: "sent", total: LONG_MONEY, currency: "EUR", lines: [], note: null, created_by: user.email, created_at: "2026-10-01T09:00:00Z" }]);
    if (path === `/masters/projects/${PROJECT.id}/invoicing-plan`) return fulfill({ project_id: PROJECT.id, rows: [{ id: "plan-reflow", label: PLAN_LABEL, amount: LONG_MONEY, position: 0 }], contracted_total: "234567890123456789.01", issued_total: LONG_MONEY, remaining: "111111101111111110.11", basis_version: 2, reporting_currency: "EUR", complete: true, issues: [] });
    if (path === `/issued/source-reflow/pdf` && options.source) { const reply = await options.source(); return fulfill(reply.body, reply.status); }
    if (path === "/masters/projects") return fulfill([PROJECT]);
    if (path === "/customers") return fulfill([{ id: "customer-reflow", name: `${LONG_LABEL}-customer`, email: "customer@example.test" }]);
    if (path === "/templates") return fulfill({ own: [{ id: "template-reflow", name: `${LONG_LABEL}-template`, active: true }], platform: [] });
    if (path.startsWith("/issuer/registry")) return fulfill([]);
    if (path.startsWith("/issuer")) return fulfill({ legal_name: "Synthetic organisation", is_complete: true });
    if (path.startsWith("/issued")) return fulfill({ items: [], total: 0 });
    return fulfill([]);
  });
}

function card(page: Page, name: string) {
  return page.locator(".card").filter({ has: page.getByRole("heading", { name, exact: true }) });
}
async function open(page: Page, width = 390, fontSize = 32) {
  await page.setViewportSize({ width, height: 844 });
  await page.goto(`/projects/${PROJECT.id}`);
  await page.evaluate((size) => { document.documentElement.style.fontSize = `${size}px`; }, fontSize);
  await expect(page.getByRole("heading", { name: `${PROJECT.code} · ${PROJECT.name}`, exact: true })).toBeVisible();
}

async function fitsPage(page: Page) {
  const bounds = await page.evaluate(() => ({ viewport: innerWidth, body: document.body.scrollWidth, root: document.documentElement.scrollWidth }));
  expect(bounds.body, `body must reflow within ${bounds.viewport}px`).toBeLessThanOrEqual(bounds.viewport + 1);
  expect(bounds.root, `document must reflow within ${bounds.viewport}px`).toBeLessThanOrEqual(bounds.viewport + 1);
}

// Range rectangles measure the rendered text, including separate wrapped lines.
// A visible ancestor alone would miss text spilling into a neighbouring metric.
async function readableText(locator: Locator, container: Locator) {
  await expect(locator).toBeVisible();
  const outer = await container.boundingBox();
  expect(outer).not.toBeNull();
  const lines = await locator.evaluate((element) => {
    const range = document.createRange(); range.selectNodeContents(element);
    return [...range.getClientRects()].filter((rect) => rect.width > 0 && rect.height > 0).map(({ x, width }) => ({ x, width }));
  });
  expect(lines.length, "text must have rendered, nonempty lines").toBeGreaterThan(0);
  for (const line of lines) {
    expect(line.x, "the complete text must remain inside its own content column").toBeGreaterThanOrEqual(outer!.x - 1);
    expect(line.x + line.width, "the complete text must remain inside its own content column").toBeLessThanOrEqual(outer!.x + outer!.width + 1);
  }
}

async function reachable(control: Locator, page: Page) {
  await control.scrollIntoViewIfNeeded();
  const bounds = await control.boundingBox();
  expect(bounds).not.toBeNull();
  expect(bounds!.width).toBeGreaterThan(0);
  expect(bounds!.x).toBeGreaterThanOrEqual(-1);
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(page.viewportSize()!.width + 1);
  await control.focus();
  await expect(control).toBeFocused();
}

async function evidence(page: Page, testInfo: TestInfo, name: string) {
  await testInfo.attach(`${name}-aria`, { body: await page.locator("main").ariaSnapshot(), contentType: "text/plain" });
  await testInfo.attach(`${name}-bounds`, { body: JSON.stringify(await page.evaluate(() => ({ viewport: innerWidth, body: document.body.scrollWidth, root: document.documentElement.scrollWidth, rootFont: getComputedStyle(document.documentElement).fontSize }))), contentType: "application/json" });
  const path = testInfo.outputPath(`${name}.png`);
  await page.screenshot({ path, fullPage: true });
  await testInfo.attach(`${name}-screenshot`, { path, contentType: "image/png" });
}

for (const [width, fontSize] of [[320, 16], [390, 16], [390, 32], [1024, 16], [1280, 16]]) {
  test(`full project content and controls reflow at ${width}px with ${fontSize}px root text`, async ({ page }, testInfo) => {
    await fixture(page);
    await open(page, width, fontSize);
    const profit = card(page, "Profitability");
    await expect(profit.getByText("active", { exact: true })).toHaveCount(1);
    for (const text of ["Profitability", "active", "live figures · net amounts, EUR"]) await readableText(profit.getByText(text, { exact: true }), profit);
    for (const [label, value] of [["Revenue", `${PNL.revenue} €`], ["Costs", `${PNL.costs} €`], ["Profit", `${PNL.profit} €`], ["Margin", `${PNL.margin_pct}%`]]) {
      const metric = profit.getByText(label, { exact: true }).locator("..");
      await readableText(metric.getByText(label, { exact: true }), metric);
      await readableText(metric.getByText(value, { exact: true }), metric);
    }
    for (const text of [`after ${PNL.credited} € credited`, `estimated ${PNL.estimated_revenue} € revenue`]) await readableText(profit.getByText(text, { exact: true }), profit);
    await expect(profit).toContainText(`invoices ${PNL.invoice_costs} € · expenses ${PNL.expense_costs} € · manual ${PNL.manual_costs} €`);
    await fitsPage(page);
    for (const name of ["Label", "Category", "Amount (EUR)", "Title", "Instalment", "Document template", "Linked customer", "Acceptance document"]) {
      const control = page.getByLabel(name, { exact: true });
      // There are three independently labelled amount controls on this page.
      await expect(control).toHaveCount(name === "Amount (EUR)" ? 3 : 1);
      for (let index = 0; index < await control.count(); index++) await reachable(control.nth(index), page);
    }
    for (const name of ["Add cost", "New offer", "Add instalment", "Record acceptance", "+ Adjustment", "Prepare final invoice"]) await reachable(page.getByRole("button", { name, exact: true }), page);
    // Wide data remains available in named keyboard-scrollable regions, without
    // widening the document or removing any value/action from the real table.
    for (const [regionName, text, action] of [["Manual cost entries", COST_LABEL, "Remove"], ["Project documents", DOCUMENT, "Download"], ["Project offers", OFFER_TITLE, "accepted"], ["Invoicing plan entries", PLAN_LABEL, "Remove"]]) {
      const region = page.getByRole("region", { name: regionName, exact: true });
      await expect(region.getByRole("table")).toHaveCount(1);
      await expect(region.getByRole("table", { name: regionName, exact: true })).toHaveCount(1);
      const columns: Record<string, string[]> = {
        "Manual cost entries": ["Label", "Category", "Amount (EUR)", "Actions"],
        "Project documents": ["Document", "Type", "Uploaded", "Actions"],
        "Project offers": ["Offer", "Title", "Total (EUR)", "Status", "Actions"],
        "Invoicing plan entries": ["Instalment", "Amount (EUR)", "Actions"],
      };
      await expect(region.getByRole("columnheader")).toHaveText(columns[regionName]);
      for (const header of await region.getByRole("columnheader").all()) await expect(header).toHaveAttribute("scope", "col");
      expect(await region.locator("tbody tr").first().locator("td").count()).toBe(columns[regionName].length);
      await expect(region.getByText(text, { exact: true })).toHaveText(text);
      await readableText(region.getByText(text, { exact: true }), region.getByRole("table"));
      if (regionName !== "Project documents") {
        const money = region.getByText(`${LONG_MONEY} €`, { exact: true });
        await expect(money).toHaveText(`${LONG_MONEY} €`);
        await readableText(money, region.getByRole("table"));
      }
      await region.scrollIntoViewIfNeeded();
      await region.focus();
      await page.keyboard.press("Shift+Tab");
      await page.keyboard.press("Tab");
      await expect(region).toBeFocused();
      const before = await region.evaluate((element) => element.scrollLeft);
      const overflow = await region.evaluate((element) => element.scrollWidth > element.clientWidth + 1);
      if (overflow) {
        await page.keyboard.press("ArrowRight");
        await expect.poll(() => region.evaluate((element) => element.scrollLeft)).toBeGreaterThan(before);
      }
      await reachable(region.getByRole("button", { name: action, exact: true }), page);
      await fitsPage(page);
    }
    if (width === 390 && fontSize === 32) await evidence(page, testInfo, "enlarged-long-project");
  });
}

test("frozen figures, source evidence and late adjustments remain fully readable", async ({ page }, testInfo) => {
  await fixture(page, { pnl: { status: "closed", basis: "net_eur_frozen", pnl_frozen_at: "2026-10-01T09:00:00Z", adjustments_complete: false,
    issues: [{ code: "missing_fx", currency: "ZZZ", detail: `${LONG_LABEL} needs conversion evidence.` }],
    adjustments: { costs: LONG_MONEY, profit: `-${LONG_MONEY}` },
    fx_trace: [{ source_id: "received-reflow", kind: "received", document_number: SOURCE_NUMBER, currency: "USD", original_amount: LONG_MONEY, amount_eur: "111111111111111111.01", rate: "1.111111111111", rate_date: "2026-10-01", approximate: true, allocated_eur: "99999999999999999.99" }],
  } });
  await open(page);
  const profit = card(page, "Profitability");
  await readableText(profit.getByText(/frozen at close/), profit);
  await expect(profit.getByText(`${PNL.profit} €`, { exact: true })).toHaveText(`${PNL.profit} €`);
  await readableText(profit.getByRole("status"), profit);
  await profit.getByText("Currency conversion evidence", { exact: true }).click();
  const source = profit.getByRole("listitem").filter({ hasText: SOURCE_NUMBER });
  await readableText(source, profit);
  await expect(source).toContainText(`USD ${LONG_MONEY} → EUR 111111111111111111.01 · rate 1.111111111111 USD / EUR at 2026-10-01 · approximate · allocated EUR 99999999999999999.99`);
  await expect(source.getByRole("link", { name: "View received source", exact: true })).toHaveAttribute("href", "/invoices/received-reflow");
  await reachable(source.getByRole("link", { name: "View received source", exact: true }), page);
  for (const value of [`costs: ${LONG_MONEY} €`, `profit: -${LONG_MONEY} €`]) await readableText(profit.getByText(value, { exact: true }), profit);
  await fitsPage(page);
  await evidence(page, testInfo, "frozen-source-adjustments");
});

test("incomplete currency evidence keeps its warning, historical values and review link readable", async ({ page }, testInfo) => {
  await fixture(page, { pnl: { basis: "legacy_currency_unverified", complete: false, revenue: null, costs: null, profit: null, margin_pct: null,
    issues: [{ code: "legacy_snapshot_unverified", currency: "ZZZ", detail: `${LONG_LABEL} remains unverified; review source evidence.` }], legacy_figures: { revenue: LONG_MONEY, profit: "200.00" },
  } });
  await open(page);
  const profit = card(page, "Profitability");
  const alert = profit.getByRole("alert");
  await expect(alert).toContainText("Profitability is unavailable until its currency basis is verified.");
  await readableText(alert.getByText(`ZZZ: ${LONG_LABEL} remains unverified; review source evidence.`, { exact: true }), profit);
  await reachable(alert.getByRole("link", { name: "Review exchange rates", exact: true }), page);
  await expect(alert.getByRole("link")).toHaveAttribute("href", "/fx");
  await expect(profit.getByText(`${LONG_MONEY} €`, { exact: true })).toHaveCount(0);
  const historical = alert.getByText("View unchanged historical values (currency basis unverified)", { exact: true });
  await reachable(historical, page);
  await historical.press("Enter");
  await expect(alert.locator("pre")).toContainText(`"revenue": "${LONG_MONEY}"`);
  await fitsPage(page);
  await evidence(page, testInfo, "incomplete-historical");
});

test("a pending project read and a failed read retain a usable bounded page and retry", async ({ page }, testInfo) => {
  const read = held();
  let calls = 0;
  await fixture(page, { readPnl: async () => { calls++; if (calls === 1) { await read.promise; return { status: 404, body: { detail: "Synthetic unavailable project" } }; } return { status: 200, body: PNL }; } });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`/projects/${PROJECT.id}`);
  await page.evaluate(() => { document.documentElement.style.fontSize = "32px"; });
  await expect(page.getByRole("heading", { name: "Project", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Costs booked by hand", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Profitability", exact: true })).toHaveCount(0);
  await reachable(page.getByRole("link", { name: "All projects", exact: true }), page);
  await fitsPage(page);
  read.release();
  const error = page.getByRole("alert").filter({ hasText: "Couldn’t load the project" });
  await expect(error).toBeVisible();
  await readableText(error.getByText("Couldn’t load the project", { exact: true }), error);
  const retry = error.getByRole("button", { name: "Try again", exact: true });
  await reachable(retry, page);
  await fitsPage(page);
  await evidence(page, testInfo, "failed-project-read");
  await retry.press("Enter");
  await expect(page.getByRole("heading", { name: "Profitability", exact: true })).toBeVisible();
  expect(calls).toBe(2);
  await fitsPage(page);
});

test("the enlarged manual-cost form submits one unchanged payload and disables while pending", async ({ page }, testInfo) => {
  const write = held();
  const expected = { method: "POST", path: `/masters/projects/${PROJECT.id}/cost-entries`, body: { label: "Equipment hire agreed at handover", category: "equipment", amount: "987654321.09" } };
  observed.get(page)!.expected = [expected];
  await fixture(page, { write: async () => { await write.promise; return { status: 201, body: { id: "new-cost" } }; } });
  await open(page);
  const form = card(page, "Costs booked by hand");
  await form.getByRole("textbox", { name: "Label", exact: true }).fill("Equipment hire agreed at handover");
  await form.getByRole("combobox", { name: "Category", exact: true }).selectOption("equipment");
  await form.getByRole("textbox", { name: "Amount (EUR)", exact: true }).fill("987654321.09");
  const add = form.getByRole("button", { name: "Add cost", exact: true });
  await reachable(add, page);
  await add.press("Enter");
  await expect.poll(() => observed.get(page)!.writes).toEqual([expected]);
  await expect(add).toBeDisabled();
  await fitsPage(page);
  await evidence(page, testInfo, "manual-cost-pending");
  write.release();
  await expect(add).toBeEnabled();
  await expect(form.getByRole("textbox", { name: "Label", exact: true })).toHaveValue("");
  await expect(form.getByRole("textbox", { name: "Amount (EUR)", exact: true })).toHaveValue("");
});

test("the enlarged final-invoice controls preserve one labelled draft request and server refusal", async ({ page }, testInfo) => {
  const write = held();
  const expected = { method: "POST", path: `/masters/projects/${PROJECT.id}/final-invoice-draft`, body: { adjustments: [{ label: "Extra work agreed at handover", amount: "500.01" }, { label: "Agreed discount", amount: "-10.009" }] } };
  observed.get(page)!.expected = [expected];
  await fixture(page, { write: async () => { await write.promise; return { status: 409, body: { detail: "A recorded acceptance is required for this final invoice" } }; } });
  await open(page);
  const final = card(page, "Acceptance & final invoice");
  await reachable(final.getByRole("button", { name: "+ Adjustment", exact: true }), page);
  await final.getByRole("button", { name: "+ Adjustment", exact: true }).press("Enter");
  const label = final.getByRole("textbox", { name: "Adjustment 1 description", exact: true });
  const amount = final.getByRole("textbox", { name: "Adjustment 1 amount (EUR)", exact: true });
  await reachable(label, page); await label.fill("Extra work agreed at handover");
  await reachable(amount, page); await amount.fill("500.01");
  await final.getByRole("button", { name: "+ Adjustment", exact: true }).press("Enter");
  const second = final.getByRole("group", { name: "Final invoice adjustment 2", exact: true });
  await second.getByRole("textbox", { name: "Adjustment 2 description", exact: true }).fill("Agreed discount");
  const secondAmount = second.getByRole("textbox", { name: "Adjustment 2 amount (EUR)", exact: true });
  await reachable(secondAmount, page); await secondAmount.fill("-10.009");
  await page.keyboard.press("Tab");
  await expect(second.getByRole("button", { name: "Remove adjustment 2", exact: true })).toBeFocused();
  const prepare = final.getByRole("button", { name: "Prepare final invoice", exact: true });
  await reachable(prepare, page); await prepare.press("Enter");
  await expect.poll(() => observed.get(page)!.writes).toEqual([expected]);
  await expect(prepare).toBeDisabled();
  await fitsPage(page);
  await evidence(page, testInfo, "final-invoice-pending");
  write.release();
  await expect(page.getByRole("alert").filter({ hasText: "A recorded acceptance is required for this final invoice" })).toBeVisible();
  await expect(prepare).toBeEnabled();
  await expect(label).toHaveValue("Extra work agreed at handover");
  await expect(amount).toHaveValue("500.01");
  await expect(secondAmount).toHaveValue("-10.009");
  await expect(page).toHaveURL(new RegExp(`/projects/${PROJECT.id}$`));
  await fitsPage(page);
});

test("the wrapped issued-source action retains its GET, pending state, error and retry", async ({ page }, testInfo) => {
  const source = held();
  let reads = 0;
  await fixture(page, { pnl: { fx_trace: [{ source_id: "source-reflow", kind: "issued", document_number: SOURCE_NUMBER, currency: "USD", original_amount: LONG_MONEY, amount_eur: "111111111111111111.01", rate: "1.111111111111", rate_date: "2026-10-01", approximate: false }] }, source: async () => { reads++; await source.promise; return { status: 503, body: { detail: "Synthetic source storage unavailable" } }; } });
  await open(page);
  const profit = card(page, "Profitability");
  await profit.getByText("Currency conversion evidence", { exact: true }).click();
  const action = profit.getByRole("button", { name: `Download source invoice ${SOURCE_NUMBER} PDF`, exact: true });
  await reachable(action, page); await action.press("Enter");
  await expect.poll(() => reads).toBe(1);
  await expect(action).toBeDisabled();
  await expect(action).toHaveText("Preparing source PDF…");
  await fitsPage(page);
  source.release();
  const error = profit.getByRole("alert").filter({ hasText: "Source PDF download failed:" });
  await expect(error).toContainText("Synthetic source storage unavailable");
  await readableText(error, profit);
  await expect(action).toBeEnabled();
  await action.press("Enter");
  await expect.poll(() => reads).toBe(2);
  await expect(action).toBeEnabled();
  await fitsPage(page);
  await evidence(page, testInfo, "issued-source-retry");
});
