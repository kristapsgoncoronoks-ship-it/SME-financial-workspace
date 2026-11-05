import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useToast } from "../components/Toast";
import { Switch } from "../components/Switch";
import { useAuth } from "../auth/AuthContext";
import { api, apiError } from "../lib/api";
import { shortDate } from "../lib/format";
import { ASSIGNABLE_ROLES, ROLE_LABELS } from "../lib/roles";
import { Badge, Button, Card, DataTable, EmptyState, ErrorState, type Column } from "../components/ui";
import type { Invite, Member, UserRoleName } from "../lib/types";

export default function Team() {
  const { user, hasPerm } = useAuth();
  const qc = useQueryClient();
  const toast = useToast();
  const canManage = hasPerm("member.manage");
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<UserRoleName>("user");
  const [error, setError] = useState<string | null>(null);

  const members = useQuery<Member[]>({ queryKey: ["team", "members"], queryFn: async () => (await api.get("/team/members")).data });
  const invites = useQuery<Invite[]>({
    queryKey: ["team", "invites"],
    queryFn: async () => (await api.get("/team/invites")).data,
    enabled: canManage,
  });

  const invite = useMutation({
    mutationFn: async () => (await api.post("/team/invites", { email, role })).data,
    onSuccess: () => {
      setEmail("");
      setError(null);
      qc.invalidateQueries({ queryKey: ["team", "invites"] });
    },
    onError: (e) => setError(apiError(e)),
  });
  const revoke = useMutation({
    mutationFn: async (id: string) => api.delete(`/team/invites/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["team", "invites"] }),
    onError: (e) => toast.error(apiError(e)),
  });
  const patch = useMutation({
    mutationFn: async (v: { id: string; body: Partial<Member> }) => (await api.patch(`/team/members/${v.id}`, v.body)).data,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["team", "members"] }),
    onError: (e) => setError(apiError(e)),
  });

  const inviteLink = (t: string) => `${location.origin}/accept-invite?token=${t}`;

  const columns: Column<Member>[] = [
    {
      key: "member", header: "Member",
      cell: (m) => (
        <div>
          <div className="font-medium">{m.name}</div>
          <div className="text-xs text-text-secondary">{m.email}</div>
        </div>
      ),
    },
    {
      key: "role", header: "Role",
      cell: (m) =>
        canManage && m.id !== user?.id ? (
          <select
            aria-label={`Role for ${m.email}`}
            className="input w-32 py-1 text-xs"
            value={m.role}
            onChange={(e) => patch.mutate({ id: m.id, body: { role: e.target.value as UserRoleName } })}
          >
            {ASSIGNABLE_ROLES.map((r) => <option key={r} value={r}>{ROLE_LABELS[r]}</option>)}
          </select>
        ) : (
          <Badge tone="neutral">{ROLE_LABELS[m.role]}</Badge>
        ),
    },
    {
      key: "approver", header: "Expense approver",
      cell: (m) =>
        canManage ? (
          <Switch
            size="sm"
            checked={m.is_expense_approver}
            onChange={() => patch.mutate({ id: m.id, body: { is_expense_approver: !m.is_expense_approver } })}
            label={`Toggle expense approver for ${m.email}`}
          />
        ) : (
          <Badge tone={m.is_expense_approver ? "success" : "neutral"}>{m.is_expense_approver ? "approver" : "—"}</Badge>
        ),
    },
    { key: "joined", header: "Joined", cell: (m) => <span className="text-slate-500">{shortDate(m.created_at)}</span> },
    {
      key: "status", header: "Status",
      cell: (m) => <Badge tone={m.is_active ? "success" : "neutral"}>{m.is_active ? "active" : "disabled"}</Badge>,
    },
    ...(canManage
      ? [{
          key: "actions", header: "", align: "right" as const,
          cell: (m: Member) =>
            m.id !== user?.id ? (
              <Button variant="ghost" size="sm" onClick={() => patch.mutate({ id: m.id, body: { is_active: !m.is_active } })} disabled={patch.isPending}>
                {m.is_active ? "Disable" : "Enable"}
              </Button>
            ) : null,
        }]
      : []),
  ];

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Team</h1>
        <p className="text-sm text-slate-500">People in this workspace. Invites share a link — no email required.</p>
      </div>

      {error && <div role="alert" className="rounded-lg bg-rose-50 px-3 py-2 text-sm text-rose-600">{error}</div>}

      {canManage && (
        <Card title="Invite a member">
          <div className="space-y-3">
            <form aria-label="Invite a member" className="flex flex-wrap items-end gap-3" onSubmit={(event) => { event.preventDefault(); if (!invite.isPending) invite.mutate(); }}>
              <div className="flex-1">
                <label className="label" htmlFor="invite-email">Email</label>
                <input id="invite-email" className="input" type="email" required value={email} onChange={(e) => setEmail(e.target.value)} placeholder="name@company.com" />
              </div>
              <div>
                <label className="label" htmlFor="invite-role">Role</label>
                <select id="invite-role" className="input w-36" value={role} onChange={(e) => setRole(e.target.value as UserRoleName)}>
                  {ASSIGNABLE_ROLES.map((r) => <option key={r} value={r}>{ROLE_LABELS[r]}</option>)}
                </select>
              </div>
              <Button type="submit" loading={invite.isPending} disabled={!email}>
                Create invite
              </Button>
            </form>

            {invites.isError && (
              <ErrorState title="Couldn’t load the pending invites" onRetry={() => invites.refetch()} />
            )}
            {(invites.data ?? []).length > 0 && (
              <div className="space-y-2 border-t border-slate-100 pt-3">
                <div className="text-xs font-medium uppercase text-text-secondary">Pending invites</div>
                {invites.data!.map((iv) => (
                  <div key={iv.id} className="flex flex-wrap items-center gap-2 rounded-lg bg-slate-50 px-3 py-2 text-sm">
                    <span className="font-medium">{iv.email}</span>
                    <Badge tone="neutral">{ROLE_LABELS[iv.role]}</Badge>
                    <input aria-label="Invitation link" readOnly value={inviteLink(iv.token)} className="input flex-1 min-w-[200px] text-xs" onFocus={(e) => e.target.select()} />
                    <button className="text-brand-600 hover:underline" onClick={() => navigator.clipboard?.writeText(inviteLink(iv.token))}>copy</button>
                    <button className="text-rose-500 hover:underline" onClick={() => revoke.mutate(iv.id)} disabled={revoke.isPending}>revoke</button>
                  </div>
                ))}
              </div>
            )}
          </div>
        </Card>
      )}

      {members.isError ? (
        <ErrorState title="Couldn’t load the members" onRetry={() => members.refetch()} />
      ) : (
      <DataTable
        caption="Workspace members"
        columns={columns}
        rows={members.data}
        rowKey={(m) => m.id}
        loading={members.isLoading}
        empty={<EmptyState title="No members yet" description="Invite teammates to collaborate in this workspace." />}
      />
      )}
    </div>
  );
}
