import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useLocation, useSearchParams } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { Badge, Button, Card, ConfirmDialog, EmptyState, PageHeader, QueryState, Skeleton, type Tone } from "../components/ui";
import { api, apiError, apiErrorCode, downloadFile } from "../lib/api";
import { money, shortDate } from "../lib/format";
import type { PaymentRun, RunInvoice } from "../lib/types";

const STATUS_TONE: Record<PaymentRun["status"], Tone> = {
  open: "info",
  approved: "warning",
  paid: "success",
  cancelled: "neutral",
};

type ExportKind = "csv" | "sepa";
const HISTORY_PAGE_SIZE = 200;
interface RunPage { items: PaymentRun[]; total: number }

function historyPage(raw: string | null): number {
  const value = raw && /^[1-9]\d*$/.test(raw) ? Number(raw) : 1;
  return Number.isSafeInteger(value) && value <= Math.floor(Number.MAX_SAFE_INTEGER / HISTORY_PAGE_SIZE) ? value : 1;
}
interface PendingExport {
  run: PaymentRun;
  kind: ExportKind;
  confirmReexport: boolean;
  acknowledgeSkipped: boolean;
}

function hasExportRecord(run: PaymentRun): boolean {
  return run.export_count !== 0 || run.exported_at !== null || run.last_msg_id !== null;
}

