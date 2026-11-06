import { test, expect, type Page } from "@playwright/test";

const ORG = { id: "recurring-org-A", name: "Synthetic recurring workspace", status: "active" };
const USER = { id: "recurring-user-A", org_id: ORG.id, name: "Synthetic recurring operator", email: "recurring@example.test", role: "owner" };
const PROFILE = { id: "recurring-issuer", legal_name: "Synthetic seller", is_complete: true, missing_fields: [], is_default: true, invoice_prefix: "SYN", credit_note_prefix: "SCN", default_penalty_rate: null };
const SCHEDULE = {
  id: "observed-schedule-text-id", title: "Observed canonical schedule", partner_id: null,
  frequency: "monthly", interval: 1, start_date: "2026-12-05", next_run_date: "2026-12-05", end_date: null,
  active: true, payment_terms_days: null, last_generated_at: null, generated_count: 0, created_at: "2026-11-05T16:00:00Z",
};
function held() { let release!: () => void; const wait = new Promise<void>((resolve) => { release = resolve; }); return { entered: false, release, wait }; }
type Held = ReturnType<typeof held>;
type Wire = {
  writes: Array<{ method: string; path: string; account: string; body: unknown; headers: Record<string, string> }>;
  reads: Array<{ path: string; account: string }>;
  rows: Record<string, Array<typeof SCHEDULE>>;
  statuses: number[]; response?: unknown; postHold?: Held; listHold?: Held; listFailure: boolean;
  postCompleted: number; abortPost: boolean; issuedReads: number[];
};
function invoice(index: number) {
  return { id: `recurring-invoice-${index}`, number: `REC-${index}`, issuer_id: PROFILE.id, buyer_name: "Synthetic buyer", buyer_email: null,
    issue_date: "2026-11-05", due_date: null, currency: "EUR", total: "121.00", subtotal: "100.00", tax_total: "21.00", lifecycle: "issued", doc_type: "invoice", status: "open",
    amount_paid: "0.00", credited_total: "0.00", outstanding: "121.00", sent_at: null, reminder_count: 0, last_reminder_at: null, vat_scheme: "standard", lines: [] };
}
async function setup(page: Page, writePermission = true, issuing = true, issuedCount = 0): Promise<Wire> {
  const wire: Wire = { writes: [], reads: [], rows: { A: [], B: [] }, statuses: [], listFailure: false, postCompleted: 0, abortPost: false, issuedReads: [] };
  await page.addInitScript(() => {
    if (!sessionStorage.getItem("recurring-fixture-seeded")) {
      localStorage.setItem("invoiceiq_token", "recurring-account-A"); sessionStorage.setItem("recurring-fixture-seeded", "1");
    }
  });
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname.replace("/api/v1", "");
    const account = request.headers().authorization === "Bearer recurring-account-B" ? "B" : "A";
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (["POST", "PUT", "PATCH", "DELETE"].includes(request.method())) wire.writes.push({ method: request.method(), path, account, body: request.postDataJSON(), headers: request.headers() });
    const organization = account === "B" ? { ...ORG, id: "recurring-org-B", name: "Replacement recurring workspace" } : ORG;
    const user = account === "B" ? { ...USER, id: "recurring-user-B", org_id: organization.id } : USER;
    if (path === "/auth/me") return json({ user, organization, permissions: ["issued.read", ...(writePermission ? ["issued.write"] : [])] });
    if (path === "/auth/organizations") return json([{ org_id: organization.id, name: organization.name, current: true }]);
    if (path === "/modules") return json([{ key: "issuing", enabled: issuing, ready: issuing, requires_issuer: true }]);
    if (path === "/issuer") return json(PROFILE);
    if (path === "/issuer/registry") return json([PROFILE]);
    if (path === "/issued" && request.method() === "GET") {
      const requested = Number(url.searchParams.get("page") ?? 1); wire.issuedReads.push(requested);
      const rows = Array.from({ length: issuedCount }, (_, index) => invoice(index + 1));
      return json({ items: rows.slice((requested - 1) * 100, requested * 100), total: rows.length });
    }
    if (path === "/issued/recurring" && request.method() === "GET") {
      wire.reads.push({ path, account });
      if (wire.listHold) { wire.listHold.entered = true; await wire.listHold.wait; }
      if (wire.listFailure) return json({ detail: "Synthetic schedules list unavailable" }, 503);
      return json(wire.rows[account]);
    }
    if (path === "/issued/recurring" && request.method() === "POST") {
      const status = wire.statuses.shift() ?? 201;
      if (wire.postHold) { wire.postHold.entered = true; await wire.postHold.wait; }
      wire.postCompleted++;
      if (wire.abortPost) return route.abort("failed");
      if (status >= 400) return json({ detail: status === 422 ? "Synthetic schedule validation refused" : "Synthetic uncertain create response" }, status);
      if (wire.response === undefined) wire.rows[account] = [SCHEDULE];
      return json(wire.response === undefined ? SCHEDULE : wire.response, status);
    }
    return json([]);
  });
  await page.goto("/issue?context=kept");
  if (issuing) await expect(page.getByRole("heading", { name: "Recurring schedules", exact: true })).toBeVisible();
  return wire;
}
const card = (page: Page) => page.locator(".card").filter({ has: page.getByRole("heading", { name: "Recurring schedules", exact: true }) });
const notice = (page: Page) => page.getByRole("status", { name: "Recurring schedule outcome", exact: true });
async function fill(page: Page) {
  await card(page).getByRole("button", { name: "New schedule", exact: true }).click();
  await card(page).getByLabel("Schedule title", { exact: true }).fill("Requested schedule title");
  await card(page).getByLabel("Customer name", { exact: true }).fill("Synthetic recurring buyer");
  await card(page).getByLabel("Start date", { exact: true }).fill("2026-12-05");
  await card(page).getByLabel("Line 1 description", { exact: true }).fill("Synthetic recurring service");
  await card(page).getByLabel("Line 1 unit price", { exact: true }).fill("123.45");
}

