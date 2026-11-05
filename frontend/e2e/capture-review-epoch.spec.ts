import { test, expect, type Page } from "@playwright/test";

type Phase = "parsed" | "queued" | "failed";
function gate() {
  let release!: () => void, reached!: () => void, done!: () => void;
  return {
    released: new Promise<void>((resolve) => { release = resolve; }),
    reached: new Promise<void>((resolve) => { reached = resolve; }),
    done: new Promise<void>((resolve) => { done = resolve; }),
    release: () => release(), enter: () => reached(), finish: () => done(),
  };
}
type Gate = ReturnType<typeof gate>;

async function setup(page: Page, phase: Phase = "parsed") {
  const state = {
    phase, attempt: 0, revision: 0, retryStatus: 202, runReads: 0, fieldReads: 0, snapshots: 0,
    retries: 0, authReads: 0, notFound: false,
    actor: "epoch-owner", org: "epoch-org", credential: "synthetic-epoch-token",
    fieldGate: null as Gate | null, snapshotGate: null as Gate | null,
    reviews: [] as Record<string, unknown>[], invoices: [] as Record<string, unknown>[],
  };
  const number = () => `ATTEMPT-${state.attempt}`;
  const fingerprint = () => ((state.attempt + state.revision) % 16).toString(16).repeat(64);
  const draft = () => ({
    vendor_name: "Synthetic Version Supplier", invoice_number: number(), issue_date: "2026-11-05", due_date: null,
    currency: "EUR", source_filename: "synthetic-version.csv", po_reference: null,
    supplier_registration_number: null, supplier_vat_id: null, supplier_iban: null,
    line_items: [{ description: "Synthetic service", quantity: "1", unit_price: "10.00", amount: "10.00",
      tax_rate: "0", category: "uncategorized", captured_tax_amount: null, captured_gross_amount: null }],
  });
  const fields = () => [
    ...Object.entries(draft()).filter(([key]) => !["line_items", "source_filename"].includes(key))
      .map(([field, value]) => ({ field, line_index: null, value, original_value: value, normalized_value: value,
        reviewed_value: null, status: value == null ? "missing" : "extracted", provider: "csv", confidence: null, low_confidence: false })),
    ...Object.entries(draft().line_items[0]).map(([field, value]) => ({ field, line_index: 0, value,
      original_value: value, normalized_value: value, reviewed_value: null,
      status: value == null ? "missing" : "extracted", provider: "csv", confidence: null, low_confidence: false })),
  ];
  const json = (value: unknown, status = 200) => ({ status, contentType: "application/json", body: JSON.stringify(value) });
  await page.addInitScript(() => {
    if (!localStorage.getItem("invoiceiq_token")) localStorage.setItem("invoiceiq_token", "synthetic-epoch-token");
  });
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request(), path = new URL(request.url()).pathname.replace("/api/v1", "");
    if (path === "/auth/me") {
      state.authReads++;
      return route.fulfill(json({ user: { id: state.actor, org_id: state.org, name: "Synthetic Owner", role: "owner", email: "owner@example.com" },
        organization: { id: state.org, name: "Synthetic Version Workspace", status: "active" } }));
    }
    if (path === "/auth/organizations") return route.fulfill(json([{ org_id: state.org, name: "Synthetic Version Workspace", role: "owner", current: true }]));
    if (path === "/modules") return route.fulfill(json([]));
    if (path === "/invoices/upload/epoch-run") {
      state.runReads++;
      return route.fulfill(state.notFound ? json({ detail: "Capture not found" }, 404) : json({ status: state.phase,
        extraction_run_id: "epoch-run", method: "csv", draft: state.phase === "parsed" ? { draft: draft(), warnings: [], fields: fields() } : null,
        error: state.phase === "failed" ? "Synthetic transient reading failure" : null }));
    }
    if (path === "/invoices/upload/epoch-run/retry") {
      state.retries++;
      if (state.retryStatus !== 202) return route.fulfill(json({ detail: "Synthetic retry unavailable" }, state.retryStatus));
      state.phase = "queued"; state.attempt++;
      return route.fulfill(json({ extraction_run_id: "epoch-run", status: "queued" }, 202));
    }
    if (path === "/invoices/captures/epoch-run/fields") {
      state.fieldReads++;
      const response = state.phase === "parsed" ? fields() : [];
      const held = state.fieldGate; state.fieldGate = null;
      held?.enter();
      try { if (held) await held.released; await route.fulfill(json(response)); }
      catch { /* A retired epoch/identity aborts its owned read. */ }
      finally { held?.finish(); }
      return;
    }
    if (path === "/invoices/captures/epoch-run/region-reads") {
      state.snapshots++;
      const response = { items: [], source_sha256: "b".repeat(64), attempt_seq: state.attempt,
        current_draft: state.phase === "parsed" ? draft() : null, review_fingerprint: fingerprint(),
        reviewed_fields: [], eligible: false, reason: "This CSV has no selectable page image." };
      const held = state.snapshotGate; state.snapshotGate = null;
      held?.enter();
      try { if (held) await held.released; await route.fulfill(json(response)); }
      catch { /* A retired epoch/identity aborts its owned read. */ }
      finally { held?.finish(); }
      return;
    }
    if (path === "/invoices/captures/epoch-run/review") {
      const body = request.postDataJSON(); state.reviews.push(body);
      if (state.phase !== "parsed") return route.fulfill(json({ code: "capture_not_ready", detail: "Wait for extraction to finish before reviewing." }, 409));
      if (body.expected_review_fingerprint !== fingerprint()) return route.fulfill(json({ code: "capture_review_changed", detail: "Saved review changed" }, 409));
      return route.fulfill({ ...json(fields()), headers: { "x-capture-review-fingerprint": fingerprint() } });
    }
    if (path === "/invoices" && request.method() === "POST") {
      const body = request.postDataJSON(); state.invoices.push(body);
      return route.fulfill(json({ code: "capture_review_changed", detail: "Synthetic confirmation stopped for inspection" }, 409));
    }
    if (path === "/invoices/captures/epoch-run/source") return route.fulfill(json({ detail: "No inline preview" }, 404));
    if (path === "/invoices/captures/epoch-run/pages") return route.fulfill(json({ status: "unsupported", pages: [], total_pages: null, truncated: false }));
    if (path === "/invoices/duplicate-candidates") return route.fulfill(json({ exact: [], cross_supplier: [], scored: [] }));
    if (path.startsWith("/vendors/resolve")) return route.fulfill(json({ outcome: "create", reason: "Synthetic supplier", candidates: [] }));
    return route.fulfill(json([]));
  });
  await page.goto("/captures/epoch-run");
  if (phase === "parsed") await expect(page.getByLabel("Invoice number", { exact: true })).toHaveValue("ATTEMPT-0");
  else await expect(page.getByRole("button", { name: "Retry extraction", exact: true })).toBeVisible();
  return state;
}

