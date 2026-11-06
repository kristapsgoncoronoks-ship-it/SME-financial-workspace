import { expect, test, type Page } from "@playwright/test";

async function setup(page: Page, options: { role?: "owner" | "auditor"; foreign?: boolean; fail?: boolean } = {}) {
  const wire = { fail: options.fail ?? false, reads: 0, mutations: [] as string[], candidates: [] as string[] };
  await page.addInitScript(() => localStorage.setItem("invoiceiq_token", "synthetic-legacy-token"));
  const json = (data: unknown) => ({ contentType: "application/json", body: JSON.stringify(data) });
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace("/api/v1", "");
    if (request.method() !== "GET") wire.mutations.push(`${request.method()} ${path}`);
    const organization = { id: "org", name: "Synthetic legacy workspace", status: "active" };
    if (path === "/auth/me") return route.fulfill(json({ user: { id: "owner", org_id: "org", name: "Synthetic reviewer", email: "reviewer@example.com", role: options.role ?? "owner" }, organization }));
    if (path === "/auth/organizations") return route.fulfill(json([{ org_id: "org", name: organization.name, current: true }]));
    if (path === "/modules") return route.fulfill(json([{ key: "issuing", name: "Issuing", enabled: true }]));
    if (path === "/reconciliation/statements") return route.fulfill(json([{ id: "statement", filename: "synthetic-legacy.csv", source_format: "csv", method: "csv", line_count: 2, created_at: "2026-11-06", unmatched: 2, matched: 0, ignored: 0 }]));
    if (path === "/reconciliation/statements/statement/lines") {
      wire.reads += 1;
      if (wire.fail) return route.fulfill({ status: 503, ...json({ detail: "Synthetic statement read unavailable" }) });
      return route.fulfill(json([
        { id: "invalid", statement_id: "statement", version: 1, line_date: "2026-11-06", description: "Legacy amount unavailable", amount: null, amount_status: "invalid", currency: options.foreign ? "USD" : "EUR", direction: "credit", balance: null, balance_status: "unprovided", status: "unmatched", matched_kind: null, matched_id: null },
        { id: "valid", statement_id: "statement", version: 1, line_date: "2026-11-06", description: "Valid amount invalid balance", amount: "-25.00", amount_status: "valid", currency: "EUR", direction: "debit", balance: null, balance_status: "invalid", status: "unmatched", matched_kind: null, matched_id: null },
      ]));
    }
    if (path.endsWith("/candidates")) {
      wire.candidates.push(path);
      return route.fulfill({ status: 409, ...json({ code: "reconciliation_target_amount_invalid", detail: "The selected cash record has an unavailable amount. Check that record with your accountant before matching." }) });
    }
    return route.fulfill(json([]));
  });
  await page.goto("/reconciliation");
  await expect(page.getByRole("heading", { name: "Bank reconciliation", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Reconcile", exact: true }).click();
  return wire;
}

test("unavailable legacy amount stays visible beside a valid row without becoming zero", async ({ page }) => {
  const wire = await setup(page);
  const invalid = page.getByRole("row").filter({ hasText: "Legacy amount unavailable" });
  await expect(invalid).toContainText("Amount unavailable");
  await expect(invalid).toContainText("Check your original bank statement with your accountant");
  await expect(invalid).toContainText("EUR");
  await expect(invalid).not.toContainText("0.00");
  await expect(invalid).not.toContainText("NaN");
  await expect(invalid.getByRole("button", { name: "Find match", exact: true })).toBeDisabled();
  await expect(invalid.getByRole("button", { name: "Ignore", exact: true })).toBeEnabled();
  const valid = page.getByRole("row").filter({ hasText: "Valid amount invalid balance" });
  await expect(valid).toContainText("25.00");
  await expect(valid).toContainText("Balance unavailable");
  await expect(valid.getByRole("button", { name: "Find match", exact: true })).toBeEnabled();
  await expect(page.getByText("0/2 reconciled", { exact: true })).toBeVisible();
  expect(wire.mutations).toEqual([]);
  expect(wire.candidates).toEqual([]);
});

test("balance-only issue keeps the candidate read and displays typed recovery guidance", async ({ page }) => {
  const wire = await setup(page);
  const valid = page.getByRole("row").filter({ hasText: "Valid amount invalid balance" });
  await valid.getByRole("button", { name: "Find match", exact: true }).click();
  await expect(valid.getByRole("alert")).toContainText("Check that record with your accountant before matching.");
  await expect(valid).not.toContainText("[object Object]");
  await expect(valid).not.toContainText("No matching receipt or payout found.");
  expect(wire.candidates.every((path) => path === "/reconciliation/lines/valid/candidates")).toBe(true);
  expect(wire.mutations).toEqual([]);
});

test("auditor reads preserve unavailable source currency and both rows without writes", async ({ page }) => {
  const wire = await setup(page, { role: "auditor", foreign: true });
  const invalid = page.getByRole("row").filter({ hasText: "Legacy amount unavailable" });
  await expect(invalid).toContainText("Amount unavailable");
  await expect(invalid).toContainText("USD");
  await expect(invalid).not.toContainText("€");
  await expect(invalid.getByRole("button", { name: "Find match", exact: true })).toBeDisabled();
  await expect(page.getByRole("row").filter({ hasText: "Valid amount invalid balance" })).toBeVisible();
  expect(wire.mutations).toEqual([]);
});

test("failed statement read stays explicit and recovers with GET only", async ({ page }) => {
  const wire = await setup(page, { fail: true });
  await expect(page.getByRole("alert").filter({ hasText: "Couldn’t load the statement lines" })).toContainText("Couldn’t load the statement lines");
  await expect(page.getByText("No lines.", { exact: true })).toHaveCount(0);
  const before = wire.reads;
  wire.fail = false;
  await page.getByRole("button", { name: "Try again", exact: true }).click();
  await expect(page.getByRole("row").filter({ hasText: "Legacy amount unavailable" })).toContainText("Amount unavailable");
  expect(wire.reads).toBe(before + 1);
  expect(wire.mutations).toEqual([]);
});

test("narrow enlarged text keeps unavailable facts and recovery text in the row", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.addInitScript(() => document.addEventListener("DOMContentLoaded", () => { document.documentElement.style.fontSize = "32px"; }));
  const wire = await setup(page);
  const invalid = page.getByRole("row").filter({ hasText: "Legacy amount unavailable" });
  await expect(invalid).toContainText("Amount unavailable");
  await expect(invalid).toContainText("Check your original bank statement with your accountant");
  await expect(invalid.getByRole("button", { name: "Find match", exact: true })).toBeDisabled();
  await page.screenshot({ path: test.info().outputPath("legacy-390-root32.png"), fullPage: true });
  const bounds = await page.evaluate(() => ({ viewport: window.innerWidth, document: document.documentElement.scrollWidth }));
  await test.info().attach("page-bounds", { body: JSON.stringify(bounds), contentType: "application/json" });
  expect(bounds.document).toBeLessThanOrEqual(bounds.viewport);
  const region = page.getByRole("region", { name: "Statement lines", exact: true });
  await region.focus();
  await expect(region).toBeFocused();
  const initialScroll = await region.evaluate((element) => element.scrollLeft);
  await page.keyboard.press("ArrowRight");
  await expect.poll(() => region.evaluate((element) => element.scrollLeft)).toBeGreaterThan(initialScroll);
  await page.screenshot({ path: test.info().outputPath("legacy-390-root32-scrolled.png"), fullPage: true });
  expect(wire.mutations).toEqual([]);
});
