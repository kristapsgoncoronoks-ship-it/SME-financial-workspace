import axios from "axios";
import { api, apiErrorCode } from "./api";
import type { ExpenseCommandScope } from "./manualExpenseCommand";
import {
  PAYOUT_READ_TIMEOUT_MS, payoutBatchDetail, payoutCommandDigest, payoutCommandGuard,
  payoutCommandHeaders, payoutDecimal, payoutMinor,
  type PayoutBatch, type PayoutBatchDetail, type PayoutReport,
} from "./reimbursementCreateCommand";

type ReviewedCancelReport = PayoutReport & { payment_reference: null; reimbursed_at: null };

interface CancelPayload {
  version: number;
  report_versions: Record<string, number>;
  reason: string | null;
}
export interface SavedCancelCommand {
  key: string;
  digest: string;
  batchId: string;
  payload: CancelPayload;
  expectedTotal: string;
  expectedMethod: string;
  reports: ReviewedCancelReport[];
}
export interface CancellationResult {
  batch: PayoutBatchDetail;
  released_reports: ReviewedCancelReport[];
  reason: string | null;
}
interface CancelReference {
  batchId: string;
  command: SavedCancelCommand | null;
  error: string | null;
}
const ID = /^[A-Za-z0-9-]{1,120}$/;
const UNVERIFIED = "The cancellation record could not be verified. Keep its reference and check it again before another batch action.";

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(UNVERIFIED);
  return value as Record<string, unknown>;
}
function prefix(scope: ExpenseCommandScope): string {
  return `invoiceiq:command:${scope.userId}:${scope.orgId}:/reimbursements/`;
}
function slot(scope: ExpenseCommandScope, batchId: string): string {
  if (!ID.test(batchId)) throw new Error(UNVERIFIED);
  return `${prefix(scope)}${batchId}/cancel`;
}
function payload(value: unknown): CancelPayload {
  const body = object(value);
  const versions = object(body.report_versions);
  const ids = Object.keys(versions).sort();
  if (!Number.isInteger(body.version) || (body.version as number) < 1 ||
      ids.length > 1000 || ids.some((id) => !ID.test(id) || !Number.isInteger(versions[id]) || (versions[id] as number) < 1) ||
      !(body.reason === null || (typeof body.reason === "string" && body.reason.length <= 1000))) throw new Error(UNVERIFIED);
  return Object.freeze({ version: body.version as number,
    report_versions: Object.freeze(Object.fromEntries(ids.map((id) => [id, versions[id] as number]))),
    reason: typeof body.reason === "string" ? body.reason.trim() || null : null });
}
function reports(value: unknown): ReviewedCancelReport[] {
  if (!Array.isArray(value) || value.length > 1000) throw new Error(UNVERIFIED);
  const rows = value.map((value) => {
    const row = object(value);
    if (typeof row.id !== "string" || !ID.test(row.id) || typeof row.title !== "string" ||
        typeof row.employee_name !== "string" || typeof row.total !== "string" || typeof row.currency !== "string" ||
        !(row.total_eur === null || typeof row.total_eur === "string") ||
        !["approved", "marked_for_reimbursement"].includes(String(row.status)) ||
        !Number.isInteger(row.version) || (row.version as number) < 1 ||
        row.payment_reference !== null || row.reimbursed_at !== null) throw new Error(UNVERIFIED);
    return Object.freeze({ id: row.id, title: row.title, employee_name: row.employee_name,
      total: row.total, currency: row.currency, total_eur: row.total_eur as string | null,
      status: row.status as string, version: row.version as number, payment_reference: null, reimbursed_at: null });
  }).sort((a, b) => a.id.localeCompare(b.id));
  if (new Set(rows.map((row) => row.id)).size !== rows.length) throw new Error(UNVERIFIED);
  return Object.freeze(rows) as unknown as ReviewedCancelReport[];
}
function fingerprint(command: Omit<SavedCancelCommand, "key" | "digest">) {
  return { batchId: command.batchId, payload: command.payload, expectedTotal: command.expectedTotal,
    expectedMethod: command.expectedMethod, reports: command.reports };
}
export function readCancelCommand(scope: ExpenseCommandScope, batchId: string): SavedCancelCommand | null {
  const stored = sessionStorage.getItem(slot(scope, batchId));
  if (stored === null) return null;
  try {
    const row = object(JSON.parse(stored));
    if (row.batchId !== batchId || typeof row.key !== "string" || !/^[A-Za-z0-9:_-]{1,120}$/.test(row.key) ||
        typeof row.digest !== "string" || !/^[a-f0-9]{64}$/.test(row.digest) ||
        typeof row.expectedMethod !== "string" || !row.expectedMethod || row.expectedMethod.length > 40) throw new Error(UNVERIFIED);
    const saved = { batchId, key: row.key, digest: row.digest, expectedMethod: row.expectedMethod,
      expectedTotal: payoutDecimal(payoutMinor(row.expectedTotal)), payload: payload(row.payload), reports: reports(row.reports) };
    if (JSON.stringify(saved.reports.map((row) => row.id)) !== JSON.stringify(Object.keys(saved.payload.report_versions).sort()) ||
        saved.reports.some((row) => row.version !== saved.payload.report_versions[row.id])) throw new Error(UNVERIFIED);
    return saved;
  } catch { throw new Error("The saved cancellation reference is unreadable. Review this batch with an administrator before another batch action."); }
}
/** References remain reachable after reload and when their batch is on another page. */
export function cancelReferences(scope: ExpenseCommandScope): CancelReference[] {
  const found: CancelReference[] = [];
  const start = prefix(scope);
  for (let i = 0; i < sessionStorage.length; i++) {
    const key = sessionStorage.key(i);
    if (!key?.startsWith(start) || !key.endsWith("/cancel")) continue;
    const batchId = key.slice(start.length, -"/cancel".length);
    if (!ID.test(batchId)) throw new Error("A saved cancellation reference is unreadable. Ask an administrator before another batch action.");
    try { found.push({ batchId, command: readCancelCommand(scope, batchId), error: null }); }
    catch (error) { found.push({ batchId, command: null, error: error instanceof Error ? error.message : UNVERIFIED }); }
  }
  return found.sort((a, b) => a.batchId.localeCompare(b.batchId));
}
function unexported(batch: PayoutBatch): boolean {
  return batch.export_count === 0 && batch.exported_at === null && batch.last_msg_id === null;
}
/** Review every current child before confirmation; the click keeps those versions. */
export async function reviewCancellationBatch(scope: ExpenseCommandScope, observed: PayoutBatch, signal: AbortSignal) {
  const current = payoutCommandGuard(signal); current();
  if (!ID.test(observed.id)) throw new Error(UNVERIFIED);
  const response = await api.get(`/reimbursements/${observed.id}`, {
    signal, headers: payoutCommandHeaders(scope), timeout: PAYOUT_READ_TIMEOUT_MS,
  });
  current();
  const batch = payoutBatchDetail(response.data);
  if (batch.id !== observed.id || batch.status !== "open" || batch.version !== observed.version ||
      !unexported(batch) || batch.paid_at !== null || batch.report_count !== observed.report_count ||
      batch.method !== observed.method || payoutMinor(batch.total_eur) !== payoutMinor(observed.total_eur)) {
    throw new Error("This batch or its reports changed. Refresh its status and review it before cancellation.");
  }
  reports(batch.reports);
  return batch;
}
export async function prepareCancelCommand(scope: ExpenseCommandScope, batch: PayoutBatchDetail, reason: string | null, signal: AbortSignal): Promise<SavedCancelCommand> {
  const current = payoutCommandGuard(signal); current();
  if (readCancelCommand(scope, batch.id)) throw new Error("Check this batch’s saved cancellation before another batch action.");
  const reviewed = reports(batch.reports);
  const body = payload({ version: batch.version, report_versions: Object.fromEntries(reviewed.map((row) => [row.id, row.version])), reason });
  const frozen = { batchId: batch.id, payload: body, expectedTotal: payoutDecimal(payoutMinor(batch.total_eur)), expectedMethod: batch.method, reports: reviewed };
  const digest = await payoutCommandDigest(fingerprint(frozen));
  current();
  if (readCancelCommand(scope, batch.id)) throw new Error("Check this batch’s saved cancellation before another batch action.");
  const saved = { ...frozen, key: crypto.randomUUID(), digest };
  sessionStorage.setItem(slot(scope, batch.id), JSON.stringify(saved));
  return saved;
}
async function verifySaved(scope: ExpenseCommandScope, saved: SavedCancelCommand, current: () => void) {
  const stored = readCancelCommand(scope, saved.batchId);
  if (!stored || stored.key !== saved.key || stored.digest !== saved.digest ||
      JSON.stringify(fingerprint(stored)) !== JSON.stringify(fingerprint(saved)) ||
      await payoutCommandDigest(fingerprint(saved)) !== saved.digest) throw new Error(UNVERIFIED);
  current();
}
function cancellationResult(value: unknown, saved: SavedCancelCommand): CancellationResult {
  const row = object(value);
  const batch = payoutBatchDetail(row.batch);
  const released = reports(row.released_reports);
  const expected = saved.reports.map((report) => ({ ...report, version: report.version + 1 }));
  if (batch.id !== saved.batchId || batch.status !== "cancelled" || batch.version !== saved.payload.version + 1 ||
      batch.report_count !== 0 || batch.reports.length !== 0 || !unexported(batch) || batch.paid_at !== null ||
      batch.method !== saved.expectedMethod || payoutDecimal(payoutMinor(batch.total_eur)) !== saved.expectedTotal ||
      row.reason !== saved.payload.reason || JSON.stringify(released) !== JSON.stringify(expected)) throw new Error(UNVERIFIED);
  return { batch, released_reports: released, reason: row.reason as string | null };
}
async function lookup(scope: ExpenseCommandScope, saved: SavedCancelCommand, signal: AbortSignal, current: () => void) {
  try {
    const response = await api.get(`/reimbursements/commands/${encodeURIComponent(saved.key)}`, {
      signal, headers: payoutCommandHeaders(scope), timeout: PAYOUT_READ_TIMEOUT_MS,
    });
    current();
    const receipt = object(response.data);
    if (receipt.status !== "committed" || receipt.command_key !== saved.key || receipt.operation !== "reimbursement_cancelled" || receipt.target_id !== saved.batchId) throw new Error(UNVERIFIED);
    return cancellationResult(receipt.result, saved);
  } catch (error) {
    current();
    if (axios.isAxiosError(error) && error.response?.status === 404) return null;
    throw error;
  }
}
/** Recovery checks only read; a missing receipt does not prove a failed command. */
export async function checkCancelCommand(scope: ExpenseCommandScope, saved: SavedCancelCommand, signal: AbortSignal) {
  const current = payoutCommandGuard(signal); current();
  await verifySaved(scope, saved, current);
  return lookup(scope, saved, signal, current);
}
export async function sendCancelCommand(scope: ExpenseCommandScope, saved: SavedCancelCommand, signal: AbortSignal, retry = false) {
  const current = payoutCommandGuard(signal); current();
  await verifySaved(scope, saved, current);
  if (retry) { const previous = await lookup(scope, saved, signal, current); if (previous) return previous; }
  let written: CancellationResult;
  try {
    const response = await api.post(`/reimbursements/${saved.batchId}/cancel`, saved.payload, {
      signal, headers: { ...payoutCommandHeaders(scope), "Idempotency-Key": saved.key }, timeout: PAYOUT_READ_TIMEOUT_MS,
    });
    current(); written = cancellationResult(response.data, saved);
  } catch (error) {
    current();
    if (axios.isAxiosError(error) && error.response &&
        ([400, 403, 404, 413, 422].includes(error.response.status) ||
          (error.response.status === 409 && ["reimbursement_cancellation_conflict", "reimbursement_selection_conflict", "batch_exported_cannot_cancel"].includes(apiErrorCode(error) ?? "")))) {
      clearCancelCommand(scope, saved.batchId, saved.key);
    }
    throw error;
  }
  const committed = await lookup(scope, saved, signal, current);
  if (!committed || committed.batch.id !== written.batch.id) throw new Error(UNVERIFIED);
  return committed;
}
export async function readCancelledBatch(scope: ExpenseCommandScope, original: CancellationResult, saved: SavedCancelCommand, signal: AbortSignal) {
  const current = payoutCommandGuard(signal); current();
  const response = await api.get(`/reimbursements/${saved.batchId}`, {
    signal, headers: payoutCommandHeaders(scope), timeout: PAYOUT_READ_TIMEOUT_MS,
  });
  current();
  return cancellationResult({ batch: response.data, released_reports: original.released_reports, reason: original.reason }, saved).batch;
}
export function clearCancelCommand(scope: ExpenseCommandScope, batchId: string, key: string) {
  if (readCancelCommand(scope, batchId)?.key === key) sessionStorage.removeItem(slot(scope, batchId));
}
