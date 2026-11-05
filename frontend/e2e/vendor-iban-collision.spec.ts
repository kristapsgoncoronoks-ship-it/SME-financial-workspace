import { test, expect, type Page, type Route } from "@playwright/test";

/**
 * DB-014 (audit 2026-09-05, P2 batch 8) — the cross-vendor collision surface.
 *
 * Two suppliers paying into one bank account is the payment-redirection
 * signal the second-approver control exists to catch (a factoring company is
 * the legitimate case). The server now names the other holders on the vendor
 * row and on a pending IBAN change; this proves the page SHOWS them — on the
 * row, in the approval inbox and in the confirm dialog — and never hides a
 * request behind them. Live app shell, API mocked via page.route. Synthetic
 * fixtures only.
 */

const ORG = { id: "org-1", name: "Haulage Co", status: "active" };

const json = (body: unknown, code = 200) => ({
  status: code,
  contentType: "application/json",
  body: JSON.stringify(body),
});

type Handler = (p: string, route: Route) => Promise<void> | void | false;

async function signedIn(page: Page, handler: Handler) {
  await page.addInitScript(() => localStorage.setItem("invoiceiq_token", "e2e-token"));
  const user = { id: "u1", email: "crew@haulage.example", name: "Site Crew", role: "owner", org_id: "org-1", is_platform_admin: false };
  await page.route("**/api/v1/**", async (route: Route) => {
    const p = new URL(route.request().url()).pathname.replace(/^.*\/api\/v1/, "");
    if (p === "/auth/me") return route.fulfill(json({ user, organization: ORG }));
    if (p === "/auth/organizations") return route.fulfill(json([{ org_id: "org-1", name: ORG.name, role: "owner", current: true }]));
    if (p === "/modules") return route.fulfill(json([]));
    if (p === "/dashboard/onboarding") return route.fulfill(json({ steps: [], done_count: 0, complete: true, dismissed: true, can_dismiss: true }));
    const handled = await handler(p, route);
    if (handled === false) return route.fulfill(json([]));
  });
}

const STEEL = { id: "v-steel", name: "Steel GmbH" };
const VENDORS = [
  { id: "v-steel", name: "Steel GmbH", tax_id: null, country: "DE", category: null, iban: "DE89370400440532013000", bic: null, status: "active", version: 1, pending_changes: [], iban_shared_with: [{ id: "v-trading", name: "Steel Trading GmbH" }] },
  { id: "v-trading", name: "Steel Trading GmbH", tax_id: null, country: "DE", category: null, iban: "DE89370400440532013000", bic: null, status: "provisional", version: 1, pending_changes: [], iban_shared_with: [STEEL] },
  { id: "v-cargo", name: "Cargo BV", tax_id: null, country: "NL", category: null, iban: "NL91ABNA0417164300", bic: null, status: "active", version: 2, pending_changes: [], iban_shared_with: [] },
];
const CHANGE = {
  id: "cr-1",
  vendor_id: "v-cargo",
  vendor_name: "Cargo BV",
  field: "iban",
  old_value: "NL91ABNA0417164300",
  new_value: "DE89370400440532013000",
  status: "pending",
  requested_by: "u2",
  requested_by_email: "capture@haulage.example",
  requested_at: "2026-09-09T06:00:00Z",
  decided_by: null,
  decided_by_email: null,
  decided_at: null,
  decision_note: null,
  source_document_id: null,
  shared_with: [STEEL, { id: "v-trading", name: "Steel Trading GmbH" }],
};

test("DB-014: a vendor row names the other suppliers on its account, a lone account says nothing", async ({ page }) => {
  await signedIn(page, (p, route) => {
    if (p === "/vendors") return route.fulfill(json(VENDORS));
    if (p === "/vendors/changes") return route.fulfill(json([]));
    return false;
  });
  await page.goto("/vendors");

  await expect(page.getByText("Same account as Steel Trading GmbH")).toBeVisible();
  await expect(page.getByText("Same account as Steel GmbH")).toBeVisible();
  await expect(page.getByText("Same account as Cargo BV")).toHaveCount(0);
});

test("DB-014: a pending IBAN change names the holders in the inbox and again in the confirm dialog", async ({ page }) => {
  await signedIn(page, (p, route) => {
    if (p === "/vendors") return route.fulfill(json(VENDORS));
    if (p === "/vendors/changes") return route.fulfill(json([CHANGE]));
    return false;
  });
  await page.goto("/vendors");

  await expect(page.getByText("Already on file for Steel GmbH, Steel Trading GmbH")).toBeVisible();
  await page.getByRole("button", { name: "Approve", exact: true }).click();
  await expect(
    page.getByRole("dialog").getByText("This account is already on file for Steel GmbH, Steel Trading GmbH."),
  ).toBeVisible();
  // The decision is still the approver's: the button is there, not hidden.
  await expect(page.getByRole("button", { name: "Approve change" })).toBeVisible();
});

