import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { AccountingExport } from "../components/AccountingExport";
import { Button, EmptyState, PageHeader, QueryState, Skeleton } from "../components/ui";
import { api, apiError } from "../lib/api";
import { money, shortDate, STATUS_STYLES } from "../lib/format";
import type { BulkDeleteResult, Invoice, InvoiceList, InvoiceStatus } from "../lib/types";

const STATUSES: (InvoiceStatus | "")[] = ["", "pending", "paid", "overdue", "draft"];
const PAGE_SIZE = 20;
const SEARCH_SETTLE_MS = 250;

// Mirrors `bulk.MAX_BATCH` server-side. Duplicated with a comment naming its
// source rather than fetched: if they drift the server still refuses, and this
// cap only decides whether the operator is told before they act or after.
const MAX_BATCH = 200;

// Human labels for the deep-linkable workflow-state filter (WO-16). The
// `in_approval` alias covers both live-chain states server-side.
const WORKFLOW_LABELS: Record<string, string> = {
  in_approval: "Awaiting approval",
  submitted: "Submitted",
  partially_approved: "Partially approved",
  approved: "Approved",
  scheduled_for_payment: "Scheduled for payment",
  partially_paid: "Partially paid",
  draft: "Draft",
  rejected: "Rejected",
  paid: "Paid",
};

function WorkflowBadge({ state }: { state?: string | null }) {
  return <span className="badge max-w-full whitespace-normal bg-slate-100 text-slate-700">{state ? WORKFLOW_LABELS[state] ?? state : "Not set"}</span>;
}

function InvoiceTotal({ invoice }: { invoice: Invoice }) {
  return (
    <div className="space-y-1">
      <p className="font-medium text-slate-800 tabular-nums">
        <span className="[overflow-wrap:anywhere]">{money(invoice.total, invoice.currency)}</span>{" "}
        <span className="text-xs font-normal text-text-secondary">{invoice.currency}</span>
      </p>
      <p className="text-xs text-text-secondary [overflow-wrap:anywhere]">Tax {money(invoice.tax_amount, invoice.currency)}</p>
    </div>
  );
}

