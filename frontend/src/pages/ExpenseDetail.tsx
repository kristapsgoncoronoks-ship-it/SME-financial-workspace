import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Field } from "../components/Field";
import { useToast } from "../components/Toast";
import { useAuth } from "../auth/AuthContext";
import { api, apiError, downloadFile } from "../lib/api";
import { EXPENSE_STATUS_STYLES, money, shortDate } from "../lib/format";
import {
  EXPENSE_CATEGORIES,
  type ApprovalStep,
  type ExpenseCategory,
  type ExpenseComment,
  type ExpenseItemInput,
  type ExpenseReportDetail,
  type ExpenseTransaction,
  type ExpenseType,
} from "../lib/types";
import { useConfirm } from "../components/ui/useConfirm";
import { ErrorState } from "../components/ui";

export default function ExpenseDetail() {
  const { confirm, dialog } = useConfirm();
  const { id } = useParams<{ id: string }>();
  const { user } = useAuth();
  const qc = useQueryClient();
  const toast = useToast();
  const navigate = useNavigate();
  const fileRef = useRef<HTMLInputElement>(null);
  const cameraRef = useRef<HTMLInputElement>(null);
  const uploadItemId = useRef<string | null>(null);

  const { data: r, isLoading, isError, refetch } = useQuery<ExpenseReportDetail>({
    queryKey: ["expense", id],
    queryFn: async () => (await api.get(`/expenses/${id}`)).data,
    enabled: !!id,
  });

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["expense", id] });
    qc.invalidateQueries({ queryKey: ["expenses"] });
  };
  const act = useMutation({
    mutationFn: async (p: { path: string; body?: unknown }) =>
      (await api.post(`/expenses/${id}/${p.path}`, p.body ?? {})).data,
    onSuccess: invalidate,
    onError: (e) => toast.error(apiError(e)),
  });
  const del = useMutation({
    mutationFn: async () => api.delete(`/expenses/${id}`),
    onSuccess: () => { invalidate(); navigate("/expenses"); },
  });
  const uploadReceipt = useMutation({
    mutationFn: async (v: { itemId: string; file: File }) => {
      const form = new FormData();
      form.append("file", v.file);
      return (await api.post(`/expenses/${id}/items/${v.itemId}/receipt`, form)).data;
    },
    onSuccess: invalidate,
    onError: (e) => toast.error(apiError(e)),
  });
  const patchItem = useMutation({
    mutationFn: async (v: { itemId: string; body: Record<string, unknown> }) =>
      (await api.patch(`/expenses/${id}/items/${v.itemId}`, v.body)).data,
    onSuccess: invalidate,
    onError: (e) => toast.error(apiError(e)),
  });
  const addItem = useMutation({
    mutationFn: async (body: ExpenseItemInput) =>
      (await api.post(`/expenses/${id}/items`, body)).data,
    onSuccess: invalidate,
    onError: (e) => toast.error(apiError(e)),
  });
  const match = useMutation({
    mutationFn: async (v: { itemId: string; transactionId: string }) =>
      (await api.post(`/expenses/${id}/items/${v.itemId}/match`, { transaction_id: v.transactionId })).data,
    onSuccess: invalidate,
    onError: (e) => toast.error(apiError(e)),
  });
  const unmatch = useMutation({
    mutationFn: async (itemId: string) => (await api.delete(`/expenses/${id}/items/${itemId}/match`)).data,
    onSuccess: invalidate,
    onError: (e) => toast.error(apiError(e)),
  });

  if (isLoading) return <div className="text-text-secondary">Loading…</div>;
  // FE-004: a failed read used to sit on "Loading…" for ever — the page never
  // said the report could not be loaded.
  if (isError || !r) {
    return <ErrorState title="Couldn’t load this expense report" onRetry={() => refetch()} />;
  }

  const isApprover = !!user?.is_expense_approver;
  const isOwnerOfReport = r.employee_id === user?.id;
  const canEdit = isOwnerOfReport && (r.status === "draft" || r.status === "returned");
  // Segregation of duties: an approver can't decide on their own report.
  const canDecide = isApprover && !isOwnerOfReport;
  const verifiedCount = r.items.filter((it) => it.verified).length;

  // Compliance: business purpose + a receipt (or a declaration; mileage/per-diem exempt).
  const itemMissing = (it: ExpenseReportDetail["items"][number]) => {
    const m: string[] = [];
    if (!it.comment || !it.comment.trim()) m.push("business purpose");
    const receiptExempt =
      it.expense_type === "mileage" ||
      it.expense_type === "per_diem" ||
      !!(it.missing_receipt_declaration && it.missing_receipt_declaration.trim());
    if (!it.has_receipt && !receiptExempt) m.push("receipt");
    return m;
  };
  const incompleteCount = r.items.filter((it) => itemMissing(it).length > 0).length;
  const readyToSubmit = r.items.length > 0 && incompleteCount === 0;

  const violations = r.policy_violations ?? [];
  const blocks = violations.filter((v) => v.severity === "block");
  const warns = violations.filter((v) => v.severity !== "block");
  const decide = (action: string) => act.mutate({ path: "decision", body: { action, version: r.version } });

  return (
    <div className="space-y-6">
      {dialog}
      <Link to="/expenses" className="text-sm text-brand-600 hover:underline">← Back to expenses</Link>

      {blocks.length > 0 && (
        <div role="alert" className="rounded-lg border border-rose-300 bg-rose-50 px-4 py-2 text-sm text-rose-800">
          <div className="font-medium">⛔ {blocks.length} item{blocks.length === 1 ? "" : "s"} blocked by policy — must be fixed before submission</div>
          <ul className="ml-4 list-disc">{blocks.map((v, i) => <li key={i}>{v.category ? `${v.category}: ` : ""}{v.message}</li>)}</ul>
        </div>
      )}
      {warns.length > 0 && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-2 text-sm text-amber-800">
          <div className="font-medium">⚠ {warns.length} item{warns.length === 1 ? "" : "s"} flagged for review</div>
          <ul className="ml-4 list-disc">{warns.map((v, i) => <li key={i}>{v.category ? `${v.category}: ` : ""}{v.message}</li>)}</ul>
        </div>
      )}
      {r.status === "returned" && (
        <div className="rounded-lg border border-orange-200 bg-orange-50 px-4 py-2 text-sm text-orange-800">
          This report was returned for correction{r.decision_note ? `: “${r.decision_note}”` : ""}. Fix it and resubmit.
        </div>
      )}

      <div className="card">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h1 className="text-xl font-semibold">{r.title}</h1>
            <p className="text-sm text-slate-500">{r.employee_name}{r.submitted_at && ` · submitted ${shortDate(r.submitted_at)}`}</p>
          </div>
          <div className="text-right">
            <div className="text-2xl font-semibold">{money(r.total, r.currency)}</div>
            <span className={`badge mt-1 ${EXPENSE_STATUS_STYLES[r.status] ?? ""}`}>{r.status.replace(/_/g, " ")}</span>
          </div>
        </div>
        <dl className="mt-4 grid grid-cols-1 gap-4 break-words text-sm sm:grid-cols-2 md:grid-cols-4">
          <Field label="Reclaimable tax" value={money(r.vat_total, r.currency)} />
          <Field label="Verified vs bank" value={`${verifiedCount} / ${r.items.length}`} />
          {r.total_eur && r.currency !== "EUR" && <Field label="Total (EUR, ECB)" value={money(r.total_eur)} />}
          {r.decided_by && <Field label="Decided by" value={r.decided_by} />}
        </dl>

        {canEdit && incompleteCount > 0 && (
          <div className="mt-4 rounded-lg bg-amber-50 px-3 py-2 text-sm text-amber-700">
            {incompleteCount} of {r.items.length} expense{r.items.length === 1 ? "" : "s"} still need a
            {" "}business purpose and/or a receipt (or a missing-receipt declaration) before you can submit.
          </div>
        )}

        <div className="mt-4 flex flex-wrap gap-2">
          {canEdit && (
            <button
              className="btn-primary disabled:opacity-50"
              disabled={!readyToSubmit || act.isPending}
              title={readyToSubmit ? "" : "Add a business purpose and receipt to every expense first"}
              onClick={() => act.mutate({ path: "submit" })}
            >
              Submit for approval
            </button>
          )}
          {isOwnerOfReport && r.status === "submitted" && (
            <button className="btn-ghost" onClick={() => act.mutate({ path: "withdraw" })} disabled={act.isPending}>Withdraw</button>
          )}
          {canEdit && r.status === "draft" && <button className="btn-ghost text-rose-600" onClick={async () => { if (await confirm({ title: "Delete this draft?", body: "The report goes to the recycle bin and can be restored for 30 days.", confirmLabel: "Delete draft" })) del.mutate(); }} disabled={del.isPending}>Delete draft</button>}
          {canDecide && (r.status === "submitted" || r.status === "partially_approved") && (
            <>
              <button className="btn bg-emerald-600 text-white hover:bg-emerald-700" onClick={() => decide("approve")}>Approve</button>
              <button className="btn border border-orange-300 bg-white text-orange-600 hover:bg-orange-50" onClick={() => decide("return_for_correction")}>Return for correction</button>
              <button className="btn border border-rose-300 bg-white text-rose-600 hover:bg-rose-50" onClick={() => decide("reject")}>Reject</button>
            </>
          )}
          {canDecide && r.status === "approved" && (
            <button className="btn bg-indigo-600 text-white hover:bg-indigo-700" onClick={() => decide("mark_for_reimbursement")}>Mark for reimbursement</button>
          )}
          {canDecide && (r.status === "approved" || r.status === "marked_for_reimbursement") && (
            <Link to="/reimbursements" className="btn-ghost min-w-0 max-w-full" aria-describedby="expense-payout-help"><span className="min-w-0 break-words">Open reimbursements</span></Link>
          )}
          {isApprover && isOwnerOfReport && r.status === "submitted" && (
            <span className="self-center text-xs text-text-secondary">Another approver must review your own report.</span>
          )}
          <button className="btn-ghost" onClick={() => downloadFile(`/expenses/${id}/pdf`, `expense-${r.title}.pdf`)}>Download PDF</button>
        </div>
        {(r.status === "approved" || r.status === "marked_for_reimbursement") && (
          <p id="expense-payout-help" className="mt-3 max-w-prose text-sm text-text-secondary">
            {canDecide
              ? "Record payment through a reimbursement batch so its payment record and checks are preserved."
              : "An expense approver records payment through a reimbursement batch."}
          </p>
        )}
      </div>

      <input aria-label="Receipt file" ref={fileRef} type="file" accept=".pdf,.png,.jpg,.jpeg" className="hidden"
        onChange={(e) => { const f = e.target.files?.[0]; if (f && uploadItemId.current) uploadReceipt.mutate({ itemId: uploadItemId.current, file: f }); e.target.value = ""; }} />
      {/* Mobile camera capture — opens the rear camera directly on phones. */}
      <input aria-label="Receipt photo" ref={cameraRef} type="file" accept="image/*" capture="environment" className="hidden"
        onChange={(e) => { const f = e.target.files?.[0]; if (f && uploadItemId.current) uploadReceipt.mutate({ itemId: uploadItemId.current, file: f }); e.target.value = ""; }} />

      {(r.approval_steps?.length ?? 0) > 0 && (
        <ApprovalChain reportId={id!} steps={r.approval_steps!} canReassign={canDecide} onChange={invalidate} />
      )}

      <div className="card overflow-x-auto p-0">
        <table className="w-full text-sm">
          <thead className="border-b border-slate-200 bg-slate-50 text-left text-xs uppercase tracking-wide text-slate-500">
            <tr>
              <th className="px-4 py-3">Date</th>
              <th className="px-4 py-3">Category</th>
              <th className="px-4 py-3">Description</th>
              <th className="px-4 py-3">Business purpose</th>
              <th className="px-4 py-3 text-right">Tax</th>
              <th className="px-4 py-3 text-right">Amount</th>
              <th className="px-4 py-3">Bank</th>
              <th className="px-4 py-3">Receipt</th>
            </tr>
          </thead>
          <tbody>
            {r.items.map((it) => {
              const missing = itemMissing(it);
              return (
              <tr key={it.id} className="border-b border-slate-100 last:border-0 align-top">
                <td className="px-4 py-3 text-slate-500">{shortDate(it.spend_date)}</td>
                <td className="px-4 py-3"><span className="badge bg-slate-100 text-slate-600">{it.category}</span></td>
                <td className="px-4 py-3">
                  {it.description}
                  {it.merchant && <div className="text-xs text-text-secondary">{it.merchant}</div>}
                  <div className="mt-1 flex flex-wrap gap-1">
                    {it.expense_type === "mileage" && <span className="badge bg-teal-100 text-teal-700">🚗 {it.mileage_distance} {it.mileage_unit ?? "km"} × {it.mileage_rate}</span>}
                    {it.expense_type === "per_diem" && <span className="badge bg-teal-100 text-teal-700">🗓 {it.per_diem_days}d × {it.per_diem_rate}</span>}
                    {it.currency && it.currency !== r.currency && it.original_amount && (
                      <span className="badge bg-slate-100 text-slate-500">{it.original_amount} {it.currency}{it.fx_source ? ` · ${it.fx_source}` : ""}</span>
                    )}
                    {it.customer_billable && <span className="badge bg-violet-100 text-violet-700">billable{it.billable_customer ? ` · ${it.billable_customer}` : ""}</span>}
                    {!it.reclaimable_tax && <span className="badge bg-slate-100 text-slate-500">tax non-reclaimable</span>}
                    {it.payment_method === "company_card" && <span className="badge bg-slate-100 text-slate-500">company card</span>}
                  </div>
                </td>
                <td className="px-4 py-3 min-w-[220px]">
                  {canEdit ? (
                    <BusinessPurpose value={it.comment ?? ""} onSave={(v) => patchItem.mutate({ itemId: it.id, body: { comment: v } })} />
                  ) : it.comment ? (
                    <span className="text-slate-600">{it.comment}</span>
                  ) : (
                    <span className="text-rose-500">— missing —</span>
                  )}
                </td>
                <td className="px-4 py-3 text-right text-slate-500">{money(it.vat_amount, r.currency)}</td>
                <td className="px-4 py-3 text-right font-medium">{money(it.amount, r.currency)}</td>
                <td className="px-4 py-3 min-w-[180px]">
                  {it.verified ? (
                    <div>
                      <span className="badge bg-emerald-100 text-emerald-700">✓ verified</span>
                      {it.bank_reference && <div className="mt-0.5 text-xs text-text-secondary">{it.bank_reference}</div>}
                      {canEdit && <button className="mt-0.5 block text-xs text-text-secondary hover:underline" onClick={() => unmatch.mutate(it.id)} disabled={unmatch.isPending}>unmatch</button>}
                    </div>
                  ) : canEdit && it.expense_type === "standard" ? (
                    <BankMatch reportId={id!} item={it} onMatch={(txnId) => match.mutate({ itemId: it.id, transactionId: txnId })} />
                  ) : (
                    <span className="text-slate-300">—</span>
                  )}
                </td>
                <td className="px-4 py-3">
                  {it.has_receipt ? (
                    <button className="text-brand-600 hover:underline" onClick={() => downloadFile(`/expenses/${id}/items/${it.id}/receipt`, `receipt-${it.id}`)}>view</button>
                  ) : it.expense_type !== "standard" ? (
                    <span className="text-slate-300">n/a</span>
                  ) : canEdit ? (
                    <div className="space-y-1">
                      <div className="flex gap-2">
                        <button className="font-medium text-brand-600 hover:underline" onClick={() => { uploadItemId.current = it.id; fileRef.current?.click(); }}>attach</button>
                        <button className="font-medium text-brand-600 hover:underline sm:inline" onClick={() => { uploadItemId.current = it.id; cameraRef.current?.click(); }}>📷 camera</button>
                      </div>
                      {it.missing_receipt_declaration
                        ? <div className="text-xs text-text-secondary">declared: {it.missing_receipt_declaration}</div>
                        : <MissingReceipt onDeclare={(v) => patchItem.mutate({ itemId: it.id, body: { missing_receipt_declaration: v } })} />}
                    </div>
                  ) : it.missing_receipt_declaration ? (
                    <span className="text-slate-500" title={it.missing_receipt_declaration}>declared missing</span>
                  ) : (
                    <span className="text-rose-400">missing</span>
                  )}
                  {canEdit && missing.length > 0 && <div className="mt-1 text-xs text-amber-600">needs {missing.join(" + ")}</div>}
                </td>
              </tr>
            );})}
          </tbody>
        </table>
      </div>

      {canEdit && <AddItem onAdd={(body) => addItem.mutate(body)} pending={addItem.isPending} />}

      <CommentThread reportId={id!} />
    </div>
  );
}

