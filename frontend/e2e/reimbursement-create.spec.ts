import { test, expect, type Page, type Route } from "@playwright/test";

// Synthetic wire responses qualify browser presentation, exact selection and
// request recovery. They do not prove committed financial rows or row locks;
// the API/PostgreSQL and connected journeys supply that independent evidence.
const ORG = { id: "create-reimbursement-org", name: "Synthetic payout workspace", status: "active" };
const USER = { id: "create-reimbursement-user", org_id: ORG.id, name: "Synthetic payout owner", role: "owner", email: "owner@example.com" };
const SLOT = `invoiceiq:command:${USER.id}:${ORG.id}:/reimbursements`;
const BATCH_ID = "10000000-0000-4000-8000-000000000001";
const stamp = "2026-10-27T09:00:00Z";
type Report = { id: string; title: string; employee_name: string; total: string; currency: string; total_eur: string | null; status: string; version: number };
function report(index: number, amount = "0.10", patch: Partial<Report> = {}): Report {
  return {
    id: `20000000-0000-4000-8000-${String(index).padStart(12, "0")}`,
    title: `Synthetic claim ${index}`, employee_name: "Synthetic claimant",
    total: amount, currency: "EUR", total_eur: amount, status: "approved", version: index + 2,
    ...patch,
  };
}
const decimal = (value: bigint) => `${value / 100n}.${String(value % 100n).padStart(2, "0")}`;
function total(rows: Report[]): string {
  return decimal(rows.reduce((sum, row) => {
    const [whole, fraction = ""] = (row.total_eur ?? row.total).split(".");
    return sum + BigInt(whole) * 100n + BigInt(fraction.padEnd(2, "0"));
  }, 0n));
}
function batch(rows: Report[]) {
  return {
    id: BATCH_ID, reference: null, method: "bank_transfer", status: "open", note: null,
    total_eur: total(rows), paid_at: null, created_by: USER.email, exported_at: null,
    export_count: 0, last_msg_id: null, version: 1, created_at: stamp,
    report_count: rows.length,
    payment_evidence: { basis: "not_recorded", bank_confirmation: "unknown", recorded_by_id: null, recorded_at: null },
    reports: rows.map((row) => ({ ...row, payment_reference: null, reimbursed_at: null })),
  };
}
type Batch = ReturnType<typeof batch>;
type Request = { key: string | undefined; user: string | undefined; org: string | undefined; payload: { report_ids: string[]; report_versions: Record<string, number>; method: string; note?: string | null } };
type Wire = {
  reports: Report[]; posts: Request[]; lookups: string[]; detailReads: number;
  eligiblePages: number[]; batchPages: number[]; committed: boolean;
  created?: Batch; postStatus: number; postCode: string; lookupStatus: number; lookupOffline: boolean;
  lost: boolean; commitOnLost: boolean; holdPost: boolean; heldPost?: Route;
  holdLookup: boolean; heldLookup?: Route; receiptPatch?: (value: Record<string, unknown>) => Record<string, unknown>;
  detailStatus: number; holdDetail: boolean; heldDetails: Route[];
  eligibleStatus: number; holdEligible: boolean; heldEligible: Route[]; holdBatch: boolean; heldBatches: Route[];
  holdOrganizations: boolean; heldOrganizations: Route[];
  scopedRequests: { account: string; path: string; method: string }[];
};
const wires = new WeakMap<Page, Wire>();
const json = (value: unknown, status = 200) => ({ status, contentType: "application/json", body: JSON.stringify(value) });
const create = (page: Page) => page.getByRole("button", { name: /^Create payout batch/ });
const check = (page: Page) => page.getByRole("button", { name: /Check.*(?:batch|payout).*(?:outcome|status)|Check saved batch/i });
const retry = (page: Page) => page.getByRole("button", { name: "Retry original request", exact: true });
const selected = (page: Page, row: Report) => page.getByRole("checkbox", { name: `Select ${row.title}`, exact: true });
const reference = (page: Page) => page.evaluate((slot) => sessionStorage.getItem(slot), SLOT);
const receipt = (key: string, value: Batch) => ({ id: "synthetic-create-receipt", status: "committed", command_key: key, operation: "reimbursement_created", target_id: value.id, result: value });

