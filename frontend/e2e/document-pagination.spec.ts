import { expect, test, type Page } from "@playwright/test";

async function setup(page: Page) {
  const organization = { id: "synthetic-doc-org", name: "Synthetic registry", status: "active" };
  const user = { id: "synthetic-doc-reader", name: "Synthetic reader", email: "reader@example.com", role: "owner", org_id: organization.id };
  const wire = {
    rows: Array.from({ length: 203 }, (_, i) => ({
      id: `doc-${i + 1}`, sha256: String(i).padStart(64, "0"), size: 52431, mime: "application/pdf",
      kind: i % 2 ? "receipts" : "logos", filename: `synthetic-document-${i + 1}.pdf`,
      uploaded_by: user.email, created_at: "2026-11-05T10:00:00Z",
    })),
    reads: [] as string[], writes: [] as string[], bytes: [] as number[], failure: 0, offline: false,
    malformed: false, emptyPage: false, hold: null as null | { wait: Promise<void>; release: () => void },
  };
  await page.addInitScript(() => localStorage.setItem("invoiceiq_token", "synthetic-doc-token"));
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname.replace("/api/v1", "");
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (request.method() !== "GET") wire.writes.push(`${request.method()} ${path}`);
    if (path === "/auth/me") return json({ user, organization });
    if (path === "/auth/organizations") return json([{ org_id: organization.id, name: organization.name, role: user.role, current: true }]);
    if (path === "/documents/page") {
      wire.reads.push(url.search);
      if (wire.hold) await wire.hold.wait;
      if (wire.offline) return route.abort("internetdisconnected");
      if (wire.failure) return json({ detail: "Synthetic registry failure" }, wire.failure);
      if (wire.malformed) return json([]);
      const kind = url.searchParams.get("kind"), offset = Number(url.searchParams.get("offset")), limit = Number(url.searchParams.get("limit"));
      const filtered = wire.rows.filter((row) => !kind || row.kind === kind);
      const body = { items: wire.emptyPage ? [] : filtered.slice(offset, offset + limit), total: filtered.length, kinds: Array.from(new Set(wire.rows.map((row) => row.kind))).sort() };
      wire.bytes.push(Buffer.byteLength(JSON.stringify(body))); return json(body);
    }
    if (path === "/documents") throw new Error("The bounded registry must not request the legacy entire population");
    return json([]);
  });
  return wire;
}
const pages = (page: Page) => page.getByRole("navigation", { name: "Document registry pages", exact: true });

test("all203 originals are reachable with at most100 rendered rows and no writes", async ({ page }) => {
  const wire = await setup(page);
  await page.goto("/documents?origin=kept");
  await expect(pages(page)).toContainText("Showing 1–100 of 203 documents");
  await expect(page.locator("tbody tr")).toHaveCount(100);
  const next = pages(page).getByRole("button", { name: "Next documents", exact: true });
  await next.focus(); await next.press("Enter");
  await expect(pages(page)).toContainText("Showing 101–200 of 203 documents");
  await expect(page.getByRole("cell", { name: "synthetic-document-101.pdf", exact: true })).toBeVisible();
  await next.click();
  await expect(pages(page)).toContainText("Showing 201–203 of 203 documents");
  await expect(page.locator("tbody tr")).toHaveCount(3); await expect(next).toBeDisabled();
  await expect(page.getByRole("cell", { name: "synthetic-document-203.pdf", exact: true })).toBeVisible();
  expect(wire.reads.map((query) => new URLSearchParams(query).get("offset"))).toEqual(["0", "100", "200"]);
  expect(wire.reads.every((query) => new URLSearchParams(query).get("limit") === "100")).toBe(true);
  expect(wire.writes).toEqual([]);
});