async function observeExternalFailure(page: Page, state: Awaited<ReturnType<typeof setup>>) {
  state.phase = "failed"; state.attempt++;
  await page.getByLabel("Vendor", { exact: true }).fill("Synthetic local edit before conflict");
  await page.getByRole("button", { name: "Save corrections", exact: true }).click();
  await expect(page.getByRole("button", { name: "Reload saved review", exact: true })).toBeVisible();
}

test("supported stale-tab failure/retry flow waits for fresh snapshot and shows the completed attempt", async ({ page }) => {
  const state = await setup(page);
  await observeExternalFailure(page, state);
  await page.getByRole("button", { name: "Reload saved review", exact: true }).click();
  await page.getByRole("button", { name: "Retry extraction", exact: true }).click();
  await expect(page.getByText("Parsing runs on the server", { exact: false })).toBeVisible();
  const snapshot = gate(); state.snapshotGate = snapshot; state.phase = "parsed";
  await snapshot.reached;
  await expect(page.getByLabel("Invoice number", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Confirm & create invoice", exact: true })).toHaveCount(0);
  snapshot.release();
  await expect(page.getByLabel("Invoice number", { exact: true })).toHaveValue("ATTEMPT-2");
  expect(state.retries).toBe(1);
  expect(state.invoices).toHaveLength(0);
});

test("delayed reads from a retired review cannot overwrite the fresh completed attempt", async ({ page }) => {
  const state = await setup(page);
  await observeExternalFailure(page, state);
  const snapshot = gate(), fields = gate(); state.snapshotGate = snapshot; state.fieldGate = fields;
  await page.getByRole("button", { name: "Reload saved review", exact: true }).click();
  await Promise.all([snapshot.reached, fields.reached]);
  await page.getByRole("button", { name: "Retry extraction", exact: true }).click();
  await expect(page.getByText("Parsing runs on the server", { exact: false })).toBeVisible();
  state.phase = "parsed";
  await expect(page.getByLabel("Invoice number", { exact: true })).toHaveValue("ATTEMPT-2");
  snapshot.release(); fields.release(); await Promise.all([snapshot.done, fields.done]);
  await expect(page.getByLabel("Invoice number", { exact: true })).toHaveValue("ATTEMPT-2");
  await expect(page.getByRole("button", { name: "Confirm & create invoice", exact: true })).toBeEnabled();
  expect(state.retries).toBe(1); expect(state.invoices).toHaveLength(0);
});

test("both fresh fields and the saved snapshot are required before editing", async ({ page }) => {
  const state = await setup(page, "failed");
  await page.getByRole("button", { name: "Retry extraction", exact: true }).click();
  await expect(page.getByText("Parsing runs on the server", { exact: false })).toBeVisible();
  const snapshot = gate(), fields = gate(); state.snapshotGate = snapshot; state.fieldGate = fields; state.phase = "parsed";
  await Promise.all([snapshot.reached, fields.reached]);
  snapshot.release(); await snapshot.done;
  await expect(page.getByLabel("Invoice number", { exact: true })).toHaveCount(0);
  fields.release();
  await expect(page.getByLabel("Invoice number", { exact: true })).toHaveValue("ATTEMPT-1");
});

test("repeated intentional retries read the newest completion without another automatic POST", async ({ page }) => {
  const state = await setup(page, "failed");
  await page.getByRole("button", { name: "Retry extraction", exact: true }).click();
  await expect(page.getByText("Parsing runs on the server", { exact: false })).toBeVisible();
  state.phase = "failed";
  await page.getByRole("button", { name: "Retry extraction", exact: true }).click();
  await expect(page.getByText("Parsing runs on the server", { exact: false })).toBeVisible();
  state.phase = "parsed";
  await expect(page.getByLabel("Invoice number", { exact: true })).toHaveValue("ATTEMPT-2");
  expect(state.retries).toBe(2); expect(state.invoices).toHaveLength(0);
});

test("a failed retry keeps its state and requires a new explicit retry", async ({ page }) => {
  const state = await setup(page, "failed"); state.retryStatus = 503;
  await page.getByRole("button", { name: "Retry extraction", exact: true }).click();
  await expect(page.getByRole("alert").filter({ hasText: "Synthetic retry unavailable" })).toBeVisible();
  expect(state.retries).toBe(1); expect(state.attempt).toBe(0); expect(state.snapshots).toBe(0);
  state.retryStatus = 202;
  await page.getByRole("button", { name: "Retry extraction", exact: true }).click();
  await expect(page.getByText("Parsing runs on the server", { exact: false })).toBeVisible(); state.phase = "parsed";
  await expect(page.getByLabel("Invoice number", { exact: true })).toHaveValue("ATTEMPT-1");
  expect(state.retries).toBe(2);
});

for (const boundary of ["actor", "organization", "session"] as const) {
  test(`late review reads cannot cross an ${boundary} change`, async ({ page }) => {
    const state = await setup(page);
    await observeExternalFailure(page, state);
    const snapshot = gate(), fields = gate(); state.snapshotGate = snapshot; state.fieldGate = fields;
    await page.getByRole("button", { name: "Reload saved review", exact: true }).click();
    await Promise.all([snapshot.reached, fields.reached]);
    const authReads = state.authReads;
    state.phase = "parsed"; state.attempt = 2;
    if (boundary === "actor") state.actor = "replacement-owner";
    if (boundary === "organization") { state.org = "replacement-org"; state.notFound = true; }
    await page.evaluate(() => {
      const previous = localStorage.getItem("invoiceiq_token");
      localStorage.setItem("invoiceiq_token", "synthetic-replacement-token");
      window.dispatchEvent(new StorageEvent("storage", { key: "invoiceiq_token", oldValue: previous, newValue: "synthetic-replacement-token" }));
    });
    await expect.poll(() => state.authReads).toBeGreaterThan(authReads);
    if (boundary === "organization") await expect(page.getByText("Couldn’t load this capture", { exact: true })).toBeVisible();
    else await expect(page.getByLabel("Invoice number", { exact: true })).toHaveValue("ATTEMPT-2");
    snapshot.release(); fields.release(); await Promise.all([snapshot.done, fields.done]);
    if (boundary === "organization") await expect(page.getByLabel("Invoice number", { exact: true })).toHaveCount(0);
    else await expect(page.getByLabel("Invoice number", { exact: true })).toHaveValue("ATTEMPT-2");
    expect(state.retries).toBe(0); expect(state.invoices).toHaveLength(0);
  });
}

test("ordinary background reads preserve unsaved edits and their original write precondition", async ({ page }) => {
  const state = await setup(page);
  await page.getByLabel("Invoice number", { exact: true }).fill("LOCAL-UNSAVED");
  const snapshots = state.snapshots; state.revision++;
  await page.clock.install(); await page.clock.fastForward(31000);
  await page.evaluate(() => { window.dispatchEvent(new Event("offline")); window.dispatchEvent(new Event("online")); });
  await expect.poll(() => state.snapshots).toBeGreaterThan(snapshots);
  await expect(page.getByLabel("Invoice number", { exact: true })).toHaveValue("LOCAL-UNSAVED");
  await page.getByRole("button", { name: "Save corrections", exact: true }).click();
  await expect(page.getByRole("button", { name: "Reload saved review", exact: true })).toBeVisible();
  expect(state.reviews[0].expected_review_fingerprint).toBe("0".repeat(64));
  await expect(page.getByLabel("Invoice number", { exact: true })).toHaveValue("LOCAL-UNSAVED");
  expect(state.retries).toBe(0); expect(state.invoices).toHaveLength(0);
});