test("verified201 publishes a named past-tense outcome before held list refresh and keeps create pending", async ({ page }) => {
  const wire = await setup(page); await fill(page); wire.postHold = held(); wire.listHold = held();
  try {
    await card(page).getByRole("button", { name: "Create schedule", exact: true }).click();
    await expect.poll(() => wire.postHold!.entered).toBe(true);
    await expect(notice(page)).toHaveCount(0);
    wire.postHold.release();
    await expect(notice(page)).toContainText("Observed canonical schedule");
    await expect(notice(page)).toContainText(/created/i);
    await expect(notice(page)).toContainText(/Dec.*2026|2026-12-05/);
    await expect(notice(page)).not.toContainText(SCHEDULE.id);
    await expect.poll(() => wire.listHold!.entered).toBe(true);
    await expect(card(page).getByRole("button", { name: "Creating…", exact: true })).toBeDisabled();
    await expect(card(page).getByRole("button", { name: "Refresh schedules", exact: true })).toBeDisabled();
    wire.listHold.release();
    await expect(card(page).getByLabel("Schedule title", { exact: true })).toHaveCount(0);
    expect(wire.writes).toHaveLength(1);
    expect(wire.writes[0]).toMatchObject({ method: "POST", path: "/issued/recurring", account: "A" });
  } finally { wire.postHold.release(); wire.listHold.release(); }
});

test("a verified creation remains acknowledged after failed refresh and recovers with GET only", async ({ page }) => {
  const wire = await setup(page); await fill(page); wire.listFailure = true;
  await card(page).getByRole("button", { name: "Create schedule", exact: true }).click();
  await expect(notice(page)).toContainText("Observed canonical schedule");
  await expect(card(page).getByText("Couldn’t load the schedules", { exact: true })).toBeVisible();
  await expect(card(page).getByRole("button", { name: "Refresh schedules", exact: true })).toBeEnabled();
  wire.listFailure = false;
  await card(page).getByRole("button", { name: "Refresh schedules", exact: true }).click();
  await expect(card(page).getByRole("cell", { name: "Observed canonical schedule", exact: true })).toBeVisible();
  await expect(notice(page)).toContainText(/created/i);
  expect(wire.writes).toHaveLength(1); expect(wire.reads.every((read) => read.path === "/issued/recurring")).toBe(true);
});

