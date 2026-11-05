import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useId, useRef, useState } from "react";
import { Link, useLocation, useSearchParams } from "react-router-dom";
import { api, apiError, downloadFile, openFile, tokenStore } from "../lib/api";
import { useAuth } from "../auth/AuthContext";
import { checkSavedInvoiceCommand, deliveryOutcome, hasSavedInvoiceCommand, invoiceCommand } from "../lib/invoiceCommands";
import { ConfirmDialog, ErrorState } from "../components/ui";
import { ISSUED_STATUS_LABELS, ISSUED_STATUS_STYLES, formatNumber, money, shortDate } from "../lib/format";
import { useModules } from "../lib/useModules";
import type {
  BulkReminderResult, CostMaster, GenerateResult, IssuedAttachment, IssuedInvoice, IssuedLineInput,
  IssuerProfile, Paginated, Partner, RecurringFrequency, RecurringSchedule, SendResult, VatScheme,
} from "../lib/types";
import { CashPaymentAction } from "../components/CashPaymentAction";
import { useConfirm } from "../components/ui/useConfirm";

const SCHEMES: { value: VatScheme; label: string }[] = [
  { value: "standard", label: "Standard VAT" },
  { value: "reverse_charge", label: "Reverse charge" },
  { value: "intra_eu", label: "Intra-EU supply (exempt)" },
  { value: "exempt", label: "Exempt" },
];

const emptyLine = (): IssuedLineInput => ({ description: "", quantity: "1", unit_price: "0", vat_rate: "21" });
const ISSUED_PAGE_SIZE = 100;
const ISSUED_READ_TIMEOUT_MS = 15_000;
interface DraftCopyResult { id: string; buyer_name: string; lifecycle: "draft"; number: null; doc_type: "invoice" }
interface DraftCopyOutcome {
  userId: string; orgId: string; sourceId: string; draftId: string; buyerName: string; sourceNumber: string | null;
}
interface DuplicateAttempt { userId: string; orgId: string; sourceId: string; generation: number; token: string | null }


function issuedPage(raw: string | null): number {
  if (!raw || !/^[1-9]\d*$/.test(raw)) return 1;
  const page = Number(raw);
  return Number.isSafeInteger(page) ? page : 1;
}

