import { test, expect, type Page, type Route } from "@playwright/test";

// Browser/Axios recovery with synthetic API state. Connected API/PG acceptance
// separately qualifies persistence, strict audit and queued intent.
test.use({ timezoneId: "Europe/Riga" });
const USER = { id: "71000000-0000-4000-8000-000000000001", org_id: "71000000-0000-4000-8000-000000000002", name: "Synthetic planner", email: "planner@example.com", role: "owner" };
const ORG = { id: USER.org_id, name: "Synthetic planning workspace", status: "active" };
const PROJECT = { id: "71000000-0000-4000-8000-000000000003", code: "SYN-PLAN", name: "Synthetic planned work" };
const ASSIGNMENT_ID = "71000000-0000-4000-8000-000000000004";
const SLOT = `invoiceiq:command:${USER.id}:${ORG.id}:/schedule/assignments`;
interface Payload {
  project_id: string; assignee_user_id: string; starts_at: string; ends_at: string; all_day: boolean;
  note: string | null; remind_hours_before: number | null; client_notice_hours_before: number | null;
  time_zone: string; notify_assignee: boolean; reminders_enabled: boolean; client_notice_enabled: boolean | null;
}
interface Assignment extends Payload {
  id: string; assignee_email: string; status: string; revision: number; created_by: string;
  project_code: string; project_name: string; notification_status: string | null;
}
interface Result { assignment: Assignment; overlaps: Assignment[] }
const json = (body: unknown, status = 200) => ({ status, contentType: "application/json", body: JSON.stringify(body) });
const saved = (page: Page) => page.evaluate((slot) => sessionStorage.getItem(slot), SLOT);
const check = (page: Page) => page.getByRole("button", { name: "Check assignment outcome", exact: true });
const retry = (page: Page) => page.getByRole("button", { name: "Retry original assignment request", exact: true });
const form = (page: Page) => page.getByRole("form", { name: "Assignment editor", exact: true });
interface Wire {
  posts: Array<{ key: string | undefined; payload: Payload; org: string | undefined; user: string | undefined }>;
  methods: Array<{ account: string; method: string; path: string }>;
  lookups: string[]; currentReads: number; listReads: number; result: Result | null; current: Assignment | null;
  lostResponse: boolean; commitOnFailure: boolean; postStatus: number; postCode: string;
  lookupStatus: number; lookupOffline: boolean; holdPost: boolean; heldPost?: Route;
  currentStatus: number; listStatus: number; holdCurrent: boolean; heldCurrent: Route[];
  receiptPatch?: (receipt: Record<string, unknown>) => Record<string, unknown>;
  overlaps: Assignment[]; planner: boolean;
}
async function setup(page: Page, existing?: Wire) {
  const wire: Wire = existing ?? { posts: [], methods: [], lookups: [], currentReads: 0, listReads: 0, result: null, current: null,
    lostResponse: false, commitOnFailure: true, postStatus: 201, postCode: "planning_command_payload_conflict",
    lookupStatus: 200, lookupOffline: false, holdPost: false, currentStatus: 200, listStatus: 200, holdCurrent: false, heldCurrent: [], overlaps: [], planner: true };
  await page.addInitScript(() => {
    if (!sessionStorage.getItem("planning-fixture-seeded")) {
      localStorage.setItem("invoiceiq_token", "synthetic-planner-A"); sessionStorage.setItem("planning-fixture-seeded", "1");
    }
  });
  await page.clock.setFixedTime(new Date("2026-10-21T09:00:00Z"));
  await page.route("**/api/v1/**", async (route) => {
    const req = route.request(), url = new URL(req.url()), path = url.pathname.replace("/api/v1", ""), method = req.method();
    const account = req.headers().authorization === "Bearer synthetic-planner-B" ? "B" : "A";
    const user = account === "B" ? { ...USER, id: "planner-B", org_id: "planning-org-B", name: "Synthetic planner B" } : USER;
    const org = account === "B" ? { ...ORG, id: "planning-org-B" } : ORG;
    if (path === "/auth/me") return route.fulfill(json({ user, organization: org, permissions: ["invoice.read", ...(wire.planner ? ["invoice.write"] : [])] }));
    if (path === "/auth/organizations") return route.fulfill(json([{ org_id: org.id, name: org.name, current: true }]));
    if (path === "/modules") return route.fulfill(json([]));
    if (path.startsWith("/schedule/")) wire.methods.push({ account, method, path });
    if (path === "/schedule/members") return route.fulfill(json([{ user_id: user.id, email: user.email, name: user.name }]));
    if (path === "/masters/projects") return route.fulfill(json([PROJECT]));
    if (path === "/schedule/assignments" && method === "GET") {
      wire.listReads++;
      if (wire.posts.length && wire.listStatus !== 200) return route.fulfill(json({ detail: "Synthetic plan read unavailable" }, wire.listStatus));
      return route.fulfill(json(account === "B" || !wire.current ? [] : [wire.current]));
    }
    if (path === "/schedule/assignments" && method === "POST") {
      const payload = req.postDataJSON() as Payload;
      wire.posts.push({ key: req.headers()["idempotency-key"], org: req.headers()["x-expected-org-id"], user: req.headers()["x-expected-user-id"], payload });
      if (wire.postStatus !== 201) return route.fulfill(json({ detail: "Synthetic assignment save unavailable", code: wire.postCode }, wire.postStatus));
      const assignment: Assignment = { ...payload, id: ASSIGNMENT_ID, assignee_email: USER.email, status: "planned", revision: 1, created_by: USER.email,
        project_code: PROJECT.code, project_name: PROJECT.name, notification_status: payload.notify_assignee ? "blocked" : "off" };
      if (!wire.lostResponse || wire.commitOnFailure) {
        wire.result = { assignment, overlaps: structuredClone(wire.overlaps) }; wire.current = structuredClone(assignment);
      }
      if (wire.holdPost) { wire.heldPost = route; return; }
      if (wire.lostResponse) return route.abort("failed");
      return route.fulfill(json(wire.result, 201));
    }
    if (path.startsWith("/schedule/commands/")) {
      expect(method).toBe("GET"); const key = path.slice("/schedule/commands/".length); wire.lookups.push(key);
      if (wire.lookupOffline) return route.abort("internetdisconnected");
      if (wire.lookupStatus !== 200 || !wire.result) return route.fulfill(json({ detail: "No committed assignment is visible yet", code: "planning_command_not_found" }, wire.lookupStatus === 200 ? 404 : wire.lookupStatus));
      const receipt = { id: "synthetic-planning-receipt", org_id: ORG.id, actor_user_id: USER.id, command_key: key, status: "committed", operation: "assignment_created", target_id: ASSIGNMENT_ID,
        result: structuredClone(wire.result), payload: structuredClone(wire.posts[0].payload) };
      return route.fulfill(json(wire.receiptPatch ? wire.receiptPatch(receipt) : receipt));
    }
    if (path === `/schedule/assignments/${ASSIGNMENT_ID}` && method === "GET") {
      wire.currentReads++;
      if (wire.holdCurrent) { wire.heldCurrent.push(route); return; }
      return route.fulfill(wire.currentStatus === 200 ? json(wire.current) : json({ detail: "Synthetic assignment read unavailable" }, wire.currentStatus));
    }
    if (path.endsWith("/notifications")) return route.fulfill(json({ smtp_configured: false, items: [] }));
    if (path === "/schedule/calendar-connections") return route.fulfill(json({ providers: [] }));
    return route.fulfill(json([]));
  });
  await page.goto("/schedule");
  await expect(page.getByRole("heading", { name: "Work planning", exact: true })).toBeVisible();
  if (!existing) await expect(page.getByRole("button", { name: "New assignment", exact: true })).toBeEnabled();
  return wire;
}
async function create(page: Page, note = "Synthetic frozen work") {
  await page.getByRole("button", { name: "New assignment", exact: true }).click();
  await form(page).getByLabel("Work details", { exact: true }).fill(note);
  await form(page).getByRole("button", { name: "Save assignment", exact: true }).click();
}