async function setup(page: Page, rows = [report(1)], options: { holdEligible?: boolean; holdBatch?: boolean; holdOrganizations?: boolean } = {}) {
  const wire: Wire = {
    reports: rows, posts: [], lookups: [], detailReads: 0, eligiblePages: [], batchPages: [], committed: false,
    postStatus: 201, postCode: "reimbursement_selection_conflict", lookupStatus: 200, lookupOffline: false,
    lost: false, commitOnLost: true, holdPost: false, holdLookup: false, detailStatus: 200, holdDetail: false,
    heldDetails: [], eligibleStatus: 200, holdEligible: options.holdEligible ?? false, heldEligible: [], holdBatch: options.holdBatch ?? false, heldBatches: [], scopedRequests: [],
    holdOrganizations: options.holdOrganizations ?? false, heldOrganizations: [],
  };
  wires.set(page, wire);
  await page.addInitScript(() => {
    if (!sessionStorage.getItem("reimbursement-create-seeded")) {
      localStorage.setItem("invoiceiq_token", "synthetic-payout-A");
      sessionStorage.setItem("reimbursement-create-seeded", "1");
    }
  });
  await page.route("**/api/v1/**", async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    const path = url.pathname.replace("/api/v1", "");
    const account = req.headers().authorization === "Bearer synthetic-payout-B" ? "B" : "A";
    const user = account === "B" ? { ...USER, id: "payout-user-B", org_id: "payout-org-B", name: "Synthetic second payout owner" } : USER;
    const org = account === "B" ? { ...ORG, id: "payout-org-B", name: "Synthetic second payout workspace" } : ORG;
    if (path === "/auth/me") return route.fulfill(json({ user, organization: org, permissions: ["invoice.read", "expense.read", "expense.write", "expense.approve"] }));
    if (path === "/auth/organizations") {
      if (wire.holdOrganizations) { wire.heldOrganizations.push(route); return; }
      return route.fulfill(json([{ org_id: org.id, name: org.name, current: true, role: user.role }]));
    }
    if (path === "/auth/logout") return route.fulfill(json({}));
    if (path === "/auth/login") return route.fulfill(json({ user: { ...USER, id: "payout-user-B", org_id: "payout-org-B", name: "Synthetic second payout owner" }, organization: { ...ORG, id: "payout-org-B", name: "Synthetic second payout workspace" }, token: { access_token: "synthetic-payout-B" } }));
    if (path === "/modules") return route.fulfill(json([{ key: "expenses", name: "Employee expenses", enabled: true, ready: true, requires_issuer: false }]));
    if (path === "/reimbursements" || path.startsWith("/reimbursements/")) wire.scopedRequests.push({ account, path, method: req.method() });
    // Old endpoint aliases let the same regression exercise the frozen control.
    // The candidate UI uses only the bounded reimbursement read contracts.
    if (path === "/reimbursements/eligible" || (path === "/expenses" && req.method() === "GET")) {
      const pageNumber = Number(url.searchParams.get("page") ?? 1);
      const size = Number(url.searchParams.get("page_size") ?? 100);
      expect(size).toBeLessThanOrEqual(100);
      wire.eligiblePages.push(pageNumber);
      if (wire.holdEligible) { wire.heldEligible.push(route); return; }
      if (wire.eligibleStatus !== 200) return route.fulfill(json({ detail: "Synthetic eligible reports unavailable" }, wire.eligibleStatus));
      const items = account === "B" ? [] : wire.reports.filter((row) => !wire.committed || !wire.created?.reports.some((saved) => saved.id === row.id));
      return route.fulfill(json({ items: items.slice((pageNumber - 1) * size, pageNumber * size), total: items.length, page: pageNumber, page_size: size }));
    }
    if (path === "/reimbursements/page" || (path === "/reimbursements" && req.method() === "GET")) {
      const pageNumber = Number(url.searchParams.get("page") ?? 1);
      const size = Number(url.searchParams.get("page_size") ?? 100);
      expect(size).toBeLessThanOrEqual(100);
      wire.batchPages.push(pageNumber);
      if (wire.holdBatch) { wire.heldBatches.push(route); return; }
      const items = account === "A" && wire.committed && wire.created ? [wire.created] : [];
      return route.fulfill(json(path === "/reimbursements" ? items : { items, total: items.length, page: pageNumber, page_size: size }));
    }
    if (path === "/reimbursements" && req.method() === "POST") {
      const payload = req.postDataJSON() as Request["payload"];
      const headers = req.headers();
      wire.posts.push({ key: headers["idempotency-key"], user: headers["x-expected-user-id"], org: headers["x-expected-org-id"], payload });
      wire.created = batch(payload.report_ids.map((id) => wire.reports.find((row) => row.id === id)!));
      if (wire.holdPost) { wire.heldPost = route; return; }
      if (wire.postStatus !== 201) return route.fulfill(json({ detail: "A selected report changed. Refresh the eligible reports before trying again.", code: wire.postCode }, wire.postStatus));
      wire.committed = !wire.lost || wire.commitOnLost;
      if (wire.lost) return route.abort("failed");
      return route.fulfill(json(wire.created, 201));
    }
    if (path.startsWith("/reimbursements/commands/")) {
      expect(req.method()).toBe("GET");
      const key = path.slice("/reimbursements/commands/".length);
      wire.lookups.push(key);
      if (wire.holdLookup) { wire.heldLookup = route; return; }
      if (account === "B") return route.fulfill(json({ detail: "No committed outcome is visible" }, 404));
      expect(req.headers()["x-expected-user-id"]).toBe(USER.id);
      expect(req.headers()["x-expected-org-id"]).toBe(ORG.id);
      if (wire.lookupOffline) return route.abort("internetdisconnected");
      if (wire.lookupStatus !== 200 || !wire.committed || !wire.created) return route.fulfill(json({ detail: "No committed outcome is visible" }, wire.lookupStatus === 200 ? 404 : wire.lookupStatus));
      const value = receipt(key, wire.created);
      return route.fulfill(json(wire.receiptPatch ? wire.receiptPatch(value) : value));
    }
    if (path === `/reimbursements/${BATCH_ID}`) {
      wire.detailReads += 1;
      expect(req.method()).toBe("GET");
      if (wire.holdDetail) { wire.heldDetails.push(route); return; }
      if (wire.detailStatus !== 200) return route.fulfill(json({ detail: "Synthetic saved batch readback unavailable" }, wire.detailStatus));
      return route.fulfill(json(wire.created ?? batch(rows)));
    }
    if (path === "/expenses/summary") return route.fulfill(json({ my_reimbursable: "0", reclaimable_vat: "0", my_submitted: 0, pending_approvals: 0 }));
    if (path === "/expenses") return route.fulfill(json({ items: [], total: 0, page: 1, page_size: 100 }));
    if (path === "/expenses/transactions") return route.fulfill(json([]));
    return route.fulfill(json([]));
  });
  await page.goto("/reimbursements");
  await expect(page.getByRole("heading", { level: 1, name: "Reimbursements", exact: true })).toBeVisible();
  return wire;
}

