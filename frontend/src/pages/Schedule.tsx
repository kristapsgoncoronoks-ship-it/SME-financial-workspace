import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import axios from "axios";
import { useEffect, useId, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { AssignmentNotifications } from "../components/AssignmentNotifications";
import { CalendarConnections } from "../components/CalendarConnections";
import { Badge, Button, ConfirmDialog, EmptyState, ErrorState, QueryState, Skeleton, TabPanel, Tabs } from "../components/ui";
import { api, apiError, tokenStore } from "../lib/api";
import { useAuth } from "../auth/AuthContext";
import { browserTimeZone, formatDate, formatTime, formatWeekday } from "../lib/format";
import { addLocalDays, assignmentWindow, localDateValue, localTimeValue, startOfLocalWeek } from "../lib/scheduleDates";
import {
  checkPlanningCommand, clearPlanningCommand, PLANNING_TIMEOUT_MS, preparePlanningCommand,
  readCurrentPlanningAssignment, readPlanningCommand, sendPlanningCommand,
  type SavedPlanningCommand, type PlanningWriteResult,
} from "../lib/planningCreateCommand";

interface Assignment {
  id: string; project_id: string; project_code?: string | null; project_name?: string | null;
  assignee_user_id: string; assignee_email: string; starts_at: string; ends_at: string;
  all_day: boolean; status: string; note: string | null; revision: number; time_zone?: string;
  notify_assignee?: boolean; reminders_enabled?: boolean; client_notice_enabled?: boolean | null;
  client_notice_hours_before?: number | null; notification_status?: string | null;
}
interface Member { user_id: string; email: string; name: string | null }
interface Project { id: string; code: string; name: string }
interface WriteResult { assignment: Assignment; overlaps: Assignment[] }
interface FeedToken { token: string; path: string }
interface Editor {
  original: Assignment | null; project_id: string; assignee_user_id: string;
  date: string; end_date: string; start: string; end: string; all_day: boolean; note: string;
  notify_assignee: boolean; reminders_enabled: boolean; client_notice_enabled: "default" | "on" | "off"; client_notice: string;
}
const STATES = [
  { value: "planned", label: "Planned", tone: "neutral" }, { value: "confirmed", label: "Confirmed", tone: "info" },
  { value: "done", label: "Done", tone: "success" }, { value: "cancelled", label: "Cancelled", tone: "danger" },
] as const;
const EMAIL_STATUS: Record<string, string> = {
  off: "Assignment email off", queued: "Assignment email queued", blocked: "Assignment email not configured",
  sent: "Assignment email accepted by mail server", failed: "Assignment email failed", unknown: "Assignment email outcome unknown",
};
function feedValue(value: FeedToken): FeedToken {
  if (!value?.token || !/^\/api\/v1\/calendar\/feed\/[A-Za-z0-9_-]+\.ics$/.test(value.path)) throw new Error("Calendar subscription link unavailable. Try loading it again.");
  return value;
}

/** Provider polling is separate from API synchronization. */
function PhoneSync() {
  const { user, org } = useAuth();
  const scope = `${user?.id ?? ""}:${org?.id ?? ""}`;
  const currentScope = useRef(scope); currentScope.current = scope;
  useEffect(() => { currentScope.current = scope; return () => { currentScope.current = ""; }; }, [scope]);
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [replace, setReplace] = useState(false);
  const key = ["schedule-feed-token", user?.id, org?.id];
  const feed = useQuery<FeedToken>({ queryKey: key, enabled: open, retry: false,
    queryFn: async () => feedValue((await api.get("/schedule/feed-token")).data) });
  useEffect(() => { setCopied(false); setError(null); setReplace(false); }, [feed.data?.token, user?.id, org?.id]);
  const regenerate = useMutation({ mutationFn: async (writeScope: string) => {
    if (currentScope.current !== writeScope) throw new Error("The workspace changed. Load your subscription again.");
    return feedValue((await api.post("/schedule/feed-token/regenerate")).data);
  },
    onSuccess: (data, writeScope) => { if (currentScope.current !== writeScope) return; qc.setQueryData(key, data); setReplace(false); setError(null); },
    onError: (e, writeScope) => { if (currentScope.current !== writeScope) return; setError(apiError(e)); setReplace(false); } });
  const url = feed.data ? `${window.location.origin}${feed.data.path}` : null;
  return <section className="card space-y-3 p-4" aria-label="Calendar subscription">
    <div className="flex flex-wrap items-center justify-between gap-3"><div>
      <h2 className="text-sm font-semibold text-slate-700">Calendar subscription (ICS)</h2>
      <p className="text-xs text-slate-500">Read-only updates for Google, Outlook or Apple Calendar. Their refresh schedule controls updates, which can take hours.</p>
    </div><Button variant="secondary" size="sm" aria-expanded={open} onClick={() => setOpen(!open)}>{open ? "Hide subscription" : "Set up subscription"}</Button></div>
    {open && <div className="space-y-3 text-sm text-slate-600">
      <p>Anyone with this secret link can read your schedule. Replacing it stops future access through the old link; subscribers must use the new one.</p>
      {feed.isLoading && <p role="status">Loading your subscription link…</p>}
      {feed.isError && <ErrorState title="Couldn’t load the subscription link" onRetry={() => { void feed.refetch(); }} />}
      {error && <p role="alert" className="text-rose-600">{error}</p>}
      {!feed.isError && url && <div className="flex flex-wrap items-center gap-2">
        <code className="max-w-full overflow-x-auto rounded bg-slate-100 px-2 py-1 text-xs">{url}</code>
        <Button variant="secondary" size="sm" disabled={regenerate.isPending} onClick={async () => {
          const copyScope = scope;
          try { await navigator.clipboard.writeText(url); if (currentScope.current !== copyScope) return; setCopied(true); setError(null); }
          catch { if (currentScope.current !== copyScope) return; setCopied(false); setError("Couldn’t copy the link. Select it above and copy it manually."); }
        }}>{copied ? "Copied" : "Copy link"}</Button>
        <Button variant="secondary" size="sm" disabled={regenerate.isPending} onClick={() => setReplace(true)}>Replace link</Button>
        <a className="btn-ghost text-xs" href={url.replace(/^https?:/, "webcal:")}>Open in calendar app</a>
      </div>}
      <p className="text-xs text-text-secondary">Google: Other calendars → From URL. Outlook: Add calendar → Subscribe from web. Apple: Add Subscription Calendar.</p>
    </div>}
    <ConfirmDialog open={replace} onClose={() => setReplace(false)} onConfirm={() => regenerate.mutate(scope)}
      title="Replace subscription link?" confirmLabel="Replace link" loading={regenerate.isPending}>
      Calendar apps using the old link will need to subscribe again with the new link.
    </ConfirmDialog>
  </section>;
}
function editValues(row: Assignment): Editor {
  const start = new Date(row.starts_at), end = new Date(row.ends_at);
  return { original: row, project_id: row.project_id, assignee_user_id: row.assignee_user_id,
    date: localDateValue(start), end_date: localDateValue(row.all_day ? addLocalDays(end, -1) : end), start: localTimeValue(start), end: localTimeValue(end),
    all_day: row.all_day, note: row.note ?? "", notify_assignee: row.notify_assignee !== false, reminders_enabled: row.reminders_enabled !== false,
    client_notice_enabled: row.client_notice_enabled == null ? "default" : row.client_notice_enabled ? "on" : "off",
    client_notice: row.client_notice_hours_before == null ? "" : String(row.client_notice_hours_before) };
}

export default function Schedule() {
  const { cacheIdentity } = useAuth();
  // Form drafts, selected records and notices never survive an identity change.
  return <PlanningWorkspace key={cacheIdentity} />;
}

function PlanningWorkspace() {
  const qc = useQueryClient();
  const { user, org, hasPerm } = useAuth();
  const scope = `${user?.id ?? ""}:${org?.id ?? ""}`;
  const commandScope = { userId: user?.id ?? "", orgId: org?.id ?? "" };
  const currentScope = useRef(scope); currentScope.current = scope;
  useEffect(() => { currentScope.current = scope; return () => { currentScope.current = ""; }; }, [scope]);
  const plannerPermission = hasPerm("invoice.write");
  const tabId = useId();
  const [view, setView] = useState("board");
  const [weekStart, setWeekStart] = useState(() => startOfLocalWeek(new Date()));
  const [filterMember, setFilterMember] = useState("");
  const [filterProject, setFilterProject] = useState("");
  const [form, setForm] = useState<Editor | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [overlapNote, setOverlapNote] = useState<string | null>(null);
  const [staleForm, setStaleForm] = useState(false);
  const [uncertainSave, setUncertainSave] = useState(false);
  const [cancelTarget, setCancelTarget] = useState<Assignment | null>(null);
  const createOperation = useRef<{ controller: AbortController; generation: number; token: string | null } | null>(null);
  const [createStage, setCreateStage] = useState<"preparing" | "saving" | "checking" | "verifying" | null>(null);
  const [createMissing, setCreateMissing] = useState(false);
  useEffect(() => () => { createOperation.current?.controller.abort(); }, []);
  useEffect(() => {
    let restored: Editor | null = null;
    try { const saved = readPlanningCommand(commandScope); if (saved) restored = { ...saved.draft, original: null }; } catch { /* The recovery alert owns unreadable storage. */ }
    setForm(restored); setErr(null); setNotice(null); setOverlapNote(null); setCancelTarget(null); setStaleForm(false); setUncertainSave(false); setFilterMember(""); setFilterProject("");
  }, [scope]);
  let pendingCreate: SavedPlanningCommand | null = null;
  let createReferenceError: string | null = null;
  try { pendingCreate = readPlanningCommand(commandScope); }
  catch (error) { createReferenceError = apiError(error); }
  const frozenCreate = pendingCreate !== null || createReferenceError !== null;
  const weekEnd = addLocalDays(weekStart, 7);
  const days = Array.from({ length: 7 }, (_, i) => addLocalDays(weekStart, i));
  const timeZone = browserTimeZone();
  const assignments = useQuery<Assignment[]>({
    queryKey: ["assignments", user?.id, org?.id, weekStart.toISOString(), filterMember, filterProject],
    queryFn: async ({ signal }) => (await api.get("/schedule/assignments", { signal, timeout: PLANNING_TIMEOUT_MS, params: {
      start: weekStart.toISOString(), end: weekEnd.toISOString(),
      ...(filterMember ? { assignee_user_id: filterMember } : {}), ...(filterProject ? { project_id: filterProject } : {}),
    } })).data });
  const members = useQuery<Member[] | null>({ queryKey: ["schedule-members", user?.id, org?.id], enabled: plannerPermission, retry: false,
    queryFn: async () => { try { return (await api.get("/schedule/members")).data; } catch (e) { if (axios.isAxiosError(e) && e.response?.status === 403) return null; throw e; } } });
  const canPlan = plannerPermission && Array.isArray(members.data);
  const projects = useQuery<Project[]>({ queryKey: ["projects", user?.id, org?.id], enabled: canPlan,
    queryFn: async () => (await api.get("/masters/projects")).data });
  async function refreshAfterWrite(writeScope: string) {
    try { await assignments.refetch({ throwOnError: true }); return currentScope.current === writeScope; } catch { return false; }
  }
  const save = useMutation({
    mutationFn: async ({ draft, writeScope }: { draft: Editor; writeScope: string }) => {
      if (currentScope.current !== writeScope) throw new Error("The workspace changed. Open this assignment again.");
      const original = draft.original, base = original ? editValues(original) : null;
      if (!original) throw new Error("Open a new assignment request before creating work.");
      const changedWindow = !base || ["date", "end_date", "start", "end", "all_day"].some((k) => draft[k as keyof Editor] !== base[k as keyof Editor]);
      const body = { ...(changedWindow ? assignmentWindow(draft) : {}), all_day: draft.all_day,
        assignee_user_id: draft.assignee_user_id, note: draft.note || null,
        notify_assignee: draft.notify_assignee, reminders_enabled: draft.reminders_enabled,
        client_notice_enabled: draft.client_notice_enabled === "default" ? null : draft.client_notice_enabled === "on",
        ...(original ? { clear_note: !draft.note, set_client_notice: true, expected_revision: original.revision,
          client_notice_hours_before: draft.client_notice ? Number(draft.client_notice) : null }
          : { project_id: draft.project_id, ...(draft.client_notice ? { client_notice_hours_before: Number(draft.client_notice) } : {}) }) };
      return (await api.patch<WriteResult>(`/schedule/assignments/${original.id}`, body)).data;
    },
    onSuccess: async (data, { draft, writeScope }) => {
      if (currentScope.current !== writeScope) return;
      const refreshed = await refreshAfterWrite(writeScope);
      if (currentScope.current !== writeScope) return;
      setForm(null); setStaleForm(false); setUncertainSave(false); setErr(null);
      setNotice(refreshed ? (draft.original ? "Assignment updated." : "Assignment saved.") : "Assignment saved, but the work plan could not reload. Refresh the plan to check its current state.");
      setOverlapNote(data.overlaps.length ? `Saved with a scheduling overlap for ${data.assignment.assignee_email}. Check the other assignments before work starts.` : null);
      void qc.invalidateQueries({ queryKey: ["calendar-connections", user?.id, org?.id] });
    },
    onError: (e, { writeScope }) => {
      if (currentScope.current !== writeScope) return;
      const conflict = axios.isAxiosError(e) && e.response?.status === 409;
      const uncertain = axios.isAxiosError(e) && (!e.response || e.response.status >= 500);
      setStaleForm(conflict); setUncertainSave(uncertain);
      setErr(conflict ? "This assignment changed since you opened it. Reload the latest values before saving." : uncertain ? "The save outcome is unconfirmed. Check the saved plan before creating or changing this assignment again. Your draft is still here." : apiError(e));
    },
  });
  const hasSavedCreate = () => {
    try { return readPlanningCommand(commandScope) !== null; } catch { return true; }
  };
  const beginCreate = () => {
    if (!canPlan || createOperation.current) return null;
    const operation = { controller: new AbortController(), generation: tokenStore.generation(), token: tokenStore.get() };
    createOperation.current = operation; setCreateStage("preparing"); setCreateMissing(false); setErr(null);
    return operation;
  };
  const ownsCreate = (operation: NonNullable<typeof createOperation.current>) =>
    createOperation.current === operation && !operation.controller.signal.aborted && currentScope.current === scope &&
    operation.generation === tokenStore.generation() && operation.token === tokenStore.get();
  const finishCreate = (operation: NonNullable<typeof createOperation.current>) => {
    if (!ownsCreate(operation)) return;
    createOperation.current = null; setCreateStage(null);
  };
  const verifyCreate = async (original: PlanningWriteResult, saved: SavedPlanningCommand, operation: NonNullable<typeof createOperation.current>) => {
    if (!ownsCreate(operation)) return;
    setCreateStage("verifying");
    const current = await readCurrentPlanningAssignment(commandScope, saved, original, operation.controller.signal);
    if (!ownsCreate(operation)) return;
    await qc.cancelQueries({ queryKey: ["assignments", user?.id, org?.id] });
    if (!ownsCreate(operation)) return;
    await assignments.refetch({ throwOnError: true });
    if (!ownsCreate(operation)) return;
    clearPlanningCommand(commandScope, saved.key);
    setForm(null); setStaleForm(false); setUncertainSave(false); setErr(null);
    setNotice(current.revision > original.assignment.revision
      ? "Assignment created. Its details have changed since the original save; review the current plan below."
      : "Assignment saved.");
    setOverlapNote(original.overlaps.length ? `Saved with a scheduling overlap for ${original.assignment.assignee_email}. This warning belongs to the original save; check the current assignments before work starts.` : null);
    void qc.invalidateQueries({ queryKey: ["calendar-connections", user?.id, org?.id] });
  };
  const createAssignment = async (source: Editor) => {
    if (source.original || hasSavedCreate() || createReferenceError) return;
    const operation = beginCreate(); if (!operation) return;
    const frozen = { ...source };
    try {
      const saved = await preparePlanningCommand(commandScope, frozen, user?.email ?? "", operation.controller.signal);
      if (!ownsCreate(operation)) return;
      setCreateStage("saving");
      const original = await sendPlanningCommand(commandScope, saved, operation.controller.signal);
      await verifyCreate(original, saved, operation);
    } catch (error) {
      if (ownsCreate(operation)) setErr(apiError(error));
    } finally { finishCreate(operation); }
  };
  const checkAssignment = async (retry = false) => {
    const operation = beginCreate(); if (!operation) return;
    try {
      const saved = readPlanningCommand(commandScope);
      if (!saved) throw new Error("No saved assignment request remains. Review the plan before creating work.");
      setCreateStage(retry ? "saving" : "checking");
      const original = retry
        ? await sendPlanningCommand(commandScope, saved, operation.controller.signal, true)
        : await checkPlanningCommand(commandScope, saved, operation.controller.signal);
      if (!ownsCreate(operation)) return;
      if (!original) {
        setCreateMissing(true);
        setErr("No committed assignment is visible for this reference yet. Check again or explicitly retry the original request with its same key and details.");
        return;
      }
      await verifyCreate(original, saved, operation);
    } catch (error) {
      if (ownsCreate(operation)) setErr(apiError(error));
    } finally { finishCreate(operation); }
  };
  const transition = useMutation({
    mutationFn: async ({ row, status, writeScope }: { row: Assignment; status: string; writeScope: string }) => {
      if (currentScope.current !== writeScope) throw new Error("The workspace changed. Reload your work plan.");
      return (await api.post(`/schedule/assignments/${row.id}/transition`, { status, expected_revision: row.revision })).data;
    },
    onSuccess: async (_, { writeScope }) => {
      if (currentScope.current !== writeScope) return;
      const refreshed = await refreshAfterWrite(writeScope);
      if (currentScope.current !== writeScope) return;
      setCancelTarget(null); setErr(null); setNotice(refreshed ? "Assignment status updated." : "Status updated, but the work plan could not reload. Refresh the plan to check its current state.");
      void qc.invalidateQueries({ queryKey: ["calendar-connections", user?.id, org?.id] });
    },
    onError: (e, { writeScope }) => { if (currentScope.current !== writeScope) return; setCancelTarget(null); setErr(axios.isAxiosError(e) && e.response?.status === 409 ? "This assignment changed. Refresh the plan before choosing another status." : apiError(e)); },
  });
  const busy = save.isPending || transition.isPending || createStage !== null;
  function newAssignment() {
    if (hasSavedCreate() || createReferenceError || createOperation.current) return;
    setErr(null); setNotice(null); setStaleForm(false); setUncertainSave(false);
    const date = localDateValue(weekStart);
    setForm({ original: null, project_id: projects.data?.[0]?.id ?? "", assignee_user_id: members.data?.[0]?.user_id ?? "",
      date, end_date: date, start: "09:00", end: "17:00", all_day: false, note: "", notify_assignee: true,
      reminders_enabled: true, client_notice_enabled: "default", client_notice: "" });
  }
  async function reloadEditor() {
    if (!form?.original) return;
    const readScope = scope;
    try {
      const result = await assignments.refetch({ throwOnError: true });
      if (currentScope.current !== readScope) return;
      const current = result.data?.find((a) => a.id === form.original?.id);
      if (!current) { setErr("This assignment is no longer in the selected week. Close the editor and refresh your filters."); return; }
      setForm(editValues(current)); setStaleForm(false); setUncertainSave(false); setErr(null);
    } catch { if (currentScope.current === readScope) setErr("Couldn’t reload the assignment. Your edits are still here; try loading the latest values again."); }
  }
  function projectTitle(row: Assignment) { return row.project_name || projects.data?.find((p) => p.id === row.project_id)?.name || row.project_code || "Project assignment"; }
  function card(row: Assignment) {
    const state = STATES.find((s) => s.value === row.status), own = row.assignee_user_id === user?.id;
    const active = row.status === "planned" || row.status === "confirmed";
    const person = members.data?.find((m) => m.user_id === row.assignee_user_id)?.name || (own ? user?.name : null) || row.assignee_email;
    const start = new Date(row.starts_at), end = new Date(row.ends_at), endDate = row.all_day ? addLocalDays(end, -1) : end;
    const dates = localDateValue(start) === localDateValue(endDate) ? formatDate(row.starts_at) : `${formatDate(row.starts_at)} – ${formatDate(endDate.toISOString())}`;
    return <article key={row.id} data-assignment-id={row.id} className="rounded-xl border border-slate-200 bg-white p-3 shadow-sm">
      <div className="flex flex-wrap items-start justify-between gap-2"><div className="min-w-0 flex-1 basis-28"><h3 className="break-words text-sm font-semibold text-slate-800">{projectTitle(row)}</h3>
        {(row.project_code || projects.data?.find((p) => p.id === row.project_id)?.code) && <p className="text-xs text-slate-500">{row.project_code || projects.data?.find((p) => p.id === row.project_id)?.code}</p>}
      </div><Badge tone={state?.tone ?? "neutral"}>{state?.label ?? row.status}</Badge></div>
      <p className="mt-2 text-xs font-medium text-slate-700">{person}</p><p className="text-xs text-slate-500">{dates}</p>
      <p className="text-xs text-slate-500">{row.all_day ? "All day" : `${formatTime(row.starts_at)}–${formatTime(row.ends_at)}`}</p>
      {row.note && <p className="mt-2 whitespace-pre-wrap break-words text-xs text-text-secondary">{row.note}</p>}
      {row.notification_status && <p className="mt-2 text-xs text-slate-500">{EMAIL_STATUS[row.notification_status] ?? "Email status unavailable"}</p>}
      <AssignmentNotifications assignmentId={row.id} revision={row.revision ?? 1} />
      {active && <div className="mt-3 flex flex-wrap gap-1 border-t border-slate-100 pt-2">
        {canPlan && <Button size="sm" variant="ghost" disabled={busy || !!form} onClick={() => { setForm(editValues(row)); setStaleForm(false); setUncertainSave(false); setErr(null); setNotice(null); }}>Edit</Button>}
        {(canPlan || own) && row.status === "planned" && <Button size="sm" variant="ghost" disabled={busy} onClick={() => transition.mutate({ row, status: "confirmed", writeScope: scope })}>Confirm</Button>}
        {(canPlan || own) && <Button size="sm" variant="ghost" disabled={busy} onClick={() => transition.mutate({ row, status: "done", writeScope: scope })}>Mark done</Button>}
        {canPlan && row.status === "confirmed" && <Button size="sm" variant="ghost" disabled={busy} onClick={() => transition.mutate({ row, status: "planned", writeScope: scope })}>Return to planned</Button>}
        {canPlan && <Button size="sm" variant="ghost" disabled={busy} className="text-rose-600" onClick={() => setCancelTarget(row)}>Cancel assignment</Button>}
      </div>}
    </article>;
  }
  return <div className="space-y-5">
    <header className="flex flex-wrap items-start justify-between gap-3"><div><h1 className="text-2xl font-semibold tracking-tight">Work planning</h1>
      <p className="text-sm text-slate-500">{plannerPermission ? "Plan assignments, see who is working and track progress." : "Your assignments. Confirm when you accept the plan; mark done when the work is finished."}</p></div>
      <div className="flex flex-wrap gap-2"><Button variant="secondary" disabled={busy || assignments.isFetching} onClick={() => { void assignments.refetch(); }}>Refresh plan</Button>
        {plannerPermission && members.data !== null && <Button className="scroll-mt-24" onClick={newAssignment} disabled={busy || frozenCreate || !!form || !canPlan || !projects.data?.length || assignments.isError}>New assignment</Button>}</div>
    </header>
    {createReferenceError && <p role="alert" className="text-sm text-rose-700">{createReferenceError}</p>}
    {pendingCreate && <section aria-label="Saved assignment request" className="card min-w-0 space-y-3 p-4 [overflow-wrap:anywhere]">
      <h2 className="text-sm font-semibold">An assignment request needs checking.</h2>
      <p className="text-sm">Request reference: {pendingCreate.key}. The original dates, person and work details are kept in the locked draft.</p>
      <p className="text-sm text-text-secondary">Check its saved outcome before creating a replacement. Checking only reads; email and calendar delivery status are separate.</p>
      <div className="flex flex-wrap gap-2">
        <Button size="sm" variant="secondary" disabled={!canPlan || busy} onClick={() => void checkAssignment()}>Check assignment outcome</Button>
        {createMissing && <Button size="sm" variant="secondary" disabled={!canPlan || busy} onClick={() => void checkAssignment(true)}>Retry original assignment request</Button>}
      </div>
      {createStage && <p role="status" className="text-sm text-text-secondary">{createStage === "saving" ? "Saving the original assignment request…" : createStage === "checking" ? "Checking the original assignment outcome…" : "Verifying the assignment and current plan…"}</p>}
      {!canPlan && <p className="text-sm text-text-secondary">Planning permission is required to check this request.</p>}
    </section>}
    {err && <div role="alert" className="rounded-lg border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">{err}</div>}
    {notice && <p role="status" className="rounded-lg bg-blue-50 px-4 py-3 text-sm text-blue-800">{notice}</p>}
    {overlapNote && <p role="status" className="rounded-lg bg-amber-50 px-4 py-3 text-sm text-amber-800">{overlapNote}</p>}
    {members.isError && <ErrorState title="Couldn’t load the planning team" onRetry={() => { void members.refetch(); }} />}
    {plannerPermission && members.data === null && <p role="status" className="text-sm text-slate-600">Planning access is unavailable. Your own assignments remain visible.</p>}
    {projects.isError && <ErrorState title="Couldn’t load projects for planning" onRetry={() => { void projects.refetch(); }} />}
    {canPlan && projects.data?.length === 0 && <p className="text-sm text-slate-600">Create a project before assigning work. <Link to="/cost-objects" className="font-medium underline">Open projects</Link></p>}
    <div className="card flex flex-wrap items-center justify-between gap-3 p-3"><div>
      <p className="text-sm font-semibold text-slate-700">{formatDate(weekStart.toISOString())} – {formatDate(addLocalDays(weekEnd, -1).toISOString())}</p>
      <p className="text-xs text-slate-500">Selected week · Times shown in {timeZone}</p>
    </div><div className="flex flex-wrap gap-1"><Button size="sm" variant="secondary" disabled={busy} onClick={() => setWeekStart(addLocalDays(weekStart, -7))}>← Previous week</Button>
      <Button size="sm" variant="secondary" disabled={busy} onClick={() => setWeekStart(startOfLocalWeek(new Date()))}>This week</Button>
      <Button size="sm" variant="secondary" disabled={busy} onClick={() => setWeekStart(addLocalDays(weekStart, 7))}>Next week →</Button></div>
    </div>
    {canPlan && <div className="flex flex-wrap gap-3">
      <label className="min-w-44 flex-1 text-xs font-medium text-slate-600">Team member<select aria-label="Filter by team member" className="input mt-1" value={filterMember} disabled={busy} onChange={(e) => setFilterMember(e.target.value)}><option value="">Everyone</option>{members.data?.map((m) => <option key={m.user_id} value={m.user_id}>{m.name || m.email}</option>)}</select></label>
      <label className="min-w-44 flex-1 text-xs font-medium text-slate-600">Project<select aria-label="Filter by project" className="input mt-1" value={filterProject} disabled={busy} onChange={(e) => setFilterProject(e.target.value)}><option value="">All projects</option>{projects.data?.map((p) => <option key={p.id} value={p.id}>{p.code} · {p.name}</option>)}</select></label>
    </div>}
    {form && <form aria-label="Assignment editor" className="card space-y-4 p-5" onSubmit={(e) => { e.preventDefault(); if (!busy && !staleForm && !uncertainSave && !frozenCreate) { if (form.original) save.mutate({ draft: form, writeScope: scope }); else void createAssignment(form); } }}>
      <div><h2 className="text-lg font-semibold text-slate-800">{form.original ? "Edit assignment" : "New assignment"}</h2><p className="text-xs text-slate-500">Times use {!form.original && pendingCreate ? pendingCreate.payload.time_zone : timeZone}. Overlaps are warnings; they do not block saving.</p></div>
      <fieldset disabled={busy || frozenCreate} className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <div><label className="label" htmlFor="plan-project">Project</label><select id="plan-project" className="input" required value={form.project_id} disabled={!!form.original} onChange={(e) => setForm({ ...form, project_id: e.target.value })}>{projects.data?.map((p) => <option key={p.id} value={p.id}>{p.code} · {p.name}</option>)}</select></div>
        <div><label className="label" htmlFor="plan-person">Person</label><select id="plan-person" className="input" required value={form.assignee_user_id} onChange={(e) => setForm({ ...form, assignee_user_id: e.target.value })}>{members.data?.map((m) => <option key={m.user_id} value={m.user_id}>{m.name || m.email}</option>)}</select></div>
        <div><label className="label" htmlFor="plan-date">Start date</label><input id="plan-date" className="input" type="date" required value={form.date} onChange={(e) => setForm({ ...form, date: e.target.value, end_date: form.end_date < e.target.value ? e.target.value : form.end_date })} /></div>
        <div><label className="label" htmlFor="plan-end-date">End date</label><input id="plan-end-date" className="input" type="date" required value={form.end_date} min={form.date} onChange={(e) => setForm({ ...form, end_date: e.target.value })} /></div>
        <label className="flex items-center gap-2 text-sm text-slate-600"><input type="checkbox" checked={form.all_day} onChange={(e) => setForm({ ...form, all_day: e.target.checked })} />All day</label>
        {!form.all_day && <><div><label className="label" htmlFor="plan-from">From</label><input id="plan-from" type="time" className="input" required value={form.start} onChange={(e) => setForm({ ...form, start: e.target.value })} /></div><div><label className="label" htmlFor="plan-to">To</label><input id="plan-to" type="time" className="input" required value={form.end} onChange={(e) => setForm({ ...form, end: e.target.value })} /></div></>}
      </fieldset>
      {form.all_day && <p className="text-xs text-slate-500">All-day work includes the selected end date.</p>}
      <div><label className="label" htmlFor="plan-note">Work details</label><textarea id="plan-note" className="input min-h-20" maxLength={2000} disabled={busy || frozenCreate} value={form.note} placeholder="What needs doing, access details or equipment" onChange={(e) => setForm({ ...form, note: e.target.value })} /></div>
      <fieldset disabled={busy || frozenCreate} className="space-y-3 rounded-lg border border-slate-200 bg-slate-50 p-3"><legend className="px-1 text-sm font-semibold text-slate-700">Email notifications</legend>
        <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={form.notify_assignee} onChange={(e) => setForm({ ...form, notify_assignee: e.target.checked })} />Email the assigned person about changes</label>
        <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={form.reminders_enabled} onChange={(e) => setForm({ ...form, reminders_enabled: e.target.checked })} />Send an upcoming-work reminder</label>
        <div className="grid gap-3 sm:grid-cols-2"><div><label className="label" htmlFor="plan-client-notice-enabled">Customer arrival email</label><select id="plan-client-notice-enabled" className="input" value={form.client_notice_enabled} onChange={(e) => setForm({ ...form, client_notice_enabled: e.target.value as Editor["client_notice_enabled"] })}><option value="default">Use workspace preference</option><option value="off">Off for this assignment</option><option value="on">On for this assignment</option></select></div>
          <div><label className="label" htmlFor="plan-client-notice-hours">Customer arrival notice (this assignment)</label><select id="plan-client-notice-hours" className="input" disabled={form.client_notice_enabled === "off"} value={form.client_notice} onChange={(e) => setForm({ ...form, client_notice: e.target.value })}><option value="">Workspace timing</option><option value="24">24 hours before</option><option value="48">48 hours before</option><option value="72">72 hours before</option>{form.client_notice && !["24", "48", "72"].includes(form.client_notice) && <option value={form.client_notice}>{form.client_notice} hours before (current)</option>}</select></div></div>
        <p className="text-xs text-slate-500">Customer email requires a customer address on the project. Email delivery status is separate from saving an assignment.</p>
      </fieldset>
      {(staleForm || (uncertainSave && form.original)) && <p role="status" className="text-sm text-amber-700">Reloading replaces this editor’s values with the current saved assignment. <Button size="sm" variant="secondary" disabled={assignments.isFetching} onClick={() => { void reloadEditor(); }}>Reload latest values</Button></p>}
      {uncertainSave && !form.original && !frozenCreate && <div role="status" className="space-y-2 text-sm text-amber-700"><p>Saving again could create a duplicate. Check this week’s saved assignments below, including any changed filters. Close the editor after reviewing the result.</p><Button size="sm" variant="secondary" disabled={assignments.isFetching} onClick={() => { void assignments.refetch(); }}>Check saved plan</Button></div>}
      <div className="flex flex-wrap gap-2"><Button type="submit" loading={save.isPending || createStage === "saving"} disabled={busy || frozenCreate || staleForm || uncertainSave || !form.project_id || !form.assignee_user_id}>{form.original ? "Save changes" : "Save assignment"}</Button><Button variant="secondary" disabled={busy || frozenCreate} onClick={() => { setForm(null); setStaleForm(false); setUncertainSave(false); }}>Close editor</Button></div>
    </form>}
    <Tabs label="Work planning views" idBase={tabId} tabs={[{ value: "board", label: "Board" }, { value: "calendar", label: "Calendar" }]} value={view} onChange={setView} />
    <TabPanel idBase={tabId} value={view}><QueryState query={assignments} loading={<Skeleton className="h-64 w-full" />} errorTitle="Couldn’t load the work plan">{(rows) => <>
      {rows.length === 0 && <EmptyState title="No assignments in this week" description={canPlan ? "Choose New assignment to plan work, or change your filters." : "Your assignments will appear here when work is planned for you."} />}
      {view === "board" ? <div className="grid items-start gap-4 md:grid-cols-2 xl:grid-cols-4">{STATES.map((state) => <section key={state.value} aria-label={`${state.label} assignments`} className="space-y-3 rounded-xl border border-slate-200 bg-slate-50 p-3">
        <h2 className="flex items-center justify-between text-sm font-semibold text-slate-700">{state.label}<span className="rounded-full bg-white px-2 py-0.5 text-xs tabular-nums">{rows.filter((a) => a.status === state.value).length}</span></h2>
        {rows.filter((a) => a.status === state.value).map(card)}{!rows.some((a) => a.status === state.value) && <p className="py-3 text-xs text-slate-400">No assignments</p>}
      </section>)}</div> : <div className="space-y-2"><p className="text-xs text-slate-500">Scroll across to see all seven days.</p><div role="region" aria-label="Weekly calendar" tabIndex={0} className="overflow-x-auto"><div className="grid min-w-[1400px] grid-cols-7 gap-3">{days.map((day) => <section key={localDateValue(day)} aria-label={formatWeekday(day.toISOString())} className="min-w-0 space-y-3 rounded-xl border border-slate-200 bg-slate-50 p-3">
        <h2 className="text-xs font-semibold text-slate-700">{formatWeekday(day.toISOString())}</h2>
        {rows.filter((a) => new Date(a.starts_at) < addLocalDays(day, 1) && new Date(a.ends_at) > day).map(card)}
        {!rows.some((a) => new Date(a.starts_at) < addLocalDays(day, 1) && new Date(a.ends_at) > day) && <p className="py-3 text-xs text-slate-400">No assignments</p>}
      </section>)}</div></div></div>}
    </>}</QueryState></TabPanel>
    <CalendarConnections disabled={!!form || busy} />
    <PhoneSync />
    <ConfirmDialog open={!!cancelTarget} onClose={() => setCancelTarget(null)} onConfirm={() => { if (cancelTarget) transition.mutate({ row: cancelTarget, status: "cancelled", writeScope: scope }); }}
      title="Cancel this assignment?" confirmLabel="Cancel assignment" cancelLabel="Keep assignment" tone="danger" loading={transition.isPending}>
      {cancelTarget && <>Cancel {projectTitle(cancelTarget)} on {formatDate(cancelTarget.starts_at)}? This closes the assignment. Its calendar and email preferences will be applied.</>}
    </ConfirmDialog>
  </div>;
}