test("assignment creation freezes full defaults and verifies its scoped receipt/current read before releasing the key", async ({ page }) => {
  const wire = await setup(page); wire.holdCurrent = true;
  await create(page);
  await expect.poll(() => wire.currentReads).toBe(1);
  expect(wire.posts).toHaveLength(1);
  expect(wire.posts[0]).toMatchObject({ key: expect.any(String), org: ORG.id, user: USER.id, payload: {
    project_id: PROJECT.id, assignee_user_id: USER.id, starts_at: "2026-10-19T06:00:00.000Z", ends_at: "2026-10-19T14:00:00.000Z", all_day: false,
    note: "Synthetic frozen work", remind_hours_before: null, client_notice_hours_before: null, time_zone: "Europe/Riga", notify_assignee: true, reminders_enabled: true, client_notice_enabled: null,
  } });
  expect(await saved(page)).not.toBeNull();
  await expect(page.getByRole("button", { name: "New assignment", exact: true })).toBeDisabled();
  wire.holdCurrent = false; await wire.heldCurrent.shift()!.fulfill(json(wire.current));
  await expect.poll(() => saved(page)).toBeNull();
  await expect(page.getByText("Assignment saved.", { exact: true })).toBeVisible();
  expect(wire.lookups).toEqual([wire.posts[0].key]);
});