test.afterEach(async ({ page }, info) => {
  const wire = wires.get(page);
  if (wire) await info.attach("synthetic-reimbursement-create-wire", { body: JSON.stringify(wire, (key, value) => key.startsWith("held") ? undefined : value, 2), contentType: "application/json" });
});

test("exact selected cents create one scoped versioned batch and await current readback", async ({ page }) => {
  const rows = [report(1, "0.10"), report(2, "0.20", { status: "marked_for_reimbursement" })];
  const wire = await setup(page, rows, { holdBatch: true });
  await selected(page, rows[0]).check();
  await selected(page, rows[1]).check();
  await expect(create(page)).toContainText("€0.30");
  await page.screenshot({ path: test.info().outputPath("eligible-desktop.png"), fullPage: true });
  wire.holdDetail = true;
  await create(page).click();
  await expect.poll(() => wire.posts.length).toBe(1);
  await expect.poll(() => wire.heldDetails.length).toBe(1);
  await expect(create(page)).toBeDisabled();
  // A list response can reveal the just-created row while its authoritative
  // readback is still pending. None of its financial actions may race recovery.
  wire.holdBatch = false;
  for (const route of wire.heldBatches.splice(0)) await route.fulfill(json({ items: [wire.created], total: 1, page: 1, page_size: 100 }));
  for (const name of ["Cancel", "Record payment", "Export CSV", "Export SEPA"]) {
    await expect(page.getByRole("button", { name, exact: true })).toBeDisabled();
  }
  await expect(page.getByRole("textbox", { name: "Payment reference", exact: true })).toBeDisabled();
  expect(wire.posts[0]).toMatchObject({ user: USER.id, org: ORG.id, payload: { report_ids: rows.map((row) => row.id), report_versions: Object.fromEntries(rows.map((row) => [row.id, row.version])), method: "bank_transfer" } });
  expect(wire.posts[0].key).toMatch(/^[A-Za-z0-9:_-]{1,120}$/);
  wire.holdDetail = false;
  await wire.heldDetails.shift()!.fulfill(json(wire.created));
  await expect.poll(() => wire.detailReads).toBe(1);
  await expect.poll(() => reference(page)).toBeNull();
  await expect(create(page)).toBeDisabled();
  expect(wire.posts).toHaveLength(1);
});

