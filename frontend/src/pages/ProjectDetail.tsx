import { ProjectPhoto } from "../components/ProjectPhoto";
import { canShowProjectFigures } from "../lib/projectProfit";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Badge, Button, PageHeader, QueryState, Skeleton } from "../components/ui";
import { api, apiError, downloadFile } from "../lib/api";
import { formatDate, shortDate } from "../lib/format";
import type { CostEntry, PlanTracking, ProjectDocument, ProjectOffer, ProjectPnl, TemplateList } from "../lib/types";
import { useConfirm } from "../components/ui/useConfirm";
import { PROJECTS_LIST_PATH } from "../lib/nav";

/**
 * One project's profitability (docs/design/project-profitability.md, phase 1).
 *
 * The screen a project-shaped business runs on: revenue − costs for one won
 * contract/job. Industry-neutral by owner requirement — nothing here names an
 * industry, and the copy speaks of projects, work and costs.
 *
 * Two rules inherited from the rest of the product:
 *  - the P&L's BASIS comes off the wire (`basis`), never asserted locally —
 *    this screen says "live figures" because the server says so, and when
 *    phase 2 freezes closed projects the copy follows the field, not a deploy;
 *  - money renders as the wire's decimal strings. No float ever touches it.
 */
const PNL_SOURCE_LABELS: Record<string, string> = { issued: "Issued invoice", received: "Received invoice", expense_report_item: "Expense", manual: "Manual cost" };

const CATEGORIES = [
  { value: "wages", label: "Wages" },
  { value: "per_diem", label: "Per diem" },
  { value: "equipment", label: "Equipment" },
  { value: "other", label: "Other" },
];

function Money({ value, negative }: { value: string | null; negative?: boolean }) {
  if (value == null) return <span>Unavailable</span>;
  return (
    <span className={`tabular-nums ${negative ? "text-rose-600" : "text-slate-800"}`}>
      {value} €
    </span>
  );
}

