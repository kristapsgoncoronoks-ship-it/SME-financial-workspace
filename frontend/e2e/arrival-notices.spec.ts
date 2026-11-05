import { test, expect, type Page, type Route } from "@playwright/test";

/**
 * Client arrival notices (WO-E) over the LIVE app shell, API mocked via
 * `page.route` (the suite's standard pattern).
 *
 * What earns its place here:
 *  - Settings → Schedule notices: the card renders both controls, and picking
 *    a customer-notice lead PUTs {client_notice_hours}; picking Off sends
 *    {clear_client_notice_hours};
 *  - the assignment form carries the per-assignment override and posts
 *    explicit inherited timing or the chosen client_notice_hours_before;
 *  - the project page's Customer card lists customers, flags one without an
 *    email, and PUTs the link;
 *  - copy is industry-neutral (owner guard list).
 *
 * Synthetic fixtures only.
 */

const ORG = { id: "org-1", name: "Test Workspace", status: "active" };

interface MockOpts {
  onScheduleSettingsPut?: (body: Record<string, unknown>) => void;
  onAssignmentCreate?: (body: Record<string, unknown>) => void;
  onCustomerLink?: (body: Record<string, unknown>) => void;
}

async function open(page: Page, path: string, opts: MockOpts = {}) {
  await page.addInitScript(() => localStorage.setItem("invoiceiq_token", "e2e-token"));
  await page.clock.setFixedTime(new Date("2026-09-02T10:00:00Z"));

  const json = (body: unknown, code = 200) => ({
    status: code,
    contentType: "application/json",
    body: JSON.stringify(body),
  });

  const planningReceipts: Record<string, { payload: Record<string, unknown>; result: { assignment: Record<string, unknown>; overlaps: unknown[] } }> = {};
  const planningRows: Record<string, unknown>[] = [];

  let scheduleSettings: Record<string, unknown> = {
    assignment_remind_hours: null,
    client_notice_hours: null,
  };

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
    if (p === "/auth/organizations") return route.fulfill(json([{ org_id: ORG.id, name: ORG.name, role: "owner", current: true }]));
    if (p === "/modules") return route.fulfill(json([]));

    if (p === "/settings/schedule") {
      if (method === "PUT") {
        const body = route.request().postDataJSON();
        opts.onScheduleSettingsPut?.(body);
        scheduleSettings = {
          assignment_remind_hours: body.clear_assignment_remind_hours
            ? null
            : (body.assignment_remind_hours ?? scheduleSettings.assignment_remind_hours),
          client_notice_hours: body.clear_client_notice_hours
            ? null
            : (body.client_notice_hours ?? scheduleSettings.client_notice_hours),
        };
      }
      return route.fulfill(json(scheduleSettings));
    }
    if (p === "/settings/lifecycle")
      return route.fulfill(json({ offer_prefix: null, final_invoice_requires_acceptance: false }));
    // The rest of the settings page: array-shaped feeds must be arrays or the
    // sibling cards crash the page before Schedule notices renders.
    if (p === "/jobs" || p === "/webhooks") return route.fulfill(json([]));
    if (p === "/settings/validation")
      return route.fulfill(json({ ai_validation_enabled: false, human_validation_enabled: false }));
    if (p === "/retention") return route.fulfill(json({ categories: [], holds: [] }));
    if (p === "/sso/connection") return route.fulfill(json(null));
    if (p === "/sso/assignable-roles") return route.fulfill(json(["user", "admin"]));

    if (p === "/schedule/members")
      return route.fulfill(json([{ user_id: "user-1", email: "someone@test.io", name: "Test User" }]));
    if (p === "/schedule/assignments") {
      if (method === "POST") {
        const payload = route.request().postDataJSON(); opts.onAssignmentCreate?.(payload);
        const assignment = { ...payload, id: `asg-new-${planningRows.length + 1}`, assignee_email: "someone@test.io", created_by: "someone@test.io", revision: 1, status: "planned", notification_status: "queued" };
        const result = { assignment, overlaps: [] };
        planningRows.push(assignment);
        planningReceipts[route.request().headers()["idempotency-key"]] = { payload, result };
        return route.fulfill(json(result, 201));
      }
      return route.fulfill(json(planningRows));
    }
    if (p.startsWith("/schedule/commands/")) {
      const key = p.slice("/schedule/commands/".length), receipt = planningReceipts[key];
      return route.fulfill(receipt ? json({ id: "planning-receipt", org_id: ORG.id, actor_user_id: "user-1", status: "committed", operation: "assignment_created", command_key: key, target_id: receipt.result.assignment.id, ...receipt }) : json({ detail: "No committed outcome" }, 404));
    }
    if (p.startsWith("/schedule/assignments/asg-new-")) return route.fulfill(json(planningRows.find((row) => p.endsWith(String(row.id)))));
    if (p === "/schedule/feed-token")
      return route.fulfill(json({ token: "tok-1", path: "/api/v1/calendar/feed/tok-1.ics" }));

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
    if (p === "/masters/projects/proj-1/customer" && method === "PUT") {
      opts.onCustomerLink?.(route.request().postDataJSON());
      return route.fulfill(
        json({
          id: "proj-1",
          code: "JOB-7",
          name: "Won contract",
          status: "active",
          version: 1,
          customer_id: "cust-1",
        }),
      );
    }
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
    if (p === "/masters/projects/proj-1/cost-entries") return route.fulfill(json([]));
    if (p === "/masters/projects/proj-1/documents") return route.fulfill(json([]));
    if (p === "/masters/projects/proj-1/offers") return route.fulfill(json([]));
    if (p === "/masters/projects/proj-1/invoicing-plan")
      return route.fulfill(json({ rows: [], contracted: "0.00", issued: "0.00", remaining: "0.00" }));
    if (p === "/customers")
      return route.fulfill(
        json([
          { id: "cust-1", name: "Riverside Office", email: "reception@riverside.example" },
          { id: "cust-2", name: "Walk-in", email: null },
        ]),
      );

    return route.fulfill(json({ items: [], total: 0 }));
  });

  await page.goto(path);
}

