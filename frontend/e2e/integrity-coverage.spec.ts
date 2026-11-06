import { expect, test, type Page } from "@playwright/test";

// Actual Settings page with intercepted API data. Native verifier coverage is
// characterized separately; these checks establish UI claims, not storage health.
const ORG = { id: "integrity-org", name: "Synthetic integrity workspace", status: "active" };
const HEALTHY = { checked: 3, ok: 3, healthy: true, issues: [] };
const EMPTY = { checked: 0, ok: 0, healthy: true, issues: [] };
const SCOPE = "Check current expense receipts, company logos, retained email attachments and original supplier invoice uploads against their recorded checksums.";
const EXCLUSIONS = "Other document categories and the version history of replaced files are outside this check.";
const json = (body: unknown, status = 200) => ({ status, contentType: "application/json", body: JSON.stringify(body) });
type Reply = { body: unknown; status?: number };

async function openSettings(page: Page, options: { replies?: Reply[]; canEdit?: boolean; hold?: Promise<void> } = {}) {
  const wire = { verifications: 0, jobs: 0, writes: [] as string[], errors: [] as string[] };
  page.on("pageerror", (error) => wire.errors.push(error.message));
  await page.addInitScript(() => localStorage.setItem("invoiceiq_token", "synthetic-integrity-token"));
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname.replace(/^.*\/api\/v1/, "");
    const method = route.request().method();
    if (method !== "GET") wire.writes.push(method + " " + path);
    if (path === "/auth/me") return route.fulfill(json({
      user: { id: "integrity-user", org_id: ORG.id, name: "Synthetic owner", email: "integrity@example.test", role: "owner", is_platform_admin: false },
      organization: ORG,
      permissions: options.canEdit === false ? ["invoice.read", "report.read"] : ["invoice.read", "report.read", "settings.manage"],
    }));
    if (path === "/auth/organizations") return route.fulfill(json([{ org_id: ORG.id, name: ORG.name, role: "owner", current: true }]));
    if (path === "/modules") return route.fulfill(json([]));
    if (path === "/dashboard/onboarding") return route.fulfill(json({ steps: [], done_count: 0, complete: true, dismissed: true, can_dismiss: true }));
    if (path === "/settings/validation") return route.fulfill(json({ ai_validation_enabled: false, human_validation_enabled: false, overcharge_block_enabled: false }));
    if (path === "/settings/lifecycle") return route.fulfill(json({ offer_prefix: null, final_invoice_requires_acceptance: false }));
    if (path === "/settings/schedule") return route.fulfill(json({ assignment_remind_hours: null, client_notice_hours: null }));
    if (path === "/sso/connection") return route.fulfill(json(null));
    if (path === "/sso/assignable-roles") return route.fulfill(json(["user"]));
    if (path === "/retention") return route.fulfill(json({ categories: [], holds: [] }));
    if (path === "/jobs") { wire.jobs++; return route.fulfill(json([])); }
    if (path === "/webhooks" || path === "/workspace/export-requests") return route.fulfill(json([]));
    if (path === "/integrity/documents/verify" && method === "POST") {
      const index = wire.verifications++;
      if (options.hold) await options.hold;
      const reply = options.replies?.[index] ?? { body: HEALTHY };
      return route.fulfill(json(reply.body, reply.status ?? 200));
    }
    return route.fulfill(json({ items: [], total: 0 }));
  });
  await page.goto("/settings");
  await expect(page.getByRole("heading", { name: "Admin panel", exact: true })).toBeVisible();
  return wire;
}

const panel = (page: Page) => page.locator("section").filter({ has: page.getByRole("heading", { name: "Data integrity", exact: true }) });
const verify = (page: Page) => panel(page).getByRole("button", { name: /Verify (stored|supported) documents/ });

test("scope describes supported references and exclusions before verification", async ({ page }) => {
  const wire = await openSettings(page);
  await expect(panel(page)).toContainText(SCOPE);
  await expect(panel(page)).toContainText(EXCLUSIONS);
  await expect(panel(page)).not.toContainText("every stored document");
  expect(wire.verifications).toBe(0);
  expect(wire.errors).toEqual([]);
});

test("zero references is neutral and does not certify other stored files", async ({ page }) => {
  const wire = await openSettings(page, { replies: [{ body: EMPTY }] });
  await verify(page).click();
  const result = panel(page).getByText("No supported document references to check.", { exact: true });
  await expect(result).toBeVisible();
  await expect(result).toHaveClass(/bg-slate-50/);
  await expect(result).not.toHaveClass(/emerald/);
  await expect(panel(page)).not.toContainText("no corruption or loss");
  await expect(panel(page)).toContainText(EXCLUSIONS);
  expect(wire.verifications).toBe(1);
  expect(wire.errors).toEqual([]);
});

