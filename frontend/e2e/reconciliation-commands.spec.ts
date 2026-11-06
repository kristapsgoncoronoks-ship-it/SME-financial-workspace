import { expect, test, type Page, type Route } from "@playwright/test";
import type { BankLine } from "../src/lib/types";
import type { ReconciliationIntent, ReconciliationReceipt } from "../src/lib/reconciliationCommands";

// Synthetic responses qualify the browser's request identity and presentation.
// They do not prove PostgreSQL transactions, bank settlement or authorization.
const ORG = "10000000-0000-4000-8000-000000000001";
const USER = "10000000-0000-4000-8000-000000000002";
const STATEMENT = "10000000-0000-4000-8000-000000000003";
const LINE = "10000000-0000-4000-8000-000000000004";
const TARGET = "10000000-0000-4000-8000-000000000005";
const DESCRIPTION = "Synthetic reviewed bank line";
const EMPTY = { status: "unmatched", matched_kind: null, matched_id: null } as const;
const json = (body: unknown, status = 200) => ({ status, contentType: "application/json", body: JSON.stringify(body) });
const initial = (): BankLine => ({
  id: LINE, statement_id: STATEMENT, version: 1, line_date: "2026-11-06", description: DESCRIPTION,
  amount: "25.00", amount_status: "valid", currency: "EUR", direction: "credit", balance: null,
  balance_status: "unprovided", ...EMPTY,
});
type Wire = {
  line: BankLine;
  writes: { key: string; payload: ReconciliationIntent; org: string; user: string }[];
  lookups: string[]; legacy: string[]; currentReads: number; historyReads: string[];
  receipts: Map<string, ReconciliationReceipt>;
  mode: "ok" | "loseCommitted" | "loseUncommitted" | "stale";
  currentFailure: boolean; lookupFailure: boolean; malformedReceipt: boolean;
  hold: boolean; pending: Route | null; newerAfterCommit: boolean;
  historyFailure: boolean; historyPage?: (before: string | null) => unknown;
  scoped: { account: string; method: string; path: string }[];
};