export default function PaymentRunsPage() {
  const qc = useQueryClient();
  const { user, org } = useAuth();
  const location = useLocation();
  const [params, setParams] = useSearchParams();
  const rawPage = params.get("runs_page");
  const page = historyPage(rawPage);
  const [err, setErr] = useState<string | null>(null);
  const [picked, setPicked] = useState<Record<string, boolean>>({});
  const [refs, setRefs] = useState<Record<string, string>>({});
  // WO-9 export flows: a re-export needs explicit confirmation (the bank sees a
  // NEW message id), and skipped payees must be acknowledged BY NAME.
  const [reexportAsk, setReexportAsk] = useState<PendingExport | null>(null);
  const [skippedAsk, setSkippedAsk] = useState<{ pending: PendingExport; detail: string } | null>(
    null,
  );
  // R17: cancelling a run discards its invoice selection (and, for an approved
  // run, a second person's sign-off) — an explicit confirm step, not a bare click.
  const [cancelAsk, setCancelAsk] = useState<PaymentRun | null>(null);

  const payable = useQuery<RunInvoice[]>({
    queryKey: ["payment-runs", "payable"],
    queryFn: async () => (await api.get("/payment-runs/payable")).data,
  });
  const runs = useQuery<RunPage>({
    queryKey: ["payment-runs", "history", user?.id, org?.id, page, HISTORY_PAGE_SIZE],
    enabled: !!user?.id && !!org?.id,
    queryFn: async ({ signal }) => {
      const result = (await api.get("/payment-runs/page", {
        params: { limit: HISTORY_PAGE_SIZE, offset: (page - 1) * HISTORY_PAGE_SIZE },
        signal, timeout: 15_000,
      })).data as RunPage;
      if (!result || !Array.isArray(result.items) || result.items.length > HISTORY_PAGE_SIZE ||
        !Number.isSafeInteger(result.total) || result.total < 0) throw new Error("Invalid payment run page");
      return result;
    },
    retry: false,
    meta: { errorHandled: true },
  });
  const lastPage = runs.data ? Math.max(1, Math.ceil(runs.data.total / HISTORY_PAGE_SIZE)) : null;
  const clamping = runs.isSuccess && !runs.isFetching && lastPage !== null && page > lastPage;
  const changePage = (next: number, replace = false) => setParams((current) => {
    const updated = new URLSearchParams(current); updated.set("runs_page", String(next)); return updated;
  }, { replace, state: location.state });
  useEffect(() => {
    const next = clamping ? lastPage : page;
    if (next === null || (!clamping && (rawPage === null || rawPage === String(page)))) return;
    setParams((current) => {
      if (historyPage(current.get("runs_page")) !== page) return current;
      const updated = new URLSearchParams(current); updated.set("runs_page", String(next)); return updated;
    }, { replace: true, state: location.state });
  }, [clamping, lastPage, page, rawPage, setParams, location.state]);

  const invalidate = () => {
    setErr(null);
    qc.invalidateQueries({ queryKey: ["payment-runs"] });
  };
  const onErr = (e: unknown) => setErr(apiError(e));

  const create = useMutation({
    mutationFn: async () =>
      (
        await api.post("/payment-runs", {
          invoice_ids: Object.keys(picked).filter((k) => picked[k]),
          method: "bank_transfer",
        })
      ).data,
    onSuccess: () => {
      setPicked({});
      invalidate();
    },
    onError: onErr,
  });
  const approve = useMutation({
    mutationFn: async (r: PaymentRun) =>
      (await api.post(`/payment-runs/${r.id}/approve`, { version: r.version })).data,
    onSuccess: invalidate,
    onError: onErr,
  });
  const pay = useMutation({
    mutationFn: async (r: PaymentRun) =>
      (await api.post(`/payment-runs/${r.id}/pay`, { version: r.version, reference: refs[r.id] || null }))
        .data,
    onSuccess: invalidate,
    onError: onErr,
  });
  const cancel = useMutation({
    mutationFn: async (id: string) => (await api.delete(`/payment-runs/${id}`)).data,
    onSuccess: invalidate,
    onError: (e: unknown) => {
      onErr(e);
      if (apiErrorCode(e) === "run_exported_cannot_cancel") {
        void qc.invalidateQueries({ queryKey: ["payment-runs"] });
      }
    },
  });

  const doExport = async (p: PendingExport) => {
    const params = new URLSearchParams();
    if (p.confirmReexport) params.set("confirm_reexport", "true");
    if (p.acknowledgeSkipped) params.set("acknowledge_skipped", "true");
    const q = params.toString() ? `?${params.toString()}` : "";
    const path = p.kind === "csv" ? "export" : "sepa";
    const ext = p.kind === "csv" ? "csv" : "xml";
    try {
      setErr(null);
      await downloadFile(
        `/payment-runs/${p.run.id}/${path}${q}`,
        `payment-run-${p.run.reference || p.run.id}.${ext}`,
        { method: "post" },
      );
      invalidate();
    } catch (e) {
      const code = apiErrorCode(e);
      if (code === "already_exported") {
        setReexportAsk({ ...p, confirmReexport: true });
      } else if (code === "skipped_payees") {
        setSkippedAsk({ pending: { ...p, acknowledgeSkipped: true }, detail: apiError(e) });
      } else {
        setErr(apiError(e));
      }
    }
  };

  const items = payable.data ?? [];
  const selectedTotal = items
    .filter((i) => picked[i.id])
    .reduce((s, i) => s + Number(i.total_eur ?? i.total), 0);

  return (
    <div className="space-y-6">
      <PageHeader
        title="Payment runs"
        description="Pay approved, scheduled supplier invoices in a batch. A second user must approve a run before it can be paid or exported (maker–checker)."
        actions={
          <Link to="/invoices" className="text-sm text-brand-600 hover:underline">
            ← Invoices
          </Link>
        }
      />

      {err && (
        <div role="alert" className="rounded-lg border border-rose-200 bg-rose-50 px-4 py-2 text-sm text-rose-700">
          {err}
        </div>
      )}

      {/* Scheduled invoices → new run */}
      <Card>
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-sm font-semibold text-slate-700">Scheduled — awaiting payment</h2>
          <Button
            size="sm"
            disabled={selectedTotal === 0}
            loading={create.isPending}
            onClick={() => create.mutate()}
          >
            Create run ({money(selectedTotal, "EUR")})
          </Button>
        </div>
        <QueryState
          query={payable}
          loading={<Skeleton className="h-16 w-full" />}
          isEmpty={(d) => d.length === 0}
          empty={<EmptyState title="No scheduled invoices awaiting payment" />}
          errorTitle="Couldn’t load scheduled invoices"
        >
          {(d) => (
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-text-secondary">
                  <th className="py-1"></th>
                  <th className="py-1">Invoice</th>
                  <th className="py-1">Supplier</th>
                  <th className="py-1 text-right">Amount</th>
                </tr>
              </thead>
              <tbody>
                {d.map((i) => (
                  <tr key={i.id} className="border-t border-slate-100">
                    <td className="py-1">
                      <input
                        type="checkbox"
                        checked={!!picked[i.id]}
                        onChange={(e) => setPicked({ ...picked, [i.id]: e.target.checked })}
                        aria-label={`Select ${i.invoice_number}`}
                      />
                    </td>
                    <td className="py-1">{i.invoice_number}</td>
                    <td className="py-1 text-slate-500">{i.vendor_name ?? "—"}</td>
                    <td className="py-1 text-right tabular-nums">{money(i.total, i.currency)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </QueryState>
      </Card>

      {/* Existing runs */}
      <Card>
        <h2 className="mb-3 text-sm font-semibold text-slate-700">Runs</h2>
        <nav aria-label="Payment run history pages" className="mb-4 flex flex-wrap items-center gap-2 text-sm text-slate-600">
          {runs.isSuccess && !runs.isFetching && !clamping && <span aria-live="polite" aria-atomic="true">
            Showing {runs.data.items.length ? `${(page - 1) * HISTORY_PAGE_SIZE + 1}–${(page - 1) * HISTORY_PAGE_SIZE + runs.data.items.length}` : "0"} of {runs.data.total} runs
          </span>}
          <Button size="sm" variant="secondary" disabled={page <= 1 || runs.isFetching || clamping} onClick={() => changePage(page - 1)}>Previous runs</Button>
          <span>Page {page}{lastPage !== null && !runs.isError ? ` of ${lastPage}` : ""}</span>
          <Button size="sm" variant="secondary" disabled={runs.isError || runs.isFetching || clamping || lastPage === null || page >= lastPage} onClick={() => changePage(page + 1)}>Next runs</Button>
        </nav>
        <QueryState
          query={{ ...runs, isLoading: runs.isLoading || runs.isFetching || clamping }}
          loading={<Skeleton className="h-24 w-full" />}
          isEmpty={(d) => d.total === 0}
          empty={<EmptyState title="No payment runs yet" />}
          errorTitle="Couldn’t load payment runs"
        >
          {(runData) => (
        <div className="space-y-3">
          {runData.items.length === 0 && <p className="text-sm text-slate-600">This page has no runs. <button type="button" className="underline" disabled={runs.isFetching} onClick={() => runs.refetch()}>Refresh this page</button> to check the current list.</p>}
          {runData.items.map((r) => (
            <div key={r.id} className="rounded-lg border border-slate-200 p-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge tone={STATUS_TONE[r.status]}>{r.status}</Badge>
                  <span className="font-medium">{money(r.total_eur, "EUR")}</span>
                  <span className="text-xs text-text-secondary">
                    {r.invoice_count} invoice{r.invoice_count === 1 ? "" : "s"} · {r.method}
                    {r.reference ? ` · ${r.reference}` : ""}
                  </span>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  {r.status === "open" && (
                    <>
                      <Button
                        size="sm"
                        loading={approve.isPending}
                        title="A different user than the creator must approve (maker–checker)"
                        onClick={() => approve.mutate(r)}
                      >
                        Approve
                      </Button>
                      <Button size="sm" variant="ghost" disabled={hasExportRecord(r)} aria-describedby={hasExportRecord(r) ? `cancel-blocked-${r.id}` : undefined} onClick={() => setCancelAsk(r)}>
                        Cancel
                      </Button>
                    </>
                  )}
                  {r.status === "approved" && (
                    <>
                      <input
                        className="w-40 rounded-lg border border-slate-300 px-2 py-1 text-sm"
                        placeholder="Payment reference…" aria-label="Payment reference"
                        value={refs[r.id] ?? ""}
                        onChange={(e) => setRefs({ ...refs, [r.id]: e.target.value })}
                      />
                      <Button
                        size="sm"
                        loading={pay.isPending}
                        title="Neither the run's creator nor its approver can pay it"
                        onClick={() => pay.mutate(r)}
                      >
                        Mark paid
                      </Button>
                      <Button size="sm" variant="ghost" disabled={hasExportRecord(r)} aria-describedby={hasExportRecord(r) ? `cancel-blocked-${r.id}` : undefined} onClick={() => setCancelAsk(r)}>
                        Cancel
                      </Button>
                    </>
                  )}
                  {(r.status === "approved" || r.status === "paid") && (
                    <>
                      <Button
                        size="sm"
                        variant="secondary"
                        onClick={() =>
                          doExport({
                            run: r,
                            kind: "csv",
                            confirmReexport: false,
                            acknowledgeSkipped: false,
                          })
                        }
                      >
                        Export CSV
                      </Button>
                      <Button
                        size="sm"
                        variant="secondary"
                        title="ISO 20022 pain.001 credit-transfer file (suppliers with an IBAN on file)"
                        onClick={() =>
                          doExport({
                            run: r,
                            kind: "sepa",
                            confirmReexport: false,
                            acknowledgeSkipped: false,
                          })
                        }
                      >
                        SEPA XML
                      </Button>
                    </>
                  )}
                </div>
              </div>
              <div className="mt-1 text-xs text-text-secondary">
                Created {shortDate(r.created_at)}
                {r.created_by ? ` by ${r.created_by}` : ""}
                {r.approved_at
                  ? ` · approved ${shortDate(r.approved_at)}${r.approved_by ? ` by ${r.approved_by}` : ""}`
                  : ""}
                {r.paid_at ? ` · paid ${shortDate(r.paid_at)}` : ""}
                {r.export_count > 0
                  ? ` · exported ${r.export_count}× (first ${shortDate(r.exported_at!)}${
                      r.last_msg_id ? `, MsgId ${r.last_msg_id}` : ""
                    })`
                  : ""}
              </div>
              {(r.status === "open" || r.status === "approved") && hasExportRecord(r) && (
                <p id={`cancel-blocked-${r.id}`} className="mt-2 text-sm text-amber-800">
                  Export records prevent cancellation. Check the bank file’s status with your bank
                  or accountant before arranging another payment. An export does not confirm payment.
                </p>
              )}
            </div>
          ))}
        </div>
          )}
        </QueryState>
      </Card>

      <ConfirmDialog
        open={reexportAsk !== null}
        onClose={() => setReexportAsk(null)}
        onConfirm={() => {
          const p = reexportAsk!;
          setReexportAsk(null);
          void doExport(p);
        }}
        title="Re-export bank file?"
        confirmLabel="Re-export with a new message id"
        tone="danger"
      >
        {reexportAsk && (
          <p className="text-sm text-slate-600">
            This run was already exported{" "}
            {reexportAsk.run.export_count > 0
              ? `${reexportAsk.run.export_count}× (first ${shortDate(reexportAsk.run.exported_at!)})`
              : ""}
            . Re-exporting produces a <strong>new file with a new message id</strong> — the bank
            will treat it as a separate payment instruction. Only continue if the previous file
            was never submitted.
          </p>
        )}
      </ConfirmDialog>

      <ConfirmDialog
        open={skippedAsk !== null}
        onClose={() => setSkippedAsk(null)}
        onConfirm={() => {
          const p = skippedAsk!.pending;
          setSkippedAsk(null);
          void doExport(p);
        }}
        title="Some suppliers will not be paid"
        confirmLabel="I understand these will not be paid"
        tone="danger"
      >
        {skippedAsk && <p className="text-sm text-slate-600">{skippedAsk.detail}</p>}
      </ConfirmDialog>

      <ConfirmDialog
        open={cancelAsk !== null}
        onClose={() => setCancelAsk(null)}
        onConfirm={() => {
          const r = cancelAsk!;
          setCancelAsk(null);
          cancel.mutate(r.id);
        }}
        title="Cancel this payment run?"
        confirmLabel="Cancel run"
        tone="danger"
        loading={cancel.isPending}
      >
        {cancelAsk && (
          <p className="text-sm text-slate-600">
            Its {cancelAsk.invoice_count} invoice{cancelAsk.invoice_count === 1 ? "" : "s"} will
            return to the scheduled pool
            {cancelAsk.status === "approved"
              ? " and the existing approval will be discarded — a cancelled run cannot be un-cancelled; the invoices would need to go through create and approve again."
              : ". This cannot be undone."}
          </p>
        )}
      </ConfirmDialog>
    </div>
  );
}
