import { test, expect, type Page, type Route } from "@playwright/test";

// Keep the first development-loader failure, including its actual Vite asset
// URL and browser state, rather than waiting for a retry to begin tracing.
test.use({ trace: "retain-on-failure", screenshot: "only-on-failure" });

// Synthetic browser routes qualify the action, its frozen request identity and
// visible recovery. Real authorization, persistence and row locks are separately
// proved by API/PostgreSQL tests and the connected acceptance journey.
const ORG = { id: "payment-org-A", name: "Synthetic reimbursement workspace", status: "active" };
const USER = { id: "payment-checker-A", org_id: ORG.id, name: "Synthetic payment checker", role: "admin", email: "checker@example.com" };
const BATCH_ID = "40000000-0000-4000-8000-000000000001";
const OTHER_BATCH_ID = "40000000-0000-4000-8000-000000000002";
const STAMP = "2026-10-27T12:00:00Z";
const SLOT = `invoiceiq:command:${USER.id}:${ORG.id}:/reimbursements/${BATCH_ID}/payment`;
type Evidence = { basis: "operator_recorded" | "legacy_unknown" | "not_recorded" | "unavailable"; bank_confirmation: "unknown"; recorded_by_id: string | null; recorded_at: string | null };
type Report = { id: string; title: string; employee_name: string; total: string; currency: string; total_eur: string | null; status: string; version: number; payment_reference: string | null; reimbursed_at: string | null };
type Batch = {
  id: string; reference: string | null; method: string; status: string; note: null;
  total_eur: string; paid_at: string | null; created_by: string; exported_at: string | null;
  export_count: number; last_msg_id: string | null; version: number; created_at: string;
  report_count: number; payment_evidence: Evidence; reports: Report[];
};
type PayPayload = { version: number; reference: string | null; method?: string | null; override_sod?: boolean; expected_total_eur: string; report_versions: Record<string, number> };
type PayRequest = { key: string | undefined; user: string | undefined; org: string | undefined; path: string; payload: PayPayload };
const openBatch = (id = BATCH_ID): Batch => ({
  id, reference: null, method: "bank_transfer", status: "open", note: null,
  total_eur: "60.00", paid_at: null, created_by: "separate-maker@example.com", exported_at: null,
  export_count: 0, last_msg_id: null, version: 3, created_at: "2026-10-26T12:00:00Z", report_count: 2,
  payment_evidence: { basis: "not_recorded", bank_confirmation: "unknown", recorded_by_id: null, recorded_at: null },
  reports: [
    { id: `50000000-0000-4000-8000-${id === BATCH_ID ? "000000000001" : "000000000003"}`, title: "Synthetic depot parking", employee_name: "Synthetic claimant", total: "10.00", currency: "EUR", total_eur: "10.00", status: "approved", version: 4, payment_reference: null, reimbursed_at: null },
    { id: `50000000-0000-4000-8000-${id === BATCH_ID ? "000000000002" : "000000000004"}`, title: "Synthetic rail fare", employee_name: "Synthetic claimant", total: "50.00", currency: "EUR", total_eur: "50.00", status: "marked_for_reimbursement", version: 7, payment_reference: null, reimbursed_at: null },
  ],
});
const json = (body: unknown, status = 200) => ({ status, contentType: "application/json", body: JSON.stringify(body) });
const record = (page: Page) => page.getByRole("button", { name: "Record payment", exact: true });
const dialog = (page: Page) => page.getByRole("dialog", { name: "Record this payment?", exact: true });
const check = (page: Page) => page.getByRole("button", { name: "Check payment record", exact: true });
const refresh = (page: Page) => page.getByRole("button", { name: "Refresh payment record", exact: true });
const retry = (page: Page) => page.getByRole("button", { name: "Retry original payment request", exact: true });
const paymentReference = (page: Page) => page.getByRole("textbox", { name: "Payment reference", exact: true });
const saved = (page: Page) => page.evaluate((slot) => sessionStorage.getItem(slot), SLOT);
const clone = <T,>(value: T): T => structuredClone(value);
function paidResult(batch: Batch, payload: PayPayload): Batch {
  return {
    ...clone(batch), status: "paid", version: batch.version + 1, paid_at: STAMP,
    reference: payload.reference, method: payload.method || batch.method,
    payment_evidence: { basis: "operator_recorded", bank_confirmation: "unknown", recorded_by_id: USER.id, recorded_at: STAMP },
    reports: batch.reports.map((row) => ({ ...row, status: "reimbursed", version: row.version + 1, payment_reference: payload.reference, reimbursed_at: STAMP })),
  };
}
type Wire = {
  batches: Batch[]; posts: PayRequest[]; deletes: string[]; lookups: string[]; detailReads: string[]; scopedRequests: Array<{ account: string; path: string; method: string }>;
  payStatus: number; payCode: string; payDetail: string; commitOnFailure: boolean; lostResponse: boolean;
  holdPay: boolean; heldPay?: Route; result?: Batch; committed: boolean;
  holdPrepare: boolean; heldPrepare: Route[]; prepareStatus: number; preparePatch?: (batch: Batch) => Batch;
  holdReadback: boolean; heldReadbacks: Route[]; readbackStatus: number;
  lookupStatus: number; lookupOffline: boolean; holdLookup: boolean; heldLookup?: Route;
  receiptPatch?: (receipt: Record<string, unknown>) => Record<string, unknown>;
  cancellationReceipts: Record<string, { batch: Batch; released_reports: Report[]; reason: string | null }>;
};
const wires = new WeakMap<Page, Wire>();
async function setup(page: Page, options: { batches?: Batch[]; holdPrepare?: boolean } = {}) {
  const wire: Wire = {
    batches: clone(options.batches ?? [openBatch()]), posts: [], deletes: [], lookups: [], detailReads: [], scopedRequests: [],
    payStatus: 200, payCode: "reimbursement_payment_conflict", payDetail: "The reimbursement batch changed; refresh and review it.",
    commitOnFailure: true, lostResponse: false, holdPay: false, committed: false,
    holdPrepare: options.holdPrepare ?? false, heldPrepare: [], prepareStatus: 200,
    holdReadback: false, heldReadbacks: [], readbackStatus: 200,
    lookupStatus: 200, lookupOffline: false, holdLookup: false, cancellationReceipts: {},
  };
  wires.set(page, wire);
  await page.addInitScript(() => {
    if (!sessionStorage.getItem("payment-fixture-seeded")) {
      localStorage.setItem("invoiceiq_token", "synthetic-checker-A");
      sessionStorage.setItem("payment-fixture-seeded", "1");
    }
  });
  await page.route("**/api/v1/**", async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    const path = url.pathname.replace("/api/v1", "");
    const account = req.headers().authorization === "Bearer synthetic-checker-B" ? "B" : "A";
    const user = account === "B" ? { ...USER, id: "payment-checker-B", org_id: "payment-org-B", name: "Synthetic second checker" } : USER;
    const org = account === "B" ? { ...ORG, id: "payment-org-B", name: "Synthetic second payment workspace" } : ORG;
    if (path === "/auth/me") return route.fulfill(json({ user, organization: org, permissions: ["invoice.read", "expense.read", "expense.write", "expense.approve"] }));
    if (path === "/auth/organizations") return route.fulfill(json([{ org_id: org.id, name: org.name, current: true, role: user.role }]));
    if (path === "/modules") return route.fulfill(json([{ key: "expenses", name: "Employee expenses", enabled: true, ready: true, requires_issuer: false }]));
    if (path === "/auth/logout") return route.fulfill(json({}));
    if (path.startsWith("/reimbursements")) wire.scopedRequests.push({ account, path, method: req.method() });
    if (path === "/reimbursements/eligible") return route.fulfill(json({ items: [], total: 0, page: 1, page_size: 100 }));
    if (path === "/reimbursements/page" || (path === "/reimbursements" && req.method() === "GET")) {
      const items = account === "B" ? [] : wire.batches.map((batch) => wire.committed && wire.result?.id === batch.id ? wire.result : batch);
      return route.fulfill(json(path === "/reimbursements" ? items : { items, total: items.length, page: 1, page_size: 100 }));
    }
    if (path.startsWith("/reimbursements/commands/")) {
      expect(req.method()).toBe("GET");
      const key = path.slice("/reimbursements/commands/".length);
      if (wire.cancellationReceipts[key]) return route.fulfill(json({ status: "committed", command_key: key,
        operation: "reimbursement_cancelled", target_id: wire.cancellationReceipts[key].batch.id, result: wire.cancellationReceipts[key] }));
      wire.lookups.push(key);
      expect(account).toBe("A");
      expect(req.headers()["x-expected-user-id"]).toBe(USER.id);
      expect(req.headers()["x-expected-org-id"]).toBe(ORG.id);
      if (wire.holdLookup) { wire.heldLookup = route; return; }
      if (wire.lookupOffline) return route.abort("internetdisconnected");
      if (wire.lookupStatus !== 200 || !wire.committed || !wire.result) return route.fulfill(json({ detail: "No committed payment outcome is visible yet." }, wire.lookupStatus === 200 ? 404 : wire.lookupStatus));
      const receipt: Record<string, unknown> = { id: "synthetic-payment-receipt", status: "committed", command_key: key, operation: "reimbursement_paid", target_id: wire.result.id, result: clone(wire.result) };
      return route.fulfill(json(wire.receiptPatch ? wire.receiptPatch(receipt) : receipt));
    }
    const batchId = wire.batches.find((batch) => path === `/reimbursements/${batch.id}` || path === `/reimbursements/${batch.id}/pay` || path === `/reimbursements/${batch.id}/cancel`)?.id;
    if (batchId && path === `/reimbursements/${batchId}/cancel` && req.method() === "POST") {
      wire.deletes.push(batchId); // Existing cross-action assertions count cancellations separately from payment POSTs.
      const batch = wire.batches.find((row) => row.id === batchId)!;
      const released = batch.reports.map((report) => ({ ...report, version: report.version + 1 }));
      batch.status = "cancelled"; batch.version += 1; batch.report_count = 0; batch.reports = [];
      const result = { batch: clone(batch), released_reports: released, reason: req.postDataJSON().reason as string | null };
      wire.cancellationReceipts[req.headers()["idempotency-key"]] = result;
      return route.fulfill(json(result));
    }
    if (batchId && path === `/reimbursements/${batchId}` && req.method() === "GET") {
      wire.detailReads.push(batchId);
      if (!wire.posts.length) {
        if (wire.holdPrepare) { wire.heldPrepare.push(route); return; }
        if (wire.prepareStatus !== 200) return route.fulfill(json({ detail: "Synthetic current batch detail unavailable" }, wire.prepareStatus));
        const batch = clone(wire.batches.find((row) => row.id === batchId)!);
        return route.fulfill(json(wire.preparePatch ? wire.preparePatch(batch) : batch));
      }
      if (wire.holdReadback) { wire.heldReadbacks.push(route); return; }
      if (wire.readbackStatus !== 200) return route.fulfill(json({ detail: "Synthetic payment current readback unavailable" }, wire.readbackStatus));
      return route.fulfill(json(wire.committed && wire.result?.id === batchId ? wire.result : wire.batches.find((batch) => batch.id === batchId)));
    }
    if (batchId && path === `/reimbursements/${batchId}/pay` && req.method() === "POST") {
      const payload = req.postDataJSON() as PayPayload;
      const headers = req.headers();
      wire.posts.push({ path, key: headers["idempotency-key"], user: headers["x-expected-user-id"], org: headers["x-expected-org-id"], payload });
      const batch = wire.batches.find((batch) => batch.id === batchId)!;
      wire.result = paidResult(batch, payload);
      wire.committed = wire.payStatus === 200 && (!wire.lostResponse || wire.commitOnFailure);
      if (wire.holdPay) { wire.heldPay = route; return; }
      if (wire.payStatus !== 200) return route.fulfill(json({ detail: wire.payDetail, code: wire.payCode }, wire.payStatus));
      if (wire.lostResponse) return route.abort("failed");
      return route.fulfill(json(wire.result));
    }
    if (path === "/expenses/summary") return route.fulfill(json({ my_reimbursable: "0", reclaimable_vat: "0", my_submitted: 0, pending_approvals: 0 }));
    if (path === "/expenses") return route.fulfill(json({ items: [], total: 0, page: 1, page_size: 100 }));
    if (path === "/expenses/transactions") return route.fulfill(json([]));
    return route.fulfill(json([]));
  });
  await page.goto("/reimbursements");
  await expect(page.getByRole("heading", { name: "Reimbursements", level: 1, exact: true })).toBeVisible();
  return wire;
}
async function confirmPayment(page: Page) {
  await record(page).click();
  await expect(dialog(page)).toBeVisible();
  await dialog(page).getByRole("button", { name: "Record payment", exact: true }).click();
}
async function expectSameBatchDisabled(page: Page) {
  for (const name of ["Record payment", "Cancel", "Export CSV", "Export SEPA"]) await expect(page.getByRole("button", { name, exact: true })).toBeDisabled();
  await expect(paymentReference(page)).toBeDisabled();
}
test.afterEach(async ({ page }, info) => {
  const wire = wires.get(page);
  if (wire) await info.attach("synthetic-payment-wire", { body: JSON.stringify(wire, (key, value) => key.startsWith("held") ? undefined : value, 2), contentType: "application/json" });
});