// Add a manual expense entry: standard / mileage / per-diem, mobile-friendly.
function AddItem({ onAdd, pending }: { onAdd: (b: ExpenseItemInput) => void; pending: boolean }) {
  const toast = useToast();
  const scanRef = useRef<HTMLInputElement>(null);
  const [scanning, setScanning] = useState(false);
  const [scanWarnings, setScanWarnings] = useState<string[]>([]);
  const [type, setType] = useState<ExpenseType>("standard");
  const [f, setF] = useState<Record<string, string>>({
    spend_date: new Date().toISOString().slice(0, 10),
    category: "travel",
    description: "",
    merchant: "",
    amount: "",
    vat_amount: "0",
    comment: "",
  });
  const set = (k: string, v: string) => setF((s) => ({ ...s, [k]: v }));

  // Advisory receipt OCR: scan a photo/PDF and prefill the fields to confirm.
  const scan = async (file: File) => {
    setScanWarnings([]);
    setScanning(true);
    try {
      const form = new FormData();
      form.append("file", file);
      const s = (await api.post("/expenses/receipt-scan", form)).data;
      setScanWarnings(Array.isArray(s.warnings) ? s.warnings.filter((warning: unknown): warning is string => typeof warning === "string" && warning.trim().length > 0) : []);
      setF((cur) => ({
        ...cur,
        merchant: s.merchant ?? cur.merchant,
        amount: s.amount ?? cur.amount,
        vat_amount: s.vat_amount ?? cur.vat_amount,
        spend_date: s.spend_date ?? cur.spend_date,
        description: cur.description || s.merchant || "",
      }));
      toast.success(`Scanned (${s.method}). Review the prefilled fields.`);
    } catch (e) {
      toast.error(apiError(e));
    } finally {
      setScanning(false);
    }
  };

  const submit = () => {
    const body: ExpenseItemInput = {
      spend_date: f.spend_date,
      category: f.category as ExpenseCategory,
      description: f.description || (type === "mileage" ? "Mileage" : type === "per_diem" ? "Per diem" : ""),
      merchant: f.merchant || null,
      amount: type === "standard" ? f.amount || "0" : "0",
      vat_amount: f.vat_amount || "0",
      payment_method: "personal",
      comment: f.comment || undefined,
      expense_type: type,
    };
    if (type === "mileage") { body.mileage_distance = f.mileage_distance || "0"; body.mileage_rate = f.mileage_rate || "0"; body.mileage_unit = "km"; }
    if (type === "per_diem") { body.per_diem_days = f.per_diem_days || "0"; body.per_diem_rate = f.per_diem_rate || "0"; }
    onAdd(body);
    setF((s) => ({ ...s, description: "", merchant: "", amount: "", comment: "", mileage_distance: "", mileage_rate: "", per_diem_days: "", per_diem_rate: "" }));
  };

  const inp = "input py-1 text-sm";
  return (
    <div className="card space-y-3">
      <input aria-label="Receipt to scan"
        ref={scanRef}
        type="file"
        accept="image/*,.pdf"
        capture="environment"
        className="hidden"
        onChange={(e) => { const file = e.target.files?.[0]; if (file) scan(file); e.target.value = ""; }}
      />
      <div className="flex items-center gap-2">
        <h2 className="text-sm font-semibold text-slate-600">Add an expense</h2>
        {type === "standard" && (
          <button className="btn-ghost text-xs" disabled={scanning} onClick={() => scanRef.current?.click()}>
            {scanning ? "Scanning…" : "📷 Scan receipt"}
          </button>
        )}
        <div className="ml-auto flex rounded-lg border border-slate-200 p-0.5 text-xs">
          {(["standard", "mileage", "per_diem"] as ExpenseType[]).map((t) => (
            <button key={t} className={`rounded-sm px-2 py-1 ${type === t ? "bg-brand-50 text-brand-700" : "text-slate-500"}`} onClick={() => setType(t)}>
              {t === "standard" ? "Standard" : t === "mileage" ? "Mileage" : "Per diem"}
            </button>
          ))}
        </div>
      </div>
      {scanWarnings.length > 0 && (
        <div role="status" aria-live="polite" aria-atomic="true" aria-label="Receipt reading notes" className="min-w-0 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900 [overflow-wrap:anywhere]">
          <ul className="space-y-1">{scanWarnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul>
        </div>
      )}
      <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
        <input aria-label="Spend date" className={inp} type="date" value={f.spend_date} onChange={(e) => set("spend_date", e.target.value)} />
        <select aria-label="Category" className={inp} value={f.category} onChange={(e) => set("category", e.target.value)}>
          {EXPENSE_CATEGORIES.map((c) => <option key={c} value={c}>{c}</option>)}
        </select>
        <input className={`${inp} col-span-2`} placeholder="Description" aria-label="Description" value={f.description} onChange={(e) => set("description", e.target.value)} />
        {type === "standard" && <>
          <input className={inp} placeholder="Merchant" aria-label="Merchant" value={f.merchant} onChange={(e) => set("merchant", e.target.value)} />
          <input className={inp} inputMode="decimal" placeholder="Amount" aria-label="Amount" value={f.amount} onChange={(e) => set("amount", e.target.value)} />
          <input className={inp} inputMode="decimal" placeholder="Tax" aria-label="Tax" value={f.vat_amount} onChange={(e) => set("vat_amount", e.target.value)} />
        </>}
        {type === "mileage" && <>
          <input className={inp} inputMode="decimal" placeholder="Distance (km)" aria-label="Distance (km)" value={f.mileage_distance ?? ""} onChange={(e) => set("mileage_distance", e.target.value)} />
          <input className={inp} inputMode="decimal" placeholder="Rate / km" aria-label="Rate / km" value={f.mileage_rate ?? ""} onChange={(e) => set("mileage_rate", e.target.value)} />
        </>}
        {type === "per_diem" && <>
          <input className={inp} inputMode="decimal" placeholder="Days" aria-label="Days" value={f.per_diem_days ?? ""} onChange={(e) => set("per_diem_days", e.target.value)} />
          <input className={inp} inputMode="decimal" placeholder="Rate / day" aria-label="Rate / day" value={f.per_diem_rate ?? ""} onChange={(e) => set("per_diem_rate", e.target.value)} />
        </>}
        <input className={`${inp} col-span-2 md:col-span-4`} placeholder="Business purpose (required to submit)" aria-label="Business purpose (required to submit)" value={f.comment} onChange={(e) => set("comment", e.target.value)} />
      </div>
      <div className="flex justify-end">
        <button className="btn-primary" disabled={pending || !f.description} onClick={submit}>Add expense</button>
      </div>
    </div>
  );
}

// Declare a missing receipt (an affidavit that substitutes for the document).
function MissingReceipt({ onDeclare }: { onDeclare: (v: string) => void }) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  if (!open) return <button className="text-xs text-text-secondary hover:underline" onClick={() => setOpen(true)}>declare missing</button>;
  return (
    <div className="flex gap-1">
      <input className="input py-0.5 text-xs" placeholder="Reason receipt is missing" aria-label="Reason receipt is missing" value={text} onChange={(e) => setText(e.target.value)} autoFocus />
      <button className="text-xs text-brand-600 hover:underline" disabled={!text.trim()} onClick={() => { onDeclare(text.trim()); setOpen(false); }}>save</button>
    </div>
  );
}

