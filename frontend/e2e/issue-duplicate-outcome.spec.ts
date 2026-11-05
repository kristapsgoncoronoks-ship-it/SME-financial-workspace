import { test, expect, type Page } from "@playwright/test";

const ORG = { id: "copy-org-A", name: "Synthetic copy workspace", status: "active" };
const USER = { id: "copy-user-A", org_id: ORG.id, name: "Synthetic copy operator", email: "copy@example.test", role: "owner" };
const PROFILE = { id: "copy-issuer", legal_name: "Synthetic seller", is_complete: true, default_penalty_rate: null };
function invoice(index: number, prefix = "A") {
  return { id: `copy-${prefix}-${index}`, number: `COPY-${prefix}-${index}`, issuer_id: PROFILE.id, buyer_name: `Synthetic buyer ${prefix}-${index}`, buyer_email: null,
    issue_date: "2026-11-05", due_date: null, currency: "EUR", total: "121.00", subtotal: "100.00", tax_total: "21.00", lifecycle: "issued", doc_type: "invoice", status: "open",
    amount_paid: "0.00", credited_total: "0.00", outstanding: "121.00", sent_at: null, reminder_count: 0, last_reminder_at: null, vat_scheme: "standard", lines: [] };
}
function held() { let release!: () => void; const wait = new Promise<void>(resolve => { release = resolve; }); return { entered: false, release, wait }; }
type Held = ReturnType<typeof held>;
type Row = ReturnType<typeof invoice>;
type Wire = { rows: Row[]; writes: { path: string; account: string; body: unknown }[]; reads: { page: number; account: string }[]; statuses: number[]; postHold?: Held; listHold?: Held; listFailure: boolean; response?: unknown };
async function setup(page: Page, writePermission = true): Promise<Wire> {
  const wire: Wire = { rows: Array.from({ length: 105 }, (_, i) => invoice(i + 1)), writes: [], reads: [], statuses: [], listFailure: false };
  await page.addInitScript(() => {
    if (!sessionStorage.getItem("copy-fixture-seeded")) {
      localStorage.setItem("invoiceiq_token", "copy-account-A");
      sessionStorage.setItem("copy-fixture-seeded", "1");
    }
  });
  await page.route("**/api/v1/**", async route => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname.replace("/api/v1", "");
    const account = request.headers().authorization === "Bearer copy-account-B" ? "B" : "A";
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (["POST", "PUT", "PATCH", "DELETE"].includes(request.method())) wire.writes.push({ path, account, body: request.postDataJSON() });
    const organization = account === "B" ? { ...ORG, id: "copy-org-B", name: "Replacement copy workspace" } : ORG;
    const user = account === "B" ? { ...USER, id: "copy-user-B", org_id: organization.id } : USER;
    if (path === "/auth/me") return json({ user, organization, permissions: ["issued.read", ...(writePermission ? ["issued.write"] : [])] });
    if (path === "/auth/organizations") return json([{ org_id: organization.id, name: organization.name, current: true }]);
    if (path === "/modules") return json([{ key: "issuing", enabled: true, ready: true, requires_issuer: true }]);
    if (path === "/issuer") return json(PROFILE);
    if (path === "/issuer/registry") return json([PROFILE]);
    if (path === "/issued" && request.method() === "GET") {
      const page = Number(url.searchParams.get("page")); wire.reads.push({ page, account });
      if (wire.listHold) { wire.listHold.entered = true; await wire.listHold.wait; }
      if (wire.listFailure) return json({ detail: "Synthetic list refresh refused" }, 503);
      const rows = account === "B" ? Array.from({ length: 105 }, (_, i) => invoice(i + 1, "B")) : wire.rows;
      return json({ items: rows.slice((page - 1) * 100, page * 100), total: rows.length });
    }
    if (path === "/issued/copy-A-105/duplicate" && request.method() === "POST") {
      const status = wire.statuses.shift() ?? 201;
      if (wire.postHold) { wire.postHold.entered = true; await wire.postHold.wait; }
      if (status >= 400) return json({ detail: "Previous duplicate attempt failed" }, status);
      const draft = { ...invoice(106), id: "observed-draft-copy", number: null, lifecycle: "draft", status: "draft", buyer_name: "Observed draft buyer" };
      if (!wire.response) wire.rows = [draft as unknown as Row, ...wire.rows];
      return json(wire.response ?? draft, status);
    }
    return json([]);
  });
  await page.goto("/issue?page=2&context=kept");
  await expect(page.getByRole("heading", { name: "Issued (105)", exact: true })).toBeVisible();
  return wire;
}
const row = (page: Page) => page.getByRole("row").filter({ has: page.getByText("COPY-A-105", { exact: true }) });
const notice = (page: Page) => page.getByRole("status", { name: "Draft copy outcome", exact: true });

