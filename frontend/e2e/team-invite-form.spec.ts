import { test, expect, type Page } from "@playwright/test";

type Wire = { posts: unknown[]; invites: { id: string; email: string; role: string; token: string }[]; status: number; wait?: Promise<void>; reads: number };
async function setup(page: Page, canManage = true): Promise<Wire> {
  const wire: Wire = { posts: [], invites: [], status: 201, reads: 0 };
  await page.addInitScript(() => localStorage.setItem("invoiceiq_token", "team-form-synthetic-token"));
  await page.route("**/api/v1/**", async route => {
    const request = route.request(), path = new URL(request.url()).pathname.replace("/api/v1", "");
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    const organization = { id: "team-form-org", name: "Synthetic invitation workspace", status: "active" };
    const user = { id: "team-form-user", org_id: organization.id, name: "Synthetic invitation operator", email: "operator@example.test", role: canManage ? "owner" : "auditor" };
    if (path === "/auth/me") return json({ user, organization, permissions: ["member.read", ...(canManage ? ["member.manage"] : [])] });
    if (path === "/auth/organizations") return json([{ org_id: organization.id, name: organization.name, current: true }]);
    if (path === "/modules" || path === "/team/members") return json([]);
    if (path === "/team/invites" && request.method() === "GET") { wire.reads++; return json(wire.invites); }
    if (path === "/team/invites" && request.method() === "POST") {
      const body = request.postDataJSON(); wire.posts.push(body); await wire.wait;
      if (wire.status !== 201) return json({ detail: "Synthetic invitation refused" }, wire.status);
      const invite = { id: "team-form-invite", email: body.email, role: body.role, token: "synthetic-invitation-token" };
      wire.invites = [invite]; return json(invite, 201);
    }
    return json([]);
  });
  await page.goto("/team");
  await expect(page.getByRole("heading", { name: "Team", exact: true })).toBeVisible();
  return wire;
}
const form = (page: Page) => page.getByRole("form", { name: "Invite a member", exact: true });

test("native email validation blocks invalid input and valid Enter submits the unchanged email and role", async ({ page }) => {
  const wire = await setup(page);
  const email = page.getByLabel("Email", { exact: true });
  const submit = form(page).getByRole("button", { name: "Create invite", exact: true });
  await expect(submit).toBeDisabled();
  await email.fill("not-an-email"); await submit.click();
  await expect(email).toBeFocused();
  expect(await email.evaluate(e => (e as HTMLInputElement).validity.typeMismatch)).toBe(true);
  await page.keyboard.press("Enter"); await page.waitForLoadState("networkidle");
  expect(wire.posts).toEqual([]);
  await email.fill("Person+review@Example.test");
  await page.getByLabel("Role", { exact: true }).selectOption("accountant");
  await email.focus(); await page.keyboard.press("Enter");
  await expect(page.getByLabel("Invitation link", { exact: true })).toHaveValue(/\/accept-invite\?token=synthetic-invitation-token$/);
  expect(wire.posts).toEqual([{ email: "Person+review@Example.test", role: "accountant" }]);
  await expect(email).toHaveValue("");
  await expect(page.getByLabel("Role", { exact: true })).toHaveValue("accountant");
});

test("pending invitation preserves its disabled guard against a second native submission", async ({ page }) => {
  const wire = await setup(page);
  let release!: () => void; wire.wait = new Promise<void>(resolve => { release = resolve; });
  try {
    const email = page.getByLabel("Email", { exact: true });
    await email.fill("pending@example.test"); await email.press("Enter");
    const submit = form(page).getByRole("button", { name: "Create invite", exact: true });
    await expect(submit).toBeDisabled(); await expect(submit).toHaveAttribute("aria-busy", "true");
    await email.focus(); await page.keyboard.press("Enter");
    expect(wire.posts).toEqual([{ email: "pending@example.test", role: "user" }]);
    release();
    await expect(page.getByLabel("Invitation link", { exact: true })).toBeVisible();
    expect(wire.posts).toHaveLength(1);
  } finally { release(); }
});

test("server refusal retains reviewed fields and explicit Enter retry uses the same payload before readback", async ({ page }) => {
  const wire = await setup(page); wire.status = 409;
  const email = page.getByLabel("Email", { exact: true });
  await email.fill("refused@example.test"); await email.press("Enter");
  await expect(page.getByRole("alert").filter({ hasText: "Synthetic invitation refused" })).toBeVisible();
  await expect(page.getByLabel("Invitation link", { exact: true })).toHaveCount(0);
  await expect(email).toHaveValue("refused@example.test");
  wire.status = 201; await email.press("Enter");
  await expect(page.getByLabel("Invitation link", { exact: true })).toBeVisible();
  await expect(page.getByRole("alert").filter({ hasText: "Synthetic invitation refused" })).toHaveCount(0);
  expect(wire.posts).toEqual([{ email: "refused@example.test", role: "user" }, { email: "refused@example.test", role: "user" }]);
  expect(wire.reads).toBeGreaterThanOrEqual(2);
});

test("a reader does not gain the invitation form or its write capability", async ({ page }) => {
  const wire = await setup(page, false);
  await expect(page.getByRole("heading", { name: "Team", exact: true })).toBeVisible();
  await expect(form(page)).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Create invite", exact: true })).toHaveCount(0);
  expect(wire.posts).toEqual([]); expect(wire.reads).toBe(0);
});