test("unconverted foreign reports are explicit and cannot join an EUR batch", async ({ page }) => {
  const foreign = report(3, "99.00", { currency: "USD", total_eur: null });
  const local = report(4, "0.30", { total_eur: null });
  const wire = await setup(page, [foreign, local]);
  await expect(selected(page, foreign)).toBeDisabled();
  await expect(page.getByRole("row").filter({ hasText: foreign.title })).toContainText(/conversion|EUR.*(unavailable|missing|unknown)/i);
  await selected(page, local).check();
  await expect(create(page)).toContainText("€0.30");
  await create(page).click();
  await expect.poll(() => wire.posts.length).toBe(1);
  expect(wire.posts[0].payload.report_ids).toEqual([local.id]);
});

test("selection retains exact observed versions across bounded eligible pages", async ({ page }) => {
  const rows = Array.from({ length: 102 }, (_, i) => report(i + 1, i === 100 ? "0.20" : "0.10"));
  const wire = await setup(page, rows);
  await selected(page, rows[0]).check();
  await page.getByRole("navigation", { name: "Eligible report pages", exact: true }).getByRole("button", { name: "Next", exact: true }).click();
  await expect(selected(page, rows[100])).toBeVisible();
  await selected(page, rows[100]).check();
  await expect(create(page)).toContainText("€0.30");
  await create(page).click();
  await expect.poll(() => wire.posts.length).toBe(1);
  expect(wire.posts[0].payload.report_ids).toEqual([rows[0].id, rows[100].id]);
  expect(wire.posts[0].payload.report_versions).toEqual({ [rows[0].id]: rows[0].version, [rows[100].id]: rows[100].version });
  expect(wire.eligiblePages).toContain(2);
  await expect(page.getByRole("navigation", { name: "Eligible report pages", exact: true })).toContainText("100 eligible reports · Page 1 of 1");
  await expect(page.getByText("No approved reports awaiting payout.", { exact: true })).toHaveCount(0);
});