async function setup(page: Page, options: { readOnly?: boolean; line?: Partial<BankLine>; saved?: string } = {}) {
  const wire: Wire = { line: { ...initial(), ...options.line }, writes: [], lookups: [], legacy: [], currentReads: 0, historyReads: [], receipts: new Map(), mode: "ok", currentFailure: false, lookupFailure: false, malformedReceipt: false, hold: false, pending: null, newerAfterCommit: false, historyFailure: false, scoped: [] };
  await page.addInitScript(({ saved, slot }) => {
    if (!sessionStorage.getItem("synthetic-reconciliation-seeded")) {
      localStorage.setItem("invoiceiq_token", "synthetic-reconciliation-A");
      sessionStorage.setItem("synthetic-reconciliation-seeded", "1");
      if (saved !== undefined) sessionStorage.setItem(slot, saved);
    }
  }, { saved: options.saved, slot: `invoiceiq:reconciliation-command:${USER}:${ORG}:${LINE}` });
  const complete = async (route: Route) => {
    const payload = route.request().postDataJSON() as ReconciliationIntent;
    const key = route.request().headers()["idempotency-key"];
    if (wire.mode === "stale") {
      wire.line = { ...wire.line, version: 2, status: "matched", matched_kind: "receipt", matched_id: TARGET };
      return route.fulfill(json({ code: "reconciliation_line_stale", detail: "This line changed. Review its current version." }, 409));
    }
    if (wire.mode === "loseUncommitted") return route.abort("failed");
    const previous = wire.receipts.get(key);
    if (previous) return route.fulfill(json(previous));
    expect(payload.expected_version).toBe(wire.line.version);
    expect(payload.expected).toEqual({ status: wire.line.status, matched_kind: wire.line.matched_kind, matched_id: wire.line.matched_id });
    const before = { ...payload.expected };
    const after = payload.operation === "match"
      ? { status: "matched" as const, matched_kind: payload.target!.kind, matched_id: payload.target!.id }
      : { status: payload.operation === "ignore" ? "ignored" as const : "unmatched" as const, matched_kind: null, matched_id: null };
    const receipt: ReconciliationReceipt = {
      contract_version: 1, status: "committed", command_id: `synthetic-command-${wire.receipts.size + 1}`,
      command_key: key, org_id: ORG, actor_user_id: USER, line_id: LINE, operation: payload.operation,
      before_version: payload.expected_version, after_version: payload.expected_version + 1, before, after,
      line: { id: LINE, statement_id: STATEMENT, version: payload.expected_version + 1, ...after },
      reason: payload.reason, recorded_at: "2026-11-06T02:00:00Z", payload_hash: "a".repeat(64), result_hash: "b".repeat(64),
    };
    wire.receipts.set(key, receipt);
    wire.line = { ...wire.line, ...after, version: receipt.after_version };
    if (wire.newerAfterCommit) wire.line = { ...wire.line, ...EMPTY, version: receipt.after_version + 1 };
    if (wire.mode === "loseCommitted") return route.abort("failed");
    return route.fulfill(json(wire.malformedReceipt ? { ...receipt, command_key: "wrong-reference" } : receipt));
  };
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request(), url = new URL(request.url());
    const path = url.pathname.replace("/api/v1", ""), method = request.method();
    const account = request.headers().authorization === "Bearer synthetic-reconciliation-B" ? "B" : "A";
    const org = { id: account === "B" ? ORG + "B" : ORG, name: account === "B" ? "Replacement workspace" : "Synthetic bank workspace", status: "active" };
    if (path === "/auth/me") return route.fulfill(json({ user: { id: account === "B" ? USER + "B" : USER, org_id: org.id, role: options.readOnly ? "auditor" : "owner", name: "Synthetic reviewer", email: "synthetic@example.com" }, organization: org,
      permissions: ["invoice.read", "issued.read", "payment.read", ...(options.readOnly ? [] : ["payment.write"])] }));
    if (path === "/auth/organizations") return route.fulfill(json([{ org_id: org.id, name: org.name, current: true }]));
    if (path === "/modules") return route.fulfill(json([{ key: "issuing", name: "Issuing", enabled: true }]));
    if (path.startsWith("/reconciliation")) wire.scoped.push({ account, method, path });
    if (path === "/reconciliation/statements") return route.fulfill(json(account === "B" ? [] : [{ id: STATEMENT, filename: "synthetic-reviewed.csv", source_format: "csv", method: "csv", line_count: 1, created_at: "2026-11-06", unmatched: wire.line.status === "unmatched" ? 1 : 0, matched: wire.line.status === "matched" ? 1 : 0, ignored: wire.line.status === "ignored" ? 1 : 0 }]));
    if (path === `/reconciliation/statements/${STATEMENT}/lines`) return route.fulfill(json([wire.line]));
    if (path === `/reconciliation/lines/${LINE}/candidates`) return route.fulfill(json([{ kind: "receipt", id: TARGET, amount: "25.00", currency: "EUR", date: "2026-11-06", reference: "Synthetic receipt", days_off: 0, score: 100 }]));
    if (path === `/reconciliation/lines/${LINE}`) {
      wire.currentReads += 1;
      return route.fulfill(wire.currentFailure ? json({ detail: "Synthetic current-line read unavailable" }, 503) : json(wire.line));
    }
    if (path.startsWith(`/reconciliation/lines/${LINE}/commands/`)) {
      expect(method).toBe("GET");
      const key = path.split("/").at(-1)!;
      wire.lookups.push(key);
      if (wire.lookupFailure) return route.fulfill(json({ detail: "Synthetic outcome lookup unavailable" }, 503));
      const receipt = wire.receipts.get(key);
      return route.fulfill(receipt ? json(wire.malformedReceipt ? { ...receipt, command_key: "wrong-reference" } : receipt) : json({ code: "reconciliation_command_not_found", detail: "Command not found" }, 404));
    }
    if (path === `/reconciliation/lines/${LINE}/commands`) {
      expect(method).toBe("POST");
      wire.writes.push({ key: request.headers()["idempotency-key"], payload: request.postDataJSON(), org: request.headers()["x-expected-org-id"], user: request.headers()["x-expected-user-id"] });
      if (wire.hold) { wire.pending = route; return; }
      return complete(route);
    }
    if (path === `/reconciliation/lines/${LINE}/history`) {
      wire.historyReads.push(url.search);
      if (wire.historyFailure) return route.fulfill(json({ detail: "Synthetic history read unavailable" }, 503));
      return route.fulfill(json(wire.historyPage?.(url.searchParams.get("before_version")) ?? { events: [...wire.receipts.values()].reverse(), next_before_version: null }));
    }
    if (path.endsWith("/match") || path.endsWith("/ignore")) {
      wire.legacy.push(`${method} ${path}`);
      return route.fulfill(json({ code: "reconciliation_command_preconditions_required", detail: "Use a versioned command" }, 428));
    }
    return route.fulfill(json([]));
  });
  await page.goto("/reconciliation");
  await page.getByRole("button", { name: "Reconcile", exact: true }).click();
  await expect(page.getByRole("row").filter({ hasText: DESCRIPTION })).toBeVisible();
  return { wire, complete, row: page.getByRole("row").filter({ hasText: DESCRIPTION }) };
}

