import { test, expect, type Page } from "@playwright/test";

const WARNING = "The source image was reduced for reading. Verify the suggested fields against the original receipt.";
const PNG = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==", "base64");
const SUGGESTION = { merchant: "Synthetic receipt shop", amount: "123.45", vat_amount: "23.45", spend_date: "2026-11-04", method: "ocr" };
type Reply = { body: unknown; status?: number; wait?: Promise<void> };
async function setup(page: Page, replies: Reply[]) {
  const scans: string[] = [], itemWrites: unknown[] = [];
  await page.addInitScript(() => localStorage.setItem("invoiceiq_token", "synthetic-receipt-warning-token"));
  await page.route("**/api/v1/**", async route => {
    const request = route.request(), path = new URL(request.url()).pathname.replace("/api/v1", "");
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    const org = { id: "receipt-warning-org", name: "Synthetic receipt workspace", status: "active" };
    const user = { id: "receipt-warning-user", org_id: org.id, name: "Receipt owner", email: "receipt@example.test", role: "owner" };
    if (path === "/auth/me") return json({ user, organization: org });
    if (path === "/auth/organizations") return json([{ org_id: org.id, name: org.name, current: true }]);
    if (path === "/modules") return json([{ key: "expenses", name: "Expenses", enabled: true, ready: true, requires_issuer: false }]);
    if (path === "/expenses/receipt-scan" && request.method() === "POST") {
      const next = replies[scans.length]; scans.push(request.postData() ?? "");
      expect(next, "each scan has an explicit synthetic response").toBeDefined();
      await next.wait; return json(next.body, next.status);
    }
    if (path === "/expenses/receipt-warning-report/items" && request.method() === "POST") {
      itemWrites.push(request.postDataJSON()); return json({ id: "synthetic-added-item", ...request.postDataJSON() }, 201);
    }
    if (path === "/expenses/receipt-warning-report") return json({
      id: "receipt-warning-report", employee_id: user.id, employee_name: user.name, title: "Synthetic receipt review", status: "draft",
      currency: "EUR", total: "0.00", vat_total: "0.00", total_eur: "0.00", version: 1,
      submitted_at: null, created_at: "2026-11-05T09:00:00Z", note: null, decided_at: null, decided_by: null, decision_note: null,
      items: [], policy_violations: [], approval_steps: [],
    });
    return json([]);
  });
  await page.goto("/expenses/receipt-warning-report");
  await expect(page.getByRole("heading", { name: "Add an expense", exact: true })).toBeVisible();
  return { scans, itemWrites };
}
async function scan(page: Page, name: string) {
  await page.getByLabel("Receipt to scan", { exact: true }).setInputFiles({ name, mimeType: "image/png", buffer: PNG });
}
const notes = (page: Page) => page.getByRole("status", { name: "Receipt reading notes", exact: true });

test("reading advice persists beside unchanged suggestions and explicit manual review", async ({ page }, info) => {
  const wire = await setup(page, [{ body: { ...SUGGESTION, warnings: [WARNING], reading_preparation: { preprocess_version: "internal-version-not-for-display", source_width: 5000, source_height: 4000, raster_width: 2000, raster_height: 1600, resized: true } } }]);
  await scan(page, "first-receipt.png");
  await expect(notes(page)).toContainText(WARNING);
  await expect(notes(page)).toHaveAttribute("aria-live", "polite");
  for (const [label, value] of [["Merchant", SUGGESTION.merchant], ["Amount", SUGGESTION.amount], ["Tax", SUGGESTION.vat_amount], ["Spend date", SUGGESTION.spend_date], ["Description", SUGGESTION.merchant]]) await expect(page.getByLabel(label, { exact: true })).toHaveValue(value);
  const toast = page.getByRole("alert").filter({ hasText: "Scanned (ocr). Review the prefilled fields." });
  await toast.getByRole("button", { name: "Dismiss", exact: true }).click();
  await expect(toast).toHaveCount(0); await expect(notes(page)).toContainText(WARNING);
  await expect(page.getByText("internal-version-not-for-display", { exact: true })).toHaveCount(0);
  expect(wire.itemWrites).toEqual([]);
  await page.getByLabel("Amount", { exact: true }).fill("98.76");
  await page.getByLabel("Description", { exact: true }).fill("Manually reviewed receipt");
  await expect(notes(page)).toContainText(WARNING);
  await page.getByRole("button", { name: "Add expense", exact: true }).click();
  await expect.poll(() => wire.itemWrites.length).toBe(1);
  expect(wire.itemWrites[0]).toMatchObject({ amount: "98.76", vat_amount: "23.45", merchant: SUGGESTION.merchant, spend_date: SUGGESTION.spend_date, description: "Manually reviewed receipt" });
  expect(wire.scans).toHaveLength(1); expect(wire.scans[0]).toContain("first-receipt.png");
  await info.attach("receipt-warning-scope", { body: JSON.stringify({ scanCount: wire.scans.length, itemWrites: wire.itemWrites, scope: "Synthetic routed UI: server warning and suggestions remain advisory until explicit Add expense; no OCR accuracy, real storage or device acceptance claim." }), contentType: "application/json" });
});

