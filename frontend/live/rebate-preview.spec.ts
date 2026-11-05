import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { writeFileSync } from "node:fs";
import { expect, test } from "@playwright/test";

function uuid(value: string): string {
  if (!/^[\da-f]{8}(?:-[\da-f]{4}){3}-[\da-f]{12}$/i.test(value)) throw new Error("Expected a synthetic UUID");
  return value;
}
function sql(statement: string, orgId: string): string {
  return execFileSync("psql", ["-X", "-q", "-v", "ON_ERROR_STOP=1", "-At", "-c",
    `SET app.current_org='${uuid(orgId)}'; ${statement}`], { encoding: "utf8", env: process.env }).trim();
}
function snapshot(orgId: string) {
  const tables = ["fuel_transactions", "vat_off_invoice_rebates", "audit_events", "jobs", "webhook_deliveries", "invoices", "issuer_profiles", "vat_refund_claims", "vat_claim_lines", "vat_claimed_invoices", "vat_fee_rates", "email_messages", "org_modules"];
  return Object.fromEntries(tables.map((table) => [table, JSON.parse(sql(
    `SELECT COALESCE(json_agg(t ORDER BY t.id), '[]'::json) FROM ${table} t WHERE t.org_id='${uuid(orgId)}'`, orgId,
  )) as Array<Record<string, unknown>>]));
}