// Reconcile a draft item against a bank/card statement line.
function BankMatch({ reportId, item, onMatch }:
  { reportId: string; item: ExpenseReportDetail["items"][number]; onMatch: (txnId: string) => void }) {
  const [open, setOpen] = useState(false);
  const candidates = useQuery<ExpenseTransaction[]>({
    queryKey: ["match-candidates", reportId, item.id],
    queryFn: async () => (await api.get(`/expenses/${reportId}/items/${item.id}/match-candidates`)).data,
    enabled: open,
  });

  if (!open) return <button className="text-brand-600 hover:underline" onClick={() => setOpen(true)}>match to bank</button>;
  return (
    <div className="space-y-1">
      {candidates.isLoading && <span className="text-xs text-text-secondary">Searching…</span>}
      {candidates.isError && <span role="alert" className="text-xs text-rose-600">Couldn’t search the statement lines.</span>}
      {candidates.data?.length === 0 && <span className="text-xs text-amber-600">No matching statement line.</span>}
      {candidates.data?.map((t) => (
        <button key={t.id} className="block w-full rounded-sm border border-slate-200 px-2 py-1 text-left text-xs hover:bg-slate-50" onClick={() => onMatch(t.id)}>
          {shortDate(t.txn_date)} · {t.merchant || t.description} · {money(t.amount, t.currency)}
        </button>
      ))}
      <button className="text-xs text-text-secondary hover:underline" onClick={() => setOpen(false)}>cancel</button>
    </div>
  );
}

