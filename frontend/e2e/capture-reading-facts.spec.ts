import { writeFile } from "node:fs/promises";
import { expect, test, type Page, type Route } from "@playwright/test";
import fixture from "./fixtures/reading-facts.json" with { type: "json" };

// Synthetic stored-observation responses exercise the actual read-only UI.
// They do not measure OCR accuracy or prove a native recognizer was run.
const OWNER = fixture.facts.owner_id;
const CURRENT = fixture.facts.observation_id;
const OLD = "ffffffff-ffff-4fff-8fff-ffffffffffff";
const ORG = "30000000-0000-4000-8000-000000000001";
const USER = "30000000-0000-4000-8000-000000000002";
const DRAFT = { vendor_name: "Synthetic supplier", invoice_number: "SYNTHETIC-1", issue_date: "2026-11-06", currency: "EUR", source_filename: "synthetic-facts.pdf", line_items: [{ description: "Synthetic service", quantity: "1", unit_price: "10.00", amount: "10.00", tax_rate: "0", category: "uncategorized" }] };
const json = (body: unknown, status = 200) => ({ status, contentType: "application/json", body: JSON.stringify(body) });
type Wire = {
  current: unknown; historical: Record<string, unknown>; history: unknown;
  currentStatus: number; historyStatus: number; recordedStatus: number;
  holdCurrent: boolean; pending: Route | null; currentReads: number;
  historyRequests: string[]; recordedRequests: string[]; writes: string[];
  scoped: { account: string; method: string; path: string; expectedOrg?: string; expectedUser?: string }[];
};
async function setup(page: Page, options: { channel?: "upload" | "inbound"; readOnly?: boolean; status?: string } = {}) {
  const channel = options.channel ?? "upload";
  const current = { ...structuredClone(fixture.facts), channel, attempt_seq: channel === "upload" ? 1 : null };
  const old = { ...structuredClone(current), observation_id: OLD, source_sha256: "f".repeat(64), attempt_seq: channel === "upload" ? 0 : null, requested_profile_id: null, profile: null };
  const summary = { ...structuredClone(fixture.summary), attempt_seq: current.attempt_seq };
  const oldSummary = { ...summary, id: OLD, source_sha256: old.source_sha256, attempt_seq: old.attempt_seq, requested_profile_id: null, profile_id: null, profile_state: "unknown", adapter: null };
  const wire: Wire = { current, historical: { [OLD]: old, [CURRENT]: current }, history: { items: [oldSummary, summary], has_more: false, next_cursor: null }, currentStatus: 200, historyStatus: 200, recordedStatus: 200, holdCurrent: false, pending: null, currentReads: 0, historyRequests: [], recordedRequests: [], writes: [], scoped: [] };
  const base = channel === "upload" ? `/invoices/captures/${OWNER}/reading-observations` : `/email/inbox/${OWNER}/reading-observations`;
  await page.addInitScript(() => {
    if (!sessionStorage.getItem("synthetic-reading-facts-seeded")) {
      localStorage.setItem("invoiceiq_token", "synthetic-reading-A");
      sessionStorage.setItem("synthetic-reading-facts-seeded", "1");
    }
  });
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname.replace("/api/v1", "");
    const account = request.headers().authorization === "Bearer synthetic-reading-B" ? "B" : "A";
    const organization = { id: account === "A" ? ORG : ORG + "B", name: account === "A" ? "Synthetic facts workspace" : "Replacement facts workspace", status: "active" };
    if (request.method() !== "GET") wire.writes.push(`${request.method()} ${path}`);
    if (path === "/auth/me") return route.fulfill(json({ user: { id: account === "A" ? USER : USER + "B", org_id: organization.id, role: options.readOnly ? "auditor" : "owner", name: "Synthetic reviewer", email: "synthetic@example.com" }, organization }));
    if (path === "/auth/organizations") return route.fulfill(json([{ org_id: organization.id, name: organization.name, current: true }]));
    if (path === "/modules") return route.fulfill(json([{ key: "email_intake", name: "Email intake", enabled: true }]));
    if (path.startsWith(base)) wire.scoped.push({ account, method: request.method(), path, expectedOrg: request.headers()["x-expected-org-id"], expectedUser: request.headers()["x-expected-user-id"] });
    if (path === base + "/current") {
      wire.currentReads++;
      if (wire.holdCurrent) { wire.pending = route; return; }
      return route.fulfill(wire.currentStatus === 200 ? json(wire.current) : json({ detail: "Synthetic reading facts unavailable", code: "reading_observation_unavailable" }, wire.currentStatus));
    }
    if (path === base) {
      wire.historyRequests.push(url.search);
      return route.fulfill(wire.historyStatus === 200 ? json(wire.history) : json({ detail: "Synthetic history unavailable" }, wire.historyStatus));
    }
    if (path.startsWith(base + "/")) {
      const id = path.split("/").at(-1)!;
      wire.recordedRequests.push(id);
      return route.fulfill(wire.recordedStatus === 200 ? json(wire.historical[id]) : json({ detail: "Recorded facts cannot be verified; the saved document is unchanged.", code: "reading_observation_unavailable" }, wire.recordedStatus));
    }
    if (path === `/invoices/upload/${OWNER}`) return route.fulfill(account === "B" ? json({ detail: "Capture not found" }, 404) : json({ extraction_run_id: OWNER, status: options.status ?? "parsed", method: "ocr", reading_observation: summary, draft: { draft: DRAFT, warnings: [], fields: [] } }));
    if (path === `/invoices/captures/${OWNER}/fields`) return route.fulfill(json([]));
    if (path === `/invoices/captures/${OWNER}/region-reads`) return route.fulfill(json({ items: [], source_sha256: "e".repeat(64), attempt_seq: 1, current_draft: DRAFT, review_fingerprint: "a".repeat(64), reviewed_fields: [], eligible: false, reason: "Synthetic preview unavailable." }));
    if (path === `/invoices/captures/${OWNER}/pages`) return route.fulfill(json({ status: "unsupported", pages: [], message: "Synthetic preview unavailable." }));
    if (path === "/invoices/duplicate-candidates") return route.fulfill(json({ exact: [], cross_supplier: [], scored: [] }));
    if (path.startsWith("/vendors/resolve")) return route.fulfill(json({ outcome: "create", needs_decision: false, candidates: [] }));
    if (path === "/email/settings") return route.fulfill(json({ address: "synthetic@example.com", token: "synthetic-address", enabled: true }));
    if (path === "/email/health") return route.fulfill(json([]));
    const inbound = { id: OWNER, filename: "synthetic-facts.pdf", from_addr: "synthetic@example.com", subject: "Synthetic source", received_at: "2026-11-06T12:00:00Z", content_type: "application/pdf", size: 1024, status: "pending", method: "ocr", error: null, invoice_id: null, created_at: "2026-11-06T12:00:00Z", reading_observation: summary, draft: { draft: DRAFT, warnings: [], fields: [] }, has_file: false };
    if (path === "/email/inbox") return route.fulfill(json({ items: [inbound], total: 1 }));
    if (path === `/email/inbox/${OWNER}`) return route.fulfill(json(inbound));
    return route.fulfill(json([]));
  });
  await page.goto(channel === "upload" ? `/captures/${OWNER}` : "/email");
  if (channel === "inbound") await page.getByRole("button", { name: /synthetic-facts\.pdf/ }).click();
  const panel = page.getByRole("region", { name: "Reading details", exact: true });
  await expect(panel.getByRole("button", { name: "View reading details", exact: true })).toBeVisible();
  return { wire, panel, base, current, old, summary, oldSummary };
}

