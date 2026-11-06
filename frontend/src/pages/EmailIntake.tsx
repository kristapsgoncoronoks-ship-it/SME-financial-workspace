import { CaptureReadingDetails } from "../components/CaptureReadingDetails";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { Field } from "../components/Field";
import { useToast } from "../components/Toast";
import { api, apiError, downloadFile } from "../lib/api";
import { INBOUND_STATUS_STYLES as STATUS_STYLES, METHOD_STYLES, methodLabel, money, shortDate } from "../lib/format";
import type { ChannelHealth, EmailSettings, InboundInvoiceDetail, InboundList } from "../lib/types";
import { ErrorState } from "../components/ui";

export default function EmailIntake() {
  const { hasPerm } = useAuth();
  const qc = useQueryClient();
  const toast = useToast();
  const [selected, setSelected] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const settings = useQuery<EmailSettings>({
    queryKey: ["email-settings"],
    queryFn: async () => (await api.get("/email/settings")).data,
  });
  const inbox = useQuery<InboundList>({
    queryKey: ["email-inbox"],
    queryFn: async () => (await api.get("/email/inbox")).data,
  });
  // H-2: an inbound channel can die while every other panel on this page stays
  // green, because "no invoices arrived" and "nothing could get through" look
  // identical from a count. This states which one it is.
  const health = useQuery<ChannelHealth[]>({
    queryKey: ["email-health"],
    queryFn: async () => (await api.get("/email/health")).data,
  });
  const cadence = useMutation({
    mutationFn: async (days: number | null) =>
      (await api.put("/email/health/email", { expected_cadence_days: days })).data,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["email-health"] }),
    onError: (e) => toast.error(apiError(e)),
  });
  const email = health.data?.find((c) => c.channel === "email");

  const rotate = useMutation({
    mutationFn: async () => (await api.post("/email/settings/rotate")).data,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["email-settings"] }),
    onError: (e) => toast.error(apiError(e)),
  });

  const copy = () => {
    if (!settings.data) return;
    navigator.clipboard?.writeText(settings.data.address);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Email invoice intake</h1>
        <p className="text-sm text-slate-500">
          Forward supplier invoices to your dedicated address. Attachments (PDF, e-invoice XML, CSV, JSON)
          are auto-parsed into a review inbox — you confirm each one into an invoice, exactly like an upload.
        </p>
      </div>

      <div className="card space-y-3">
        <div className="label">Your inbound address</div>
        {settings.isError && (
          <ErrorState title="Couldn’t load your inbound address" onRetry={() => settings.refetch()} />
        )}
        <div className="flex flex-wrap items-center gap-2">
          <code className="max-w-full break-all rounded-lg bg-slate-100 px-3 py-2 text-sm font-medium text-slate-700">
            {settings.data?.address ?? "…"}
          </code>
          <button className="btn-ghost" onClick={copy}>{copied ? "Copied!" : "Copy"}</button>
          {hasPerm("settings.manage") && (
            <button
              className="btn-ghost text-rose-600"
              onClick={() => { if (confirm("Rotate the inbound address? The old one will stop working.")) rotate.mutate(); }}
             disabled={rotate.isPending}>
              Rotate
            </button>
          )}
        </div>
        <p className="text-xs text-text-secondary">
          Point your mailbox forwarding (or your provider's inbound-parse webhook) at this address.
          Anything sent here lands in the inbox below.
        </p>
      </div>

      {health.isError && (
        <div className="card">
          <ErrorState title="Couldn’t check the inbound channel" onRetry={() => health.refetch()} />
        </div>
      )}
      {email && (
        <div
          className={
            "card space-y-2 " +
            (email.state === "failing"
              ? "border-rose-200 bg-rose-50"
              : email.state === "silent"
                ? "border-amber-200 bg-amber-50"
                : "")
          }
        >
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <h2 className="text-sm font-semibold text-slate-700">Is this address still working?</h2>
            {hasPerm("settings.manage") && (
              <label className="flex items-center gap-2 text-xs text-slate-500">
                Tell us when to worry:
                <select
                  className="input py-1 text-xs"
                  value={email.expected_cadence_days ?? ""}
                  onChange={(e) =>
                    cadence.mutate(e.target.value === "" ? null : Number(e.target.value))
                  }
                >
                  {/* An empty value is not "no setting" — it is an explicit refusal
                      to guess, and the label says so. */}
                  <option value="">don’t judge my quiet periods</option>
                  <option value="1">I expect invoices daily</option>
                  <option value="7">at least weekly</option>
                  <option value="14">at least fortnightly</option>
                  <option value="30">at least monthly</option>
                </select>
              </label>
            )}
          </div>
          <p className="text-sm text-slate-700">{email.headline}</p>
          {email.consecutive_failures > 0 && (
            <p className="text-xs text-slate-500">
              {email.consecutive_failures}{" "}
              {email.consecutive_failures === 1 ? "delivery has" : "deliveries have"} failed since
              the last success.
            </p>
          )}
        </div>
      )}

      <div className="grid min-w-0 grid-cols-1 gap-6 lg:grid-cols-5">
        <div className="min-w-0 lg:col-span-2">
          <div className="card p-0">
            <div className="flex items-center justify-between border-b border-slate-200 px-4 py-3">
              <h2 className="text-sm font-semibold text-slate-600">Inbox</h2>
              {/* Not "0 received" when the read failed — the alert is below. */}
              {!!inbox.data && (
                <span className="text-xs text-text-secondary">{inbox.data.total} received</span>
              )}
            </div>
            {inbox.isError ? (
              <ErrorState title="Couldn’t load the inbox" onRetry={() => inbox.refetch()} />
            ) : inbox.isLoading ? (
              <div className="px-4 py-6 text-sm text-text-secondary">Loading…</div>
            ) : (inbox.data?.items.length ?? 0) === 0 ? (
              <div className="px-4 py-8 text-center text-sm text-text-secondary">
                Nothing yet. Forward an invoice to the address above.
              </div>
            ) : (
              <ul className="divide-y divide-slate-100">
                {inbox.data!.items.map((it) => (
                  <li key={it.id}>
                    <button
                      className={`flex w-full flex-wrap items-start justify-between gap-3 px-4 py-3 text-left hover:bg-slate-50 ${
                        selected === it.id ? "bg-brand-50" : ""
                      }`}
                      onClick={() => setSelected(it.id)}
                    >
                      <div className="min-w-0">
                        <div className="truncate text-sm font-medium text-slate-700">{it.filename}</div>
                        <div className="truncate text-xs text-text-secondary">
                          {it.from_addr || "unknown sender"} · {shortDate(it.received_at)}
                        </div>
                        {(it.status === "pending" || it.status === "confirmed") && it.method && it.method !== "unknown" && METHOD_STYLES[it.method] && (
                          <span className={`badge mt-1 max-w-full whitespace-normal ${METHOD_STYLES[it.method]}`}>
                            {methodLabel(it.method)}
                          </span>
                        )}
                      </div>
                      <span className={`badge shrink-0 ${STATUS_STYLES[it.status] ?? ""}`}>{it.status}</span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>

        <div className="min-w-0 lg:col-span-3">
          {selected ? (
            <InboundDetail id={selected} onDone={() => { setSelected(null); }} />
          ) : (
            <div className="card grid place-items-center py-16 text-sm text-text-secondary">
              Select an email to review its parsed invoice.
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function InboundDetail({ id, onDone }: { id: string; onDone: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const navigate = useNavigate();
  const detail = useQuery<InboundInvoiceDetail>({
    queryKey: ["email-inbox", id],
    queryFn: async () => (await api.get(`/email/inbox/${id}`)).data,
  });
  const { data: row, isLoading } = detail;

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["email-inbox"] });
    qc.invalidateQueries({ queryKey: ["email-settings"] });
  };
  const confirm_ = useMutation({
    mutationFn: async () => (await api.post(`/email/inbox/${id}/confirm`, {})).data,
    onSuccess: (inv: { id: string }) => { invalidate(); navigate(`/invoices/${inv.id}`); },
    onError: (e) => toast.error(apiError(e)),
  });
  const discard = useMutation({
    mutationFn: async () => (await api.post(`/email/inbox/${id}/discard`)).data,
    onSuccess: () => { invalidate(); onDone(); },
    onError: (e) => toast.error(apiError(e)),
  });
  const del = useMutation({
    mutationFn: async () => api.delete(`/email/inbox/${id}`),
    onSuccess: () => { invalidate(); onDone(); },
    onError: (e) => toast.error(apiError(e)),
  });

  if (detail.isError) {
    return (
      <div className="card">
        <ErrorState title="Couldn’t load this email" onRetry={() => detail.refetch()} />
      </div>
    );
  }
  if (isLoading || !row) return <div className="card text-sm text-text-secondary">Loading…</div>;

  const d = row.draft?.draft;
  const total = d
    ? d.line_items.reduce((s, li) => s + Number(li.amount ?? Number(li.quantity) * Number(li.unit_price)), 0)
    : 0;

  return (
    <div className="card space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0 max-w-full break-words">
          <h2 className="text-sm font-semibold text-slate-600">{row.filename}</h2>
          <p className="text-xs text-text-secondary">
            {row.subject || "(no subject)"} · from {row.from_addr || "unknown"} · {shortDate(row.received_at)}
          </p>
        </div>
        <div className="flex max-w-full shrink-0 flex-wrap items-center gap-2">
          {(row.status === "pending" || row.status === "confirmed") && row.method && METHOD_STYLES[row.method] && (
            <span className={`badge max-w-full whitespace-normal ${METHOD_STYLES[row.method]}`}>
              {methodLabel(row.method)}
            </span>
          )}
          <span className={`badge ${STATUS_STYLES[row.status] ?? ""}`}>{row.status}</span>
        </div>
      </div>

      <CaptureReadingDetails channel="inbound" ownerId={id} revision={`${row.status}:${row.reading_observation?.id ?? "unknown"}`} />

      {row.has_file && (
        <button className="text-sm text-brand-600 hover:underline" onClick={() => downloadFile(`/email/inbox/${id}/file`, row.filename)}>
          Download original attachment
        </button>
      )}

      {row.status === "rejected" && (
        <div role="alert" className="rounded-lg bg-rose-50 px-3 py-2 text-sm text-rose-600">
          🛡️ Blocked by the security scan — {row.error}. The file was quarantined and not stored.
        </div>
      )}

      {row.status === "failed" && (
        <div role="alert" className="rounded-lg bg-rose-50 px-3 py-2 text-sm text-rose-600">
          Could not parse this attachment: {row.error}
        </div>
      )}

      {row.invoice_id && (
        <div className="rounded-lg bg-emerald-50 px-3 py-2 text-sm text-emerald-700">
          Confirmed into an invoice.{" "}
          <button className="underline" onClick={() => navigate(`/invoices/${row.invoice_id}`)}>View invoice</button>
        </div>
      )}

      {d && (
        <>
          {row.draft!.warnings.length > 0 && (
            <ul className="rounded-lg bg-amber-50 px-4 py-3 text-xs text-amber-700">
              {row.draft!.warnings.map((w, i) => <li key={i}>• {w}</li>)}
            </ul>
          )}
          <dl className="grid grid-cols-2 gap-3 text-sm md:grid-cols-3">
            <Field label="Vendor" value={d.vendor_name || "—"} />
            <Field label="Invoice #" value={d.invoice_number} />
            <Field label="Issue date" value={d.issue_date} />
          </dl>
          <div className="overflow-x-auto rounded-lg border border-slate-200">
            <table className="w-full text-sm">
              <thead className="bg-slate-50 text-left text-xs uppercase text-slate-500">
                <tr>
                  <th className="px-3 py-2">Description</th>
                  <th className="px-3 py-2">Category</th>
                  <th className="px-3 py-2 text-right">Qty</th>
                  <th className="px-3 py-2 text-right">Unit</th>
                  <th className="px-3 py-2 text-right">Amount</th>
                </tr>
              </thead>
              <tbody>
                {d.line_items.map((li, i) => (
                  <tr key={i} className="border-t border-slate-100">
                    <td className="px-3 py-2">{li.description}</td>
                    <td className="px-3 py-2 text-slate-500">{li.category}</td>
                    <td className="px-3 py-2 text-right text-slate-500">{Number(li.quantity)}</td>
                    <td className="px-3 py-2 text-right text-slate-500">{money(li.unit_price)}</td>
                    <td className="px-3 py-2 text-right font-medium">
                      {money(li.amount ?? Number(li.quantity) * Number(li.unit_price))}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="text-right text-sm text-slate-500">Net of tax · subtotal {money(total)}</div>
        </>
      )}

      <div className="flex flex-wrap gap-2">
        {row.status === "pending" && d && (
          <button className="btn-primary" disabled={confirm_.isPending} onClick={() => confirm_.mutate()}>
            {confirm_.isPending ? "Confirming…" : "Confirm as invoice"}
          </button>
        )}
        {(row.status === "pending" || row.status === "failed" || row.status === "rejected") && (
          <button className="btn-ghost" onClick={() => discard.mutate()} disabled={discard.isPending}>Discard</button>
        )}
        <button className="btn-ghost text-rose-600" onClick={() => { if (confirm("Delete this inbound email permanently?")) del.mutate(); }} disabled={del.isPending}>
          Delete
        </button>
      </div>
    </div>
  );
}