// Inline-editable business purpose for a draft expense item. Saves on blur/Enter.
function BusinessPurpose({ value, onSave }: { value: string; onSave: (v: string) => void }) {
  const [text, setText] = useState(value);
  const dirty = text.trim() !== value.trim();
  return (
    <input
      className={`input py-1 text-sm ${!value.trim() ? "border-rose-300 bg-rose-50 placeholder:text-rose-400" : ""}`}
      placeholder="Why was this spent? (required)" aria-label="Why was this spent? (required)"
      value={text}
      onChange={(e) => setText(e.target.value)}
      onBlur={() => dirty && onSave(text.trim())}
      onKeyDown={(e) => { if (e.key === "Enter") { (e.target as HTMLInputElement).blur(); } }}
    />
  );
}

function CommentThread({ reportId }: { reportId: string }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [body, setBody] = useState("");
  const comments = useQuery<ExpenseComment[]>({
    queryKey: ["expense", reportId, "comments"],
    queryFn: async () => (await api.get(`/expenses/${reportId}/comments`)).data,
  });
  const post = useMutation({
    mutationFn: async () => (await api.post(`/expenses/${reportId}/comments`, { body })).data,
    onSuccess: () => { setBody(""); qc.invalidateQueries({ queryKey: ["expense", reportId, "comments"] }); },
    onError: (e) => toast.error(apiError(e)),
  });

  return (
    <div className="card space-y-3">
      <h2 className="text-sm font-semibold text-slate-600">Comments</h2>
      <div className="space-y-3">
        {comments.isError && <p role="alert" className="text-sm text-rose-600">Couldn’t load the comments.</p>}
        {comments.data?.length === 0 && <p className="text-sm text-text-secondary">No comments yet.</p>}
        {comments.data?.map((c) => (
          <div key={c.id} className="flex gap-3">
            <div className="grid h-8 w-8 shrink-0 place-items-center rounded-full bg-brand-100 text-xs font-semibold text-brand-700">
              {c.author_name.slice(0, 2).toUpperCase()}
            </div>
            <div className="flex-1 rounded-lg bg-slate-50 px-3 py-2">
              <div className="text-xs text-text-secondary">{c.author_name} · {shortDate(c.created_at)}</div>
              <div className="text-sm text-slate-700 whitespace-pre-wrap">{c.body}</div>
            </div>
          </div>
        ))}
      </div>
      <div className="flex gap-2">
        <input className="input flex-1" placeholder="Add a comment…" aria-label="Add a comment" value={body} onChange={(e) => setBody(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && body.trim()) post.mutate(); }} />
        <button className="btn-primary" disabled={!body.trim() || post.isPending} onClick={() => post.mutate()}>Post</button>
      </div>
    </div>
  );
}

