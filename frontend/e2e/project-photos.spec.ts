import { test, expect, type Page, type Route } from "@playwright/test";

/**
 * Job photos (WO-F) over the LIVE app shell, API mocked via `page.route`.
 *
 * What earns its place here:
 *  - the Photos card renders bounded authenticated derivatives; originals
 *    are fetched only after explicit activation, and "+ Add photo" uploads with
 *    kind=photo;
 *  - photos stay OUT of the documents table (they live in the grid);
 *  - copy is industry-neutral (owner guard list).
 *
 * Synthetic fixtures only.
 */

const ORG = { id: "org-1", name: "Test Workspace", status: "active" };

// A 1×1 transparent PNG, base64 — real enough for an <img>.
const PNG_B64 =
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==";

const DOCS = [
  {
    id: "doc-contract",
    kind: "contract",
    filename: "signed-contract.pdf",
    content_type: "application/pdf",
    uploaded_by: "someone@test.io",
    created_at: "2026-08-20T10:00:00+00:00",
  },
  {
    id: "doc-photo-1",
    kind: "photo",
    filename: "before.jpg",
    content_type: "image/png",
    uploaded_by: "someone@test.io",
    created_at: "2026-08-21T10:00:00+00:00",
  },
];

async function open(page: Page, opts: { onUpload?: (kind: string | null, body: string | null) => void; beforeUpload?: () => Promise<void>; photos?: typeof DOCS; onPhotoRead?: (kind: "thumbnail" | "download", id: string) => { status?: number; body?: Buffer; abort?: boolean } | void | Promise<{ status?: number; body?: Buffer; abort?: boolean } | void> } = {}) {
  // Arm browser chooser interception before navigation/native key activation.
  page.on("filechooser", () => {});
  await page.addInitScript(() => localStorage.setItem("invoiceiq_token", "e2e-token"));

  const json = (body: unknown, code = 200) => ({
    status: code,
    contentType: "application/json",
    body: JSON.stringify(body),
  });

  await page.route("**/api/v1/**", async (route: Route) => {
    const url = new URL(route.request().url());
    const p = url.pathname.replace(/^.*\/api\/v1/, "");
    const method = route.request().method();

    if (p === "/auth/me")
      return route.fulfill(
        json({
          user: {
            id: "user-1",
            email: "someone@test.io",
            name: "Test User",
            role: "owner",
            org_id: "org-1",
            is_platform_admin: false,
          },
          organization: ORG,
        }),
      );
    if (p === "/auth/organizations") return route.fulfill(json([{ org_id: ORG.id, name: ORG.name, current: true }]));
    if (p === "/modules") return route.fulfill(json([]));

    if (p === "/masters/projects/proj-1/documents") {
      if (method === "POST") {
        opts.onUpload?.(url.searchParams.get("kind"), route.request().postData());
        await opts.beforeUpload?.();
        return route.fulfill(json({ ...DOCS[1], id: "doc-photo-new" }, 201));
      }
      return route.fulfill(json(opts.photos ?? DOCS));
    }
    if (p.startsWith("/masters/projects/proj-1/documents/") && /\/(thumbnail|download)$/.test(p)) {
      const kind = p.endsWith("/thumbnail") ? "thumbnail" : "download";
      const response = await opts.onPhotoRead?.(kind, p.split("/").at(-2)!);
      if (response?.abort) return route.abort("failed");
      return route.fulfill({ status: response?.status ?? 200, contentType: "image/png", body: response?.body ?? Buffer.from(PNG_B64, "base64") });
    }
    if (p === "/masters/projects/proj-1/cost-entries") return route.fulfill(json([]));
    if (p === "/masters/projects-pnl-summary") return route.fulfill(json([]));
    if (p === "/masters/projects/proj-1/offers") return route.fulfill(json([]));
    if (p === "/masters/projects/proj-1/invoicing-plan")
      return route.fulfill(
        json({ rows: [], contracted: "0.00", issued: "0.00", remaining: "0.00" }),
      );
    if (p === "/masters/projects/proj-1/pnl")
      return route.fulfill(
        json({
          project_id: "proj-1",
          code: "JOB-7",
          name: "Won contract",
          status: "active",
          basis: "net_eur_live",
  basis_version: 2, reporting_currency: "EUR", complete: true,
  issues: [], fx_trace: [], legacy_figures: {}, adjustments_complete: true,
          revenue: "0.00",
          credited: "0.00",
          costs: "0.00",
          invoice_costs: "0.00",
          expense_costs: "0.00",
          manual_costs: "0.00",
          profit: "0.00",
          margin_pct: null,
          pnl_frozen_at: null,
          adjustments: [],
          accepted_at: null,
          accepted_by: null,
          acceptance_document_id: null,
          acceptance_note: null,
        }),
      );
    if (p === "/masters/projects")
      return route.fulfill(
        json([
          {
            id: "proj-1",
            code: "JOB-7",
            name: "Won contract",
            status: "active",
            version: 1,
            customer_id: null,
          },
        ]),
      );
    if (p === "/customers") return route.fulfill(json([]));

    return route.fulfill(json({ items: [], total: 0 }));
  });

  await page.goto("/projects/proj-1");
}

