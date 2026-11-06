import { test, expect, type Page } from "@playwright/test";

// Synthetic API contracts exercise real UI values/fences, not FX or live data.
const catalog = {
  measures: [
    { key: "net", label: "Net spend", unit: "money" },
    { key: "tax", label: "Tax", unit: "money" },
    { key: "gross", label: "Gross spend", unit: "money" },
    { key: "quantity", label: "Quantity", unit: "number" },
    { key: "lines", label: "Line items", unit: "count" },
    { key: "invoices", label: "Invoices", unit: "count" },
  ],
  dimensions: [{ key: "category", label: "Category", temporal: false }, { key: "vendor", label: "Vendor", temporal: false }],
};

async function open(page: Page) {
  const requests: URL[] = [];
  const methods: string[] = [];
  let refusal = false, empty = false, mismatched = false;
  let beforeRead: ((url: URL) => Promise<void>) | undefined;
  await page.addInitScript(() => localStorage.setItem("invoiceiq_token", "explore-currency-synthetic"));
  await page.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname.replace("/api/v1", "");
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (path === "/auth/me") return json({ user: { id: "currency-owner", org_id: "currency-org", role: "owner", name: "Synthetic owner", email: "currency@example.test" }, organization: { id: "currency-org", name: "Synthetic currency fixture", status: "active" } });
    if (path === "/auth/organizations") return json([{ org_id: "currency-org", name: "Synthetic currency fixture", current: true }]);
    if (path === "/modules") return json([]);
    if (path === "/analytics/fields") return json(catalog);
    if (path === "/analytics/explore") {
      requests.push(url); methods.push(route.request().method());
      await beforeRead?.(url);
      const measure = catalog.measures.find((item) => item.key === url.searchParams.get("measure"))!;
      const code = url.searchParams.get("currency");
      if (refusal) return json({ detail: "Synthetic currency refusal", code: "explore_currency_invalid" }, 422);
      if (measure.unit === "money" && !code) return json({ detail: "Choose a native currency.", code: "explore_currency_required" }, 422);
      if (url.searchParams.has("format")) {
        if (measure.unit === "money" && url.searchParams.get("export_contract") !== "2") return json({ detail: "Use the currency-labelled export contract.", code: "explore_export_contract_required" }, 422);
        return route.fulfill({ status: 200, contentType: "text/csv", body: `Category,${measure.label}${measure.unit === "money" ? ` (${code})` : ""}\r\nSynthetic services,1200.00\r\n` });
      }
      const dimensions = url.searchParams.getAll("dim").map((key) => catalog.dimensions.find((item) => item.key === key)!);
      return json({ measure, dimensions, rows: empty ? [] : [{ category: "Synthetic services", vendor: "Synthetic supplier", value: measure.unit === "number" ? "3.25" : measure.unit === "count" ? "3" : "1200.00" }], scope: "bounded_groups", limit: 100, returned_rows: empty ? 0 : 1, truncated: false,
        currency: measure.unit === "money" ? mismatched ? "EUR" : code : null, currency_basis: measure.unit === "money" ? "native" : "not_applicable" });
    }
    return json({ items: [], total: 0 });
  });
  await page.goto("/explore");
  await expect(page.getByRole("heading", { name: "Explore", exact: true })).toBeVisible();
  return { requests, methods, refuse: (value: boolean) => { refusal = value; }, setEmpty: () => { empty = true; }, mismatch: () => { mismatched = true; }, hold: (callback: typeof beforeRead) => { beforeRead = callback; } };
}

const currency = (page: Page) => page.getByLabel("Native currency", { exact: true });
const csv = (page: Page) => page.getByRole("button", { name: "Download CSV (up to 100 groups)", exact: true });
const usd = (page: Page) => page.getByRole("cell", { name: /US\$1,200\.00|\$1,200\.00/ });