test("the old validation error clears only after a verified201 rather than while a retry is pending", async ({ page }) => {
  const wire = await setup(page); wire.statuses = [422, 201]; await fill(page);
  await card(page).getByRole("button", { name: "Create schedule", exact: true }).click();
  await expect(card(page).getByRole("alert")).toContainText("Synthetic schedule validation refused");
  wire.postHold = held();
  try {
    await card(page).getByRole("button", { name: "Create schedule", exact: true }).click();
    await expect.poll(() => wire.postHold!.entered).toBe(true);
    await expect(card(page).getByRole("alert")).toContainText("Synthetic schedule validation refused");
    await expect(notice(page)).toHaveCount(0);
    wire.postHold.release();
    await expect(notice(page)).toContainText("Observed canonical schedule");
    await expect(card(page).getByRole("alert")).toHaveCount(0);
    expect(wire.writes).toHaveLength(2);
  } finally { wire.postHold.release(); }
});

test("closing and reopening during create keeps pending and preserves the fresh editor after the previous result", async ({ page }) => {
  const wire = await setup(page); await fill(page); wire.postHold = held(); wire.listHold = held();
  try {
    await card(page).getByRole("button", { name: "Create schedule", exact: true }).click();
    await expect.poll(() => wire.postHold!.entered).toBe(true);
    await card(page).getByRole("button", { name: "Close", exact: true }).click();
    await card(page).getByRole("button", { name: "New schedule", exact: true }).click();
    await card(page).getByLabel("Schedule title", { exact: true }).fill("Fresh unsaved editor");
    await card(page).getByLabel("Customer name", { exact: true }).fill("Fresh buyer");
    await card(page).getByLabel("Line 1 description", { exact: true }).fill("Fresh unsaved line");
    await expect(card(page).getByRole("button", { name: "Creating…", exact: true })).toBeDisabled();
    wire.postHold.release();
    await expect(notice(page)).toContainText("Observed canonical schedule");
    await expect.poll(() => wire.listHold!.entered).toBe(true);
    await expect(card(page).getByRole("button", { name: "Refresh schedules", exact: true })).toBeDisabled();
    wire.listHold.release();
    await expect(card(page).getByLabel("Schedule title", { exact: true })).toHaveValue("Fresh unsaved editor");
    await expect(card(page).getByLabel("Line 1 description", { exact: true })).toHaveValue("Fresh unsaved line");
    await expect(card(page).getByRole("button", { name: "Create schedule", exact: true })).toBeEnabled();
    expect(wire.writes).toHaveLength(1);
  } finally { wire.postHold.release(); wire.listHold.release(); }
});

test("a prior failed read cannot restart a held create invalidation through Try again", async ({ page }) => {
  const wire = await setup(page); await fill(page); wire.listFailure = true;
  await card(page).getByRole("button", { name: "Create schedule", exact: true }).click();
  await expect(card(page).getByText("Couldn’t load the schedules", { exact: true })).toBeVisible();
  await expect(card(page).getByRole("button", { name: "Try again", exact: true })).toBeVisible();
  wire.listFailure = false; wire.listHold = held();
  wire.response = { ...SCHEDULE, id: "second-schedule-text-id", title: "Second canonical result" };
  wire.rows.A = [wire.response as typeof SCHEDULE];
  await fill(page);
  try {
    await card(page).getByRole("button", { name: "Create schedule", exact: true }).click();
    await expect(notice(page)).toContainText("Second canonical result");
    await expect.poll(() => wire.listHold!.entered).toBe(true);
    await expect(card(page).getByRole("button", { name: "Try again", exact: true })).toHaveCount(0);
    await expect(card(page).getByRole("button", { name: "Refresh schedules", exact: true })).toBeDisabled();
    await expect(card(page).getByRole("button", { name: "Creating…", exact: true })).toBeDisabled();
    wire.listHold.release();
    await expect(card(page).getByLabel("Schedule title", { exact: true })).toHaveCount(0);
    expect(wire.writes).toHaveLength(2);
  } finally { wire.listHold.release(); }
});