test("the photos card shows the grid and uploads with kind=photo", async ({ page }) => {
  const uploads: (string | null)[] = [];
  await open(page, { onUpload: (k) => uploads.push(k) });

  await expect(page.getByRole("heading", { name: "Job photos" })).toBeVisible();
  await page.locator('[data-photo-id="doc-photo-1"]').scrollIntoViewIfNeeded();
  await expect(page.getByRole("img", { name: "before.jpg", exact: true })).toBeVisible();

  await page
    .locator('input[type="file"][accept="image/*"]')
    .setInputFiles({ name: "after.png", mimeType: "image/png", buffer: Buffer.from(PNG_B64, "base64") });
  await expect.poll(() => uploads.length).toBe(1);
  expect(uploads[0]).toBe("photo");
});

for (const fontSize of [16, 32]) {
  for (const key of ["Enter", "Space"]) {
    for (const kind of [null, "photo"]) {
      test(`native ${key} chooses and uploads ${kind ?? "document"} at 390px/${fontSize}px text`, async ({ page }) => {
        await page.setViewportSize({ width: 390, height: 844 });
        const uploads: { kind: string | null; body: string | null }[] = [];
        await open(page, { onUpload: (kind, body) => uploads.push({ kind, body }) });
        await page.evaluate((size) => { document.documentElement.style.fontSize = `${size}px`; }, fontSize);
        const photoInput = page.locator('input[type="file"][accept="image/*"]');
        await expect(photoInput).toHaveAttribute("capture", "environment");
        const button = page.getByRole("button", { name: kind ? "+ Add photo" : "Attach document", exact: true });
        await expect(button).toBeEnabled();
        await button.focus();
        // Native sequential focus reaches the action, not the hidden input.
        await page.keyboard.press("Shift+Tab");
        await page.keyboard.press("Tab");
        await expect(button).toBeFocused();
        const chooser = page.waitForEvent("filechooser", { timeout: 5_000 });
        await page.keyboard.press(key);
        const filename = kind ? "keyboard-photo.png" : "keyboard-contract.pdf";
        await (await chooser).setFiles({ name: filename, mimeType: kind ? "image/png" : "application/pdf", buffer: kind ? Buffer.from(PNG_B64, "base64") : Buffer.from("synthetic contract") });
        await expect.poll(() => uploads.length).toBe(1);
        expect(uploads[0].kind).toBe(kind);
        expect(uploads[0].body).toContain(filename);
        await expect(button).toBeEnabled();
        const box = await button.boundingBox();
        expect(box!.x).toBeGreaterThanOrEqual(0);
        expect(box!.x + box!.width).toBeLessThanOrEqual(391);
        expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
      });
    }
  }
}

for (const kind of [null, "photo"]) {
  test(`${kind ?? "document"} chooser action and input are disabled during upload`, async ({ page }) => {
    let release = () => {};
    const pending = new Promise<void>((resolve) => { release = resolve; });
    const uploads: (string | null)[] = [];
    await open(page, { onUpload: (kind) => uploads.push(kind), beforeUpload: () => pending });
    const button = page.getByRole("button", { name: kind ? "+ Add photo" : "Attach document", exact: true });
    await button.focus();
    const chooser = page.waitForEvent("filechooser");
    await page.keyboard.press("Enter");
    await (await chooser).setFiles({ name: kind ? "pending.png" : "pending.pdf", mimeType: kind ? "image/png" : "application/pdf", buffer: kind ? Buffer.from(PNG_B64, "base64") : Buffer.from("synthetic") });
    await expect.poll(() => uploads.length).toBe(1);
    await expect(kind ? page.getByRole("button", { name: "Uploading…", exact: true }) : button).toBeDisabled();
    await expect(page.getByLabel(kind ? "Choose project photo" : "Choose project document", { exact: true })).toBeDisabled();
    release();
    await expect(button).toBeEnabled();
  });
}

