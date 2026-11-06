import { test, expect, type Page } from "@playwright/test";

const catalog = {
  measures: [{ key: "net", label: "Net spend", unit: "money" }, { key: "lines", label: "Lines", unit: "count" }],
  dimensions: [{ key: "category", label: "Category", temporal: false }, { key: "vendor", label: "Vendor", temporal: false }],
};

async function open(page: Page, options: { count?: number; failQuery?: boolean; failExport?: boolean; beforeExport?: () => Promise<void> } = {}) {
  const requests: URL[] = [];
  let queryFails = options.failQuery ?? false;
  let exportFails = options.failExport ?? false;
  await page.addInitScript(() => localStorage.setItem("invoiceiq_token", "explore-synthetic-token"));
  await page.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname.replace("/api/v1", "");
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (path === "/auth/me") return json({ user: { id: "explore-owner", org_id: "explore-org", role: "owner", name: "Synthetic owner", email: "explore@example.test" }, organization: { id: "explore-org", name: "Synthetic Explore", status: "active" } });
    if (path === "/auth/organizations") return json([{ org_id: "explore-org", name: "Synthetic Explore", current: true }]);
    if (path === "/modules") return json([]);
    if (path === "/analytics/fields") return json(catalog);
    if (path === "/analytics/explore") {
      requests.push(url);
      const format = url.searchParams.get("format");
      if (format) {
        await options.beforeExport?.();
        if (exportFails) return json({ detail: "Synthetic download unavailable" }, 503);
        return route.fulfill({ status: 200, contentType: "text/csv", body: "Category,Net spend\r\ngroup-0001,1.00\r\n" });
      }
      if (queryFails) return json({ detail: "Synthetic query unavailable" }, 503);
      const limit = Number(url.searchParams.get("limit") || 100);
      const count = Math.min(options.count ?? 501, limit);
      const dimensions = url.searchParams.getAll("dim").map((key) => catalog.dimensions.find((dim) => dim.key === key)!);
      return json({ measure: catalog.measures.find((measure) => measure.key === url.searchParams.get("measure")), dimensions,
        currency: url.searchParams.get("measure") === "net" ? url.searchParams.get("currency") : null,
        currency_basis: url.searchParams.get("measure") === "net" ? "native" : "not_applicable",
        rows: Array.from({ length: count }, (_, index) => ({ category: `group-${String(index + 1).padStart(4, "0")}`, vendor: `Synthetic vendor ${index + 1}`, value: String(index + 1) + ".00" })),
        scope: "bounded_groups", limit, returned_rows: count, truncated: (options.count ?? 501) > limit });
    }
    return json({ items: [], total: 0 });
  });
  await page.goto("/explore");
  await page.getByLabel("Native currency", { exact: true }).fill("EUR");
  return { requests, recoverQuery: () => { queryFails = false; }, recoverExport: () => { exportFails = false; } };
}

