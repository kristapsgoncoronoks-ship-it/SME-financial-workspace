import { test, expect, type Page } from "@playwright/test";

async function setup(page: Page) {
  const candidateRequests: string[] = [];
  let matched = false;
  const json = (data: unknown) => ({ contentType: "application/json", body: JSON.stringify(data) });
  await page.addInitScript(() => localStorage.setItem("invoiceiq_token", "synthetic-token"));
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname.replace("/api/v1", "");
    const org = { id: "org", name: "Synthetic Workspace", status: "active" };
    if (path === "/auth/me") return route.fulfill(json({ user: { id: "owner", org_id: "org", name: "Owner", email: "owner@example.com", role: "owner" }, organization: org }));
    if (path === "/auth/organizations") return route.fulfill(json([{ org_id: org.id, name: org.name, current: true }]));
    if (path === "/modules") return route.fulfill(json([{ key: "issuing", name: "Issuing", enabled: true }]));
    if (path === "/reconciliation/statements") return route.fulfill(json([{ id: "statement", filename: "synthetic.csv", source_format: "csv", method: "csv", line_count: 3, created_at: "2026-10-14", unmatched: 3, matched: 0, ignored: 0 }]));
    const lines = [
      { id: "usd", statement_id: "statement", version: 1, line_date: "2026-10-14", description: "Foreign credit", amount: "100.00", currency: "USD", direction: "credit", status: "unmatched", matched_kind: null, matched_id: null },
      { id: "eur", statement_id: "statement", version: matched ? 2 : 1, line_date: "2026-10-14", description: "Base debit", amount: "-25.00", currency: "EUR", direction: "debit", status: matched ? "matched" : "unmatched", matched_kind: matched ? "payment_run" : null, matched_id: matched ? "run" : null },
      { id: "unknown", statement_id: "statement", version: 1, line_date: "2026-10-14", description: "Unstated unit", amount: "50.00", currency: null, direction: "credit", status: "unmatched", matched_kind: null, matched_id: null },
    ].map((line) => ({ ...line, amount_status: "valid", balance: null, balance_status: "unprovided" }));
    if (path === "/reconciliation/statements/statement/lines") return route.fulfill(json(lines));
    if (path === "/reconciliation/lines/eur") return route.fulfill(json(lines[1]));
    if (path.endsWith("/candidates")) {
      candidateRequests.push(path);
      return route.fulfill(json([{ kind: "payment_run", id: "run", amount: "25.00", currency: "EUR", date: "2026-10-14", days_off: 0, score: 100 }]));
    }
    if (path === "/reconciliation/lines/eur/commands") {
      expect(route.request().postDataJSON()).toEqual({ operation: "match", expected_version: 1,
        expected: { status: "unmatched", matched_kind: null, matched_id: null }, target: { kind: "payment_run", id: "run" }, reason: null });
      const key = route.request().headers()["idempotency-key"];
      expect(key).toBeTruthy();
      matched = true;
      const before = { status: "unmatched", matched_kind: null, matched_id: null };
      const after = { status: "matched", matched_kind: "payment_run", matched_id: "run" };
      return route.fulfill(json({ contract_version: 1, status: "committed", command_id: "currency-match", command_key: key,
        org_id: "org", actor_user_id: "owner", line_id: "eur", operation: "match", before_version: 1, after_version: 2,
        before, after, line: { ...after, id: "eur", statement_id: "statement", version: 2 }, reason: null,
        payload_hash: "a".repeat(64), result_hash: "b".repeat(64), recorded_at: "2026-11-06T01:00:00Z" }));
    }
    return route.fulfill(json([]));
  });
  await page.goto("/reconciliation");
  await page.getByRole("button", { name: "Reconcile", exact: true }).click();
  return candidateRequests;
}

test("bank lines retain source units and foreign matching explains its disabled action", async ({ page }) => {
  const requests = await setup(page);
  const usd = page.getByRole("row").filter({ hasText: "Foreign credit" });
  await expect(usd).toContainText("USD");
  await expect(usd).not.toContainText("€");
  await expect(usd).toContainText("Reconcile this USD line separately.");
  await expect(usd.getByRole("button", { name: "Find match", exact: true })).toBeDisabled();
  const unknown = page.getByRole("row").filter({ hasText: "Unstated unit" });
  await expect(unknown).toContainText("Currency unknown");
  await expect(unknown).not.toContainText("€");
  await expect(unknown).toContainText("Matching assumes EUR; verify before confirming.");
  expect(requests).toEqual([]);
});

test("EUR candidates show their currency and correct target kind then complete matching", async ({ page }) => {
  const requests = await setup(page);
  const eur = page.getByRole("row").filter({ hasText: "Base debit" });
  await expect(eur).toContainText("EUR");
  await eur.getByRole("button", { name: "Find match", exact: true }).click();
  await expect(eur).toContainText("Supplier payment run");
  await eur.getByRole("button", { name: "Match", exact: true }).click();
  await expect(eur.getByRole("button", { name: "Unmatch", exact: true })).toBeVisible();
  // Matching invalidates reconciliation queries, so a mounted candidate query
  // may refetch. Every request must still belong to the eligible EUR line.
  expect([...new Set(requests)]).toEqual(["/reconciliation/lines/eur/candidates"]);
});

test("an oversized statement explains refusal and the same file can be selected again", async ({ page }) => {
  await setup(page);
  let calls = 0;
  let release!: () => void;
  const pending = new Promise<void>((resolve) => { release = resolve; });
  await page.route("**/api/v1/reconciliation/import", async (route) => {
    calls += 1;
    if (calls === 1) {
      await pending;
      return route.fulfill({ status: 413, contentType: "application/json", body: JSON.stringify({ detail: "File too large (max 1 MB)" }) });
    }
    return route.fulfill({ contentType: "application/json", body: JSON.stringify({ imported: 1, warnings: ["Recovery import completed"] }) });
  });
  const input = page.getByLabel("Statement file");
  const file = { name: "synthetic.csv", mimeType: "text/csv", buffer: Buffer.from("date,description,amount\n2026-10-18,Recovery,-1\n") };
  await input.setInputFiles(file);
  await expect(input).toBeDisabled();
  expect(calls).toBe(1);
  release();
  await expect(page.getByRole("alert")).toContainText("File too large (max 1 MB)");
  await expect(input).toBeEnabled();
  await input.setInputFiles(file);
  await expect(page.getByText("Recovery import completed")).toBeVisible();
  await expect(page.getByRole("alert")).toHaveCount(0);
  expect(calls).toBe(2);
});