test("record payment first reviews exact current detail and explains that it records without transferring", async ({ page }) => {
  const wire = await setup(page);
  await paymentReference(page).fill("SYNTHETIC-OPTIONAL-REF");
  await record(page).click();
  await expect.poll(() => wire.detailReads.length).toBe(1);
  expect(wire.posts).toHaveLength(0);
  await expect(dialog(page)).toBeVisible();
  await expect(dialog(page)).toContainText("€60.00");
  await expect(dialog(page)).toContainText("SYNTHETIC-OPTIONAL-REF");
  await expect(dialog(page)).toContainText(/does not (?:send|transfer)|no money.*sent/i);
  await expect(dialog(page)).toContainText(/bank.*(?:unknown|confirm)|confirm.*bank/i);
  await dialog(page).getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(dialog(page)).toBeHidden();
  expect(wire.posts).toHaveLength(0);
  await expect(record(page)).toBeEnabled();
});

test("one frozen payment request waits for receipt and current readback before releasing its key", async ({ page }) => {
  const wire = await setup(page);
  const original = clone(wire.batches[0]);
  await paymentReference(page).fill("SYNTHETIC-RECORDED-REF");
  wire.holdReadback = true;
  await confirmPayment(page);
  await expect.poll(() => wire.heldReadbacks.length).toBe(1);
  expect(wire.posts).toHaveLength(1);
  const request = wire.posts[0];
  expect(request).toMatchObject({ user: USER.id, org: ORG.id, path: `/reimbursements/${BATCH_ID}/pay`, payload: { version: 3, reference: "SYNTHETIC-RECORDED-REF", expected_total_eur: "60.00", report_versions: Object.fromEntries(original.reports.map((row) => [row.id, row.version])) } });
  expect(request.key).toMatch(/^[A-Za-z0-9:_-]{1,120}$/);
  expect(wire.lookups).toEqual([request.key]);
  expect(await saved(page)).not.toBeNull();
  await expect(page.getByRole("button", { name: "Export CSV", exact: true })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Export SEPA", exact: true })).toBeDisabled();
  wire.holdReadback = false;
  await wire.heldReadbacks.shift()!.fulfill(json(wire.result));
  await expect.poll(() => saved(page)).toBeNull();
  await expect(page.getByText("Operator-recorded payment. Bank confirmation is unknown.", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Export CSV", exact: true })).toBeEnabled();
  expect(wire.posts).toHaveLength(1);
});

test("preparing payment freezes the reference and excludes rapid conflicting actions", async ({ page }) => {
  const wire = await setup(page, { holdPrepare: true });
  await paymentReference(page).fill("SYNTHETIC-FROZEN-REF");
  await record(page).evaluate((button) => { (button as HTMLButtonElement).click(); (button as HTMLButtonElement).click(); });
  await expect.poll(() => wire.heldPrepare.length).toBe(1);
  await expectSameBatchDisabled(page);
  expect(wire.posts).toHaveLength(0);
  wire.holdPrepare = false;
  await wire.heldPrepare.shift()!.fulfill(json(wire.batches[0]));
  await expect(dialog(page)).toContainText("SYNTHETIC-FROZEN-REF");
  wire.holdPay = true;
  await dialog(page).getByRole("button", { name: "Record payment", exact: true }).evaluate((button) => { (button as HTMLButtonElement).click(); (button as HTMLButtonElement).click(); });
  await expect.poll(() => wire.posts.length).toBe(1);
  expect(wire.posts[0].payload.reference).toBe("SYNTHETIC-FROZEN-REF");
  await wire.heldPay!.fulfill(json(wire.result));
  await expect.poll(() => saved(page)).toBeNull();
});

test("a held review reserves shared confirmation ownership across two batches", async ({ page }) => {
  const wire = await setup(page, { batches: [openBatch(), openBatch(OTHER_BATCH_ID)], holdPrepare: true });
  const first = page.getByRole("group", { name: `Payout batch ${BATCH_ID}`, exact: true });
  const second = page.getByRole("group", { name: `Payout batch ${OTHER_BATCH_ID}`, exact: true });
  await first.getByRole("textbox", { name: "Payment reference", exact: true }).fill("SYNTHETIC-FIRST-BATCH-REF");
  await first.getByRole("button", { name: "Record payment", exact: true }).click();
  await expect.poll(() => wire.heldPrepare.length).toBe(1);
  await expect(second.getByRole("button", { name: "Cancel", exact: true })).toBeDisabled();
  await expect(second.getByRole("button", { name: "Record payment", exact: true })).toBeDisabled();
  expect(wire.posts).toHaveLength(0);
  expect(wire.deletes).toHaveLength(0);
  wire.holdPrepare = false;
  await wire.heldPrepare.shift()!.fulfill(json(wire.batches[0]));
  await expect(dialog(page)).toBeVisible();
  await expect(dialog(page)).toContainText("SYNTHETIC-FIRST-BATCH-REF");
  await dialog(page).getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(dialog(page)).toBeHidden();
  await expect(second.getByRole("button", { name: "Cancel", exact: true })).toBeEnabled();
  await second.getByRole("button", { name: "Cancel", exact: true }).click();
  const cancelDialog = page.getByRole("dialog", { name: "Cancel this reimbursement batch?", exact: true });
  await expect(cancelDialog).toBeVisible();
  await cancelDialog.getByRole("button", { name: "Cancel batch", exact: true }).click();
  await expect.poll(() => wire.deletes).toEqual([OTHER_BATCH_ID]);
  await expect(second.getByText("cancelled", { exact: true })).toBeVisible();
  expect(wire.posts).toHaveLength(0);
  await first.getByRole("button", { name: "Record payment", exact: true }).click();
  await expect(dialog(page)).toBeVisible();
  await dialog(page).getByRole("button", { name: "Record payment", exact: true }).click();
  await expect.poll(() => saved(page)).toBeNull();
  await expect(first.getByText("Operator-recorded payment. Bank confirmation is unknown.", { exact: true })).toBeVisible();
  expect(wire.posts).toHaveLength(1);
  expect(wire.posts[0].path).toBe(`/reimbursements/${BATCH_ID}/pay`);
  expect(wire.deletes).toEqual([OTHER_BATCH_ID]);
});

test("a lost payment response uses an explicit GET-only check without another payment POST", async ({ page }) => {
  const wire = await setup(page);
  wire.lostResponse = true;
  await confirmPayment(page);
  await expect(check(page)).toBeVisible();
  await expectSameBatchDisabled(page);
  expect(wire.posts).toHaveLength(1);
  expect(wire.lookups).toHaveLength(0);
  expect(wire.posts[0].payload.reference).toBeNull();
  expect(await saved(page)).not.toBeNull();
  await check(page).click();
  await expect.poll(() => saved(page)).toBeNull();
  expect(wire.posts).toHaveLength(1);
  expect(wire.lookups).toEqual([wire.posts[0].key]);
});

test("a missing receipt retains uncertainty and only an explicit retry reuses the frozen request", async ({ page }) => {
  const wire = await setup(page);
  wire.lostResponse = true;
  wire.commitOnFailure = false;
  await paymentReference(page).fill("SYNTHETIC-RETRY-SAME-INPUTS");
  await confirmPayment(page);
  const original = clone(wire.posts[0]);
  await check(page).click();
  await expect(retry(page)).toBeVisible();
  expect(wire.posts).toHaveLength(1);
  expect(await saved(page)).not.toBeNull();
  await expectSameBatchDisabled(page);
  wire.lostResponse = false;
  await retry(page).click();
  await expect.poll(() => wire.posts.length).toBe(2);
  await expect.poll(() => saved(page)).toBeNull();
  expect(wire.posts[1]).toEqual(original);
  expect(wire.lookups.every((key) => key === original.key)).toBe(true);
});

for (const failure of ["503", "429", "offline"] as const) {
  test(`payment-status failure (${failure}) retains the key and never sends a replacement POST`, async ({ page }) => {
    const wire = await setup(page);
    wire.lostResponse = true;
    await confirmPayment(page);
    wire.lookupStatus = failure === "offline" ? 200 : Number(failure);
    wire.lookupOffline = failure === "offline";
    await check(page).click();
    await expect(check(page)).toBeEnabled();
    expect(await saved(page)).not.toBeNull();
    expect(wire.posts).toHaveLength(1);
    await expect(retry(page)).toHaveCount(0);
    wire.lookupOffline = false;
    wire.lookupStatus = 200;
    await check(page).click();
    await expect.poll(() => saved(page)).toBeNull();
    expect(wire.posts).toHaveLength(1);
  });
}

for (const mismatch of ["operation", "key", "target", "amount", "report_set", "actor", "confirmation"] as const) {
  test(`payment receipt mismatch (${mismatch}) cannot discard the saved key`, async ({ page }) => {
    const wire = await setup(page);
    wire.lostResponse = true;
    await confirmPayment(page);
    wire.receiptPatch = (receipt) => {
      if (mismatch === "operation") return { ...receipt, operation: "reimbursement_created" };
      if (mismatch === "key") return { ...receipt, command_key: "another-synthetic-command" };
      if (mismatch === "target") return { ...receipt, target_id: OTHER_BATCH_ID };
      const value = clone(receipt.result) as Batch;
      if (mismatch === "amount") value.total_eur = "61.00";
      if (mismatch === "report_set") value.reports[0].id = "50000000-0000-4000-8000-000000000009";
      if (mismatch === "actor") value.payment_evidence = { ...value.payment_evidence, recorded_by_id: "another-synthetic-checker" };
      if (mismatch === "confirmation") value.payment_evidence = { ...value.payment_evidence, bank_confirmation: "provider_confirmed" as "unknown" };
      return { ...receipt, result: value };
    };
    await check(page).click();
    await expect(check(page)).toBeEnabled();
    expect(await saved(page)).not.toBeNull();
    expect(wire.posts).toHaveLength(1);
    await expect(retry(page)).toHaveCount(0);
    wire.receiptPatch = undefined;
    await check(page).click();
    await expect.poll(() => saved(page)).toBeNull();
    expect(wire.posts).toHaveLength(1);
  });
}

for (const refusal of [
  { status: 403, code: "maker_is_checker", detail: "You created this batch; a different authorized user must record its payment." },
  { status: 409, code: "reimbursement_payment_conflict", detail: "This reimbursement batch changed. Refresh and review the current version." },
  { status: 409, code: "reimbursement_selection_conflict", detail: "A linked report changed; review the current selection." },
  { status: 409, code: "reimbursement_total_conflict", detail: "The locked reports no longer match the recorded batch total." },
] as const) {
  test(`a definite ${refusal.code} refusal does not record or repeat payment`, async ({ page }) => {
    const wire = await setup(page);
    wire.payStatus = refusal.status;
    wire.payCode = refusal.code;
    wire.payDetail = refusal.detail;
    await confirmPayment(page);
    await expect(page.getByRole("alert").filter({ hasText: refusal.detail })).toBeVisible();
    await expect.poll(() => saved(page)).toBeNull();
    await expect(record(page)).toBeEnabled();
    expect(wire.committed).toBe(false);
    expect(wire.detailReads.length).toBeGreaterThan(1);
    expect(wire.posts).toHaveLength(1);
    expect(wire.lookups).toHaveLength(0);
  });
}

test("a key conflict stays unresolved instead of enabling a new payment intention", async ({ page }) => {
  const wire = await setup(page);
  wire.payStatus = 409;
  wire.payCode = "expense_command_payload_conflict";
  wire.payDetail = "This command key already committed with different inputs.";
  wire.lookupStatus = 409;
  await confirmPayment(page);
  await expect(check(page)).toBeVisible();
  await expectSameBatchDisabled(page);
  expect(await saved(page)).not.toBeNull();
  await check(page).click();
  await expect(check(page)).toBeEnabled();
  expect(await saved(page)).not.toBeNull();
  expect(wire.posts).toHaveLength(1);
  await expect(retry(page)).toHaveCount(0);
});

test("payment readback failure retains its original receipt and offers GET-only recovery", async ({ page }) => {
  const wire = await setup(page);
  wire.readbackStatus = 503;
  await confirmPayment(page);
  await expect(refresh(page)).toBeVisible();
  expect(await saved(page)).not.toBeNull();
  expect(wire.posts).toHaveLength(1);
  const previousReads = wire.detailReads.length;
  wire.readbackStatus = 200;
  await refresh(page).click();
  await expect.poll(() => saved(page)).toBeNull();
  expect(wire.detailReads.length).toBeGreaterThan(previousReads);
  expect(wire.posts).toHaveLength(1);
});

test("payment recovery survives reload with the original key and optional reference", async ({ page }) => {
  const wire = await setup(page);
  wire.lostResponse = true;
  await paymentReference(page).fill("SYNTHETIC-RELOAD-REF");
  await confirmPayment(page);
  const original = clone(wire.posts[0]);
  await expect(check(page)).toBeVisible();
  await page.reload();
  await expect(check(page)).toBeVisible();
  expect(wire.posts).toHaveLength(1);
  await check(page).click();
  await expect.poll(() => saved(page)).toBeNull();
  expect(wire.posts).toEqual([original]);
  expect(wire.lookups.every((key) => key === original.key)).toBe(true);
});

test("navigation retires pending payment continuations but preserves read-only recovery on return", async ({ page }) => {
  const wire = await setup(page);
  wire.holdPay = true;
  await confirmPayment(page);
  await expect.poll(() => wire.heldPay !== undefined).toBe(true);
  await expect.poll(() => saved(page)).not.toBeNull();
  await page.getByRole("link", { name: /Expenses/, exact: false }).filter({ hasText: "← Expenses" }).click();
  await expect(page.getByRole("heading", { name: "Expenses", level: 1, exact: true })).toBeVisible();
  await wire.heldPay!.fulfill(json(wire.result)).catch(() => undefined);
  await page.goto("/reimbursements");
  await expect(check(page)).toBeVisible();
  expect(wire.lookups).toHaveLength(0);
  await check(page).click();
  await expect.poll(() => saved(page)).toBeNull();
  expect(wire.posts).toHaveLength(1);
});

test("a replacement account cannot continue or display the previous account's payment command", async ({ page, context }) => {
  const wire = await setup(page);
  wire.holdPay = true;
  await confirmPayment(page);
  await expect.poll(() => wire.heldPay !== undefined).toBe(true);
  const original = wire.posts[0];
  const sender = await context.newPage();
  await sender.route("**/payment-identity-sender", (route) => route.fulfill({ contentType: "text/html", body: "<!doctype html><title>Synthetic payment identity sender</title>" }));
  await sender.goto("http://localhost:5173/payment-identity-sender");
  await sender.evaluate(() => localStorage.setItem("invoiceiq_token", "synthetic-checker-B"));
  await expect(page.getByRole("button", { name: "Account menu", exact: true })).toContainText("Synthetic second checker");
  await wire.heldPay!.fulfill(json(wire.result)).catch(() => undefined);
  await expect(check(page)).toHaveCount(0);
  await expect(refresh(page)).toHaveCount(0);
  expect(await saved(page)).not.toBeNull();
  expect(wire.posts).toHaveLength(1);
  expect(wire.scopedRequests.filter((request) => request.account === "B" && (request.method === "POST" || request.path.includes(original.key!)))).toEqual([]);
  await sender.close();
});

test("a native payment timeout retains one uncertain command for explicit GET-only recovery", async ({ page }) => {
  const wire = await setup(page);
  wire.holdPay = true;
  await confirmPayment(page);
  const failed = page.waitForEvent("requestfailed", {
    predicate: (request) => request.method() === "POST" && new URL(request.url()).pathname === `/api/v1/reimbursements/${BATCH_ID}/pay`, timeout: 20_000,
  });
  await expect(check(page)).toBeVisible({ timeout: 20_000 });
  expect((await failed).failure()?.errorText).toMatch(/aborted/i);
  expect(wire.posts).toHaveLength(1);
  expect(wire.lookups).toHaveLength(0);
  expect(await saved(page)).not.toBeNull();
  await check(page).click();
  await expect.poll(() => saved(page)).toBeNull();
  expect(wire.posts).toHaveLength(1);
});

test("changed reviewed amounts refuse before confirmation or financial POST", async ({ page }) => {
  const wire = await setup(page);
  wire.preparePatch = (batch) => ({ ...batch, total_eur: "61.00" });
  await record(page).click();
  await expect(page.getByRole("alert").filter({ hasText: /amount|total|changed|verify|refresh/i })).toBeVisible();
  await expect(dialog(page)).toHaveCount(0);
  expect(wire.posts).toHaveLength(0);
  expect(await saved(page)).toBeNull();
  await expect(record(page)).toBeEnabled();
});

test("legacy paid records keep evidence basis and bank confirmation explicitly unknown", async ({ page }) => {
  const legacy = paidResult(openBatch(), { version: 3, reference: null, expected_total_eur: "60.00", report_versions: {} });
  legacy.payment_evidence = { basis: "legacy_unknown", bank_confirmation: "unknown", recorded_by_id: null, recorded_at: null };
  await setup(page, { batches: [legacy] });
  await expect(page.getByText("Legacy payment record; its evidence basis is unknown. Bank confirmation is unknown.", { exact: true })).toBeVisible();
  await expect(record(page)).toHaveCount(0);
  await expect(page.getByText(/provider confirmed|bank confirmed|payment sent/i)).toHaveCount(0);
});

test("payment confirmation and recovery stay contained with enlarged narrow text", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.addInitScript(() => document.addEventListener("DOMContentLoaded", () => { document.documentElement.style.fontSize = "32px"; }));
  const wire = await setup(page);
  await paymentReference(page).fill("SYNTHETIC-" + "R".repeat(190));
  await record(page).click();
  await expect(dialog(page)).toBeVisible();
  const bounds = await dialog(page).evaluate((element) => ({ left: element.getBoundingClientRect().left, right: element.getBoundingClientRect().right, viewport: innerWidth, scroll: element.scrollWidth, client: element.clientWidth }));
  expect(bounds.left).toBeGreaterThanOrEqual(-1);
  expect(bounds.right).toBeLessThanOrEqual(bounds.viewport + 1);
  expect(bounds.scroll).toBeLessThanOrEqual(bounds.client + 1);
  await page.screenshot({ path: test.info().outputPath("payment-confirm-390-root32.png"), fullPage: true });
  wire.lostResponse = true;
  await dialog(page).getByRole("button", { name: "Record payment", exact: true }).click();
  await expect(check(page)).toBeVisible();
  const pageBounds = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, viewport: innerWidth }));
  expect(pageBounds.scroll).toBeLessThanOrEqual(pageBounds.viewport + 1);
  await page.screenshot({ path: test.info().outputPath("payment-recovery-390-root32.png"), fullPage: true });
  expect(wire.posts).toHaveLength(1);
});

