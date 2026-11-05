import { test, expect, type Page } from "@playwright/test";

// Synthetic browser contract evidence. Canonical Decimal planning and no-write
// PostgreSQL assertions are separate backend/native and connected-live evidence.
const ORG = { id: "rebate-preview-org", name: "Synthetic rebate workspace", status: "active" };
const USER = { id: "rebate-preview-owner", org_id: ORG.id, name: "Owner", role: "owner", email: "owner@example.com" };
function allocation(index: number) {
  return { fuel_transaction_id: `synthetic-fuel-${index}`, supplier: "CARDNET", country: "LV", qty: "1.000", net_eur: "100.00", share_eur: "1.00", old_net_eur_eff: "100.00", new_net_eur_eff: "99.00" };
}
interface Wire {
  rows: ReturnType<typeof allocation>[];
  total: string;
  changed: number;
  warnings: string[];
  reads: Array<{ query: string; headers: Record<string, string> }>;
  writes: Array<{ method: string; path: string; body: unknown }>;
  refusal: { status: number; code: string; detail: string } | null;
  mutate: ((body: Record<string, unknown>) => unknown) | null;
  hold: { entered: boolean; release: () => void; wait: Promise<void> } | null;
  offline: boolean;
  identity: { user: typeof USER; org: typeof ORG };
}
async function setup(page: Page, count = 1, role = "owner", enabled = true): Promise<Wire> {
  const wire: Wire = { rows: Array.from({ length: count }, (_, index) => allocation(index + 1)), total: `${count}.00`, changed: count, warnings: [], reads: [], writes: [], refusal: null, mutate: null, hold: null, offline: false, identity: { user: { ...USER, role }, org: ORG } };
  const json = (value: unknown, status = 200) => ({ status, contentType: "application/json", body: JSON.stringify(value) });
  await page.addInitScript(() => { if (!localStorage.getItem("invoiceiq_token")) localStorage.setItem("invoiceiq_token", "rebate-preview-synthetic-token"); });
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname.replace("/api/v1", ""), method = request.method();
    if (["POST", "PUT", "PATCH", "DELETE"].includes(method)) wire.writes.push({ method, path, body: request.postDataJSON() });
    if (path === "/auth/me") return route.fulfill(json({ user: wire.identity.user, organization: wire.identity.org }));
    if (path === "/auth/organizations") return route.fulfill(json([{ org_id: wire.identity.org.id, name: wire.identity.org.name, current: true }]));
    if (path === "/modules") return route.fulfill(json([{ key: "transport", name: "Transport", enabled, ready: enabled, requires_issuer: false }]));
    if (path === "/transport/rebates/preview") {
      wire.reads.push({ query: url.search, headers: request.headers() });
      if (wire.offline) return route.abort("internetdisconnected");
      if (wire.hold) { wire.hold.entered = true; await wire.hold.wait; }
      if (wire.refusal) return route.fulfill(json({ code: wire.refusal.code, detail: wire.refusal.detail }, wire.refusal.status));
      const requestedPage = Number(url.searchParams.get("page") ?? 1), size = Number(url.searchParams.get("page_size") ?? 100);
      const body = { period: url.searchParams.get("period"), supplier: url.searchParams.get("supplier"), rebate_total_eur: wire.total, lines_allocated: wire.rows.length, changed: wire.changed, source_warnings: wire.warnings, allocations: wire.rows.slice((requestedPage - 1) * size, requestedPage * size), page: requestedPage, page_size: size };
      return route.fulfill(json(wire.mutate ? wire.mutate(body) : body));
    }
    if (path === "/transport/rebates" && method === "GET") return route.fulfill(json([]));
    if (path === "/transport/rebates" && method === "POST") {
      const body = request.postDataJSON(); wire.total = "2.00";
      return route.fulfill(json({ id: "synthetic-source", ...body, amount_eur: body.amount_local, fx_source: "eur", fx_rate: null, fx_ecb_rate: null, fx_ecb_date: null }));
    }
    return route.fulfill(json([]));
  });
  return wire;
}
async function preview(page: Page, period = "2026-05", supplier = "CARDNET") {
  const form = page.getByRole("form", { name: "Rebate preview", exact: true });
  await form.getByRole("textbox", { name: "Preview period", exact: true }).fill(period);
  await form.getByLabel("Preview network code (optional)", { exact: true }).fill(supplier);
  await form.getByRole("button", { name: "Preview rebate effect", exact: true }).click();
}
const table = (page: Page) => page.getByRole("table", { name: "Read-only rebate allocations", exact: true });

