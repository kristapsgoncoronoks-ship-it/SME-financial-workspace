import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { writeFileSync } from "node:fs";
import { expect, test } from "@playwright/test";

function uuid(value: string): string {
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value)) throw new Error("Expected a synthetic UUID");
  return value;
}
function sql(statement: string, orgId: string): string {
  return execFileSync("psql", ["-X", "-q", "-v", "ON_ERROR_STOP=1", "-At", "-c",
    `SET app.current_org='${uuid(orgId)}'; ${statement}`], { encoding: "utf8", env: process.env }).trim();
}
function snapshot(orgId: string) {
  const tables = ["issued_invoices", "payments", "issuer_profiles", "audit_events", "jobs", "webhook_deliveries", "email_messages"];
  return Object.fromEntries(tables.map((table) => [table, JSON.parse(sql(
    `SELECT COALESCE(json_agg(t ORDER BY t.id), '[]'::json) FROM ${table} t WHERE t.org_id='${uuid(orgId)}'`, orgId,
  )) as Array<Record<string, unknown>>]));
}

test("native issued pagination reaches all105 tied-timestamp rows, reloads and clamps through bounded GETs without domain writes", async ({ page }, info) => {
  // All live files share the real per-IP auth guard. Keep a complete new
  // window without disabling it or changing the client address.
  test.setTimeout(test.info().timeout + 61_000);
  await new Promise((resolve) => setTimeout(resolve, 30_500));
  await new Promise((resolve) => setTimeout(resolve, 30_500));
  const email = `issued-page-live-${randomUUID()}@example.com`, password = "synthetic-pagination-test-password";
  const registration = await page.request.post("/api/v1/auth/register", {
    data: { organization_name: "Synthetic native issued pagination", name: "Synthetic page owner", email, password },
  });
  expect(registration.status()).toBe(201);
  const identity = await registration.json(), orgId = uuid(identity.organization.id);
  const headers = { Authorization: `Bearer ${identity.token.access_token}` };
  const profile = {
    legal_name: "Synthetic page seller", registration_number: "SYN-PAGE-101", vat_number: "NL123456789B01",
    address_line1: "Synthetic lane 1", city: "Amsterdam", postal_code: "1015 CJ", country: "NL",
    iban: "NL91ABNA0417164300", bic: "ABNANL2A", email: "synthetic-seller@example.com",
  };
  const savedIssuer = await page.request.put("/api/v1/issuer", { headers, data: profile });
  expect(savedIssuer.status()).toBe(200);
  const defaultIssuer = await savedIssuer.json(); expect(defaultIssuer.is_complete).toBe(true);
  const issuerId = uuid(defaultIssuer.id);
  expect((await page.request.put("/api/v1/modules/issuing", { headers, data: { enabled: true } })).status()).toBe(200);
  const secondIssuer = await page.request.post("/api/v1/issuer/registry", { headers, data: {
    ...profile, legal_name: "Synthetic second page seller", invoice_prefix: "SP2", credit_note_prefix: "SC2",
  } });
  expect(secondIssuer.status()).toBe(201);
  const secondIssuerId = uuid((await secondIssuer.json()).id);
  const ids = Array.from({ length: 105 }, () => uuid(randomUUID())).sort();
  const numbers = ids.map((_, index) => `LIVE-PAGE-${String(index).padStart(3, "0")}`);
  // Declared historical list fixtures. This is not an invoice issuance or legal
  // numbering journey; no payment, email, PDF rendering or provider is requested.
  const values = ids.map((id, index) => `('${id}','${orgId}','${index % 2 ? secondIssuerId : issuerId}',
    'standard','invoice','issued','${numbers[index]}','2026-05-10','2099-01-01','${index % 2 ? "USD" : "EUR"}',
    'Synthetic page buyer ${index}','{}','standard',37.29,0,37.29,0,0,0,0,'2026-11-05T12:00:00Z','2026-11-05T12:00:00Z')`).reverse();
  sql(`INSERT INTO issued_invoices
    (id,org_id,issuer_id,kind,doc_type,lifecycle,number,issue_date,due_date,currency,buyer_name,seller_json,vat_scheme,
    subtotal,tax_total,total,amount_paid,credited_total,reminder_count,dunning_level,created_at,updated_at)
    VALUES ${values.join(",")}`, orgId);
  const seeded = snapshot(orgId);
  expect(seeded.issued_invoices).toHaveLength(105);
  const original = await page.request.get("/api/v1/issued?page=1&page_size=100", { headers });
  expect(original.status()).toBe(200);
  const originalBody = await original.json();
  expect(originalBody.total).toBe(105);
  expect(originalBody.items.map((row: { id: string }) => row.id)).toEqual(ids.slice(0, 100));
  expect(snapshot(orgId)).toEqual(seeded);

  const firstRead = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === "/api/v1/issued" && url.searchParams.get("page") === "1" && response.request().method() === "GET";
  });
  await page.goto("/login?next=/issue");
  await page.getByLabel("Email", { exact: true }).fill(email);
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  expect((await firstRead).status()).toBe(200);
  await expect(page.getByRole("heading", { name: "Issued (105)", exact: true })).toBeVisible();
  expect(await page.getByText(/^LIVE-PAGE-\d{3}$/).allTextContents()).toEqual(numbers.slice(0, 100));
  const beforePaging = snapshot(orgId);
  const requests: Array<{ method: string; path: string; query: string }> = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname.startsWith("/api/v1/issued")) requests.push({ method: request.method(), path: url.pathname, query: url.search });
  });
  const pages = page.getByRole("navigation", { name: "Issued invoice pages", exact: true });
  const secondRead = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === "/api/v1/issued" && url.searchParams.get("page") === "2";
  });
  await pages.getByRole("button", { name: "Next page", exact: true }).click();
  const secondBody = await (await secondRead).json();
  expect(secondBody.total).toBe(105);
  expect(secondBody.items.map((row: { id: string }) => row.id)).toEqual(ids.slice(100));
  expect(secondBody.items.every((row: { total: string; currency: string; status: string }) => row.total === "37.29" && ["EUR", "USD"].includes(row.currency) && row.status === "open")).toBe(true);
  await expect(pages).toContainText("Showing 101–105 of 105");
  expect(await page.getByText(/^LIVE-PAGE-\d{3}$/).allTextContents()).toEqual(numbers.slice(100));
  await expect(page).toHaveURL(/page=2/);
  await page.reload();
  await expect(pages).toContainText("Page 2 of 2");
  expect(await page.getByText(/^LIVE-PAGE-\d{3}$/).allTextContents()).toEqual(numbers.slice(100));
  expect(snapshot(orgId)).toEqual(beforePaging);
  expect(requests.every((request) => request.method === "GET")).toBe(true);
  expect(requests.filter((request) => request.path === "/api/v1/issued").every((request) => new URLSearchParams(request.query).get("page_size") === "100")).toBe(true);
  const afterReadOnlyPaging = snapshot(orgId);

  // Explicit fixture mutation represents a changed list between visits. It does
  // not claim the product permits deleting final legal documents.
  sql(`DELETE FROM issued_invoices WHERE org_id='${orgId}' AND id IN (${ids.slice(100).map((id) => `'${uuid(id)}'`).join(",")})`, orgId);
  const afterFixtureShrink = snapshot(orgId);
  const clampStart = requests.length;
  await page.reload();
  await expect(pages).toContainText("Page 1 of 1");
  await expect(pages).toContainText("Showing 1–100 of 100");
  await expect(page).toHaveURL(/page=1/);
  expect(await page.getByText(/^LIVE-PAGE-\d{3}$/).allTextContents()).toEqual(numbers.slice(0, 100));
  expect(requests.slice(clampStart).filter((request) => request.path === "/api/v1/issued").map((request) => new URLSearchParams(request.query).get("page"))).toEqual(["2", "1"]);
  expect(snapshot(orgId)).toEqual(afterFixtureShrink);
  expect(requests.every((request) => request.method === "GET")).toBe(true);
  expect(afterFixtureShrink.jobs).toEqual([]); expect(afterFixtureShrink.webhook_deliveries).toEqual([]);
  const evidence = info.outputPath("native-issued-pagination-outcome.json");
  writeFileSync(evidence, JSON.stringify({
    scope: "Real PostgreSQL seeded historical105-row list, real browser login, real API bounded pages/reload/clamp. No issuance/payment/provider action. Controlled synthetic fixture shrink explicitly separated from GET-only invariance.",
    org_id: orgId, ordered_ids: ids, ordered_numbers: numbers, first_api_page: originalBody, second_api_page: secondBody,
    browser_requests: requests, before_read_only_paging: beforePaging, after_read_only_paging: afterReadOnlyPaging,
    after_explicit_fixture_shrink: afterFixtureShrink, final: snapshot(orgId),
  }, null, 2) + "\n");
  await info.attach("native-issued-pagination-outcome", { path: evidence, contentType: "application/json" });
});
