import { test, expect, type Page } from "@playwright/test";

/**
 * AR "Issue" screen: Void / Write off require an explicit ConfirmDialog before
 * firing (WO-34 / R16), over the LIVE app shell with `page.route` mocking —
 * same deterministic, no-backend pattern as `upload-duplicate.spec.ts`.
 *
 * Prior behaviour used a bare `window.prompt(...)` in the button's `onClick`,
 * which fired the mutation the moment the native prompt was dismissed with OK
 * (even with an empty reason). This spec proves the click now opens a styled
 * confirm dialog first, that cancelling issues NO request, and that confirming
 * calls the expected endpoint exactly once.
 *
 * Synthetic fixtures only (master context §10): fictional names/ids/figures.
 */

const ORG = { id: "org-1", name: "Test Workspace", status: "active" };
const USER = {
  id: "user-1",
  email: "owner@test.io",
  name: "Owner",
  role: "owner",
  org_id: "org-1",
};

const ISSUER = {
  id: "issuer-1",
  name: "Fictional Freight OÜ",
  is_default: true,
  legal_name: "Fictional Freight OÜ",
  trade_name: null,
  vat_number: null,
  registration_number: "12345678",
  address_line1: "Testivälja 1",
  address_line2: null,
  city: "Tallinn",
  postal_code: "10111",
  country: "EE",
  email: "billing@fictional-freight.test",
  phone: null,
  iban: null,
  bic: null,
  default_currency: "EUR",
  invoice_prefix: "INV",
  credit_note_prefix: "CN",
  next_number: 42,
  payment_terms_days: 14,
  default_penalty_rate: null,
  payment_instructions: null,
  notes: null,
  is_complete: true,
  missing_fields: [],
  has_logo: false,
};

const MODULES = [
  {
    key: "issuing",
    name: "Invoice issuing",
    description: "Issue customer invoices",
    core: false,
    enabled: true,
    requires_issuer: true,
    ready: true,
  },
];

function invoice(id: string, number: string): Record<string, unknown> {
  return {
    id,
    number,
    lifecycle: "issued",
    kind: "standard",
    doc_type: "invoice",
    corrected_invoice_id: null,
    credited_total: "0.00",
    issuer_id: ISSUER.id,
    partner_id: null,
    issue_date: "2026-07-01",
    supply_date: null,
    due_date: "2026-07-15",
    currency: "EUR",
    buyer_name: "Fictional Buyer OÜ",
    buyer_vat_number: null,
    vat_scheme: "standard",
    note: null,
    po_reference: null,
    tax_exemption_reason: null,
    subtotal: "100.00",
    tax_total: "21.00",
    total: "121.00",
    buyer_email: null,
    amount_paid: "0.00",
    paid_date: null,
    status: "issued",
    outstanding: "121.00",
    penalty_rate: null,
    penalty_accrued: "0.00",
    days_overdue: 0,
    reminder_count: 0,
    last_reminder_at: null,
    sent_at: null,
    viewed_at: null,
    voided_at: null,
    void_reason: null,
    disputed_at: null,
    dispute_reason: null,
    written_off_at: null,
    writeoff_reason: null,
  };
}

const VOID_INVOICE = invoice("inv-void-1", "INV-2026-0042");
const WRITEOFF_INVOICE = invoice("inv-wo-1", "INV-2026-0043");

async function mockApi(page: Page): Promise<{ calls: string[] }> {
  await page.addInitScript(() => localStorage.setItem("invoiceiq_token", "e2e-token"));
  const calls: string[] = [];

  const json = (body: unknown, status = 200) => ({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });

  await page.route("**/api/v1/**", async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    const path = url.pathname.replace(/^.*\/api\/v1/, "");
    const method = req.method();

    if (path === "/auth/me") return route.fulfill(json({ user: USER, organization: ORG }));
    if (path === "/auth/organizations") return route.fulfill(json([{ org_id: ORG.id, name: ORG.name, role: USER.role, current: true }]));
    if (path === "/modules") return route.fulfill(json(MODULES));
    if (path === "/issuer" && method === "GET") return route.fulfill(json(ISSUER));
    if (path === "/issuer/registry") return route.fulfill(json([ISSUER]));
    if (path === "/partners") return route.fulfill(json([]));
    if (path === "/issued/recurring") return route.fulfill(json([]));
    if (path === "/issued" && method === "GET") {
      return route.fulfill(json({ items: [VOID_INVOICE, WRITEOFF_INVOICE], total: 2 }));
    }
    if (path === `/issued/${VOID_INVOICE.id}/void` && method === "POST") {
      calls.push("void");
      return route.fulfill(json({ ...VOID_INVOICE, voided_at: "2026-07-27T12:00:00Z" }));
    }
    if (path === `/issued/${WRITEOFF_INVOICE.id}/write-off` && method === "POST") {
      calls.push("write-off");
      return route.fulfill(
        json({ ...WRITEOFF_INVOICE, lifecycle: "written_off", written_off_at: "2026-07-27T12:00:00Z" }),
      );
    }

    return route.fulfill(json([]));
  });

  return { calls };
}

