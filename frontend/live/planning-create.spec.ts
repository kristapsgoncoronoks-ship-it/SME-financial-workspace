import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { writeFileSync } from "node:fs";
import { expect, test } from "@playwright/test";

test.use({ timezoneId: "Europe/Riga" });
function uuid(value: string): string {
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value)) throw new Error("Expected a synthetic UUID");
  return value;
}
function sql(statement: string, orgId: string): string {
  return execFileSync("psql", ["-X", "-q", "-v", "ON_ERROR_STOP=1", "-At", "-c",
    `SET app.current_org='${uuid(orgId)}'; ${statement}`], { encoding: "utf8", env: process.env }).trim();
}
function snapshot(orgId: string) {
  const tables = ["project_assignments", "planning_commands", "email_messages", "jobs", "audit_events", "calendar_event_mappings", "calendar_connections"];
  return Object.fromEntries(tables.map((table) => [table, JSON.parse(sql(
    `SELECT COALESCE(json_agg(t ORDER BY t.id), '[]'::json) FROM ${table} t WHERE t.org_id='${uuid(orgId)}'`, orgId,
  )) as Array<Record<string, unknown>>]));
}

test("native planning creation recovers a committed lost response after later editing without duplicating its frozen outcome or queued intent", async ({ page }, info) => {
  // Other live files share the real per-IP20/minute auth guard. Keep a complete
  // fresh window without changing the limiter, retries or client address.
  test.setTimeout(test.info().timeout + 61_000);
  await new Promise((resolve) => setTimeout(resolve, 30_500));
  await new Promise((resolve) => setTimeout(resolve, 30_500));
  await expect.poll(async () => {
    try { return (await page.request.get("http://127.0.0.1:8000/health")).status(); } catch { return 0; }
  }, { timeout: 30_000 }).toBe(200);
  const email = `planning-live-${randomUUID()}@example.com`, password = "synthetic-planning-test-password";
  const registration = await page.request.post("/api/v1/auth/register", {
    data: { organization_name: "Synthetic native planning", name: "Synthetic native planner", email, password },
  });
  expect(registration.status()).toBe(201);
  const identity = await registration.json(), orgId = uuid(identity.organization.id), userId = uuid(identity.user.id);
  const headers = { Authorization: `Bearer ${identity.token.access_token}` };
  const projectResponse = await page.request.post("/api/v1/masters/projects", { headers, data: { code: "SYN-PLAN", name: "Synthetic native planned work" } });
  expect(projectResponse.status()).toBe(201);
  const projectId = uuid((await projectResponse.json()).id);

  await page.goto("/login?next=/schedule");
  await page.getByLabel("Email", { exact: true }).fill(email);
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Work planning", exact: true })).toBeVisible();
  // Two weeks keeps the 24 h reminder due moment in the future on every weekday.
  for (let week = 0; week < 2; week++) await page.getByRole("button", { name: "Next week →", exact: true }).click();
  await page.getByRole("button", { name: "New assignment", exact: true }).click();
  const editor = page.getByRole("form", { name: "Assignment editor", exact: true });
  const date = await editor.getByLabel("Start date", { exact: true }).inputValue();
  const window = await page.evaluate((day) => ({ starts_at: new Date(`${day}T09:00:00`).toISOString(), ends_at: new Date(`${day}T17:00:00`).toISOString(), time_zone: Intl.DateTimeFormat().resolvedOptions().timeZone }), date);
  // This declared legacy assignment supplies a real advisory overlap. No email,
  // reminder or provider delivery is requested by this setup call.
  const overlapResponse = await page.request.post("/api/v1/schedule/assignments", { headers,
    data: { project_id: projectId, assignee_user_id: userId, ...window, all_day: false, note: "Declared synthetic overlap setup",
      remind_hours_before: null, client_notice_hours_before: null, notify_assignee: false, reminders_enabled: false, client_notice_enabled: false },
  });
  expect(overlapResponse.status()).toBe(201);
  const overlapId = uuid((await overlapResponse.json()).assignment.id);

  let key = "", assignmentId = "";
  let originalPayload: Record<string, unknown> | null = null;
  let originalResult: { assignment: Record<string, unknown>; overlaps: Array<Record<string, unknown>> } | null = null;
  const browserRequests: Array<{ method: string; path: string; key?: string }> = [];
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (path.startsWith("/api/v1/schedule/")) browserRequests.push({ method: request.method(), path, key: request.headers()["idempotency-key"] });
  });
  await page.route("**/api/v1/schedule/assignments", async (route) => {
    if (route.request().method() !== "POST") { await route.continue(); return; }
    key = uuid(route.request().headers()["idempotency-key"]);
    originalPayload = route.request().postDataJSON();
    expect(originalPayload).toMatchObject({ project_id: projectId, assignee_user_id: userId, ...window,
      all_day: false, note: "Synthetic original native work", remind_hours_before: null, client_notice_hours_before: null,
      notify_assignee: true, reminders_enabled: true, client_notice_enabled: null });
    const actual = await route.fetch({ maxRetries: 0 }); expect(actual.status()).toBe(201);
    originalResult = await actual.json(); assignmentId = uuid(String(originalResult!.assignment.id));
    expect(originalResult!.overlaps.map((row) => row.id)).toEqual([overlapId]);
    await route.abort("failed"); // Real committed server state; only its response is lost.
  });
  await editor.getByLabel("Work details", { exact: true }).fill("Synthetic original native work");
  await editor.getByRole("button", { name: "Save assignment", exact: true }).click();
  const check = page.getByRole("button", { name: "Check assignment outcome", exact: true });
  await expect.poll(() => originalResult).not.toBeNull(); await expect(check).toBeEnabled();
  await page.unroute("**/api/v1/schedule/assignments");
  const slot = `invoiceiq:command:${userId}:${orgId}:/schedule/assignments`;
  expect(await page.evaluate((value) => sessionStorage.getItem(value), slot)).not.toBeNull();
  const committed = snapshot(orgId);
  expect(committed.project_assignments).toHaveLength(2); expect(committed.planning_commands).toHaveLength(1);
  expect(committed.planning_commands[0]).toMatchObject({ actor_user_id: userId, command_key: key, assignment_id: assignmentId });
  expect(JSON.parse(String(committed.planning_commands[0].result_json))).toEqual(originalResult);
  expect(committed.audit_events.filter((event) => event.action === "assignment.create" && JSON.parse(String(event.meta)).assignment_id === assignmentId)).toHaveLength(1);
  const messages = committed.email_messages.filter((message) => String(message.command_key).startsWith(`assignment:${assignmentId}:`));
  expect(messages).toHaveLength(1); expect(messages[0].status).toBe("blocked");
  expect(committed.jobs).toHaveLength(1); expect(committed.jobs[0].kind).toBe("assignment.reminder");
  expect(committed.calendar_connections).toEqual([]); expect(committed.calendar_event_mappings).toEqual([]);

  // A later ordinary edit must not replace the original create receipt. The UI
  // then reads the current revision separately before releasing the create key.
  const edit = await page.request.patch(`/api/v1/schedule/assignments/${assignmentId}`, { headers,
    data: { note: "Later native reviewed edit", expected_revision: 1 },
  });
  expect(edit.status()).toBe(200); expect((await edit.json()).assignment.revision).toBe(2);
  const edited = snapshot(orgId);
  await page.goto("/dashboard"); await page.goto("/schedule"); await page.reload();
  await expect(editor.getByLabel("Work details", { exact: true })).toHaveValue("Synthetic original native work");
  await expect(editor.getByLabel("Work details", { exact: true })).toBeDisabled();
  const recoveryStart = browserRequests.length;
  await check.click();
  await expect.poll(() => page.evaluate((value) => sessionStorage.getItem(value), slot)).toBeNull();
  await expect(page.getByText("Assignment created. Its details have changed since the original save; review the current plan below.", { exact: true })).toBeVisible();
  await expect(page.getByText(/Saved with a scheduling overlap/)).toBeVisible();
  expect(browserRequests.slice(recoveryStart).every((request) => request.method === "GET")).toBe(true);
  expect(browserRequests.slice(recoveryStart).some((request) => request.path === `/api/v1/schedule/commands/${key}`)).toBe(true);
  expect(browserRequests.filter((request) => request.method === "POST")).toEqual([{ method: "POST", path: "/api/v1/schedule/assignments", key }]);
  for (let week = 0; week < 2; week++) await page.getByRole("button", { name: "Next week →", exact: true }).click();
  await expect(page.locator(`[data-assignment-id="${assignmentId}"]`).getByText("Later native reviewed edit", { exact: true })).toBeVisible();
  expect(snapshot(orgId)).toEqual(edited);

  const replay = await page.request.post("/api/v1/schedule/assignments", { headers: { ...headers, "Idempotency-Key": key }, data: originalPayload });
  expect(replay.status()).toBe(201); expect(await replay.json()).toEqual(originalResult);
  const receipt = await page.request.get(`/api/v1/schedule/commands/${key}`, { headers });
  expect(receipt.status()).toBe(200);
  const originalReceipt = await receipt.json();
  expect(originalReceipt).toMatchObject({ org_id: orgId, actor_user_id: userId, command_key: key, operation: "assignment_created" });
  expect(originalReceipt.result).toEqual(originalResult);
  expect(snapshot(orgId)).toEqual(edited);
  const evidencePath = info.outputPath("native-planning-create-outcome.json");
  writeFileSync(evidencePath, JSON.stringify({
    scope: "Real browser/API/PostgreSQL creation and recovery. Declared legacy overlap setup; real later API edit; blocked SMTP and queued reminder only; no external provider action.",
    org_id: orgId, assignment_id: assignmentId, original_key: key, original_payload: originalPayload, original_result: originalResult,
    browser_requests: browserRequests, committed_before_edit: committed, edited_before_read_only_recovery: edited,
  }, null, 2) + "\n");
  await info.attach("native-planning-create-outcome", { path: evidencePath, contentType: "application/json" });
});