test("photos stay out of the documents table", async ({ page }) => {
  await open(page);
  await expect(page.getByRole("heading", { name: "Job photos" })).toBeVisible();
  const docsCard = page.locator(".card", { hasText: "Contract & documents" });
  await expect(docsCard.getByText("signed-contract.pdf")).toBeVisible();
  await expect(docsCard.getByText("before.jpg")).toHaveCount(0);
});

test("the photos copy is industry-neutral", async ({ page }) => {
  await open(page);
  await expect(page.getByRole("heading", { name: "Job photos" })).toBeVisible();
  const text = (await page.locator("body").innerText()).toLowerCase();
  for (const word of ["cargo", "fuel", "vehicle", "driver", "truck", "site crew"]) {
    expect(text).not.toContain(word);
  }
});


for (const [width, fontSize] of [[390, 16], [390, 32], [1280, 16]]) {
  test(`30 photos fetch only near-visible derivatives and release their URLs at ${width}px/${fontSize}px text`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 844 });
    await page.addInitScript(() => {
      const originalCreate = URL.createObjectURL.bind(URL), originalRevoke = URL.revokeObjectURL.bind(URL);
      const owned = new Set<string>();
      Object.assign(window, { photoUrls: owned, photoUrlCounts: { created: 0, revoked: 0 } });
      URL.createObjectURL = blob => { const url = originalCreate(blob); owned.add(url); (window as unknown as { photoUrlCounts: { created: number } }).photoUrlCounts.created++; return url; };
      URL.revokeObjectURL = url => { owned.delete(url); (window as unknown as { photoUrlCounts: { revoked: number } }).photoUrlCounts.revoked++; originalRevoke(url); };
    });
    const reads: { kind: string; id: string; bytes: number }[] = [];
    const photos = Array.from({ length: 30 }, (_, i) => ({ ...DOCS[1], id: `photo-${i}`, filename: `Synthetic evidence ${i}.png` }));
    await open(page, { photos, onPhotoRead: (kind, id) => {
      const body = kind === "download" ? Buffer.concat([Buffer.from(PNG_B64, "base64"), Buffer.alloc(512_000)]) : Buffer.from(PNG_B64, "base64");
      reads.push({ kind, id, bytes: body.length }); return { body };
    } });
    await page.evaluate(size => { document.documentElement.style.fontSize = `${size}px`; }, fontSize);
    await page.locator('[data-photo-id="photo-0"]').scrollIntoViewIfNeeded();
    await expect(page.getByRole("img", { name: "Synthetic evidence 0.png", exact: true })).toBeVisible();
    expect(reads.filter(r => r.kind === "thumbnail").length).toBeGreaterThan(0);
    expect(reads.filter(r => r.kind === "thumbnail").length).toBeLessThan(30);
    expect(reads.filter(r => r.kind === "download")).toEqual([]);
    const first = page.getByRole("button", { name: "Open original photo Synthetic evidence 0.png", exact: true });
    await first.focus(); await page.keyboard.press("Enter");
    const dialog = page.getByRole("dialog", { name: "Synthetic evidence 0.png", exact: true });
    await expect(dialog.getByRole("img", { name: "Synthetic evidence 0.png", exact: true })).toBeVisible();
    expect(reads.filter(r => r.kind === "download")).toEqual([{ kind: "download", id: "photo-0", bytes: 512_070 }]);
    const download = page.waitForEvent("download");
    await dialog.getByRole("link", { name: "Download original", exact: true }).click();
    expect((await download).suggestedFilename()).toBe("Synthetic evidence 0.png");
    expect(reads.filter(r => r.kind === "download")).toHaveLength(1);
    await page.keyboard.press("Escape");
    await expect(first).toBeFocused();
    await page.locator('[data-photo-id="photo-29"]').scrollIntoViewIfNeeded();
    await expect(page.getByRole("img", { name: "Synthetic evidence 29.png", exact: true })).toBeVisible();
    await expect.poll(() => page.evaluate(() => (window as unknown as { photoUrlCounts: { revoked: number } }).photoUrlCounts.revoked)).toBeGreaterThan(0);
    const projectsSummary = page.waitForResponse(response => new URL(response.url()).pathname === "/api/v1/masters/projects-pnl-summary" && response.status() === 200);
    await page.getByRole("link", { name: "All projects", exact: true }).click();
    await projectsSummary;
    await expect(page).toHaveURL(/\/cost-objects\?kind=projects$/);
    await expect(page.getByRole("heading", { name: "Cost objects", exact: true })).toBeVisible();
    await expect(page.getByRole("tab", { name: "Projects", exact: true })).toHaveAttribute("aria-selected", "true");
    await expect(page.getByRole("link", { name: "JOB-7", exact: true })).toBeVisible();
    await expect(page.getByText("This page hit an error", { exact: true })).toHaveCount(0);
    await expect.poll(() => page.evaluate(() => (window as unknown as { photoUrls: Set<string> }).photoUrls.size)).toBe(0);
    await testInfo.attach("photo-request-bytes", { body: JSON.stringify({ viewport: width, fontSize, reads, urls: await page.evaluate(() => (window as unknown as { photoUrlCounts: unknown }).photoUrlCounts), scope: "Synthetic PNG derivative is 70 bytes; original is the same PNG plus 512000 trailing bytes. Counts/bytes qualify this fixture only, not real-image savings or physical devices." }), contentType: "application/json" });
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
  });
}