test("rapid activations freeze selection before digest and send one command", async ({ page }) => {
  await page.addInitScript(() => {
    const original = crypto.subtle.digest.bind(crypto.subtle);
    const state = window as unknown as { payoutDigestHeld?: boolean; releasePayoutDigest?: () => void };
    let held = false;
    crypto.subtle.digest = async (...args: Parameters<SubtleCrypto["digest"]>) => {
      if (held) return original(...args);
      held = true;
      state.payoutDigestHeld = true;
      await new Promise<void>((resolve) => { state.releasePayoutDigest = resolve; });
      return original(...args);
    };
  });
  const rows = [report(1)];
  const wire = await setup(page, rows);
  await selected(page, rows[0]).check();
  await create(page).evaluate((button: HTMLButtonElement) => { button.click(); button.click(); });
  await expect.poll(() => page.evaluate(() => (window as unknown as { payoutDigestHeld?: boolean }).payoutDigestHeld)).toBe(true);
  await expect(selected(page, rows[0])).toBeDisabled();
  await expect(create(page)).toBeDisabled();
  expect(wire.posts).toHaveLength(0);
  await page.evaluate(() => (window as unknown as { releasePayoutDigest: () => void }).releasePayoutDigest());
  await expect.poll(() => wire.posts.length).toBe(1);
  await expect.poll(() => wire.detailReads).toBe(1);
  expect(wire.posts).toHaveLength(1);
});

test("a lost create response recovers the committed receipt without another POST", async ({ page }) => {
  const rows = [report(1)];
  const wire = await setup(page, rows);
  wire.lost = true;
  await selected(page, rows[0]).check();
  await create(page).click();
  await expect(check(page)).toBeVisible();
  await check(page).click();
  await expect.poll(() => wire.lookups.length).toBeGreaterThan(0);
  await expect.poll(() => wire.detailReads).toBe(1);
  await expect.poll(() => reference(page)).toBeNull();
  expect(wire.posts).toHaveLength(1);
  expect(wire.lookups).toEqual([wire.posts[0].key]);
});

test("an absent receipt stays unresolved and retry reuses the exact original key and payload", async ({ page }) => {
  const rows = [report(1)];
  const wire = await setup(page, rows);
  wire.lost = true;
  wire.commitOnLost = false;
  await selected(page, rows[0]).check();
  await create(page).click();
  await expect(check(page)).toBeVisible();
  await expect(create(page)).toBeDisabled();
  await expect(selected(page, rows[0])).toBeDisabled();
  await expect.poll(() => reference(page)).not.toBeNull();
  const first = wire.posts[0];
  await check(page).click();
  await expect(page.getByRole("alert").filter({ hasText: /unresolved|No committed|not.*committed/i })).toBeVisible();
  expect(wire.posts).toHaveLength(1);
  wire.lost = false;
  await retry(page).click();
  await expect.poll(() => wire.posts.length).toBe(2);
  expect(wire.posts[1]).toEqual(first);
  await expect.poll(() => wire.detailReads).toBe(1);
});

for (const failure of ["server", "offline"] as const) {
  test(`a ${failure} receipt lookup keeps the saved command and later checks only GET`, async ({ page }) => {
    const rows = [report(1)];
    const wire = await setup(page, rows);
    wire.lost = true;
    wire.lookupStatus = failure === "server" ? 503 : 200;
    wire.lookupOffline = failure === "offline";
    await selected(page, rows[0]).check();
    await create(page).click();
    await expect(check(page)).toBeVisible();
    await expect.poll(() => reference(page)).not.toBeNull();
    await check(page).click();
    await expect.poll(() => wire.lookups.length).toBe(1);
    await expect.poll(() => reference(page)).not.toBeNull();
    const original = wire.posts[0].key;
    wire.lookupStatus = 200;
    wire.lookupOffline = false;
    await check(page).click();
    await expect.poll(() => wire.detailReads).toBe(1);
    await expect.poll(() => reference(page)).toBeNull();
    expect(wire.posts).toHaveLength(1);
    expect(wire.lookups.every((key) => key === original)).toBe(true);
  });
}