test("native read-only rebate preview plans every entity and country before paging and preserves exact rows through reload and refusal", async ({ page }, info) => {
  // Preserve the production per-IP auth budget shared by the live files.
  test.setTimeout(test.info().timeout + 61_000);
  await new Promise((resolve) => setTimeout(resolve, 30_500));
  await new Promise((resolve) => setTimeout(resolve, 30_500));
  const email = `rebate-preview-live-${randomUUID()}@example.com`, password = "synthetic-rebate-preview-password";
  const registration = await page.request.post("/api/v1/auth/register", { data: {
    organization_name: "Synthetic native rebate preview", name: "Synthetic rebate reviewer", email, password,
  } });
  expect(registration.status()).toBe(201);
  const identity = await registration.json(), orgId = uuid(identity.organization.id);
  const headers = { Authorization: `Bearer ${identity.token.access_token}` };
  // Commercial plan placement remains deliberately undecided. Preserve its real
  // refusal; declare only this synthetic tenant's fixture prerequisite directly.
  expect((await page.request.put("/api/v1/modules/transport", { headers, data: { enabled: true } })).status()).toBe(402);
  sql(`INSERT INTO org_modules (id,org_id,key,enabled) VALUES ('${uuid(randomUUID())}','${orgId}','transport',true)
    ON CONFLICT (org_id,key) DO UPDATE SET enabled=true`, orgId);
  const firstIssuer = await page.request.put("/api/v1/issuer", { headers, data: { legal_name: "Synthetic preview entity A" } });
  expect(firstIssuer.status()).toBe(200);
  const entityA = uuid((await firstIssuer.json()).id);
  const secondIssuer = await page.request.post("/api/v1/issuer/registry", { headers, data: {
    legal_name: "Synthetic preview entity B", invoice_prefix: "PRB", credit_note_prefix: "PCB",
  } });
  expect(secondIssuer.status()).toBe(201);
  const entityB = uuid((await secondIssuer.json()).id);
  const rows = Array.from({ length: 105 }, (_, index) => ({
    id: uuid(randomUUID()), entity: index % 2 ? entityB : entityA,
    sequence: Math.floor(index / 2) + 1, country: index < 2 ? "EE" : "LV",
  }));
  const ordered = [...rows].sort((a, b) => a.country.localeCompare(b.country) || a.sequence - b.sequence || a.id.localeCompare(b.id));
  const warningId = uuid(randomUUID()), historicalId = uuid(randomUUID()), refusedId = uuid(randomUUID());
  const rowValue = (id: string, entity: string, supplier: string, period: string, sequence: number, country: string, net: string, effective = net) => `('${uuid(id)}','${orgId}','${uuid(entity)}','${supplier}','${period}',${sequence},'${country}','SYNTHETIC-VEHICLE','${period}-12','','Synthetic preview station','DIESEL','Diesel',1.000,'EUR',${net},0,${net},${net},0,${effective},'eur')`;
  // Declared validated fixtures, not an ingestion/approval journey. Equal line
  // sequences across entities make the canonical ID tie-break observable.
  sql(`INSERT INTO fuel_transactions
    (id,org_id,entity_id,supplier,period,line_seq,country,vehicle_ref,txn_date,txn_time,station,product,product_group,qty,currency,net_local,vat_local,gross_local,net_eur,vat_eur,net_eur_eff,fx_source)
    VALUES ${[
      ...[...ordered].reverse().map((row) => rowValue(row.id, row.entity, "Q8", "2026-05", row.sequence, row.country, "10.00")),
      rowValue(warningId, entityA, "MISSINGNET", "2026-05", 1, "LV", "10.00"),
      rowValue(historicalId, entityA, "Q8", "2026-04", 1, "LV", "99.00", "88.00"),
      rowValue(refusedId, entityA, "Q8", "2026-06", 1, "LV", "1.00"),
    ].join(",")}`, orgId);
  const record = async (period: string, country: string, supplier: string, amount: string, reference: string) => {
    const response = await page.request.post("/api/v1/transport/rebates", { headers, data: {
      supplier, country, period, source_ref: reference, source_party: "Synthetic rebate document issuer",
      rebate_date: "2026-06-10", currency: "EUR", amount_local: amount,
    } });
    expect(response.status()).toBe(200);
    const body = await response.json(); expect(body.amount_eur).toBe(amount); expect(body.fx_source).toBe("eur");
  };
  // Country totals give exactly one cent per eligible litre without client-side
  // allocation. A prior-period source supplies a genuine missing-source warning.
  await record("2026-05", "EE", "Q8", "0.02", "SYNTH-EE-REBATE");
  await record("2026-05", "LV", "Q8", "1.03", "SYNTH-LV-REBATE");
  await record("2026-04", "LV", "MISSINGNET", "0.25", "SYNTH-PRIOR-SOURCE");
  await record("2026-06", "LV", "Q8", "1.00", "SYNTH-REFUSED-PLAN");
  const seeded = snapshot(orgId);
  const apiFirst = await page.request.get("/api/v1/transport/rebates/preview?period=2026-05&page=1&page_size=100", { headers });
  expect(apiFirst.status()).toBe(200);
  const firstBody = await apiFirst.json();
  expect(firstBody).toMatchObject({ period: "2026-05", supplier: null, rebate_total_eur: "1.05", lines_allocated: 105, changed: 105, page: 1, page_size: 100 });
  expect(firstBody.allocations.map((row: { fuel_transaction_id: string }) => row.fuel_transaction_id)).toEqual(ordered.slice(0, 100).map((row) => row.id));
  expect(firstBody.source_warnings).toHaveLength(1); expect(firstBody.source_warnings[0]).toContain("MISSINGNET/LV");
  expect(snapshot(orgId)).toEqual(seeded);

  await page.goto("/login?next=/rebates");
  await page.getByLabel("Email", { exact: true }).fill(email);
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  const form = page.getByRole("form", { name: "Rebate preview", exact: true });
  await expect(form).toBeVisible();
  const requests: Array<{ method: string; path: string; query: string }> = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname.startsWith("/api/v1/transport/")) requests.push({ method: request.method(), path: url.pathname, query: url.search });
  });
  const beforeBrowserPreview = snapshot(orgId);
  await form.getByRole("textbox", { name: "Preview period", exact: true }).fill("2026-05");
  await form.getByRole("button", { name: "Preview rebate effect", exact: true }).click();
  const table = page.getByRole("table", { name: "Read-only rebate allocations", exact: true });
  await expect(table.getByRole("row")).toHaveCount(101);
  await expect(page.getByText("Would change: 105", { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Missing-source warnings (1)", exact: true })).toBeVisible();
  const pages = page.getByRole("navigation", { name: "Rebate allocation pages", exact: true });
  const nextRead = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === "/api/v1/transport/rebates/preview" && url.searchParams.get("page") === "2";
  });
  await pages.getByRole("button", { name: "Next allocations", exact: true }).click();
  const secondBody = await (await nextRead).json();
  expect(secondBody.allocations.map((row: { fuel_transaction_id: string }) => row.fuel_transaction_id)).toEqual(ordered.slice(100).map((row) => row.id));
  expect(secondBody.allocations.every((row: { net_eur: string; share_eur: string; old_net_eur_eff: string; new_net_eur_eff: string }) => row.net_eur === "10.00" && row.old_net_eur_eff === "10.00" && row.share_eur === "0.01" && row.new_net_eur_eff === "9.99")).toBe(true);
  expect(secondBody.rebate_total_eur).toBe("1.05"); expect(secondBody.changed).toBe(105);
  await expect(table.getByRole("row")).toHaveCount(6);
  await expect(pages).toContainText("Showing 101–105 of 105 allocations");
  await page.reload();
  await expect(pages).toContainText("Page 2 of 2");
  await expect(table.getByRole("row")).toHaveCount(6);
  expect(snapshot(orgId)).toEqual(beforeBrowserPreview);

  await form.getByRole("textbox", { name: "Preview period", exact: true }).fill("2026-06");
  await expect(table).toHaveCount(0);
  await form.getByRole("button", { name: "Preview rebate effect", exact: true }).click();
  await expect(page.getByText("Preview unavailable", { exact: true })).toBeVisible();
  await expect(table).toHaveCount(0);
  const refused = await page.request.get("/api/v1/transport/rebates/preview?period=2026-06&page=1&page_size=100", { headers });
  expect(refused.status()).toBe(422); expect((await refused.json()).code).toBe("rebate_exceeds_net");
  expect(snapshot(orgId)).toEqual(beforeBrowserPreview);
  expect(requests.every((request) => request.method === "GET")).toBe(true);
  expect(requests.filter((request) => request.path.endsWith("/preview")).every((request) => new URLSearchParams(request.query).get("page_size") === "100")).toBe(true);
  expect(seeded.jobs).toEqual([]); expect(seeded.webhook_deliveries).toEqual([]);
  const evidence = info.outputPath("native-rebate-preview-outcome.json");
  writeFileSync(evidence, JSON.stringify({
    scope: "Declared validated fuel fixtures across two entities/countries; real API source recording and real browser/API GET-only canonical preview, paging, reload and whole-plan refusal. No close, approval or provider action is claimed.",
    org_id: orgId, ordered_ids: ordered.map((row) => row.id), first_api_page: firstBody, second_api_page: secondBody,
    browser_requests: requests, before_direct_api_read: seeded, before_browser_preview: beforeBrowserPreview, final: snapshot(orgId),
  }, null, 2) + "\n");
  await info.attach("native-rebate-preview-outcome", { path: evidence, contentType: "application/json" });
});
