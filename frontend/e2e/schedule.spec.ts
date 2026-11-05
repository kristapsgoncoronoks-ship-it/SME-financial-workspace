import { test, expect, type Page, type Route } from "@playwright/test";

/** Synthetic API fixtures exercise the actual application shell. Server state is
 * mutable so a successful action must survive the authoritative list readback. */
test.use({ timezoneId: "Europe/Riga" });
const ORG = { id: "org-1", name: "Test Workspace", status: "active" };
const ASSIGNMENT = {
  id: "asg-1", project_id: "proj-1", project_code: "JOB-7", project_name: "Won contract",
  assignee_user_id: "user-1", assignee_email: "someone@test.io",
  starts_at: "2026-10-20T09:00:17Z", ends_at: "2026-10-20T17:00:21Z", all_day: false,
  created_by: "someone@test.io", remind_hours_before: null as number | null, status: "planned", note: "Bring the signed contract", revision: 1, time_zone: "Europe/Riga",
  notify_assignee: true, reminders_enabled: true, client_notice_enabled: null as boolean | null,
  client_notice_hours_before: null as number | null, notification_status: "queued",
};
type Row = typeof ASSIGNMENT;
interface State {
  rows: Row[]; creates: Record<string, unknown>[]; patches: Record<string, unknown>[];
  transitions: Record<string, unknown>[]; listRequests: string[]; memberStatus: number;
  failReads: boolean; conflict: boolean; feedFailures: number; regenerated: number;
  lostCreateResponse: boolean; holdCreate: boolean; delayedCreate?: Route; identityB: boolean;
  beforeRead?: () => Promise<void>;
  receipts: Record<string, { payload: Record<string, unknown>; result: { assignment: Row; overlaps: Row[] } }>;
}
async function open(page: Page, opts: { planner?: boolean; memberStatus?: number; rows?: Row[] } = {}) {
  const planner = opts.planner ?? true;
  const state: State = { rows: structuredClone(opts.rows ?? [ASSIGNMENT]), creates: [], patches: [], transitions: [], listRequests: [], memberStatus: opts.memberStatus ?? 200, failReads: false, conflict: false, feedFailures: 0, regenerated: 0, lostCreateResponse: false, holdCreate: false, identityB: false, receipts: {} };
  await page.addInitScript(() => localStorage.setItem("invoiceiq_token", "e2e-token"));
  await page.clock.setFixedTime(new Date("2026-10-21T09:00:00Z"));
  const json = (body: unknown, status = 200) => ({ status, contentType: "application/json", body: JSON.stringify(body) });
  await page.route("**/api/v1/**", async (route: Route) => {
    const url = new URL(route.request().url()), path = url.pathname.replace(/^.*\/api\/v1/, ""), method = route.request().method();
    if (path === "/auth/me") return route.fulfill(json({ user: { id: state.identityB ? "owner-B" : "user-1", email: state.identityB ? "B@test.io" : "someone@test.io", name: state.identityB ? "Owner B" : "Test User", role: planner ? "owner" : "user", org_id: state.identityB ? "org-B" : ORG.id, is_platform_admin: false }, organization: state.identityB ? { ...ORG, id: "org-B", name: "Workspace B" } : ORG }));
    if (path === "/auth/organizations") return route.fulfill(json([{ org_id: state.identityB ? "org-B" : ORG.id, name: state.identityB ? "Workspace B" : ORG.name, current: true }]));
    if (path === "/modules") return route.fulfill(json([]));
    if (path === "/schedule/members") return route.fulfill(state.memberStatus === 200 ? json([{ user_id: "user-1", email: "someone@test.io", name: "Test User" }, { user_id: "user-2", email: "crew@test.io", name: "Crew Person" }]) : json({ detail: "Planning team unavailable" }, state.memberStatus));
    if (path === "/masters/projects") return route.fulfill(json([{ id: "proj-1", code: "JOB-7", name: "Won contract", status: "active", version: 1 }]));
    if (path === "/schedule/assignments") {
      if (method === "POST") {
        const body = route.request().postDataJSON(); state.creates.push(body);
        const row: Row = { ...ASSIGNMENT, ...body, id: `asg-new-${state.creates.length}`, assignee_email: body.assignee_user_id === "user-2" ? "crew@test.io" : "someone@test.io", notification_status: body.notify_assignee ? "queued" : "off" };
        state.rows.push(row);
        state.receipts[route.request().headers()["idempotency-key"]] = { payload: body, result: { assignment: row, overlaps: body.note === "Overlap example" ? [ASSIGNMENT] : [] } };
        if (state.lostCreateResponse) return route.abort("failed");
        if (state.holdCreate) { state.delayedCreate = route; return; }
        return route.fulfill(json({ assignment: row, overlaps: body.note === "Overlap example" ? [ASSIGNMENT] : [] }, 201));
      }
      state.listRequests.push(url.search);
      await state.beforeRead?.();
      if (state.failReads) return route.fulfill(json({ detail: "Read temporarily unavailable" }, 503));
      const start = Date.parse(url.searchParams.get("start") ?? ""), end = Date.parse(url.searchParams.get("end") ?? "");
      return route.fulfill(json(state.rows.filter((row) => Date.parse(row.starts_at) < end && Date.parse(row.ends_at) > start && (!url.searchParams.get("assignee_user_id") || row.assignee_user_id === url.searchParams.get("assignee_user_id")) && (!url.searchParams.get("project_id") || row.project_id === url.searchParams.get("project_id")))));
    }
    if (path.startsWith("/schedule/commands/") && method === "GET") {
      const key = path.slice("/schedule/commands/".length), receipt = state.receipts[key];
      return route.fulfill(receipt ? json({ id: "planning-receipt", org_id: ORG.id, actor_user_id: "user-1", command_key: key, status: "committed", operation: "assignment_created", target_id: receipt.result.assignment.id, ...receipt }) : json({ detail: "No committed assignment is visible" }, 404));
    }
    if (path.startsWith("/schedule/assignments/asg-new-") && method === "GET") {
      const row = state.rows.find((value) => path.endsWith(value.id));
      return route.fulfill(state.failReads ? json({ detail: "Read temporarily unavailable" }, 503) : row ? json(row) : json({ detail: "Assignment missing" }, 404));
    }
    if (path === "/schedule/assignments/asg-1" && method === "PATCH") {
      const body = route.request().postDataJSON(); state.patches.push(body);
      if (state.conflict) { state.conflict = false; state.rows[0] = { ...state.rows[0], note: "Another planner updated this", revision: 2 }; return route.fulfill(json({ detail: "Revision changed" }, 409)); }
      state.rows[0] = { ...state.rows[0], ...body, revision: state.rows[0].revision + 1 };
      return route.fulfill(json({ assignment: state.rows[0], overlaps: [] }));
    }
    if (path === "/schedule/assignments/asg-1/transition") {
      const body = route.request().postDataJSON(); state.transitions.push(body);
      state.rows[0] = { ...state.rows[0], status: body.status, revision: state.rows[0].revision + 1 };
      return route.fulfill(json(state.rows[0]));
    }
    if (path.endsWith("/notifications")) return route.fulfill(json({ smtp_configured: false, items: [] }));
    if (path === "/schedule/calendar-connections") return route.fulfill(json({ providers: ["google", "outlook"].map((provider) => ({ provider, configured: false, reason: "Not configured", connection: null })) }));
    if (path === "/schedule/feed-token" || path === "/schedule/feed-token/regenerate") {
      if (method === "POST") {
        if (state.feedFailures > 0) { state.feedFailures--; return route.fulfill(json({ detail: "Unable to replace link" }, 503)); }
        state.regenerated++;
      }
      const token = state.regenerated ? "tok-NEW" : "tok-OLD";
      return route.fulfill(json({ token, path: `/api/v1/calendar/feed/${token}.ics` }));
    }
    return route.fulfill(json({ items: [], total: 0 }));
  });
  return state;
}
const card = (page: Page) => page.locator('[data-assignment-id="asg-1"]').first();
async function editor(page: Page) { await page.getByRole("button", { name: "New assignment", exact: true }).click(); return page.getByRole("form", { name: "Assignment editor" }); }