test("voiding an invoice requires an explicit confirm", async ({ page }) => {
  const { calls } = await mockApi(page);
  await page.goto("/issue");

  await expect(page.getByRole("heading", { level: 1, name: "Issue invoices" })).toBeVisible();
  const row = page.locator("tr", { hasText: VOID_INVOICE.number as string });

  // Clicking "Void" opens the confirm dialog — it must NOT fire the request yet.
  await row.getByRole("button", { name: "Void" }).click();
  const dialog = page.getByRole("dialog", { name: /Void invoice/ });
  await expect(dialog).toBeVisible();
  expect(calls).not.toContain("void");

  // Cancelling leaves the invoice unvoided and issues no request.
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await expect(dialog).toBeHidden();
  expect(calls).not.toContain("void");

  // Re-open and confirm — exactly one call to /void, dialog closes.
  await row.getByRole("button", { name: "Void" }).click();
  await expect(page.getByRole("dialog", { name: /Void invoice/ })).toBeVisible();
  await page.getByRole("dialog", { name: /Void invoice/ }).getByRole("button", { name: "Void invoice" }).click();
  await expect(page.getByRole("dialog", { name: /Void invoice/ })).toBeHidden();
  expect(calls.filter((c) => c === "void")).toHaveLength(1);
});

test("writing off an invoice requires an explicit confirm", async ({ page }) => {
  const { calls } = await mockApi(page);
  await page.goto("/issue");

  await expect(page.getByRole("heading", { level: 1, name: "Issue invoices" })).toBeVisible();
  const row = page.locator("tr", { hasText: WRITEOFF_INVOICE.number as string });

  // Clicking "Write off" opens the confirm dialog — it must NOT fire the request yet.
  await row.getByRole("button", { name: "Write off" }).click();
  const dialog = page.getByRole("dialog", { name: /Write off invoice/ });
  await expect(dialog).toBeVisible();
  expect(calls).not.toContain("write-off");

  // Cancelling leaves the invoice as-is and issues no request.
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await expect(dialog).toBeHidden();
  expect(calls).not.toContain("write-off");

  // Re-open and confirm — exactly one call to /write-off, dialog closes.
  await row.getByRole("button", { name: "Write off" }).click();
  await expect(page.getByRole("dialog", { name: /Write off invoice/ })).toBeVisible();
  await page
    .getByRole("dialog", { name: /Write off invoice/ })
    .getByRole("button", { name: "Write off as bad debt" })
    .click();
  await expect(page.getByRole("dialog", { name: /Write off invoice/ })).toBeHidden();
  expect(calls.filter((c) => c === "write-off")).toHaveLength(1);
});

// R014 browser seams prove scope, pending and recovery; backend tests prove contents.
test("ZIP overflow recovers by retrying the exact selected scope", async ({ page }) => {
  await mockApi(page);
  const scopes: string[] = [];
  let release!: () => void;
  const held = new Promise<void>((resolve) => { release = resolve; });
  await page.route("**/api/v1/issued/export.zip?*", async (route) => {
    scopes.push(new URL(route.request().url()).search);
    if (scopes.length === 1) {
      await held;
      return route.fulfill({ status: 422, contentType: "application/json", body: JSON.stringify({ detail: "More than 500 numbered invoices match." }) });
    }
    return route.fulfill({ status: 200, contentType: "application/zip", body: Buffer.from("synthetic browser download seam") });
  });
  await page.goto("/issue");
  await page.getByLabel("ZIP period from").fill("2026-01-01");
  await page.getByLabel("ZIP period to").fill("2026-12-31");
  await page.getByLabel("ZIP status").selectOption("paid");
  await page.getByRole("button", { name: "Download PDFs (ZIP)" }).click();
  await expect(page.getByRole("button", { name: "Preparing…" })).toBeDisabled();
  await expect(page.getByLabel("ZIP period from")).toBeDisabled();
  release();
  await expect(page.getByRole("alert").filter({ hasText: "then retry the ZIP download" })).toContainText("500");
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download PDFs (ZIP)" }).click();
  await download;
  expect(scopes).toEqual(["?from=2026-01-01&to=2026-12-31&status=paid", "?from=2026-01-01&to=2026-12-31&status=paid"]);
  await expect(page.getByText("Archive prepared; check your browser downloads.")).toBeVisible();
});

test("ZIP refuses an inverted period before making a request", async ({ page }) => {
  await mockApi(page);
  let calls = 0;
  await page.route("**/api/v1/issued/export.zip?*", async (route) => { calls++; await route.abort(); });
  await page.goto("/issue");
  await page.getByLabel("ZIP period from").fill("2026-12-31");
  await page.getByLabel("ZIP period to").fill("2026-01-01");
  await expect(page.getByRole("button", { name: "Download PDFs (ZIP)" })).toBeDisabled();
  await expect(page.getByRole("alert").filter({ hasText: "must not follow" })).toBeVisible();
  expect(calls).toBe(0);
});

const SUPPORTING_FILE = { id: "synthetic-attachment-1", filename: "synthetic-support.txt", mime: "text/plain", size: 26, note: null, uploaded_by_email: null, created_at: "2026-10-16T12:00:00Z", deleted_at: null };

