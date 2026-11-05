import { expect, test, type Page } from "@playwright/test";

function history(count = 201) {
  return Array.from({ length: count }, (_, index) => ({
    id: `synthetic-run-${index + 1}`, reference: `SYN-RUN-${index + 1}`, method: "bank_transfer",
    status: ["open", "approved", "paid", "cancelled"][index % 4], total_eur: "37.29", note: null,
    paid_at: null, created_by: "synthetic-maker@example.com", approved_by: null, approved_at: null,
    exported_at: null, export_count: 0, last_msg_id: null, version: 1,
    created_at: "2026-05-01T10:00:00Z", invoice_count: index % 3,
  }));
}
async function setup(page: Page, count = 201) {
  const org = { id: "synthetic-history-org", name: "Synthetic run history", status: "active" };
  const wire = {
    rows: history(count), reads: [] as string[], readHeaders: [] as Record<string, string>[],
    writes: [] as string[], bodies: [] as unknown[], failure: 0, emptyPage: false,
    hold: null as null | { wait: Promise<void>; release: () => void },
    identity: { user: { id: "synthetic-reader", role: "owner", org_id: org.id, name: "Synthetic reader", email: "synthetic@example.com" }, organization: org },
  };
  await page.addInitScript(() => localStorage.setItem("invoiceiq_token", "synthetic-history-token"));
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname.replace("/api/v1", "");
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (path.startsWith("/payment-runs") && request.method() !== "GET") {
      wire.writes.push(`${request.method()} ${path}${url.search}`);
      wire.bodies.push(request.postDataJSON());
      if (path.endsWith("/export")) return route.fulfill({ status: 200, contentType: "text/csv", body: "reference,total_eur\nSYN-RUN-201,37.29\n" });
      if (path.endsWith("/pay")) { wire.rows[200] = { ...wire.rows[200], status: "paid", version: wire.rows[200].version + 1 }; return json(wire.rows[200]); }
    }
    if (path === "/auth/me") return json(wire.identity);
    if (path === "/auth/organizations") return json([{ org_id: wire.identity.organization.id, name: wire.identity.organization.name, current: true }]);
    if (path === "/payment-runs/page") {
      wire.reads.push(url.search);
      wire.readHeaders.push(request.headers());
      if (wire.hold) await wire.hold.wait;
      if (wire.failure) return json({ detail: "Synthetic scoped history read failed" }, wire.failure);
      const limit = Number(url.searchParams.get("limit")), offset = Number(url.searchParams.get("offset"));
      return json({ items: wire.emptyPage ? [] : wire.rows.slice(offset, offset + limit), total: wire.rows.length });
    }
    if (path === "/payment-runs/payable") return json([
      { id: "synthetic-payable", invoice_number: "SYN-PAYABLE", vendor_name: "Synthetic supplier", total: "100.00", currency: "EUR", total_eur: "100.00", workflow_state: "scheduled_for_payment" },
    ]);
    return json([]);
  });
  return wire;
}
const nav = (page: Page) => page.getByRole("navigation", { name: "Payment run history pages", exact: true });

test("all201 runs are reachable through bounded history reads without changing payable selection", async ({ page }) => {
  const wire = await setup(page);
  await page.goto("/payment-runs");
  await expect(page.getByRole("heading", { name: "Payment runs", exact: true })).toBeVisible();
  await expect(nav(page)).toContainText("Showing 1–200 of 201 runs");
  await expect(page.getByText(/SYN-RUN-200$/)).toHaveCount(1);
  await expect(page.getByText(/SYN-RUN-201$/)).toHaveCount(0);
  await page.getByRole("checkbox", { name: "Select SYN-PAYABLE", exact: true }).check();
  await expect(page.getByRole("button", { name: /Create run/ })).toContainText("€100.00");
  const next = nav(page).getByRole("button", { name: "Next runs", exact: true });
  await next.focus(); await next.press("Enter");
  await expect(nav(page)).toContainText("Showing 201–201 of 201 runs");
  await expect(page.getByText(/SYN-RUN-201$/)).toHaveCount(1);
  await expect(page.getByText(/SYN-RUN-200$/)).toHaveCount(0);
  await expect(page.getByRole("checkbox", { name: "Select SYN-PAYABLE", exact: true })).toBeChecked();
  await expect(next).toBeDisabled();
  expect(wire.reads.map((query) => new URLSearchParams(query).get("offset"))).toEqual(["0", "200"]);
  expect(wire.reads.every((query) => new URLSearchParams(query).get("limit") === "200")).toBe(true);
  expect(wire.writes).toEqual([]);
});