for (const [width, fontSize] of [[390, 16], [390, 32], [1280, 16]]) {
  test(`native reverse-Tab reveals New assignment below the sticky header at ${width}px/${fontSize}px text`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 844 });
    await open(page); await page.goto("/schedule");
    await page.evaluate((size) => { document.documentElement.style.fontSize = `${size}px`; }, fontSize);
    const calendar = page.getByRole("tab", { name: "Calendar", exact: true });
    await calendar.click();
    await page.keyboard.press("Tab");
    await page.keyboard.press("Tab");
    await expect(page.getByRole("region", { name: "Weekly calendar", exact: true })).toBeFocused();
    await page.mouse.wheel(0, 700);
    await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThan(100);
    const create = page.getByRole("button", { name: "New assignment", exact: true });
    let reached = false;
    for (let index = 0; index < 10; index++) {
      await page.keyboard.press("Shift+Tab");
      if (await create.evaluate((element) => element === document.activeElement)) { reached = true; break; }
    }
    expect(reached, "native reverse-Tab reaches the existing create action").toBe(true);
    const bounds = await create.boundingBox();
    const header = await page.locator("header.sticky").boundingBox();
    const measurements = { width, fontSize, button: bounds, header, scrollY: await page.evaluate(() => window.scrollY) };
    await testInfo.attach("native-reverse-tab-bounds", { body: JSON.stringify(measurements), contentType: "application/json" });
    await page.screenshot({ path: testInfo.outputPath("native-reverse-tab.png") });
    expect(bounds!.y, "the focused button and its ring clear the sticky header").toBeGreaterThanOrEqual(header!.y + header!.height + 3);
    expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(844);
    await page.keyboard.press("Enter");
    await expect(page.getByRole("form", { name: "Assignment editor" })).toBeVisible();
  });
}