test("money waits for an explicit native currency and never preselects EUR", async ({ page }) => {
  const fixture = await open(page);
  await expect(currency(page)).toHaveValue("");
  await expect(page.getByText("Choose a native currency to run a money query.", { exact: true })).toBeVisible();
  await expect(csv(page)).toBeDisabled();
  await expect(page.getByRole("table")).toHaveCount(0);
  expect(fixture.requests).toHaveLength(0);
  for (const invalid of ["US", "U1D", "ßAA", " USD"]) {
    await currency(page).fill(invalid);
    await expect(currency(page)).toHaveAttribute("aria-invalid", "true");
    await expect(page.getByText("Enter a three-letter currency code, such as USD.", { exact: true })).toBeVisible();
    expect(fixture.requests).toHaveLength(0);
  }
});

for (const measure of ["net", "tax", "gross"]) {
  test(`${measure} uses the selected native USD unit in Table and versioned download`, async ({ page }, info) => {
    const fixture = await open(page);
    await page.getByLabel("Measure", { exact: true }).selectOption(measure);
    await page.getByLabel("Chart", { exact: true }).selectOption("table");
    await currency(page).fill("usd");
    await expect(currency(page)).toHaveValue("USD");
    await expect(usd(page)).toBeVisible();
    await expect(page.getByRole("columnheader").last()).toContainText("(USD)");
    await expect(page.getByText("Native amounts in USD. No currency conversion is applied.", { exact: true })).toBeVisible();
    if (measure === "net") await page.screenshot({ path: info.outputPath("explore-usd-table-desktop.png"), fullPage: true });
    expect(fixture.requests.at(-1)!.searchParams.get("currency")).toBe("USD");
    const downloaded = page.waitForEvent("download");
    await csv(page).click();
    expect((await downloaded).suggestedFilename()).toBe("explore-USD-up-to-100-groups.csv");
    const exported = fixture.requests.at(-1)!;
    expect(exported.searchParams.get("currency")).toBe("USD");
    expect(exported.searchParams.get("export_contract")).toBe("2");
    expect(exported.searchParams.get("measure")).toBe(measure);
    expect(fixture.methods.every((method) => method === "GET")).toBe(true);
  });
}

for (const measure of ["quantity", "lines", "invoices"]) {
  test(`${measure} works without currency using its existing nonmoney unit`, async ({ page }) => {
    const fixture = await open(page);
    await page.getByLabel("Measure", { exact: true }).selectOption(measure);
    await page.getByLabel("Chart", { exact: true }).selectOption("table");
    await expect(page.getByRole("cell", { name: measure === "quantity" ? "3.25" : "3", exact: true })).toBeVisible();
    await expect(csv(page)).toBeEnabled();
    expect(fixture.requests.at(-1)!.searchParams.has("currency")).toBe(false);
    const downloaded = page.waitForEvent("download");
    await csv(page).click();
    expect((await downloaded).suggestedFilename()).toBe("explore-up-to-100-groups.csv");
    expect(fixture.methods.every((method) => method === "GET")).toBe(true);
  });
}

test("server currency refusal retains selection and recovers with an explicit GET", async ({ page }) => {
  const fixture = await open(page);
  fixture.refuse(true);
  await currency(page).fill("USD");
  const alert = page.getByRole("alert").filter({ hasText: "Couldn’t run this query" });
  await expect(alert).toContainText("Check the native currency code or try this query again.");
  await expect(currency(page)).toHaveValue("USD");
  await expect(csv(page)).toBeDisabled();
  fixture.refuse(false); fixture.setEmpty();
  await alert.getByRole("button", { name: "Try again", exact: true }).click();
  await expect(page.getByText("No data for this selection.", { exact: true })).toBeVisible();
  await expect(page.getByText("Try another currency or adjust the filters.", { exact: true })).toBeVisible();
  await expect(page.getByRole("table")).toHaveCount(0);
  await expect(csv(page)).toBeDisabled();
  // No currency registry/default is inferred from an empty native cut.
  await currency(page).fill("ZZZ");
  await expect(page.getByText("Native amounts in ZZZ. No currency conversion is applied.", { exact: true })).toBeVisible();
  await expect(page.getByText("No data for this selection.", { exact: true })).toBeVisible();
  expect(fixture.requests.at(-1)!.searchParams.get("currency")).toBe("ZZZ");
  expect(fixture.methods.every((method) => method === "GET")).toBe(true);
});

