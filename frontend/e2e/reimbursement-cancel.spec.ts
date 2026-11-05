import { test, expect, type Page, type Route } from "@playwright/test";

// Synthetic browser responses prove client presentation and request interlocks.
// Actual persistence/authorization/row locks are covered by the backend API/PG
// reimbursement tests; these mocks do not qualify bank disposition or settlement.
const ORG = { id: "reimbursement-org", name: "Synthetic payout workspace", status: "active" };
const USER = { id: "reimbursement-user", org_id: ORG.id, name: "Synthetic owner", role: "owner", email: "owner@example.com" };
const OPEN_BATCH = {
  id: "reimbursement-batch", reference: null, method: "bank_transfer", status: "open", note: null,
  total_eur: "60.00", paid_at: null, created_by: "maker@example.com",
  exported_at: null as string | null, export_count: 0, last_msg_id: null as string | null,
  version: 1, created_at: "2026-10-26T00:00:00Z", report_count: 1,
  payment_evidence: { basis: "not_recorded", bank_confirmation: "unknown", recorded_by_id: null, recorded_at: null },
};
const EXPORTED = { export_count: 1, exported_at: "2026-10-27T08:00:00Z" };
const CONFLICT = {
  code: "batch_exported_cannot_cancel",
  detail: "This batch has a recorded export and cannot be cancelled. Check the bank file’s status with your bank or accountant before arranging another payment.",
};
const json = (body: unknown, status = 200) => ({ status, contentType: "application/json", body: JSON.stringify(body) });
type Batch = typeof OPEN_BATCH;
type CancelPayload = { version: number; report_versions: Record<string, number>; reason: string | null };
const detail = (batch: Batch) => ({
  ...batch,
  reports: batch.status === "cancelled" ? [] : [{
    id: "reimbursement-report", title: "Synthetic depot parking", employee_name: "Synthetic payee",
    total: "60.00", currency: "EUR", total_eur: "60.00" as string | null, status: "marked_for_reimbursement",
    payment_reference: null as string | null, reimbursed_at: null as string | null, version: 4,
  }],
});
type CancelResult = { batch: ReturnType<typeof detail>; released_reports: ReturnType<typeof detail>["reports"]; reason: string | null };
const cancellation = (batch: Batch, reason: string | null = null): CancelResult => ({
  batch: detail({ ...batch, status: "cancelled", version: batch.version + 1, report_count: 0 }),
  released_reports: detail(batch).reports.map((report) => ({ ...report, version: report.version + 1 })), reason,
});
type Wire = {
  batch: Batch; batchReads: number; detailReads: number; approvedReads: number; posts: number; deletes: number; exports: string[];
  requests: Array<{ key: string | undefined; payload: CancelPayload }>; lookups: string[]; result: CancelResult | null; reviewedReports: ReturnType<typeof detail>["reports"];
  scopedRequests: Array<{ account: string; method: string; path: string }>;
  postStatus: number; lookupOffline: boolean;
  lostResponse: boolean; commitOnFailure: boolean; lookupStatus: number; listStatus: number; hideBatch: boolean; resultPatch?: (result: CancelResult) => CancelResult;
  exportCalls: { path: string; search: string }[]; exportConflict: "already_exported" | "skipped_payees" | null;
  holdCancel: boolean; pendingCancel?: Route; conflict: boolean;
  holdExport: boolean; pendingExport?: Route;
  holdReadback: boolean; pendingReadbacks: Route[]; readbackStatus: number;
};