for (const channel of ["upload", "inbound"] as const) test(`${channel}: details are lazy, factual and read-only with default language unknown`, async ({ page }) => {
  const { wire, panel } = await setup(page, { channel, readOnly: true });
  expect(wire.currentReads).toBe(0);
  expect(wire.historyRequests).toEqual([]);
  await panel.getByRole("button", { name: "View reading details", exact: true }).click();
  const current = panel.getByRole("region", { name: "Current full-document reading", exact: true });
  await expect(current.getByRole("heading", { name: "Observed reader profile", exact: true })).toBeVisible();
  await expect(current.getByText("Selected language", { exact: true }).locator("..")).toContainText("Engine default · selected value not observed");
  await current.getByText("Recorded installation inventory and reader limits", { exact: true }).click();
  await expect(current).toContainText("Installed language packs: eng, lav");
  await expect(current).toContainText("Installed packs do not identify the selected language");
  const pages = current.getByRole("region", { name: "Page reading outcomes", exact: true });
  await expect(pages).toContainText("Native text used");
  await expect(pages).toContainText("OCR completed");
  await expect(pages.getByText("Target raster density", { exact: true }).last().locator("..")).toContainText("300 dpi target; achieved density is not recorded");
  await expect(pages.getByText("Actual raster dimensions", { exact: true }).last().locator("..")).toContainText("600 × 800 pixels");
  await expect(pages).toContainText("0 ms");
  await expect(panel).toContainText("Manual corrections and selected-area re-reads are recorded separately");
  await expect(panel).toContainText("do not measure recognition accuracy");
  await expect(panel.locator("input,textarea,select")).toHaveCount(0);
  expect(wire.writes).toEqual([]);
  expect(wire.scoped.every((request) => request.expectedOrg === ORG && request.expectedUser === USER)).toBe(true);
  await page.screenshot({ path: test.info().outputPath(`${channel}-reading-facts-desktop.png`), fullPage: true });
});