async function ignore(page: Page, reason = "Synthetic bank fee handled separately") {
  const row = page.getByRole("row").filter({ hasText: DESCRIPTION });
  await row.getByRole("button", { name: "Ignore", exact: true }).click();
  await row.getByLabel("Reason for ignoring").fill(reason);
  await row.getByRole("button", { name: "Confirm ignore", exact: true }).click();
}

test("Ignore requires a reason and Restore records a separate reviewed transition", async ({ page }) => {
  const { wire, row } = await setup(page);
  await row.getByRole("button", { name: "Ignore", exact: true }).click();
  await expect(row.getByRole("button", { name: "Confirm ignore", exact: true })).toBeDisabled();
  await row.getByLabel("Reason for ignoring").fill("   ");
  await expect(row.getByRole("button", { name: "Confirm ignore", exact: true })).toBeDisabled();
  await row.getByLabel("Reason for ignoring").fill("  Reviewed fee — already handled  ");
  await row.getByRole("button", { name: "Confirm ignore", exact: true }).click();
  await expect(row).toContainText("Ignore recorded at version 2. Current line: version 2, ignored.");
  expect(wire.writes[0]).toMatchObject({ org: ORG, user: USER, payload: { operation: "ignore", expected_version: 1, expected: EMPTY, target: null, reason: "Reviewed fee — already handled" } });
  await row.getByRole("button", { name: "Restore", exact: true }).click();
  await expect(row).toContainText("Restore recorded at version 3");
  expect(wire.writes[1].payload).toEqual({ operation: "restore", expected_version: 2, expected: { status: "ignored", matched_kind: null, matched_id: null }, target: null, reason: null });
  expect(wire.writes[1].key).not.toBe(wire.writes[0].key);
  expect(wire.legacy).toEqual([]);
  await row.getByRole("button", { name: "History", exact: true }).click();
  const history = row.getByRole("region", { name: "Line command history" });
  await expect(history).toContainText("Reviewed fee — already handled");
  await expect(history).toContainText("version 2 → 3");
  await expect(history).toContainText("Earlier changes may predate this history");
  expect(wire.writes).toHaveLength(2);
  await page.screenshot({ path: test.info().outputPath("reconciliation-command-history-desktop.png"), fullPage: true });
});

test("Match sends the reviewed version and target and Unmatch preserves the exact association", async ({ page }) => {
  const { wire, row } = await setup(page);
  await row.getByRole("button", { name: "Find match", exact: true }).click();
  await row.getByRole("button", { name: "Match", exact: true }).click();
  await expect(row).toContainText("Match recorded at version 2");
  expect(wire.writes[0].payload).toEqual({ operation: "match", expected_version: 1, expected: EMPTY, target: { kind: "receipt", id: TARGET }, reason: null });
  await row.getByRole("button", { name: "Unmatch", exact: true }).click();
  await expect(row).toContainText("Unmatch recorded at version 3");
  expect(wire.writes[1].payload).toEqual({ operation: "unmatch", expected_version: 2, expected: { status: "matched", matched_kind: "receipt", matched_id: TARGET }, target: null, reason: null });
  expect(wire.legacy).toEqual([]);
});

test("pending save prevents a second command while its request is unresolved", async ({ page }) => {
  const { wire, row, complete } = await setup(page);
  wire.hold = true;
  await ignore(page);
  await expect(row.getByText("Saving the reviewed request…", { exact: true })).toBeVisible();
  await expect(row.getByRole("button", { name: "Ignore", exact: true })).toBeDisabled();
  await expect(row.getByRole("button", { name: "Retry same request", exact: true })).toBeDisabled();
  await expect(row.getByRole("button", { name: "Check saved outcome", exact: true })).toBeDisabled();
  expect(wire.writes).toHaveLength(1);
  wire.hold = false;
  await complete(wire.pending!);
  await expect(row).toContainText("Ignore recorded at version 2");
  expect(wire.writes).toHaveLength(1);
});

