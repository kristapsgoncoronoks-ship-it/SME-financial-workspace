import { canShowProjectFigures } from "../lib/projectProfit";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { Badge, Button, Card, EmptyState, Modal, QueryState, Skeleton } from "../components/ui";
import { api, apiError } from "../lib/api";
import type { CostMaster, MasterStatus, ProjectPnl } from "../lib/types";

type Kind = "departments" | "cost-centers" | "projects";

const KINDS: { kind: Kind; label: string; singular: string }[] = [
  { kind: "departments", label: "Departments", singular: "department" },
  { kind: "cost-centers", label: "Cost centers", singular: "cost center" },
  { kind: "projects", label: "Projects", singular: "project" },
];

const EMPTY_FORM = { code: "", name: "", department_id: "", start_date: "", end_date: "" };
type MasterContext = { kind: Kind; draftVersion: number };
type CreateMaster = MasterContext & { body: Record<string, unknown> };
type RenameMaster = MasterContext & { id: string; name: string; version: number };
type TransitionMaster = MasterContext & { id: string; version: number; status: MasterStatus };

const STATUS_TONE: Record<MasterStatus, "success" | "warning" | "neutral"> = {
  active: "success",
  closed: "warning",
  archived: "neutral",
};

/** Legal next statuses per kind — mirrors the server's transition table
 * (`costing._TRANSITIONS`); the server remains the enforcement. */
function nextStatuses(kind: Kind, status: MasterStatus): MasterStatus[] {
  if (kind === "projects") {
    if (status === "active") return ["closed", "archived"];
    if (status === "closed") return ["active", "archived"];
    return ["active"];
  }
  return status === "active" ? ["archived"] : ["active"];
}

/** Cost-allocation masters: the departments, cost centers and projects the
 * invoice/expense dimension tags resolve against. Archived, never deleted. */