test("a malformed committed receipt cannot discard its saved reference", async ({ page }) => {
  const rows = [report(1)];
  const wire = await setup(page, rows);
  wire.lost = true;
  wire.receiptPatch = (value) => ({ ...value, target_id: "10000000-0000-4000-8000-000000000002" });
  await selected(page, rows[0]).check();
  await create(page).click();
  await expect(check(page)).toBeVisible();
  await check(page).click();
  await expect.poll(() => wire.lookups.length).toBe(1);
  await expect.poll(() => reference(page)).not.toBeNull();
  await expect(create(page)).toBeDisabled();
  expect(wire.detailReads).toBe(0);
  wire.receiptPatch = undefined;
  await check(page).click();
  await expect.poll(() => wire.detailReads).toBe(1);
  expect(wire.posts).toHaveLength(1);
});

test("reload preserves the saved request and checks its receipt before another batch", async ({ page }) => {
  const rows = [report(1)];
  const wire = await setup(page, rows);
  wire.lost = true;
  wire.lookupStatus = 503;
  await selected(page, rows[0]).check();
  await create(page).click();
  await expect(check(page)).toBeVisible();
  const saved = await reference(page);
  expect(saved).not.toBeNull();
  await page.reload();
  await expect(check(page)).toBeVisible();
  await expect(create(page)).toBeDisabled();
  expect(await reference(page)).toBe(saved);
  expect(wire.posts).toHaveLength(1);
  wire.lookupStatus = 200;
  await check(page).click();
  await expect.poll(() => wire.detailReads).toBe(1);
  expect(wire.posts).toHaveLength(1);
});

test("a stale selection refusal preserves selected reports for review", async ({ page }) => {
  const rows = [report(1), report(2)];
  const wire = await setup(page, rows);
  wire.postStatus = 409;
  await selected(page, rows[0]).check();
  await selected(page, rows[1]).check();
  await create(page).click();
  await expect(page.getByRole("alert").filter({ hasText: /selected report changed|selected.*changed|refresh.*eligible/i })).toBeVisible();
  await expect(selected(page, rows[0])).toBeChecked();
  await expect(selected(page, rows[1])).toBeChecked();
  expect(wire.posts).toHaveLength(1);
});

test("a committed batch with failed current readback offers read-only recovery", async ({ page }) => {
  const rows = [report(1)];
  const wire = await setup(page, rows);
  wire.detailStatus = 503;
  await selected(page, rows[0]).check();
  await create(page).click();
  await expect(page.getByRole("alert").filter({ hasText: /saved|created|committed/i })).toBeVisible();
  await expect(create(page)).toBeDisabled();
  const recovery = page.getByRole("button", { name: /(?:Retry|Refresh|Load).*saved batch|Reload.*batch/i });
  await expect(recovery).toBeVisible();
  const before = wire.detailReads;
  wire.detailStatus = 200;
  await recovery.click();
  await expect.poll(() => wire.detailReads).toBeGreaterThan(before);
  expect(wire.posts).toHaveLength(1);
});

test("eligible loading and error states do not assert an empty payable queue", async ({ page }) => {
  const wire = await setup(page, [], { holdEligible: true, holdOrganizations: true });
  await expect.poll(() => wire.heldEligible.length).toBe(1);
  await expect.poll(() => wire.heldOrganizations.length).toBe(1);
  // The sidebar can load organizations at the same time as this queue. Scope
  // to the queue's exact status so unrelated navigation text cannot match it.
  const eligibleLoading = page.getByRole("status").filter({ hasText: /^Loading eligible reports…$/ });
  const organizationsLoading = page.getByRole("status").filter({ hasText: /^Loading organization choices…$/ });
  await expect(organizationsLoading).toBeVisible();
  await expect(eligibleLoading).toBeVisible();
  await test.info().attach("concurrent-loading-locators", {
    body: JSON.stringify({
      oldBroadSelectorMatches: await page.getByText(/Loading.*(?:eligible|approved|report)/i).count(),
      eligibleStatusMatches: await eligibleLoading.count(),
      organizationStatusMatches: await organizationsLoading.count(),
    }),
    contentType: "application/json",
  });
  await expect(page.getByText("No approved reports awaiting payout.", { exact: true })).toHaveCount(0);
  wire.holdEligible = false;
  wire.eligibleStatus = 503;
  await wire.heldEligible.shift()!.fulfill(json({ detail: "Synthetic eligible queue unavailable" }, 503));
  await expect(page.getByRole("alert").filter({ hasText: /load.*(?:eligible|approved|report)/i })).toBeVisible();
  await expect(organizationsLoading).toBeVisible();
  await expect(page.getByText("No approved reports awaiting payout.", { exact: true })).toHaveCount(0);
  expect(wire.posts).toHaveLength(0);
  wire.holdOrganizations = false;
  for (const route of wire.heldOrganizations.splice(0)) {
    await route.fulfill(json([{ org_id: ORG.id, name: ORG.name, current: true, role: USER.role }]));
  }
  await expect(organizationsLoading).toHaveCount(0);
  await expect(page.getByRole("alert").filter({ hasText: /load.*(?:eligible|approved|report)/i })).toBeVisible();
  expect(wire.posts).toHaveLength(0);
});