test("attachment download keeps pending state and explains a safe retry", async ({ page }) => {
  await mockApi(page);
  await page.route(`**/api/v1/issued/${VOID_INVOICE.id}/attachment-policy`, route => route.fulfill({ json: { max_bytes: 15728640 } }));
  await page.route(`**/api/v1/issued/${VOID_INVOICE.id}/attachments`, route => route.fulfill({ json: [SUPPORTING_FILE] }));
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  let calls = 0;
  await page.route(`**/api/v1/issued/${VOID_INVOICE.id}/attachments/${SUPPORTING_FILE.id}/download`, async route => {
    calls++;
    if (calls === 1) { await held; return route.fulfill({ status: 503, json: { detail: "Synthetic storage unavailable" } }); }
    return route.fulfill({ contentType: "text/plain", body: "Synthetic supporting text" });
  });
  await page.goto("/issue");
  const row = page.locator("tr", { hasText: VOID_INVOICE.number as string });
  await row.getByRole("button", { name: /Files/ }).click();
  await row.getByRole("button", { name: SUPPORTING_FILE.filename, exact: true }).click();
  await expect(row.getByRole("button", { name: `Downloading ${SUPPORTING_FILE.filename}…` })).toBeDisabled();
  release();
  await expect(row.getByRole("alert")).toContainText("Synthetic storage unavailable");
  const download = page.waitForEvent("download");
  await row.getByRole("button", { name: SUPPORTING_FILE.filename, exact: true }).click();
  await download;
  expect(calls).toBe(2);
});

test("attachment upload blocks a second selection and reconciles an uncertain response", async ({ page }) => {
  await mockApi(page);
  let committed = false;
  let posts = 0;
  let lookups = 0;
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  await page.route(`**/api/v1/issued/${VOID_INVOICE.id}/attachment-policy`, route => route.fulfill({ json: { max_bytes: 15728640 } }));
  await page.route(`**/api/v1/issued/${VOID_INVOICE.id}/attachments`, async route => {
    if (route.request().method() === "GET") return route.fulfill({ json: committed ? [SUPPORTING_FILE] : [] });
    posts++;
    expect(route.request().headers()["idempotency-key"]).toBeTruthy();
    expect(route.request().headers()["x-expected-org-id"]).toBe(ORG.id);
    await held;
    committed = true;
    return route.fulfill({ status: 503, json: { detail: "Synthetic lost response after commit" } });
  });
  await page.route(`**/api/v1/issued/${VOID_INVOICE.id}/attachment-commands/*`, route => { lookups++; return route.fulfill({ json: SUPPORTING_FILE }); });
  await page.goto("/issue");
  const row = page.locator("tr", { hasText: VOID_INVOICE.number as string });
  await row.getByRole("button", { name: /Files/ }).click();
  const chooser = row.getByLabel("Add supporting file");
  await expect(chooser).toBeEnabled();
  await chooser.setInputFiles({ name: SUPPORTING_FILE.filename, mimeType: "text/plain", buffer: Buffer.from("Synthetic supporting text") });
  await expect.poll(() => posts).toBe(1);
  await expect(chooser).toBeDisabled();
  release();
  await expect(row.getByRole("button", { name: SUPPORTING_FILE.filename, exact: true })).toBeVisible();
  expect(posts).toBe(1); expect(lookups).toBe(1);
});

test("credit command resolves a lost response without another legal write", async ({ page }) => {
  await mockApi(page);
  let committed = false;
  let posts = 0;
  let checks = 0;
  const cn = { ...VOID_INVOICE, id: "synthetic-cn-1", number: "SYNTH-CN-1", doc_type: "credit_note", corrected_invoice_id: VOID_INVOICE.id };
  await page.route(`**/api/v1/issued/${VOID_INVOICE.id}/credit-note`, async route => {
    posts++;
    expect(route.request().headers()["idempotency-key"]).toBeTruthy();
    expect(route.request().postDataJSON()).toEqual({ expected_credited_total: "0.00" });
    committed = true;
    return route.fulfill({ status: 503, json: { detail: "Synthetic lost response after commit" } });
  });
  await page.route(`**/api/v1/issued/${VOID_INVOICE.id}/credit-commands/*`, route => { checks++; return route.fulfill({ json: cn }); });
  await page.route(/\/api\/v1\/issued(?:\?.*)?$/, route => route.fulfill({ json: { items: committed ? [{ ...VOID_INVOICE, credited_total: "121.00" }, WRITEOFF_INVOICE, cn] : [VOID_INVOICE, WRITEOFF_INVOICE], total: committed ? 3 : 2 } }));
  page.on("dialog", dialog => dialog.accept());
  await page.goto("/issue");
  const row = page.locator("tr", { hasText: VOID_INVOICE.number as string });
  await row.getByRole("button", { name: "Credit", exact: true }).click();
  await expect(row.getByText("Fully credited", { exact: true })).toBeVisible();
  expect(posts).toBe(1); expect(checks).toBe(1);
});