test("lost committed response is looked up once and historical outcome stays distinct from newer current state", async ({ page }) => {
  const { wire, row } = await setup(page);
  wire.mode = "loseCommitted"; wire.newerAfterCommit = true;
  await ignore(page);
  await expect(row).toContainText("Ignore recorded at version 2. Current line: version 3, unmatched.");
  expect(wire.writes).toHaveLength(1);
  expect(wire.lookups).toEqual([wire.writes[0].key]);
  await expect(row.getByRole("button", { name: "Ignore", exact: true })).toBeEnabled();
  expect(wire.currentReads).toBe(1);
});

test("unknown outcome survives reload and explicit retry checks then reuses the exact key and body", async ({ page }) => {
  const { wire, row } = await setup(page);
  wire.mode = "loseUncommitted";
  await ignore(page, "Original reason must not change");
  await expect(row.getByRole("alert")).toContainText("outcome is unconfirmed");
  const original = wire.writes[0];
  await row.getByRole("button", { name: "Check saved outcome", exact: true }).click();
  await expect(row).toContainText("does not prove the request stopped");
  expect(wire.writes).toHaveLength(1);
  await page.reload();
  await page.getByRole("button", { name: "Reconcile", exact: true }).click();
  await expect(row).toContainText(original.key);
  await expect(row).toContainText("Original reason must not change");
  await expect(row.getByRole("button", { name: "Ignore", exact: true })).toBeDisabled();
  wire.mode = "ok";
  const lookups = wire.lookups.length;
  await row.getByRole("button", { name: "Retry same request", exact: true }).click();
  await expect(row).toContainText("Ignore recorded at version 2");
  expect(wire.lookups.length).toBe(lookups + 1);
  expect(wire.writes).toHaveLength(2);
  expect(wire.writes[1]).toEqual(original);
});

test("failed current readback retains the committed receipt and checking never posts again", async ({ page }) => {
  const { wire, row } = await setup(page);
  wire.currentFailure = true;
  await ignore(page);
  await expect(row.getByRole("alert")).toContainText("Ignore recorded. The current view could not be refreshed");
  await expect(row).toContainText("Recorded action needs current readback");
  await expect(row.getByRole("button", { name: "Retry same request", exact: true })).toHaveCount(0);
  wire.currentFailure = false;
  await row.getByRole("button", { name: "Check saved outcome", exact: true }).click();
  await expect(row).toContainText("Current line: version 2, ignored");
  expect(wire.writes).toHaveLength(1);
  expect(wire.currentReads).toBe(2);
});

test("stale intent never advances itself and explicit refresh requires a new reviewed action", async ({ page }) => {
  const { wire, row } = await setup(page);
  wire.mode = "stale";
  await ignore(page);
  await expect(row.getByRole("alert")).toContainText("This request was refused");
  expect(wire.writes).toHaveLength(1);
  expect(wire.currentReads).toBe(0);
  await row.getByRole("button", { name: "Refresh and review line", exact: true }).click();
  await expect(row).toContainText("Refreshed version 2");
  expect(wire.writes).toHaveLength(1);
  wire.mode = "ok";
  await row.getByRole("button", { name: "Unmatch", exact: true }).click();
  await expect(row).toContainText("Unmatch recorded at version 3");
  expect(wire.writes[1].payload.expected_version).toBe(2);
  expect(wire.writes[1].payload.expected.matched_id).toBe(TARGET);
  expect(wire.writes[1].key).not.toBe(wire.writes[0].key);
});

test("malformed outcome and failed lookup preserve the request and offer no replacement action", async ({ page }) => {
  const { wire, row } = await setup(page);
  wire.malformedReceipt = true;
  await ignore(page);
  await expect(row.getByRole("alert")).toContainText("outcome could not be verified");
  await expect(row.getByRole("button", { name: "Ignore", exact: true })).toBeDisabled();
  wire.lookupFailure = true;
  await row.getByRole("button", { name: "Check saved outcome", exact: true }).click();
  await expect(row.getByRole("alert")).toContainText("Synthetic outcome lookup unavailable");
  expect(wire.writes).toHaveLength(1);
  expect(wire.legacy).toEqual([]);
});

test("read-only reviewer can inspect bounded history but cannot import or mutate", async ({ page }) => {
  const { wire, row } = await setup(page, { readOnly: true });
  await expect(page.getByLabel("Statement file")).toBeDisabled();
  await expect(row.getByRole("button", { name: "Ignore", exact: true })).toBeDisabled();
  await expect(row.getByRole("button", { name: "Find match", exact: true })).toBeDisabled();
  await row.getByRole("button", { name: "History", exact: true }).click();
  await expect(row).toContainText("No command history recorded on this page");
  expect(wire.historyReads).toEqual(["?limit=20"]);
  expect(wire.writes).toEqual([]);
});