export default function ProjectDetail() {
  const { confirm, dialog } = useConfirm();
  const { id } = useParams<{ id: string }>();
  const qc = useQueryClient();
  const [err, setErr] = useState<string | null>(null);
  const [label, setLabel] = useState("");
  const [category, setCategory] = useState("wages");
  const [amount, setAmount] = useState("");
  const documentInput = useRef<HTMLInputElement>(null);

  const pnl = useQuery<ProjectPnl>({
    queryKey: ["project-pnl", id],
    queryFn: async () => (await api.get(`/masters/projects/${id}/pnl`)).data,
  });
  const sourceDownload = useMutation({
    mutationFn: async (sourceId: string) => downloadFile(`/issued/${sourceId}/pdf`, `invoice-${sourceId}.pdf`),
  });
  const entries = useQuery<CostEntry[]>({
    queryKey: ["project-cost-entries", id],
    queryFn: async () => (await api.get(`/masters/projects/${id}/cost-entries`)).data,
  });
  const docs = useQuery<ProjectDocument[]>({
    queryKey: ["project-documents", id],
    queryFn: async () => (await api.get(`/masters/projects/${id}/documents`)).data,
  });

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["project-pnl", id] });
    qc.invalidateQueries({ queryKey: ["project-cost-entries", id] });
  };

  const addEntry = useMutation({
    mutationFn: async () =>
      (
        await api.post(`/masters/projects/${id}/cost-entries`, {
          label,
          category,
          amount,
        })
      ).data,
    onSuccess: () => {
      setLabel("");
      setAmount("");
      setErr(null);
      refresh();
    },
    onError: (e) => setErr(apiError(e)),
  });

  const removeEntry = useMutation({
    mutationFn: async (entryId: string) =>
      api.delete(`/masters/projects/${id}/cost-entries/${entryId}`),
    onSuccess: () => {
      setErr(null);
      refresh();
    },
    onError: (e) => setErr(apiError(e)),
  });

  const upload = useMutation({
    mutationFn: async (file: File) => {
      const form = new FormData();
      form.append("file", file);
      return (await api.post(`/masters/projects/${id}/documents`, form)).data;
    },
    onSuccess: () => {
      setErr(null);
      qc.invalidateQueries({ queryKey: ["project-documents", id] });
    },
    onError: (e) => setErr(apiError(e)),
  });

  // WO-I: toggle a document's visibility in the customer's portal.
  const shareDoc = useMutation({
    mutationFn: async (arg: { docId: string; shared: boolean }) =>
      (
        await api.put(`/masters/projects/${id}/documents/${arg.docId}/share`, {
          shared: arg.shared,
        })
      ).data,
    onSuccess: () => {
      setErr(null);
      qc.invalidateQueries({ queryKey: ["project-documents", id] });
    },
    onError: (e) => setErr(apiError(e)),
  });

  return (
    <div className="min-w-0 space-y-6 [overflow-wrap:anywhere]">
      {dialog}
      <PageHeader
        title={<span className="break-words">{pnl.data ? `${pnl.data.code} · ${pnl.data.name}` : "Project"}</span>}
        description="Everything this project earned and cost — sales invoices issued under it, supplier and subcontractor invoices allocated to it, approved expenses, and costs booked by hand."
        actions={<Link to={PROJECTS_LIST_PATH} className="btn-ghost">All projects</Link>}
      />

      {err && (
        <div role="alert" className="rounded-md border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">
          {err}
        </div>
      )}

      <QueryState
        query={pnl}
        loading={<Skeleton className="h-40 w-full" />}
        errorTitle="Couldn’t load the project"
      >
        {(data) => (
          <div className="card min-w-0 p-6">
            <div className="mb-4 flex min-w-0 flex-wrap items-center gap-3">
              <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">
                Profitability
              </h2>
              <Badge tone={data.status === "active" ? "success" : "neutral"}>{data.status}</Badge>
              {/* The basis is the server's word, not this screen's. */}
              {data.basis === "net_eur_live" && (
                <span className="text-xs text-text-secondary">
                  live figures · net amounts, EUR
                </span>
              )}
              {data.basis === "net_eur_frozen" && (
                <span className="text-xs text-text-secondary">
                  frozen at close
                  {data.pnl_frozen_at ? ` (${shortDate(data.pnl_frozen_at)})` : ""} · net
                  amounts, EUR
                </span>
              )}
            </div>
            {!canShowProjectFigures(data) && (
              <div role="alert" className="mb-4 rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
                <p className="font-medium">Profitability is unavailable until its currency basis is verified.</p>
                {(data.issues ?? []).map((issue, index) => <p key={index}>{issue.currency ? `${issue.currency}: ` : ""}{issue.detail}</p>)}
                <Link to="/fx" className="underline">Review exchange rates</Link>
                {Object.keys(data.legacy_figures ?? {}).length > 0 && <details><summary>View unchanged historical values (currency basis unverified)</summary><pre className="overflow-x-auto text-xs">{JSON.stringify(data.legacy_figures, null, 2)}</pre></details>}
              </div>
            )}
            {canShowProjectFigures(data) && (
            <div className="grid grid-cols-[repeat(auto-fit,minmax(min(100%,8rem),1fr))] gap-4">
              <div className="min-w-0">
                <p className="text-xs uppercase tracking-wide text-text-secondary">Revenue</p>
                <p className="text-xl font-semibold">
                  <Money value={data.revenue} />
                </p>
                {data.credited !== "0.00" && (
                  <p className="text-xs text-text-secondary">after {data.credited} € credited</p>
                )}
              </div>
              <div className="min-w-0">
                <p className="text-xs uppercase tracking-wide text-text-secondary">Costs</p>
                <p className="text-xl font-semibold">
                  <Money value={data.costs} />
                </p>
                <p className="text-xs text-text-secondary">
                  invoices {data.invoice_costs} € · expenses {data.expense_costs} € · manual{" "}
                  {data.manual_costs} €
                </p>
              </div>
              <div className="min-w-0">
                <p className="text-xs uppercase tracking-wide text-text-secondary">Profit</p>
                <p className="text-xl font-semibold">
                  <Money value={data.profit} negative={data.profit?.startsWith("-") ?? false} />
                </p>
              </div>
              <div className="min-w-0">
                <p className="text-xs uppercase tracking-wide text-text-secondary">Margin</p>
                <p className="text-xl font-semibold tabular-nums">
                  {data.margin_pct != null ? `${data.margin_pct}%` : "—"}
                </p>
                {data.estimated_revenue != null && (
                  <p className="text-xs text-text-secondary">
                    estimated {data.estimated_revenue} € revenue
                  </p>
                )}
              </div>
            </div>
            )}
            {data.basis.startsWith("eur_mixed_cost_") && <p className="mt-2 text-xs text-amber-800">EUR figures include gross invoice costs where a net breakdown was unavailable; review source evidence.</p>}
            {!data.adjustments_complete && data.pnl_frozen_at && <div role="status" className="mt-2 text-sm text-amber-800"><p>Current adjustments are incomplete; the stored snapshot is unchanged.</p><ul>{data.issues.map((issue, index) => <li key={index}>{issue.currency ? `${issue.currency}: ` : ""}{issue.detail}</li>)}</ul></div>}
            {(data.fx_trace ?? []).length > 0 && <details className="mt-3 text-xs"><summary>Currency conversion evidence</summary><ul className="mt-2 space-y-1">{data.fx_trace.map((row, index) => <li key={`${row.kind}-${row.source_id}-${index}`}>{PNL_SOURCE_LABELS[row.kind] ?? "Source"} · {row.currency} {row.original_amount} → {row.amount_eur == null ? "unavailable" : `EUR ${row.amount_eur}`} · rate {row.rate ?? "missing"} {row.currency} / EUR{row.rate_date ? ` at ${row.rate_date}` : ""}{row.approximate ? " · approximate" : ""}{row.allocated_eur ? ` · allocated EUR ${row.allocated_eur}` : ""}{row.document_number ? ` · ${row.document_number}` : ""}
                {row.kind === "received" && <Link className="ml-2 underline" to={`/invoices/${row.source_id}`}>View received source</Link>}
                {row.kind === "issued" && row.document_number && <button className="ml-2 underline" aria-label={`Download source invoice ${row.document_number} PDF`} disabled={sourceDownload.isPending} onClick={() => sourceDownload.mutate(row.source_id)}>{sourceDownload.isPending && sourceDownload.variables === row.source_id ? "Preparing source PDF…" : "Download source PDF"}</button>}
              </li>)}</ul></details>}
            {sourceDownload.isError && <p role="alert" className="mt-2 text-sm text-rose-700">Source PDF download failed: {apiError(sourceDownload.error)}. Retry the source download when access or storage is restored.</p>}
            {Object.keys(data.adjustments ?? {}).length > 0 && (
              <div className="mt-4 rounded-md border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
                <p className="font-medium">Changes since close</p>
                <p className="text-xs">
                  New or corrected sources and current conversion rates can differ from the frozen basis.
                  The stored figures above are unchanged. These are live differences, not posted accounting adjustments.
                </p>
                <ul className="mt-1 text-xs">
                  {Object.entries(data.adjustments).map(([k, v]) => (
                    <li key={k} className="tabular-nums">
                      {k.replace(/_/g, " ")}: {v} €
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        )}
      </QueryState>

      <div className="grid gap-6 lg:grid-cols-2">
        <div className="card min-w-0 space-y-4 p-6">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">
            Costs booked by hand
          </h2>
          <p className="text-sm text-slate-500">
            Costs that never arrive as an invoice or an expense report — wages for
            the job, per diems, equipment hire. A labelled amount, nothing more.
          </p>
          <form
            className="flex flex-wrap items-end gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              addEntry.mutate();
            }}
          >
            <div className="min-w-0 max-w-full grow">
              <label className="label" htmlFor="label">Label</label>
              <input id="label"
                className="input min-w-0 max-w-full"
                value={label}
                onChange={(e) => setLabel(e.target.value)}
                placeholder="Wages for the job"
                required
              />
            </div>
            <div className="min-w-0 max-w-full">
              <label className="label" htmlFor="category">Category</label>
              <select id="category"
                className="input min-w-0 max-w-full"
                value={category}
                onChange={(e) => setCategory(e.target.value)}
              >
                {CATEGORIES.map((c) => (
                  <option key={c.value} value={c.value}>
                    {c.label}
                  </option>
                ))}
              </select>
            </div>
            <div className="min-w-0 max-w-full">
              <label className="label" htmlFor="amount-eur">Amount (EUR)</label>
              <input id="amount-eur"
                className="input min-w-0 w-32 max-w-full"
                value={amount}
                onChange={(e) => setAmount(e.target.value)}
                placeholder="300.00"
                required
              />
            </div>
            <Button type="submit" className="max-w-full" disabled={addEntry.isPending}>
              Add cost
            </Button>
          </form>

          {(entries.data?.length ?? 0) > 0 && (
            <div role="region" aria-label="Manual cost entries" tabIndex={0} className="relative max-w-full overflow-x-auto">
            <table className="w-full text-sm [overflow-wrap:normal]">
              <caption className="sr-only">Manual cost entries</caption>
              <thead className="text-left text-xs text-text-secondary">
                <tr>
                  <th scope="col" className="py-2 font-medium">Label</th>
                  <th scope="col" className="py-2 font-medium">Category</th>
                  <th scope="col" className="py-2 text-right font-medium">Amount (EUR)</th>
                  <th scope="col" className="py-2 pl-3 text-right font-medium">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {entries.data?.map((e) => (
                  <tr key={e.id}>
                    <td className="py-2 text-slate-700">{e.label}</td>
                    <td className="py-2 text-text-secondary">{e.category}</td>
                    <td className="whitespace-nowrap py-2 text-right tabular-nums text-slate-700">{e.amount} €</td>
                    <td className="py-2 pl-3 text-right">
                      <button
                        className="btn-ghost text-xs"
                        onClick={async () => { if (await confirm({ title: "Remove this cost entry?", body: "The project P&L no longer counts it. Allocated supplier invoices and expenses are unaffected.", confirmLabel: "Remove" })) removeEntry.mutate(e.id); }}
                        disabled={removeEntry.isPending}
                      >
                        Remove
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            </div>
          )}
        </div>

        <div className="card min-w-0 space-y-4 p-6">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">
            Contract &amp; documents
          </h2>
          <p className="text-sm text-slate-500">
            The signed contract this project fulfils, kept with the numbers it
            explains.
          </p>
          <GenerateDocument
            projectId={id!}
            onDone={() => qc.invalidateQueries({ queryKey: ["project-documents", id] })}
            onError={(m) => setErr(m || null)}
          />
          <button type="button" className="btn-secondary inline-flex min-h-11 min-w-11 sm:min-h-0 sm:min-w-0 items-center justify-center max-w-full" disabled={upload.isPending} onClick={() => documentInput.current?.click()}>
            Attach document
          </button>
          <input
            ref={documentInput}
            type="file"
            aria-label="Choose project document"
            disabled={upload.isPending}
            className="hidden"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) upload.mutate(f);
              e.target.value = "";
            }}
          />
          {(docs.data?.filter((d) => d.kind !== "photo").length ?? 0) > 0 && (
            <div role="region" aria-label="Project documents" tabIndex={0} className="relative max-w-full overflow-x-auto">
            <table className="w-full text-sm [overflow-wrap:normal]">
              <caption className="sr-only">Project documents</caption>
              <thead className="text-left text-xs text-text-secondary">
                <tr>
                  <th scope="col" className="py-2 font-medium">Document</th>
                  <th scope="col" className="py-2 font-medium">Type</th>
                  <th scope="col" className="py-2 font-medium">Uploaded</th>
                  <th scope="col" className="py-2 pl-3 text-right font-medium">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {docs.data?.filter((d) => d.kind !== "photo").map((d) => (
                  <tr key={d.id}>
                    <td className="py-2 text-slate-700">{d.filename}</td>
                    <td className="py-2 text-text-secondary">{d.kind}</td>
                    <td className="whitespace-nowrap py-2 text-text-secondary">{shortDate(d.created_at)}</td>
                    <td className="py-2 pl-3 text-right">
                      <button
                        className={`btn-ghost text-xs ${d.shared_with_customer ? "text-emerald-600" : ""}`}
                        title="Show or hide in the customer's portal"
                        onClick={() =>
                          shareDoc.mutate({ docId: d.id, shared: !d.shared_with_customer })
                        }
                       disabled={shareDoc.isPending}>
                        {d.shared_with_customer ? "Shared ✓" : "Share"}
                      </button>
                      <button
                        className="btn-ghost text-xs"
                        onClick={() =>
                          downloadFile(
                            `/masters/projects/${id}/documents/${d.id}/download`,
                            d.filename,
                          ).catch((e) => setErr(apiError(e)))
                        }
                      >
                        Download
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            </div>
          )}
        </div>
      </div>

      <PhotosCard
        projectId={id!}
        photos={(docs.data ?? []).filter((d) => d.kind === "photo")}
        onChanged={() => qc.invalidateQueries({ queryKey: ["project-documents", id] })}
        onError={(m) => setErr(m || null)}
      />

      <CustomerCard projectId={id!} onError={(m) => setErr(m || null)} />

      <div className="grid gap-6 lg:grid-cols-2">
        <OffersCard projectId={id!} onChanged={refresh} onError={(m) => setErr(m || null)} />
        <PlanCard projectId={id!} onError={(m) => setErr(m || null)} />
      </div>

      {pnl.data && (
        <AcceptanceAndFinalInvoice
          projectId={id!}
          pnl={pnl.data}
          docs={docs.data ?? []}
          onChanged={() => qc.invalidateQueries({ queryKey: ["project-pnl", id] })}
          onError={(m) => setErr(m || null)}
        />
      )}
    </div>
  );
}

/** Job-photo previews are bounded derivatives. Originals remain an explicit action. */
function PhotosCard({
  projectId,
  photos,
  onChanged,
  onError,
}: {
  projectId: string;
  photos: ProjectDocument[];
  onChanged: () => void;
  onError: (m: string) => void;
}) {
  const photoInput = useRef<HTMLInputElement>(null);
  const upload = useMutation({
    mutationFn: async (file: File) => {
      const form = new FormData();
      form.append("file", file);
      return (
        await api.post(`/masters/projects/${projectId}/documents?kind=photo`, form)
      ).data;
    },
    onSuccess: () => {
      onError("");
      onChanged();
    },
    onError: (e) => onError(apiError(e)),
  });

  return (
    <div className="card min-w-0 space-y-3 p-6">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">
            Job photos
          </h2>
          <p className="text-sm text-slate-500">
            Before, during, after — pictures taken on site live with the project
            and back up the acceptance when it matters.
          </p>
        </div>
        <button type="button" className="btn-secondary inline-flex min-h-11 min-w-11 sm:min-h-0 sm:min-w-0 items-center justify-center max-w-full" disabled={upload.isPending} onClick={() => photoInput.current?.click()}>
          {upload.isPending ? "Uploading…" : "+ Add photo"}
        </button>
        <input
          ref={photoInput}
          type="file"
          aria-label="Choose project photo"
          accept="image/*"
          capture="environment"
          disabled={upload.isPending}
          className="hidden"
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) upload.mutate(f);
            e.target.value = "";
          }}
        />
      </div>
      {photos.length === 0 ? (
        <p className="text-sm text-text-secondary">
          No photos yet — on a phone, the button opens the camera.
        </p>
      ) : (
        <div className="flex flex-wrap gap-3">
          {photos.map((d) => (
            <ProjectPhoto key={d.id} projectId={projectId} doc={d} />
          ))}
        </div>
      )}
    </div>
  );
}

/** WO-E: which customer this project is FOR. The link powers the customer
 * arrival notices (and, later, the CRM timeline and client portal): the
 * notice resolves its recipient through project → customer → email at send
 * time, so linking after scheduling still works. */
function CustomerCard({
  projectId,
  onError,
}: {
  projectId: string;
  onError: (m: string) => void;
}) {
  const qc = useQueryClient();

  const projects = useQuery<{ id: string; customer_id: string | null }[]>({
    queryKey: ["projects"],
    queryFn: async () => (await api.get("/masters/projects")).data,
  });
  const customers = useQuery<{ id: string; name: string; email: string | null }[]>({
    queryKey: ["customers"],
    queryFn: async () => {
      try {
        const data = (await api.get("/customers")).data;
        return Array.isArray(data) ? data : []; // shape-guard, not just error-guard
      } catch {
        return []; // issuing module off → no customers to link, hide quietly
      }
    },
  });

  const linked =
    (Array.isArray(projects.data) ? projects.data : []).find((p) => p.id === projectId)
      ?.customer_id ?? "";

  const save = useMutation({
    mutationFn: async (customer_id: string) =>
      (
        await api.put(`/masters/projects/${projectId}/customer`, {
          customer_id: customer_id || null,
        })
      ).data,
    onSuccess: () => {
      onError("");
      qc.invalidateQueries({ queryKey: ["projects"] });
    },
    onError: (e) => onError(apiError(e)),
  });

  if (!customers.data || customers.data.length === 0) return null;
  const current = customers.data.find((c) => c.id === linked);

  return (
    <div className="card min-w-0 space-y-2 p-6">
      <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">Customer</h2>
      <p className="text-sm text-slate-500">
        Who this work is for. With a customer (and their email) linked, scheduled
        work can send them an arrival notice ahead of time — see Settings →
        Schedule notices.
      </p>
      <div className="flex flex-wrap items-center gap-2">
        <select aria-label="Linked customer"
          className="input min-w-0 max-w-full sm:w-80"
          value={linked}
          disabled={save.isPending}
          onChange={(e) => save.mutate(e.target.value)}
        >
          <option value="">No customer linked</option>
          {customers.data.map((c) => (
            <option key={c.id} value={c.id}>
              {c.name}
              {c.email ? "" : " (no email — notices can’t send)"}
            </option>
          ))}
        </select>
        {current && !current.email && (
          <span className="text-xs text-amber-600">
            This customer has no email address — arrival notices will not send.
          </span>
        )}
      </div>
    </div>
  );
}

/** Acceptance & handover + the adjustable final invoice (WO-D — the close of
 * the lifecycle). Acceptance is the customer's recorded sign-off; the final
 * invoice starts from the contracted remainder and every deviation is an
 * explicitly LABELLED adjustment line — reconciling in the open, per the
 * owner's decision. The composed lines open in the normal issuing form. */
function AcceptanceAndFinalInvoice({
  projectId,
  pnl,
  docs,
  onChanged,
  onError,
}: {
  projectId: string;
  pnl: ProjectPnl;
  docs: ProjectDocument[];
  onChanged: () => void;
  onError: (m: string) => void;
}) {
  const { confirm, dialog } = useConfirm();
  const navigate = useNavigate();
  const [note, setNote] = useState("");
  const [docId, setDocId] = useState("");
  const [adjustments, setAdjustments] = useState<{ label: string; amount: string }[]>([]);

  const record = useMutation({
    mutationFn: async () =>
      (
        await api.post(`/masters/projects/${projectId}/acceptance`, {
          note: note || null,
          document_id: docId || null,
        })
      ).data,
    onSuccess: () => {
      setNote("");
      setDocId("");
      onError("");
      onChanged();
    },
    onError: (e) => onError(apiError(e)),
  });

  const revoke = useMutation({
    mutationFn: async () => (await api.delete(`/masters/projects/${projectId}/acceptance`)).data,
    onSuccess: () => {
      onError("");
      onChanged();
    },
    onError: (e) => onError(apiError(e)),
  });

  const prepare = useMutation({
    mutationFn: async () =>
      (
        await api.post(`/masters/projects/${projectId}/final-invoice-draft`, {
          adjustments: adjustments.filter((a) => a.label.trim() || a.amount.trim()),
        })
      ).data,
    onSuccess: (draft) => {
      onError("");
      // Hand the composed lines to the NORMAL issuing form — one issuing path.
      navigate("/issue", {
        state: { finalInvoice: { project_id: projectId, lines: draft.lines } },
      });
    },
    onError: (e) => onError(apiError(e)),
  });

  const setAdj = (i: number, patch: Partial<{ label: string; amount: string }>) =>
    setAdjustments(adjustments.map((a, j) => (j === i ? { ...a, ...patch } : a)));

  return (
    <div className="card min-w-0 space-y-4 p-6">
      {dialog}
      <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">
        Acceptance &amp; final invoice
      </h2>

      {pnl.accepted_at ? (
        <div className="flex flex-wrap items-center gap-3 text-sm">
          <Badge tone="success">accepted</Badge>
          <span className="text-slate-600">
            {formatDate(pnl.accepted_at)} · {pnl.accepted_by}
            {pnl.acceptance_note ? ` — ${pnl.acceptance_note}` : ""}
          </span>
          <button
            className="btn-ghost text-xs text-rose-500"
            disabled={revoke.isPending}
            onClick={async () => { if (await confirm({ title: "Revoke the acceptance?", body: "The handover is reopened and the final invoice can be adjusted again. The revocation is audited.", confirmLabel: "Revoke" })) revoke.mutate(); }}
          >
            Revoke
          </button>
        </div>
      ) : (
        <div className="space-y-2">
          <p className="text-sm text-slate-500">
            The customer's sign-off that the work is done — what makes the final
            invoice unarguable. Generate the acceptance document above, get it
            signed, then record it here.
          </p>
          <div className="flex flex-wrap gap-2">
            <select aria-label="Acceptance document" className="input min-w-0 max-w-full" value={docId} onChange={(e) => setDocId(e.target.value)}>
              <option value="">No document linked</option>
              {docs.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.filename}
                </option>
              ))}
            </select>
            <input
              className="input min-w-0 max-w-full grow"
              placeholder="Note (optional) — e.g. signed at handover" aria-label="Note (optional) — e.g. signed at handover"
              value={note}
              onChange={(e) => setNote(e.target.value)}
            />
            <Button className="max-w-full" disabled={record.isPending} onClick={() => record.mutate()}>
              Record acceptance
            </Button>
          </div>
        </div>
      )}

      <div className="space-y-2 border-t border-slate-100 pt-3">
        <p className="text-sm text-slate-500">
          <span className="font-medium text-slate-700">Final invoice:</span> the
          contracted remainder, plus any adjustments — each one labelled, so the
          difference explains itself. A total at or below zero belongs in a
          credit note instead.
        </p>
        {adjustments.map((a, i) => (
          <div key={i} role="group" aria-label={`Final invoice adjustment ${i + 1}`} className="flex flex-wrap gap-2">
            <input
              className="input min-w-0 max-w-full grow"
              placeholder="Adjustment label — e.g. extra work agreed on site" aria-label={`Adjustment ${i + 1} description`}
              value={a.label}
              onChange={(e) => setAdj(i, { label: e.target.value })}
            />
            <input
              className="input min-w-0 w-32 max-w-full"
              placeholder="±0.00" aria-label={`Adjustment ${i + 1} amount (EUR)`}
              value={a.amount}
              onChange={(e) => setAdj(i, { amount: e.target.value })}
            />
            <button
              className="btn-ghost text-xs text-rose-500"
              aria-label={`Remove adjustment ${i + 1}`}
              onClick={() => setAdjustments(adjustments.filter((_, j) => j !== i))}
            >
              Remove
            </button>
          </div>
        ))}
        <div className="flex flex-wrap gap-2">
          <button
            className="btn-ghost text-xs"
            onClick={() => setAdjustments([...adjustments, { label: "", amount: "" }])}
          >
            + Adjustment
          </button>
          <Button
            variant="secondary"
            className="max-w-full"
            disabled={prepare.isPending}
            onClick={() => prepare.mutate()}
          >
            Prepare final invoice
          </Button>
        </div>
      </div>
    </div>
  );
}