// EX17: report-level actions navigate to the canonical payout workflow. These
// mocked journeys do not establish backend authorization or a committed payment.
const EXPENSE_ID = "50000000-0000-4000-8000-000000000017";
type ExpenseDecisionRequest = { path: string; body: { action: string; version: number } };
const expenseWires = new WeakMap<Page, { decisions: ExpenseDecisionRequest[] }>();
async function setupExpenseReport(page: Page, options: { status?: string; own?: boolean; approver?: boolean } = {}) {
  const approver = options.approver !== false;
  const user = { ...USER, is_expense_approver: approver, role: approver ? "admin" : "user" };
  const report = {
    id: EXPENSE_ID, employee_id: options.own ? user.id : "synthetic-claimant-17",
    employee_name: "Synthetic claimant", title: "Synthetic site visit expenses",
    status: options.status ?? "approved", currency: "EUR", total: "60.00", vat_total: "0.00", total_eur: "60.00",
    submitted_at: STAMP, created_at: STAMP, version: 7,
    note: null, decided_at: STAMP, decided_by: "approver@example.com", decision_note: null,
    items: [], policy_violations: [], approval_steps: [],
  };
  const wire = { decisions: [] as ExpenseDecisionRequest[] };
  expenseWires.set(page, wire);
  await page.addInitScript(() => localStorage.setItem("invoiceiq_token", "synthetic-expense-boundary"));
  await page.route("**/api/v1/**", async (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname.replace("/api/v1", "");
    if (path === "/auth/me") return route.fulfill(json({ user, organization: ORG, permissions: ["invoice.read", "expense.read", "expense.write", ...(approver ? ["expense.approve"] : [])] }));
    if (path === "/auth/organizations") return route.fulfill(json([{ org_id: ORG.id, name: ORG.name, current: true, role: user.role }]));
    if (path === "/modules") return route.fulfill(json([{ key: "expenses", name: "Employee expenses", enabled: true, ready: true, requires_issuer: false }]));
    if (path === `/expenses/${EXPENSE_ID}` && req.method() === "GET") return route.fulfill(json(report));
    if (path === `/expenses/${EXPENSE_ID}/decision` && req.method() === "POST") {
      const body = req.postDataJSON() as ExpenseDecisionRequest["body"];
      wire.decisions.push({ path, body });
      if (body.action === "mark_for_reimbursement") {
        report.status = "marked_for_reimbursement";
        report.version += 1;
        return route.fulfill(json(report));
      }
      return route.fulfill(json({ detail: "Use a reimbursement batch to record payment.", code: "reimbursement_batch_required" }, 409));
    }
    if (path === "/reimbursements/page") return route.fulfill(json({ items: [], total: 0, page: 1, page_size: 100 }));
    if (path === "/reimbursements/eligible") return route.fulfill(json({ items: [], total: 0, page: 1, page_size: 100 }));
    return route.fulfill(json([]));
  });
  const browserErrors: string[] = [], consoleErrors: string[] = [];
  const failedReads: { url: string; error: string | null }[] = [], errorResponses: { url: string; status: number; contentType: string | undefined }[] = [];
  page.on("pageerror", error => browserErrors.push(error.message));
  page.on("console", message => { if (message.type() === "error") consoleErrors.push(message.text()); });
  page.on("requestfailed", request => failedReads.push({ url: request.url(), error: request.failure()?.errorText ?? null }));
  page.on("response", response => { if (response.status() >= 400) errorResponses.push({ url: response.url(), status: response.status(), contentType: response.headers()["content-type"] }); });
  await page.goto(`/expenses/${EXPENSE_ID}`);
  try {
    await expect(page.getByRole("heading", { name: report.title, level: 1, exact: true })).toBeVisible();
  } catch (error) {
    await test.info().attach("expense-detail-setup-diagnostics", { body: JSON.stringify({ browserErrors, consoleErrors, failedReads, errorResponses, url: page.url(), body: await page.locator("body").innerText() }), contentType: "application/json" });
    throw error;
  }
  return wire;
}
test.afterEach(async ({ page }, info) => {
  const wire = expenseWires.get(page);
  if (wire) await info.attach("synthetic-expense-terminal-wire", { body: JSON.stringify(wire, null, 2), contentType: "application/json" });
});