test("ordinary invoice paging keeps a same-owner late creation outcome", async ({ page }) => {
  const wire = await setup(page, true, true, 105); await fill(page); wire.postHold = held();
  try {
    await card(page).getByRole("button", { name: "Create schedule", exact: true }).click();
    await expect.poll(() => wire.postHold!.entered).toBe(true);
    await page.getByRole("navigation", { name: "Issued invoice pages", exact: true }).getByRole("button", { name: "Next page", exact: true }).click();
    await expect(page.getByText("REC-105", { exact: true })).toBeVisible();
    wire.postHold.release();
    await expect(notice(page)).toContainText("Observed canonical schedule");
    await expect(page).toHaveURL(/context=kept.*page=2/);
    expect(wire.writes).toHaveLength(1); expect(wire.issuedReads).toEqual([1, 2]);
  } finally { wire.postHold.release(); }
});

for (const status of [201, 503]) {
  test(`a late${status} cannot publish or invalidate the replacement account`, async ({ page }) => {
    const wire = await setup(page); wire.statuses = [status]; await fill(page); wire.postHold = held();
    try {
      await card(page).getByRole("button", { name: "Create schedule", exact: true }).click();
      await expect.poll(() => wire.postHold!.entered).toBe(true);
      await page.evaluate(() => {
        const oldValue = localStorage.getItem("invoiceiq_token"); localStorage.setItem("invoiceiq_token", "recurring-account-B");
        window.dispatchEvent(new StorageEvent("storage", { key: "invoiceiq_token", oldValue, newValue: "recurring-account-B", storageArea: localStorage }));
      });
      await expect(page.getByText("Replacement recurring workspace", { exact: true }).first()).toBeVisible();
      await expect(card(page).getByRole("button", { name: "New schedule", exact: true })).toBeVisible();
      const readsBeforeRelease = wire.reads.length;
      wire.postHold.release(); await expect.poll(() => wire.postCompleted).toBe(1); await page.waitForLoadState("networkidle");
      await expect(notice(page)).toHaveCount(0); await expect(card(page).getByRole("alert")).toHaveCount(0);
      expect(wire.reads).toHaveLength(readsBeforeRelease);
      expect(wire.writes).toHaveLength(1); expect(wire.writes[0].account).toBe("A");
    } finally { wire.postHold.release(); }
  });
}

test("changing only the credential token fences the old same-actor response", async ({ page }) => {
  const wire = await setup(page); await fill(page); wire.postHold = held();
  try {
    await card(page).getByRole("button", { name: "Create schedule", exact: true }).click();
    await expect.poll(() => wire.postHold!.entered).toBe(true);
    await page.evaluate(() => localStorage.setItem("invoiceiq_token", "recurring-account-A-renewed"));
    wire.postHold.release(); await expect.poll(() => wire.postCompleted).toBe(1); await page.waitForLoadState("networkidle");
    await expect(notice(page)).toHaveCount(0); await expect(card(page).getByRole("alert")).toHaveCount(0);
    expect(wire.reads).toHaveLength(1); expect(wire.writes).toHaveLength(1);
  } finally { wire.postHold.release(); }
});