test("the selected-report controls and table remain contained at 390px with enlarged text", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.addInitScript(() => document.addEventListener("DOMContentLoaded", () => { document.documentElement.style.fontSize = "32px"; }));
  const rows = [report(1, "999999999999.99", { title: "Synthetic depot visit with a long claim title", employee_name: "Synthetic claimant with a long display name" })];
  await setup(page, rows);
  await selected(page, rows[0]).check();
  await expect(create(page)).toContainText("€999,999,999,999.99");
  const bounds = await page.evaluate(() => ({ client: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth }));
  expect(bounds.scroll).toBeLessThanOrEqual(bounds.client + 1);
  await expect(selected(page, rows[0])).toBeVisible();
  await page.screenshot({ path: test.info().outputPath("eligible-390-root32.png"), fullPage: true });
});

test("leaving a pending batch retains its reference and returning checks only its recorded outcome", async ({ page }) => {
  const rows = [report(1)];
  const wire = await setup(page, rows);
  wire.holdPost = true;
  await selected(page, rows[0]).check();
  await create(page).click();
  await expect.poll(() => wire.heldPost !== undefined).toBe(true);
  await expect.poll(() => reference(page)).not.toBeNull();
  const original = wire.posts[0];
  await page.getByRole("link", { name: /Expenses/, exact: false }).filter({ hasText: "← Expenses" }).click();
  await expect(page).toHaveURL(/\/expenses$/);
  await expect(page.getByRole("heading", { level: 1, name: "Expenses", exact: true })).toBeVisible();
  // Model a server commit after the initiating view has gone. The retired
  // component must not publish success or launch a replacement request.
  wire.committed = true;
  await wire.heldPost!.fulfill(json(wire.created, 201)).catch(() => undefined);
  await expect(page.getByText("Payout batch created", { exact: true })).toHaveCount(0);
  await page.goto("/reimbursements");
  await expect(check(page)).toBeVisible();
  await expect(create(page)).toBeDisabled();
  await check(page).click();
  await expect.poll(() => wire.detailReads).toBe(1);
  expect(wire.posts).toHaveLength(1);
  expect(wire.lookups.every((key) => key === original.key)).toBe(true);
});

test("a replacement account cannot continue or display the previous account's pending batch", async ({ page, context }) => {
  const rows = [report(1)];
  const wire = await setup(page, rows);
  wire.holdPost = true;
  await selected(page, rows[0]).check();
  await create(page).click();
  await expect.poll(() => wire.heldPost !== undefined).toBe(true);
  await expect.poll(() => reference(page)).not.toBeNull();
  const original = wire.posts[0];
  const sender = await context.newPage();
  await sender.route("**/payout-storage-sender", (route) => route.fulfill({ contentType: "text/html", body: "<!doctype html><title>Synthetic identity sender</title>" }));
  await sender.goto("http://localhost:5173/payout-storage-sender");
  await sender.evaluate(() => localStorage.setItem("invoiceiq_token", "synthetic-payout-B"));
  await expect(page.getByRole("button", { name: "Account menu", exact: true })).toContainText("Synthetic second payout owner");
  wire.committed = true;
  await wire.heldPost!.fulfill(json(wire.created, 201)).catch(() => undefined);
  await expect(page.getByText(rows[0].title, { exact: true })).toHaveCount(0);
  await expect(page.getByText("Payout batch created", { exact: true })).toHaveCount(0);
  await expect(check(page)).toHaveCount(0);
  expect(wire.posts).toHaveLength(1);
  expect(wire.scopedRequests.filter((request) => request.account === "B" && (request.method === "POST" || request.path.includes(original.key!)))).toEqual([]);
  expect(await reference(page)).not.toBeNull();
  await sender.close();
});