for (const status of ["approved", "marked_for_reimbursement"]) {
  test(`EX17 ${status} report uses keyboard navigation to payout batches without a terminal POST`, async ({ page }, info) => {
    const wire = await setupExpenseReport(page, { status });
    await expect(page.getByText(status.replace(/_/g, " "), { exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "Mark reimbursed", exact: true })).toHaveCount(0);
    const link = page.getByRole("link", { name: "Open reimbursements", exact: true });
    await expect(link).toHaveAttribute("href", "/reimbursements");
    await expect(link).toHaveAccessibleDescription("Record payment through a reimbursement batch so its payment record and checks are preserved.");
    // Reach the link through native keyboard order, rather than programmatic
    // focus, then activate it without a report-level payment command.
    for (let step = 0; step < 40 && !(await link.evaluate((element) => element === document.activeElement)); step += 1) await page.keyboard.press("Tab");
    await expect(link).toBeFocused();
    const bounds = await link.boundingBox();
    expect(bounds).not.toBeNull();
    expect(bounds!.width).toBeGreaterThanOrEqual(24);
    expect(bounds!.height).toBeGreaterThanOrEqual(24);
    if (status === "approved") await page.screenshot({ path: info.outputPath("expense-payout-desktop-keyboard.png"), fullPage: true });
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(/\/reimbursements$/);
    await expect(page.getByRole("heading", { name: "Reimbursements", level: 1, exact: true })).toBeVisible();
    expect(wire.decisions).toEqual([]);
  });
}