test("an unmounted owner cannot refresh or acknowledge into a newly mounted editor", async ({ page }) => {
  const wire = await setup(page); await fill(page); wire.postHold = held();
  try {
    await card(page).getByRole("button", { name: "Create schedule", exact: true }).click();
    await expect.poll(() => wire.postHold!.entered).toBe(true);
    await page.getByRole("link", { name: "Company details", exact: true }).click();
    await expect(page).toHaveURL(/\/issuer$/);
    await expect(page.getByRole("heading", { name: "Issuer companies", exact: true })).toBeVisible();
    await expect(card(page)).toHaveCount(0);
    await page.goBack();
    await expect(card(page).getByRole("button", { name: "New schedule", exact: true })).toBeVisible();
    const readsBeforeRelease = wire.reads.length;
    wire.postHold.release(); await expect.poll(() => wire.postCompleted).toBe(1); await page.waitForLoadState("networkidle");
    await expect(notice(page)).toHaveCount(0); expect(wire.reads).toHaveLength(readsBeforeRelease);
    expect(wire.writes).toHaveLength(1);
  } finally { wire.postHold.release(); }
});

for (const status of [200, 202, 503]) {
  test(`a noncanonical${status} never claims creation or automatically repeats the write`, async ({ page }) => {
    const wire = await setup(page); wire.statuses = [status]; await fill(page);
    await card(page).getByRole("button", { name: "Create schedule", exact: true }).click();
    await expect(card(page).getByRole("alert")).toContainText("creation outcome is unknown");
    await expect(notice(page)).toHaveCount(0); expect(wire.writes).toHaveLength(1);
    await card(page).getByRole("button", { name: "Refresh schedules", exact: true }).click();
    await expect.poll(() => wire.reads.length).toBeGreaterThan(1);
    await expect(notice(page)).toHaveCount(0); expect(wire.writes).toHaveLength(1);
  });
}

const invalidBodies: Array<[string, unknown]> = [
  ["empty body", {}], ["blank id", { ...SCHEDULE, id: "   " }],
  ["numeric title", { ...SCHEDULE, title: 7 }], ["invalid frequency", { ...SCHEDULE, frequency: "fortnightly" }],
  ["invalid calendar date", { ...SCHEDULE, next_run_date: "2026-02-31" }],
];
for (const [name, response] of invalidBodies) {
  test(`an unverified201 with ${name} cannot publish an acknowledgement`, async ({ page }) => {
    const wire = await setup(page); wire.response = response; await fill(page);
    await card(page).getByRole("button", { name: "Create schedule", exact: true }).click();
    await expect(card(page).getByRole("alert")).toContainText("creation outcome is unknown");
    await expect(notice(page)).toHaveCount(0);
    expect(wire.writes).toHaveLength(1); expect(wire.reads).toHaveLength(1);
  });
}

test("a lost POST response offers read-only review without inventing a saved result", async ({ page }) => {
  const wire = await setup(page); wire.abortPost = true; wire.rows.A = [SCHEDULE]; await fill(page);
  await card(page).getByRole("button", { name: "Create schedule", exact: true }).click();
  await expect(card(page).getByRole("alert")).toContainText("request may have been saved");
  await expect(notice(page)).toHaveCount(0);
  await card(page).getByRole("button", { name: "Refresh schedules", exact: true }).click();
  await expect(card(page).getByRole("cell", { name: "Observed canonical schedule", exact: true })).toBeVisible();
  await expect(notice(page)).toHaveCount(0); expect(wire.writes).toHaveLength(1);
  expect(wire.writes[0].headers["idempotency-key"]).toBeUndefined();
});

test("the unchanged input contract keeps typed money and the existing zero-interval fallback", async ({ page }) => {
  const wire = await setup(page); await fill(page);
  await card(page).getByLabel("Every N periods", { exact: true }).fill("0");
  await card(page).getByLabel("Line 1 unit price", { exact: true }).fill("99999999999999.99");
  await card(page).getByRole("button", { name: "Create schedule", exact: true }).click();
  await expect(notice(page)).toBeVisible();
  expect(wire.writes).toHaveLength(1);
  expect(wire.writes[0].body).toEqual({ template: { buyer_name: "Synthetic recurring buyer", buyer_email: null, vat_scheme: "standard", lines: [{ description: "Synthetic recurring service", quantity: "1", unit_price: "99999999999999.99", vat_rate: "21" }] }, frequency: "monthly", interval: 1, start_date: "2026-12-05", end_date: null, title: "Requested schedule title" });
  expect(wire.writes[0].headers["x-expected-org-id"]).toBe(ORG.id); expect(wire.writes[0].headers["x-expected-user-id"]).toBe(USER.id);
  expect(wire.writes[0].headers["idempotency-key"]).toBeUndefined();
});