test("a native create timeout retains one uncertain command and permits GET-only recovery", async ({ page }) => {
  const rows = [report(1)];
  const wire = await setup(page, rows);
  wire.holdPost = true;
  await selected(page, rows[0]).check();
  const failed = page.waitForEvent("requestfailed", {
    predicate: (request) => request.method() === "POST" && new URL(request.url()).pathname === "/api/v1/reimbursements",
    timeout: 20_000,
  });
  await create(page).click();
  await expect.poll(() => wire.heldPost !== undefined).toBe(true);
  // Exercise the real bounded Axios request; no fake timers or network timeout
  // substitution. The larger expectation belongs to this new 15s contract only.
  await expect(check(page)).toBeVisible({ timeout: 20_000 });
  expect((await failed).failure()?.errorText).toMatch(/aborted/i);
  await expect.poll(() => reference(page)).not.toBeNull();
  expect(wire.posts).toHaveLength(1);
  expect(wire.lookups).toHaveLength(0);
  wire.committed = true;
  await check(page).click();
  await expect.poll(() => wire.detailReads).toBe(1);
  expect(wire.posts).toHaveLength(1);
});

test("a total beyond the supported batch range remains selected but cannot be posted", async ({ page }) => {
  const rows = [report(1, "999999999999.99"), report(2, "0.01")];
  const wire = await setup(page, rows);
  await selected(page, rows[0]).check();
  await selected(page, rows[1]).check();
  await expect(create(page)).toBeDisabled();
  await expect(page.getByRole("alert").filter({ hasText: /maximum|range|split|999999999999|999,999,999,999/i })).toBeVisible();
  await expect(selected(page, rows[0])).toBeChecked();
  await expect(selected(page, rows[1])).toBeChecked();
  expect(wire.posts).toHaveLength(0);
});

for (const width of [320, 1280]) {
  test(`populated eligible payout facts and native selection stay contained at ${width}px`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 844 });
    const rows = [report(1, "999999999999.99", { title: "Synthetic depot visit with a long claim title", employee_name: "Synthetic claimant with a long display name" }), report(2, "36.99", { title: "Synthetic foreign-currency claim", currency: "USD", total_eur: "33.33" })];
    const wire = await setup(page, rows);
    const region = page.getByRole("region", { name: "Eligible expense reports", exact: true });
    await region.focus(); await page.keyboard.press("Tab");
    await expect(selected(page, rows[0])).toBeFocused(); await page.keyboard.press("Space");
    await expect(selected(page, rows[0])).toBeChecked();
    await expect(create(page)).toContainText("€999,999,999,999.99");
    await expect(region).toContainText(/US\$\s*36\.99/); await expect(region).toContainText("€33.33");
    await region.focus();
    if (await region.evaluate(e => e.scrollWidth > e.clientWidth)) { await page.keyboard.press("ArrowRight"); await expect.poll(() => region.evaluate(e => e.scrollLeft)).toBeGreaterThan(0); }
    const bounds = await page.evaluate(() => ({ width: innerWidth, root: document.documentElement.scrollWidth, body: document.body.scrollWidth }));
    expect(bounds.root).toBeLessThanOrEqual(width); expect(bounds.body).toBeLessThanOrEqual(width);
    expect(wire.posts).toEqual([]);
    await info.attach("eligible-payout-reflow", { body: JSON.stringify({ bounds, scope: "Synthetic Chromium populated eligible table and native selection/local scrolling; no payout write or physical-device/full accessibility acceptance." }), contentType: "application/json" });
  });
}
