import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { writeFileSync } from "node:fs";
import { expect, test } from "@playwright/test";

function uuid(value: string) {
  if (!/^[\da-f]{8}(?:-[\da-f]{4}){3}-[\da-f]{12}$/i.test(value)) throw new Error("Expected synthetic UUID");
  return value;
}
function sql(orgId: string, statement: string) {
  return execFileSync("psql", ["-X", "-q", "-v", "ON_ERROR_STOP=1", "-At", "-c", `SET app.current_org='${uuid(orgId)}'; ${statement}`], { env: process.env, encoding: "utf8" }).trim();
}
function snapshot(orgId: string) {
  return Object.fromEntries(["payment_runs", "invoices", "supplier_payments", "vendors", "issuer_profiles", "audit_events", "jobs", "webhook_deliveries", "email_messages"].map((table) => [table, JSON.parse(sql(orgId, `SELECT COALESCE(json_agg(t ORDER BY t.id),'[]'::json) FROM ${table} t WHERE t.org_id='${uuid(orgId)}'`)) as Array<Record<string, unknown>>]));
}

test("native AP run history reaches201 exact rows and counts through paging and reload with no financial write", async ({ page }, info) => {
  test.setTimeout(test.info().timeout + 61_000);
  // Preserve the shared production auth window; never disable or bypass it.
  await new Promise((resolve) => setTimeout(resolve, 30_500));
  await new Promise((resolve) => setTimeout(resolve, 30_500));
  const email = `ap-history-live-${randomUUID()}@example.com`, password = "Synthetic-AP-History-Password-32";
  const registration = await page.request.post("/api/v1/auth/register", { data: {
    organization_name: "Synthetic AP run history", name: "Synthetic history reviewer", email, password,
  } });
  expect(registration.status()).toBe(201);
  const identity = await registration.json(), orgId = uuid(identity.organization.id);
  const headers = { Authorization: `Bearer ${identity.token.access_token}` };
  expect((await page.request.put("/api/v1/issuer", { headers, data: { legal_name: "Synthetic AP history entity" } })).status()).toBe(200);
  const ids = Array.from({ length: 201 }, () => uuid(randomUUID())).sort();
  // Declared historical display fixtures; no create/approval/payment journey.
  const values = ids.map((id, index) => `('${id}','${orgId}','SYN-RUN-${String(index + 1).padStart(3, "0")}','bank_transfer','${["open", "approved", "paid", "cancelled"][index % 4]}',37.29,0,1,'2026-05-01T10:00:00Z','2026-05-01T10:00:00Z')`).reverse();
  sql(orgId, `INSERT INTO payment_runs (id,org_id,reference,method,status,total_eur,export_count,version,created_at,updated_at) VALUES ${values.join(",")}`);
  for (let index = 0; index < 3; index++) {
    const invoice = await page.request.post("/api/v1/invoices", { headers, data: {
      vendor_name: "Synthetic history supplier", invoice_number: `SYN-COUNT-${index}`, issue_date: "2026-05-01", currency: "EUR",
      line_items: [{ description: "Synthetic count fixture", quantity: "1", unit_price: "37.29", tax_rate: "0" }],
    } });
    expect(invoice.status()).toBe(201);
    const invoiceId = uuid((await invoice.json()).id);
    sql(orgId, `UPDATE invoices SET payment_run_id='${ids[index === 0 ? 0 : 200]}' WHERE org_id='${orgId}' AND id='${invoiceId}'`);
  }
  const directBefore = snapshot(orgId);
  const first = await page.request.get("/api/v1/payment-runs/page?limit=200&offset=0", { headers });
  expect(first.status()).toBe(200);
  const firstBody = await first.json();
  expect(firstBody.total).toBe(201); expect(firstBody.items).toHaveLength(200);
  expect(firstBody.items.map((row: { id: string }) => row.id)).toEqual(ids.slice(0, 200));
  expect(firstBody.items[0].invoice_count).toBe(1);
  const legacy = await page.request.get("/api/v1/payment-runs?limit=200&offset=200", { headers });
  expect(legacy.status()).toBe(200); expect(Array.isArray(await legacy.json())).toBe(true);
  expect(snapshot(orgId)).toEqual(directBefore);

  await page.goto("/login?next=/payment-runs");
  await page.getByLabel("Email", { exact: true }).fill(email);
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Payment runs", exact: true })).toBeVisible();
  const nav = page.getByRole("navigation", { name: "Payment run history pages", exact: true });
  await expect(nav).toContainText("Showing 1–200 of 201 runs");
  const before = snapshot(orgId), requests: Array<{ method: string; path: string; query: string }> = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname.startsWith("/api/v1/payment-runs")) requests.push({ method: request.method(), path: url.pathname, query: url.search });
  });
  const lastRead = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === "/api/v1/payment-runs/page" && url.searchParams.get("offset") === "200";
  });
  const next = nav.getByRole("button", { name: "Next runs", exact: true });
  await next.focus(); await next.press("Enter");
  const lastBody = await (await lastRead).json();
  expect(lastBody.total).toBe(201); expect(lastBody.items).toHaveLength(1);
  expect(lastBody.items[0]).toMatchObject({ id: ids[200], reference: "SYN-RUN-201", invoice_count: 2, total_eur: "37.29" });
  await expect(nav).toContainText("Showing 201–201 of 201 runs");
  await expect(page.getByText(/SYN-RUN-201$/)).toBeVisible();
  await expect(next).toBeDisabled();
  await page.reload();
  await expect(nav).toContainText("Page 2 of 2");
  await expect(page.getByText(/SYN-RUN-201$/)).toBeVisible();
  expect(snapshot(orgId)).toEqual(before);
  expect(requests.every((request) => request.method === "GET")).toBe(true);
  expect(requests.filter((request) => request.path.endsWith("/page")).every((request) => new URLSearchParams(request.query).get("limit") === "200")).toBe(true);
  const evidence = info.outputPath("native-ap-history-outcome.json");
  writeFileSync(evidence, JSON.stringify({
    scope: "Declared201 historical run fixtures plus three real invoice creations linked by fixture SQL. Actual browser/API paging, keyboard navigation and reload; no product payment/export/approval/cancellation is requested.",
    org_id: orgId, ordered_ids: ids, first_page: firstBody, last_page: lastBody,
    direct_before: directBefore, browser_before: before, browser_after: snapshot(orgId), requests,
  }, null, 2) + "\n");
  await info.attach("native-ap-history-outcome", { path: evidence, contentType: "application/json" });
});