test("a lost assignment response survives reload with its frozen draft and checks the original key using GET only", async ({ page }) => {
  const wire = await setup(page); wire.lostResponse = true;
  await create(page);
  await expect(check(page)).toBeEnabled();
  const original = structuredClone(wire.posts[0]);
  await page.reload();
  await expect(form(page).getByLabel("Work details", { exact: true })).toHaveValue("Synthetic frozen work");
  await expect(form(page).getByLabel("Work details", { exact: true })).toBeDisabled();
  await check(page).click(); await expect.poll(() => saved(page)).toBeNull();
  expect(wire.posts).toEqual([original]);
  expect(wire.lookups).toEqual([original.key]);
});

for (const status of [429, 503]) {
  test(`uncertain assignment POST ${status} preserves one frozen request and permits only an explicit same-key retry`, async ({ page }) => {
    const wire = await setup(page); wire.postStatus = status;
    await create(page); await expect(check(page)).toBeEnabled();
    const original = structuredClone(wire.posts[0]);
    expect(await saved(page)).not.toBeNull(); expect(wire.lookups).toHaveLength(0);
    await check(page).click(); await expect(retry(page)).toBeEnabled();
    expect(wire.posts).toHaveLength(1);
    wire.postStatus = 201;
    await retry(page).click(); await expect.poll(() => saved(page)).toBeNull();
    expect(wire.posts).toEqual([original, original]);
    expect(wire.lookups.every((key) => key === original.key)).toBe(true);
  });
}

test("a missing assignment receipt retains the original draft and only explicit retry repeats its key and body", async ({ page }) => {
  const wire = await setup(page); wire.lostResponse = true; wire.commitOnFailure = false;
  await create(page); await expect(check(page)).toBeEnabled();
  const original = structuredClone(wire.posts[0]);
  await check(page).click(); await expect(retry(page)).toBeEnabled();
  await expect(form(page).getByLabel("Work details", { exact: true })).toBeDisabled();
  expect(wire.posts).toHaveLength(1);
  wire.lostResponse = false;
  await retry(page).click(); await expect.poll(() => saved(page)).toBeNull();
  expect(wire.posts).toEqual([original, original]);
});

for (const failure of [429, 503, "offline"] as const) {
  test(`assignment receipt ${failure} preserves the reference without repeating the write`, async ({ page }) => {
    const wire = await setup(page); wire.lostResponse = true;
    await create(page); await expect(check(page)).toBeEnabled();
    if (failure === "offline") wire.lookupOffline = true; else wire.lookupStatus = failure;
    await check(page).click(); await expect.poll(() => wire.lookups.length).toBe(1); await expect(check(page)).toBeEnabled();
    expect(await saved(page)).not.toBeNull(); expect(wire.posts).toHaveLength(1);
    wire.lookupOffline = false; wire.lookupStatus = 200;
    await check(page).click(); await expect.poll(() => saved(page)).toBeNull();
    expect(wire.posts).toHaveLength(1);
  });
}