for (const [width, fontSize] of [[320, 16], [390, 32], [1280, 16]]) {
  test(`supplier facts, bank fields and second-approver actions reflow at ${width}px/${fontSize}px text`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 844 });
    const writes: { path: string; body: unknown }[] = [];
    await signedIn(page, (path, route) => {
      if (route.request().method() !== "GET") {
        writes.push({ path, body: route.request().postDataJSON() });
        return route.fulfill(json({ ...VENDORS[2], iban: "NL91ABNA0417164300", bic: "SYNTHETIC" }));
      }
      if (path === "/vendors") return route.fulfill(json(VENDORS));
      if (path === "/vendors/changes") return route.fulfill(json([CHANGE, { ...CHANGE, id: "cr-mine", requested_by: "u1", vendor_name: "Own supplier request" }]));
      return false;
    });
    await page.goto("/vendors");
    await expect(page.getByRole("heading", { name: "Suppliers", exact: true })).toBeVisible();
    await page.evaluate(size => { document.documentElement.style.fontSize = `${size}px`; }, fontSize);
    await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
    const bounds = await page.evaluate(() => ({ viewport: innerWidth, body: document.body.scrollWidth, root: document.documentElement.scrollWidth, escaped: [...document.querySelectorAll("body *")].filter(e => { const r=e.getBoundingClientRect(); return r.right > innerWidth+1 && !e.closest("[role=region]"); }).map(e=>({tag:e.tagName,cls:e.className,right:e.getBoundingClientRect().right,text:e.textContent?.slice(0,80),pos:getComputedStyle(e).position})), tables: [...document.querySelectorAll("main table")].map(e => ({ width: e.getBoundingClientRect().width, scroll: e.scrollWidth })), containers: [...document.querySelectorAll("main, main > div, main section, main section > div, main [role=region]")].map(e => ({ tag: e.tagName, classes: e.className, width: e.getBoundingClientRect().width, scroll: e.scrollWidth, display: getComputedStyle(e).display, overflow: getComputedStyle(e).overflowX })) }));
    await testInfo.attach("supplier-page-bounds", { body: JSON.stringify(bounds), contentType: "application/json" });
    expect(bounds.root).toBeLessThanOrEqual(width);
    expect(bounds.body).toBeLessThanOrEqual(width);
    const changes = page.getByRole("region", { name: "Pending supplier bank changes", exact: true });
    const suppliers = page.getByRole("region", { name: "Suppliers and bank details", exact: true });
    for (const region of [changes, suppliers]) {
      await region.focus(); await page.keyboard.press("Shift+Tab"); await page.keyboard.press("Tab");
      await expect(region).toBeFocused();
      if (await region.evaluate(e => e.scrollWidth > e.clientWidth)) {
        await page.keyboard.press("ArrowRight");
        await expect.poll(() => region.evaluate(e => e.scrollLeft)).toBeGreaterThan(0);
        await region.evaluate(e => new Promise<void>(resolve => { let previous = e.scrollLeft, stable = 0; const frame = () => { stable = e.scrollLeft === previous ? stable + 1 : 0; previous = e.scrollLeft; if (stable >= 5) resolve(); else requestAnimationFrame(frame); }; requestAnimationFrame(frame); }));
      }
      await expect(region.getByRole("table")).toHaveCount(1);
    }
    await expect(changes.getByText("Already on file for Steel GmbH, Steel Trading GmbH")).toHaveCount(2);
    const own = changes.getByRole("row").filter({ hasText: "Own supplier request" });
    await expect(own.getByRole("button", { name: "Approve", exact: true })).toHaveCount(0);
    await expect(own).toContainText("Yours — a different approver must decide");
    await expect(suppliers.getByText("Same account as Steel Trading GmbH")).toBeVisible();
    const iban = suppliers.getByRole("textbox", { name: "IBAN for Cargo BV", exact: true });
    await iban.focus(); await expect(iban).toBeInViewport({ ratio: 0.99 });
    await expect(iban).toHaveValue("NL91ABNA0417164300");
    const bic = suppliers.getByRole("textbox", { name: "BIC for Cargo BV", exact: true });
    await bic.fill("SYNTHETIC");
    const save = suppliers.getByRole("row").filter({ hasText: "Cargo BV" }).getByRole("button", { name: "Save", exact: true });
    await save.focus(); await expect(save).toBeInViewport({ ratio: 0.99 }); await page.keyboard.press("Enter");
    await expect.poll(() => writes).toEqual([{ path: "/vendors/v-cargo", body: { iban: "NL91ABNA0417164300", bic: "SYNTHETIC", version: 2 } }]);
    await changes.getByRole("button", { name: "Approve", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "Apply this bank-detail change?", exact: true });
    await expect(dialog).toContainText("This account is already on file for Steel GmbH, Steel Trading GmbH.");
    await dialog.getByRole("button", { name: "Approve change", exact: true }).focus();
    await expect(dialog.getByRole("button", { name: "Approve change", exact: true })).toBeInViewport({ ratio: 0.99 });
    await page.keyboard.press("Escape");
    expect(writes).toHaveLength(1);
    await page.screenshot({ path: testInfo.outputPath("supplier-reflow.png"), fullPage: true });
  });
}