/** Offers/estimates — the project's first artifact (lifecycle phase 4). */
function OffersCard({
  projectId,
  onChanged,
  onError,
}: {
  projectId: string;
  onChanged: () => void;
  onError: (m: string) => void;
}) {
  const qc = useQueryClient();
  const [title, setTitle] = useState("");
  const [amount, setAmount] = useState("");

  const offers = useQuery<ProjectOffer[]>({
    queryKey: ["project-offers", projectId],
    queryFn: async () => (await api.get(`/masters/projects/${projectId}/offers`)).data,
  });

  const refetch = () => {
    qc.invalidateQueries({ queryKey: ["project-offers", projectId] });
    qc.invalidateQueries({ queryKey: ["project-plan", projectId] });
    onChanged();
  };

  const create = useMutation({
    mutationFn: async () =>
      (
        await api.post(`/masters/projects/${projectId}/offers`, {
          title: title || null,
          lines: [{ description: title || "Work as offered", amount }],
        })
      ).data,
    onSuccess: () => {
      setTitle("");
      setAmount("");
      onError("");
      refetch();
    },
    onError: (e) => onError(apiError(e)),
  });

  const transition = useMutation({
    mutationFn: async ({ offerId, status }: { offerId: string; status: string }) =>
      (
        await api.post(`/masters/projects/${projectId}/offers/${offerId}/transition`, {
          status,
        })
      ).data,
    onSuccess: () => {
      onError("");
      refetch();
    },
    onError: (e) => onError(apiError(e)),
  });

  const NEXT: Record<string, string[]> = {
    draft: ["sent", "accepted", "rejected"],
    sent: ["accepted", "rejected", "draft"],
  };

  return (
    <div className="card min-w-0 space-y-4 p-6">
      <div>
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">
          Offers &amp; estimates
        </h2>
        <p className="text-sm text-slate-500">
          What was quoted for this work. Accepting an offer records the estimate
          and seeds the invoicing plan.
        </p>
      </div>
      <form
        className="flex flex-wrap items-end gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          create.mutate();
        }}
      >
        <div className="min-w-0 max-w-full grow">
          <label className="label" htmlFor="title">Title</label>
          <input id="title"
            className="input min-w-0 max-w-full"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="Offer for the work"
          />
        </div>
        <div className="min-w-0 max-w-full">
          <label className="label" htmlFor="amount-eur-2">Amount (EUR)</label>
          <input id="amount-eur-2"
            className="input min-w-0 w-32 max-w-full"
            value={amount}
            onChange={(e) => setAmount(e.target.value)}
            placeholder="10000.00"
            required
          />
        </div>
        <Button type="submit" className="max-w-full" disabled={create.isPending}>
          New offer
        </Button>
      </form>
      {(offers.data?.length ?? 0) > 0 && (
        <div role="region" aria-label="Project offers" tabIndex={0} className="relative max-w-full overflow-x-auto">
        <table className="w-full text-sm [overflow-wrap:normal]">
          <caption className="sr-only">Project offers</caption>
          <thead className="text-left text-xs text-text-secondary">
            <tr>
              <th scope="col" className="py-2 font-medium">Offer</th>
              <th scope="col" className="py-2 font-medium">Title</th>
              <th scope="col" className="py-2 text-right font-medium">Total (EUR)</th>
              <th scope="col" className="py-2 pl-2 font-medium">Status</th>
              <th scope="col" className="py-2 pl-2 text-right font-medium">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {offers.data?.map((o) => (
              <tr key={o.id}>
                <td className="py-2 font-mono text-xs text-slate-600">
                  {o.number} v{o.version}
                </td>
                <td className="py-2 text-slate-700">{o.title ?? "—"}</td>
                <td className="whitespace-nowrap py-2 text-right tabular-nums text-slate-700">{o.total} €</td>
                <td className="py-2 pl-2">
                  <Badge
                    tone={
                      o.status === "accepted"
                        ? "success"
                        : o.status === "rejected"
                          ? "danger"
                          : "neutral"
                    }
                  >
                    {o.status}
                  </Badge>
                </td>
                <td className="py-2 pl-2 text-right">
                  {(NEXT[o.status] ?? []).map((next) => (
                    <button
                      key={next}
                      className="btn-ghost text-xs"
                      disabled={transition.isPending}
                      onClick={() => transition.mutate({ offerId: o.id, status: next })}
                    >
                      {next === "draft" ? "back to draft" : next}
                    </button>
                  ))}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      )}
    </div>
  );
}

/** The invoicing plan — contracted vs. actually issued (lifecycle phase 4). */
function PlanCard({ projectId, onError }: { projectId: string; onError: (m: string) => void }) {
  const qc = useQueryClient();
  const [label, setLabel] = useState("");
  const [amount, setAmount] = useState("");

  const plan = useQuery<PlanTracking>({
    queryKey: ["project-plan", projectId],
    queryFn: async () => (await api.get(`/masters/projects/${projectId}/invoicing-plan`)).data,
  });

  const save = useMutation({
    mutationFn: async (rows: { label: string; amount: string }[]) =>
      (await api.put(`/masters/projects/${projectId}/invoicing-plan`, rows)).data,
    onSuccess: () => {
      setLabel("");
      setAmount("");
      onError("");
      qc.invalidateQueries({ queryKey: ["project-plan", projectId] });
    },
    onError: (e) => onError(apiError(e)),
  });

  const rows = plan.data?.rows ?? [];

  return (
    <div className="card min-w-0 space-y-4 p-6">
      <div>
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">
          Invoicing plan
        </h2>
        <p className="text-sm text-slate-500">
          The agreed schedule, tracked against what has actually been invoiced —
          the gap is what remains to bill.
        </p>
      </div>
      {plan.isError && <div role="alert" className="text-sm text-rose-700">{apiError(plan.error)} <button className="underline" disabled={plan.isFetching} onClick={() => { void plan.refetch(); }}>Retry invoicing-plan read</button></div>}
      {plan.data && !plan.isError && (!plan.data.complete || plan.data.basis_version !== 2) && <p role="status" className="text-sm text-amber-800">Contracted {plan.data.contracted_total} EUR. Invoiced and remaining amounts need verified currency evidence; review the project sources and exchange rates.</p>}
      {plan.data?.complete && plan.data.basis_version === 2 && !plan.isError && (
        <p className="text-sm tabular-nums text-slate-600">
          Contracted {plan.data.contracted_total} € · invoiced {plan.data.issued_total} € ·{" "}
          <span
            className={
              Number(plan.data.remaining) > 0 ? "font-medium text-amber-700" : "text-text-secondary"
            }
          >
            remaining {plan.data.remaining} €
          </span>
        </p>
      )}
      {rows.length > 0 && (
        <div role="region" aria-label="Invoicing plan entries" tabIndex={0} className="relative max-w-full overflow-x-auto">
        <table className="w-full text-sm [overflow-wrap:normal]">
          <caption className="sr-only">Invoicing plan entries</caption>
          <thead className="text-left text-xs text-text-secondary">
            <tr>
              <th scope="col" className="py-2 font-medium">Instalment</th>
              <th scope="col" className="py-2 text-right font-medium">Amount (EUR)</th>
              <th scope="col" className="py-2 pl-3 text-right font-medium">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {rows.map((r) => (
              <tr key={r.id}>
                <td className="py-2 text-slate-700">{r.label}</td>
                <td className="whitespace-nowrap py-2 text-right tabular-nums text-slate-700">{r.amount} €</td>
                <td className="py-2 pl-3 text-right">
                  <button
                    className="btn-ghost text-xs"
                    disabled={save.isPending}
                    onClick={() =>
                      save.mutate(
                        rows
                          .filter((x) => x.id !== r.id)
                          .map((x) => ({ label: x.label, amount: x.amount })),
                      )
                    }
                  >
                    Remove
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      )}
      <form
        className="flex flex-wrap items-end gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate([
            ...rows.map((x) => ({ label: x.label, amount: x.amount })),
            { label, amount },
          ]);
        }}
      >
        <div className="min-w-0 max-w-full grow">
          <label className="label" htmlFor="instalment">Instalment</label>
          <input id="instalment"
            className="input min-w-0 max-w-full"
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            placeholder="Advance on signing"
            required
          />
        </div>
        <div className="min-w-0 max-w-full">
          <label className="label" htmlFor="amount-eur-3">Amount (EUR)</label>
          <input id="amount-eur-3"
            className="input min-w-0 w-32 max-w-full"
            value={amount}
            onChange={(e) => setAmount(e.target.value)}
            placeholder="3000.00"
            required
          />
        </div>
        <Button type="submit" variant="secondary" className="max-w-full" disabled={save.isPending}>
          Add instalment
        </Button>
      </form>
    </div>
  );
}