test("Board and Calendar share records and filters; keyboard changes the view", async ({ page }, testInfo) => {
  const state = await open(page); await page.goto("/schedule");
  await expect(page.getByRole("heading", { name: "Work planning", exact: true })).toBeVisible();
  await expect(page.getByRole("region", { name: "Planned assignments" }).locator('[data-assignment-id="asg-1"]')).toHaveCount(1);
  await expect(card(page).getByRole("heading", { name: "Won contract" })).toBeVisible();
  await expect(card(page).getByText("Assignment email queued", { exact: true })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("planning-desktop-board.png"), fullPage: true });
  await page.getByRole("tab", { name: "Board", exact: true }).focus(); await page.keyboard.press("ArrowRight");
  await expect(page.getByRole("tab", { name: "Calendar", exact: true })).toHaveAttribute("aria-selected", "true");
  await expect(card(page).getByText("Bring the signed contract")).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("planning-desktop-calendar.png"), fullPage: true });
  await page.getByLabel("Filter by team member").selectOption("user-2");
  await expect(page.locator('[data-assignment-id="asg-1"]')).toHaveCount(0);
  await expect.poll(() => state.listRequests.some((q) => q.includes("assignee_user_id=user-2"))).toBe(true);
  await page.getByRole("tab", { name: "Board", exact: true }).click();
  await expect(page.getByLabel("Filter by team member")).toHaveValue("user-2");
  await expect(page.getByText("No assignments in this week", { exact: true })).toBeVisible();
});

test("new assignment preserves local Monday, exact window, explicit email opt-outs and server readback", async ({ page }) => {
  const state = await open(page); await page.goto("/schedule"); const form = await editor(page);
  await expect(form.getByLabel("Start date", { exact: true })).toHaveValue("2026-10-19");
  await form.getByLabel("Person", { exact: true }).selectOption("user-2");
  await form.getByLabel("Start date", { exact: true }).fill("2026-10-22");
  await form.getByLabel("From", { exact: true }).fill("09:15"); await form.getByLabel("To", { exact: true }).fill("17:45");
  await form.getByLabel("Work details").fill("Gate code 4711");
  await form.getByLabel("Email the assigned person about changes").uncheck(); await form.getByLabel("Send an upcoming-work reminder").uncheck();
  await form.getByLabel("Customer arrival email", { exact: true }).selectOption("off");
  await form.getByRole("button", { name: "Save assignment", exact: true }).click();
  await expect(page.getByText("Assignment saved.", { exact: true })).toBeVisible();
  expect(state.creates).toHaveLength(1);
  expect(state.creates[0]).toMatchObject({ project_id: "proj-1", assignee_user_id: "user-2", starts_at: "2026-10-22T06:15:00.000Z", ends_at: "2026-10-22T14:45:00.000Z", time_zone: "Europe/Riga", all_day: false, note: "Gate code 4711", notify_assignee: false, reminders_enabled: false, client_notice_enabled: false });
  const saved = page.locator('[data-assignment-id="asg-new-1"]');
  await expect(saved.getByText("Gate code 4711")).toBeVisible(); await expect(saved.getByText("Assignment email off", { exact: true })).toBeVisible();
});