test("history URL pages survive reload and browser navigation without losing unrelated context", async ({ page }) => {
  const wire = await setup(page);
  await page.goto("/payment-runs?origin=kept");
  await expect(nav(page)).toContainText("Page 1 of 2");
  await nav(page).getByRole("button", { name: "Next runs", exact: true }).click();
  await expect(nav(page)).toContainText("Page 2 of 2");
  await expect(page).toHaveURL(/origin=kept/);
  await page.reload(); await expect(nav(page)).toContainText("Page 2 of 2");
  await page.goBack(); await expect(nav(page)).toContainText("Page 1 of 2");
  await page.goForward(); await expect(nav(page)).toContainText("Page 2 of 2");
  expect(wire.writes).toEqual([]);
});

for (const status of [403, 429, 503]) {
  test(`history ${status} retains the requested page and retries only its GET`, async ({ page }) => {
    const wire = await setup(page); wire.failure = status;
    await page.goto("/payment-runs?runs_page=2");
    await expect(page.getByText("Couldn’t load payment runs", { exact: true })).toBeVisible();
    await expect(page.getByText("No payment runs yet", { exact: true })).toHaveCount(0);
    await expect(page).toHaveURL(/runs_page=2/);
    expect(wire.reads).toHaveLength(1);
    wire.failure = 0;
    await page.getByRole("button", { name: "Try again", exact: true }).click();
    await expect(nav(page)).toContainText("Showing 201–201 of 201 runs");
    expect(wire.reads).toHaveLength(2); expect(wire.reads[0]).toBe(wire.reads[1]);
    expect(wire.writes).toEqual([]);
  });
}

test("a successful smaller population clamps the stale last page, while an empty current page is explicit", async ({ page }) => {
  const wire = await setup(page);
  await page.goto("/payment-runs?runs_page=2");
  await expect(nav(page)).toContainText("Page 2 of 2");
  // Read fixture shrink only; no product deletion or accounting lifecycle claim.
  wire.rows = []; await page.reload();
  await expect(page.getByText("No payment runs yet", { exact: true })).toBeVisible();
  await expect(page).toHaveURL(/runs_page=1/);
  wire.rows = history(); wire.emptyPage = true; await page.reload();
  await expect(page.getByText("This page has no runs.", { exact: false })).toBeVisible();
  await expect(page.getByText("No payment runs yet", { exact: true })).toHaveCount(0);
  wire.emptyPage = false;
  await page.getByRole("button", { name: "Refresh this page", exact: true }).click();
  await expect(nav(page)).toContainText("Showing 1–200 of 201 runs");
  expect(wire.writes).toEqual([]);
});

for (const raw of ["0", "-1", "bad", "9999999999999999999999999"]) {
  test(`invalid history page ${raw} normalizes to a bounded first-page GET`, async ({ page }) => {
    const wire = await setup(page, 1);
    await page.goto(`/payment-runs?runs_page=${raw}`);
    await expect(nav(page)).toContainText("Page 1 of 1");
    await expect(page).toHaveURL(/runs_page=1/);
    expect(new URLSearchParams(wire.reads[0]).get("offset")).toBe("0");
    expect(wire.writes).toEqual([]);
  });
}