test("history publication order never substitutes for canonical current and selected history stays labelled", async ({ page }) => {
  const { wire, panel, old } = await setup(page);
  await panel.getByRole("button", { name: "View reading details", exact: true }).click();
  const current = panel.getByRole("region", { name: "Current full-document reading", exact: true });
  await current.getByText("Observation identity and fingerprints", { exact: true }).click();
  await expect(current).toContainText(CURRENT);
  await expect(current).not.toContainText(OLD);
  await panel.getByRole("button", { name: "View recorded reading 0", exact: true }).click();
  const recorded = panel.getByRole("region", { name: "Recorded full-document reading", exact: true });
  await expect(recorded).toContainText("not presented as the current reading");
  await expect(recorded).toContainText("Reader profile and engine were not observed");
  await recorded.getByText("Observation identity and fingerprints", { exact: true }).click();
  await expect(recorded).toContainText(old.source_sha256);
  await expect(recorded).toContainText(OLD);
  expect(wire.recordedRequests).toEqual([OLD]);
  await panel.getByRole("button", { name: "Show current reading", exact: true }).click();
  await expect(panel.getByRole("region", { name: "Current full-document reading", exact: true })).toContainText("Tesseract");
  expect(wire.writes).toEqual([]);
});

test("no verified current and no history remain unknown rather than a successful or empty OCR result", async ({ page }) => {
  const { wire, panel } = await setup(page);
  wire.current = null; wire.history = { items: [], has_more: false, next_cursor: null };
  await panel.getByRole("button", { name: "View reading details", exact: true }).click();
  await expect(panel).toContainText("No verified current reading observation is available");
  await expect(panel).toContainText("This does not mean the document was read successfully");
  await expect(panel).toContainText("No reading observations recorded on this page");
  await expect(panel).not.toContainText("Reading completed");
  expect(wire.writes).toEqual([]);
});

for (const invalid of ["wrong owner", "wrong channel", "numeric timestamp", "unsafe count", "raw text"]) test(`malformed ${invalid} response never becomes a displayed reading and GET retry recovers`, async ({ page }) => {
  const { wire, panel, current } = await setup(page);
  wire.current = { ...current, ...(invalid === "wrong owner" ? { owner_id: "40000000-0000-4000-8000-000000000001" } : invalid === "wrong channel" ? { channel: "inbound" } : invalid === "numeric timestamp" ? { created_at: 1 } : invalid === "unsafe count" ? { page_count: Number.MAX_SAFE_INTEGER + 1 } : { raw_text: "PRIVATE_UNPUBLISHED_WORD_STREAM" }) };
  await panel.getByRole("button", { name: "View reading details", exact: true }).click();
  const section = panel.getByRole("region", { name: "Current full-document reading", exact: true });
  await expect(section.getByRole("alert")).toContainText("Couldn’t load reading facts");
  await expect(section).not.toContainText("No verified current");
  await expect(panel).not.toContainText("PRIVATE_UNPUBLISHED_WORD_STREAM");
  wire.current = current;
  await section.getByRole("button", { name: "Try again", exact: true }).click();
  await expect(section).toContainText("Tesseract");
  expect(wire.writes).toEqual([]);
});