test("history failure recovers by GET and earlier/newest pages keep the bounded cursor", async ({ page }) => {
  const { wire, row } = await setup(page);
  wire.historyFailure = true;
  await row.getByRole("button", { name: "History", exact: true }).click();
  await expect(row.getByRole("alert")).toContainText("Couldn’t load command history");
  wire.historyFailure = false;
  const historyEvent = (version: number) => ({ command_id: `history-${version}`, actor_user_id: USER,
    operation: version % 2 === 0 ? "ignore" : "restore", before_version: version - 1, after_version: version,
    before: { status: version % 2 === 0 ? "unmatched" : "ignored", matched_kind: null, matched_id: null },
    after: { status: version % 2 === 0 ? "ignored" : "unmatched", matched_kind: null, matched_id: null },
    reason: version % 2 === 0 ? "Earlier reviewed reason" : null, recorded_at: "2026-11-06T02:00:00Z" });
  wire.historyPage = (before) => ({ events: before === null ? Array.from({ length: 20 }, (_, index) => historyEvent(22 - index)) : [historyEvent(2)], next_before_version: before === null ? 3 : null });
  await row.getByRole("button", { name: "Try again", exact: true }).click();
  await row.getByRole("button", { name: "Earlier events", exact: true }).click();
  await expect(row.getByRole("button", { name: "Newest events", exact: true })).toBeVisible();
  expect(wire.historyReads.at(-1)).toBe("?limit=20&before_version=3");
  await row.getByRole("button", { name: "Newest events", exact: true }).click();
  await expect(row.getByRole("button", { name: "Earlier events", exact: true })).toBeVisible();
  expect(wire.writes).toEqual([]);
});

test("unknown stored request or missing version blocks new writes without inventing a version", async ({ page }) => {
  const { wire, row } = await setup(page, { saved: "{invalid", line: { version: undefined as unknown as number } });
  await expect(row.getByRole("alert")).toContainText("saved bank-line request cannot be read");
  await expect(row).toContainText("usable line version");
  await expect(row.getByRole("button", { name: "Ignore", exact: true })).toBeDisabled();
  await expect(row.getByRole("button", { name: "Find match", exact: true })).toBeDisabled();
  expect(wire.writes).toEqual([]);
});

test("session replacement during an unresolved save cannot run lookups or writes in the new workspace", async ({ page }) => {
  const { wire } = await setup(page);
  wire.hold = true;
  await ignore(page);
  await expect.poll(() => wire.writes.length).toBe(1);
  const readsBefore = wire.lookups.length;
  await page.evaluate(() => {
    localStorage.setItem("invoiceiq_token", "synthetic-reconciliation-B");
    window.dispatchEvent(new StorageEvent("storage", { key: "invoiceiq_token", newValue: "synthetic-reconciliation-B" }));
  });
  await expect(page.getByText("No statements imported yet.", { exact: true })).toBeVisible();
  expect(wire.lookups.length).toBe(readsBefore);
  expect(wire.scoped.filter((request) => request.account === "B" && (request.method !== "GET" || request.path.includes("/commands/")))).toEqual([]);
  expect(wire.writes).toHaveLength(1);
  expect(await page.evaluate(() => Object.keys(sessionStorage).some((key) => key.startsWith("invoiceiq:reconciliation-command:")))).toBe(true);
  await wire.pending!.abort().catch(() => undefined);
});

test("390px at 200% text preserves reason, request recovery and keyboard table access", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.addInitScript(() => document.addEventListener("DOMContentLoaded", () => { document.documentElement.style.fontSize = "32px"; }));
  const { wire, row } = await setup(page);
  wire.mode = "loseUncommitted";
  await ignore(page, "A long reviewed reason with punctuation <not markup> and sufficient detail for a later review.");
  await expect(row).toContainText("request outcome is unconfirmed");
  await page.screenshot({ path: test.info().outputPath("reconciliation-command-390-32.png"), fullPage: true });
  const bounds = await page.evaluate(() => ({ width: innerWidth, document: document.documentElement.scrollWidth }));
  expect(bounds.document).toBeLessThanOrEqual(bounds.width);
  const region = page.getByRole("region", { name: "Statement lines", exact: true });
  const sizes = await row.getByRole("button", { name: "Check saved outcome", exact: true }).evaluate((button) => ({
    button: button.getBoundingClientRect().width,
    review: button.closest("td")!.firstElementChild!.getBoundingClientRect().width,
    viewport: button.closest('[aria-label="Statement lines"]')!.clientWidth,
  }));
  expect(sizes.review).toBeLessThanOrEqual(sizes.viewport);
  expect(sizes.button).toBeLessThanOrEqual(sizes.viewport);
  await region.focus();
  const before = await region.evaluate((node) => node.scrollLeft);
  await page.keyboard.press("ArrowLeft");
  await expect.poll(() => region.evaluate((node) => node.scrollLeft)).toBeLessThan(before);
  expect(wire.writes).toHaveLength(1);
});