test("overlap warning follows a persisted save and remains advisory", async ({ page }) => {
  const state = await open(page); await page.goto("/schedule"); const form = await editor(page);
  await form.getByLabel("Work details").fill("Overlap example"); await form.getByRole("button", { name: "Save assignment", exact: true }).click();
  await expect(page.getByText(/Saved with a scheduling overlap/)).toBeVisible();
  expect(state.creates).toHaveLength(1); await expect(page.locator('[data-assignment-id="asg-new-1"]')).toBeVisible();
});

test("planner edits keep sub-minute times unless rescheduled and preserve email preferences", async ({ page }) => {
  const state = await open(page); state.rows[0] = { ...state.rows[0], notify_assignee: false, reminders_enabled: false, client_notice_enabled: false, client_notice_hours_before: 5 };
  await page.goto("/schedule"); await card(page).getByRole("button", { name: "Edit", exact: true }).click();
  const form = page.getByRole("form", { name: "Assignment editor" });
  await expect(form.getByLabel("Email the assigned person about changes")).not.toBeChecked();
  await expect(form.getByLabel("Customer arrival email", { exact: true })).toHaveValue("off");
  await expect(form.getByLabel("Customer arrival notice (this assignment)", { exact: true })).toHaveValue("5");
  await form.getByLabel("Work details").fill("Updated instructions"); await form.getByRole("button", { name: "Save changes", exact: true }).click();
  await expect(page.getByText("Assignment updated.", { exact: true })).toBeVisible();
  expect(state.patches[0]).toMatchObject({ note: "Updated instructions", expected_revision: 1, notify_assignee: false, reminders_enabled: false, client_notice_enabled: false, client_notice_hours_before: 5 });
  expect(state.patches[0]).not.toHaveProperty("starts_at"); expect(state.patches[0]).not.toHaveProperty("ends_at"); expect(state.patches[0]).not.toHaveProperty("time_zone");
  await card(page).getByRole("button", { name: "Edit", exact: true }).click();
  await form.getByLabel("Start date", { exact: true }).fill("2026-10-25"); await form.getByLabel("All day", { exact: true }).check();
  await form.getByRole("button", { name: "Save changes", exact: true }).click(); await expect(page.getByText("Assignment updated.", { exact: true })).toBeVisible();
  expect(state.patches[1]).toMatchObject({ expected_revision: 2, all_day: true, starts_at: "2026-10-24T21:00:00.000Z", ends_at: "2026-10-25T22:00:00.000Z", time_zone: "Europe/Riga" });
  await expect(card(page).getByText("All day", { exact: true })).toBeVisible();
});

test("status changes wait for authoritative readback and use the observed revision", async ({ page }) => {
  const state = await open(page); await page.goto("/schedule"); await expect(card(page)).toBeVisible();
  let release: () => void = () => {}; const gate = new Promise<void>((resolve) => { release = resolve; });
  state.beforeRead = () => gate;
  await card(page).getByRole("button", { name: "Confirm", exact: true }).click();
  await expect.poll(() => state.transitions.length).toBe(1);
  expect(state.transitions[0]).toEqual({ status: "confirmed", expected_revision: 1 });
  await expect(page.getByText("Assignment status updated.", { exact: true })).toHaveCount(0);
  await expect(card(page).getByRole("button", { name: "Mark done", exact: true })).toBeDisabled();
  release(); state.beforeRead = undefined;
  await expect(page.getByRole("region", { name: "Confirmed assignments" }).locator('[data-assignment-id="asg-1"]')).toBeVisible();
  await card(page).getByRole("button", { name: "Mark done", exact: true }).click();
  await expect(page.getByRole("region", { name: "Done assignments" }).locator('[data-assignment-id="asg-1"]')).toBeVisible();
  expect(state.transitions[1]).toEqual({ status: "done", expected_revision: 2 });
  await expect(card(page).getByRole("button", { name: "Edit", exact: true })).toHaveCount(0);
});

test("an assignee sees project context and only their legal status controls", async ({ page }) => {
  const state = await open(page, { planner: false }); await page.goto("/schedule");
  await expect(card(page).getByRole("heading", { name: "Won contract" })).toBeVisible();
  await expect(page.getByRole("button", { name: "New assignment", exact: true })).toHaveCount(0);
  await expect(page.getByLabel("Filter by team member")).toHaveCount(0); await expect(card(page).getByRole("button", { name: "Edit", exact: true })).toHaveCount(0);
  await expect(card(page).getByRole("button", { name: "Cancel assignment", exact: true })).toHaveCount(0);
  await card(page).getByRole("button", { name: "Confirm", exact: true }).click();
  await expect(card(page).getByText("Confirmed", { exact: true })).toBeVisible(); expect(state.transitions[0]).toEqual({ status: "confirmed", expected_revision: 1 });
});