test("history and historical-facts failures have independent GET-only recovery", async ({ page }) => {
  const { wire, panel } = await setup(page);
  wire.historyStatus = 403;
  await panel.getByRole("button", { name: "View reading details", exact: true }).click();
  const history = panel.getByRole("region", { name: "Recorded reading history", exact: true });
  await expect(history.getByRole("alert")).toContainText("Couldn’t load reading history");
  await expect(history).not.toContainText("No reading observations");
  wire.historyStatus = 200;
  await history.getByRole("button", { name: "Try again", exact: true }).click();
  await expect(history.getByRole("button", { name: "View recorded reading 0", exact: true })).toBeVisible();
  wire.recordedStatus = 409;
  await history.getByRole("button", { name: "View recorded reading 0", exact: true }).click();
  const recorded = panel.getByRole("region", { name: "Recorded full-document reading", exact: true });
  await expect(recorded.getByRole("alert")).toContainText("saved document is unchanged");
  await expect(recorded.getByRole("heading", { name: "Observed reader profile", exact: true })).toHaveCount(0);
  wire.recordedStatus = 200;
  await recorded.getByRole("button", { name: "Try again", exact: true }).click();
  await expect(recorded).toContainText("Reader profile and engine were not observed");
  expect(wire.writes).toEqual([]);
});

test("bounded history pages expose at most twenty summaries without fetching every full record", async ({ page }) => {
  const { wire, panel, oldSummary } = await setup(page);
  const items = Array.from({ length: 20 }, (_, index) => ({ ...oldSummary, id: `50000000-0000-4000-8000-${String(index).padStart(12, "0")}`, attempt_seq: index + 10 }));
  const cursor = Buffer.from(JSON.stringify([1, fixture.facts.created_at, items[19].id])).toString("base64url");
  wire.history = { items, has_more: true, next_cursor: cursor };
  await panel.getByRole("button", { name: "View reading details", exact: true }).click();
  const history = panel.getByRole("region", { name: "Recorded reading history", exact: true });
  await expect(history.getByRole("button", { name: /View recorded reading/ })).toHaveCount(20);
  wire.history = { items: [oldSummary], has_more: false, next_cursor: null };
  await history.getByRole("button", { name: "More recorded readings", exact: true }).click();
  await expect(history.getByRole("button", { name: /View recorded reading/ })).toHaveCount(1);
  expect(wire.historyRequests.at(-1)).toBe("?cursor=" + cursor);
  expect(wire.recordedRequests).toEqual([]);
  expect(wire.writes).toEqual([]);
});

test("pending read disables repeated refresh and closing aborts it without starting any reader action", async ({ page }) => {
  const { wire, panel, current } = await setup(page);
  wire.holdCurrent = true;
  await panel.getByRole("button", { name: "View reading details", exact: true }).click();
  await expect(panel.getByRole("button", { name: "Loading reading details…", exact: true })).toBeDisabled();
  await expect(panel.getByText("Loading recorded facts…", { exact: true })).toBeVisible();
  expect(wire.currentReads).toBe(1);
  const oldRequest = wire.pending!;
  await panel.getByRole("button", { name: "Close reading details", exact: true }).click();
  wire.holdCurrent = false;
  await oldRequest.fulfill(json(current)).catch(() => undefined);
  await panel.getByRole("button", { name: "View reading details", exact: true }).click();
  await expect(panel).toContainText("Tesseract");
  expect(wire.currentReads).toBe(2);
  expect(wire.writes).toEqual([]);
});

test("held facts from a retired session cannot appear after switching accounts", async ({ page }) => {
  const { wire, panel, current } = await setup(page);
  wire.holdCurrent = true;
  await panel.getByRole("button", { name: "View reading details", exact: true }).click();
  await expect(panel.getByText("Loading recorded facts…", { exact: true })).toBeVisible();
  const oldRequest = wire.pending!;
  await page.evaluate(() => {
    localStorage.setItem("invoiceiq_token", "synthetic-reading-B");
    window.dispatchEvent(new StorageEvent("storage", { key: "invoiceiq_token", newValue: "synthetic-reading-B" }));
  });
  await expect(page.getByText("Couldn’t load this capture", { exact: true })).toBeVisible();
  await oldRequest.fulfill(json(current)).catch(() => undefined);
  await expect(page.getByText("Tesseract", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("region", { name: "Reading details", exact: true })).toHaveCount(0);
  expect(wire.scoped.filter((request) => request.account === "B")).toEqual([]);
  expect(wire.writes).toEqual([]);
});

for (const channel of ["upload", "inbound"] as const) test(`${channel}: reading facts remain contained at390px and200percent text`, async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.addInitScript(() => document.addEventListener("DOMContentLoaded", () => { document.documentElement.style.fontSize = "32px"; }));
  const { wire, panel } = await setup(page, { channel });
  const before = await page.evaluate(() => document.documentElement.scrollWidth);
  await panel.getByRole("button", { name: "View reading details", exact: true }).click();
  await expect(panel).toContainText("Tesseract");
  await panel.getByText("Recorded installation inventory and reader limits", { exact: true }).click();
  await panel.getByText("Observation identity and fingerprints", { exact: true }).click();
  await page.screenshot({ path: test.info().outputPath(`${channel}-reading-facts-390-root32.png`), fullPage: true });
  const bounds = await page.evaluate(() => ({ width: innerWidth, document: document.documentElement.scrollWidth,
    offenders: [...document.querySelectorAll("main *")].map((element) => { const rect = element.getBoundingClientRect(); return { tag: element.tagName, class: element.className, right: rect.right, left: rect.left, width: rect.width, text: element.textContent?.slice(0, 120) }; }).filter((entry) => entry.right > innerWidth + 1 || entry.left < -1).sort((a, b) => b.width - a.width).slice(0, 25),
  }));
  await writeFile(test.info().outputPath("layout-bounds.json"), JSON.stringify({ before, ...bounds }, null, 2));
  await test.info().attach("page-bounds", { body: JSON.stringify({ before, ...bounds }), contentType: "application/json" });
  expect(bounds.document).toBeLessThanOrEqual(bounds.width);
  expect(wire.writes).toEqual([]);
});