async function setup(page: Page, patch: Partial<Batch> = {}) {
  const wire: Wire = {
    batch: { ...OPEN_BATCH, ...patch }, batchReads: 0, detailReads: 0, approvedReads: 0, posts: 0, deletes: 0, exports: [], exportCalls: [], exportConflict: null,
    requests: [], lookups: [], result: null, reviewedReports: patch.report_count === 0 ? [] : detail(OPEN_BATCH).reports, lostResponse: false, commitOnFailure: true, lookupStatus: 200, listStatus: 200, hideBatch: false,
    scopedRequests: [], postStatus: 200, lookupOffline: false,
    holdCancel: false, conflict: false, holdExport: false, holdReadback: false, pendingReadbacks: [], readbackStatus: 200,
  };
  await page.addInitScript(() => {
    if (!sessionStorage.getItem("cancellation-fixture-seeded")) {
      localStorage.setItem("invoiceiq_token", "synthetic-reimbursement-token");
      sessionStorage.setItem("cancellation-fixture-seeded", "1");
    }
  });
  await page.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname.replace("/api/v1", "");
    const method = route.request().method();
    const account = route.request().headers().authorization === "Bearer synthetic-reimbursement-B" ? "B" : "A";
    const user = account === "B" ? { ...USER, id: "reimbursement-user-B", org_id: "reimbursement-org-B", name: "Synthetic replacement account" } : USER;
    const org = account === "B" ? { ...ORG, id: "reimbursement-org-B", name: "Synthetic replacement workspace" } : ORG;
    if (path === "/auth/me") return route.fulfill(json({ user, organization: org, permissions: ["invoice.read", "expense.read", "expense.approve"] }));
    if (path === "/auth/organizations") return route.fulfill(json([{ org_id: org.id, name: org.name, role: user.role, current: true }]));
    if (path === "/modules") return route.fulfill(json([{ key: "expenses", name: "Employee expenses", enabled: true, ready: true, requires_issuer: false }]));
    if (path.startsWith("/reimbursements")) wire.scopedRequests.push({ account, method, path });
    if (path === "/reimbursements/eligible" && method === "GET") {
      wire.approvedReads += 1;
      return route.fulfill(json({ items: [], total: 0, page: 1, page_size: 100 }));
    }
    if (path === "/reimbursements/page" && method === "GET") {
      wire.batchReads += 1;
      if (wire.posts > 0 && wire.listStatus !== 200) return route.fulfill(json({ detail: "Synthetic batch list unavailable" }, wire.listStatus));
      return route.fulfill(json({ items: wire.hideBatch || account === "B" ? [] : [wire.batch], total: wire.hideBatch || account === "B" ? 0 : 1, page: 1, page_size: 100 }));
    }
    if (path.startsWith("/reimbursements/commands/")) {
      expect(method).toBe("GET");
      const key = path.slice("/reimbursements/commands/".length);
      wire.lookups.push(key);
      if (wire.lookupOffline) return route.abort("internetdisconnected");
      if (wire.lookupStatus !== 200 || !wire.result) return route.fulfill(json({ detail: "No committed cancellation is visible yet." }, wire.lookupStatus === 200 ? 404 : wire.lookupStatus));
      return route.fulfill(json({ status: "committed", command_key: key, operation: "reimbursement_cancelled", target_id: OPEN_BATCH.id,
        result: wire.resultPatch ? wire.resultPatch(structuredClone(wire.result)) : wire.result }));
    }
    if (path === `/reimbursements/${OPEN_BATCH.id}` && method === "GET") {
      wire.batchReads += 1;
      wire.detailReads += 1;
      if ((wire.posts > 0 || wire.exports.length > 0) && wire.holdReadback) { wire.pendingReadbacks.push(route); return; }
      if ((wire.posts > 0 || wire.exports.length > 0) && wire.readbackStatus !== 200) return route.fulfill(json({ detail: "Synthetic batch readback unavailable" }, wire.readbackStatus));
      return route.fulfill(json({ ...detail(wire.batch), reports: wire.batch.status === "cancelled" ? [] : wire.reviewedReports }));
    }
    if (path === `/reimbursements/${OPEN_BATCH.id}` && method === "DELETE") {
      wire.deletes += 1;
      return route.fulfill(json({ detail: "The browser must use the typed cancellation command." }, 405));
    }
    if (path === `/reimbursements/${OPEN_BATCH.id}/cancel` && method === "POST") {
      wire.posts += 1;
      expect(route.request().headers()["x-expected-user-id"]).toBe(USER.id);
      expect(route.request().headers()["x-expected-org-id"]).toBe(ORG.id);
      const payload = route.request().postDataJSON() as CancelPayload;
      wire.requests.push({ key: route.request().headers()["idempotency-key"], payload });
      if (wire.postStatus !== 200) return route.fulfill(json({ detail: "Synthetic cancellation result unavailable", code: wire.postStatus === 429 ? "rate_limited" : "expense_audit_unavailable" }, wire.postStatus));
      const result = cancellation(wire.batch, payload.reason);
      result.released_reports = wire.reviewedReports.map((report) => ({ ...report, version: report.version + 1 }));
      if (!wire.conflict && (!wire.lostResponse || wire.commitOnFailure)) wire.result = result;
      if (wire.holdCancel) { wire.pendingCancel = route; return; }
      if (wire.conflict) {
        wire.batch = { ...wire.batch, ...EXPORTED };
        return route.fulfill(json(CONFLICT, 409));
      }
      if (!wire.lostResponse || wire.commitOnFailure) wire.batch = result.batch;
      if (wire.lostResponse) return route.abort("failed");
      return route.fulfill(json(result));
    }
    if (path === `/reimbursements/${OPEN_BATCH.id}/export` || path === `/reimbursements/${OPEN_BATCH.id}/sepa`) {
      expect(method).toBe("POST");
      wire.exports.push(path);
      wire.exportCalls.push({ path, search: url.search });
      if (wire.holdExport) { wire.pendingExport = route; return; }
      if (wire.exportConflict === "already_exported" && url.searchParams.get("confirm_reexport") !== "true") {
        wire.batch = { ...wire.batch, ...EXPORTED };
        return route.fulfill(json({ code: "already_exported", detail: "This batch was already exported. Re-exporting produces a NEW bank file with a new message id." }, 409));
      }
      if (wire.exportConflict === "skipped_payees" && url.searchParams.get("acknowledge_skipped") !== "true") {
        return route.fulfill(json({ code: "skipped_payees", detail: "1 payee(s) have no IBAN on file and would be dropped from the bank file: Synthetic skipped payee. Add their bank details, or pass acknowledge_skipped=true to export without them." }, 409));
      }
      wire.batch = { ...wire.batch, ...EXPORTED };
      const sepa = path.endsWith("/sepa");
      return route.fulfill({
        contentType: sepa ? "application/xml" : "text/csv",
        headers: { "Content-Disposition": `attachment; filename="synthetic-reimbursement.${sepa ? "xml" : "csv"}"`, "X-Content-Type-Options": "nosniff", ...(sepa ? { "X-Skipped": "1" } : {}) },
        body: sepa ? '<?xml version="1.0" encoding="UTF-8"?><Document xmlns="urn:iso:std:iso:20022:tech:xsd:pain.001.001.03"><SyntheticFixtureOnly/></Document>' : "employee,amount\nSynthetic payee,60.00\n",
      });
    }
    return route.fulfill(json([]));
  });
  await page.goto("/reimbursements");
  await expect(page.getByRole("heading", { level: 1, name: "Reimbursements", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Cancel", exact: true })).toBeVisible();
  return wire;
}

const cancel = (page: Page) => page.getByRole("button", { name: "Cancel", exact: true });
const confirmation = (page: Page) => page.getByRole("dialog", { name: /Cancel this reimbursement batch/i });
async function confirmCancel(page: Page) {
  await cancel(page).click();
  await expect(confirmation(page)).toBeVisible();
  await confirmation(page).getByRole("button", { name: "Cancel batch", exact: true }).click();
}
async function expectConflictingActionsDisabled(page: Page) {
  for (const name of ["Cancel", "Record payment", "Export CSV", "Export SEPA"]) {
    await expect(page.getByRole("button", { name, exact: true })).toBeDisabled();
  }
  await expect(page.getByRole("textbox", { name: "Payment reference", exact: true })).toBeDisabled();
}

for (const [name, marker] of [
  ["recorded export count", { export_count: 1 }],
  ["legacy negative count", { export_count: -1 }],
  ["legacy timestamp only", { exported_at: EXPORTED.exported_at }],
  ["legacy message ID only", { last_msg_id: "REIMB-SYNTHETIC-LEGACY" }],
  ["legacy empty nonnull message ID", { last_msg_id: "" }],
] as const) {
  test(`${name} blocks cancellation with an accessible bank-disposition explanation`, async ({ page }) => {
    const wire = await setup(page, marker);
    await expect(cancel(page)).toBeDisabled();
    await expect(cancel(page)).toHaveAccessibleDescription(/bank.*accountant|accountant.*bank/i);
    const description = await cancel(page).getAttribute("aria-describedby");
    expect(description).toBeTruthy();
    const help = page.locator(`[id="${description}"]`);
    await expect(help).toBeVisible();
    await expect(help).toContainText(/status|reconcil/i);
    expect(wire.posts).toBe(0);
  });
}

test("unexported cancellation explains report release, allows dismissal, then sends one typed POST", async ({ page }) => {
  const wire = await setup(page);
  await expect(cancel(page)).toBeEnabled();
  await cancel(page).click();
  const dialog = confirmation(page);
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText(/unexported/i);
  await expect(dialog).toContainText(/report.*(return|available)|return.*report/i);
  await expect(dialog).not.toContainText("nothing is exported or paid");
  expect(wire.posts).toBe(0);
  await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(dialog).toBeHidden();
  expect(wire.posts).toBe(0);
  await confirmCancel(page);
  await expect.poll(() => wire.posts).toBe(1);
  await expect.poll(() => wire.batchReads).toBeGreaterThan(1);
  expect(wire.detailReads).toBeGreaterThan(0);
  await expect(page.getByText("cancelled", { exact: true })).toBeVisible();
  await expect(cancel(page)).toHaveCount(0);
  expect(wire.posts).toBe(1);
});

test("a stale exported conflict refreshes current data and keeps its recovery alert", async ({ page }) => {
  const wire = await setup(page);
  wire.conflict = true;
  await confirmCancel(page);
  await expect.poll(() => wire.posts).toBe(1);
  await expect.poll(() => wire.batchReads).toBeGreaterThan(1);
  expect(wire.detailReads).toBeGreaterThan(0);
  await expect(cancel(page)).toBeDisabled();
  const alert = page.getByRole("alert").filter({ hasText: "recorded export" });
  await expect(alert).toBeVisible();
  await expect(alert).toContainText("bank or accountant");
  await expect(cancel(page)).toHaveAccessibleDescription(/bank.*accountant/i);
  expect(wire.posts).toBe(1);
});

test("a failed readback stays locked and its explicit retry only reads current status", async ({ page }) => {
  const wire = await setup(page);
  wire.conflict = true;
  wire.readbackStatus = 500;
  await confirmCancel(page);
  const refresh = page.getByRole("button", { name: "Refresh batch status", exact: true });
  await expect(refresh).toBeVisible();
  await expectConflictingActionsDisabled(page);
  expect(wire.posts).toBe(1);
  expect(wire.exports).toHaveLength(0);
  const failedReads = wire.batchReads;
  wire.readbackStatus = 200;
  await refresh.click();
  await expect.poll(() => wire.batchReads).toBeGreaterThan(failedReads);
  await expect(refresh).toHaveCount(0);
  await expect(cancel(page)).toBeDisabled();
  await expect(cancel(page)).toHaveAccessibleDescription(/bank.*accountant/i);
  await expect(page.getByRole("alert").filter({ hasText: "recorded export" })).toBeVisible();
  expect(wire.posts).toBe(1);
  expect(wire.exports).toHaveLength(0);
});

test("a real scoped read timeout offers a GET-only retry without repeating cancellation", async ({ page }) => {
  const wire = await setup(page);
  wire.conflict = true;
  wire.holdReadback = true;
  // This is the real 15s Axios deadline, with no fake clock or timeout mock.
  // The 20s expectation budget belongs to this new bounded-read contract.
  const abortedRead = page.waitForEvent("requestfailed", {
    predicate: (request) => request.method() === "GET" && new URL(request.url()).pathname === `/api/v1/reimbursements/${OPEN_BATCH.id}`,
    timeout: 20_000,
  });
  await confirmCancel(page);
  await expect.poll(() => wire.pendingReadbacks.length).toBe(1);
  const refresh = page.getByRole("button", { name: "Refresh batch status", exact: true });
  await expect(refresh).toBeVisible({ timeout: 20_000 });
  expect((await abortedRead).failure()?.errorText).toMatch(/aborted/i);
  await expectConflictingActionsDisabled(page);
  expect(wire.detailReads).toBe(2);
  expect(wire.posts).toBe(1);
  expect(wire.exports).toHaveLength(0);

  // The aborted Route is not replayable. A new user-requested GET must own the
  // recovery, and the original cancellation must never be sent a second time.
  wire.pendingReadbacks.splice(0);
  await refresh.click();
  await expect.poll(() => wire.pendingReadbacks.length).toBe(1);
  expect(wire.detailReads).toBe(3);
  wire.holdReadback = false;
  await wire.pendingReadbacks.shift()!.fulfill(json(detail(wire.batch)));
  await expect(cancel(page)).toHaveAccessibleDescription(/bank.*accountant/i);
  await expect(page.getByRole("button", { name: "Export CSV", exact: true })).toBeEnabled();
  await expect(refresh).toHaveCount(0);
  await expect(page.getByRole("alert").filter({ hasText: "recorded export" })).toBeVisible();
  expect(wire.posts).toBe(1);
  expect(wire.exports).toHaveLength(0);
});

test("a pending export blocks competing actions until exported readback arrives", async ({ page }) => {
  const wire = await setup(page);
  wire.holdExport = true;
  wire.holdReadback = true;
  await page.getByRole("button", { name: "Export CSV", exact: true }).click();
  await expect.poll(() => wire.pendingExport !== undefined).toBe(true);
  await expectConflictingActionsDisabled(page);
  expect(wire.posts).toBe(0);
  wire.batch = { ...wire.batch, ...EXPORTED };
  await wire.pendingExport!.fulfill({ contentType: "text/csv", body: "employee,amount\nSynthetic payee,60.00\n" });
  await expect.poll(() => wire.pendingReadbacks.length).toBeGreaterThan(0);
  await expectConflictingActionsDisabled(page);
  wire.holdReadback = false;
  for (const route of wire.pendingReadbacks.splice(0)) await route.fulfill(json(detail(wire.batch)));
  await expect(cancel(page)).toBeDisabled();
  await expect(cancel(page)).toHaveAccessibleDescription(/bank.*accountant/i);
  await expect(page.getByRole("button", { name: "Export CSV", exact: true })).toBeEnabled();
  await expect.poll(() => wire.approvedReads).toBeGreaterThan(1);
  expect(wire.exports).toHaveLength(1);
  expect(wire.posts).toBe(0);
});

test("a pending cancellation blocks payment and exports until cancelled readback arrives", async ({ page }) => {
  const wire = await setup(page);
  wire.holdCancel = true;
  wire.holdReadback = true;
  await confirmCancel(page);
  await expect.poll(() => wire.pendingCancel !== undefined).toBe(true);
  await expectConflictingActionsDisabled(page);
  wire.batch = { ...wire.batch, status: "cancelled", version: 2, report_count: 0 };
  await wire.pendingCancel!.fulfill(json(wire.result));
  await expect.poll(() => wire.pendingReadbacks.length).toBeGreaterThan(0);
  await expectConflictingActionsDisabled(page);
  wire.holdReadback = false;
  for (const route of wire.pendingReadbacks.splice(0)) await route.fulfill(json(detail(wire.batch)));
  await expect(page.getByText("cancelled", { exact: true })).toBeVisible();
  await expect(cancel(page)).toHaveCount(0);
  await expect.poll(() => wire.approvedReads).toBeGreaterThan(1);
  expect(wire.posts).toBe(1);
  expect(wire.exports).toHaveLength(0);
});

for (const scenario of [
  { code: "already_exported", button: "Export CSV", path: "export", title: "Re-export bank file?", confirm: "Re-export with a new message id", flag: "confirm_reexport", extension: "csv" },
  { code: "skipped_payees", button: "Export SEPA", path: "sepa", title: "Some employees will not be paid", confirm: "I understand these will not be paid", flag: "acknowledge_skipped", extension: "xml" },
] as const) {
  test(`${scenario.code} warning waits for readback then confirms one flagged export`, async ({ page }) => {
    const wire = await setup(page);
    wire.exportConflict = scenario.code;
    wire.holdReadback = true;
    await page.getByRole("button", { name: scenario.button, exact: true }).click();
    await expect.poll(() => wire.pendingReadbacks.length).toBe(1);
    const warning = page.getByRole("dialog", { name: scenario.title, exact: true });
    await expect(warning).toHaveCount(0);
    await expectConflictingActionsDisabled(page);
    expect(wire.exportCalls).toEqual([{ path: `/reimbursements/${OPEN_BATCH.id}/${scenario.path}`, search: "" }]);
    expect(wire.posts).toBe(0);

    // Publishing this fresh detail releases the row before the existing warning
    // can be confirmed; confirmation must never disappear into a busy no-op.
    expect(wire.batch.id).toBe(OPEN_BATCH.id);
    wire.holdReadback = false;
    await wire.pendingReadbacks.shift()!.fulfill(json(detail(wire.batch)));
    await expect(warning).toBeVisible();
    await expect(warning).toContainText(scenario.code === "already_exported" ? "new file with a new message id" : "Synthetic skipped payee");
    const download = page.waitForEvent("download");
    await warning.getByRole("button", { name: scenario.confirm, exact: true }).click();
    await expect(warning).toBeHidden();
    await expect.poll(() => wire.exportCalls.length).toBe(2);
    expect(wire.exportCalls).toEqual([
      { path: `/reimbursements/${OPEN_BATCH.id}/${scenario.path}`, search: "" },
      { path: `/reimbursements/${OPEN_BATCH.id}/${scenario.path}`, search: `?${scenario.flag}=true` },
    ]);
    expect((await download).suggestedFilename()).toBe(`reimbursement-${OPEN_BATCH.id}.${scenario.extension}`);
    await expect.poll(() => wire.detailReads).toBe(2);
    await expect(page.getByRole("button", { name: scenario.button, exact: true })).toBeEnabled();
    await expect(cancel(page)).toBeDisabled();
    expect(wire.posts).toBe(0);
    expect(wire.exportCalls).toHaveLength(2);
  });
}

test("the blocked batch content stays within its card at 390px with 32px root text", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.addInitScript(() => document.addEventListener("DOMContentLoaded", () => { document.documentElement.style.fontSize = "32px"; }));
  await setup(page, EXPORTED);
  await expect(cancel(page)).toBeDisabled();
  const description = await cancel(page).getAttribute("aria-describedby");
  expect(description).toBeTruthy();
  const help = page.locator(`[id="${description}"]`);
  await expect(help).toBeVisible();
  // Keep this cancellation regression scoped to its payout card. Creation has
  // a separate full-page enlarged-text containment check; no device/a11y certification.
  const bounds = await help.evaluate((element) => {
    const row = element.parentElement!;
    const rect = row.getBoundingClientRect();
    return { width: rect.width, scrollWidth: row.scrollWidth, clientWidth: row.clientWidth, helpRight: element.getBoundingClientRect().right, rowRight: rect.right };
  });
  expect(bounds.scrollWidth).toBeLessThanOrEqual(bounds.clientWidth + 1);
  expect(bounds.helpRight).toBeLessThanOrEqual(bounds.rowRight + 1);
  expect(bounds.width).toBeLessThanOrEqual(390);
});

const SLOT = `invoiceiq:command:${USER.id}:${ORG.id}:/reimbursements/${OPEN_BATCH.id}/cancel`;
const savedCommand = (page: Page) => page.evaluate((slot) => sessionStorage.getItem(slot), SLOT);
const checkCancellation = (page: Page) => page.getByRole("button", { name: "Check cancellation", exact: true });

test("cancellation freezes reviewed versions and an optional reason with one original key", async ({ page }) => {
  const wire = await setup(page);
  await cancel(page).click();
  await expect(confirmation(page)).toBeVisible();
  await expect(confirmation(page)).toContainText("€60.00");
  await confirmation(page).getByRole("textbox", { name: "Cancellation reason (optional)", exact: true }).fill("  Synthetic duplicate selection  ");
  await confirmation(page).getByRole("button", { name: "Cancel batch", exact: true }).click();
  await expect.poll(() => wire.posts).toBe(1);
  await expect.poll(() => savedCommand(page)).toBeNull();
  expect(wire.requests[0]).toMatchObject({ key: expect.any(String), payload: { version: 1, report_versions: { "reimbursement-report": 4 }, reason: "Synthetic duplicate selection" } });
  expect(wire.lookups).toEqual([wire.requests[0].key]);
  expect(wire.deletes).toBe(0);
});

test("a lost cancellation response survives reload and checks the original key using only GET", async ({ page }) => {
  const wire = await setup(page);
  wire.lostResponse = true;
  await confirmCancel(page);
  await expect(checkCancellation(page)).toBeVisible();
  const original = structuredClone(wire.requests[0]);
  expect(await savedCommand(page)).not.toBeNull();
  expect(wire.lookups).toHaveLength(0);
  await page.reload();
  await expect(checkCancellation(page)).toBeVisible();
  await checkCancellation(page).click();
  await expect.poll(() => savedCommand(page)).toBeNull();
  expect(wire.posts).toBe(1);
  expect(wire.requests).toEqual([original]);
  expect(wire.lookups).toEqual([original.key]);
  expect(wire.deletes).toBe(0);
});

test("only an explicit retry after a missing cancellation receipt sends the identical original request", async ({ page }) => {
  const wire = await setup(page);
  wire.lostResponse = true; wire.commitOnFailure = false;
  await confirmCancel(page);
  await expect(checkCancellation(page)).toBeVisible();
  const original = structuredClone(wire.requests[0]);
  await checkCancellation(page).click();
  const retry = page.getByRole("button", { name: "Retry original cancellation", exact: true });
  await expect(retry).toBeVisible();
  await expectConflictingActionsDisabled(page);
  expect(wire.posts).toBe(1);
  wire.lostResponse = false;
  await retry.click();
  await expect.poll(() => savedCommand(page)).toBeNull();
  expect(wire.requests).toEqual([original, original]);
  expect(wire.lookups.every((key) => key === original.key)).toBe(true);
  expect(wire.deletes).toBe(0);
});

for (const mismatch of ["released selection", "child version", "reason"] as const) {
  test(`a cancellation receipt with mismatched ${mismatch} keeps the original command locked`, async ({ page }) => {
    const wire = await setup(page);
    wire.lostResponse = true;
    await confirmCancel(page);
    await expect(checkCancellation(page)).toBeVisible();
    wire.resultPatch = (result) => {
      if (mismatch === "released selection") result.released_reports = [];
      if (mismatch === "child version") result.released_reports[0].version += 1;
      if (mismatch === "reason") result.reason = "Different intent";
      return result;
    };
    await checkCancellation(page).click();
    await expect(page.getByRole("alert").filter({ hasText: /could not be verified/i })).toBeVisible();
    expect(await savedCommand(page)).not.toBeNull();
    expect(wire.posts).toBe(1);
    wire.resultPatch = undefined;
    await checkCancellation(page).click();
    await expect.poll(() => savedCommand(page)).toBeNull();
    expect(wire.posts).toBe(1);
  });
}

test("a native cancellation POST timeout retains one command for explicit GET-only recovery", async ({ page }) => {
  const wire = await setup(page);
  wire.holdCancel = true;
  await confirmCancel(page);
  const failed = page.waitForEvent("requestfailed", {
    predicate: (request) => request.method() === "POST" && new URL(request.url()).pathname === `/api/v1/reimbursements/${OPEN_BATCH.id}/cancel`, timeout: 20_000,
  });
  await expect(checkCancellation(page)).toBeVisible({ timeout: 20_000 });
  expect((await failed).failure()?.errorText).toMatch(/aborted/i);
  expect(wire.posts).toBe(1);
  expect(wire.lookups).toHaveLength(0);
  wire.batch = wire.result!.batch;
  await checkCancellation(page).click();
  await expect.poll(() => savedCommand(page)).toBeNull();
  expect(wire.posts).toBe(1);
});


for (const change of ["version", "export", "payment"] as const) {
  test(`a changed current ${change} refuses before confirmation and any cancellation write`, async ({ page }) => {
    const wire = await setup(page);
    if (change === "version") wire.batch = { ...wire.batch, version: 2 };
    if (change === "export") wire.batch = { ...wire.batch, ...EXPORTED };
    if (change === "payment") wire.batch = { ...wire.batch, status: "paid", paid_at: "2026-11-05T12:00:00Z", version: 2 };
    await cancel(page).click();
    await expect(page.getByRole("alert").filter({ hasText: /changed|refresh/i })).toBeVisible();
    await expect(confirmation(page)).toHaveCount(0);
    expect(wire.posts).toBe(0);
    expect(wire.lookups).toHaveLength(0);
    expect(await savedCommand(page)).toBeNull();
  });
}

test("rapid cancellation confirmation sends one command and keeps competing actions disabled", async ({ page }) => {
  const wire = await setup(page);
  wire.holdCancel = true;
  await cancel(page).evaluate((button) => { (button as HTMLButtonElement).click(); (button as HTMLButtonElement).click(); });
  await expect(confirmation(page)).toBeVisible();
  await confirmation(page).getByRole("button", { name: "Cancel batch", exact: true }).evaluate((button) => { (button as HTMLButtonElement).click(); (button as HTMLButtonElement).click(); });
  await expect.poll(() => wire.posts).toBe(1);
  await expectConflictingActionsDisabled(page);
  wire.batch = wire.result!.batch;
  await wire.pendingCancel!.fulfill(json(wire.result));
  await expect.poll(() => savedCommand(page)).toBeNull();
  expect(wire.posts).toBe(1);
});

test("navigation aborts cancellation continuations and retains the original reference on return", async ({ page }) => {
  const wire = await setup(page);
  wire.holdCancel = true;
  await confirmCancel(page);
  await expect.poll(() => wire.pendingCancel !== undefined).toBe(true);
  await page.goto("/expenses");
  wire.batch = wire.result!.batch;
  await wire.pendingCancel!.fulfill(json(wire.result)).catch(() => undefined);
  await page.goto("/reimbursements");
  await expect(checkCancellation(page)).toBeVisible();
  expect(wire.lookups).toHaveLength(0);
  await checkCancellation(page).click();
  await expect.poll(() => savedCommand(page)).toBeNull();
  expect(wire.posts).toBe(1);
});

test("an unreadable saved cancellation locks its batch without sending recovery or financial writes", async ({ page }) => {
  const wire = await setup(page);
  await page.evaluate((slot) => sessionStorage.setItem(slot, "unreadable synthetic reference"), SLOT);
  await page.reload();
  await expect(page.getByRole("alert").filter({ hasText: /saved cancellation reference is unreadable/i })).toBeVisible();
  await expectConflictingActionsDisabled(page);
  await expect(checkCancellation(page)).toHaveCount(0);
  expect(wire.posts).toBe(0);
  expect(wire.lookups).toHaveLength(0);
});

test("revoked approval permission retains a saved cancellation without sending its lookup or retry", async ({ page }) => {
  const wire = await setup(page);
  wire.lostResponse = true;
  await confirmCancel(page);
  await expect(checkCancellation(page)).toBeVisible();
  await page.route("**/api/v1/auth/me", (route) => route.fulfill(json({ user: { ...USER, role: "user" }, organization: ORG, permissions: ["invoice.read", "expense.read"] })));
  await page.reload();
  await expect(checkCancellation(page)).toBeDisabled();
  expect(wire.lookups).toHaveLength(0);
  expect(wire.posts).toBe(1);
  expect(await savedCommand(page)).not.toBeNull();
});


test("a committed cancellation keeps its reference after current readback fails and refreshes with GET only", async ({ page }) => {
  const wire = await setup(page);
  wire.readbackStatus = 503;
  await confirmCancel(page);
  const refresh = page.getByRole("button", { name: "Refresh cancellation", exact: true });
  await expect(refresh).toBeVisible();
  expect(await savedCommand(page)).not.toBeNull();
  expect(wire.posts).toBe(1);
  wire.readbackStatus = 200;
  await refresh.click();
  await expect.poll(() => savedCommand(page)).toBeNull();
  expect(wire.posts).toBe(1);
  expect(wire.lookups).toEqual([wire.requests[0].key, wire.requests[0].key]);
});

test("a cancellation remains recoverable until the current batch list also refreshes", async ({ page }) => {
  const wire = await setup(page);
  wire.listStatus = 503;
  await confirmCancel(page);
  const refresh = page.getByRole("button", { name: "Refresh cancellation", exact: true });
  await expect(refresh).toBeVisible();
  await expect(refresh).toBeEnabled();
  expect(await savedCommand(page)).not.toBeNull();
  wire.listStatus = 200;
  await refresh.click();
  await expect.poll(() => savedCommand(page)).toBeNull();
  expect(wire.posts).toBe(1);
});

test("saved cancellation recovery stays reachable when its batch is outside the visible page", async ({ page }) => {
  const wire = await setup(page);
  wire.lostResponse = true;
  await confirmCancel(page);
  await expect(checkCancellation(page)).toBeVisible();
  wire.hideBatch = true;
  await page.reload();
  await expect(page.getByRole("group", { name: `Payout batch ${OPEN_BATCH.id}`, exact: true })).toHaveCount(0);
  await checkCancellation(page).click();
  await expect.poll(() => savedCommand(page)).toBeNull();
  expect(wire.posts).toBe(1);
});

test("cancellation review and uncertainty fit their surfaces at desktop and enlarged mobile text", async ({ page }, info) => {
  const wire = await setup(page);
  await cancel(page).click();
  await expect(confirmation(page)).toBeVisible();
  await page.screenshot({ path: info.outputPath("cancellation-review-desktop.png"), animations: "disabled" });
  await confirmation(page).getByRole("button", { name: "Cancel", exact: true }).click();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.evaluate(() => { document.documentElement.style.fontSize = "32px"; });
  await cancel(page).click();
  await expect(confirmation(page)).toBeVisible();
  const bounds = await confirmation(page).evaluate((element) => ({ width: element.getBoundingClientRect().width, scrollWidth: element.scrollWidth, clientWidth: element.clientWidth }));
  expect(bounds.width).toBeLessThanOrEqual(390);
  expect(bounds.scrollWidth).toBeLessThanOrEqual(bounds.clientWidth + 1);
  await page.screenshot({ path: info.outputPath("cancellation-review-mobile-32px.png"), animations: "disabled" });
  wire.lostResponse = true;
  await confirmation(page).getByRole("button", { name: "Cancel batch", exact: true }).click();
  await expect(checkCancellation(page)).toBeVisible();
  const recovery = page.getByRole("group", { name: `Saved cancellation ${OPEN_BATCH.id}`, exact: true });
  const recoveryBounds = await recovery.evaluate((element) => ({ scrollWidth: element.scrollWidth, clientWidth: element.clientWidth }));
  expect(recoveryBounds.scrollWidth).toBeLessThanOrEqual(recoveryBounds.clientWidth + 1);
  await page.screenshot({ path: info.outputPath("cancellation-recovery-mobile-32px.png"), animations: "disabled" });
  expect(wire.posts).toBe(1);
});


test("cancellation can release reviewed foreign reports with unknown FX without imposing a payment total policy", async ({ page }) => {
  const wire = await setup(page);
  wire.reviewedReports[0] = { ...wire.reviewedReports[0], currency: "GBP", total: "500.00", total_eur: null };
  await confirmCancel(page);
  await expect.poll(() => savedCommand(page)).toBeNull();
  await expect(page.getByText("Cancellation saved. The reviewed reports were released from this batch.", { exact: true })).toBeVisible();
  expect(wire.posts).toBe(1);
});

for (const field of ["payment_reference", "reimbursed_at"] as const) {
  test(`contradictory report ${field} evidence refuses before a cancellation write`, async ({ page }) => {
    const wire = await setup(page);
    wire.reviewedReports[0][field] = field === "payment_reference" ? "SYNTHETIC-RECORDED" : "2026-11-05T12:00:00Z";
    await cancel(page).click();
    await expect(page.getByRole("alert").filter({ hasText: /could not be verified/i })).toBeVisible();
    await expect(confirmation(page)).toHaveCount(0);
    expect(wire.posts).toBe(0);
    expect(await savedCommand(page)).toBeNull();
  });
}


test("an empty legacy open batch can cancel its exact reviewed empty report set", async ({ page }) => {
  const wire = await setup(page, { report_count: 0, total_eur: "0.00" });
  await confirmCancel(page);
  await expect(page.getByText("Cancellation saved. The reviewed reports were released from this batch.", { exact: true })).toBeVisible();
  expect(wire.requests).toHaveLength(1);
  expect(wire.requests[0].payload).toEqual({ version: 1, report_versions: {}, reason: null });
  expect(wire.result?.released_reports).toEqual([]);
  expect(await savedCommand(page)).toBeNull();
});

for (const status of [429, 503]) {
  test(`an uncertain cancellation POST ${status} retains its original key until an explicit same-key retry`, async ({ page }) => {
    const wire = await setup(page);
    wire.postStatus = status;
    await confirmCancel(page);
    await expect(checkCancellation(page)).toBeVisible();
    const original = structuredClone(wire.requests[0]);
    expect(await savedCommand(page)).not.toBeNull();
    await expectConflictingActionsDisabled(page);
    expect(wire.posts).toBe(1);
    expect(wire.lookups).toHaveLength(0);
    await checkCancellation(page).click();
    const retry = page.getByRole("button", { name: "Retry original cancellation", exact: true });
    await expect(retry).toBeVisible();
    expect(wire.posts).toBe(1);
    expect(wire.lookups).toEqual([original.key]);
    wire.postStatus = 200;
    await retry.click();
    await expect.poll(() => savedCommand(page)).toBeNull();
    expect(wire.requests).toEqual([original, original]);
    expect(wire.lookups.every((key) => key === original.key)).toBe(true);
    expect(wire.deletes).toBe(0);
  });
}

for (const failure of [429, 503, "offline"] as const) {
  test(`a cancellation receipt lookup ${failure} retains its reference and never repeats the POST`, async ({ page }) => {
    const wire = await setup(page);
    wire.lostResponse = true;
    await confirmCancel(page);
    await expect(checkCancellation(page)).toBeVisible();
    if (failure === "offline") wire.lookupOffline = true;
    else wire.lookupStatus = failure;
    await checkCancellation(page).click();
    await expect(checkCancellation(page)).toBeEnabled();
    expect(await savedCommand(page)).not.toBeNull();
    expect(wire.posts).toBe(1);
    expect(wire.lookups).toEqual([wire.requests[0].key]);
    wire.lookupOffline = false; wire.lookupStatus = 200;
    await checkCancellation(page).click();
    await expect.poll(() => savedCommand(page)).toBeNull();
    expect(wire.posts).toBe(1);
    expect(wire.lookups).toEqual([wire.requests[0].key, wire.requests[0].key]);
  });
}

test("replacement account and organization cannot continue or reveal an old cancellation command", async ({ page, context }) => {
  const wire = await setup(page);
  wire.holdCancel = true;
  await confirmCancel(page);
  await expect.poll(() => wire.pendingCancel !== undefined).toBe(true);
  const original = structuredClone(wire.requests[0]);
  const sender = await context.newPage();
  await sender.route("**/cancellation-identity-sender", (route) => route.fulfill({ contentType: "text/html", body: "<!doctype html><title>Synthetic cancellation identity sender</title>" }));
  await sender.goto("http://localhost:5173/cancellation-identity-sender");
  await sender.evaluate(() => localStorage.setItem("invoiceiq_token", "synthetic-reimbursement-B"));
  await expect(page.getByRole("button", { name: "Account menu", exact: true })).toContainText("Synthetic replacement account");
  wire.batch = wire.result!.batch;
  await wire.pendingCancel!.fulfill(json(wire.result)).catch(() => undefined);
  await expect(checkCancellation(page)).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Refresh cancellation", exact: true })).toHaveCount(0);
  expect(await savedCommand(page)).not.toBeNull();
  expect(wire.lookups).toHaveLength(0);
  expect(wire.scopedRequests.filter((request) => request.account === "B" && (request.method === "POST" || request.path.includes(original.key!)))).toEqual([]);
  await sender.evaluate(() => localStorage.setItem("invoiceiq_token", "synthetic-reimbursement-token"));
  await expect(page.getByRole("button", { name: "Account menu", exact: true })).toContainText(USER.name);
  await expect(checkCancellation(page)).toBeVisible();
  await checkCancellation(page).click();
  await expect.poll(() => savedCommand(page)).toBeNull();
  expect(wire.posts).toBe(1);
  expect(wire.lookups).toEqual([original.key]);
  await sender.close();
});