test("a planner access refusal preserves personal view while a failed team load offers recovery", async ({ page }) => {
  const state = await open(page, { memberStatus: 503 }); await page.goto("/schedule");
  await expect(page.getByText("Couldn’t load the planning team", { exact: true })).toBeVisible();
  await expect(page.getByText(/Planning access is unavailable/)).toHaveCount(0);
  await expect(page.getByRole("button", { name: "New assignment", exact: true })).toBeDisabled();
  state.memberStatus = 200; await page.getByRole("alert").filter({ hasText: "Couldn’t load the planning team" }).getByRole("button", { name: "Try again", exact: true }).click();
  await expect(page.getByRole("button", { name: "New assignment", exact: true })).toBeEnabled();
  state.memberStatus = 403; await page.reload();
  await expect(page.getByText(/Planning access is unavailable/)).toBeVisible();
  await expect(card(page)).toBeVisible(); await expect(page.getByRole("button", { name: "New assignment", exact: true })).toHaveCount(0);
});

test("a stale edit cannot overwrite another planner until explicitly reloaded", async ({ page }) => {
  const state = await open(page); state.conflict = true; await page.goto("/schedule");
  await card(page).getByRole("button", { name: "Edit", exact: true }).click(); const form = page.getByRole("form", { name: "Assignment editor" });
  await form.getByLabel("Work details").fill("My stale draft"); await form.getByRole("button", { name: "Save changes", exact: true }).click();
  await expect(page.getByText(/This assignment changed since you opened it/)).toBeVisible(); await expect(form.getByRole("button", { name: "Save changes", exact: true })).toBeDisabled();
  await expect(form.getByLabel("Work details")).toHaveValue("My stale draft"); expect(state.patches).toHaveLength(1);
  await form.getByRole("button", { name: "Reload latest values", exact: true }).click();
  await expect(form.getByLabel("Work details")).toHaveValue("Another planner updated this");
  await form.getByLabel("Work details").fill("Reviewed update"); await form.getByRole("button", { name: "Save changes", exact: true }).click();
  await expect(page.getByText("Assignment updated.", { exact: true })).toBeVisible(); expect(state.patches[1].expected_revision).toBe(2);
});

test("a committed save with failed readback offers GET recovery without duplicate creation", async ({ page }) => {
  const state = await open(page); await page.goto("/schedule"); const form = await editor(page); state.failReads = true;
  await form.getByRole("button", { name: "Save assignment", exact: true }).click();
  const check = page.getByRole("button", { name: "Check assignment outcome", exact: true });
  await expect(check).toBeEnabled();
  await expect(form.getByRole("button", { name: "Save assignment", exact: true })).toBeDisabled();
  expect(state.creates).toHaveLength(1);
  state.failReads = false; await check.click();
  await expect(page.getByRole("form", { name: "Assignment editor" })).toHaveCount(0);
  await expect(page.locator('[data-assignment-id="asg-new-1"]')).toBeVisible(); expect(state.creates).toHaveLength(1);
});

test("a lost keyed create response retains the frozen draft and only checks its original receipt", async ({ page }) => {
  const state = await open(page); state.lostCreateResponse = true; await page.goto("/schedule");
  const form = await editor(page); await form.getByLabel("Work details").fill("Uncertain saved work");
  await form.getByRole("button", { name: "Save assignment", exact: true }).click();
  await expect(page.getByRole("button", { name: "Check assignment outcome", exact: true })).toBeEnabled();
  await expect(form.getByLabel("Work details")).toHaveValue("Uncertain saved work");
  await expect(form.getByRole("button", { name: "Save assignment", exact: true })).toBeDisabled();
  await expect(page.getByRole("button", { name: "New assignment", exact: true })).toBeDisabled();
  await page.getByRole("button", { name: "Check assignment outcome", exact: true }).click();
  await expect(page.locator('[data-assignment-id="asg-new-1"]').getByText("Uncertain saved work")).toBeVisible();
  expect(state.creates).toHaveLength(1);
  await expect(form).toHaveCount(0);
  await expect(page.getByRole("button", { name: "New assignment", exact: true })).toBeEnabled();
});

