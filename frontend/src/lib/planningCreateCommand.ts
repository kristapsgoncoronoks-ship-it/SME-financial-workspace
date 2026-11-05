import axios from "axios";
import { api, tokenStore } from "./api";
import { assignmentWindow } from "./scheduleDates";

export interface PlanningScope { userId: string; orgId: string }
export interface PlanningDraft {
  project_id: string; assignee_user_id: string; date: string; end_date: string;
  start: string; end: string; all_day: boolean; note: string;
  notify_assignee: boolean; reminders_enabled: boolean;
  client_notice_enabled: "default" | "on" | "off"; client_notice: string;
}
export interface PlanningPayload {
  project_id: string; assignee_user_id: string; starts_at: string; ends_at: string;
  all_day: boolean; note: string | null; remind_hours_before: number | null;
  client_notice_hours_before: number | null; time_zone: string;
  notify_assignee: boolean; reminders_enabled: boolean; client_notice_enabled: boolean | null;
}
export interface PlanningAssignment {
  id: string; project_id: string; project_code?: string | null; project_name?: string | null;
  assignee_user_id: string; assignee_email: string; starts_at: string; ends_at: string;
  all_day: boolean; status: string; note: string | null; revision: number; time_zone: string;
  created_by: string; remind_hours_before: number | null; client_notice_hours_before: number | null;
  notify_assignee: boolean; reminders_enabled: boolean; client_notice_enabled: boolean | null;
  notification_status?: string | null;
}
export interface PlanningWriteResult { assignment: PlanningAssignment; overlaps: PlanningAssignment[] }
export interface SavedPlanningCommand {
  key: string; digest: string; payload: PlanningPayload; draft: PlanningDraft; creator: string;
  actorId: string; orgId: string;
}
export const PLANNING_TIMEOUT_MS = 15_000;
const ID = /^[A-Za-z0-9-]{1,120}$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const UNVERIFIED = "The saved assignment outcome could not be verified. Keep its reference and check it again before creating a replacement.";
function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(UNVERIFIED);
  return value as Record<string, unknown>;
}
function id(value: unknown): string {
  if (typeof value !== "string" || !ID.test(value)) throw new Error(UNVERIFIED);
  return UUID.test(value) ? value.toLowerCase() : value;
}
function instant(value: unknown): string {
  if (typeof value !== "string" || !Number.isFinite(Date.parse(value))) throw new Error(UNVERIFIED);
  return new Date(value).toISOString();
}
function hours(value: unknown): number | null {
  if (value === null) return null;
  if (!Number.isInteger(value) || (value as number) < 1 || (value as number) > 336) throw new Error(UNVERIFIED);
  return value as number;
}
function payload(value: unknown): PlanningPayload {
  const row = object(value);
  if (typeof row.all_day !== "boolean" || typeof row.notify_assignee !== "boolean" || typeof row.reminders_enabled !== "boolean" ||
      !(row.client_notice_enabled === null || typeof row.client_notice_enabled === "boolean") ||
      !(row.note === null || (typeof row.note === "string" && row.note.length <= 2000)) ||
      typeof row.time_zone !== "string" || row.time_zone.length < 1 || row.time_zone.length > 64) throw new Error(UNVERIFIED);
  const start = instant(row.starts_at), end = instant(row.ends_at);
  if (Date.parse(end) <= Date.parse(start)) throw new Error(UNVERIFIED);
  return Object.freeze({ project_id: id(row.project_id), assignee_user_id: id(row.assignee_user_id),
    starts_at: start, ends_at: end, all_day: row.all_day, note: row.note as string | null,
    remind_hours_before: hours(row.remind_hours_before), client_notice_hours_before: hours(row.client_notice_hours_before),
    time_zone: row.time_zone, notify_assignee: row.notify_assignee, reminders_enabled: row.reminders_enabled,
    client_notice_enabled: row.client_notice_enabled as boolean | null });
}
function draft(value: unknown): PlanningDraft {
  const row = object(value);
  if (!["date", "end_date"].every((key) => typeof row[key] === "string" && /^\d{4}-\d{2}-\d{2}$/.test(row[key] as string)) ||
      !["start", "end"].every((key) => typeof row[key] === "string" && /^\d{2}:\d{2}$/.test(row[key] as string)) ||
      typeof row.all_day !== "boolean" || typeof row.note !== "string" || row.note.length > 2000 ||
      typeof row.notify_assignee !== "boolean" || typeof row.reminders_enabled !== "boolean" ||
      !["default", "on", "off"].includes(String(row.client_notice_enabled)) ||
      typeof row.client_notice !== "string" || !/^(?:\d{1,3})?$/.test(row.client_notice)) throw new Error(UNVERIFIED);
  return Object.freeze({ project_id: id(row.project_id), assignee_user_id: id(row.assignee_user_id),
    date: row.date as string, end_date: row.end_date as string, start: row.start as string, end: row.end as string,
    all_day: row.all_day, note: row.note, notify_assignee: row.notify_assignee, reminders_enabled: row.reminders_enabled,
    client_notice_enabled: row.client_notice_enabled as PlanningDraft["client_notice_enabled"], client_notice: row.client_notice });
}
function slot(scope: PlanningScope): string {
  return `invoiceiq:command:${scope.userId}:${scope.orgId}:/schedule/assignments`;
}
function fingerprint(saved: Pick<SavedPlanningCommand, "key" | "payload" | "draft" | "creator" | "actorId" | "orgId">) {
  return { key: saved.key, actorId: saved.actorId, orgId: saved.orgId, payload: saved.payload, draft: saved.draft, creator: saved.creator };
}
function stable(value: unknown): string {
  return JSON.stringify(value, (_key, item: unknown) => item && typeof item === "object" && !Array.isArray(item)
    ? Object.fromEntries(Object.entries(item).sort(([first], [second]) => first.localeCompare(second))) : item);
}
async function digest(value: unknown): Promise<string> {
  const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(JSON.stringify(value)));
  return Array.from(new Uint8Array(bytes), (byte) => byte.toString(16).padStart(2, "0")).join("");
}
function guard(signal: AbortSignal) {
  const generation = tokenStore.generation(), token = tokenStore.get();
  return () => {
    if (signal.aborted || generation !== tokenStore.generation() || token !== tokenStore.get()) {
      throw new axios.CanceledError("The assignment request was interrupted. Check its saved reference in the original account and workspace.");
    }
  };
}
function headers(scope: PlanningScope) {
  return { "X-Expected-User-ID": scope.userId, "X-Expected-Org-ID": scope.orgId };
}
export function readPlanningCommand(scope: PlanningScope): SavedPlanningCommand | null {
  const stored = sessionStorage.getItem(slot(scope));
  if (stored === null) return null;
  try {
    const row = object(JSON.parse(stored));
    if (typeof row.key !== "string" || !UUID.test(row.key) || typeof row.digest !== "string" || !/^[a-f0-9]{64}$/.test(row.digest) ||
        typeof row.creator !== "string" || !row.creator || row.creator.length > 320 ||
        row.actorId !== scope.userId || row.orgId !== scope.orgId) throw new Error(UNVERIFIED);
    return { key: row.key, digest: row.digest, creator: row.creator, actorId: scope.userId, orgId: scope.orgId,
      payload: payload(row.payload), draft: draft(row.draft) };
  } catch { throw new Error("The saved assignment request is unreadable. Keep its reference and ask an administrator before creating a replacement."); }
}
export async function preparePlanningCommand(scope: PlanningScope, source: PlanningDraft, creator: string, signal: AbortSignal) {
  const current = guard(signal); current();
  if (readPlanningCommand(scope)) throw new Error("Check the saved assignment request before creating another assignment.");
  const frozen = draft({ ...source });
  const body = payload({ ...assignmentWindow(frozen), project_id: frozen.project_id, assignee_user_id: frozen.assignee_user_id,
    all_day: frozen.all_day, note: frozen.note || null, remind_hours_before: null,
    client_notice_hours_before: frozen.client_notice ? Number(frozen.client_notice) : null,
    notify_assignee: frozen.notify_assignee, reminders_enabled: frozen.reminders_enabled,
    client_notice_enabled: frozen.client_notice_enabled === "default" ? null : frozen.client_notice_enabled === "on" });
  const intention = { key: crypto.randomUUID(), payload: body, draft: frozen, creator, actorId: scope.userId, orgId: scope.orgId };
  const hash = await digest(fingerprint(intention)); current();
  if (readPlanningCommand(scope)) throw new Error("Check the saved assignment request before creating another assignment.");
  const saved = { ...intention, digest: hash };
  sessionStorage.setItem(slot(scope), JSON.stringify(saved));
  return saved;
}
async function verifySaved(scope: PlanningScope, saved: SavedPlanningCommand, current: () => void) {
  const stored = readPlanningCommand(scope);
  if (!stored || stored.key !== saved.key || stored.digest !== saved.digest ||
      JSON.stringify(fingerprint(stored)) !== JSON.stringify(fingerprint(saved)) ||
      await digest(fingerprint(saved)) !== saved.digest) throw new Error(UNVERIFIED);
  current();
}
function assignment(value: unknown): PlanningAssignment {
  const row = object(value);
  if (typeof row.assignee_email !== "string" || typeof row.created_by !== "string" ||
      !["planned", "confirmed", "done", "cancelled"].includes(String(row.status)) ||
      !Number.isInteger(row.revision) || (row.revision as number) < 1) throw new Error(UNVERIFIED);
  const body = payload(row);
  return { ...row, ...body, id: id(row.id), assignee_email: row.assignee_email, created_by: row.created_by,
    status: row.status as string, revision: row.revision as number } as PlanningAssignment;
}
function originalResult(value: unknown, saved: SavedPlanningCommand): PlanningWriteResult {
  const row = object(value), main = assignment(row.assignment);
  if (!Array.isArray(row.overlaps)) throw new Error(UNVERIFIED);
  const overlaps = row.overlaps.map(assignment);
  if (main.status !== "planned" || main.revision !== 1 || main.created_by !== saved.creator ||
      JSON.stringify(payload(main)) !== JSON.stringify(saved.payload) ||
      overlaps.some((other) => other.id === main.id || other.assignee_user_id !== main.assignee_user_id) ||
      new Set(overlaps.map((other) => other.id)).size !== overlaps.length) throw new Error(UNVERIFIED);
  return { assignment: main, overlaps };
}
async function lookup(scope: PlanningScope, saved: SavedPlanningCommand, signal: AbortSignal, current: () => void) {
  try {
    const response = await api.get(`/schedule/commands/${encodeURIComponent(saved.key)}`, { signal, headers: headers(scope), timeout: PLANNING_TIMEOUT_MS });
    current(); const receipt = object(response.data);
    if (receipt.status !== "committed" || receipt.command_key !== saved.key || receipt.operation !== "assignment_created" ||
        receipt.org_id !== saved.orgId || receipt.actor_user_id !== saved.actorId ||
        JSON.stringify(payload(receipt.payload)) !== JSON.stringify(saved.payload)) throw new Error(UNVERIFIED);
    const result = originalResult(receipt.result, saved);
    if (result.assignment.id !== receipt.target_id) throw new Error(UNVERIFIED);
    return result;
  } catch (error) {
    current();
    if (axios.isAxiosError(error) && error.response?.status === 404) return null;
    throw error;
  }
}
export async function checkPlanningCommand(scope: PlanningScope, saved: SavedPlanningCommand, signal: AbortSignal) {
  const current = guard(signal); current(); await verifySaved(scope, saved, current);
  return lookup(scope, saved, signal, current);
}
export async function sendPlanningCommand(scope: PlanningScope, saved: SavedPlanningCommand, signal: AbortSignal, retry = false) {
  const current = guard(signal); current(); await verifySaved(scope, saved, current);
  if (retry) { const previous = await lookup(scope, saved, signal, current); if (previous) return previous; }
  let written: PlanningWriteResult;
  try {
    const response = await api.post("/schedule/assignments", saved.payload, { signal,
      headers: { ...headers(scope), "Idempotency-Key": saved.key }, timeout: PLANNING_TIMEOUT_MS });
    current(); written = originalResult(response.data, saved);
  } catch (error) {
    current();
    if (axios.isAxiosError(error) && error.response && [400, 403, 404, 413, 422].includes(error.response.status)) clearPlanningCommand(scope, saved.key);
    throw error;
  }
  const committed = await lookup(scope, saved, signal, current);
  if (!committed || stable(committed) !== stable(written)) throw new Error(UNVERIFIED);
  return committed;
}
export async function readCurrentPlanningAssignment(scope: PlanningScope, saved: SavedPlanningCommand, original: PlanningWriteResult, signal: AbortSignal) {
  const current = guard(signal); current();
  const response = await api.get(`/schedule/assignments/${original.assignment.id}`, { signal, headers: headers(scope), timeout: PLANNING_TIMEOUT_MS });
  current(); const row = assignment(response.data);
  if (row.id !== original.assignment.id || row.project_id !== saved.payload.project_id || row.created_by !== saved.creator ||
      row.revision < original.assignment.revision || (row.revision === 1 && JSON.stringify(payload(row)) !== JSON.stringify(saved.payload))) throw new Error(UNVERIFIED);
  return row;
}
export function clearPlanningCommand(scope: PlanningScope, key: string) {
  if (readPlanningCommand(scope)?.key === key) sessionStorage.removeItem(slot(scope));
}
