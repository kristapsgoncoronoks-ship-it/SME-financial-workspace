import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { writeFileSync } from "node:fs";
import { expect, test } from "@playwright/test";

function uuid(value: string): string {
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value)) {
    throw new Error("Expected a synthetic UUID");
  }
  return value;
}

function sql(statement: string, orgId: string): string {
  const scoped = `SET app.current_org = '${uuid(orgId)}'; ${statement}`;
  return execFileSync("psql", ["-X", "-q", "-v", "ON_ERROR_STOP=1", "-At", "-c", scoped],
    { encoding: "utf8", env: process.env }).trim();
}

function committedState(orgId: string) {
  uuid(orgId);
  const tables = ["reimbursement_batches", "expense_reports", "expense_commands", "audit_events", "jobs", "webhook_deliveries"];
  return Object.fromEntries(tables.map((table) => [table, JSON.parse(sql(
    `SELECT COALESCE(json_agg(t ORDER BY t.id), '[]'::json) FROM ${table} t WHERE t.org_id='${orgId}'`, orgId,
  )) as Array<Record<string, unknown>>]));
}

test("native cancellation recovers a committed lost response through GET only and preserves original-key PG rows", async ({ page }, info) => {
  // The preceding live file's checker login/reload and owner navigation share
  // this IP's production 20/minute auth window. StrictMode can repeat restore
  // reads, so give this independent file a complete fresh window rather than
  // rely on its predecessor's duration or relax the limiter.
  test.setTimeout(test.info().timeout + 61_000);
  await new Promise((resolve) => setTimeout(resolve, 30_500));
  await new Promise((resolve) => setTimeout(resolve, 30_500));
  await expect.poll(async () => {
    try { return (await page.request.get("http://127.0.0.1:8000/health")).status(); }
    catch { return 0; }
  }, { timeout: 30_000 }).toBe(200);

  const email = `ex19-live-${randomUUID()}@example.com`;
  const password = "synthetic-ex19-test-password";
  const registration = await page.request.post("/api/v1/auth/register", {
    data: { organization_name: "Synthetic EX19 native browser", name: "Synthetic cancellation approver", email, password },
  });
  expect(registration.status()).toBe(201);
  const identity = await registration.json();
  const orgId = uuid(identity.organization.id), userId = uuid(identity.user.id);
  const headers = { Authorization: `Bearer ${identity.token.access_token}` };
  expect((await page.request.put("/api/v1/modules/expenses", { headers, data: { enabled: true } })).status()).toBe(200);

  // Approval is deliberately seeded. This journey qualifies cancellation and
  // recovery, and makes no claim about an employee's approval-policy journey.
  const first = uuid(randomUUID()), second = uuid(randomUUID());
  sql(`INSERT INTO expense_reports
    (id,org_id,employee_id,employee_name,title,status,currency,total,vat_total,total_eur,version)
    VALUES
    ('${first}','${orgId}','${userId}','Synthetic payee','Synthetic EX19 parking','approved','EUR',10.00,0,10.00,4),
    ('${second}','${orgId}','${userId}','Synthetic payee','Synthetic EX19 rail','marked_for_reimbursement','EUR',50.00,0,50.00,7)`, orgId);
  const creation = await page.request.post("/api/v1/reimbursements", {
    headers: { ...headers, "Idempotency-Key": randomUUID() },
    data: { report_ids: [first, second], report_versions: { [first]: 4, [second]: 7 }, method: "bank_transfer", note: null },
  });
  expect(creation.status()).toBe(201);
  const batch = await creation.json();
  const batchId = uuid(batch.id);
  expect(batch.report_count).toBe(2);
  const reviewedVersions = Object.fromEntries(batch.reports.map((report: { id: string; version: number }) => [report.id, report.version]));

  // A wildcard subscriber would expose an invented cancellation event. The
  // initial batch creation happened before this synthetic endpoint existed.
  sql(`INSERT INTO webhook_endpoints (id,org_id,url,secret,events,active)
    VALUES ('${randomUUID()}','${orgId}','https://example.invalid/ex19-synthetic','synthetic-test-signing-key','*',true)`, orgId);
  expect(sql(`SELECT count(*) FROM jobs WHERE org_id='${orgId}'`, orgId)).toBe("0");
  expect(sql(`SELECT count(*) FROM webhook_deliveries WHERE org_id='${orgId}'`, orgId)).toBe("0");

  await page.goto("/login?next=/reimbursements");
  await page.getByLabel("Email", { exact: true }).fill(email);
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Reimbursements", exact: true })).toBeVisible();
  const row = page.getByRole("group", { name: `Payout batch ${batchId}`, exact: true });
  await expect(row.getByText("open", { exact: true })).toBeVisible();

  const cancelPath = `/api/v1/reimbursements/${batchId}/cancel`;
  const slot = `invoiceiq:command:${userId}:${orgId}:/reimbursements/${batchId}/cancel`;
  let originalKey = "";
  let originalPayload: unknown;
  let frozenResult: Record<string, unknown> | null = null;
  const requests: Array<{ method: string; path: string; key?: string }> = [];
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (path.startsWith("/api/v1/reimbursements")) requests.push({ method: request.method(), path, key: request.headers()["idempotency-key"] });
  });
  await page.route(`**${cancelPath}`, async (route) => {
    expect(route.request().method()).toBe("POST");
    originalKey = uuid(route.request().headers()["idempotency-key"]);
    originalPayload = route.request().postDataJSON();
    expect(originalPayload).toEqual({ version: batch.version, report_versions: reviewedVersions, reason: "Synthetic duplicate selection" });
    const actual = await route.fetch({ maxRetries: 0 });
    expect(actual.status()).toBe(200);
    frozenResult = await actual.json();
    await route.abort("failed"); // The real server committed; only this response is lost.
  });
  await row.getByRole("button", { name: "Cancel", exact: true }).click();
  const confirmation = page.getByRole("dialog", { name: "Cancel this reimbursement batch?", exact: true });
  await expect(confirmation).toBeVisible();
  await confirmation.getByRole("textbox", { name: "Cancellation reason (optional)", exact: true }).fill("Synthetic duplicate selection");
  await confirmation.getByRole("button", { name: "Cancel batch", exact: true }).click();
  const check = page.getByRole("button", { name: "Check cancellation", exact: true });
  await expect(check).toBeVisible();
  // The saved reference appears while its POST is still in flight. Wait for
  // the real commit and the aborted response to finish before reading PG rows.
  await expect.poll(() => originalKey).not.toBe("");
  await expect.poll(() => frozenResult).not.toBeNull();
  await expect(check).toBeEnabled();
  expect(await page.evaluate((key) => sessionStorage.getItem(key), slot)).not.toBeNull();
  await page.unroute(`**${cancelPath}`);

  const committed = committedState(orgId);
  expect(committed.reimbursement_batches).toHaveLength(1);
  expect(committed.reimbursement_batches[0]).toMatchObject({ id: batchId, status: "cancelled", version: batch.version + 1, export_count: 0, exported_at: null, last_msg_id: null, paid_at: null });
  expect(committed.expense_reports).toHaveLength(2);
  for (const report of committed.expense_reports) {
    expect(report).toMatchObject({ payout_batch_id: null, payment_reference: null, payment_method: null, reimbursed_at: null,
      status: report.id === first ? "approved" : "marked_for_reimbursement", version: reviewedVersions[String(report.id)] + 1 });
  }
  const cancellationReceipts = committed.expense_commands.filter((command) => command.operation === "reimbursement_cancelled");
  expect(cancellationReceipts).toHaveLength(1);
  expect(cancellationReceipts[0]).toMatchObject({ employee_id: userId, command_key: originalKey, target_id: batchId });
  expect(JSON.parse(String(cancellationReceipts[0].result_json))).toEqual(frozenResult);
  const audits = committed.audit_events.filter((event) => event.action === "expense.reimbursement_cancel");
  expect(audits).toHaveLength(1);
  expect(JSON.parse(String(audits[0].meta))).toMatchObject({ command_key: originalKey, reason: "Synthetic duplicate selection" });
  expect(committed.jobs).toEqual([]);
  expect(committed.webhook_deliveries).toEqual([]);

  // Retire the old component, then reload. Neither transition repeats the POST.
  await page.getByRole("link", { name: "← Expenses", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Expenses", exact: true })).toBeVisible();
  await page.goto("/reimbursements");
  await page.reload();
  await expect(check).toBeVisible();
  const recoveryStart = requests.length;
  await check.click();
  await expect.poll(() => page.evaluate((key) => sessionStorage.getItem(key), slot)).toBeNull();
  await expect(page.getByText("Cancellation saved. The reviewed reports were released from this batch.", { exact: true })).toBeVisible();
  expect(requests.slice(recoveryStart).every((request) => request.method === "GET")).toBe(true);
  expect(requests.slice(recoveryStart).some((request) => request.path === `/api/v1/reimbursements/commands/${originalKey}`)).toBe(true);
  expect(requests.filter((request) => request.method === "POST")).toEqual([{ method: "POST", path: cancelPath, key: originalKey }]);
  expect(committedState(orgId)).toEqual(committed);

  const replay = await page.request.post(cancelPath, {
    headers: { ...headers, "Idempotency-Key": originalKey }, data: originalPayload,
  });
  expect(replay.status()).toBe(200);
  expect(await replay.json()).toEqual(frozenResult);
  const lookup = await page.request.get(`/api/v1/reimbursements/commands/${originalKey}`, { headers });
  expect(lookup.status()).toBe(200);
  expect((await lookup.json()).result).toEqual(frozenResult);
  expect(committedState(orgId)).toEqual(committed);
  const evidencePath = info.outputPath("native-ex19-outcome.json");
  writeFileSync(evidencePath, JSON.stringify({
    scope: "Real API/PostgreSQL/browser cancellation; approval state seeded; no bank action",
    org_id: orgId, batch_id: batchId, command_key: originalKey, payload: originalPayload, result: frozenResult,
    browser_requests: requests, committed_rows: committed,
  }, null, 2) + "\n");
  await info.attach("native-ex19-outcome", { contentType: "application/json", path: evidencePath });
});