export default function Issue() {
  const qc = useQueryClient();
  const { user, org } = useAuth();
  const [copyOutcome, setCopyOutcome] = useState<DraftCopyOutcome | null>(null);
  const duplicateAuthority = useRef({ userId: user?.id, orgId: org?.id });
  duplicateAuthority.current = { userId: user?.id, orgId: org?.id };
  const duplicateOwnerActive = useRef(true);
  useEffect(() => { duplicateOwnerActive.current = true; return () => { duplicateOwnerActive.current = false; }; }, []);
  const ownsDuplicateAttempt = (attempt: DuplicateAttempt) => duplicateOwnerActive.current &&
    duplicateAuthority.current.userId === attempt.userId && duplicateAuthority.current.orgId === attempt.orgId &&
    tokenStore.generation() === attempt.generation && tokenStore.get() === attempt.token;
  const visibleCopyOutcome = copyOutcome?.userId === user?.id && copyOutcome?.orgId === org?.id ? copyOutcome : null;
  const location = useLocation();
  const [searchParams, setSearchParams] = useSearchParams();
  const rawPage = searchParams.get("page");
  const page = issuedPage(rawPage);
  const modules = useModules();
  const issuer = useQuery<IssuerProfile>({ queryKey: ["issuer"], queryFn: async () => (await api.get("/issuer")).data });
  const list = useQuery<Paginated<IssuedInvoice>>({
    queryKey: ["issued", user?.id, org?.id, page, ISSUED_PAGE_SIZE],
    enabled: !!user?.id && !!org?.id,
    queryFn: async ({ signal }) => (await api.get("/issued", {
      params: { page, page_size: ISSUED_PAGE_SIZE }, signal, timeout: ISSUED_READ_TIMEOUT_MS,
    })).data,
    retry: false,
    meta: { errorHandled: true },
  });
  const totalPages = list.data ? Math.max(1, Math.ceil(list.data.total / ISSUED_PAGE_SIZE)) : null;
  const clamping = list.isSuccess && !list.isFetching && totalPages !== null && page > totalPages;
  const loadingPage = list.isPending || clamping;
  const visibleRows = !list.isError && !clamping ? list.data?.items ?? [] : [];
  const changePage = (next: number) => setSearchParams((current) => {
    const updated = new URLSearchParams(current);
    updated.set("page", String(next));
    return updated;
  }, { state: location.state });

  useEffect(() => {
    const next = clamping ? totalPages : page;
    if (next === null || (!clamping && (rawPage === null || rawPage === String(page)))) return;
    setSearchParams((current) => {
      if (issuedPage(current.get("page")) !== page) return current;
      const updated = new URLSearchParams(current);
      updated.set("page", String(next));
      return updated;
    }, { replace: true, state: location.state });
  }, [clamping, totalPages, page, rawPage, setSearchParams, location.state]);

  const enabled = modules.isEnabled("issuing");
  const ready = !!issuer.data?.is_complete;

  if (modules.data && !enabled) {
    return (
      <Gate>
        The invoice issuing module isn't active. Activate it in{" "}
        <Link to="/settings" className="font-medium underline">Settings → Modules</Link>.
      </Gate>
    );
  }
  if (issuer.data && !ready) {
    return (
      <Gate>
        Before issuing,{" "}
        <Link to="/issuer" className="font-medium underline">complete your company registration details</Link>.
      </Gate>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Issue invoices</h1>
          <p className="text-sm text-slate-500">EN 16931-compliant PDF with embedded Factur-X XML.</p>
        </div>
        <div className="flex gap-2">
          <Link to="/issue/reports" className="btn-ghost">Reports</Link>
          <Link to="/issuer" className="btn-ghost">Company details</Link>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <BulkActions />
      </div>

      <NewInvoice
        onCreated={() => qc.invalidateQueries({ queryKey: ["issued"] })}
        defaultPenalty={issuer.data?.default_penalty_rate ?? null}
      />

      <RecurringSchedules onGenerated={() => qc.invalidateQueries({ queryKey: ["issued"] })} />

      {visibleCopyOutcome && <div className="min-w-0 space-y-2 rounded-lg border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-800 [overflow-wrap:anywhere]">
        <p role="status" aria-live="polite" aria-atomic="true" aria-label="Draft copy outcome">
          Draft copy created for {visibleCopyOutcome.buyerName} from {visibleCopyOutcome.sourceNumber ?? "the selected invoice"}.
        </p>
        {list.isFetching && <p>Refreshing the invoice list…</p>}
        <button type="button" className="btn-ghost" disabled={list.isFetching} onClick={() => { if (page === 1) void list.refetch(); else changePage(1); }}>Show latest invoices</button>
      </div>}

      <div>
        <div className="mb-2 flex flex-wrap items-center justify-between gap-3">
          <h2 className="text-sm font-semibold text-slate-600">
            Issued{list.data && !list.isError ? ` (${list.data.total})` : ""}
          </h2>
          <nav aria-label="Issued invoice pages" className="flex flex-wrap items-center gap-2 text-sm text-slate-500">
            <span aria-live="polite" aria-atomic="true">
              {list.isError ? `Page ${page} could not be loaded.` : loadingPage ? "Loading issued invoices…" : list.data ? (
                <>Showing {list.data.items.length === 0 ? "0" : `${(page - 1) * ISSUED_PAGE_SIZE + 1}–${(page - 1) * ISSUED_PAGE_SIZE + list.data.items.length}`} of {list.data.total}</>
              ) : ""}
            </span>
            <button type="button" className="btn-ghost" disabled={page <= 1 || list.isFetching || clamping}
              onClick={() => changePage(page - 1)}>Previous page</button>
            <span>Page {page}{totalPages !== null && !list.isError && !clamping ? ` of ${totalPages}` : ""}</span>
            <button type="button" className="btn-ghost" disabled={list.isError || list.isFetching || clamping || totalPages === null || page >= totalPages}
              onClick={() => changePage(page + 1)}>Next page</button>
          </nav>
        </div>
        <div className="card overflow-x-auto p-0">
          <table className="w-full text-sm">
            <thead className="border-b border-slate-200 bg-slate-50 text-left text-xs uppercase tracking-wide text-slate-500">
              <tr>
                <th className="px-4 py-3">Number</th>
                <th className="px-4 py-3">Buyer</th>
                <th className="px-4 py-3">Issued</th>
                <th className="px-4 py-3">Due</th>
                <th className="px-4 py-3">Status</th>
                <th className="px-4 py-3 text-right">Total</th>
                <th className="px-4 py-3 text-right">Payment</th>
                <th className="px-4 py-3 text-right">Send</th>
                <th className="px-4 py-3 text-right">PDF / XML</th>
              </tr>
            </thead>
            <tbody>
              {visibleRows.map((inv) => (
                <tr key={inv.id} className="border-b border-slate-100 last:border-0 hover:bg-slate-50">
                  <td className="px-4 py-3 font-medium">
                    {inv.number ?? <span className="italic text-text-secondary">Draft — unnumbered</span>}
                    {inv.kind === "penalty" && <span className="badge ml-2 bg-rose-100 text-rose-700">Penalty</span>}
                    {inv.doc_type === "credit_note" && <span className="badge ml-2 bg-violet-100 text-violet-700">Credit note</span>}
                    {inv.doc_type === "credit_note" && inv.corrected_invoice_number && (
                      <div className="text-xs text-text-secondary">corrects {inv.corrected_invoice_number}</div>
                    )}
                    {inv.doc_type !== "credit_note" && Number(inv.credited_total) > 0 && (
                      <div className="text-xs text-violet-500">−{money(inv.credited_total, inv.currency)} credited</div>
                    )}
                  </td>
                  <td className="px-4 py-3">
                    {inv.buyer_name}
                    {inv.buyer_email && <div className="text-xs text-text-secondary">{inv.buyer_email}</div>}
                  </td>
                  <td className="px-4 py-3 text-slate-500">{shortDate(inv.issue_date)}</td>
                  <td className="px-4 py-3 text-slate-500">{shortDate(inv.due_date)}</td>
                  <td className="px-4 py-3">
                    <span className={`badge ${ISSUED_STATUS_STYLES[inv.status] ?? "bg-slate-100 text-slate-600"}`}>
                      {ISSUED_STATUS_LABELS[inv.status] ?? inv.status}
                    </span>
                    {inv.sent_at && <span className="badge ml-1 bg-sky-50 text-sky-600" title={`Sent ${shortDate(inv.sent_at)}`}>Sent</span>}
                    {inv.status === "overdue" && (
                      <div className="mt-0.5 text-xs text-rose-500">
                        {inv.days_overdue}d overdue
                        {Number(inv.penalty_accrued) > 0 && <> · +{money(inv.penalty_accrued, inv.currency)} interest</>}
                        {Number(inv.penalty_accrued) === 0 && <LateInterestPeek id={inv.id} />}
                      </div>
                    )}
                    {inv.status !== "overdue" && Number(inv.outstanding) > 0 && Number(inv.amount_paid) > 0 && (
                      <div className="mt-0.5 text-xs text-text-secondary">{money(inv.outstanding, inv.currency)} left</div>
                    )}
                  </td>
                  <td className="px-4 py-3 text-right font-medium">{money(inv.total, inv.currency)}</td>
                  <td className="px-4 py-3 text-right">
                    {inv.doc_type === "credit_note" ? (
                      <span className="text-xs text-text-secondary">—</span>
                    ) : inv.lifecycle === "draft" || inv.lifecycle === "approved" ? (
                      <DraftActions inv={inv} onDone={() => qc.invalidateQueries({ queryKey: ["issued"] })} />
                    ) : (
                      <div className="flex flex-col items-end gap-1">
                        <CashPaymentAction inv={inv} onDone={() => qc.invalidateQueries({ queryKey: ["issued"] })} />
                        <CreditAction inv={inv} onDone={() => qc.invalidateQueries({ queryKey: ["issued"] })} />
                        <VoidAction inv={inv} onDone={() => qc.invalidateQueries({ queryKey: ["issued"] })} />
                        {inv.lifecycle !== "written_off" && (
                          <DisputeActions inv={inv} onDone={() => qc.invalidateQueries({ queryKey: ["issued"] })} />
                        )}
                        <DuplicateAction inv={inv} onDone={() => qc.invalidateQueries({ queryKey: ["issued"] })} onCreated={setCopyOutcome} ownsAttempt={ownsDuplicateAttempt} />
                      </div>
                    )}
                  </td>
                  <td className="px-4 py-3 text-right">
                    {inv.doc_type === "credit_note" || !inv.number ? (
                      <span className="text-xs text-text-secondary">—</span>
                    ) : (
                      <SendActions inv={inv} onDone={() => qc.invalidateQueries({ queryKey: ["issued"] })} />
                    )}
                  </td>
                  <td className="px-4 py-3 text-right whitespace-nowrap">
                    {inv.number ? (
                      <>
                        <button className="text-brand-600 hover:underline" onClick={() => openFile(`/issued/${inv.id}/pdf?inline=1`)}>
                          View
                        </button>
                        <span className="mx-1.5 text-slate-300">·</span>
                        <button className="text-brand-600 hover:underline" onClick={() => downloadFile(`/issued/${inv.id}/pdf`, `${inv.number}.pdf`)}>
                          PDF
                        </button>
                        <span className="mx-1.5 text-slate-300">·</span>
                        <button className="text-brand-600 hover:underline" onClick={() => downloadFile(`/issued/${inv.id}/xml`, `${inv.number}.xml`)}>
                          XML
                        </button>
                      </>
                    ) : (
                      <span className="text-xs text-text-secondary">Issue first</span>
                    )}
                    <IssuedAttachments inv={inv} />
                  </td>
                </tr>
              ))}
              {list.isError && (
                <tr>
                  <td colSpan={9}>
                    <ErrorState title="Couldn’t load the issued invoices" onRetry={() => list.refetch()} />
                  </td>
                </tr>
              )}
              {loadingPage && (
                <tr><td colSpan={9} className="px-4 py-8 text-center text-text-secondary">Loading issued invoices…</td></tr>
              )}
              {!loadingPage && !list.isError && list.data?.total === 0 && (
                <tr>
                  <td colSpan={9} className="px-4 py-8 text-center text-text-secondary">No invoices issued yet.</td>
                </tr>
              )}
              {!loadingPage && !list.isError && list.data && list.data.total > 0 && list.data.items.length === 0 && (
                <tr><td colSpan={9} className="px-4 py-8 text-center text-text-secondary">
                  This page has no invoices. <button type="button" className="underline" disabled={list.isFetching} onClick={() => list.refetch()}>Refresh this page</button> to check the current list.
                </td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

// Bulk collections: download every invoice PDF in a period, and send reminders
// to all overdue invoices that have a customer email.
function BulkActions() {
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [zipStatus, setZipStatus] = useState("");
  const invalidZipPeriod = !!from && !!to && from > to;
  const [msg, setMsg] = useState<string | null>(null);

  const zip = useMutation({
    mutationFn: async () => {
      const q = new URLSearchParams();
      if (from) q.set("from", from);
      if (to) q.set("to", to);
      if (zipStatus) q.set("status", zipStatus);
      await downloadFile(`/issued/export.zip?${q.toString()}`, `invoices_${from || "all"}_${to || "all"}.zip`);
    },
  });

  const remind = useMutation({
    mutationFn: async () => (await api.post("/issued/reminders/run")).data as BulkReminderResult,
    onSuccess: (r) => setMsg(
      `Sent ${r.sent} reminder${r.sent === 1 ? "" : "s"}` +
      (r.skipped_no_email ? ` · ${r.skipped_no_email} skipped (no email)` : "")
    ),
    onError: (e) => setMsg(apiError(e)),
  });

  return (
    <div className="card flex w-full flex-wrap items-end gap-3">
      <div>
        <label className="label" htmlFor="period-from">ZIP period from</label>
        <input id="period-from" type="date" className="input" value={from} disabled={zip.isPending} onChange={(e) => { zip.reset(); setFrom(e.target.value); }} />
      </div>
      <div>
        <label className="label" htmlFor="to">ZIP period to</label>
        <input id="to" type="date" className="input" value={to} disabled={zip.isPending} onChange={(e) => { zip.reset(); setTo(e.target.value); }} />
      </div>
      <div>
        <label className="label" htmlFor="zip-status">ZIP status</label>
        <select id="zip-status" className="input" disabled={zip.isPending} value={zipStatus} onChange={(e) => { zip.reset(); setZipStatus(e.target.value); }}>
          <option value="">All numbered statuses</option>
          {Object.entries(ISSUED_STATUS_LABELS).filter(([key]) => !["draft", "approved"].includes(key)).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
        </select>
      </div>
      <button className="btn-ghost" aria-describedby="zip-scope" disabled={zip.isPending || invalidZipPeriod} onClick={() => { setMsg(null); zip.mutate(); }}>
        {zip.isPending ? "Preparing…" : "Download PDFs (ZIP)"}
      </button>
      <button className="btn-ghost" disabled={remind.isPending} onClick={() => { setMsg(null); remind.mutate(); }}>
        {remind.isPending ? "Sending…" : "Remind all overdue"}
      </button>
      <p id="zip-scope" className="w-full text-xs text-slate-500">ZIP covers numbered documents across the current workspace’s issuers in the selected period/status, up to 500. It includes a document list and PDF verification hashes. Reminders cover all overdue invoices independently of ZIP filters.</p>
      {invalidZipPeriod && <p role="alert" className="w-full text-sm text-rose-700">ZIP period start must not follow its end.</p>}
      {zip.isError && <p role="alert" className="w-full text-sm text-rose-700">{apiError(zip.error)} Check access or narrow the selection, then retry the ZIP download.</p>}
      {zip.isSuccess && <p role="status" className="w-full text-xs text-slate-500">Archive prepared; check your browser downloads.</p>}
      {msg && <span className="text-sm text-slate-500">{msg}</span>}
    </div>
  );
}

// Per-invoice delivery: email the invoice PDF, or send a payment reminder.
function SendActions({ inv, onDone }: { inv: IssuedInvoice; onDone: () => void }) {
  const { user, org } = useAuth();
  const { confirm, dialog } = useConfirm();
  const [note, setNote] = useState<string | null>(null);
  const [deliveryKey, setDeliveryKey] = useState<string | null>(null);
  const delivery = useQuery<SendResult>({
    queryKey: ["invoice-delivery", user?.id, org?.id, inv.id, deliveryKey],
    queryFn: async () => (await api.get(`/issued/${inv.id}/delivery-commands/${deliveryKey}`)).data,
    enabled: deliveryKey !== null && !!user && !!org,
    retry: false,
    refetchInterval: (query) => ["queued", "sending"].includes(query.state.data?.message.status ?? "") ? 1000 : false,
  });
  useEffect(() => {
    if (delivery.isError) setNote("Could not check delivery. Refresh the mail log before retrying.");
    else if (delivery.data) {
      setNote(deliveryOutcome(delivery.data.message.status, delivery.data.message.error));
      if (delivery.data.message.status === "sent") void onDone();
    }
  }, [delivery.data, delivery.isError]);
  useEffect(() => { setDeliveryKey(null); setNote(null); }, [user?.id, org?.id, inv.id]);


  const email = useMutation({
    mutationFn: async (resend: boolean) => {
      if (!user || !org) throw new Error("Wait for your account and company to load.");
      return invoiceCommand<SendResult>(`/issued/${inv.id}/send`, { resend },
        { userId: user.id, orgId: org.id },
        (key) => `/issued/${inv.id}/delivery-commands/${key}`, resend ? undefined : `invoice:${inv.id}`);
    },
    onSuccess: async (r) => { setDeliveryKey(r.message.command_key ?? null); setNote(deliveryOutcome(r.message.status, r.message.error)); await onDone(); },
    onError: (e) => setNote(apiError(e)),
  });
  const remind = useMutation({
    mutationFn: async () => (await api.post(`/issued/${inv.id}/reminder`, {})).data as SendResult,
    onSuccess: async (r) => { setNote(deliveryOutcome(r.message.status, r.message.error)); await onDone(); },
    onError: (e) => setNote(apiError(e)),
  });

  const noEmail = !inv.buyer_email;
  const paid = inv.status === "paid";

  return (
    <div className="flex flex-col items-end gap-0.5">
      <div className="flex items-center gap-2">
        <button
          className="text-brand-600 hover:underline disabled:text-slate-300 disabled:no-underline"
          disabled={email.isPending || noEmail || ["queued", "sending"].includes(delivery.data?.message.status ?? "")}
          title={noEmail ? "Add a customer email first" : "Email the invoice PDF"}
          onClick={() => email.mutate(false)}
        >
          Email
        </button>
        <span className="text-slate-300">·</span>
        <button
          className="text-amber-600 hover:underline disabled:text-slate-300 disabled:no-underline"
          disabled={remind.isPending || noEmail || paid}
          title={paid ? "Already paid" : noEmail ? "Add a customer email first" : "Send a payment reminder"}
          onClick={() => remind.mutate()}
        >
          Remind
        </button>
      </div>
      {["sent", "unknown", "failed", "blocked"].includes(delivery.data?.message.status ?? "") && (
        <button
          className="text-xs text-brand-600 hover:underline disabled:text-slate-300"
          disabled={email.isPending || noEmail}
          title="Request a new delivery after checking the previous outcome"
          onClick={async () => {
            if (await confirm({ title: "Request another delivery?", body: "Check the mail relay first if the previous outcome is unknown. Another delivery can duplicate a message already accepted by the relay.", confirmLabel: "Request delivery" })) email.mutate(true);
          }}
        >Request new delivery</button>
      )}
      {dialog}
      {inv.reminder_count > 0 && (
        <span className="text-xs text-text-secondary">{inv.reminder_count} reminder{inv.reminder_count === 1 ? "" : "s"} sent</span>
      )}
      {note && <span className="text-xs text-emerald-600">{note}</span>}
    </div>
  );
}

// Issue a FULL credit note against an invoice (one click + confirm). Partial
// credits are available via the API; the common case here is a full cancellation.
function CreditAction({ inv, onDone }: { inv: IssuedInvoice; onDone: () => void }) {
  const { user, org } = useAuth();
  return <CreditControls key={`${user?.id}:${org?.id}:${inv.id}`} inv={inv} onDone={onDone} />;
}

function CreditControls({ inv, onDone }: { inv: IssuedInvoice; onDone: () => void }) {
  const { user, org, hasPerm } = useAuth();
  const [note, setNote] = useState<string | null>(null);
  const path = `/issued/${inv.id}/credit-note`;
  const scope = user && org ? { userId: user.id, orgId: org.id } : null;
  const lookup = (key: string) => `/issued/${inv.id}/credit-commands/${key}`;
  const unresolved = scope && hasSavedInvoiceCommand(path, scope);
  const fullyCredited = Number(inv.credited_total) >= Number(inv.total) && Number(inv.total) > 0;
  const finish = async (cn: IssuedInvoice | null) => {
    setNote(cn ? `Credited (${cn.number})` : "No committed credit found yet. Retry the original command after reviewing the invoice.");
    if (cn) await onDone();
  };
  const credit = useMutation({
    mutationFn: async () => {
      if (!scope) throw new Error("Sign in again before issuing a credit note.");
      return invoiceCommand<IssuedInvoice>(path, { expected_credited_total: inv.credited_total }, scope, lookup);
    },
    onSuccess: finish,
    onError: e => setNote(apiError(e)),
  });
  const check = useMutation({
    mutationFn: async () => { if (!scope) throw new Error("Sign in again before checking this credit."); return checkSavedInvoiceCommand<IssuedInvoice>(path, scope, lookup); },
    onSuccess: finish,
    onError: e => setNote(apiError(e)),
  });
  if (fullyCredited && !unresolved) return <span className="text-xs text-violet-500">Fully credited</span>;
  const pending = credit.isPending || check.isPending;
  return <div className="flex items-center gap-1">
    {!fullyCredited && <button className="text-xs text-violet-600 hover:underline disabled:text-slate-300" disabled={pending || !hasPerm("issued.write")}
      title="Issue one credit command for the reviewed remaining amount; retries check its saved result"
      onClick={() => { if (window.confirm(`Issue a credit note for the remaining balance of ${inv.number}?`)) credit.mutate(); }}>Credit</button>}
    {unresolved && <button className="text-xs text-violet-600" disabled={pending || !hasPerm("issued.write")} onClick={() => check.mutate()}>Check previous credit</button>}
    {note && <span role={credit.isError || check.isError ? "alert" : "status"} className="text-xs text-slate-500">{note}</span>}
  </div>;
}

function VoidAction({ inv, onDone }: { inv: IssuedInvoice; onDone: () => void }) {
  const [err, setErr] = useState<string | null>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [reason, setReason] = useState("");
  const doVoid = useMutation({
    mutationFn: async (r: string) => (await api.post(`/issued/${inv.id}/void`, { reason: r })).data,
    onSuccess: () => {
      setConfirmOpen(false);
      setReason("");
      onDone();
    },
    onError: (e) => setErr(apiError(e)),
  });
  // Only an unpaid, un-credited standard invoice can be voided.
  if (
    inv.doc_type === "credit_note" ||
    inv.voided_at !== null ||
    Number(inv.amount_paid) > 0 ||
    Number(inv.credited_total) > 0
  )
    return null;
  return (
    <div className="flex items-center gap-1">
      <button
        className="text-xs text-rose-600 hover:underline disabled:text-slate-300"
        disabled={doVoid.isPending}
        title="Cancel (void) this unpaid invoice"
        onClick={() => {
          setErr(null);
          setReason("");
          setConfirmOpen(true);
        }}
      >
        Void
      </button>
      {err && <span className="text-xs text-rose-500">{err}</span>}
      <ConfirmDialog
        open={confirmOpen}
        onClose={() => setConfirmOpen(false)}
        onConfirm={() => doVoid.mutate(reason)}
        title={`Void invoice ${inv.number}?`}
        confirmLabel="Void invoice"
        tone="danger"
        loading={doVoid.isPending}
      >
        <p>
          This permanently cancels the invoice — it stops being a live receivable and this
          cannot be undone.
        </p>
        <label className="label mt-3 block" htmlFor="void-reason">Reason (optional)</label>
        <input
          id="void-reason"
          className="input"
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          placeholder="Why is this being voided?"
        />
      </ConfirmDialog>
    </div>
  );
}

// Draft/approved workflow: approve, finalize (issue = allocate the number), or
// cancel. A never-issued draft has no number and is not a receivable.
function DraftActions({ inv, onDone }: { inv: IssuedInvoice; onDone: () => void }) {
  const { user, org } = useAuth();
  const [err, setErr] = useState<string | null>(null);
  const post = (path: string) => async () => (await api.post(`/issued/${inv.id}/${path}`, {})).data;
  const approve = useMutation({ mutationFn: post("approve"), onSuccess: onDone, onError: (e) => setErr(apiError(e)) });
  const issue = useMutation({ mutationFn: async () => {
    if (!user || !org) throw new Error("Wait for your account and company to load.");
    return invoiceCommand(`/issued/${inv.id}/issue`, {}, { userId: user.id, orgId: org.id },
      (key) => `/issued/commands/${key}?action=issue`, `issue:${inv.id}`);
  }, onSuccess: onDone, onError: (e) => setErr(apiError(e)) });
  const cancel = useMutation({ mutationFn: post("cancel"), onSuccess: onDone, onError: (e) => setErr(apiError(e)) });
  return (
    <div className="flex flex-col items-end gap-1">
      <div className="flex items-center gap-2">
        {inv.lifecycle === "draft" && (
          <button
            className="text-xs text-indigo-600 hover:underline disabled:text-slate-300"
            disabled={approve.isPending}
            title="Mark this draft as approved (a review gate before issuing)"
            onClick={() => approve.mutate()}
          >
            Approve
          </button>
        )}
        <button
          className="text-xs font-medium text-emerald-600 hover:underline disabled:text-slate-300"
          disabled={issue.isPending}
          title="Finalize: allocate the invoice number and make it a live receivable"
          onClick={() => {
            if (window.confirm("Issue this invoice? A number will be allocated and it becomes final.")) issue.mutate();
          }}
        >
          Issue
        </button>
        <button
          className="text-xs text-rose-600 hover:underline disabled:text-slate-300"
          disabled={cancel.isPending}
          title="Discard this draft (kept for the audit trail as cancelled)"
          onClick={() => {
            if (window.confirm("Cancel this draft?")) cancel.mutate();
          }}
        >
          Cancel
        </button>
      </div>
      {err && <span className="text-xs text-rose-500">{err}</span>}
    </div>
  );
}

// Post-issue receivable workflow: flag/resolve a dispute, or write off bad debt.
function DisputeActions({ inv, onDone }: { inv: IssuedInvoice; onDone: () => void }) {
  const [err, setErr] = useState<string | null>(null);
  const [writeOffOpen, setWriteOffOpen] = useState(false);
  const [writeOffReason, setWriteOffReason] = useState("");
  const dispute = useMutation({
    mutationFn: async (reason: string) => (await api.post(`/issued/${inv.id}/dispute`, { reason })).data,
    onSuccess: onDone,
    onError: (e) => setErr(apiError(e)),
  });
  const undispute = useMutation({
    mutationFn: async () => (await api.post(`/issued/${inv.id}/undispute`, {})).data,
    onSuccess: onDone,
    onError: (e) => setErr(apiError(e)),
  });
  const writeOff = useMutation({
    mutationFn: async (reason: string) => (await api.post(`/issued/${inv.id}/write-off`, { reason })).data,
    onSuccess: () => {
      setWriteOffOpen(false);
      setWriteOffReason("");
      onDone();
    },
    onError: (e) => setErr(apiError(e)),
  });
  return (
    <div className="flex items-center gap-2">
      {inv.lifecycle === "disputed" ? (
        <button
          className="text-xs text-emerald-600 hover:underline disabled:text-slate-300"
          disabled={undispute.isPending}
          title="Resolve the dispute — return to the normal receivable lifecycle"
          onClick={() => undispute.mutate()}
        >
          Resolve
        </button>
      ) : (
        <button
          className="text-xs text-amber-600 hover:underline disabled:text-slate-300"
          disabled={dispute.isPending}
          title="Flag as disputed (the buyer contests it)"
          onClick={() => {
            const r = window.prompt("Reason for the dispute (optional):");
            if (r !== null) dispute.mutate(r);
          }}
        >
          Dispute
        </button>
      )}
      <button
        className="text-xs text-slate-500 hover:underline disabled:text-slate-300"
        disabled={writeOff.isPending}
        title="Write off as bad debt (no longer collectible)"
        onClick={() => {
          setErr(null);
          setWriteOffReason("");
          setWriteOffOpen(true);
        }}
      >
        Write off
      </button>
      {err && <span className="text-xs text-rose-500">{err}</span>}
      <ConfirmDialog
        open={writeOffOpen}
        onClose={() => setWriteOffOpen(false)}
        onConfirm={() => writeOff.mutate(writeOffReason)}
        title={`Write off invoice ${inv.number}?`}
        confirmLabel="Write off as bad debt"
        tone="danger"
        loading={writeOff.isPending}
      >
        <p>
          This marks the invoice as uncollectible bad debt — it stays in the audit trail but is
          removed from active receivables tracking.
        </p>
        <label className="label mt-3 block" htmlFor="writeoff-reason">Reason (optional)</label>
        <input
          id="writeoff-reason"
          className="input"
          value={writeOffReason}
          onChange={(e) => setWriteOffReason(e.target.value)}
          placeholder="Why is this being written off?"
        />
      </ConfirmDialog>
    </div>
  );
}

// Copy any invoice into a fresh editable draft.
function DuplicateAction({ inv, onDone, onCreated, ownsAttempt }: { inv: IssuedInvoice; onDone: () => Promise<unknown>; onCreated: (outcome: DraftCopyOutcome) => void; ownsAttempt: (attempt: DuplicateAttempt) => boolean }) {
  const { user, org, hasPerm } = useAuth();
  const [err, setErr] = useState<string | null>(null);
  const active = useRef(true);
  useEffect(() => { active.current = true; return () => { active.current = false; }; }, []);
  const canCopy = !!user?.id && !!org?.id && hasPerm("issued.write");
  const dup = useMutation<DraftCopyResult, unknown, DuplicateAttempt>({
    mutationFn: async (attempt) => {
      const response = await api.post(`/issued/${attempt.sourceId}/duplicate`, {});
      const result = response.data as Partial<DraftCopyResult> | null;
      if (response.status !== 201 || !result || typeof result.id !== "string" || !result.id || result.id === attempt.sourceId ||
          typeof result.buyer_name !== "string" || result.lifecycle !== "draft" || result.number !== null || result.doc_type !== "invoice") {
        throw new Error("The copy outcome could not be verified. Check the latest invoices before copying again.");
      }
      return result as DraftCopyResult;
    },
    onSuccess: async (result, attempt) => {
      if (!ownsAttempt(attempt)) return;
      if (active.current) setErr(null);
      onCreated({ userId: attempt.userId, orgId: attempt.orgId, sourceId: attempt.sourceId, draftId: result.id, buyerName: result.buyer_name, sourceNumber: inv.number });
      await onDone();
    },
    onError: (error, attempt) => { if (active.current && ownsAttempt(attempt)) setErr(apiError(error)); },
    retry: false,
  });
  return (
    <div className="flex items-center gap-1">
      <button
        className="text-xs text-slate-500 hover:underline disabled:text-slate-300"
        disabled={dup.isPending || !canCopy}
        title={canCopy ? "Copy into a new draft" : "Invoice write permission is required to copy"}
        onClick={() => {
          if (!canCopy || dup.isPending || !user || !org) return;
          dup.mutate({ userId: user.id, orgId: org.id, sourceId: inv.id, generation: tokenStore.generation(), token: tokenStore.get() });
        }}
      >
        Duplicate
      </button>
      {err && <span role="alert" className="text-xs text-rose-500">{err}</span>}
    </div>
  );
}

// Supporting attachments (signed PO, delivery note, contract). Expandable per row.
function IssuedAttachments({ inv }: { inv: IssuedInvoice }) {
  const { user, org } = useAuth();
  return <IssuedAttachmentControls key={`${user?.id}:${org?.id}:${inv.id}`} inv={inv} />;
}

function IssuedAttachmentDownload({ invoiceId, attachment }: { invoiceId: string; attachment: IssuedAttachment }) {
  const download = useMutation({ mutationFn: async () => downloadFile(`/issued/${invoiceId}/attachments/${attachment.id}/download`, attachment.filename) });
  return <div className="min-w-0">
    <button className="truncate text-brand-600 hover:underline" disabled={download.isPending} onClick={() => download.mutate()}>
      {download.isPending ? `Downloading ${attachment.filename}…` : attachment.filename}
    </button>
    {download.isError && <p role="alert" className="text-rose-600">{apiError(download.error)} Retry this file’s download after checking access or storage.</p>}
  </div>;
}

function IssuedAttachmentControls({ inv }: { inv: IssuedInvoice }) {
  const { user, org, hasPerm } = useAuth();
  const { confirm, dialog } = useConfirm();
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [failedFile, setFailedFile] = useState<File | null>(null);
  const key = ["issued-attachments", inv.id];
  const path = `/issued/${inv.id}/attachments`;
  const lookup = (command: string) => `/issued/${inv.id}/attachment-commands/${command}`;
  const scope = user && org ? { userId: user.id, orgId: org.id } : null;
  const canWrite = hasPerm("issued.write");
  const list = useQuery<IssuedAttachment[]>({ queryKey: key, queryFn: async () => (await api.get(path)).data, enabled: open });
  const policy = useQuery<{ max_bytes: number }>({ queryKey: ["issued-attachment-policy", inv.id], queryFn: async () => { const data = (await api.get(`/issued/${inv.id}/attachment-policy`)).data; if (!Number.isSafeInteger(data?.max_bytes) || data.max_bytes <= 0) throw new Error("Upload limit is unavailable. Reload it before choosing a file."); return data; }, enabled: open });
  const finish = async (result: IssuedAttachment | null) => {
    if (result) setFailedFile(null);
    setErr(result?.deleted_at ? "This upload committed, but the file is now in Trash. Restore it there or intentionally attach it again." : result ? null : "No committed upload was found yet. Retry or reselect the original file to use the same command.");
    await qc.invalidateQueries({ queryKey: key });
  };
  const upload = useMutation({
    mutationFn: async (file: File) => {
      if (!scope || !policy.data || !Number.isSafeInteger(policy.data.max_bytes) || policy.data.max_bytes <= 0) throw new Error("Reload the upload limit before choosing a file.");
      if (file.size > policy.data.max_bytes) throw new Error(`File too large (maximum ${formatNumber(policy.data.max_bytes)} bytes).`);
      const digest = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
      const sha256 = Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2, "0")).join("");
      const fd = new FormData(); fd.append("file", file);
      return invoiceCommand<IssuedAttachment>(path, fd, scope, lookup, undefined, path, false, { filename: file.name, size: file.size, mime: file.type, sha256 });
    },
    onSuccess: finish,
    onError: e => setErr(apiError(e)),
  });
  const check = useMutation({
    mutationFn: async () => { if (!scope) throw new Error("Sign in again before checking this upload."); return checkSavedInvoiceCommand<IssuedAttachment>(path, scope, lookup); },
    onSuccess: finish,
    onError: e => setErr(apiError(e)),
  });
  const remove = useMutation({
    mutationFn: async (aid: string) => (await api.delete(`${path}/${aid}`)).data,
    onSuccess: () => qc.invalidateQueries({ queryKey: key }),
    onError: e => setErr(apiError(e)),
  });
  const pending = upload.isPending || check.isPending;
  const unresolved = scope && hasSavedInvoiceCommand(path, scope);
  const count = list.data?.length ?? 0;
  return <div className="mt-1 text-right">
    {dialog}
    <button className="text-xs text-slate-500 hover:underline" onClick={() => setOpen(o => !o)}>📎 Files{open && count ? ` (${count})` : ""}</button>
    {open && <div className="mt-1 space-y-1 rounded-lg border border-slate-200 p-2 text-left">
      {list.data?.map(a => <div key={a.id} className="flex items-start justify-between gap-2 text-xs">
        <IssuedAttachmentDownload invoiceId={inv.id} attachment={a} />
        <button className="text-rose-500 hover:underline" disabled={remove.isPending || !canWrite} onClick={async () => { if (await confirm({ title: "Remove this attachment?", body: "It goes to the recycle bin and can be restored for 30 days.", confirmLabel: "Remove" })) remove.mutate(a.id); }}>remove</button>
      </div>)}
      {list.isError && <div role="alert" className="text-xs text-rose-600">Couldn’t load the files. <button onClick={() => list.refetch()}>Retry file list</button></div>}
      {policy.isError && <div role="alert" className="text-xs text-rose-600">Couldn’t load the upload limit. <button onClick={() => policy.refetch()}>Retry upload limit</button></div>}
      {list.data && list.data.length === 0 && <div className="text-xs text-text-secondary">No files yet.</div>}
      <label className="block text-xs text-brand-600">
        {upload.isPending ? "Uploading…" : "+ Add file"}
        <input type="file" aria-label="Add supporting file" className="hidden" disabled={pending || !canWrite || !policy.data || policy.isError}
          onChange={e => { const file = e.target.files?.[0]; if (file) { setErr(null); setFailedFile(file); upload.mutate(file); } e.target.value = ""; }} />
      </label>
      {policy.data && <p className="text-xs text-slate-500">Up to {formatNumber(policy.data.max_bytes)} bytes. One upload command is retained until its result is known.</p>}
      {failedFile && upload.isError && <button className="text-xs text-brand-600" disabled={pending} onClick={() => upload.mutate(failedFile)}>Retry original upload</button>}
      {unresolved && <button className="text-xs text-brand-600" disabled={pending} onClick={() => check.mutate()}>Check previous upload</button>}
      {err && <div role="alert" className="text-xs text-rose-600">{err}</div>}
    </div>}
  </div>;
}

function Gate({ children }: { children: React.ReactNode }) {
  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-semibold tracking-tight">Issue invoices</h1>
      <div className="card text-sm text-slate-600">{children}</div>
    </div>
  );
}

const emptyBuyer = { buyer_name: "", buyer_email: "", buyer_vat_number: "", buyer_address_line1: "", buyer_postal_code: "", buyer_city: "", buyer_country: "" };

function NewInvoice({ onCreated, defaultPenalty }: { onCreated: () => void; defaultPenalty: string | null }) {
  const { user, org } = useAuth();
  // WO-D: the project page's "Prepare final invoice" hands its composed lines
  // (contracted remainder + labelled adjustments) here via router state — ONE
  // issuing path for every invoice, final ones included. The human still sees,
  // edits and issues; the composer only prefilled.
  const prefill = (useLocation().state as
    | { finalInvoice?: { project_id: string; lines: { description: string; quantity: string; unit_price: string }[] } }
    | null)?.finalInvoice;
  const [buyer, setBuyer] = useState({ ...emptyBuyer });
  const [scheme, setScheme] = useState<VatScheme>("standard");
  const [penalty, setPenalty] = useState("");
  const [poRef, setPoRef] = useState("");
  const [exemption, setExemption] = useState("");
  const [partnerId, setPartnerId] = useState("");
  const [issuerId, setIssuerId] = useState("");
  const [projectId, setProjectId] = useState(prefill?.project_id ?? "");
  const [lines, setLines] = useState<IssuedLineInput[]>(
    prefill ? prefill.lines.map((l) => ({ ...l, vat_rate: "21" })) : [emptyLine()],
  );
  const [error, setError] = useState<string | null>(null);

  const partners = useQuery<Partner[]>({
    queryKey: ["partners"],
    queryFn: async () => (await api.get("/partners")).data,
  });
  // Issuer companies: when more than one exists, the user picks which entity
  // issues (and numbers) this invoice; otherwise the default entity is used.
  const issuers = useQuery<IssuerProfile[]>({
    queryKey: ["issuer-registry"],
    queryFn: async () => (await api.get("/issuer/registry")).data,
  });
  // The revenue side of the project P&L: an invoice issued under a project is
  // what makes its profitability computable at all.
  const projects = useQuery<CostMaster[]>({
    queryKey: ["masters", "projects"],
    queryFn: async () => (await api.get("/masters/projects")).data,
  });
  const selectedPartner = partners.data?.find((p) => p.id === partnerId);

  const pickPartner = (id: string) => {
    setPartnerId(id);
    const p = partners.data?.find((x) => x.id === id);
    if (p) {
      setBuyer({
        ...emptyBuyer,
        buyer_name: p.name, buyer_email: p.email ?? "", buyer_vat_number: p.vat_number ?? "",
        buyer_address_line1: p.address_line1 ?? "", buyer_city: p.city ?? "",
        buyer_postal_code: p.postal_code ?? "", buyer_country: p.country ?? "",
      });
      if (p.penalty_rate) setPenalty(p.penalty_rate);
    }
  };

  const create = useMutation({
    mutationFn: async (asDraft: boolean) => {
      const payload: Record<string, unknown> = {
        ...buyer,
        buyer_email: buyer.buyer_email || null,
        vat_scheme: scheme,
        lines,
        draft: asDraft,
      };
      if (partnerId) payload.partner_id = partnerId;
      if (issuerId) payload.issuer_id = issuerId;
      if (projectId) payload.project_id = projectId;
      if (penalty.trim() !== "") payload.penalty_rate = penalty;
      if (poRef.trim() !== "") payload.po_reference = poRef.trim();
      if (exemption.trim() !== "") payload.tax_exemption_reason = exemption.trim();
      if (!user || !org) throw new Error("Wait for your account and company to load.");
      return invoiceCommand("/issued", payload, { userId: user.id, orgId: org.id },
        (key) => `/issued/commands/${key}`);
    },
    onSuccess: async () => {
      setBuyer({ ...emptyBuyer });
      setLines([emptyLine()]);
      setPenalty("");
      setPoRef("");
      setExemption("");
      setPartnerId("");
      setIssuerId("");
      setProjectId("");
      setError(null);
      await onCreated();
    },
    onError: (e) => setError(apiError(e)),
  });

  const savedScope = user && org ? { userId: user.id, orgId: org.id } : null;
  const hasSaved = savedScope !== null && hasSavedInvoiceCommand("/issued", savedScope);
  const checkSaved = useMutation({
    mutationFn: async () => {
      if (!savedScope) throw new Error("Wait for your account and company to load.");
      return checkSavedInvoiceCommand<IssuedInvoice>("/issued", savedScope, (key) => `/issued/commands/${key}`);
    },
    onSuccess: async (result) => {
      setError(result ? `Saved request found: ${result.number ?? "draft invoice"}. Review it in the list before issuing another.` : "No committed invoice is visible yet. Keep the same request identity and inputs; it may still be in flight.");
      if (result) await onCreated();
    },
    onError: (e) => setError(apiError(e)),
  });

  const zero = scheme !== "standard";
  const total = lines.reduce((sum, l) => {
    const gross = Number(l.quantity || 0) * Number(l.unit_price || 0);
    const net = gross * (1 - Number(l.discount_percent || 0) / 100);
    const vat = zero ? 0 : (net * Number(l.vat_rate || 0)) / 100;
    return sum + net + vat;
  }, 0);

  const setLine = (i: number, patch: Partial<IssuedLineInput>) =>
    setLines(lines.map((l, idx) => (idx === i ? { ...l, ...patch } : l)));

  return (
    <div className="card space-y-4">
      <h2 className="text-sm font-semibold text-slate-600">New invoice</h2>
      {hasSaved && <button className="btn-ghost" title="Look up the committed result of the previous request without issuing another invoice" disabled={checkSaved.isPending || create.isPending} onClick={() => checkSaved.mutate()}>Check previous request</button>}


      {(issuers.data?.length ?? 0) > 1 && (
        <div>
          <label className="label" htmlFor="issue-from-company">Issue from (company)</label>
          <select id="issue-from-company" className="input sm:w-1/2" value={issuerId} onChange={(e) => setIssuerId(e.target.value)}>
            <option value="">
              — Default{" "}
              {issuers.data?.find((i) => i.is_default)
                ? `(${issuers.data.find((i) => i.is_default)!.name || issuers.data.find((i) => i.is_default)!.legal_name})`
                : ""}{" "}
              —
            </option>
            {issuers.data?.map((i) => (
              <option key={i.id} value={i.id}>
                {i.name || i.legal_name} · {i.invoice_prefix}
              </option>
            ))}
          </select>
          <p className="mt-1 text-xs text-text-secondary">
            Each company keeps its own gap-free numbering series — manage them under{" "}
            <Link to="/issuer" className="underline">Company details</Link>.
          </p>
        </div>
      )}

      {(projects.data?.length ?? 0) > 0 && (
        <div>
          <label className="label" htmlFor="project">Project</label>
          <select id="project"
            className="input sm:w-1/2"
            value={projectId}
            onChange={(e) => setProjectId(e.target.value)}
          >
            <option value="">— No project —</option>
            {projects.data
              ?.filter((m) => m.status !== "archived")
              .map((m) => (
                <option key={m.id} value={m.id}>
                  {m.code} · {m.name}
                </option>
              ))}
          </select>
          <p className="mt-1 text-xs text-text-secondary">
            Links this revenue to a project so its profitability can be calculated.
          </p>
        </div>
      )}

      {(partners.data?.length ?? 0) > 0 && (
        <div>
          <label className="label" htmlFor="partner-optional">Partner (optional)</label>
          <select id="partner-optional" className="input sm:w-1/2" value={partnerId} onChange={(e) => pickPartner(e.target.value)}>
            <option value="">— One-off customer —</option>
            {partners.data?.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
          {selectedPartner && (selectedPartner.requires_contract || selectedPartner.requires_acceptance) && (
            <p className="mt-1 text-xs text-amber-600">
              This partner has a pre-invoicing workflow. Issuing is blocked until its documents are signed —{" "}
              <Link to="/partners" className="underline">manage on Partners</Link>.
            </p>
          )}
        </div>
      )}

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        <Field label="Customer name" v={buyer.buyer_name} on={(v) => setBuyer({ ...buyer, buyer_name: v })} span2 />
        <Field label="Customer VAT no." v={buyer.buyer_vat_number} on={(v) => setBuyer({ ...buyer, buyer_vat_number: v })} />
        <Field label="Customer email" v={buyer.buyer_email} on={(v) => setBuyer({ ...buyer, buyer_email: v })} span2 />
        <Field label="Address" v={buyer.buyer_address_line1} on={(v) => setBuyer({ ...buyer, buyer_address_line1: v })} span2 />
        <Field label="Postcode" v={buyer.buyer_postal_code} on={(v) => setBuyer({ ...buyer, buyer_postal_code: v })} />
        <Field label="City" v={buyer.buyer_city} on={(v) => setBuyer({ ...buyer, buyer_city: v })} />
        <Field label="Country (ISO)" v={buyer.buyer_country} on={(v) => setBuyer({ ...buyer, buyer_country: v })} />
        <div>
          <label className="label" htmlFor="vat-scheme">VAT scheme</label>
          <select id="vat-scheme" className="input" value={scheme} onChange={(e) => setScheme(e.target.value as VatScheme)}>
            {SCHEMES.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
          </select>
        </div>
        <div>
          <label className="label" htmlFor="late-payment-interest-p-a">Late-payment interest (% p.a.)</label>
          <input id="late-payment-interest-p-a"
            className="input" inputMode="decimal" value={penalty}
            placeholder={defaultPenalty ? `default ${Number(defaultPenalty)}%` : "none"}
            onChange={(e) => setPenalty(e.target.value)}
          />
        </div>
        <Field label="PO reference" v={poRef} on={setPoRef} />
        {zero && (
          <Field
            label="VAT-exemption reason"
            v={exemption}
            on={setExemption}
            span2
          />
        )}
      </div>

      <div className="overflow-x-auto rounded-lg border border-slate-200">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-left text-xs uppercase text-slate-500">
            <tr>
              <th className="px-3 py-2">Description</th>
              <th className="px-3 py-2 w-20">Qty</th>
              <th className="px-3 py-2 w-28">Unit price</th>
              <th className="px-3 py-2 w-20">Disc %</th>
              <th className="px-3 py-2 w-20">VAT %</th>
              <th className="px-3 py-2"></th>
            </tr>
          </thead>
          <tbody>
            {lines.map((l, i) => (
              <tr key={i} className="border-t border-slate-100">
                <td className="px-3 py-2"><input aria-label={`Line ${i + 1} description`} className="input" value={l.description} onChange={(e) => setLine(i, { description: e.target.value })} /></td>
                <td className="px-3 py-2"><input aria-label={`Line ${i + 1} quantity`} className="input" value={l.quantity} onChange={(e) => setLine(i, { quantity: e.target.value })} /></td>
                <td className="px-3 py-2"><input aria-label={`Line ${i + 1} unit price`} className="input" value={l.unit_price} onChange={(e) => setLine(i, { unit_price: e.target.value })} /></td>
                <td className="px-3 py-2"><input className="input" value={l.discount_percent ?? ""} placeholder="0" aria-label="0" onChange={(e) => setLine(i, { discount_percent: e.target.value })} /></td>
                <td className="px-3 py-2"><input aria-label={`Line ${i + 1} VAT rate`} className="input" value={l.vat_rate} disabled={zero} onChange={(e) => setLine(i, { vat_rate: e.target.value })} /></td>
                <td className="px-3 py-2 text-right">
                  {lines.length > 1 && (
                    <button className="text-rose-500 hover:underline" onClick={() => setLines(lines.filter((_, idx) => idx !== i))}>remove</button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <button className="btn-ghost" onClick={() => setLines([...lines, emptyLine()])}>+ Add line</button>

      {error && <div role="alert" className="rounded-lg bg-rose-50 px-3 py-2 text-sm text-rose-600">{error}</div>}

      <div className="flex items-center justify-between">
        <span className="text-sm text-slate-500">Total incl. VAT: <span className="font-semibold text-slate-700">{money(total)}</span></span>
        <div className="flex items-center gap-2">
          <button
            className="btn-ghost"
            disabled={create.isPending || checkSaved.isPending || !buyer.buyer_name || lines.some((l) => !l.description)}
            title="Save an editable draft without allocating a legal invoice number"
            onClick={() => create.mutate(true)}
          >
            Save draft
          </button>
          <button
            className="btn-primary"
            disabled={create.isPending || checkSaved.isPending || !buyer.buyer_name || lines.some((l) => !l.description)}
            title="Create a final numbered invoice; retries check the saved request before another write"
            onClick={() => create.mutate(false)}
          >
            {create.isPending ? "Issuing…" : "Issue invoice"}
          </button>
        </div>
      </div>
    </div>
  );
}

const FREQUENCIES: { value: RecurringFrequency; label: string }[] = [
  { value: "weekly", label: "Weekly" },
  { value: "monthly", label: "Monthly" },
  { value: "quarterly", label: "Quarterly" },
  { value: "yearly", label: "Yearly" },
];

// Recurring-invoice schedules: a template + cadence that generates real invoices.
// "Run due now" materialises every invoice that has reached its date.
interface RecurringCreatePayload {
  template: { buyer_name: string; buyer_email: string | null; vat_scheme: VatScheme; lines: IssuedLineInput[] };
  frequency: RecurringFrequency; interval: number; start_date: string; end_date: string | null; title: string | null;
}
interface RecurringCreateAttempt {
  userId: string; orgId: string; generation: number; token: string | null; editor: number; payload: RecurringCreatePayload;
}

function recurringDate(value: unknown): value is string {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const date = new Date(`${value}T00:00:00Z`);
  return Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === value;
}

function verifiedRecurringSchedule(value: unknown): value is RecurringSchedule {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const result = value as Record<string, unknown>;
  const nullableString = (field: unknown) => field === null || typeof field === "string";
  const nullableDate = (field: unknown) => field === null || recurringDate(field);
  return typeof result.id === "string" && result.id.trim().length > 0 &&
    nullableString(result.title) && nullableString(result.partner_id) &&
    FREQUENCIES.some((frequency) => frequency.value === result.frequency) &&
    typeof result.interval === "number" && Number.isInteger(result.interval) && result.interval > 0 &&
    recurringDate(result.start_date) && recurringDate(result.next_run_date) && nullableDate(result.end_date) &&
    typeof result.active === "boolean" &&
    (result.payment_terms_days === null || (typeof result.payment_terms_days === "number" && Number.isInteger(result.payment_terms_days) && result.payment_terms_days >= 0)) &&
    nullableDate(result.last_generated_at) &&
    typeof result.generated_count === "number" && Number.isInteger(result.generated_count) && result.generated_count >= 0 &&
    typeof result.created_at === "string" && recurringDate(result.created_at.slice(0, 10)) &&
    /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?$/.test(result.created_at) &&
    Number.isFinite(Date.parse(result.created_at));
}

function RecurringSchedules({ onGenerated }: { onGenerated: () => void }) {
  const qc = useQueryClient();
  const { user, org, hasPerm } = useAuth();
  const [open, setOpen] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [created, setCreated] = useState<{ attempt: RecurringCreateAttempt; schedule: RecurringSchedule } | null>(null);
  const [createError, setCreateError] = useState<{ attempt: RecurringCreateAttempt; message: string; uncertain: boolean } | null>(null);
  const authority = useRef({ userId: user?.id, orgId: org?.id });
  authority.current = { userId: user?.id, orgId: org?.id };
  const active = useRef(true);
  useEffect(() => { active.current = true; return () => { active.current = false; }; }, []);
  const editor = useRef(0);
  const createInFlight = useRef(false);
  const ownsAttempt = (attempt: RecurringCreateAttempt) => active.current &&
    authority.current.userId === attempt.userId && authority.current.orgId === attempt.orgId &&
    tokenStore.generation() === attempt.generation && tokenStore.get() === attempt.token;
  const canCreate = !!user?.id && !!org?.id && hasPerm("issued.write");
  const visibleCreated = created && ownsAttempt(created.attempt) ? created.schedule : null;
  const visibleCreateError = createError && ownsAttempt(createError.attempt) ? createError : null;

  const list = useQuery<RecurringSchedule[]>({
    queryKey: ["recurring", user?.id, org?.id],
    enabled: !!user?.id && !!org?.id,
    queryFn: async ({ signal }) => (await api.get("/issued/recurring", { signal, timeout: ISSUED_READ_TIMEOUT_MS })).data,
  });

  const create = useMutation<RecurringSchedule, unknown, RecurringCreateAttempt>({
    mutationFn: async (attempt) => {
      const response = await api.post<unknown>("/issued/recurring", attempt.payload);
      if (response.status !== 201 || !verifiedRecurringSchedule(response.data)) {
        throw new Error("The schedule outcome could not be verified.");
      }
      return response.data;
    },
    onSuccess: async (schedule, attempt) => {
      if (!ownsAttempt(attempt)) return;
      setCreateError(null);
      setCreated({ attempt, schedule });
      await qc.invalidateQueries({ queryKey: ["recurring", attempt.userId, attempt.orgId], exact: true });
      if (ownsAttempt(attempt) && editor.current === attempt.editor) setOpen(false);
    },
    onError: (error, attempt) => {
      if (!ownsAttempt(attempt)) return;
      const status = (error as { response?: { status?: number } } | null)?.response?.status;
      setCreateError({ attempt, message: apiError(error), uncertain: typeof status !== "number" || status >= 500 });
    },
    onSettled: () => { createInFlight.current = false; },
    retry: false,
  });

  const run = useMutation({
    mutationFn: async () => (await api.post("/issued/recurring/run")).data as GenerateResult,
    onSuccess: (r) => {
      setMsg(r.generated ? `Generated ${r.generated} invoice${r.generated === 1 ? "" : "s"}: ${r.numbers.join(", ")}` : "Nothing due right now.");
      qc.invalidateQueries({ queryKey: ["recurring"] });
      onGenerated();
    },
    onError: (e) => setMsg(apiError(e)),
  });

  const toggle = useMutation({
    mutationFn: async (v: { id: string; active: boolean }) => (await api.patch(`/issued/recurring/${v.id}`, { active: v.active })).data,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["recurring"] }),
  });
  const remove = useMutation({
    mutationFn: async (id: string) => api.delete(`/issued/recurring/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["recurring"] }),
  });

  return (
    <div className="card space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-sm font-semibold text-slate-600">Recurring schedules</h2>
          <p className="text-xs text-text-secondary">Generate invoices automatically on a cadence (retainers, subscriptions, rent).</p>
        </div>
        <div className="flex items-center gap-2">
          <button className="btn-ghost" disabled={run.isPending} onClick={() => { setMsg(null); run.mutate(); }}>
            {run.isPending ? "Running…" : "Run due now"}
          </button>
          <button className="btn-primary" disabled={!canCreate} onClick={() => { if (!open) editor.current += 1; setOpen((o) => !o); }}>{open ? "Close" : "New schedule"}</button>
        </div>
      </div>

      {msg && <div className="rounded-lg bg-slate-50 px-3 py-2 text-sm text-slate-600">{msg}</div>}

      {visibleCreated && <div className="min-w-0 rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-800 [overflow-wrap:anywhere]">
        <p role="status" aria-live="polite" aria-atomic="true" aria-label="Recurring schedule outcome">
          Recurring schedule created: {visibleCreated.title || "Untitled schedule"}. Next run: {shortDate(visibleCreated.next_run_date)}.
        </p>
      </div>}

      {open && <NewRecurring pending={create.isPending} canCreate={canCreate} onCreate={(payload) => {
        if (!canCreate || createInFlight.current || !user || !org) return;
        createInFlight.current = true;
        create.mutate({ userId: user.id, orgId: org.id, generation: tokenStore.generation(), token: tokenStore.get(), editor: editor.current, payload });
      }} />}

      {visibleCreateError && <div role="alert" className="rounded-lg bg-rose-50 px-3 py-2 text-sm text-rose-600">
        <p>{visibleCreateError.message}</p>
        {visibleCreateError.uncertain && <p>The creation outcome is unknown. Check the schedules before creating another; the request may have been saved.</p>}
      </div>}

      {(visibleCreated || visibleCreateError?.uncertain) && <div className="space-y-2 text-sm text-text-secondary">
        {list.isFetching && <p>Refreshing the schedule list…</p>}
        <button type="button" className="btn-ghost" disabled={create.isPending || list.isFetching} onClick={() => { void list.refetch(); }}>Refresh schedules</button>
      </div>}

      {list.isError && <ErrorState title="Couldn’t load the schedules" description="The schedule list could not be refreshed. Retrying this read will not create another schedule." onRetry={create.isPending || list.isFetching ? undefined : () => list.refetch()} />}
      {(list.data?.length ?? 0) > 0 && (
        <div className="overflow-x-auto rounded-lg border border-slate-200">
          <table className="w-full text-sm">
            <thead className="bg-slate-50 text-left text-xs uppercase text-slate-500">
              <tr>
                <th className="px-3 py-2">Schedule</th>
                <th className="px-3 py-2">Cadence</th>
                <th className="px-3 py-2">Next run</th>
                <th className="px-3 py-2">Generated</th>
                <th className="px-3 py-2">State</th>
                <th className="px-3 py-2"></th>
              </tr>
            </thead>
            <tbody>
              {list.data!.map((s) => (
                <tr key={s.id} className="border-t border-slate-100">
                  <td className="px-3 py-2 font-medium">{s.title || "Untitled schedule"}</td>
                  <td className="px-3 py-2 text-slate-500">
                    Every {s.interval > 1 ? `${s.interval} ` : ""}{s.frequency.replace(/ly$/, s.interval > 1 ? "s" : "")}
                  </td>
                  <td className="px-3 py-2 text-slate-500">{shortDate(s.next_run_date)}</td>
                  <td className="px-3 py-2 text-slate-500">{s.generated_count}</td>
                  <td className="px-3 py-2">
                    <span className={`badge ${s.active ? "bg-emerald-100 text-emerald-700" : "bg-slate-100 text-slate-500"}`}>
                      {s.active ? "active" : "paused"}
                    </span>
                  </td>
                  <td className="px-3 py-2 text-right whitespace-nowrap">
                    <button className="text-brand-600 hover:underline" onClick={() => toggle.mutate({ id: s.id, active: !s.active })} disabled={toggle.isPending}>
                      {s.active ? "pause" : "resume"}
                    </button>
                    <span className="mx-1.5 text-slate-300">·</span>
                    <button
                      className="text-rose-500 hover:underline"
                      onClick={() => { if (window.confirm("Delete this schedule? Invoices already generated are kept.")) remove.mutate(s.id); }}
                     disabled={remove.isPending}>
                      delete
                    </button>
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

function NewRecurring({ onCreate, pending, canCreate }: { onCreate: (payload: RecurringCreatePayload) => void; pending: boolean; canCreate: boolean }) {
  const [title, setTitle] = useState("");
  const [buyerName, setBuyerName] = useState("");
  const [buyerEmail, setBuyerEmail] = useState("");
  const [scheme, setScheme] = useState<VatScheme>("standard");
  const [frequency, setFrequency] = useState<RecurringFrequency>("monthly");
  const [interval, setInterval] = useState("1");
  const [startDate, setStartDate] = useState(new Date().toISOString().slice(0, 10));
  const [endDate, setEndDate] = useState("");
  const [lines, setLines] = useState<IssuedLineInput[]>([emptyLine()]);

  const zero = scheme !== "standard";
  const setLine = (i: number, patch: Partial<IssuedLineInput>) =>
    setLines(lines.map((l, idx) => (idx === i ? { ...l, ...patch } : l)));

  return (
    <div className="space-y-3 rounded-lg border border-slate-200 bg-slate-50/50 p-3">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        <Field label="Schedule title" v={title} on={setTitle} span2 />
        <div>
          <label className="label" htmlFor="frequency">Frequency</label>
          <select id="frequency" className="input" value={frequency} onChange={(e) => setFrequency(e.target.value as RecurringFrequency)}>
            {FREQUENCIES.map((f) => <option key={f.value} value={f.value}>{f.label}</option>)}
          </select>
        </div>
        <Field label="Customer name" v={buyerName} on={setBuyerName} span2 />
        <Field label="Customer email" v={buyerEmail} on={setBuyerEmail} />
        <div>
          <label className="label" htmlFor="every-n-periods">Every N periods</label>
          <input id="every-n-periods" className="input" inputMode="numeric" value={interval} onChange={(e) => setInterval(e.target.value)} />
        </div>
        <div>
          <label className="label" htmlFor="start-date">Start date</label>
          <input id="start-date" className="input" type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)} />
        </div>
        <div>
          <label className="label" htmlFor="end-date-optional">End date (optional)</label>
          <input id="end-date-optional" className="input" type="date" value={endDate} onChange={(e) => setEndDate(e.target.value)} />
        </div>
        <div>
          <label className="label" htmlFor="vat-scheme-2">VAT scheme</label>
          <select id="vat-scheme-2" className="input" value={scheme} onChange={(e) => setScheme(e.target.value as VatScheme)}>
            {SCHEMES.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
          </select>
        </div>
      </div>

      <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-left text-xs uppercase text-slate-500">
            <tr>
              <th className="px-3 py-2">Description</th>
              <th className="px-3 py-2 w-20">Qty</th>
              <th className="px-3 py-2 w-28">Unit price</th>
              <th className="px-3 py-2 w-20">Disc %</th>
              <th className="px-3 py-2 w-20">VAT %</th>
              <th className="px-3 py-2"></th>
            </tr>
          </thead>
          <tbody>
            {lines.map((l, i) => (
              <tr key={i} className="border-t border-slate-100">
                <td className="px-3 py-2"><input aria-label={`Line ${i + 1} description`} className="input" value={l.description} onChange={(e) => setLine(i, { description: e.target.value })} /></td>
                <td className="px-3 py-2"><input aria-label={`Line ${i + 1} quantity`} className="input" value={l.quantity} onChange={(e) => setLine(i, { quantity: e.target.value })} /></td>
                <td className="px-3 py-2"><input aria-label={`Line ${i + 1} unit price`} className="input" value={l.unit_price} onChange={(e) => setLine(i, { unit_price: e.target.value })} /></td>
                <td className="px-3 py-2"><input className="input" value={l.discount_percent ?? ""} placeholder="0" aria-label="0" onChange={(e) => setLine(i, { discount_percent: e.target.value })} /></td>
                <td className="px-3 py-2"><input aria-label={`Line ${i + 1} VAT rate`} className="input" value={l.vat_rate} disabled={zero} onChange={(e) => setLine(i, { vat_rate: e.target.value })} /></td>
                <td className="px-3 py-2 text-right">
                  {lines.length > 1 && (
                    <button className="text-rose-500 hover:underline" onClick={() => setLines(lines.filter((_, idx) => idx !== i))}>remove</button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <button className="btn-ghost" onClick={() => setLines([...lines, emptyLine()])}>+ Add line</button>

      <div className="flex justify-end">
        <button
          className="btn-primary"
          disabled={pending || !canCreate || !buyerName || lines.some((l) => !l.description)}
          onClick={() => onCreate({
            template: { buyer_name: buyerName, buyer_email: buyerEmail || null, vat_scheme: scheme, lines },
            frequency,
            interval: Number(interval) || 1,
            start_date: startDate,
            end_date: endDate || null,
            title: title || null,
          })}
        >
          {pending ? "Creating…" : "Create schedule"}
        </button>
      </div>
    </div>
  );
}

function Field({ label, v, on, span2 }: { label: string; v: string; on: (v: string) => void; span2?: boolean }) {
  const id = useId();
  return (
    <div className={span2 ? "sm:col-span-2" : ""}>
      <label className="label" htmlFor={id}>{label}</label>
      <input id={id} className="input" value={v} onChange={(e) => on(e.target.value)} />
    </div>
  );
}

interface LateInterestOut {
  basis: "statutory" | "contractual" | "unavailable";
  reason?: string;
  rate_pp?: string;
  interest_eur?: string;
  recovery_cost_eur?: string | null;
  total_eur?: string;
}

/** WO-K: the ADVISORY statutory late-payment figure (2011/7/EU) — fetched only
 * when the user asks, computed server-side, never booked. Shown on overdue
 * rows that carry no contractual interest of their own. */
function LateInterestPeek({ id }: { id: string }) {
  const [out, setOut] = useState<LateInterestOut | null>(null);
  const [busy, setBusy] = useState(false);

  const fetchIt = async () => {
    setBusy(true);
    try {
      const detail = (await api.get(`/issued/${id}`)).data as { late_interest: LateInterestOut | null };
      setOut(detail.late_interest ?? { basis: "unavailable", reason: "No figure applies." });
    } catch {
      setOut({ basis: "unavailable", reason: "Could not compute." });
    } finally {
      setBusy(false);
    }
  };

  if (out) {
    if (out.basis === "statutory") {
      return (
        <span title={`Statutory (2011/7/EU): ${out.rate_pp}% p.a. interest ${out.interest_eur} + €${out.recovery_cost_eur} recovery cost. Advisory — nothing is booked.`}>
          {" "}· statutory {out.total_eur} €
        </span>
      );
    }
    return <span title={out.reason ?? undefined}> · {out.reason ? "n/a" : `+${out.total_eur} €`}</span>;
  }
  return (
    <button className="ml-1 underline decoration-dotted" disabled={busy} onClick={fetchIt}>
      {busy ? "…" : "statutory interest?"}
    </button>
  );
}