for (const mismatch of ["operation", "key", "target", "org", "actor", "payload", "window", "preferences"] as const) {
  test(`a mismatched assignment receipt ${mismatch} cannot release its original reference`, async ({ page }) => {
    const wire = await setup(page); wire.lostResponse = true;
    await create(page); await expect(check(page)).toBeEnabled();
    wire.receiptPatch = (receipt) => {
      if (mismatch === "operation") receipt.operation = "assignment_updated";
      if (mismatch === "key") receipt.command_key = "another-key";
      if (mismatch === "target") receipt.target_id = PROJECT.id;
      if (mismatch === "org") receipt.org_id = "other-org";
      if (mismatch === "actor") receipt.actor_user_id = "other-actor";
      if (mismatch === "payload") receipt.payload = { ...(receipt.payload as Payload), note: "Changed intent" };
      if (mismatch === "window") (receipt.result as Result).assignment.ends_at = "2026-10-19T18:00:00Z";
      if (mismatch === "preferences") (receipt.result as Result).assignment.notify_assignee = false;
      return receipt;
    };
    await check(page).click();
    await expect(page.getByRole("alert").filter({ hasText: /could not be verified/i })).toBeVisible();
    expect(await saved(page)).not.toBeNull(); expect(wire.posts).toHaveLength(1);
    wire.receiptPatch = undefined;
    await check(page).click(); await expect.poll(() => saved(page)).toBeNull();
    expect(wire.posts).toHaveLength(1);
  });
}

for (const read of ["current", "list"] as const) {
  test(`committed assignment ${read} read failure retains the key until GET-only recovery completes`, async ({ page }) => {
    const wire = await setup(page);
    if (read === "current") wire.currentStatus = 503; else wire.listStatus = 503;
    await create(page); await expect(check(page)).toBeEnabled();
    expect(await saved(page)).not.toBeNull(); expect(wire.posts).toHaveLength(1);
    wire.currentStatus = 200; wire.listStatus = 200;
    const start = wire.methods.length;
    await check(page).click(); await expect.poll(() => saved(page)).toBeNull();
    expect(wire.methods.slice(start).every((request) => request.method === "GET")).toBe(true);
    expect(wire.posts).toHaveLength(1);
  });
}

test("a frozen create receipt remains valid after a later edit and keeps its original advisory overlap", async ({ page }) => {
  const wire = await setup(page); wire.lostResponse = true;
  await create(page); await expect(check(page)).toBeEnabled();
  wire.result!.overlaps = [{ ...structuredClone(wire.result!.assignment), id: "71000000-0000-4000-8000-000000000005", status: "confirmed", revision: 3 }];
  wire.current = { ...wire.current!, note: "Later reviewed edit", revision: 2, status: "confirmed" };
  const original = structuredClone(wire.result);
  await check(page).click(); await expect.poll(() => saved(page)).toBeNull();
  await expect(page.getByText(/original.*overlap|overlap.*original|scheduling overlap/i)).toBeVisible();
  await expect(page.locator(`[data-assignment-id="${ASSIGNMENT_ID}"]`).getByText("Later reviewed edit", { exact: true })).toBeVisible();
  expect(wire.result).toEqual(original); expect(wire.posts).toHaveLength(1);
});

test("a payload-key conflict retains the frozen original and never turns into a replacement creation", async ({ page }) => {
  const wire = await setup(page); wire.postStatus = 409;
  await create(page); await expect(check(page)).toBeEnabled();
  expect(await saved(page)).not.toBeNull();
  await expect(page.getByRole("button", { name: "New assignment", exact: true })).toBeDisabled();
  expect(wire.posts).toHaveLength(1);
});