export default function CostObjectsPage() {
  const qc = useQueryClient();
  const { hasPerm } = useAuth();
  const admin = hasPerm("settings.manage"); // cosmetic — the server enforces SETTINGS_MANAGE
  const [searchParams, setSearchParams] = useSearchParams();
  const kind = KINDS.find((entry) => entry.kind === searchParams.get("kind"))?.kind ?? "departments";
  const setKind = (next: Kind) => setSearchParams((previous) => {
    const params = new URLSearchParams(previous);
    if (next === "departments") params.delete("kind");
    else params.set("kind", next);
    return params;
  });
  const [showArchived, setShowArchived] = useState(false);
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<CostMaster | null>(null);
  const [dialogKind, setDialogKind] = useState<Kind | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [form, setForm] = useState({ ...EMPTY_FORM });
  const [editName, setEditName] = useState("");
  const currentKind = useRef(kind);
  currentKind.current = kind;
  const draftVersion = useRef(0);

  useEffect(() => {
    // Browser history can change kind while a dialog is open. Rendering is
    // guarded by its origin immediately; discard that draft after navigation.
    draftVersion.current += 1;
    setCreating(false);
    setEditing(null);
    setDialogKind(null);
    setForm({ ...EMPTY_FORM });
    setEditName("");
    setErr(null);
  }, [kind]);

  const context = (): MasterContext => ({ kind, draftVersion: draftVersion.current });
  const isCurrent = (origin: MasterContext) => origin.kind === currentKind.current && origin.draftVersion === draftVersion.current;
  const beginDraft = () => {
    draftVersion.current += 1;
    setDialogKind(kind);
    setErr(null);
  };
  const closeDraft = () => {
    draftVersion.current += 1;
    setCreating(false);
    setEditing(null);
    setDialogKind(null);
  };

  const meta = KINDS.find((k) => k.kind === kind)!;

  const rowsQuery = useQuery<CostMaster[]>({
    queryKey: ["masters", kind, showArchived],
    queryFn: async () =>
      (await api.get(`/masters/${kind}${showArchived ? "?include_archived=true" : ""}`)).data,
  });
  // Department picker for cost-center creation.
  const departments = useQuery<CostMaster[]>({
    queryKey: ["masters", "departments", false],
    queryFn: async () => (await api.get("/masters/departments")).data,
    enabled: kind === "cost-centers",
  });

  // "Which contracts lose money" — the list's real question (projects only).
  const summary = useQuery<ProjectPnl[]>({
    queryKey: ["masters", "projects-pnl-summary"],
    queryFn: async () => (await api.get("/masters/projects-pnl-summary")).data,
    enabled: kind === "projects",
  });
  const pnlFor = (id: string) => summary.data?.find((r) => r.project_id === id);

  const refresh = () => qc.invalidateQueries({ queryKey: ["masters"] });

  const create = useMutation({
    mutationFn: async (command: CreateMaster) =>
      (await api.post(`/masters/${command.kind}`, command.body)).data as CostMaster,
    onSuccess: (_data, command) => {
      if (isCurrent(command)) {
        setCreating(false);
        setDialogKind(null);
        setForm({ ...EMPTY_FORM });
        setErr(null);
      }
      refresh();
    },
    onError: (e, command) => { if (isCurrent(command)) setErr(apiError(e)); },
  });

  const rename = useMutation({
    mutationFn: async (command: RenameMaster) =>
      (
        await api.patch(`/masters/${command.kind}/${command.id}`, {
          name: command.name,
          version: command.version,
        })
      ).data,
    onSuccess: (_data, command) => {
      if (isCurrent(command)) {
        setEditing(null);
        setDialogKind(null);
        setErr(null);
      }
      refresh();
    },
    onError: (e, command) => {
      if (isCurrent(command)) {
        setErr(apiError(e));
        setEditing(null);
        setDialogKind(null);
      }
      // A 409 means our version is stale — refetch so the next edit carries the fresh one.
      qc.invalidateQueries({ queryKey: ["masters"] });
    },
  });

  const transition = useMutation({
    mutationFn: async (command: TransitionMaster) =>
      (await api.patch(`/masters/${command.kind}/${command.id}`, { status: command.status, version: command.version })).data,
    onSuccess: (_data, command) => {
      if (isCurrent(command)) setErr(null);
      refresh();
    },
    onError: (e, command) => {
      if (isCurrent(command)) setErr(apiError(e));
      qc.invalidateQueries({ queryKey: ["masters"] });
    },
  });

  const deptName = (id: string | null | undefined) =>
    departments.data?.find((d) => d.id === id)?.name ?? (id ? "…" : "—");

  const input = "rounded-lg border border-slate-300 px-2 py-1 text-sm w-full";
  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-xl font-semibold">Cost objects</h1>
          <p className="text-slate-500">
            The departments, cost centers and projects that spend is allocated to. Invoice and
            expense dimension tags resolve against these masters by code or name.
          </p>
        </div>
        {admin && <Button onClick={() => { beginDraft(); setCreating(true); }}>New {meta.singular}</Button>}
      </div>

      {err && (
        <div role="alert" className="rounded-lg border border-rose-200 bg-rose-50 px-4 py-2 text-sm text-rose-700">
          {err}
        </div>
      )}

      <div className="flex min-w-0 flex-wrap items-center justify-between gap-3">
        <div className="flex min-w-0 max-w-full flex-wrap gap-1 rounded-lg bg-slate-100 p-1" role="tablist" aria-label="Master kind">
          {KINDS.map((k) => (
            <button
              key={k.kind}
              role="tab"
              aria-selected={kind === k.kind}
              className={`rounded-md px-3 py-1 text-sm ${
                kind === k.kind ? "bg-white font-medium shadow-sm" : "text-slate-500"
              }`}
              onClick={() => setKind(k.kind)}
            >
              {k.label}
            </button>
          ))}
        </div>
        <label className="flex items-center gap-2 text-sm text-slate-500">
          <input
            type="checkbox"
            checked={showArchived}
            onChange={(e) => setShowArchived(e.target.checked)}
          />
          Show archived
        </label>
      </div>

      <Card>
        <QueryState
          query={rowsQuery}
          loading={<Skeleton className="h-24 w-full" />}
          isEmpty={(rows) => rows.length === 0}
          empty={
            <EmptyState
              title={`No ${meta.label.toLowerCase()} yet`}
              description={
                admin
                  ? `Create your first ${meta.singular} — free-text dimension tags on invoices and expenses will link to it automatically.`
                  : `An administrator hasn't created any ${meta.label.toLowerCase()} yet.`
              }
            />
          }
          errorTitle={`Couldn’t load ${meta.label.toLowerCase()}`}
        >
          {(rows) => (
            <div className="min-w-0 max-w-full overflow-x-auto" role="region" aria-label={`${meta.label} table`} tabIndex={0}>
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-text-secondary">
                  <th className="py-1">Code</th>
                  <th className="py-1">Name</th>
                  {kind === "cost-centers" && <th className="py-1">Department</th>}
                  {kind === "projects" && <th className="py-1">Dates</th>}
                  {kind === "projects" && <th className="py-1 text-right">Profit</th>}
                  {kind === "projects" && <th className="py-1 text-right">Margin</th>}
                  <th className="py-1">Status</th>
                  {admin && <th className="py-1"></th>}
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.id} className="border-t border-slate-100">
                    <td className="py-1 font-mono text-xs font-medium">
                      {kind === "projects" ? (
                        <Link to={`/projects/${row.id}`} className="text-brand-600 hover:underline">
                          {row.code}
                        </Link>
                      ) : (
                        row.code
                      )}
                    </td>
                    <td className="py-1">{row.name}</td>
                    {kind === "cost-centers" && (
                      <td className="py-1 text-slate-500">{deptName(row.department_id)}</td>
                    )}
                    {kind === "projects" && (
                      <td className="py-1 text-xs text-slate-500">
                        {row.start_date ?? "—"} → {row.end_date ?? "open"}
                      </td>
                    )}
                    {kind === "projects" && (
                      <td className="py-1 text-right tabular-nums text-xs">
                        {canShowProjectFigures(pnlFor(row.id)) ? `${pnlFor(row.id)!.profit} €` : pnlFor(row.id) ? "Incomplete / unverified" : "—"}
                      </td>
                    )}
                    {kind === "projects" && (
                      <td className="py-1 text-right tabular-nums text-xs">
                        {canShowProjectFigures(pnlFor(row.id)) && pnlFor(row.id)?.margin_pct != null
                          ? `${pnlFor(row.id)!.margin_pct}%`
                          : "—"}
                      </td>
                    )}
                    <td className="py-1">
                      <Badge tone={STATUS_TONE[row.status]}>{row.status}</Badge>
                    </td>
                    {admin && (
                      <td className="py-1 text-right">
                        <Button
                          size="sm"
                          variant="secondary"
                          onClick={() => {
                            beginDraft();
                            setEditing(row);
                            setEditName(row.name);
                          }}
                        >
                          Rename
                        </Button>{" "}
                        {nextStatuses(kind, row.status).map((s) => (
                          <Button
                            key={s}
                            size="sm"
                            variant="secondary"
                            loading={transition.isPending}
                            onClick={() => transition.mutate({ ...context(), id: row.id, version: row.version, status: s })}
                          >
                            {s === "active" ? "Reactivate" : s === "closed" ? "Close" : "Archive"}
                          </Button>
                        ))}
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
            </div>
          )}
        </QueryState>
      </Card>

      <Modal open={creating && dialogKind === kind} onClose={closeDraft} title={`New ${meta.singular}`}>
        <form
          className="space-y-3"
          onSubmit={(e) => {
            e.preventDefault();
            if (dialogKind !== kind) return;
            const body: Record<string, unknown> = { code: form.code.trim(), name: form.name.trim() };
            if (kind === "cost-centers" && form.department_id) body.department_id = form.department_id;
            if (kind === "projects") {
              if (form.start_date) body.start_date = form.start_date;
              if (form.end_date) body.end_date = form.end_date;
            }
            create.mutate({ ...context(), body });
          }}
        >
          <div className="grid grid-cols-2 gap-3">
            <label className="text-sm">
              Code
              <input
                className={input}
                required
                maxLength={40}
                value={form.code}
                onChange={(e) => setForm({ ...form, code: e.target.value })}
                placeholder={kind === "projects" ? "PRJ-1" : kind === "cost-centers" ? "CC-100" : "OPS"}
              />
            </label>
            <label className="text-sm">
              Name
              <input
                className={input}
                required
                maxLength={200}
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
              />
            </label>
          </div>
          {kind === "cost-centers" && (
            <label className="block text-sm">
              Department (optional)
              <select
                className={input}
                value={form.department_id}
                onChange={(e) => setForm({ ...form, department_id: e.target.value })}
              >
                <option value="">—</option>
                {(departments.data ?? []).map((d) => (
                  <option key={d.id} value={d.id}>
                    {d.code} — {d.name}
                  </option>
                ))}
              </select>
            </label>
          )}
          {kind === "projects" && (
            <div className="grid grid-cols-2 gap-3">
              <label className="text-sm">
                Start date (optional)
                <input
                  className={input}
                  type="date"
                  value={form.start_date}
                  onChange={(e) => setForm({ ...form, start_date: e.target.value })}
                />
              </label>
              <label className="text-sm">
                End date (optional)
                <input
                  className={input}
                  type="date"
                  value={form.end_date}
                  onChange={(e) => setForm({ ...form, end_date: e.target.value })}
                />
              </label>
            </div>
          )}
          <div className="flex flex-wrap justify-end gap-2 pt-2">
            <Button type="button" variant="secondary" onClick={closeDraft}>
              Cancel
            </Button>
            <Button type="submit" loading={create.isPending}>
              Create {meta.singular}
            </Button>
          </div>
        </form>
      </Modal>

      <Modal open={editing !== null && dialogKind === kind} onClose={closeDraft} title={`Rename ${meta.singular}`}>
        <form
          className="space-y-3"
          onSubmit={(e) => {
            e.preventDefault();
            if (editing && dialogKind === kind) rename.mutate({ ...context(), id: editing.id, version: editing.version, name: editName.trim() });
          }}
        >
          <label className="block text-sm">
            Name
            <input
              className={input}
              required
              maxLength={200}
              value={editName}
              onChange={(e) => setEditName(e.target.value)}
            />
          </label>
          <div className="flex flex-wrap justify-end gap-2 pt-2">
            <Button type="button" variant="secondary" onClick={closeDraft}>
              Cancel
            </Button>
            <Button type="submit" loading={rename.isPending}>
              Save
            </Button>
          </div>
        </form>
      </Modal>
    </div>
  );
}