test("kind filtering resets the page and preserves every available kind", async ({ page }) => {
  const wire = await setup(page);
  await page.goto("/documents?documents_page=2&origin=kept");
  await expect(pages(page)).toContainText("Page 2 of 3");
  await page.getByLabel("Kind", { exact: true }).selectOption("logos");
  await expect(pages(page)).toContainText("Showing 1–100 of 102 documents");
  await expect(page).toHaveURL(/documents_page=1/); await expect(page).toHaveURL(/origin=kept/);
  await expect(page.getByLabel("Kind").locator("option")).toHaveText(["All kinds", "logos", "receipts"]);
  await page.getByLabel("Kind", { exact: true }).selectOption("receipts");
  await expect(pages(page)).toContainText("Showing 1–100 of 101 documents");
  await expect(page.getByRole("cell", { name: "synthetic-document-1.pdf", exact: true })).toHaveCount(0);
  expect(wire.writes).toEqual([]);
});

test("registry URL, kind and unrelated context survive reload and browser history", async ({ page }) => {
  const wire = await setup(page);
  await page.goto("/documents?kind=logos&origin=kept");
  await expect(pages(page)).toContainText("Page 1 of 2");
  await pages(page).getByRole("button", { name: "Next documents", exact: true }).click();
  await expect(pages(page)).toContainText("Page 2 of 2");
  await page.reload(); await expect(pages(page)).toContainText("Showing 101–102 of 102 documents");
  await expect(page.getByLabel("Kind")).toHaveValue("logos");
  await page.goBack(); await expect(pages(page)).toContainText("Page 1 of 2");
  await page.goForward(); await expect(pages(page)).toContainText("Page 2 of 2");
  await expect(page).toHaveURL(/origin=kept/); expect(wire.writes).toEqual([]);
});

for (const failure of [403, 429, 503, "offline", "malformed"] as const) {
  test(`registry ${failure} is recoverable with only the requested GET`, async ({ page }) => {
    const wire = await setup(page);
    if (typeof failure === "number") wire.failure = failure;
    if (failure === "offline") wire.offline = true;
    if (failure === "malformed") wire.malformed = true;
    await page.goto("/documents?documents_page=3");
    await expect(page.getByText("Couldn’t load the document registry", { exact: true })).toBeVisible();
    await expect(page.getByText("No documents yet", { exact: true })).toHaveCount(0);
    await expect(pages(page).getByRole("button", { name: "Next documents", exact: true })).toBeDisabled();
    expect(wire.reads).toHaveLength(1);
    wire.failure = 0; wire.offline = false; wire.malformed = false;
    await page.getByRole("button", { name: "Try again", exact: true }).click();
    await expect(pages(page)).toContainText("Showing 201–203 of 203 documents");
    expect(wire.reads).toHaveLength(2); expect(wire.reads[1]).toBe(wire.reads[0]); expect(wire.writes).toEqual([]);
  });
}

for (const raw of ["0", "-1", "1.5", "text", "999999999999999999999"]) {
  test(`invalid registry page ${raw} uses and normalizes page1`, async ({ page }) => {
    const wire = await setup(page);
    await page.goto(`/documents?documents_page=${raw}&origin=kept`);
    await expect(pages(page)).toContainText("Showing 1–100 of 203 documents");
    await expect(page).toHaveURL(/documents_page=1/); await expect(page).toHaveURL(/origin=kept/);
    expect(wire.reads.every((query) => new URLSearchParams(query).get("offset") === "0")).toBe(true);
  });
}

test("smaller current population clamps stale URLs", async ({ page }) => {
  const wire = await setup(page);
  await page.goto("/documents?documents_page=9");
  await expect(pages(page)).toContainText("Showing 201–203 of 203 documents");
  await expect(page).toHaveURL(/documents_page=3/);
  wire.rows = []; await page.reload();
  await expect(page.getByText("No documents yet", { exact: true })).toBeVisible();
  await expect(page).toHaveURL(/documents_page=1/);
  expect(wire.writes).toEqual([]);
});