test("a nullable canonical title uses the existing untitled label while keeping its id internal", async ({ page }) => {
  const wire = await setup(page); wire.response = { ...SCHEDULE, title: null }; await fill(page);
  await card(page).getByRole("button", { name: "Create schedule", exact: true }).click();
  await expect(notice(page)).toContainText("Untitled schedule"); await expect(notice(page)).not.toContainText(SCHEDULE.id);
  expect(wire.writes).toHaveLength(1);
});

test("read permission retains schedule review without gaining create permission", async ({ page }) => {
  const wire = await setup(page, false);
  await expect(card(page).getByRole("heading", { name: "Recurring schedules", exact: true })).toBeVisible();
  await expect(card(page).getByRole("button", { name: "New schedule", exact: true })).toBeDisabled();
  expect(wire.reads).toHaveLength(1); expect(wire.writes).toEqual([]);
});

test("inactive issuing module exposes no recurring create control", async ({ page }) => {
  const wire = await setup(page, true, false);
  await expect(page.getByText("The invoice issuing module isn't active.", { exact: false })).toBeVisible();
  await expect(page.getByRole("button", { name: "New schedule", exact: true })).toHaveCount(0);
  expect(wire.writes).toEqual([]);
});

test("a long malicious canonical title stays escaped and readable at390px with enlarged text", async ({ page }, info) => {
  const wire = await setup(page); const title = '<img src=x onerror="alert(1)">' + "LONG".repeat(25);
  wire.response = { ...SCHEDULE, title }; await page.setViewportSize({ width: 390, height: 844 }); await fill(page);
  let dialogs = 0; page.on("dialog", async (dialog) => { dialogs++; await dialog.dismiss(); });
  await card(page).getByRole("button", { name: "Create schedule", exact: true }).click();
  await expect(notice(page)).toContainText(title); await expect(notice(page).locator("img")).toHaveCount(0); expect(dialogs).toBe(0);
  await page.evaluate(() => { document.documentElement.style.fontSize = "32px"; });
  await notice(page).scrollIntoViewIfNeeded(); const box = await notice(page).boundingBox(); expect(box).not.toBeNull();
  expect(box!.x + box!.width).toBeLessThanOrEqual(390);
  await notice(page).screenshot({ path: info.outputPath("recurring-outcome-390-text32.png") });
  expect(wire.writes).toHaveLength(1);
});

for (const status of [201, 422]) {
  test(`same-token session renewal fences the old${status} callback without changing actor or organization`, async ({ page }) => {
    const wire = await setup(page); wire.statuses = [status]; await fill(page); wire.postHold = held();
    try {
      await card(page).getByRole("button", { name: "Create schedule", exact: true }).click();
      await expect.poll(() => wire.postHold!.entered).toBe(true);
      const boundary = await page.evaluate(async () => {
        const apiModule = await import("/src/lib/api.ts");
        const token = apiModule.tokenStore.get(), before = apiModule.tokenStore.generation();
        apiModule.tokenStore.set(token);
        return { tokenBefore: token, tokenAfter: apiModule.tokenStore.get(), before, after: apiModule.tokenStore.generation() };
      });
      expect(boundary.tokenAfter).toBe(boundary.tokenBefore); expect(boundary.after).toBeGreaterThan(boundary.before);
      wire.postHold.release(); await expect.poll(() => wire.postCompleted).toBe(1); await page.waitForLoadState("networkidle");
      await expect(notice(page)).toHaveCount(0); await expect(card(page).getByRole("alert")).toHaveCount(0);
      expect(wire.reads).toHaveLength(1); expect(wire.writes).toHaveLength(1);
    } finally { wire.postHold.release(); }
  });
}