for (const status of [403, 404, 422, 503]) {
  test(`photo preview ${status} has local explanation, explicit retry and an original action`, async ({ page }) => {
    let recovered = false; const reads: string[] = [];
    await open(page, { onPhotoRead: kind => { reads.push(kind); return { status: kind === "thumbnail" && !recovered ? status : 200 }; } });
    const tile = page.locator('[data-photo-id="doc-photo-1"]');
    await tile.scrollIntoViewIfNeeded();
    await expect(tile.getByRole("alert")).toContainText(status === 403 ? "access" : status === 404 ? "missing" : status === 422 ? "Preview unavailable" : "could not be loaded");
    expect(reads).toEqual(["thumbnail"]);
    recovered = true;
    await tile.getByRole("button", { name: "Retry preview", exact: true }).click();
    await expect(tile.getByRole("img", { name: "before.jpg", exact: true })).toBeVisible();
    expect(reads).toEqual(["thumbnail", "thumbnail"]);
    await tile.getByRole("button", { name: "Open original photo before.jpg", exact: true }).click();
    await expect(page.getByRole("dialog").getByRole("img", { name: "before.jpg", exact: true })).toBeVisible();
    expect(reads).toEqual(["thumbnail", "thumbnail", "download"]);
  });
}


test("a network preview failure retries locally without an original fetch", async ({ page }) => {
  let offline = true; const reads: string[] = [];
  await open(page, { onPhotoRead: kind => { reads.push(kind); return { abort: offline }; } });
  const tile = page.locator('[data-photo-id="doc-photo-1"]');
  await tile.scrollIntoViewIfNeeded();
  await expect(tile.getByRole("alert")).toContainText("preview could not be loaded");
  expect(reads).toEqual(["thumbnail"]);
  offline = false;
  await tile.getByRole("button", { name: "Retry preview", exact: true }).click();
  await expect(tile.getByRole("img", { name: "before.jpg", exact: true })).toBeVisible();
  expect(reads).toEqual(["thumbnail", "thumbnail"]);
});

test("leaving a near-visible photo cancels its pending derivative read", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  let release!: () => void;
  const pending = new Promise<void>(resolve => { release = resolve; });
  const failed: string[] = []; let started = false;
  page.on("requestfailed", request => { if (request.url().endsWith("/thumbnail")) failed.push(request.failure()?.errorText ?? "unknown"); });
  await open(page, { onPhotoRead: async kind => { if (kind === "thumbnail") { started = true; await pending; } } });
  await page.locator('[data-photo-id="doc-photo-1"]').scrollIntoViewIfNeeded();
  await expect.poll(() => started).toBe(true);
  await page.getByRole("heading", { level: 1 }).scrollIntoViewIfNeeded();
  await expect.poll(() => failed.length).toBe(1);
  expect(failed[0]).toContain("ABORTED");
  release();
  await expect(page.getByRole("alert").filter({ hasText: "preview could not" })).toHaveCount(0);
});


test("near-visible thumbnail reads wait for the earlier response and discard cancelled queued work", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  const started: string[] = [];
  const photos = Array.from({ length: 30 }, (_, i) => ({ ...DOCS[1], id: `serial-${i}`, filename: `Sequential ${i}.png` }));
  await open(page, { photos, onPhotoRead: async (kind, id) => {
    expect(kind).toBe("thumbnail"); started.push(id);
    if (started.length === 1) await gate;
  } });
  await page.locator('[data-photo-id="serial-0"]').scrollIntoViewIfNeeded();
  await expect.poll(() => started.length).toBe(1);
  // Activate the real route without scrolling it into view first: scrolling
  // itself can cancel an old near-visible read and legitimately start another.
  await page.getByRole("link", { name: "All projects", exact: true }).evaluate(e => (e as HTMLAnchorElement).click());
  await expect(page.locator("[data-photo-id]")).toHaveCount(0);
  const atUnmount = [...started];
  release();
  await page.waitForLoadState("networkidle");
  expect(started).toEqual(atUnmount);
});