for (const status of [201, 500]) {
  test(`a late save response (${status}) cannot expose the previous workspace draft or notice`, async ({ page }) => {
    const state = await open(page); state.holdCreate = true; await page.goto("/schedule");
    const form = await editor(page); await form.getByLabel("Work details").fill("PRIVATE workspace A draft");
    await form.getByRole("button", { name: "Save assignment", exact: true }).click();
    await expect.poll(() => !!state.delayedCreate).toBe(true);
    state.identityB = true; state.rows = [];
    await page.evaluate(() => {
      localStorage.setItem("invoiceiq_token", "token-B");
      window.dispatchEvent(new StorageEvent("storage", { key: "invoiceiq_token", newValue: "token-B", storageArea: localStorage }));
    });
    await expect(page.getByText("Owner B", { exact: true })).toBeVisible();
    await expect(page.getByRole("form", { name: "Assignment editor" })).toHaveCount(0);
    await expect(page.getByText("No assignments in this week", { exact: true })).toBeVisible();
    await state.delayedCreate!.fulfill({ status, contentType: "application/json", body: JSON.stringify(status === 201 ? { assignment: { ...ASSIGNMENT, id: "PRIVATE-old-save" }, overlaps: [ASSIGNMENT] } : { detail: "PRIVATE workspace A failure" }) }).catch(() => undefined);
    await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
    await expect(page.getByText(/PRIVATE|scheduling overlap|Assignment saved\./)).toHaveCount(0);
    await expect(page.getByRole("button", { name: "New assignment", exact: true })).toBeEnabled();
  });
}

test("cancellation requires explicit confirmation and closes the same assignment", async ({ page }) => {
  const state = await open(page); await page.goto("/schedule");
  await card(page).getByRole("button", { name: "Cancel assignment", exact: true }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Keep assignment", exact: true }).click();
  expect(state.transitions).toHaveLength(0);
  await card(page).getByRole("button", { name: "Cancel assignment", exact: true }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Cancel assignment", exact: true }).click();
  await expect(page.getByRole("region", { name: "Cancelled assignments" }).locator('[data-assignment-id="asg-1"]')).toBeVisible();
  expect(state.transitions[0]).toEqual({ status: "cancelled", expected_revision: 1 });
  await expect(card(page).getByRole("button", { name: "Mark done", exact: true })).toHaveCount(0);
});

test("ICS explains polling and secret access, with copy failure and replacement recovery", async ({ page }) => {
  const state = await open(page); state.feedFailures = 1;
  await page.addInitScript(() => Object.defineProperty(navigator, "clipboard", { value: { writeText: async () => { throw new Error("Clipboard denied"); } } }));
  await page.goto("/schedule"); await page.getByRole("button", { name: "Set up subscription", exact: true }).click();
  await expect(page.getByText(/refresh schedule controls updates, which can take hours/)).toBeVisible(); await expect(page.getByText(/Anyone with this secret link/)).toBeVisible();
  await page.getByRole("button", { name: "Copy link", exact: true }).click(); await expect(page.getByText(/Couldn’t copy the link/)).toBeVisible(); await expect(page.getByRole("button", { name: "Copied", exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "Replace link", exact: true }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Replace link", exact: true }).click();
  await expect(page.getByText("Unable to replace link", { exact: true })).toBeVisible(); await expect(page.getByText(/calendar\/feed\/tok-OLD\.ics/)).toBeVisible();
  await page.getByRole("button", { name: "Replace link", exact: true }).click(); await page.getByRole("dialog").getByRole("button", { name: "Replace link", exact: true }).click();
  await expect(page.getByText(/calendar\/feed\/tok-NEW\.ics/)).toBeVisible(); await expect(page.getByText(/tok-OLD/)).toHaveCount(0); expect(state.regenerated).toBe(1);
});

test("planning remains usable on a narrow screen without page overflow", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 }); await open(page); await page.goto("/schedule"); await expect(card(page)).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("planning-mobile-board.png"), fullPage: true });
  await page.getByRole("tab", { name: "Calendar", exact: true }).click(); await expect(card(page)).toBeVisible();
  const calendar = page.getByRole("region", { name: "Weekly calendar", exact: true });
  await calendar.focus(); await page.keyboard.press("ArrowRight");
  await expect.poll(() => calendar.evaluate((el) => el.scrollLeft)).toBeGreaterThan(0);
  await card(page).scrollIntoViewIfNeeded();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("planning-mobile-calendar.png"), fullPage: true });
  const text = (await page.locator("main").innerText()).toLowerCase();
  for (const word of ["cargo", "fuel", "vehicle", "driver", "truck", "site crew"]) expect(text).not.toContain(word);
});