test("EX17 queue action keeps its reviewed version and reads back without a terminal payment", async ({ page }) => {
  const wire = await setupExpenseReport(page);
  await page.getByRole("button", { name: "Mark for reimbursement", exact: true }).click();
  await expect(page.getByRole("button", { name: "Mark for reimbursement", exact: true })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Open reimbursements", exact: true })).toBeVisible();
  expect(wire.decisions).toEqual([{ path: `/expenses/${EXPENSE_ID}/decision`, body: { action: "mark_for_reimbursement", version: 7 } }]);
  await page.reload();
  await expect(page.getByRole("button", { name: "Mark reimbursed", exact: true })).toHaveCount(0);
  expect(wire.decisions).toHaveLength(1);
});

for (const access of ["own", "nonapprover"]) {
  test(`EX17 ${access} report reader gets truthful guidance without finance progression controls`, async ({ page }) => {
    const wire = await setupExpenseReport(page, { own: access === "own", approver: access !== "nonapprover" });
    await expect(page.getByText("An expense approver records payment through a reimbursement batch.", { exact: true })).toBeVisible();
    await expect(page.getByRole("link", { name: "Open reimbursements", exact: true })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Mark reimbursed", exact: true })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Mark for reimbursement", exact: true })).toHaveCount(0);
    expect(wire.decisions).toEqual([]);
  });
}