test("explicitly observed language and zero engine settings stay distinct from engine defaults", async ({ page }) => {
  const { wire, panel, current } = await setup(page);
  wire.current = { ...current, profile: { ...current.profile, language_mode: "explicit", resolved_language: "lav", psm_mode: "explicit", resolved_psm: 0, oem_mode: "explicit", resolved_oem: 0 } };
  await panel.getByRole("button", { name: "View reading details", exact: true }).click();
  const profile = panel.getByRole("region", { name: "Observed reader profile", exact: true });
  await expect(profile.getByText("Selected language", { exact: true }).locator("..")).toContainText("Explicit · lav");
  await expect(profile.getByText("Page segmentation", { exact: true }).locator("..")).toContainText("Explicit · 0");
  await expect(profile.getByText("OCR engine mode", { exact: true }).locator("..")).toContainText("Explicit · 0");
  expect(wire.writes).toEqual([]);
});

test("recorded resource exit, timeout and unattempted pages never become completed output", async ({ page }) => {
  const { wire, panel, current } = await setup(page);
  wire.current = { ...current, observation_id: "60000000-0000-4000-8000-000000000001", attempt_seq: 2, source_failure_seq: 1,
    outcome: "failure", failure_code: "processing_timeout", page_count: 3,
    selected_stream_complete: false, selected_stream_bytes: null, selected_stream_sha256: null,
    pages: [
      { ...current.pages[0], status: "outcome_unknown", cause: "resource_or_native_exit", selected_kind: "ocr", selected_chars: null, selected_bytes: null, selected_sha256: null },
      { ...current.pages[1], status: "timed_out", cause: "engine_timeout", ocr_chars: null, ocr_bytes: null, ocr_sha256: null, selected_chars: null, selected_bytes: null, selected_sha256: null },
      { ...current.pages[0], page: 3, status: "not_attempted", cause: "processing_stopped", selected_kind: null, native_chars: null, native_bytes: null, native_sha256: null, selected_chars: null, selected_bytes: null, selected_sha256: null, source_width: null, source_height: null, source_unit: null, elapsed_ms: null },
    ],
  };
  await panel.getByRole("button", { name: "View reading details", exact: true }).click();
  const pages = panel.getByRole("region", { name: "Page reading outcomes", exact: true });
  await expect(pages.getByRole("listitem").nth(0)).toContainText("Outcome unknown");
  await expect(pages.getByRole("listitem").nth(0)).toContainText("resource or native exit");
  await expect(pages.getByRole("listitem").nth(0)).not.toContainText("Timed out");
  await expect(pages.getByRole("listitem").nth(1)).toContainText("Timed out");
  await expect(pages.getByRole("listitem").nth(1)).toContainText("engine timeout");
  await expect(pages.getByRole("listitem").nth(2)).toContainText("Not attempted");
  await expect(pages).not.toContainText("OCR completed");
  await expect(pages).not.toContainText("Native text used");
  await expect(panel.getByText("Complete selected output", { exact: true }).locator("..")).toContainText("Not recorded as complete");
  expect(wire.writes).toEqual([]);
});
