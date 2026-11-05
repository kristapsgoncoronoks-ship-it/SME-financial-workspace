import { test, expect, type Page, type Route } from "@playwright/test";

// Synthetic browser fixtures only. These checks prove local read recovery and
// request boundaries; actual PostgreSQL/session acceptance is recorded separately.
type Account = "A" | "B";
type Reply =
  | { kind: "valid" }
  | { kind: "hold" }
  | { kind: "offline" }
  | { kind: "response"; body: unknown; status?: number; headers?: Record<string, string> };
type Event = { method: string; path: string; account: Account; expectedOrg?: string };

const NAME_A = "Verified North Workspace";
const NAME_B = "Verified South Workspace";
const OTHER_A = "Other North Workspace";
const PRIVATE_ERROR = "PRIVATE diagnostic for the previous organization's internal service";
const wire = (body: unknown, status = 200, headers: Record<string, string> = {}) => ({
  status, contentType: "application/json", body: JSON.stringify(body), headers,
});

async function setup(page: Page, initial: Reply = { kind: "valid" }, nameA = NAME_A, options: { manyChoices?: boolean; emailA?: string } = {}) {
  const replies: Record<Account, Reply> = { A: initial, B: { kind: "valid" } };
  const active = { A: { id: "org-A", name: nameA }, B: { id: "org-B", name: NAME_B } };
  const reads: Record<Account, number> = { A: 0, B: 0 };
  const held: { route: Route; account: Account }[] = [];
  const events: Event[] = [];
  const errors: string[] = [];
  let switchReply: { kind: "valid" | "hold" | "lost" | "rejected" } = { kind: "valid" };
  const heldSwitches: { route: Route; account: Account; selected: { org_id: string; name: string } }[] = [];
  let identityFailure = false;
  page.on("pageerror", error => errors.push(error.message));
  const choices = (account: Account) => [
    { org_id: `org-${account}`, name: account === "A" ? nameA : NAME_B, current: active[account].id === `org-${account}`, role: "owner" },
    { org_id: `org-${account}-other`, name: account === "A" ? OTHER_A : "Other South Workspace", current: active[account].id === `org-${account}-other`, role: "owner" },
    ...(options.manyChoices ? Array.from({ length: 24 }, (_, i) => ({ org_id: `org-${account}-extra-${i}`, name: `Organization ${i + 1} ${"SyntheticUnbrokenCompanyName".repeat(4)}`, current: false, role: "owner" })) : []),
  ];
  const identity = (account: Account) => ({
    user: { id: `user-${account}`, email: account === "A" ? options.emailA ?? "A@example.com" : "B@example.com", name: `Owner ${account}`, role: "owner", org_id: active[account].id },
    organization: { ...active[account], status: "active" },
  });
  await page.addInitScript(() => {
    if (!sessionStorage.getItem("org-recovery-seeded")) {
      localStorage.setItem("invoiceiq_token", "token-A");
      sessionStorage.setItem("org-recovery-seeded", "1");
    }
    (window as unknown as { orgRecoveryDocument: string }).orgRecoveryDocument = crypto.randomUUID();
  });
  await page.route("**/api/v1/**", async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace("/api/v1", "");
    const account = request.headers().authorization === "Bearer token-B" ? "B" : "A";
    events.push({ method: request.method(), path, account, expectedOrg: request.headers()["x-expected-org-id"] });
    if (path === "/auth/me") return route.fulfill(identityFailure ? wire({ detail: PRIVATE_ERROR }, 503) : wire(identity(account)));
    if (path === "/auth/login") return route.fulfill(wire({ ...identity("B"), token: { access_token: "token-B" } }));
    if (path === "/auth/logout") return route.fulfill({ status: 204, body: "" });
    if (path === "/auth/organizations") {
      reads[account]++;
      const reply = replies[account];
      if (reply.kind === "hold") { held.push({ route, account }); return; }
      if (reply.kind === "offline") return route.abort("connectionrefused");
      return route.fulfill(reply.kind === "valid" ? wire(choices(account)) : wire(reply.body, reply.status, reply.headers));
    }
    if (path.startsWith("/auth/switch-org/")) {
      const selected = choices(account).find(row => path.endsWith(`/${row.org_id}`));
      if (!selected) return route.fulfill(wire({ detail: "Organization not found" }, 404));
      if (switchReply.kind === "hold") { heldSwitches.push({ route, account, selected }); return; }
      if (switchReply.kind === "rejected") return route.fulfill(wire({ detail: PRIVATE_ERROR }, 403));
      active[account] = { id: selected.org_id, name: selected.name };
      if (switchReply.kind === "lost") return route.abort("connectionrefused");
      return route.fulfill(wire({ org_id: selected.org_id, name: selected.name, role: "owner" }));
    }
    if (path === "/dashboard") return route.fulfill(wire({ as_of: "2026-10-26", approvals: null, captures: null, payables: null, receivables: null, cash: null }));
    if (path === "/dashboard/onboarding") return route.fulfill(wire({ steps: [], complete: true, dismissed: true }));
    return route.fulfill(wire([]));
  });
  await page.goto("/");
  await expect(page.getByRole("button", { name: "Account menu", exact: true })).toBeVisible();
  return {
    reads: (account: Account = "A") => reads[account], events, errors,
    setSwitch: (kind: typeof switchReply["kind"]) => { switchReply = { kind }; },
    setIdentityFailure: (value: boolean) => { identityFailure = value; },
    heldSwitches: () => heldSwitches.length,
    releaseSwitches: async () => {
      for (const item of heldSwitches.splice(0)) {
        active[item.account] = { id: item.selected.org_id, name: item.selected.name };
        await item.route.fulfill(wire({ org_id: item.selected.org_id, name: item.selected.name, role: "owner" }));
      }
    },
    set: (reply: Reply, account: Account = "A") => { replies[account] = reply; },
    held: (account: Account = "A") => held.filter(item => item.account === account).length,
    release: async (reply: Exclude<Reply, { kind: "hold" }>, account: Account = "A") => {
      const pending = held.filter(item => item.account === account);
      expect(pending.length).toBeGreaterThan(0);
      for (const item of pending) {
        held.splice(held.indexOf(item), 1);
        if (reply.kind === "offline") await item.route.abort("connectionrefused");
        else await item.route.fulfill(reply.kind === "valid" ? wire(choices(account)) : wire(reply.body, reply.status, reply.headers));
      }
    },
  };
}