test("EX17 historical reimbursed report does not invent a batch receipt or confirmed payment", async ({ page }) => {
  const wire = await setupExpenseReport(page, { status: "reimbursed" });
  await expect(page.getByText("reimbursed", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Mark reimbursed", exact: true })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Open reimbursements", exact: true })).toHaveCount(0);
  await expect(page.getByText(/bank confirmed|provider confirmed|payment sent|payment record and checks are preserved/i)).toHaveCount(0);
  expect(wire.decisions).toEqual([]);
});

test("EX17 payout guidance and link stay contained at narrow enlarged text", async ({ page }, info) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.addInitScript(() => document.addEventListener("DOMContentLoaded", () => { document.documentElement.style.fontSize = "32px"; }));
  const wire = await setupExpenseReport(page, { status: "marked_for_reimbursement" });
  const link = page.getByRole("link", { name: "Open reimbursements", exact: true });
  await expect(link).toBeVisible();
  const bounds = await link.evaluate((element) => ({ left: element.getBoundingClientRect().left, right: element.getBoundingClientRect().right, containerLeft: element.parentElement!.getBoundingClientRect().left, containerRight: element.parentElement!.getBoundingClientRect().right, viewport: innerWidth, fontSize: getComputedStyle(element).fontSize }));
  expect(bounds.left).toBeGreaterThanOrEqual(-1);
  expect(bounds.right).toBeLessThanOrEqual(bounds.viewport + 1);
  expect(bounds.left).toBeGreaterThanOrEqual(bounds.containerLeft - 1);
  expect(bounds.right).toBeLessThanOrEqual(bounds.containerRight + 1);
  expect(bounds.fontSize).toBe("28px");
  const pageBounds = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, viewport: innerWidth }));
  expect(pageBounds.scroll).toBeLessThanOrEqual(pageBounds.viewport + 1);
  await info.attach("expense-payout-layout-metrics", { body: JSON.stringify({ bounds, pageBounds }, null, 2), contentType: "application/json" });
  await page.screenshot({ path: info.outputPath("expense-payout-390-root32.png"), fullPage: true });
  expect(wire.decisions).toEqual([]);
});