test("canonical preview reads an explicit scope and shows server amounts without applying or writing", async ({ page }) => {
  const wire = await setup(page);
  await page.goto("/rebates");
  await preview(page);
  await expect(table(page).getByRole("cell", { name: "€99.00", exact: true })).toBeVisible();
  await expect(page.getByText("No transaction figure has changed.", { exact: false })).toBeVisible();
  await expect(page.getByText("Would change: 1", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: /apply|merge now/i })).toHaveCount(0);
  expect(wire.reads).toHaveLength(1);
  expect(Object.fromEntries(new URLSearchParams(wire.reads[0].query))).toEqual({ period: "2026-05", supplier: "CARDNET", page: "1", page_size: "100" });
  expect(wire.reads[0].headers["x-expected-org-id"]).toBe(ORG.id);
  expect(wire.reads[0].headers["x-expected-user-id"]).toBe(USER.id);
  expect(wire.writes).toEqual([]);
});

test("blank network scope requests every supplier and country without adding a filter", async ({ page }) => {
  const wire = await setup(page);
  await page.goto("/rebates"); await preview(page, "2026-05", "");
  await expect(table(page)).toBeVisible();
  expect(Object.fromEntries(new URLSearchParams(wire.reads[0].query))).toEqual({ period: "2026-05", page: "1", page_size: "100" });
  await expect(page.getByText("2026-05 · All suppliers · All countries and entities", { exact: true })).toBeVisible();
  expect(wire.writes).toEqual([]);
});

test("large server Decimal strings and unchanged counts render without float conversion", async ({ page }) => {
  const wire = await setup(page); wire.total = "99999999999999.99"; wire.changed = 0;
  wire.rows[0] = { ...wire.rows[0], net_eur: "99999999999999.99", old_net_eur_eff: "99999999999999.98", new_net_eur_eff: "99999999999999.98", share_eur: "0.01", qty: "999999999.125" };
  await page.goto("/rebates"); await preview(page);
  await expect(table(page).getByRole("cell", { name: "€99,999,999,999,999.99", exact: true })).toBeVisible();
  await expect(table(page).getByRole("cell", { name: "€99,999,999,999,999.98", exact: true })).toHaveCount(2);
  await expect(table(page).getByRole("cell", { name: "999999999.125", exact: true })).toBeVisible();
  await expect(page.getByText("Would change: 0", { exact: true })).toBeVisible();
  expect(wire.writes).toEqual([]);
});

test("101 allocations page and reload with whole-scope totals on every bounded response", async ({ page }) => {
  const wire = await setup(page, 101);
  await page.goto("/rebates?context=kept"); await preview(page);
  await expect(table(page).getByRole("row")).toHaveCount(101);
  await expect(page.getByText("Would change: 101", { exact: true })).toBeVisible();
  const nav = page.getByRole("navigation", { name: "Rebate allocation pages", exact: true });
  await nav.getByRole("button", { name: "Next allocations", exact: true }).click();
  await expect(table(page).getByRole("cell", { name: "synthetic-fuel-101", exact: true })).toBeVisible();
  await expect(table(page).getByRole("row")).toHaveCount(2);
  await expect(page.getByText("Would change: 101", { exact: true })).toBeVisible();
  await expect(page.getByText("€101.00", { exact: true })).toBeVisible();
  await expect(nav).toContainText("Showing 101–101 of 101 allocations");
  await page.reload();
  await expect(nav).toContainText("Page 2 of 2");
  await expect(page).toHaveURL(/context=kept/);
  expect(wire.reads.map(({ query }) => Object.fromEntries(new URLSearchParams(query)))).toEqual([
    { period: "2026-05", supplier: "CARDNET", page: "1", page_size: "100" },
    { period: "2026-05", supplier: "CARDNET", page: "2", page_size: "100" },
    { period: "2026-05", supplier: "CARDNET", page: "2", page_size: "100" },
  ]);
  expect(wire.writes).toEqual([]);
});