test("a new source clears stale reading advice while pending, when unchanged, and on failure", async ({ page }) => {
  let release!: () => void; const pending = new Promise<void>(resolve => { release = resolve; });
  const wire = await setup(page, [
    { body: { ...SUGGESTION, warnings: [WARNING] } },
    { wait: pending, body: { ...SUGGESTION, merchant: "Second synthetic source", warnings: [] } },
    { body: { ...SUGGESTION, warnings: [WARNING] } },
    { status: 503, body: { detail: "Synthetic receipt reading unavailable" } },
  ]);
  try {
    await scan(page, "reduced.png"); await expect(notes(page)).toBeVisible();
    await scan(page, "unchanged.png");
    await expect(page.getByRole("button", { name: "Scanning…", exact: true })).toBeDisabled();
    await expect(notes(page)).toHaveCount(0);
    release();
    await expect(page.getByLabel("Merchant", { exact: true })).toHaveValue("Second synthetic source");
    await expect(page.getByRole("button", { name: "📷 Scan receipt", exact: true })).toBeEnabled();
    await expect(notes(page)).toHaveCount(0);
    await scan(page, "reduced-again.png"); await expect(notes(page)).toBeVisible();
    await scan(page, "failed-new-source.png");
    await expect(page.getByRole("alert").filter({ hasText: "Synthetic receipt reading unavailable" })).toBeVisible();
    await expect(notes(page)).toHaveCount(0);
    expect(wire.scans).toHaveLength(4); expect(wire.itemWrites).toEqual([]);
  } finally { release(); }
});

test("only returned text advice renders; hostile text is escaped and absent warnings preserve the old scan flow", async ({ page }) => {
  const hostile = "<img src=x onerror=window.receiptWarningExecuted=true>";
  const wire = await setup(page, [
    { body: { ...SUGGESTION, warnings: [null, 12, {}, "   ", hostile, WARNING] } },
    { body: SUGGESTION },
  ]);
  await scan(page, "hostile-advice.png");
  await expect(notes(page).getByRole("listitem")).toHaveText([hostile, WARNING]);
  await expect(notes(page).locator("img,script")).toHaveCount(0);
  expect(await page.evaluate(() => "receiptWarningExecuted" in window)).toBe(false);
  await scan(page, "legacy-no-warnings.png");
  await expect(page.getByRole("button", { name: "📷 Scan receipt", exact: true })).toBeEnabled();
  await expect(notes(page)).toHaveCount(0);
  await expect(page.getByLabel("Amount", { exact: true })).toHaveValue(SUGGESTION.amount);
  expect(wire.itemWrites).toEqual([]);
});


test("receipt reading advice wraps within its card with enlarged narrow text", async ({ page }, info) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const wire = await setup(page, [{ body: { ...SUGGESTION, warnings: [WARNING] } }]);
  await page.evaluate(() => { document.documentElement.style.fontSize = "32px"; });
  await scan(page, "narrow-receipt.png");
  await expect(notes(page)).toContainText(WARNING);
  await notes(page).scrollIntoViewIfNeeded();
  const bounds = await notes(page).evaluate(e => { const r = e.getBoundingClientRect(), card = e.closest(".card")!.getBoundingClientRect(); return { left: r.left, right: r.right, client: e.clientWidth, scroll: e.scrollWidth, cardLeft: card.left, cardRight: card.right }; });
  expect(bounds.scroll).toBeLessThanOrEqual(bounds.client);
  expect(bounds.left).toBeGreaterThanOrEqual(bounds.cardLeft); expect(bounds.right).toBeLessThanOrEqual(bounds.cardRight);
  await expect(page.getByLabel("Amount", { exact: true })).toHaveValue(SUGGESTION.amount);
  expect(wire.itemWrites).toEqual([]);
  await notes(page).screenshot({ path: info.outputPath("receipt-warning-390-root32.png") });
  await info.attach("receipt-advice-bounds", { body: JSON.stringify({ bounds, scope: "Synthetic390px/32px-root-text reading note containment only; no physical-device, screenreader or whole-page reflow claim." }), contentType: "application/json" });
});