for (const [width, fontSize] of [[320, 16], [390, 32], [1280, 16]]) {
  test(`payout facts, actions and shell remain contained at ${width}px/${fontSize}px text`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 844 });
    const longMaker = `synthetic-${"operator".repeat(15)}@example.test`;
    const open = { ...openBatch(), created_by: longMaker };
    const exported = { ...openBatch(OTHER_BATCH_ID), created_by: longMaker, export_count: 1, exported_at: STAMP, last_msg_id: `synthetic-${"message".repeat(20)}` };
    const paid = paidResult(openBatch("40000000-0000-4000-8000-000000000003"), { version: 3, reference: "Synthetic paid reference", expected_total_eur: "60.00", report_versions: {} });
    const wire = await setup(page, { batches: [open, exported, paid] });
    await page.evaluate(size => { document.documentElement.style.fontSize = `${size}px`; }, fontSize);
    await expect(page.getByRole("group", { name: `Payout batch ${BATCH_ID}`, exact: true })).toBeVisible();
    const bounds = await page.evaluate(() => ({ viewport: innerWidth, root: document.documentElement.scrollWidth, body: document.body.scrollWidth, groups: [...document.querySelectorAll('[role="group"][aria-label^="Payout batch"]')].map(e => ({ name: e.getAttribute("aria-label"), width: e.getBoundingClientRect().width, client: e.clientWidth, scroll: e.scrollWidth })) }));
    expect(bounds.root).toBeLessThanOrEqual(width); expect(bounds.body).toBeLessThanOrEqual(width);
    for (const box of bounds.groups) expect(box.scroll).toBeLessThanOrEqual(box.client + 1);
    const openRow = page.getByRole("group", { name: `Payout batch ${BATCH_ID}`, exact: true });
    await expect(openRow).toContainText("€60.00"); await expect(openRow).toContainText("2 reports"); await expect(openRow).toContainText(longMaker);
    const blockedRow = page.getByRole("group", { name: `Payout batch ${OTHER_BATCH_ID}`, exact: true });
    await expect(blockedRow.getByRole("button", { name: "Cancel", exact: true })).toBeDisabled();
    await expect(blockedRow).toContainText("An export does not confirm payment."); await expect(blockedRow).toContainText(exported.last_msg_id);
    await expect(page.getByRole("group", { name: `Payout batch ${paid.id}`, exact: true })).toContainText("Bank confirmation is unknown.");
    const reference = openRow.getByRole("textbox", { name: "Payment reference", exact: true });
    const exact = `SYNTHETIC-${"R".repeat(150)}`;
    await reference.fill(exact); await expect(reference).toHaveValue(exact);
    for (const name of ["Record payment", "Cancel", "Export CSV", "Export SEPA"]) {
      const action = openRow.getByRole("button", { name, exact: true });
      await action.focus(); await page.keyboard.press("Shift+Tab"); await page.keyboard.press("Tab");
      await expect(action).toBeFocused(); await expect(action).toBeInViewport({ ratio: 0.99 });
    }
    const recordAction = openRow.getByRole("button", { name: "Record payment", exact: true });
    await recordAction.focus(); await page.keyboard.press("Enter");
    await expect(dialog(page)).toContainText("€60.00"); await expect(dialog(page)).toContainText(exact);
    await page.keyboard.press("Escape"); await expect(recordAction).toBeFocused();
    const cancelAction = openRow.getByRole("button", { name: "Cancel", exact: true });
    await cancelAction.focus(); await page.keyboard.press("Enter");
    await expect(page.getByRole("dialog", { name: "Cancel this reimbursement batch?", exact: true })).toBeVisible();
    await page.keyboard.press("Escape"); await expect(cancelAction).toBeFocused();
    expect(wire.posts).toEqual([]); expect(wire.deletes).toEqual([]);
    await info.attach("payout-reflow-bounds", { body: JSON.stringify({ bounds, scope: "Synthetic Chromium at the stated viewport/root text sizes, exact facts and native keyboard review; no financial action, device or complete WCAG acceptance." }), contentType: "application/json" });
    await page.screenshot({ path: info.outputPath("payout-reflow.png"), fullPage: true });
  });
}