test("rapid create activation reserves one frozen draft before asynchronous hashing", async ({ page }) => {
  const wire = await setup(page); wire.holdPost = true;
  await page.getByRole("button", { name: "New assignment", exact: true }).click();
  await form(page).getByRole("button", { name: "Save assignment", exact: true }).evaluate((button) => { (button as HTMLButtonElement).click(); (button as HTMLButtonElement).click(); });
  await expect.poll(() => wire.posts.length).toBe(1);
  await expect(form(page).getByLabel("Work details", { exact: true })).toBeDisabled();
  await wire.heldPost!.fulfill(json(wire.result, 201));
  await expect.poll(() => saved(page)).toBeNull(); expect(wire.posts).toHaveLength(1);
});

test("a real create deadline retains an uncertain assignment for explicit GET-only recovery", async ({ page }) => {
  const wire = await setup(page); wire.holdPost = true;
  const failed = page.waitForEvent("requestfailed", { predicate: (request) => request.method() === "POST" && new URL(request.url()).pathname === "/api/v1/schedule/assignments", timeout: 20_000 });
  await create(page); await expect(check(page)).toBeEnabled({ timeout: 20_000 });
  expect((await failed).failure()?.errorText).toMatch(/aborted/i);
  expect(wire.posts).toHaveLength(1); expect(wire.lookups).toHaveLength(0);
  await check(page).click(); await expect.poll(() => saved(page)).toBeNull(); expect(wire.posts).toHaveLength(1);
});

test("unreadable planning command storage blocks creation and cannot send a recovery write", async ({ page }) => {
  const wire = await setup(page);
  await page.evaluate((slot) => sessionStorage.setItem(slot, "unreadable synthetic command"), SLOT);
  await page.reload();
  await expect(page.getByRole("alert").filter({ hasText: /saved.*(unreadable|could not be verified)/i })).toBeVisible();
  await expect(page.getByRole("button", { name: "New assignment", exact: true })).toBeDisabled();
  expect(wire.posts).toHaveLength(0); expect(wire.lookups).toHaveLength(0);
});

test("replacement identity cannot continue or reveal the old workspace's frozen planning draft", async ({ page, context }) => {
  const wire = await setup(page); wire.holdPost = true;
  await create(page, "PRIVATE original planning draft"); await expect.poll(() => !!wire.heldPost).toBe(true);
  const original = structuredClone(wire.posts[0]);
  const sender = await context.newPage();
  await sender.route("**/planning-identity-sender", (route) => route.fulfill({ contentType: "text/html", body: "<!doctype html><title>Synthetic planning identity sender</title>" }));
  await sender.goto("http://localhost:5173/planning-identity-sender");
  await sender.evaluate(() => localStorage.setItem("invoiceiq_token", "synthetic-planner-B"));
  await expect(page.getByRole("button", { name: "Account menu", exact: true })).toContainText("Synthetic planner B");
  await wire.heldPost!.fulfill(json(wire.result, 201)).catch(() => undefined);
  await expect(page.getByText(/PRIVATE original planning draft/)).toHaveCount(0);
  await expect(check(page)).toHaveCount(0);
  expect(await saved(page)).not.toBeNull();
  expect(wire.methods.filter((request) => request.account === "B" && (request.method === "POST" || request.path.includes(original.key!)))).toEqual([]);
  expect(wire.posts).toHaveLength(1); await sender.close();
});

test("a real current-assignment read deadline keeps its reference and only the explicit check reads again", async ({ page }) => {
  const wire = await setup(page); wire.holdCurrent = true;
  const failed = page.waitForEvent("requestfailed", { predicate: (request) => request.method() === "GET" && new URL(request.url()).pathname === `/api/v1/schedule/assignments/${ASSIGNMENT_ID}`, timeout: 20_000 });
  await create(page); await expect(check(page)).toBeEnabled({ timeout: 20_000 });
  expect((await failed).failure()?.errorText).toMatch(/aborted/i);
  expect(await saved(page)).not.toBeNull(); expect(wire.posts).toHaveLength(1);
  wire.heldCurrent = []; wire.holdCurrent = false;
  await check(page).click(); await expect.poll(() => saved(page)).toBeNull();
  expect(wire.currentReads).toBe(2); expect(wire.posts).toHaveLength(1);
});