test("terminal version remains readable in history while further writes are disabled", async ({ page }) => {
  const { wire, row } = await setup(page, { line: { version: 2147483647, status: "ignored" } });
  wire.historyPage = () => ({ events: [{ command_id: "terminal-history", actor_user_id: USER,
    operation: "ignore", before_version: 2147483646, after_version: 2147483647, before: EMPTY,
    after: { status: "ignored", matched_kind: null, matched_id: null }, reason: "Recorded terminal review", recorded_at: "2026-11-06T02:00:00Z" }], next_before_version: null });
  await expect(row.getByRole("button", { name: "Restore", exact: true })).toBeDisabled();
  await expect(row).toContainText("reached its version limit");
  await row.getByRole("button", { name: "History", exact: true }).click();
  await expect(row).toContainText("version 2147483646 → 2147483647");
  expect(wire.writes).toEqual([]);
});

test("malformed history has a readable recovery instead of a misleading empty result", async ({ page }) => {
  const { wire, row } = await setup(page);
  wire.historyPage = () => ({ events: [{ operation: "unknown" }], next_before_version: null });
  await row.getByRole("button", { name: "History", exact: true }).click();
  await expect(row.getByRole("alert")).toContainText("Couldn’t load command history");
  await expect(row).not.toContainText("No command history recorded on this page");
  wire.historyPage = () => ({ events: [], next_before_version: null });
  await row.getByRole("button", { name: "Try again", exact: true }).click();
  await expect(row).toContainText("No command history recorded on this page");
  expect(wire.writes).toEqual([]);
});

test("incomplete current DTO cannot erase a confirmed request or turn unavailable money into zero", async ({ page }) => {
  const { wire, row } = await setup(page);
  let complete = false;
  await page.route(`**/api/v1/reconciliation/lines/${LINE}`, async (route) => route.fulfill(json(complete ? wire.line : {
    id: LINE, statement_id: STATEMENT, version: 2, status: "ignored", matched_kind: null, matched_id: null,
  })));
  await ignore(page);
  await expect(row.getByRole("alert")).toContainText("current bank line could not be read");
  await expect(row).toContainText("Recorded action needs current readback");
  await expect(row).not.toContainText("€0.00");
  complete = true;
  await row.getByRole("button", { name: "Check saved outcome", exact: true }).click();
  await expect(row).toContainText("Current line: version 2, ignored");
  expect(wire.writes).toHaveLength(1);
});

test("a reviewer who lost write permission can recover their own committed outcome using GET only", async ({ page }) => {
  const key = "synthetic-original-reviewed-key";
  const payload: ReconciliationIntent = { operation: "ignore", expected_version: 1, expected: EMPTY, target: null, reason: "Original reviewed reason" };
  const after = { status: "ignored", matched_kind: null, matched_id: null } as const;
  const saved = JSON.stringify({ contract: 1, key, lineId: LINE, statementId: STATEMENT, payload });
  const { wire, row } = await setup(page, { readOnly: true, saved, line: { ...after, version: 2 } });
  wire.receipts.set(key, { contract_version: 1, status: "committed", command_id: "previous-own-command", command_key: key,
    org_id: ORG, actor_user_id: USER, line_id: LINE, operation: "ignore", before_version: 1, after_version: 2,
    before: EMPTY, after, line: { id: LINE, statement_id: STATEMENT, version: 2, ...after }, reason: payload.reason,
    recorded_at: "2026-11-06T02:00:00Z", payload_hash: "a".repeat(64), result_hash: "b".repeat(64) });
  await expect(row.getByRole("button", { name: "Retry same request", exact: true })).toBeDisabled();
  await row.getByRole("button", { name: "Check saved outcome", exact: true }).click();
  await expect(row).toContainText("Ignore recorded at version 2. Current line: version 2, ignored.");
  expect(wire.lookups).toEqual([key]);
  expect(wire.writes).toEqual([]);
});
