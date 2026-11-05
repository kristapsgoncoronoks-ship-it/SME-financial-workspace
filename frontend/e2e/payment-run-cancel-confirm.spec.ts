import { test, expect, type Page } from "@playwright/test";

/**
 * Payment Runs: "Cancel" requires an explicit ConfirmDialog before firing
 * (WO-39 / R17), over the LIVE app shell with `page.route` mocking — same
 * deterministic, no-backend pattern as `issue-destructive-actions.spec.ts` /
 * `upload-duplicate.spec.ts`.
 *
 * Prior behaviour called `cancel.mutate(r.id)` directly in the button's
 * `onClick`, firing `DELETE /payment-runs/{id}` on a single click with no
 * chance to back out — inconsistent with this exact page's own ConfirmDialog
 * pattern for re-export / skipped-payee acknowledgement. This spec proves the
 * click now opens a styled confirm dialog first, that cancelling the DIALOG
 * issues NO request, and that confirming calls the delete endpoint exactly
 * once.
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

const OPEN_RUN = {
  id: "run-open-1",
  reference: null,
  method: "bank_transfer",
  status: "open",
  note: null,
  total_eur: "1200.00",
  paid_at: null,
  created_by: "owner@test.io",
  approved_by: null,
  approved_at: null,
  exported_at: null as string | null,
  export_count: 0,
  last_msg_id: null as string | null,
  version: 1,
  created_at: "2026-07-20T09:00:00Z",
  invoice_count: 3,
};

interface Wire { calls: string[]; run: typeof OPEN_RUN; reads: number; conflict: boolean; writes: Array<{ method: string; path: string }> }
async function mockApi(page: Page, patch: Partial<typeof OPEN_RUN> = {}): Promise<Wire> {
  await page.addInitScript(() => localStorage.setItem("invoiceiq_token", "e2e-token"));
  const calls: string[] = [];
  const wire: Wire = { calls, run: { ...OPEN_RUN, ...patch }, reads: 0, conflict: false, writes: [] };

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
    if (path.startsWith("/payment-runs") && ["POST", "DELETE", "PUT", "PATCH"].includes(method)) wire.writes.push({ method, path });

    if (path === "/auth/me") return route.fulfill(json({ user: USER, organization: ORG }));
    if (path === "/auth/organizations") return route.fulfill(json([{ org_id: ORG.id, name: ORG.name, current: true }]));
    if (path === "/modules") return route.fulfill(json([]));
    if (path === "/payment-runs/payable" && method === "GET") return route.fulfill(json([]));
    if (path === "/payment-runs" && method === "GET") { wire.reads++; return route.fulfill(json([wire.run])); }
    if (path === `/payment-runs/${OPEN_RUN.id}` && method === "DELETE") {
      calls.push("cancel");
      if (wire.conflict) {
        wire.run = { ...wire.run, export_count: 1, exported_at: "2026-11-05T10:00:00Z" };
        return route.fulfill(json({ code: "run_exported_cannot_cancel", detail: "Run export records prevent cancellation. Check the bank file’s status with your bank or accountant before arranging another payment." }, 409));
      }
      wire.run = { ...wire.run, status: "cancelled", version: wire.run.version + 1 };
      return route.fulfill({ status: 204, contentType: "application/json", body: "" });
    }

    return route.fulfill(json([]));
  });

  return wire;
}

test("cancelling a payment run requires an explicit confirm", async ({ page }) => {
  const { calls } = await mockApi(page);
  await page.goto("/payment-runs");

  await expect(page.getByRole("heading", { level: 1, name: "Payment runs" })).toBeVisible();
  await expect(page.getByText(`${OPEN_RUN.invoice_count} invoices`)).toBeVisible();
  // Only one run is seeded, so exactly one row-level "Cancel" action button exists
  // before the dialog opens (the dialog's own "Cancel" dismiss button appears later,
  // scoped to `dialog` below so the two never get confused).
  const cancelButton = page.getByRole("button", { name: "Cancel", exact: true });
  await expect(cancelButton).toHaveCount(1);

  // Clicking "Cancel" opens the confirm dialog — it must NOT fire the request yet.
  await cancelButton.click();
  const dialog = page.getByRole("dialog", { name: /Cancel this payment run/ });
  await expect(dialog).toBeVisible();
  expect(calls).not.toContain("cancel");

  // Cancelling the DIALOG leaves the run unmodified and issues no request.
  await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(dialog).toBeHidden();
  expect(calls).not.toContain("cancel");

  // Re-open and confirm — exactly one call to the delete endpoint, dialog closes.
  await cancelButton.click();
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "Cancel run" }).click();
  await expect(dialog).toBeHidden();
  // `expect.poll`, not a bare `expect`, and the difference is a real race:
  // `onConfirm` in PaymentRuns.tsx closes the dialog SYNCHRONOUSLY (`setCancelAsk(null)`)
  // and only then calls `cancel.mutate(...)`, so `toBeHidden()` is satisfied BEFORE the
  // request is issued. A plain assertion on `calls` therefore reads the array while the
  // DELETE is still in flight and sees `[]` on a loaded machine. Same idiom as
  // `excise.spec.ts`; the app behaviour is deliberate and unchanged.
  await expect.poll(() => calls.filter((c) => c === "cancel").length).toBe(1);
});

for (const status of ["open", "approved"]) {
  for (const [name, evidence] of [
    ["export count", { export_count: 1 }], ["negative export count", { export_count: -1 }],
    ["export timestamp", { exported_at: "2026-11-05T10:00:00Z" }],
    ["legacy message", { last_msg_id: "AP-SYNTHETIC-LEGACY" }], ["empty nonnull message", { last_msg_id: "" }],
  ] as const) {
    test(`${status} run with ${name} blocks cancellation and explains bank-file disposition`, async ({ page }, testInfo) => {
      if (status === "approved" && name === "export count") await page.setViewportSize({ width: 390, height: 844 });
      const wire = await mockApi(page, { status, ...evidence }); await page.goto("/payment-runs");
      await expect(page.getByRole("heading", { name: "Payment runs", exact: true })).toBeVisible();
      const cancel = page.getByRole("button", { name: "Cancel", exact: true });
      await expect(cancel).toBeDisabled(); await expect(cancel).toHaveAccessibleDescription(/bank.*accountant/i);
      await expect(page.getByText("An export does not confirm payment.", { exact: false })).toBeVisible();
      expect(wire.calls).toEqual([]); expect(wire.writes).toEqual([]);
      await expect(page.getByRole("dialog")).toHaveCount(0);
      if (status === "approved" && name === "export count") {
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
        await page.screenshot({ path: testInfo.outputPath("exported-cancel-blocked-390.png"), fullPage: true });
      }
    });
  }
}
for (const status of ["open", "approved"]) {
  test(`unexported ${status} cancellation preserves Escape dismissal and one explicit DELETE`, async ({ page }) => {
    const wire = await mockApi(page, { status }); await page.goto("/payment-runs");
    const cancel = page.getByRole("button", { name: "Cancel", exact: true }); await cancel.click();
    const dialog = page.getByRole("dialog", { name: "Cancel this payment run?", exact: true }); await expect(dialog).toBeVisible();
    if (status === "approved") await expect(dialog).toContainText("existing approval will be discarded");
    await page.keyboard.press("Escape"); await expect(dialog).toBeHidden(); expect(wire.writes).toEqual([]);
    await cancel.click(); await dialog.getByRole("button", { name: "Cancel run", exact: true }).click();
    await expect.poll(() => wire.calls).toEqual(["cancel"]);
    await expect(page.getByText("cancelled", { exact: true })).toBeVisible();
    expect(wire.writes).toEqual([{ method: "DELETE", path: `/payment-runs/${OPEN_RUN.id}` }]);
  });
}
test("a concurrently recorded export refusal refreshes status with GET and never repeats cancellation", async ({ page }) => {
  const wire = await mockApi(page, { status: "approved" }); wire.conflict = true;
  await page.goto("/payment-runs"); await page.getByRole("button", { name: "Cancel", exact: true }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Cancel run", exact: true }).click();
  await expect(page.getByRole("alert").filter({ hasText: "export records prevent cancellation" })).toBeVisible();
  await expect.poll(() => wire.reads).toBeGreaterThan(1);
  await expect(page.getByRole("button", { name: "Cancel", exact: true })).toBeDisabled();
  expect(wire.calls).toEqual(["cancel"]);
  expect(wire.writes).toEqual([{ method: "DELETE", path: `/payment-runs/${OPEN_RUN.id}` }]);
});