test("a successful retry clears the old copy error and keeps a truthful outcome while list refresh is pending", async ({ page }) => {
  const wire = await setup(page); wire.statuses = [503, 201];
  await row(page).getByRole("button", { name: "Duplicate", exact: true }).click();
  await expect(row(page).getByRole("alert")).toContainText("Previous duplicate attempt failed");
  wire.postHold = held(); wire.listHold = held();
  try {
    await row(page).getByRole("button", { name: "Duplicate", exact: true }).click();
    await expect.poll(() => wire.postHold!.entered).toBe(true);
    await expect(row(page).getByRole("alert")).toContainText("Previous duplicate attempt failed");
    await expect(notice(page)).toHaveCount(0);
    wire.postHold.release();
    await expect(notice(page)).toContainText("Observed draft buyer"); await expect(notice(page)).toContainText("COPY-A-105");
    await expect.poll(() => wire.listHold!.entered).toBe(true);
    await expect(row(page).getByRole("alert")).toHaveCount(0);
    await expect(row(page).getByRole("button", { name: "Duplicate", exact: true })).toBeDisabled();
    await expect(page.getByRole("button", { name: "Show latest invoices", exact: true })).toBeDisabled();
    wire.listHold.release();
    await expect(page.getByRole("navigation", { name: "Issued invoice pages", exact: true })).toContainText("Showing 101–106 of 106");
    await expect(page.getByText("Draft — unnumbered", { exact: true })).toHaveCount(0);
    const latest = page.getByRole("button", { name: "Show latest invoices", exact: true });
    await latest.focus(); await page.keyboard.press("Enter");
    await expect(page).toHaveURL(/page=1&context=kept/);
    await expect(page.getByText("Draft — unnumbered", { exact: true })).toBeVisible();
    expect(wire.writes).toEqual([{ path: "/issued/copy-A-105/duplicate", account: "A", body: {} }, { path: "/issued/copy-A-105/duplicate", account: "A", body: {} }]);
  } finally { wire.postHold.release(); wire.listHold.release(); }
});

test("a copied outcome survives failed list refresh and recovers using only bounded GETs", async ({ page }) => {
  const wire = await setup(page); wire.listFailure = true;
  await row(page).getByRole("button", { name: "Duplicate", exact: true }).click();
  await expect(notice(page)).toContainText("Observed draft buyer");
  await expect(page.getByText("Couldn’t load the issued invoices", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Show latest invoices", exact: true }).click();
  await expect(page).toHaveURL(/page=1&context=kept/);
  // URL navigation can precede rendering the new page's failed read.
  await expect(page.getByRole("navigation", { name: "Issued invoice pages", exact: true })).toContainText("Page 1 could not be loaded.");
  await expect(page.getByText("Couldn’t load the issued invoices", { exact: true })).toBeVisible();
  await expect(notice(page)).toContainText("Observed draft buyer");
  wire.listFailure = false; await page.getByRole("button", { name: "Try again", exact: true }).click();
  await expect(page.getByText("Draft — unnumbered", { exact: true })).toBeVisible();
  expect(wire.writes).toEqual([{ path: "/issued/copy-A-105/duplicate", account: "A", body: {} }]);
  expect(wire.reads.map(read => read.page)).toEqual([2, 2, 1, 1]);
});

test("a late copy response cannot publish an outcome or navigation in a replacement account", async ({ page }) => {
  const wire = await setup(page); wire.postHold = held();
  try {
    await row(page).getByRole("button", { name: "Duplicate", exact: true }).click();
    await expect.poll(() => wire.postHold!.entered).toBe(true);
    await page.evaluate(() => { const oldValue = localStorage.getItem("invoiceiq_token"); localStorage.setItem("invoiceiq_token", "copy-account-B"); window.dispatchEvent(new StorageEvent("storage", { key: "invoiceiq_token", oldValue, newValue: "copy-account-B", storageArea: localStorage })); });
    await expect(page.getByText("COPY-B-105", { exact: true })).toBeVisible();
    wire.postHold.release(); await page.waitForLoadState("networkidle");
    await expect(notice(page)).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Show latest invoices", exact: true })).toHaveCount(0);
    await expect(page).toHaveURL(/page=2&context=kept/);
    expect(wire.writes).toEqual([{ path: "/issued/copy-A-105/duplicate", account: "A", body: {} }]);
  } finally { wire.postHold.release(); }
});

test("a reader retains bounded list access without gaining the copy write", async ({ page }) => {
  const wire = await setup(page, false);
  await expect(page.getByText("COPY-A-105", { exact: true })).toBeVisible();
  await expect(row(page).getByRole("button", { name: "Duplicate", exact: true })).toBeDisabled();
  expect(wire.writes).toEqual([]);
});

for (const response of [
  { ...invoice(106), id: "unexpected-issued-document" },
  { ...invoice(105), id: "copy-A-105", number: null, lifecycle: "draft", status: "draft" },
]) {
  test(`an unverified ${response.id} response does not invent a created draft or automatically repeat the copy`, async ({ page }) => {
    const wire = await setup(page); wire.response = response;
    await row(page).getByRole("button", { name: "Duplicate", exact: true }).click();
    await expect(row(page).getByRole("alert")).toContainText("copy outcome could not be verified");
    await expect(notice(page)).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Show latest invoices", exact: true })).toHaveCount(0);
    expect(wire.writes).toEqual([{ path: "/issued/copy-A-105/duplicate", account: "A", body: {} }]);
  });
}


test("ordinary paging during a pending copy retains its same-session outcome and refreshes the current page", async ({ page }) => {
  const wire = await setup(page); wire.postHold = held();
  try {
    await row(page).getByRole("button", { name: "Duplicate", exact: true }).click();
    await expect.poll(() => wire.postHold!.entered).toBe(true);
    await page.getByRole("button", { name: "Previous page", exact: true }).click();
    await expect(page.getByText("COPY-A-1", { exact: true })).toBeVisible();
    wire.postHold.release();
    await expect(notice(page)).toContainText("Observed draft buyer");
    await expect(notice(page)).toContainText("COPY-A-105");
    await expect(notice(page)).not.toContainText("observed-draft-copy");
    await expect(page.getByRole("heading", { name: "Issued (106)", exact: true })).toBeVisible();
    await expect(page.getByText("Draft — unnumbered", { exact: true })).toBeVisible();
    expect(wire.reads.map(read => read.page)).toEqual([2, 1, 1]);
    expect(wire.writes).toEqual([{ path: "/issued/copy-A-105/duplicate", account: "A", body: {} }]);
    await expect(page).toHaveURL(/page=1&context=kept/);
  } finally { wire.postHold.release(); }
});