test("no allocated lines still shows complete advisory missing-source warnings", async ({ page }) => {
  const wire = await setup(page, 0); wire.warnings = ["A recorded source is missing for CARDNET/LV."];
  await page.goto("/rebates"); await preview(page);
  await expect(page.getByText("No fuel lines are allocated in this preview.", { exact: true })).toBeVisible();
  await expect(page.getByText(wire.warnings[0], { exact: true })).toBeVisible();
  await expect(page.getByText("Would change: 0", { exact: true })).toBeVisible();
  expect(wire.writes).toEqual([]);
});

test("changed preview inputs immediately hide old financial results until explicit resubmission", async ({ page }) => {
  const wire = await setup(page);
  await page.goto("/rebates"); await preview(page);
  await expect(table(page)).toBeVisible();
  await page.getByRole("textbox", { name: "Preview period", exact: true }).fill("2026-06");
  await expect(table(page)).toHaveCount(0);
  await expect(page.getByText("Would change: 1", { exact: true })).toHaveCount(0);
  await expect(page.getByText(/Preview inputs changed/)).toBeVisible();
  expect(wire.reads).toHaveLength(1);
  await page.getByRole("button", { name: "Preview rebate effect", exact: true }).click();
  await expect(table(page)).toBeVisible();
  expect(new URLSearchParams(wire.reads.at(-1)!.query).get("period")).toBe("2026-06");
  expect(wire.writes).toEqual([]);
});

test("a late old-scope response cannot replace a newer preview", async ({ page }) => {
  const wire = await setup(page);
  let release = () => {}; const wait = new Promise<void>((resolve) => { release = resolve; });
  const hold = { entered: false, release, wait }; wire.hold = hold;
  try {
    await page.goto("/rebates"); await preview(page);
    await expect.poll(() => hold.entered).toBe(true);
    wire.hold = null;
    await page.getByRole("textbox", { name: "Preview period", exact: true }).fill("2026-06");
    await page.getByRole("button", { name: "Preview rebate effect", exact: true }).click();
    await expect(table(page)).toBeVisible();
    release();
    await expect(page.getByText("2026-06 · CARDNET · All countries and entities", { exact: true })).toBeVisible();
    await expect(page.getByText("2026-05 · CARDNET · All countries and entities", { exact: true })).toHaveCount(0);
    expect(wire.writes).toEqual([]);
  } finally { release(); }
});

for (const [status, code] of [[413, "rebate_preview_too_large"], [422, "rebate_no_litres_to_allocate"], [403, "forbidden"], [429, "rate_limited"], [503, "unavailable"]] as const) {
  test(`preview read ${status} refuses the whole display and retries GET only`, async ({ page }) => {
    const wire = await setup(page); wire.refusal = { status, code, detail: "Synthetic whole-scope preview refusal." };
    await page.goto("/rebates"); await preview(page);
    await expect(page.getByText(status === 413 ? "Preview scope is too large" : "Preview unavailable", { exact: true })).toBeVisible();
    await expect(table(page)).toHaveCount(0);
    await expect(page.getByText("Would change: 0", { exact: true })).toHaveCount(0);
    expect(wire.reads).toHaveLength(1); expect(wire.writes).toEqual([]);
    wire.refusal = null;
    await page.getByRole("button", { name: "Try again", exact: true }).click();
    await expect(table(page)).toBeVisible();
    expect(wire.reads).toHaveLength(2);
    expect(wire.reads[0].query).toBe(wire.reads[1].query);
    expect(wire.writes).toEqual([]);
  });
}

for (const [name, mutate] of [
  ["wrong scope", (body: Record<string, unknown>) => ({ ...body, period: "2026-04" })],
  ["numeric money", (body: Record<string, unknown>) => ({ ...body, rebate_total_eur: 1 })],
  ["unbounded allocation page", (body: Record<string, unknown>) => ({ ...body, lines_allocated: 101, allocations: Array.from({ length: 101 }, (_, index) => allocation(index)) })],
] as const) {
  test(`a ${name} response cannot be presented as a valid preview`, async ({ page }) => {
    const wire = await setup(page); wire.mutate = mutate;
    await page.goto("/rebates"); await preview(page);
    await expect(page.getByText("Preview unavailable", { exact: true })).toBeVisible();
    await expect(table(page)).toHaveCount(0);
    expect(wire.writes).toEqual([]);
  });
}