test("an absent kind has an explicit empty state and retains other kind choices", async ({ page }) => {
  const wire = await setup(page);
  await page.goto("/documents?kind=absent");
  await expect(page.getByText("No originals match this kind. Choose All kinds to view the registry.", { exact: true })).toBeVisible();
  await expect(page.getByLabel("Kind").locator("option")).toHaveText(["All kinds", "absent", "logos", "receipts"]);
  await page.getByLabel("Kind", { exact: true }).selectOption("");
  await expect(pages(page)).toContainText("Showing 1–100 of 203 documents");
  expect(wire.writes).toEqual([]);
});

test("an in-flight page disables repeated navigation and kind changes", async ({ page }) => {
  const wire = await setup(page);
  await page.goto("/documents");
  await expect(pages(page)).toContainText("Page 1 of 3");
  let release!: () => void;
  wire.hold = { wait: new Promise<void>((resolve) => { release = resolve; }), release: () => release() };
  try {
    await pages(page).getByRole("button", { name: "Next documents", exact: true }).click();
    await expect(pages(page).getByRole("button", { name: "Next documents", exact: true })).toBeDisabled();
    await expect(page.getByLabel("Kind", { exact: true })).toBeDisabled();
    expect(wire.reads).toHaveLength(2);
  } finally { wire.hold.release(); wire.hold = null; }
  await expect(pages(page)).toContainText("Showing 101–200 of 203 documents");
  expect(wire.reads).toHaveLength(2); expect(wire.writes).toEqual([]);
});

test("positive total with an empty concurrent page has explicit read-only refresh", async ({ page }) => {
  const wire = await setup(page); wire.emptyPage = true;
  await page.goto("/documents?documents_page=3");
  await expect(page.getByText(/This page has no documents/)).toBeVisible();
  await expect(page.getByText("No documents yet", { exact: true })).toHaveCount(0);
  wire.emptyPage = false;
  await page.getByRole("button", { name: "Refresh this page", exact: true }).click();
  await expect(pages(page)).toContainText("Showing 201–203 of 203 documents");
  expect(wire.reads).toHaveLength(2); expect(wire.writes).toEqual([]);
});

for (const width of [1280, 390, 320]) {
  test(`registry bounds its table at${width}px with reachable metadata and controls`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    const wire = await setup(page);
    await page.goto("/documents");
    await expect(pages(page)).toContainText("Showing 1–100 of 203 documents");
    const region = page.getByRole("region", { name: "Stored original metadata", exact: true });
    await region.focus(); await expect(region).toBeFocused();
    const metrics = await page.evaluate(() => ({ rows: document.querySelectorAll("tbody tr").length, scroll: document.documentElement.scrollWidth, client: document.documentElement.clientWidth }));
    expect(metrics.rows).toBe(100); expect(metrics.scroll).toBe(metrics.client);
    if (width < 640) {
      for (const control of [pages(page).getByRole("button", { name: "Next documents", exact: true }), page.getByLabel("Kind", { exact: true })]) {
        const box = await control.boundingBox(); expect(box).not.toBeNull(); expect(box!.height).toBeGreaterThanOrEqual(44);
      }
      expect(await region.evaluate((element) => element.scrollWidth > element.clientWidth)).toBe(true);
    }
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({ path: testInfo.outputPath(`documents-${width}.png`), fullPage: false });
    await testInfo.attach("bounded-registry", { body: JSON.stringify({ metrics, responseBytes: wire.bytes, reads: wire.reads, writes: wire.writes }), contentType: "application/json" });
  });
}

test("enlarged text retains kind and paging controls on a320px screen", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 900 });
  const wire = await setup(page);
  await page.goto("/documents");
  await expect(pages(page)).toContainText("Page 1 of 3");
  await page.addStyleTag({ content: "html { font-size: 32px !important; }" });
  expect(await page.evaluate(() => document.documentElement.scrollWidth === document.documentElement.clientWidth)).toBe(true);
  await pages(page).getByRole("button", { name: "Next documents", exact: true }).click();
  await expect(pages(page)).toContainText("Showing 101–200 of 203 documents");
  await page.getByLabel("Kind", { exact: true }).selectOption("receipts");
  await expect(pages(page)).toContainText("Showing 1–100 of 101 documents");
  expect(wire.writes).toEqual([]);
});