// The report's multi-step approval chain: an ordered timeline of steps + a
// reassign control on the current pending step (approvers only).
function ApprovalChain({ reportId, steps, canReassign, onChange }:
  { reportId: string; steps: ApprovalStep[]; canReassign: boolean; onChange: () => void }) {
  const toast = useToast();
  const [reassigning, setReassigning] = useState(false);
  const members = useQuery<{ id: string; email: string; name: string; is_expense_approver?: boolean }[]>({
    queryKey: ["team", "members"],
    queryFn: async () => (await api.get("/team/members")).data,
    enabled: reassigning,
  });
  const reassign = useMutation({
    mutationFn: async (approver_id: string) =>
      (await api.post(`/expenses/${reportId}/reassign`, { approver_id })).data,
    onSuccess: () => { setReassigning(false); onChange(); },
    onError: (e) => toast.error(apiError(e)),
  });

  const tone: Record<string, string> = {
    approved: "bg-emerald-100 text-emerald-700",
    rejected: "bg-rose-100 text-rose-700",
    skipped: "bg-slate-100 text-text-secondary",
    pending: "bg-amber-100 text-amber-700",
  };
  const pendingSeq = steps.find((s) => s.status === "pending")?.seq;

  return (
    <div className="card space-y-2">
      <h2 className="text-sm font-semibold text-slate-600">Approval chain</h2>
      <ol className="space-y-2">
        {steps.map((s) => (
          <li key={s.id} className="flex flex-wrap items-center gap-2 text-sm">
            <span className="grid h-6 w-6 place-items-center rounded-full bg-slate-100 text-xs font-semibold text-slate-500">{s.seq + 1}</span>
            <span className={`badge ${tone[s.status] ?? "bg-slate-100 text-slate-500"}`}>{s.status}</span>
            <span className="text-slate-600">
              {s.kind === "finance_final" ? "Finance sign-off · " : ""}
              {s.approver_email ?? "Any approver"}
            </span>
            {s.decided_by_email && s.status !== "pending" && (
              <span className="text-xs text-text-secondary">by {s.decided_by_email}</span>
            )}
            {canReassign && s.seq === pendingSeq && !reassigning && (
              <button className="text-xs text-brand-600 hover:underline" onClick={() => setReassigning(true)}>reassign</button>
            )}
            {canReassign && s.seq === pendingSeq && reassigning && (
              <span className="flex items-center gap-1">
                <select aria-label="Reassign to"
                  className="input py-0.5 text-xs"
                  defaultValue=""
                  onChange={(e) => e.target.value && reassign.mutate(e.target.value)}
                >
                  <option value="" disabled>Reassign to…</option>
                  {(members.data ?? []).filter((m) => m.is_expense_approver).map((m) => (
                    <option key={m.id} value={m.id}>{m.name} ({m.email})</option>
                  ))}
                </select>
                <button className="text-xs text-text-secondary hover:underline" onClick={() => setReassigning(false)}>cancel</button>
              </span>
            )}
          </li>
        ))}
      </ol>
    </div>
  );
}
