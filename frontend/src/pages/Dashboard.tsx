import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import axios from "axios";
import { lazy, Suspense, useId, useState } from "react";
import { Link } from "react-router-dom";
import { ErrorBoundary } from "../components/ErrorBoundary";
import { Button, EmptyState, ErrorState, QueryState, Skeleton } from "../components/ui";
import { api } from "../lib/api";
import { money } from "../lib/format";
import type { DashboardData } from "../lib/types";

const DashboardSpendSnapshot = lazy(() => import("../components/DashboardSpendSnapshot"));

/**
 * The composed home dashboard (WO-16 / I1.1): "what needs me today". One
 * `GET /dashboard` projection — a section the server nulls (missing permission
 * or module off) renders NOTHING, so no role ever sees dead cards or 403s.
 * Every tile links to its filtered worklist; every figure is the canonical
 * service's own number (the SPA does no money math beyond formatting).
 */
export default function Dashboard() {
  const dash = useQuery<DashboardData>({
    queryKey: ["dashboard"],
    queryFn: async () => (await api.get("/dashboard")).data,
  });

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Today</h1>
        <p className="text-sm text-slate-500">
          What needs you — approvals, captures to review, and money at risk.
        </p>
      </div>

      <SetupChecklist />

      <NextActions />

      <QueryState query={dash} loading={<TilesSkeleton />} errorTitle="Couldn’t load your dashboard">
        {(d) => <DashboardBody d={d} />}
      </QueryState>
    </div>
  );
}

interface OnboardingStep {
  key: string;
  label: string;
  detail: string;
  href: string;
  done: boolean;
}

interface OnboardingOut {
  steps: OnboardingStep[];
  done_count: number;
  complete: boolean;
  dismissed: boolean;
  can_dismiss: boolean;
}

/** Getting-started checklist (WO-P / R19): DERIVED server-side from rows that
 * already exist — finish a step through its own screen and the card notices.
 * Renders nothing once complete or dismissed (dismissal is org-wide,
 * settings-authority only; the server says whether to offer the button). */
function SetupChecklist() {
  const qc = useQueryClient();
  const card = useQuery<OnboardingOut>({
    queryKey: ["onboarding"],
    queryFn: async () => (await api.get("/dashboard/onboarding")).data,
  });
  const dismiss = useMutation({
    mutationFn: async () => (await api.post("/dashboard/onboarding/dismiss")).data,
    onSuccess: (d: OnboardingOut) => qc.setQueryData(["onboarding"], d),
  });

  const d = card.data;
  if (!d || !Array.isArray(d.steps) || d.complete || d.dismissed) return null;

  return (
    <section className="card" aria-label="Getting started">
      <div className="mb-3 flex items-start justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold text-slate-600">Getting started</h2>
          <p className="text-xs text-slate-500">
            {d.done_count} of {d.steps.length} done — finish setting up your workspace.
          </p>
        </div>
        {d.can_dismiss && (
          <button
            className="text-xs text-text-secondary hover:text-slate-600 hover:underline"
            disabled={dismiss.isPending}
            onClick={() => dismiss.mutate()}
          >
            Dismiss
          </button>
        )}
      </div>
      <ol className="space-y-2">
        {d.steps.map((s) => (
          <li key={s.key} className="flex items-start gap-3">
            <span
              aria-hidden
              className={`mt-0.5 grid h-5 w-5 shrink-0 place-items-center rounded-full text-xs font-bold ${
                s.done ? "bg-emerald-100 text-emerald-600" : "bg-slate-100 text-slate-400"
              }`}
            >
              {s.done ? "\u2713" : "\u00b7"}
            </span>
            {s.done ? (
              <span className="text-sm text-text-secondary line-through">{s.label}</span>
            ) : (
              <span className="text-sm">
                <Link className="font-medium text-brand-600 hover:underline" to={s.href}>
                  {s.label}
                </Link>
                <span className="block text-xs text-slate-500">{s.detail}</span>
              </span>
            )}
          </li>
        ))}
      </ol>
    </section>
  );
}

