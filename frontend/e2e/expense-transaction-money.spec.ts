import { expect, test, type Page } from "@playwright/test";

// Mocked-data browser contracts; real restricted-PG reads/refusals are separate.
const ORG = { id: "expense-money-org", name: "Synthetic money workspace", status: "active" };
const USER = { id: "expense-money-user", org_id: ORG.id, name: "Synthetic owner", email: "money@example.test", role: "owner" };
const txn = (id: string, amount: unknown, amount_status?: string) => ({ id, version: 1, import_batch_id: "source-batch", currency_source: "statement", txn_date: "2026-11-06", description: id === "unavailable" ? "Historical unavailable transaction" : "Finite statement transaction", merchant: null, amount, ...(amount_status ? { amount_status } : {}), currency: "EUR", direction: "debit", source: "bank_statement", status: "available" });
const ITEM = { id: "item", expense_type: "standard", spend_date: "2026-11-06", category: "travel", description: "Synthetic expense item", merchant: null, amount: "12.34", currency: "EUR", original_amount: "12.34", fx_rate: "1.00", fx_source: "eur", vat_amount: "0.00", reclaimable_tax: false, payment_method: "company_card", receipt_url: null, receipt_sha256: null, receipt_size: null, receipt_name: null, receipt_content_type: null, has_receipt: false, business_purpose: "Synthetic trip", bank_reference: null, verified: false };
const REPORT = { id: "report", employee_id: USER.id, employee_name: USER.name, title: "Synthetic money report", status: "draft", currency: "EUR", total: "12.34", vat_total: "0.00", total_eur: "12.34", version: 1, submitted_at: null, created_at: "2026-11-06T00:00:00Z", note: null, decided_at: null, decided_by: null, decision_note: null, items: [ITEM], policy_violations: [], approval_steps: [] };

async function setup(page: Page, options: { initialValid?: boolean; detail?: boolean; readOnly?: boolean; malformed?: boolean } = {}) {
  const wire = { rows: [txn("unavailable", options.initialValid ? "12.34" : options.malformed ? "NaN" : null, options.initialValid ? "valid" : options.malformed ? "valid" : "invalid"), txn("finite", "12.34")], reports: [] as Record<string, unknown>[], matches: [] as unknown[], removals: [] as string[], imports: 0, errors: [] as string[], created: false };
  page.on("pageerror", (error) => wire.errors.push(error.message));
  await page.addInitScript(() => localStorage.setItem("invoiceiq_token", "synthetic-expense-money-token"));
  await page.route("**/api/v1/**", async route => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname.replace("/api/v1", "");
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (path === "/auth/me") return json({ user: { ...USER, role: options.readOnly ? "auditor" : "owner" }, organization: ORG });
    if (path === "/auth/organizations") return json([{ org_id: ORG.id, name: ORG.name, current: true }]);
    if (path === "/modules") return json([{ key: "expenses", name: "Expenses", enabled: true, ready: true, requires_issuer: false }]);
    if (path === "/expenses/transactions" && request.method() === "GET") return json(wire.rows);
    if (path.startsWith("/expenses/transactions/") && request.method() === "DELETE") {
      wire.removals.push(request.url()); wire.rows = wire.rows.filter(row => row.id !== "unavailable");
      return route.fulfill({ status: 204, body: "" });
    }
    if (path === "/expenses/import/bank-statement" && request.method() === "POST") {
      wire.imports += 1; wire.rows = [txn("unavailable", null, "invalid"), txn("finite", "12.34")];
      return json({ imported: 0, method: "csv", batch_id: "source-batch", replayed: true, warnings: [], available: 2, assigned: 0, binned: 0, unavailable: 0 });
    }
    if (path === "/expenses" && request.method() === "POST") {
      wire.reports.push(request.postDataJSON()); wire.created = true;
      expect(request.headers()["idempotency-key"]).toBeTruthy();
      expect(request.headers()["x-expected-user-id"]).toBe(USER.id);
      expect(request.headers()["x-expected-org-id"]).toBe(ORG.id);
      return json({ ...REPORT, id: "saved-report", title: request.postDataJSON().title, items: [] }, 201);
    }
    if (path === "/expenses" && request.method() === "GET") return json({ items: [], total: 0, page: Number(url.searchParams.get("page") ?? 1), page_size: 100 });
    if (path === "/expenses/summary") return json({ my_reimbursable: "0.00", reclaimable_vat: "0.00", my_submitted: 0, pending_approvals: 0 });
    if (path === "/expenses/report") return json(REPORT);
    if (path === "/expenses/report/items/item/match-candidates") return json(wire.rows);
    if (path === "/expenses/report/items/item/match" && request.method() === "POST") { wire.matches.push(request.postDataJSON()); return json(REPORT); }
    return json([]);
  });
  await page.goto(options.detail ? "/expenses/report" : "/expenses");
  return wire;
}

const invalidRow = (page: Page) => page.getByRole("row").filter({ hasText: "Historical unavailable transaction" });