/** Generate a document from a template — the client's saved version or a
 * platform master — rendered against THIS project and filed with its papers. */
function GenerateDocument({
  projectId,
  onDone,
  onError,
}: {
  projectId: string;
  onDone: () => void;
  onError: (m: string) => void;
}) {
  const [choice, setChoice] = useState("");

  const templates = useQuery<TemplateList>({
    queryKey: ["templates"],
    queryFn: async () => (await api.get("/templates")).data,
  });

  const generate = useMutation({
    mutationFn: async () => {
      const [scope, id] = choice.split(":");
      return (
        await api.post(`/masters/projects/${projectId}/generate-document`, {
          template_scope: scope,
          template_id: id,
        })
      ).data;
    },
    onSuccess: () => {
      setChoice("");
      onError("");
      onDone();
    },
    onError: (e) => onError(apiError(e)),
  });

  const options = [
    ...(templates.data?.own ?? [])
      .filter((t) => t.active)
      .map((t) => ({ value: `own:${t.id}`, label: `${t.name} (yours)` })),
    ...(templates.data?.platform ?? []).map((t) => ({
      value: `platform:${t.id}`,
      label: `${t.name} (standard)`,
    })),
  ];
  if (options.length === 0) return null;

  return (
    <div className="flex flex-wrap items-center gap-2">
      <select aria-label="Document template"
        className="input min-w-0 max-w-full grow"
        value={choice}
        onChange={(e) => setChoice(e.target.value)}
      >
        <option value="">Generate from template…</option>
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
      <Button
        variant="secondary"
        className="max-w-full"
        disabled={!choice || generate.isPending}
        onClick={() => generate.mutate()}
      >
        Generate
      </Button>
    </div>
  );
}
