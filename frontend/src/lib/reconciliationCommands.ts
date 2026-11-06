import axios from "axios";
import { api, tokenStore } from "./api";
import type { BankLine, MatchCandidate } from "./types";

export type ReconciliationScope = { userId: string; orgId: string };
export type ReconciliationOperation = "match" | "ignore" | "unmatch" | "restore";
export type ReconciliationState = Pick<BankLine, "status" | "matched_kind" | "matched_id">;
export type ReconciliationIntent = {
  operation: ReconciliationOperation;
  expected_version: number;
  expected: ReconciliationState;
  target: { kind: MatchCandidate["kind"]; id: string } | null;
  reason: string | null;
};
export type ReconciliationReceipt = {
  contract_version: 1;
  status: "committed";
  command_id: string;
  command_key: string;
  org_id: string;
  actor_user_id: string;
  line_id: string;
  operation: ReconciliationOperation;
  before_version: number;
  after_version: number;
  before: ReconciliationState;
  after: ReconciliationState;
  line: ReconciliationState & { id: string; statement_id: string; version: number };
  payload_hash: string;
  result_hash: string;
  reason: string | null;
  recorded_at: string;
};
export type ReconciliationHistory = {
  events: Pick<ReconciliationReceipt, "command_id" | "actor_user_id" | "operation" | "before_version" | "after_version" | "before" | "after" | "reason" | "recorded_at">[];
  next_before_version: number | null;
};
export type SavedReconciliationCommand = {
  contract: 1;
  key: string;
  lineId: string;
  statementId: string;
  payload: ReconciliationIntent;
};

const MAX_VERSION = 2147483647;
const KEY = /^[A-Za-z0-9:_-]{1,120}$/;
const HASH = /^[a-f0-9]{64}$/;
const kinds = ["receipt", "reimbursement", "payment_run", "issued_payment"];
export const validReconciliationVersion = (value: unknown): value is number =>
  Number.isSafeInteger(value) && Number(value) >= 1 && Number(value) <= MAX_VERSION;

function validState(value: unknown): value is ReconciliationState {
  if (!value || typeof value !== "object") return false;
  const state = value as ReconciliationState;
  return state.status === "matched"
    ? kinds.includes(state.matched_kind ?? "") && typeof state.matched_id === "string" && state.matched_id.length > 0
    : ["unmatched", "ignored"].includes(state.status) && state.matched_kind === null && state.matched_id === null;
}
const sameState = (a: ReconciliationState, b: ReconciliationState) =>
  a.status === b.status && a.matched_kind === b.matched_kind && a.matched_id === b.matched_id;
const slot = (scope: ReconciliationScope, lineId: string) =>
  `invoiceiq:reconciliation-command:${scope.userId}:${scope.orgId}:${lineId}`;

export function loadReconciliationCommand(scope: ReconciliationScope, lineId: string): SavedReconciliationCommand | null {
  const raw = sessionStorage.getItem(slot(scope, lineId));
  if (raw === null) return null;
  try {
    if (raw.length > 12000) throw new Error();
    const saved = JSON.parse(raw) as SavedReconciliationCommand;
    const p = saved.payload;
    const required = { match: "unmatched", ignore: "unmatched", unmatch: "matched", restore: "ignored" };
    if (saved.contract !== 1 || !KEY.test(saved.key) || saved.lineId !== lineId || typeof saved.statementId !== "string" ||
        !p || !Object.prototype.hasOwnProperty.call(required, p.operation) || !validReconciliationVersion(p.expected_version) || !validState(p.expected) ||
        required[p.operation] !== p.expected.status ||
        (p.operation === "match" ? !p.target || !kinds.includes(p.target.kind) || typeof p.target.id !== "string" : p.target !== null) ||
        (p.reason !== null && (typeof p.reason !== "string" || p.reason.length > 1000 || p.reason.includes("\0"))) ||
        (p.operation === "ignore" && !p.reason?.trim())) throw new Error();
    return saved;
  } catch {
    throw new Error("The saved bank-line request cannot be read. Keep its reference and ask an administrator before starting another action on this line.");
  }
}

export function startReconciliationCommand(
  scope: ReconciliationScope, line: BankLine, operation: ReconciliationOperation,
  target: ReconciliationIntent["target"] = null, reason: string | null = null,
): SavedReconciliationCommand {
  if (loadReconciliationCommand(scope, line.id)) throw new Error("Check the saved request before starting another action on this line.");
  const expected = { status: line.status, matched_kind: line.matched_kind, matched_id: line.matched_id };
  if (!validReconciliationVersion(line.version) || !validState(expected)) {
    throw new Error("The bank line has no usable version. Reload the statement and review it before acting.");
  }
  const saved: SavedReconciliationCommand = {
    contract: 1, key: crypto.randomUUID(), lineId: line.id, statementId: line.statement_id,
    payload: { operation, expected_version: line.version, expected, target, reason: reason?.trim() || null },
  };
  sessionStorage.setItem(slot(scope, line.id), JSON.stringify(saved));
  return saved;
}

export function clearReconciliationCommand(scope: ReconciliationScope, saved: SavedReconciliationCommand): void {
  // Another continuation must not erase a replacement request.
  if (loadReconciliationCommand(scope, saved.lineId)?.key === saved.key) sessionStorage.removeItem(slot(scope, saved.lineId));
}

export function reconciliationContinuation(signal: AbortSignal): () => void {
  const generation = tokenStore.generation(), token = tokenStore.get();
  return () => {
    if (signal.aborted || generation !== tokenStore.generation() || token !== tokenStore.get()) {
      throw new axios.CanceledError("Session changed. Check the saved bank-line request in its original workspace.");
    }
  };
}