test("Table is a table, all bounded groups are reachable, and view switches preserve filters and page", async ({ page }) => {
  const { requests } = await open(page);
  await page.getByLabel("Chart", { exact: true }).selectOption("table");
  await expect(page.locator(".recharts-surface")).toHaveCount(0);
  await page.getByLabel("Group limit").selectOption("500");
  await page.getByLabel("Status filter").selectOption("paid");
  await page.getByLabel("From", { exact: true }).fill("2026-01-01");
  await page.getByLabel("Chart", { exact: true }).selectOption("table");
  await expect(page.getByRole("table")).toHaveCount(1);
  await expect(page.locator(".recharts-surface")).toHaveCount(0);
  await expect(page.getByText("Showing 500 groups. More groups match; narrow the filters or increase the group limit.", { exact: true })).toBeVisible();
  await expect(page.getByRole("row")).toHaveCount(101);
  await page.getByRole("button", { name: "Next groups", exact: true }).click();
  await page.getByRole("button", { name: "Next groups", exact: true }).click();
  await expect(page.getByRole("cell", { name: "group-0201", exact: true })).toBeVisible();
  const reads = requests.length;
  await page.getByLabel("Chart", { exact: true }).selectOption("line");
  await expect(page.locator(".recharts-surface")).toHaveCount(1);
  await page.getByLabel("Chart", { exact: true }).selectOption("table");
  await expect(page.getByRole("cell", { name: "group-0201", exact: true })).toBeVisible();
  expect(requests.length).toBe(reads);
  await page.getByRole("button", { name: "Next groups", exact: true }).click();
  await page.getByRole("button", { name: "Next groups", exact: true }).click();
  await expect(page.getByRole("cell", { name: "group-0500", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Next groups", exact: true })).toBeDisabled();
  await expect(page.getByLabel("Status filter")).toHaveValue("paid");
  await expect(page.getByLabel("From", { exact: true })).toHaveValue("2026-01-01");
});

test("all 1000 returned groups appear exactly once across bounded table pages", async ({ page }) => {
  await open(page, { count: 1000 });
  await page.getByLabel("Group limit").selectOption("1000");
  await page.getByLabel("Chart", { exact: true }).selectOption("table");
  await expect(page.getByText("Showing 1000 groups. All matching groups are included in this result.", { exact: true })).toBeVisible();
  const groups: string[] = [];
  for (let index = 0; index < 10; index += 1) {
    await expect(page.getByRole("row")).toHaveCount(101);
    groups.push(...await page.locator("tbody tr td:first-child").allTextContents());
    if (index < 9) await page.getByRole("button", { name: "Next groups", exact: true }).click();
  }
  expect(groups).toEqual(Array.from({ length: 1000 }, (_, index) => `group-${String(index + 1).padStart(4, "0")}`));
  await expect(page.getByRole("button", { name: "Next groups", exact: true })).toBeDisabled();
});

test("the maximum group limit still discloses truncation and offers filter recovery", async ({ page }) => {
  await open(page, { count: 1001 });
  await page.getByLabel("Group limit").selectOption("1000");
  await page.getByLabel("Chart", { exact: true }).selectOption("table");
  await expect(page.getByText("Showing 1000 groups. More groups match; narrow the filters to see another bounded result.", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Download CSV (up to 1000 groups)", exact: true })).toBeEnabled();
});

test("an in-flight download guards repeat activation and preserves its selected filter scope", async ({ page }) => {
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  const fixture = await open(page, { beforeExport: () => gate });
  const button = page.getByRole("button", { name: "Download CSV (up to 100 groups)", exact: true });
  await expect(button).toBeEnabled();
  const download = page.waitForEvent("download");
  await button.evaluate((element) => { (element as HTMLButtonElement).click(); (element as HTMLButtonElement).click(); });
  await expect(page.getByText("Preparing bounded download…", { exact: true })).toBeVisible();
  await expect(page.getByLabel("Group limit")).toBeDisabled();
  await expect(page.getByLabel("Status filter")).toBeDisabled();
  await expect(button).toBeDisabled();
  expect(fixture.requests.filter((url) => url.searchParams.has("format"))).toHaveLength(1);
  release();
  await download;
  await expect(button).toBeEnabled();
  await expect(page.getByLabel("Group limit")).toHaveValue("100");
});

test("bounded downloads preserve the query and offer visible recovery after failure", async ({ page }) => {
  const fixture = await open(page, { failExport: true });
  await page.getByLabel("Group limit").selectOption("1000");
  await page.getByLabel("Break by").selectOption("vendor");
  await page.getByLabel("Status filter").selectOption("paid");
  await expect(page.getByRole("region", { name: "Chart series", exact: true })).toBeVisible();
  await expect(page.getByRole("region", { name: "Chart series", exact: true }).getByRole("listitem")).toHaveCount(501);
  await page.getByRole("region", { name: "Chart series", exact: true }).focus();
  await page.keyboard.press("End");
  await expect(page.getByText("Synthetic vendor 501", { exact: true })).toBeInViewport();
  await expect(page.getByRole("button", { name: "Download CSV (up to 1000 groups)", exact: true })).toBeEnabled();
  await page.getByRole("button", { name: "Download CSV (up to 1000 groups)", exact: true }).click();
  await expect(page.getByRole("alert").filter({ hasText: "Synthetic download unavailable" })).toBeVisible();
  fixture.recoverExport();
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download CSV (up to 1000 groups)", exact: true }).click();
  expect((await download).suggestedFilename()).toBe("explore-EUR-up-to-1000-groups.csv");
  const exported = fixture.requests.filter((url) => url.searchParams.has("format"));
  expect(exported).toHaveLength(2);
  expect(exported[1].searchParams.getAll("dim")).toEqual(["category", "vendor"]);
  expect(exported[1].searchParams.get("status")).toBe("paid");
  expect(exported[1].searchParams.get("limit")).toBe("1000");
  await expect(page.getByText("Downloads rerun these filters on current data, up to the selected group limit.", { exact: true })).toBeVisible();
});

test("query failure recovers through GET and empty results have no export action", async ({ page }) => {
  const fixture = await open(page, { failQuery: true, count: 0 });
  await expect(page.getByText("Couldn’t run this query", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: /Download CSV/ })).toBeDisabled();
  fixture.recoverQuery();
  await page.getByRole("button", { name: "Try again", exact: true }).click();
  await expect(page.getByText("No data for this selection.", { exact: true })).toBeVisible();
  await expect(page.getByRole("table")).toHaveCount(0);
  await expect(page.getByRole("button", { name: /Download CSV/ })).toBeDisabled();
});

for (const rootSize of [16, 32]) {
  test(`Table controls and scope remain reachable at 390px and ${rootSize}px text`, async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.addInitScript((size) => { document.addEventListener("DOMContentLoaded", () => { document.documentElement.style.fontSize = `${size}px`; }); }, rootSize);
    await open(page, { count: 201 });
    await page.getByLabel("Group limit").selectOption("500");
    await page.getByLabel("Chart", { exact: true }).selectOption("table");
    await page.getByRole("button", { name: "Next groups", exact: true }).click();
    await page.getByRole("button", { name: "Next groups", exact: true }).click();
    await expect(page.getByRole("cell", { name: "group-0201", exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Previous groups", exact: true }).focus();
    await expect(page.getByRole("button", { name: "Previous groups", exact: true })).toBeFocused();
    const region = page.getByRole("region", { name: "Explore result table", exact: true });
    if (await region.evaluate((element) => element.scrollWidth > element.clientWidth)) {
      await region.focus();
      for (let index = 0; index < 10; index += 1) await page.keyboard.press("ArrowRight");
      await expect.poll(() => region.evaluate((element) => element.scrollLeft)).toBeGreaterThan(0);
      await expect.poll(() => page.locator("tbody td:last-child").evaluate((element) => {
        const boundary = element.closest('[role="region"]')!.getBoundingClientRect();
        return element.getBoundingClientRect().right <= boundary.right + 1;
      })).toBe(true);
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  });
}