interface NextAction {
  kind: string;
  ref_id: string;
  title: string;
  detail: string;
  link: string;
  age_days: number | null;
  due_date: string | null;
  dismissible: boolean;
}

/** Next actions (WO-C): DERIVED work items — an offer to follow up, an
 * invoice to chase, uploads to confirm, a filing coming due. Every item
 * clears itself when the underlying work happens; Dismiss silences one item
 * permanently. Renders nothing for roles without planning rights (403) and
 * nothing when the list is empty — an empty card would be noise. */
function NextActions() {
  const [err, setErr] = useState<string | null>(null);
  const actions = useQuery<NextAction[] | null>({
    queryKey: ["next-actions"],
    meta: { errorHandled: true },
    queryFn: async () => {
      try {
        return (await api.get("/next-actions")).data;
      } catch (error) {
        // Only the documented permission refusal means this surface is not
        // theirs. Availability failures need their own visible recovery.
        if (axios.isAxiosError(error) && error.response?.status === 403) return null;
        throw error;
      }
    },
  });
  const qc = useQueryClient();

  const dismiss = async (a: NextAction) => {
    try {
      await api.post("/next-actions/dismiss", { kind: a.kind, ref_id: a.ref_id });
      qc.invalidateQueries({ queryKey: ["next-actions"] });
    } catch {
      setErr("Couldn’t dismiss that item.");
    }
  };
  const complete = async (a: NextAction) => {
    try {
      await api.post(`/next-actions/deadlines/${a.ref_id}/complete`);
      qc.invalidateQueries({ queryKey: ["next-actions"] });
    } catch {
      setErr("Couldn’t mark that deadline done.");
    }
  };

  const items = Array.isArray(actions.data) ? actions.data : [];
  // A retry without cached data temporarily becomes pending again. Keep its
  // recovery surface mounted while that read is in flight.
  const readFailed = actions.isError || (actions.isPending && actions.errorUpdatedAt > 0);
  if (!readFailed && items.length === 0) return null;
  return (
    <section className="card space-y-2 p-6" aria-label="Next actions">
      <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">
        Next actions
      </h2>
      {readFailed && (
        <ErrorState
          title={items.length > 0 ? "Couldn’t refresh next actions" : "Couldn’t load next actions"}
          description={items.length > 0
            ? "Previously loaded actions are shown below. Refresh them before making changes."
            : "Next actions could not be checked. Try again, or come back in a moment."}
          onRetry={actions.isFetching ? undefined : () => { void actions.refetch({ cancelRefetch: false }); }}
          className="px-0! py-3!"
        />
      )}
      {readFailed && actions.isFetching && (
        <p role="status" className="text-sm text-text-secondary">Checking next actions…</p>
      )}
      {err && <div className="text-sm text-rose-600">{err}</div>}
      <ul className="divide-y divide-slate-100" aria-busy={actions.isFetching}>
        {items.map((a) => (
          <li key={`${a.kind}:${a.ref_id}`} className="flex items-start justify-between gap-3 py-2">
            <div className="min-w-0">
              <Link to={a.link} className="text-sm font-medium text-slate-700 hover:underline">
                {a.title}
              </Link>
              <div className="text-xs text-slate-500">{a.detail}</div>
            </div>
            <div className="flex shrink-0 gap-2">
              {a.kind === "deadline" && (
                <button className="btn-ghost text-xs" disabled={readFailed} onClick={() => complete(a)}>
                  Done
                </button>
              )}
              {a.dismissible && (
                <button className="btn-ghost text-xs text-text-secondary" disabled={readFailed} onClick={() => dismiss(a)}>
                  Dismiss
                </button>
              )}
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}

function TilesSkeleton() {
  return (
    <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
      {Array.from({ length: 4 }).map((_, i) => (
        <Skeleton key={i} className="h-28 w-full rounded-xl" />
      ))}
    </div>
  );
}

function DashboardBody({ d }: { d: DashboardData }) {
  const attention =
    (d.approvals?.total ?? 0) +
    (d.captures?.pending ?? 0) +
    (d.payables ? d.payables.overdue_count + d.payables.due_soon_count : 0) +
    (d.receivables && Number(d.receivables.overdue) > 0 ? 1 : 0);

  return (
    <div className="space-y-6">
      {/* Attention tiles — each links to its worklist. */}
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        {d.approvals && (
          <Tile
            to="/invoices?workflow_state=in_approval"
            label="Waiting on you"
            value={String(d.approvals.total)}
            sub="approvals across the workspace"
            tone={d.approvals.total > 0 ? "rose" : "slate"}
          />
        )}
        {d.captures && (
          <Tile
            to="/captures"
            label="Captures to review"
            value={String(d.captures.pending)}
            sub={
              d.captures.low_confidence_fields > 0
                ? `${d.captures.low_confidence_fields} low-confidence fields`
                : "nothing flagged"
            }
            tone={d.captures.low_confidence_fields > 0 ? "amber" : "slate"}
          />
        )}
        {d.payables && (
          <Tile
            to="/cash-position"
            label="Overdue payables"
            value={money(d.payables.overdue_amount, d.payables.currency)}
            sub={
              `${d.payables.overdue_count} invoice${d.payables.overdue_count === 1 ? "" : "s"}` +
              (d.payables.other_currencies.length > 0
                ? ` · + ${d.payables.other_currencies.join(", ")}`
                : "")
            }
            tone={d.payables.overdue_count > 0 ? "rose" : "slate"}
          />
        )}
        {d.payables && (
          <Tile
            to="/cash-position"
            label="Due within 7 days"
            value={money(d.payables.due_soon_amount, d.payables.currency)}
            sub={`${d.payables.due_soon_count} invoice${d.payables.due_soon_count === 1 ? "" : "s"}`}
            tone={d.payables.due_soon_count > 0 ? "amber" : "slate"}
          />
        )}
        {d.receivables && (
          <Tile
            to="/issue/reports"
            label="Overdue receivables"
            value={money(d.receivables.overdue, d.receivables.currency)}
            sub={`${money(d.receivables.outstanding, d.receivables.currency)} outstanding`}
            tone={Number(d.receivables.overdue) > 0 ? "rose" : "slate"}
          />
        )}
        {d.cash && (
          <Tile
            to="/cash-position"
            label="Working-capital gap"
            value={money(d.cash.net_position, d.cash.currency)}
            sub="receivables − payables — not a bank balance"
            tone={Number(d.cash.net_position) >= 0 ? "emerald" : "amber"}
          />
        )}
      </div>

      {attention === 0 && (
        <div className="card p-0">
          <EmptyState
            title="Nothing needs you right now"
            description="No approvals waiting, no captures to review, nothing overdue. New work will appear here."
          />
        </div>
      )}

      {d.approvals && d.approvals.total > 0 && <ApprovalsCard a={d.approvals} />}

      {/* Spend snapshot — only for roles the server gave the reporting section. */}
      {d.cash && <OptionalSpendSnapshot />}
    </div>
  );
}

function Tile({
  to,
  label,
  value,
  sub,
  tone = "slate",
}: {
  to: string;
  label: string;
  value: string;
  sub?: string;
  tone?: "slate" | "rose" | "amber" | "emerald";
}) {
  const tones = {
    slate: "text-slate-800",
    rose: "text-rose-600",
    amber: "text-amber-600",
    emerald: "text-emerald-600",
  } as const;
  return (
    <Link
      to={to}
      className="group rounded-xl border border-slate-200 bg-white p-5 shadow-xs transition hover:border-brand-300 hover:shadow-sm focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-500"
    >
      <div className="flex items-center justify-between text-sm font-medium text-slate-500">
        {label}
        <span aria-hidden className="text-slate-300 transition group-hover:text-brand-500">
          →
        </span>
      </div>
      <div className={`mt-1 text-2xl font-semibold tracking-tight tabular-nums ${tones[tone]}`}>
        {value}
      </div>
      {sub && <div className="mt-1 truncate text-xs text-text-secondary">{sub}</div>}
    </Link>
  );
}

function ApprovalsCard({ a }: { a: NonNullable<DashboardData["approvals"]> }) {
  const counters: { label: string; count: number | null; to: string }[] = [
    { label: "Expense reports", count: a.expense_reports, to: "/expenses" },
    { label: "Payment runs to check", count: a.payment_runs, to: "/payment-runs" },
    { label: "Supplier detail changes", count: a.vendor_changes, to: "/vendors" },
  ];
  const visible = counters.filter((c) => (c.count ?? 0) > 0);
  return (
    <div className="card p-0">
      <div className="flex items-center justify-between border-b border-slate-100 px-4 py-3">
        <h2 className="text-sm font-semibold text-slate-600">Waiting on you</h2>
        {a.invoices !== null && (
          <Link
            to="/invoices?workflow_state=in_approval"
            className="text-sm text-brand-600 hover:underline"
          >
            All invoices awaiting approval →
          </Link>
        )}
      </div>
      {a.invoices && a.invoices.length > 0 && (
        <table className="w-full text-sm">
          <thead className="border-b border-slate-100 text-left text-xs uppercase tracking-wide text-text-secondary">
            <tr>
              <th scope="col" className="px-4 py-2">Invoice</th>
              <th scope="col" className="px-4 py-2">Supplier</th>
              <th scope="col" className="px-4 py-2 text-right">Total</th>
              <th scope="col" className="px-4 py-2">
                <span className="sr-only">Action</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {a.invoices.map((inv) => (
              <tr
                key={inv.invoice_id}
                className="border-b border-slate-100 last:border-0 hover:bg-slate-50"
              >
                <td className="px-4 py-2 font-medium text-slate-700">{inv.invoice_number}</td>
                <td className="px-4 py-2 text-slate-500">{inv.vendor_name ?? "—"}</td>
                <td className="px-4 py-2 text-right tabular-nums">
                  {money(inv.total, inv.currency)}
                </td>
                <td className="px-4 py-2 text-right">
                  <Link
                    to={`/invoices/${inv.invoice_id}`}
                    className="font-medium text-brand-600 hover:underline"
                  >
                    Decide →
                  </Link>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {visible.length > 0 && (
        <div className="flex flex-wrap gap-x-6 gap-y-1 px-4 py-3 text-sm">
          {visible.map((c) => (
            <Link key={c.label} to={c.to} className="text-slate-600 hover:underline">
              <span className="font-semibold text-rose-600 tabular-nums">{c.count}</span> {c.label}{" "}
              →
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}

// Optional reporting starts only after an explicit choice. A chart load or
// render failure is contained here so approvals and daily work remain usable.
function OptionalSpendSnapshot() {
  const [expanded, setExpanded] = useState(false);
  const panelId = useId();
  return <section aria-label="Spend snapshot" className="space-y-4">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <h2 className="text-sm font-semibold text-slate-600">Spend snapshot</h2>
      <div className="flex flex-wrap items-center gap-3">
        <Link to="/explore" className="text-sm text-brand-600 hover:underline">Explore →</Link>
        <Button variant="secondary" aria-expanded={expanded} aria-controls={panelId} onClick={() => setExpanded(value => !value)}>{expanded ? "Hide spend snapshot" : "Show spend snapshot"}</Button>
      </div>
    </div>
    {expanded && <div id={panelId}><ErrorBoundary fallback={<ErrorState title="Spend snapshot could not load" description="Your worklist is still available. Reload the page to try the charts again." />}>
      <Suspense fallback={<div role="status" className="card"><p className="mb-3 text-sm text-text-secondary">Loading spend snapshot…</p><Skeleton className="h-40 w-full" /></div>}>
        <DashboardSpendSnapshot />
      </Suspense>
    </ErrorBoundary></div>}
  </section>;
}