const visibleText = (page: Page, text: string) => page.getByText(text, { exact: true }).filter({ visible: true });
const retry = (page: Page) => page.getByRole("button", { name: "Retry organizations", exact: true });
const organizationError = (page: Page) => page.getByRole("alert").filter({ hasText: /organization choices|organization requests/i });

test("a held organization read keeps verified scope visible and names its loading state", async ({ page }) => {
  const state = await setup(page, { kind: "hold" });
  await expect.poll(() => state.held()).toBeGreaterThan(0);
  await expect(visibleText(page, NAME_A)).toBeVisible();
  await expect(page.getByRole("status").filter({ hasText: "Loading organization choices" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Switch organization" })).toHaveCount(0);
  await state.release({ kind: "valid" });
  await expect(page.getByRole("button", { name: "Switch organization" })).toBeVisible();
  await expect(page.getByRole("status").filter({ hasText: "Loading organization choices" })).toHaveCount(0);
  expect(state.events.every(event => event.method === "GET")).toBe(true);
  expect(state.errors).toEqual([]);
});

for (const failure of ["server", "offline", "forbidden"] as const) {
  test(`an organization ${failure} failure has local recovery and one held GET retry`, async ({ page }, testInfo) => {
    const initial: Reply = failure === "offline" ? { kind: "offline" } : { kind: "response", body: { detail: PRIVATE_ERROR }, status: failure === "forbidden" ? 403 : 500 };
    const state = await setup(page, initial);
    await expect(organizationError(page)).toBeVisible();
    await expect(visibleText(page, NAME_A)).toBeVisible();
    await expect(page.locator("body")).not.toContainText(PRIVATE_ERROR);
    await expect(page.getByRole("button", { name: "Switch organization" })).toHaveCount(0);
    if (failure === "forbidden") await expect(organizationError(page)).toContainText(/access/i);
    if (failure === "server") await page.screenshot({ path: testInfo.outputPath("desktop-read-error.png"), fullPage: true });
    const before = state.reads();
    state.set({ kind: "hold" });
    await retry(page).click();
    await expect.poll(() => state.held()).toBeGreaterThan(0);
    await expect(retry(page)).toBeDisabled();
    await retry(page).evaluate(button => button.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    expect(state.reads()).toBe(before + 1);
    await expect(visibleText(page, NAME_A)).toBeVisible();
    await state.release({ kind: "valid" });
    await expect(page.getByRole("button", { name: "Switch organization" })).toBeVisible();
    await expect(organizationError(page)).toHaveCount(0);
    expect(state.events.every(event => event.method === "GET")).toBe(true);
    expect(state.errors).toEqual([]);
  });
}

for (const format of ["seconds", "HTTP-date"] as const) {
  test(`organization429 honors ${format} without automatic reads or switch replay`, async ({ page }) => {
    await page.clock.install();
    const state = await setup(page, { kind: "hold" });
    await expect.poll(() => state.held()).toBeGreaterThan(0);
    // Derive an absolute header from the browser at response time. Keep the
    // clock running during React's lazy boot; advance it only after the error.
    const now = await page.evaluate(() => Date.now());
    const header = format === "seconds" ? "60" : new Date(now + 60_000).toUTCString();
    await state.release({ kind: "response", body: { detail: PRIVATE_ERROR }, status: 429, headers: { "Retry-After": header } });
    await expect(organizationError(page)).toContainText("Too many organization requests");
    await expect(visibleText(page, NAME_A)).toBeVisible();
    await expect(retry(page)).toBeDisabled();
    const before = state.reads();
    await retry(page).evaluate(button => button.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    await page.clock.fastForward(1_000);
    await expect(retry(page)).toBeDisabled();
    expect(state.reads()).toBe(before);
    state.set({ kind: "valid" });
    await page.clock.fastForward(60_100);
    await expect(retry(page)).toBeEnabled();
    expect(state.reads()).toBe(before);
    await retry(page).click();
    await expect(page.getByRole("button", { name: "Switch organization" })).toBeVisible();
    expect(state.reads()).toBe(before + 1);
    expect(state.events.every(event => event.method === "GET")).toBe(true);
    expect(state.errors).toEqual([]);
  });
}

for (const header of [undefined, "not-a-date", "9999999999999999999999999999999"]) {
  test(`organization429 with ${header ?? "missing"} Retry-After remains manually recoverable`, async ({ page }) => {
    const state = await setup(page, { kind: "response", body: { detail: PRIVATE_ERROR }, status: 429, headers: header === undefined ? {} : { "Retry-After": header } });
    await expect(organizationError(page)).toContainText("Too many organization requests");
    await expect(retry(page)).toBeEnabled();
    const before = state.reads();
    state.set({ kind: "valid" });
    await retry(page).click();
    await expect(page.getByRole("button", { name: "Switch organization" })).toBeVisible();
    expect(state.reads()).toBe(before + 1);
    expect(state.events.every(event => event.method === "GET")).toBe(true);
    expect(state.errors).toEqual([]);
  });
}

for (const invalid of ["empty", "malformed", "duplicate"] as const) {
  test(`an ${invalid} organization list does not invent a current option and can be read again`, async ({ page }) => {
    const row = { org_id: "org-A", name: "PRIVATE unverified organization name", current: true, role: "owner" };
    const body = invalid === "empty" ? [] : invalid === "malformed" ? { items: [row] } : [row, row];
    const state = await setup(page, { kind: "response", body });
    await expect(organizationError(page)).toBeVisible();
    await expect(visibleText(page, NAME_A)).toBeVisible();
    await expect(page.locator("body")).not.toContainText(row.name);
    await expect(page.getByRole("button", { name: "Switch organization" })).toHaveCount(0);
    const before = state.reads();
    state.set({ kind: "valid" });
    await retry(page).click();
    await expect(page.getByRole("button", { name: "Switch organization" })).toBeVisible();
    expect(state.reads()).toBe(before + 1);
    expect(state.events.every(event => event.method === "GET")).toBe(true);
    expect(state.errors).toEqual([]);
  });
}

test("a current-organization mismatch never relabels scope from the first membership", async ({ page }) => {
  const state = await setup(page, { kind: "response", body: [{ org_id: "org-unexpected", name: "PRIVATE first membership", current: true, role: "owner" }] });
  await expect(visibleText(page, NAME_A)).toBeVisible();
  await expect(page.locator("body")).not.toContainText("PRIVATE first membership");
  await expect(page.getByRole("button", { name: "Switch organization" })).toHaveCount(0);
  const reload = page.getByRole("button", { name: "Reload workspace", exact: true });
  await expect(reload).toBeVisible();
  state.set({ kind: "valid" });
  await reload.click();
  await expect(page.getByRole("button", { name: "Switch organization" })).toBeVisible();
  await expect(visibleText(page, NAME_A)).toBeVisible();
  expect(state.events.every(event => event.method === "GET")).toBe(true);
  expect(state.errors).toEqual([]);
});

test("one valid organization retains the static verified label", async ({ page }) => {
  const state = await setup(page, { kind: "response", body: [{ org_id: "org-A", name: NAME_A, current: true, role: "owner" }] });
  await expect(visibleText(page, NAME_A)).toBeVisible();
  await expect(page.getByRole("button", { name: "Switch organization" })).toHaveCount(0);
  await expect(retry(page)).toHaveCount(0);
  expect(state.events.every(event => event.method === "GET")).toBe(true);
});

test("a recovered picker preserves explicit switch POST and fresh document readback", async ({ page }) => {
  const state = await setup(page, { kind: "response", body: { detail: "Rate limited" }, status: 429 });
  await expect(organizationError(page)).toBeVisible();
  state.set({ kind: "valid" });
  await retry(page).click();
  const picker = page.getByRole("button", { name: "Switch organization" });
  await expect(picker).toBeVisible();
  const before = await page.evaluate(() => (window as unknown as { orgRecoveryDocument: string }).orgRecoveryDocument);
  await picker.click();
  await page.getByRole("menu", { name: "Switch organization" }).getByRole("menuitem", { name: OTHER_A, exact: true }).click();
  await expect(picker).toContainText(OTHER_A);
  expect(await page.evaluate(() => (window as unknown as { orgRecoveryDocument: string }).orgRecoveryDocument)).not.toBe(before);
  const writes = state.events.filter(event => event.method !== "GET");
  expect(writes).toEqual([{ method: "POST", path: "/auth/switch-org/org-A-other", account: "A", expectedOrg: "org-A" }]);
  expect(state.errors).toEqual([]);
});

test("mobile enlarged-text organization recovery fits and supports a native keyboard retry", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const longName = "Verified Northern Commercial and Residential Services Workspace with a deliberately long company name";
  const state = await setup(page, { kind: "response", body: { detail: PRIVATE_ERROR }, status: 403 }, longName);
  await page.evaluate(() => { document.documentElement.style.fontSize = "32px"; });
  await page.getByRole("button", { name: "Open navigation menu", exact: true }).click();
  const drawer = page.getByRole("dialog", { name: "Menu", exact: true });
  await expect(drawer).toBeVisible();
  await expect(drawer.getByText(longName, { exact: true })).toBeVisible();
  const action = drawer.getByRole("button", { name: "Retry organizations", exact: true });
  // Traverse from the drawer's close button using actual Tab input.
  await drawer.getByRole("button", { name: "Close panel", exact: true }).focus();
  for (let i = 0; i < 8 && !(await action.evaluate(el => el === document.activeElement)); i++) await page.keyboard.press("Tab");
  await expect(action).toBeFocused();
  await page.screenshot({ path: testInfo.outputPath("mobile-200-read-error-focused.png"), fullPage: true });
  await testInfo.attach("mobile-200-error-aria", { body: await drawer.ariaSnapshot(), contentType: "text/yaml" });
  state.set({ kind: "valid" });
  await page.keyboard.press("Enter");
  await expect(drawer.getByRole("button", { name: "Switch organization" })).toBeVisible();
  const metrics = await page.evaluate(() => ({ viewport: innerWidth, document: document.documentElement.scrollWidth, scrollX }));
  expect(metrics.document).toBe(metrics.viewport);
  expect(metrics.scrollX).toBe(0);
  expect(state.events.every(event => event.method === "GET")).toBe(true);
  expect(state.errors).toEqual([]);
});

for (const status of [200, 429]) {
  test(`a late previous-account organization response${status} cannot disclose names or impose a cooldown`, async ({ page }) => {
    const state = await setup(page, { kind: "hold" });
    await expect.poll(() => state.held()).toBeGreaterThan(0);
    await page.getByRole("button", { name: "Account menu", exact: true }).click();
    await page.getByRole("menuitem", { name: "Sign out", exact: true }).click();
    await page.getByLabel("Email", { exact: true }).fill("B@example.com");
    await page.getByLabel("Password", { exact: true }).fill("synthetic-password");
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    await expect(visibleText(page, NAME_B)).toBeVisible();
    await expect(page.getByRole("button", { name: "Switch organization" })).toBeVisible();
    const response: Reply = status === 200
      ? { kind: "response", body: [{ org_id: "org-A", name: "PRIVATE previous-account workspace", current: true, role: "owner" }] }
      : { kind: "response", body: { detail: "PRIVATE previous-account failure" }, status: 429, headers: { "Retry-After": "3600" } };
    await state.release(response);
    await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
    await expect(visibleText(page, NAME_B)).toBeVisible();
    await expect(page.locator("body")).not.toContainText("PRIVATE previous-account");
    await expect(page.getByRole("button", { name: "Switch organization" })).toBeVisible();
    await expect(retry(page)).toHaveCount(0);
    expect(state.reads("B")).toBeGreaterThan(0);
    expect(await page.evaluate(() => localStorage.getItem("invoiceiq_token"))).toBe("token-B");
    expect(state.events.filter(event => event.method !== "GET").map(event => event.path)).toEqual(["/auth/logout", "/auth/login"]);
    expect(state.errors).toEqual([]);
  });
}

test("an active previous-account cooldown cannot delay the next account's explicit read retry", async ({ page }) => {
  await page.clock.install();
  const state = await setup(page, { kind: "response", body: { detail: PRIVATE_ERROR }, status: 429, headers: { "Retry-After": "3600" } });
  await expect(organizationError(page)).toContainText("Too many organization requests");
  await expect(retry(page)).toBeDisabled();
  state.set({ kind: "response", body: { detail: "PRIVATE B access detail" }, status: 403 }, "B");
  await page.getByRole("button", { name: "Account menu", exact: true }).click();
  await page.getByRole("menuitem", { name: "Sign out", exact: true }).click();
  await page.getByLabel("Email", { exact: true }).fill("B@example.com");
  await page.getByLabel("Password", { exact: true }).fill("synthetic-password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(visibleText(page, NAME_B)).toBeVisible();
  await expect(organizationError(page)).toContainText(/current access/i);
  await expect(retry(page)).toBeEnabled();
  const before = state.reads("B");
  await page.clock.fastForward(3_600_100);
  expect(state.reads("B")).toBe(before);
  await expect(retry(page)).toBeEnabled();
  state.set({ kind: "valid" }, "B");
  await retry(page).click();
  await expect(page.getByRole("button", { name: "Switch organization" })).toBeVisible();
  expect(state.reads("B")).toBe(before + 1);
  await expect(page.locator("body")).not.toContainText(NAME_A);
  await expect(page.locator("body")).not.toContainText("PRIVATE");
  expect(state.events.filter(event => event.method !== "GET").map(event => event.path)).toEqual(["/auth/logout", "/auth/login"]);
  expect(state.errors).toEqual([]);
});

test("remounting the live shell does not automatically retry a cached organization429", async ({ page }) => {
  await page.clock.install();
  const state = await setup(page, { kind: "response", body: { detail: PRIVATE_ERROR }, status: 429, headers: { "Retry-After": "60" } });
  await expect(organizationError(page)).toContainText("Too many organization requests");
  await expect(retry(page)).toBeDisabled();
  const before = state.reads();
  const document = await page.evaluate(() => (window as unknown as { orgRecoveryDocument: string }).orgRecoveryDocument);
  // Synthetic same-document route entry, followed by native browser Back,
  // exercises Layout retirement/remount without discarding its owned cache.
  await page.evaluate(() => {
    history.pushState(null, "", "/design/gallery");
    dispatchEvent(new PopStateEvent("popstate"));
  });
  await expect(page.getByRole("heading", { name: "InvoiceIQ design system", exact: true })).toBeVisible();
  await page.goBack();
  await expect(page).toHaveURL(/\/$/);
  await expect(visibleText(page, NAME_A)).toBeVisible();
  await expect(retry(page)).toBeDisabled();
  expect(state.reads()).toBe(before);
  expect(await page.evaluate(() => (window as unknown as { orgRecoveryDocument: string }).orgRecoveryDocument)).toBe(document);
  state.set({ kind: "valid" });
  await page.clock.fastForward(60_100);
  await expect(retry(page)).toBeEnabled();
  expect(state.reads()).toBe(before);
  await retry(page).click();
  await expect(page.getByRole("button", { name: "Switch organization" })).toBeVisible();
  expect(state.reads()).toBe(before + 1);
  expect(state.events.every(event => event.method === "GET")).toBe(true);
  expect(state.errors).toEqual([]);
});

const switchWrites = (events: Event[]) => events.filter(event => event.path.startsWith("/auth/switch-org/"));
async function switchToOther(page: Page) {
  await page.getByRole("button", { name: "Switch organization", exact: true }).click();
  await page.getByRole("menuitem", { name: OTHER_A, exact: true }).click();
}

test("an organization switch is guarded while pending and succeeds with one explicit POST", async ({ page }) => {
  const state = await setup(page);
  state.setSwitch("hold");
  await switchToOther(page);
  await expect.poll(state.heldSwitches).toBe(1);
  await expect(page.getByRole("status").filter({ hasText: "Switching organization" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Switch organization", exact: true })).toHaveCount(0);
  expect(switchWrites(state.events)).toHaveLength(1);
  await state.releaseSwitches();
  await expect(page.getByRole("button", { name: "Switch organization", exact: true })).toContainText(OTHER_A);
  expect(switchWrites(state.events)).toHaveLength(1);
  expect(state.errors).toEqual([]);
});

test("a refused switch has local sanitized feedback and permits another explicit choice", async ({ page }) => {
  const state = await setup(page);
  state.setSwitch("rejected");
  await switchToOther(page);
  await expect(page.getByRole("alert").filter({ hasText: "organization could not be switched" })).toBeVisible();
  await expect(visibleText(page, NAME_A)).toBeVisible();
  await expect(page.locator("body")).not.toContainText(PRIVATE_ERROR);
  expect(switchWrites(state.events)).toHaveLength(1);
  state.setSwitch("valid");
  await switchToOther(page);
  await expect(page.getByRole("button", { name: "Switch organization", exact: true })).toContainText(OTHER_A);
  expect(switchWrites(state.events)).toHaveLength(2);
  expect(state.errors).toEqual([]);
});

test("a missing switch response is checked with GET only and refreshes authoritative identity", async ({ page }) => {
  const state = await setup(page);
  state.setSwitch("lost");
  await switchToOther(page);
  await expect(page.getByRole("alert").filter({ hasText: "switch outcome is unconfirmed" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Switch organization", exact: true })).toHaveCount(0);
  const check = page.getByRole("button", { name: "Check current organization", exact: true });
  state.setIdentityFailure(true);
  await check.click();
  await expect(page.getByRole("alert").filter({ hasText: "could not be verified" })).toBeVisible();
  await expect(page.locator("body")).not.toContainText(PRIVATE_ERROR);
  expect(switchWrites(state.events)).toHaveLength(1);
  state.setIdentityFailure(false);
  await check.click();
  await expect(page.getByRole("button", { name: "Switch organization", exact: true })).toContainText(OTHER_A);
  expect(switchWrites(state.events)).toHaveLength(1);
  expect(state.errors).toEqual([]);
});

test("a late previous-account switch response cannot navigate or relabel the new account", async ({ page }) => {
  const state = await setup(page);
  state.setSwitch("hold");
  await switchToOther(page);
  await expect.poll(state.heldSwitches).toBe(1);
  await page.getByRole("button", { name: "Account menu", exact: true }).click();
  await page.getByRole("menuitem", { name: "Sign out", exact: true }).click();
  await page.getByLabel("Email", { exact: true }).fill("B@example.com");
  await page.getByLabel("Password", { exact: true }).fill("synthetic-password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(visibleText(page, NAME_B)).toBeVisible();
  const document = await page.evaluate(() => (window as unknown as { orgRecoveryDocument: string }).orgRecoveryDocument);
  await state.releaseSwitches();
  await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
  await expect(visibleText(page, NAME_B)).toBeVisible();
  expect(await page.evaluate(() => (window as unknown as { orgRecoveryDocument: string }).orgRecoveryDocument)).toBe(document);
  expect(switchWrites(state.events)).toHaveLength(1);
  expect(state.errors).toEqual([]);
});

for (const [width, fontSize] of [[390, 16], [390, 32], [1280, 16]]) {
  test(`organization menu contains and wraps long choices with native keyboard scrolling at ${width}px/${fontSize}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 844 });
    const state = await setup(page, { kind: "valid" }, NAME_A, { manyChoices: true });
    await page.evaluate((size) => { document.documentElement.style.fontSize = `${size}px`; }, fontSize);
    if (width < 1024) await page.getByRole("button", { name: "Open navigation menu", exact: true }).click();
    await page.getByRole("button", { name: "Switch organization", exact: true }).click();
    const menu = page.getByRole("menu", { name: "Switch organization", exact: true });
    const bounds = await menu.boundingBox();
    const measurements = await menu.evaluate(element => ({ scrollWidth: element.scrollWidth, clientWidth: element.clientWidth, scrollHeight: element.scrollHeight, clientHeight: element.clientHeight }));
    await testInfo.attach("menu-bounds", { body: JSON.stringify({ width, fontSize, bounds, measurements }), contentType: "application/json" });
    expect(bounds!.x).toBeGreaterThanOrEqual(0);
    expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(width);
    expect(bounds!.y).toBeGreaterThanOrEqual(0);
    expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(844);
    expect(measurements.scrollWidth).toBeLessThanOrEqual(measurements.clientWidth + 1);
    expect(measurements.scrollHeight).toBeGreaterThan(measurements.clientHeight);
    const last = menu.getByRole("menuitem").last();
    await page.keyboard.press("End");
    await expect(last).toBeFocused();
    expect(await menu.evaluate(element => element.scrollTop)).toBeGreaterThan(0);
    const lastBox = await last.boundingBox();
    expect(lastBox!.y).toBeGreaterThanOrEqual(bounds!.y - 1);
    expect(lastBox!.y + lastBox!.height).toBeLessThanOrEqual(bounds!.y + bounds!.height + 1);
    expect(await last.locator("span.font-medium").evaluate(element => getComputedStyle(element).textOverflow)).not.toBe("ellipsis");
    await page.screenshot({ path: testInfo.outputPath("contained-organization-menu.png") });
    expect(switchWrites(state.events)).toHaveLength(0);
    expect(state.errors).toEqual([]);
  });
}

test("the shared account menu wraps a long email and keeps keyboard actions within an enlarged mobile viewport", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const email = `${"synthetic-long-local-part".repeat(5)}@example.test`;
  const state = await setup(page, { kind: "valid" }, NAME_A, { emailA: email });
  await page.evaluate(() => { document.documentElement.style.fontSize = "32px"; });
  await page.getByRole("button", { name: "Account menu", exact: true }).click();
  const menu = page.getByRole("menu", { name: "Account menu", exact: true });
  const box = await menu.boundingBox();
  expect(box!.x).toBeGreaterThanOrEqual(0);
  expect(box!.x + box!.width).toBeLessThanOrEqual(390);
  expect(box!.y + box!.height).toBeLessThanOrEqual(844);
  await expect(menu.getByText(email, { exact: true })).toHaveText(email);
  expect(await menu.evaluate(element => element.scrollWidth <= element.clientWidth + 1)).toBe(true);
  await page.keyboard.press("End");
  await expect(menu.getByRole("menuitem", { name: "Sign out", exact: true })).toBeFocused();
  await page.keyboard.press("Home");
  await expect(menu.getByRole("menuitem", { name: "Account settings", exact: true })).toBeFocused();
  await page.screenshot({ path: testInfo.outputPath("contained-account-menu.png") });
  expect(state.events.every(event => event.method === "GET")).toBe(true);
});

test("the shared legal-entity menu fits at enlarged mobile width and retains native choice activation", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/design");
  await page.evaluate(() => { document.documentElement.style.fontSize = "32px"; });
  await page.getByRole("button", { name: "Open navigation menu", exact: true }).click();
  const drawer = page.getByRole("dialog", { name: "Menu", exact: true });
  const trigger = drawer.getByRole("button", { name: "Switch legal entity", exact: true });
  await trigger.click();
  const menu = page.getByRole("menu", { name: "Switch legal entity", exact: true });
  const box = await menu.boundingBox();
  expect(box!.x).toBeGreaterThanOrEqual(0);
  expect(box!.x + box!.width).toBeLessThanOrEqual(390);
  expect(box!.y).toBeGreaterThanOrEqual(0);
  expect(box!.y + box!.height).toBeLessThanOrEqual(844);
  await page.keyboard.press("End");
  await expect(menu.getByRole("menuitem", { name: /Northwind Eesti/ })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(trigger).toContainText("Northwind Eesti");
});