test("inbox retains unavailable money without zero, selection or financial writes", async ({ page }) => {
  const wire = await setup(page);
  const row = invalidRow(page);
  await expect(row).toContainText("Amount unavailable");
  await expect(row).toContainText("Review the original statement");
  await expect(row).not.toContainText("0.00");
  await expect(row).not.toContainText("NaN");
  await expect(row.getByRole("checkbox")).toBeDisabled();
  await expect(row.getByRole("button", { name: "remove", exact: true })).toBeEnabled();
  const finite = page.getByRole("row").filter({ hasText: "Finite statement transaction" });
  await finite.getByRole("checkbox").check();
  await expect(page.getByRole("button", { name: /Create report from 1/ })).toContainText("12.34");
  await page.getByLabel("Report title", { exact: true }).fill("Synthetic finite draft");
  await page.getByRole("button", { name: /Create report from 1/ }).click();
  await expect.poll(() => wire.reports.length).toBe(1);
  expect(wire.reports[0]).toEqual({ title: "Synthetic finite draft", currency: "EUR", transaction_ids: ["finite"], transaction_versions: { finite: 1 } });
  expect(wire.matches).toEqual([]); expect(wire.errors).toEqual([]);
});

test("an unavailable selected row after refresh blocks sums and can be deselected", async ({ page }) => {
  const wire = await setup(page, { initialValid: true });
  await invalidRow(page).getByRole("checkbox").check();
  await page.getByLabel("Statement account reference", { exact: true }).fill("Synthetic account");
  await page.getByLabel("Statement file", { exact: true }).setInputFiles({ name: "synthetic.csv", mimeType: "text/csv", buffer: Buffer.from("date,amount\n2026-11-06,-12.34\n") });
  await expect.poll(() => wire.imports).toBe(1);
  await expect(invalidRow(page)).toContainText("Amount unavailable");
  await expect(page.getByRole("button", { name: /Create report from 1/ })).toBeDisabled();
  await invalidRow(page).getByRole("checkbox").uncheck();
  await page.getByRole("row").filter({ hasText: "Finite statement transaction" }).getByRole("checkbox").check();
  await expect(page.getByRole("button", { name: /Create report from 1/ })).toBeEnabled();
  expect(wire.reports).toEqual([]); expect(wire.errors).toEqual([]);
});

test("authorized bin action remains available for a bad amount", async ({ page }) => {
  const wire = await setup(page);
  await invalidRow(page).getByRole("button", { name: "remove", exact: true }).click();
  await page.getByRole("button", { name: "Remove", exact: true }).click();
  await expect.poll(() => wire.removals.length).toBe(1);
  expect(wire.removals[0]).toContain("/expenses/transactions/unavailable?version=1");
  await expect(invalidRow(page)).toHaveCount(0);
  expect(wire.reports).toEqual([]); expect(wire.matches).toEqual([]); expect(wire.errors).toEqual([]);
});

test("detail cannot match unavailable candidate and retains legacy finite candidate", async ({ page }) => {
  const wire = await setup(page, { detail: true });
  await page.getByRole("button", { name: "match to bank", exact: true }).click();
  const bad = page.getByRole("button").filter({ hasText: "Historical unavailable transaction" });
  await expect(bad).toContainText("Amount unavailable");
  await expect(bad).toBeDisabled();
  await expect(bad).not.toContainText("0.00");
  await page.getByRole("button").filter({ hasText: "Finite statement transaction" }).click();
  await expect.poll(() => wire.matches.length).toBe(1);
  expect(wire.matches[0]).toEqual({ transaction_id: "finite" });
  expect(wire.errors).toEqual([]);
});

test("malformed amount cannot become zero even with a claimed valid status", async ({ page }) => {
  const wire = await setup(page, { malformed: true, readOnly: true });
  await expect(invalidRow(page)).toContainText("Amount unavailable");
  await expect(invalidRow(page)).not.toContainText("NaN");
  await expect(invalidRow(page).getByRole("checkbox")).toBeDisabled();
  await expect(invalidRow(page).getByRole("button", { name: "remove", exact: true })).toBeDisabled();
  expect(wire.reports).toEqual([]); expect(wire.removals).toEqual([]); expect(wire.errors).toEqual([]);
});

test("390px enlarged text contains unavailable facts and keeps the table reachable", async ({ page }, info) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.addInitScript(() => document.addEventListener("DOMContentLoaded", () => { document.documentElement.style.fontSize = "32px"; }));
  const wire = await setup(page);
  await expect(invalidRow(page)).toContainText("Amount unavailable");
  const bounds = await page.evaluate(() => ({ document: document.documentElement.scrollWidth, viewport: innerWidth }));
  await info.attach("page-bounds", { body: JSON.stringify(bounds), contentType: "application/json" });
  expect(bounds.document).toBeLessThanOrEqual(bounds.viewport);
  const region = page.getByRole("region", { name: "Available expense transactions", exact: true });
  await region.focus(); await expect(region).toBeFocused();
  await page.keyboard.press("ArrowRight");
  await expect.poll(() => region.evaluate(element => element.scrollLeft)).toBeGreaterThan(0);
  await page.screenshot({ path: info.outputPath("expense-money-390-root32.png"), fullPage: true });
  await region.evaluate(element => { element.scrollLeft = element.scrollWidth; });
  await expect(invalidRow(page).getByText("Amount unavailable", { exact: true })).toBeInViewport();
  await page.screenshot({ path: info.outputPath("expense-money-390-root32-scrolled.png") });
  expect(wire.reports).toEqual([]); expect(wire.errors).toEqual([]);
});