function readReceipt(value: unknown, saved: SavedReconciliationCommand, scope: ReconciliationScope): ReconciliationReceipt {
  const r = value as ReconciliationReceipt;
  const p = saved.payload;
  const after: ReconciliationState = p.operation === "match"
    ? { status: "matched", matched_kind: p.target!.kind, matched_id: p.target!.id }
    : { status: p.operation === "ignore" ? "ignored" : "unmatched", matched_kind: null, matched_id: null };
  if (!r || r.contract_version !== 1 || r.status !== "committed" || typeof r.command_id !== "string" ||
      r.command_key !== saved.key || r.org_id !== scope.orgId || r.actor_user_id !== scope.userId ||
      r.line_id !== saved.lineId || r.operation !== p.operation || r.before_version !== p.expected_version ||
      r.after_version !== p.expected_version + 1 || !validReconciliationVersion(r.after_version) ||
      !validState(r.before) || !sameState(r.before, p.expected) || !validState(r.after) || !sameState(r.after, after) ||
      !r.line || r.line.id !== saved.lineId || r.line.statement_id !== saved.statementId || r.line.version !== r.after_version ||
      !validState(r.line) || !sameState(r.line, after) || r.reason !== p.reason ||
      !HASH.test(r.payload_hash) || !HASH.test(r.result_hash) || !Number.isFinite(Date.parse(r.recorded_at))) {
    throw new Error("The bank-line outcome could not be verified. Keep this request reference and check its saved outcome again.");
  }
  return r;
}

function config(scope: ReconciliationScope, signal: AbortSignal) {
  return { signal, headers: { "X-Expected-Org-ID": scope.orgId, "X-Expected-User-ID": scope.userId } };
}

export async function lookupReconciliationCommand(saved: SavedReconciliationCommand, scope: ReconciliationScope, signal: AbortSignal): Promise<ReconciliationReceipt | null> {
  const current = reconciliationContinuation(signal);
  current();
  try {
    const response = await api.get(`/reconciliation/lines/${saved.lineId}/commands/${saved.key}`, config(scope, signal));
    current();
    return readReceipt(response.data, saved, scope);
  } catch (error) {
    current();
    if (axios.isAxiosError(error) && error.response?.status === 404) return null;
    throw error;
  }
}

export async function postReconciliationCommand(saved: SavedReconciliationCommand, scope: ReconciliationScope, signal: AbortSignal): Promise<ReconciliationReceipt> {
  const current = reconciliationContinuation(signal);
  current();
  const response = await api.post(`/reconciliation/lines/${saved.lineId}/commands`, saved.payload, {
    ...config(scope, signal), headers: { ...config(scope, signal).headers, "Idempotency-Key": saved.key },
  });
  current();
  return readReceipt(response.data, saved, scope);
}

export async function readCurrentReconciliationLine(saved: SavedReconciliationCommand, scope: ReconciliationScope, signal: AbortSignal): Promise<BankLine> {
  const current = reconciliationContinuation(signal);
  current();
  const response = await api.get<BankLine>(`/reconciliation/lines/${saved.lineId}`, config(scope, signal));
  current();
  const line = response.data;
  const finiteMoney = (amount: unknown) => typeof amount === "string" && amount.trim() !== "" && Number.isFinite(Number(amount));
  if (!line || line.id !== saved.lineId || line.statement_id !== saved.statementId || !validReconciliationVersion(line.version) ||
      line.version < saved.payload.expected_version || !validState(line) || typeof line.description !== "string" ||
      !Number.isFinite(Date.parse(line.line_date)) || !["credit", "debit"].includes(line.direction) ||
      !(line.currency === null || typeof line.currency === "string") ||
      !(line.amount_status === "valid" ? finiteMoney(line.amount) : line.amount_status === "invalid" && line.amount === null) ||
      !(line.balance_status === "valid" ? finiteMoney(line.balance) : ["invalid", "unprovided"].includes(line.balance_status) && line.balance === null)) {
    throw new Error("The current bank line could not be read. Check again before choosing another action.");
  }
  return line;
}

export function readReconciliationHistory(value: unknown, before: number | null): ReconciliationHistory {
  const page = value as ReconciliationHistory;
  const invalid = () => new Error("This history page could not be read. Try loading it again.");
  if (!page || !Array.isArray(page.events) || page.events.length > 20) throw invalid();
  let previous = before ?? MAX_VERSION + 1;
  const identities = new Set<string>();
  for (const event of page.events) {
    if (!event || typeof event.command_id !== "string" || !event.command_id || identities.has(event.command_id) ||
        typeof event.actor_user_id !== "string" || !event.actor_user_id ||
        !Object.prototype.hasOwnProperty.call({ match: 1, ignore: 1, unmatch: 1, restore: 1 }, event.operation) ||
        !validReconciliationVersion(event.before_version) || !validReconciliationVersion(event.after_version) ||
        event.after_version !== event.before_version + 1 || event.after_version >= previous ||
        !validState(event.before) || !validState(event.after) ||
        !(event.reason === null || (typeof event.reason === "string" && event.reason.length <= 1000)) ||
        !Number.isFinite(Date.parse(event.recorded_at))) throw invalid();
    identities.add(event.command_id);
    previous = event.after_version;
  }
  if (page.next_before_version !== null && (page.events.length === 0 ||
      page.next_before_version !== page.events[page.events.length - 1].after_version)) throw invalid();
  return page;
}