test("unavailable durable browser storage refuses every create write and preserves the unsent draft", async ({ page }) => {
  const wire = await setup(page);
  await page.evaluate((slot) => {
    const original = Storage.prototype.setItem;
    Storage.prototype.setItem = function (key, value) {
      if (key === slot) throw new DOMException("Synthetic saved planning storage unavailable", "QuotaExceededError");
      original.call(this, key, value);
    };
  }, SLOT);
  await create(page, "Synthetic unsent draft");
  await expect(page.getByRole("alert").filter({ hasText: /storage unavailable/i })).toBeVisible();
  await expect(form(page).getByLabel("Work details", { exact: true })).toHaveValue("Synthetic unsent draft");
  expect(await saved(page)).toBeNull(); expect(wire.posts).toHaveLength(0); expect(wire.lookups).toHaveLength(0);
});

test("revoked planning permission retains the saved reference and cannot check or retry it", async ({ page }) => {
  const wire = await setup(page); wire.lostResponse = true;
  await create(page); await expect(check(page)).toBeEnabled();
  wire.planner = false; await page.reload();
  await expect(check(page)).toBeDisabled();
  expect(await saved(page)).not.toBeNull(); expect(wire.lookups).toHaveLength(0); expect(wire.posts).toHaveLength(1);
});

test("the saved planning request and locked draft stay contained at enlarged narrow text", async ({ page }, info) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.addInitScript(() => document.addEventListener("DOMContentLoaded", () => { document.documentElement.style.fontSize = "32px"; }));
  const wire = await setup(page); wire.lostResponse = true; wire.commitOnFailure = false;
  await create(page, "Synthetic long work details ".repeat(15));
  await expect(check(page)).toBeEnabled();
  await check(page).click(); await expect(retry(page)).toBeEnabled();
  const panel = page.getByRole("region", { name: "Saved assignment request", exact: true });
  const widths = await panel.evaluate((element) => ({ client: element.clientWidth, scroll: element.scrollWidth, right: element.getBoundingClientRect().right }));
  expect(widths.scroll).toBeLessThanOrEqual(widths.client + 1); expect(widths.right).toBeLessThanOrEqual(390);
  await retry(page).scrollIntoViewIfNeeded();
  await page.screenshot({ path: info.outputPath("planning-recovery-390-root32.png"), animations: "disabled" });
  expect(wire.posts).toHaveLength(1);
});


test("a browser time-zone change keeps the original draft zone and exact same-key retry instants", async ({ page, context, browser }) => {
  const wire = await setup(page); wire.lostResponse = true; wire.commitOnFailure = false;
  await create(page); await expect(check(page)).toBeEnabled();
  const original = structuredClone(wire.posts[0]), stored = await saved(page);
  expect(stored).not.toBeNull();
  // Restore the same saved browser intent in a real different-zone context.
  // Chromium prevents changing Playwright's existing context override via a
  // second CDP session; this models a browser restart after changing the zone.
  const changed = await browser.newContext({ baseURL: "http://localhost:5173", timezoneId: "America/New_York", storageState: await context.storageState() });
  try {
    const returned = await changed.newPage();
    await returned.addInitScript(({ slot, data }) => {
      sessionStorage.setItem(slot, data); sessionStorage.setItem("planning-fixture-seeded", "1");
    }, { slot: SLOT, data: stored! });
    await setup(returned, wire);
    expect(await returned.evaluate(() => Intl.DateTimeFormat().resolvedOptions().timeZone)).toBe("America/New_York");
    await expect(form(returned).getByText("Times use Europe/Riga. Overlaps are warnings; they do not block saving.", { exact: true })).toBeVisible();
    await expect(form(returned).getByLabel("From", { exact: true })).toHaveValue("09:00");
    await check(returned).click(); await expect(retry(returned)).toBeEnabled();
    wire.lostResponse = false;
    await retry(returned).click(); await expect.poll(() => saved(returned)).toBeNull();
    expect(wire.posts).toEqual([original, original]);
  } finally { await changed.close(); }
});