test("clearing, invalidating and changing currency fence cached and in-flight money results", async ({ page }) => {
  const fixture = await open(page);
  await currency(page).fill("EUR");
  await expect(page.getByRole("cell", { name: "€1,200.00", exact: true })).toBeVisible();
  await expect(csv(page)).toBeEnabled();
  await currency(page).fill("");
  await expect(page.getByText("Choose a native currency to run a money query.", { exact: true })).toBeVisible();
  await expect(csv(page)).toBeDisabled();
  await expect(page.getByRole("table")).toHaveCount(0);
  await currency(page).fill("U1D");
  await expect(csv(page)).toBeDisabled();
  const readCount = fixture.requests.length;
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  fixture.hold((url) => url.searchParams.get("currency") === "USD" ? gate : Promise.resolve());
  await currency(page).fill("USD");
  await expect.poll(() => fixture.requests.length).toBeGreaterThan(readCount);
  await expect(csv(page)).toBeDisabled();
  await expect(page.getByRole("table")).toHaveCount(0);
  await currency(page).fill("JPY");
  await expect(page.getByRole("cell", { name: /JP¥1,200|¥1,200/ })).toBeVisible();
  release();
  await expect(csv(page)).toBeEnabled();
  await expect(page.getByRole("cell", { name: "€1,200.00", exact: true })).toHaveCount(0);
  await expect(usd(page)).toHaveCount(0);
  expect(fixture.requests.every((url) => !url.searchParams.has("currency") || /^[A-Z]{3}$/.test(url.searchParams.get("currency")!))).toBe(true);
});

test("mismatched currency metadata cannot render money or enable download", async ({ page }) => {
  const fixture = await open(page);
  fixture.mismatch();
  await currency(page).fill("USD");
  await expect(page.getByText("The result currency could not be verified. Try this query again.", { exact: true })).toBeVisible();
  await expect(csv(page)).toBeDisabled();
  await expect(page.getByRole("table")).toHaveCount(0);
});

test("USD is retained in bar, line, pie and stacked values and meaningful axes", async ({ page }) => {
  await open(page);
  await currency(page).fill("USD");
  await expect(usd(page)).toBeVisible();
  for (const chart of ["bar", "line", "pie", "stacked"]) {
    await page.getByLabel("Chart", { exact: true }).selectOption(chart);
    if (chart === "stacked") await page.getByLabel("Break by", { exact: true }).selectOption("vendor");
    await expect(page.locator(".recharts-surface")).toBeVisible();
    // Recharts renders tick text outside its structural yAxis group.
    if (chart !== "pie") await expect(page.getByRole("application").getByText(/^(?:US)?\$1\.2K$/)).toBeVisible();
    if (chart === "pie") {
      await expect(page.locator(".recharts-pie-sector path")).toBeVisible();
      // A full donut's bounding-box center is its empty hole. Point at the
      // visible ring rather than forcing a hover through the SVG background.
      const bounds = (await page.locator(".recharts-surface").boundingBox())!;
      await page.mouse.move(bounds.x + bounds.width / 2 + 80, bounds.y + bounds.height / 2);
    } else {
      await (chart === "line" ? page.locator(".recharts-line-dot").first() : page.locator(".recharts-bar-rectangle").first()).hover();
    }
    await expect(page.locator(".recharts-tooltip-wrapper")).toContainText(/US\$1,200\.00|\$1,200\.00/);
    await expect(page.locator(".recharts-tooltip-wrapper")).not.toContainText("€");
  }
});

test("currency validation and table stay contained at 390px with 32px root text", async ({ page }, info) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.addInitScript(() => document.addEventListener("DOMContentLoaded", () => { document.documentElement.style.fontSize = "32px"; }));
  await open(page);
  await currency(page).fill("U1D");
  await expect(currency(page)).toHaveAttribute("aria-invalid", "true");
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await currency(page).fill("USD");
  await page.getByLabel("Chart", { exact: true }).selectOption("table");
  await expect(usd(page)).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.screenshot({ path: info.outputPath("explore-usd-390-root32.png"), fullPage: true });
});