for (const count of [1, 3]) {
  test(`positive result is limited to ${count} checked supported reference${count === 1 ? "" : "s"}`, async ({ page }) => {
    const wire = await openSettings(page, { replies: [{ body: { ...HEALTHY, checked: count, ok: count } }] });
    await verify(page).click();
    await expect(panel(page)).toContainText(`No problems found in ${count} checked supported reference${count === 1 ? "" : "s"}.`);
    await expect(panel(page)).toContainText(EXCLUSIONS);
    await expect(panel(page)).not.toContainText("no corruption or loss");
    expect(wire.writes).toEqual(["POST /integrity/documents/verify"]);
    expect(wire.errors).toEqual([]);
  });
}

test("problems retain reference counts and issue details without expanding coverage", async ({ page }) => {
  const issue = { kind: "upload", entity_id: "synthetic-upload", problem: "missing", detail: "Stored original is unavailable" };
  const wire = await openSettings(page, { replies: [{ body: { checked: 3, ok: 2, healthy: false, issues: [issue] } }] });
  await verify(page).click();
  await expect(panel(page)).toContainText("1 problem found across 3 checked supported references.");
  await expect(panel(page)).toContainText(issue.problem);
  await expect(panel(page)).toContainText(issue.kind);
  await expect(panel(page)).toContainText(issue.detail);
  await expect(panel(page)).toContainText(EXCLUSIONS);
  expect(wire.errors).toEqual([]);
});

for (const created of [true, false]) {
  test(`queued response ${created ? "creates" : "reuses"} a job without claiming it is running`, async ({ page }) => {
    const wire = await openSettings(page, { replies: [{ status: 202, body: { queued: true, job_id: "synthetic-job", job_kind: "integrity.verify_documents", references: 501, sync_limit: 500, created } }] });
    await expect.poll(() => wire.jobs).toBeGreaterThan(0);
    await verify(page).click();
    const queued = panel(page).getByTestId("integrity-queued");
    await expect(queued).toContainText("501 supported references exceed the 500 this page checks while you wait.");
    await expect(queued).toContainText(created ? "Verification job queued." : "Today's verification job already exists.");
    await expect(queued).toContainText("Check Background jobs for its status and result.");
    await expect(queued).not.toContainText("running");
    await expect(page.getByRole("alert")).toContainText(created ? "Verification job queued." : "Today's verification job already exists.");
    await expect.poll(() => wire.jobs).toBeGreaterThan(1);
    await expect(panel(page)).not.toContainText("No problems found");
    expect(wire.verifications).toBe(1);
    expect(wire.errors).toEqual([]);
  });
}

test("a failed verification offers the existing action for an explicit retry", async ({ page }) => {
  const wire = await openSettings(page, { replies: [{ status: 503, body: { detail: "Storage service unavailable. Try again.", code: "storage_unavailable" } }, { body: HEALTHY }] });
  await verify(page).click();
  await expect(page.getByRole("alert")).toContainText("Storage service unavailable. Try again.");
  await expect(verify(page)).toBeEnabled();
  await expect(panel(page)).not.toContainText("No problems found");
  await expect(panel(page)).not.toContainText("No supported document references to check.");
  await verify(page).click();
  await expect(panel(page)).toContainText("No problems found in 3 checked supported references.");
  expect(wire.verifications).toBe(2);
  expect(wire.errors).toEqual([]);
});

test("served read-only permissions retain the existing hidden integrity action", async ({ page }) => {
  const wire = await openSettings(page, { canEdit: false });
  await expect(page.getByText("You can view these settings, but only an admin can change them.")).toBeVisible();
  await expect(panel(page)).toHaveCount(0);
  expect(wire.verifications).toBe(0);
  expect(wire.writes).toEqual([]);
  expect(wire.errors).toEqual([]);
});

test("pending verification cannot post twice and remains contained at narrow enlarged text", async ({ page }) => {
  let release!: () => void;
  const hold = new Promise<void>((resolve) => { release = resolve; });
  await page.setViewportSize({ width: 390, height: 844 });
  const wire = await openSettings(page, { hold });
  await page.evaluate(() => { document.documentElement.style.fontSize = "32px"; });
  await expect(panel(page)).toContainText(SCOPE);
  await verify(page).click();
  await expect(panel(page).getByRole("button", { name: "Verifying…", exact: true })).toBeDisabled();
  expect(wire.verifications).toBe(1);
  release();
  await expect(panel(page)).toContainText("No problems found in 3 checked supported references.");
  const geometry = await panel(page).evaluate((element) => ({ width: element.getBoundingClientRect().width, scroll: element.scrollWidth, right: element.getBoundingClientRect().right, viewport: window.innerWidth }));
  expect(geometry.right).toBeLessThanOrEqual(geometry.viewport);
  expect(geometry.scroll).toBeLessThanOrEqual(Math.ceil(geometry.width));
  expect(wire.verifications).toBe(1);
  expect(wire.errors).toEqual([]);
});