test("inactive module exposes no preview request or recording action", async ({ page }) => {
  const wire = await setup(page, 1, "owner", false);
  await page.goto("/rebates?preview_period=2026-05");
  await expect(page.getByRole("heading", { name: "Off-invoice rebates", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Preview rebate effect", exact: true })).toHaveCount(0);
  expect(wire.reads).toEqual([]); expect(wire.writes).toEqual([]);
});

test("an auditor can preview with read permission without gaining a record form", async ({ page }) => {
  const wire = await setup(page, 1, "auditor");
  await page.goto("/rebates"); await preview(page);
  await expect(table(page)).toBeVisible();
  await expect(page.getByRole("button", { name: "Record rebate", exact: true })).toHaveCount(0);
  expect(wire.writes).toEqual([]);
});

test("invalid month or blank period cannot submit a preview by button or Enter", async ({ page }) => {
  const wire = await setup(page);
  await page.goto("/rebates");
  const period = page.getByRole("textbox", { name: "Preview period", exact: true });
  for (const value of ["", "2026-13", "2026-1"]) {
    await period.fill(value); await period.press("Enter");
    await expect(page.getByRole("button", { name: "Preview rebate effect", exact: true })).toBeDisabled();
  }
  expect(wire.reads).toEqual([]); expect(wire.writes).toEqual([]);
});

test("recording keeps its original typed amount and refreshes an active preview", async ({ page }) => {
  const wire = await setup(page);
  await page.goto("/rebates"); await preview(page);
  await expect(table(page)).toBeVisible();
  await page.getByLabel("Supplier").fill("CARDNET");
  await page.getByLabel("Country").fill("lv");
  await page.getByLabel("Period adjusted").fill("2026-05");
  await page.getByLabel("Document date").fill("2026-06-10");
  await page.getByLabel("Document number").fill("SYN-SOURCE");
  await page.getByLabel("Issued by").fill("Synthetic rebate issuer");
  await page.getByLabel("Amount on the document").fill("2.00");
  await page.getByRole("button", { name: "Record rebate", exact: true }).click();
  await expect(page.getByText("€2.00", { exact: true }).first()).toBeVisible();
  await expect.poll(() => wire.reads.length).toBe(2);
  expect(wire.writes).toHaveLength(1);
  expect(wire.writes[0]).toMatchObject({ method: "POST", path: "/transport/rebates", body: { supplier: "CARDNET", country: "LV", period: "2026-05", amount_local: "2.00", currency: "EUR" } });
  expect(wire.writes[0].body).not.toHaveProperty("amount_eur");
});

test("advisory warnings have bounded display paging without changing the monetary plan", async ({ page }) => {
  const wire = await setup(page); wire.warnings = Array.from({ length: 101 }, (_, index) => `Missing-source advisory ${index + 1}`);
  await page.goto("/rebates"); await preview(page);
  await expect(page.getByRole("heading", { name: "Missing-source warnings (101)", exact: true })).toBeVisible();
  await expect(page.getByText("Missing-source advisory 101", { exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "Next warnings", exact: true }).click();
  await expect(page.getByText("Missing-source advisory 101", { exact: true })).toBeVisible();
  await expect(page.getByText("Would change: 1", { exact: true })).toBeVisible();
  expect(wire.reads).toHaveLength(1); expect(wire.writes).toEqual([]);
});

test("a shrinking successful plan clamps its last page on reload", async ({ page }) => {
  const wire = await setup(page, 101);
  await page.goto("/rebates?preview_period=2026-05&preview_supplier=CARDNET&preview_page=2");
  await expect(table(page).getByRole("cell", { name: "synthetic-fuel-101", exact: true })).toBeVisible();
  wire.rows = []; wire.total = "0.00"; wire.changed = 0;
  await page.reload();
  await expect(page.getByText("No fuel lines are allocated in this preview.", { exact: true })).toBeVisible();
  await expect(page).toHaveURL(/preview_page=1/);
  expect(new URLSearchParams(wire.reads.at(-1)!.query).get("page")).toBe("1");
  expect(wire.writes).toEqual([]);
});

test("untrusted long labels remain escaped and controls fit390px with enlarged text", async ({ page }, info) => {
  const wire = await setup(page);
  const label = '<img src=x onerror="alert(1)">' + "LONG".repeat(80);
  wire.rows[0].fuel_transaction_id = label; wire.warnings = [label];
  let dialogs = 0; page.on("dialog", async (dialog) => { dialogs++; await dialog.dismiss(); });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/rebates"); await preview(page);
  await expect(table(page).getByText(label, { exact: true })).toBeVisible();
  await expect(table(page).locator("img")).toHaveCount(0); expect(dialogs).toBe(0);
  await page.evaluate(() => { document.documentElement.style.fontSize = "32px"; });
  const form = page.getByRole("form", { name: "Rebate preview", exact: true });
  const box = await form.boundingBox(); expect(box).not.toBeNull();
  expect(box!.x + box!.width).toBeLessThanOrEqual(390);
  await form.screenshot({ path: info.outputPath("preview-form-390-text32.png") });
  expect(wire.writes).toEqual([]);
});

test("an offline preview retains its scope and recovers through explicit GET only", async ({ page }) => {
  const wire = await setup(page); wire.offline = true;
  await page.goto("/rebates"); await preview(page);
  await expect(page.getByText("Preview unavailable", { exact: true })).toBeVisible();
  await expect(table(page)).toHaveCount(0);
  expect(wire.reads).toHaveLength(1);
  wire.offline = false;
  await page.getByRole("button", { name: "Try again", exact: true }).click();
  await expect(table(page)).toBeVisible();
  expect(wire.reads[0].query).toBe(wire.reads[1].query);
  expect(wire.writes).toEqual([]);
});

test("a timed-out preview exposes recovery within its15-second read budget", async ({ page }) => {
  const wire = await setup(page);
  let release = () => {}; const wait = new Promise<void>((resolve) => { release = resolve; });
  wire.hold = { entered: false, release, wait };
  try {
    await page.goto("/rebates"); await preview(page);
    await expect(page.getByText("Preview unavailable", { exact: true })).toBeVisible({ timeout: 18_000 });
    await expect(table(page)).toHaveCount(0);
    wire.hold = null; release();
    await page.getByRole("button", { name: "Try again", exact: true }).click();
    await expect(table(page)).toBeVisible();
    expect(wire.reads).toHaveLength(2); expect(wire.writes).toEqual([]);
  } finally { release(); }
});

test("a replacement account and company cannot reuse the previous preview cache or request scope", async ({ page }) => {
  const wire = await setup(page);
  await page.goto("/rebates"); await preview(page);
  await expect(table(page).getByText("synthetic-fuel-1", { exact: true })).toBeVisible();
  wire.identity = { user: { ...USER, id: "replacement-owner", org_id: "replacement-org" }, org: { ...ORG, id: "replacement-org", name: "Replacement synthetic workspace" } };
  wire.rows[0] = { ...wire.rows[0], fuel_transaction_id: "replacement-fuel-only" };
  await page.evaluate(() => {
    const oldValue = localStorage.getItem("invoiceiq_token");
    localStorage.setItem("invoiceiq_token", "rebate-replacement-token");
    window.dispatchEvent(new StorageEvent("storage", { key: "invoiceiq_token", oldValue, newValue: "rebate-replacement-token", storageArea: localStorage }));
  });
  await expect(table(page).getByText("replacement-fuel-only", { exact: true })).toBeVisible();
  await expect(table(page).getByText("synthetic-fuel-1", { exact: true })).toHaveCount(0);
  expect(wire.reads.at(-1)!.headers["x-expected-user-id"]).toBe("replacement-owner");
  expect(wire.reads.at(-1)!.headers["x-expected-org-id"]).toBe("replacement-org");
  expect(wire.writes).toEqual([]);
});