test("schedule-notice settings render and PUT the picked lead / the clear", async ({ page }) => {
  const puts: Record<string, unknown>[] = [];
  await open(page, "/settings", { onScheduleSettingsPut: (b) => puts.push(b) });

  await expect(page.getByRole("heading", { name: "Schedule notices" })).toBeVisible();
  const select = page.locator("select").filter({ hasText: "Off (default)" }).first();
  await select.selectOption("48");
  await expect.poll(() => puts.length).toBe(1);
  expect(puts[0]).toEqual({ client_notice_hours: 48 });

  await select.selectOption("");
  await expect.poll(() => puts.length).toBe(2);
  expect(puts[1]).toEqual({ clear_client_notice_hours: true });
});

test("the assignment form freezes inherited null timing and an explicit per-assignment notice override", async ({
  page,
}) => {
  const posts: Record<string, unknown>[] = [];
  await open(page, "/schedule", { onAssignmentCreate: (b) => posts.push(b) });

  await page.getByRole("button", { name: "New assignment", exact: true }).click();
  await page.getByRole("button", { name: "Save assignment" }).click();
  await expect.poll(() => posts.length).toBe(1);
  expect(posts[0]).toMatchObject({ client_notice_hours_before: null });
  await expect(page.getByText("Assignment saved.", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: "New assignment", exact: true }).click();
  await page
    .getByLabel("Customer arrival notice (this assignment)", { exact: true })
    .selectOption("48");
  await page.getByRole("button", { name: "Save assignment" }).click();
  await expect.poll(() => posts.length).toBe(2);
  expect(posts[1]).toMatchObject({ client_notice_hours_before: 48 });
});

test("the project customer card links a customer and flags a missing email", async ({ page }) => {
  const links: Record<string, unknown>[] = [];
  await open(page, "/projects/proj-1", { onCustomerLink: (b) => links.push(b) });

  await expect(page.getByRole("heading", { name: "Customer" })).toBeVisible();
  const select = page.locator("select").filter({ hasText: "No customer linked" }).first();
  await expect(select.locator("option", { hasText: "no email — notices can’t send" })).toHaveCount(
    1,
  );

  await select.selectOption("cust-1");
  await expect.poll(() => links.length).toBe(1);
  expect(links[0]).toEqual({ customer_id: "cust-1" });
});

test("the arrival-notice copy is industry-neutral", async ({ page }) => {
  await open(page, "/settings", {});
  await expect(page.getByRole("heading", { name: "Schedule notices" })).toBeVisible();
  const text = (await page.locator("body").innerText()).toLowerCase();
  for (const word of ["cargo", "fuel", "vehicle", "driver", "truck", "site crew"]) {
    expect(text).not.toContain(word);
  }
});