for (const [width, textSize] of [[320, 16], [390, 32], [1280, 16]]) {
test(`history controls fit${width}px with${textSize}px text and retain native keyboard activation`, async ({ page }, info) => {
  const wire = await setup(page);
  await page.setViewportSize({ width, height: 844 });
  await page.goto("/payment-runs");
  await expect(nav(page)).toContainText("Page 1 of 2");
  await page.addStyleTag({ content: `html { font-size: ${textSize}px; }` });
  await nav(page).scrollIntoViewIfNeeded();
  const rect = await nav(page).evaluate((element) => ({ width: element.getBoundingClientRect().width, scroll: element.scrollWidth }));
  expect(rect.scroll).toBeLessThanOrEqual(rect.width + 1);
  const next = nav(page).getByRole("button", { name: "Next runs", exact: true });
  await next.focus(); await next.press("Enter");
  await expect(nav(page)).toContainText("Page 2 of 2");
  await page.screenshot({ path: info.outputPath(`history${width}-text${textSize}.png`) });
  expect(wire.writes).toEqual([]);
});
}

test("replacement actor and organization cannot reuse the previous history cache or read scope", async ({ page }) => {
  const wire = await setup(page);
  await page.goto("/payment-runs?runs_page=2");
  await expect(page.getByText(/SYN-RUN-201$/)).toHaveCount(1);
  wire.identity = { user: { ...wire.identity.user, id: "replacement-reader", org_id: "replacement-org" }, organization: { ...wire.identity.organization, id: "replacement-org", name: "Replacement synthetic tenant" } };
  wire.rows = [{ ...wire.rows[0], id: "replacement-run", reference: "ONLY-REPLACEMENT-RUN" }];
  await page.evaluate(() => {
    const oldValue = localStorage.getItem("invoiceiq_token");
    localStorage.setItem("invoiceiq_token", "replacement-history-token");
    window.dispatchEvent(new StorageEvent("storage", { key: "invoiceiq_token", oldValue, newValue: "replacement-history-token", storageArea: localStorage }));
  });
  await expect(page.getByText(/ONLY-REPLACEMENT-RUN$/)).toHaveCount(1);
  await expect(page.getByText(/SYN-RUN-201$/)).toHaveCount(0);
  expect(wire.readHeaders.at(-1)!["x-expected-user-id"]).toBe("replacement-reader");
  expect(wire.readHeaders.at(-1)!["x-expected-org-id"]).toBe("replacement-org");
  expect(wire.writes).toEqual([]);
});

test("a timed-out history read exposes GET-only retry within15 seconds", async ({ page }) => {
  const wire = await setup(page);
  let release = () => {}; const wait = new Promise<void>((resolve) => { release = resolve; });
  wire.hold = { wait, release };
  try {
    await page.goto("/payment-runs?runs_page=2");
    await expect(page.getByText("Couldn’t load payment runs", { exact: true })).toBeVisible({ timeout: 18_000 });
    await expect(page).toHaveURL(/runs_page=2/);
    wire.hold = null; release();
    await page.getByRole("button", { name: "Try again", exact: true }).click();
    await expect(nav(page)).toContainText("Page 2 of 2");
    expect(wire.reads).toHaveLength(2); expect(wire.reads[0]).toBe(wire.reads[1]);
    expect(wire.writes).toEqual([]);
  } finally { release(); }
});

test("export and payment from the last page retain their exact ID and existing payload scope", async ({ page }) => {
  const wire = await setup(page);
  wire.rows[200].status = "paid";
  await page.goto("/payment-runs?runs_page=2");
  await expect(nav(page)).toContainText("Page 2 of 2");
  const downloaded = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export CSV", exact: true }).click();
  await downloaded;
  expect(wire.writes).toEqual(["POST /payment-runs/synthetic-run-201/export"]);
  wire.rows[200] = { ...wire.rows[200], status: "approved", version: 7 };
  await page.reload();
  await page.getByRole("textbox", { name: "Payment reference", exact: true }).fill("EXACT-REFERENCE-201");
  await page.getByRole("button", { name: "Mark paid", exact: true }).click();
  await expect.poll(() => wire.writes.length).toBe(2);
  expect(wire.writes[1]).toBe("POST /payment-runs/synthetic-run-201/pay");
  expect(wire.bodies[1]).toEqual({ version: 7, reference: "EXACT-REFERENCE-201" });
});