export default function Invoices() {
  const { hasPerm } = useAuth();
  // Filters initialise from the URL so dashboard tiles deep-link a FILTERED
  // worklist (WO-16); `workflow_state` lives in the URL only (chip to clear).
  const [searchParams, setSearchParams] = useSearchParams();
  const workflowState = searchParams.get("workflow_state") ?? "";
  const [status, setStatus] = useState<string>(searchParams.get("status") ?? "");
  const [q, setQ] = useState(searchParams.get("q") ?? "");
  const [settledQ, setSettledQ] = useState(q);
  const [page, setPage] = useState(1);

  useEffect(() => {
    if (q === settledQ) return;
    const timer = window.setTimeout(() => {
      setSettledQ(q);
      setPage(1);
    }, SEARCH_SETTLE_MS);
    return () => window.clearTimeout(timer);
  }, [q, settledQ]);

  const applySearch = (value: string) => {
    setSettledQ(value);
    setPage(1);
  };

  const query = useQuery<InvoiceList>({
    queryKey: ["invoices", status, settledQ, page, workflowState],
    queryFn: async ({ signal }) => {
      const params = new URLSearchParams({ page: String(page), page_size: String(PAGE_SIZE) });
      if (status) params.set("status", status);
      if (settledQ) params.set("q", settledQ);
      if (workflowState) params.set("workflow_state", workflowState);
      return (await api.get(`/invoices?${params.toString()}`, { signal })).data;
    },
  });
  const { data } = query;

  const totalPages = data ? Math.max(1, Math.ceil(data.total / PAGE_SIZE)) : 1;

  // Multi-select delete. Safe to offer only because deletion is now REVERSIBLE:
  // everything this does lands in the recycle bin for 30 days.
  const qc = useQueryClient();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [result, setResult] = useState<BulkDeleteResult | null>(null);
  const [err, setErr] = useState<string | null>(null);

  // Only a DRAFT can be bulk-deleted, so only a draft is offered a checkbox: a
  // control on a row the server will only skip teaches the operator to distrust
  // every control. A draft can still be skipped (a recorded payment is not
  // visible in this list), and the outcomes below say so.
  const selectable = (d?: InvoiceList) =>
    (d?.items ?? []).filter((i) => (i.workflow_state ?? "draft") === "draft");

  const toggle = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const bulkDelete = useMutation({
    mutationFn: async (ids: string[]) =>
      (
        await api.post("/invoices/bulk-delete", {
          invoice_ids: ids,
          // GUARD 1, and it lives half on the client: the count this screen
          // DISPLAYED as selected. The server refuses the batch if it disagrees
          // with what arrived, so a selection built against a stale list cannot
          // apply — but only if this number is honest.
          agreed_count: ids.length,
          // GUARD 4. Never "everything matching the filter": that is a set the
          // operator never enumerated. Kept even though the bin made this
          // reversible — hundreds of invoices vanishing is still an incident.
          selection: "explicit",
        })
      ).data as BulkDeleteResult,
    onSuccess: (res) => {
      // Keep the per-record outcomes on screen. Collapsing a batch to "done"
      // discards the half the operator most needs: which were skipped, and why.
      setResult(res);
      setSelected(new Set());
      setErr(null);
      qc.invalidateQueries({ queryKey: ["invoices"] });
    },
    onError: (e: unknown) => setErr(apiError(e)),
  });

  return (
    <div className="min-w-0 space-y-4">
      <PageHeader
        title="Invoices"
        actions={
          <>
            {hasPerm("export.run") && <AccountingExport status={status} q={q} workflowState={workflowState} />}
            {/* Reachable from the list it came from rather than the global
                nav: somebody who has just deleted an invoice by mistake looks
                here, not in a sidebar. Deliberately NOT a nav item — a label
                containing "Invoices" also collides with the existing one. */}
            <Link to="/invoices/trash" className="btn-ghost">Deleted</Link>
            <Link to="/upload" className="btn-primary">Upload invoice</Link>
          </>
        }
      />

      <div className="flex flex-wrap items-end gap-3">
        <div className="min-w-0 w-full sm:w-auto">
          <label className="label" htmlFor="search-number">Search number</label>
          <input id="search-number"
            className="input w-full sm:w-56"
            placeholder="INV-2026-…"
            value={q}
            onChange={(e) => {
              setQ(e.target.value);
              if (!e.target.value) applySearch("");
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.nativeEvent.isComposing) {
                e.preventDefault();
                applySearch(q);
              }
            }}
          />
        </div>
        <div className="min-w-0 max-w-full">
          <label className="label" htmlFor="status">Status</label>
          <select id="status"
            className="input w-40 max-w-full"
            value={status}
            onChange={(e) => {
              setStatus(e.target.value);
              applySearch(q);
            }}
          >
            {STATUSES.map((s) => (
              <option key={s} value={s}>
                {s === "" ? "All" : s}
              </option>
            ))}
          </select>
        </div>
        {workflowState && (
          <span className="badge flex max-w-full items-center gap-1.5 bg-brand-50 text-brand-700">
            {WORKFLOW_LABELS[workflowState] ?? workflowState}
            <button
              type="button"
              aria-label="Clear workflow filter"
              className="text-brand-500 hover:text-brand-700"
              onClick={() => {
                searchParams.delete("workflow_state");
                setSearchParams(searchParams, { replace: true });
                applySearch(q);
              }}
            >
              ×
            </button>
          </span>
        )}
      </div>

      {err && (
        <div role="alert" className="rounded-md border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">
          {err}
        </div>
      )}

      {result && (
        <div className="rounded-md border border-slate-200 bg-slate-50 px-4 py-3 text-sm">
          <p className="font-medium text-slate-800">
            Moved {result.deleted} to deleted items
            {result.skipped > 0 && `, left ${result.skipped} alone`}.{" "}
            <Link to="/invoices/trash" className="text-brand-600 hover:underline">
              Restore within 30 days
            </Link>
          </p>
          {result.outcomes.some((o) => o.result === "skipped") && (
            <ul className="mt-2 space-y-1 text-slate-600">
              {result.outcomes
                .filter((o) => o.result === "skipped")
                .map((o) => (
                  // Skips are INFORMATION, not errors: an approved or paid
                  // invoice being left alone is the system working, and
                  // painting it red is how operators learn to ignore red.
                  <li key={o.ref_id}>{o.reason}</li>
                ))}
            </ul>
          )}
        </div>
      )}

      {selected.size > 0 && (
        <div className="flex flex-wrap items-center gap-3 rounded-md border border-slate-200 bg-white px-4 py-3 text-sm">
          <span className="font-medium text-slate-700">
            {selected.size} selected
          </span>
          <Button
            variant="secondary"
            size="sm"
            disabled={bulkDelete.isPending}
            onClick={() => bulkDelete.mutate([...selected])}
          >
            Delete
          </Button>
          <button className="btn-ghost py-1" onClick={() => setSelected(new Set())}>
            Clear
          </button>
          <span className="text-slate-500">
            Deleted invoices can be restored for 30 days.
          </span>
        </div>
      )}

      <div className="card @container min-w-0 p-0">
        <QueryState
          query={query}
          loading={<Skeleton className="m-4 h-40 w-[calc(100%-2rem)]" />}
          isEmpty={(d) => d.items.length === 0}
          empty={<EmptyState title="No invoices match these filters" />}
          errorTitle="Couldn’t load invoices"
        >
          {(d) => (
            <table className="w-full table-fixed text-sm" aria-label="Invoice worklist">
              <thead className="border-b border-slate-200 bg-slate-50 text-left text-xs uppercase tracking-wide text-slate-500">
                <tr>
                  <th scope="col" className="w-10 px-3 py-3 sm:w-12 sm:px-4">
                    <span className="sr-only">Selection</span>
                    {selectable(d).length > 0 && (
                      <input
                        type="checkbox"
                        aria-label="Select every draft on this page"
                        checked={
                          selectable(d).length > 0 &&
                          selectable(d).every((i) => selected.has(i.id))
                        }
                        onChange={(e) =>
                          setSelected((prev) => {
                            const next = new Set(prev);
                            for (const i of selectable(d)) {
                              if (e.target.checked) {
                                // Never build a batch the server would refuse
                                // outright: at the cap, stop adding.
                                if (next.size < MAX_BATCH) next.add(i.id);
                              } else next.delete(i.id);
                            }
                            return next;
                          })
                        }
                      />
                    )}
                  </th>
                  <th scope="col" className="px-3 py-3 @3xl:w-[30%] @3xl:px-4">Supplier &amp; invoice</th>
                  <th scope="col" className="hidden px-4 py-3 @3xl:table-cell @3xl:w-[18%]">Due date</th>
                  <th scope="col" className="hidden px-4 py-3 @3xl:table-cell @3xl:w-[24%]">Workflow</th>
                  <th scope="col" className="hidden px-4 py-3 text-right @3xl:table-cell">Total</th>
                </tr>
              </thead>
              <tbody>
                {d.items.map((inv) => (
                  <tr key={inv.id} className="border-b border-slate-100 last:border-0 hover:bg-slate-50">
                    <td className="px-3 py-3 align-top sm:px-4">
                      {(inv.workflow_state ?? "draft") === "draft" && (
                        <input
                          type="checkbox"
                          aria-label={`Select ${inv.invoice_number}`}
                          checked={selected.has(inv.id)}
                          disabled={!selected.has(inv.id) && selected.size >= MAX_BATCH}
                          onChange={() => toggle(inv.id)}
                        />
                      )}
                    </td>
                    <td className="px-3 py-3 align-top @3xl:px-4">
                      <p className="font-medium text-slate-800 [overflow-wrap:anywhere]">{inv.supplier_name?.trim() || "Supplier unavailable"}</p>
                      <Link to={`/invoices/${inv.id}`} className="mt-0.5 inline-block max-w-full font-medium text-brand-600 hover:underline focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus focus-visible:ring-offset-1 [overflow-wrap:anywhere]">
                        {inv.invoice_number}
                      </Link>
                      <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-text-secondary">
                        <span>Issued {shortDate(inv.issue_date)}</span>
                        <span>Invoice status: <span className={`badge ${STATUS_STYLES[inv.status] ?? ""}`}>{inv.status}</span></span>
                      </div>
                      {/* In a narrow container each invoice remains one native
                          table row with all decision facts and just one link /
                          checkbox. Wider tables expose the same facts in columns. */}
                      <dl className="mt-3 grid grid-cols-1 gap-x-3 gap-y-3 @3xs:grid-cols-2 @3xl:hidden">
                        <div className="min-w-0">
                          <dt className="text-xs text-text-secondary">Due date</dt>
                          <dd className="mt-1">{inv.due_date ? shortDate(inv.due_date) : "Not set"}</dd>
                        </div>
                        <div className="min-w-0">
                          <dt className="text-xs text-text-secondary">Workflow</dt>
                          <dd className="mt-1 [overflow-wrap:anywhere]"><WorkflowBadge state={inv.workflow_state} /></dd>
                        </div>
                        <div className="flex min-w-0 flex-col items-start gap-1 @3xs:col-span-2 @3xs:flex-row @3xs:justify-between @3xs:gap-3">
                          <dt className="text-xs text-text-secondary">Total</dt>
                          <dd className="min-w-0 w-full text-right @3xs:w-auto"><InvoiceTotal invoice={inv} /></dd>
                        </div>
                      </dl>
                    </td>
                    <td className="hidden px-4 py-3 align-top @3xl:table-cell">{inv.due_date ? shortDate(inv.due_date) : "Not set"}</td>
                    <td className="hidden px-4 py-3 align-top [overflow-wrap:anywhere] @3xl:table-cell"><WorkflowBadge state={inv.workflow_state} /></td>
                    <td className="hidden px-4 py-3 text-right align-top @3xl:table-cell"><InvoiceTotal invoice={inv} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </QueryState>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-3 text-sm text-slate-500">
        <span>{data ? `${data.total} invoices` : ""}</span>
        <div className="flex flex-wrap items-center gap-2">
          <button className="btn-ghost" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
            Prev
          </button>
          <span>
            Page {page} / {totalPages}
          </span>
          <button className="btn-ghost" disabled={page >= totalPages} onClick={() => setPage((p) => p + 1)}>
            Next
          </button>
        </div>
      </div>
    </div>
  );
}
