import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import {
  Bar, BarChart, CartesianGrid, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import { PageHeader, QueryState } from "../components/ui";
import { api, downloadFile } from "../lib/api";
import { CHART_PALETTE as PALETTE, compactMoney, money, monthLabel, shortDate } from "../lib/format";
import { useModules } from "../lib/useModules";
import type {
  IssuedPartnerReport, IssuedReceivablesReport, IssuedSummaryReport, IssuedVatReport,
} from "../lib/types";

type Tab = "summary" | "receivables" | "partners" | "vat";
const TABS: { key: Tab; label: string }[] = [
  { key: "summary", label: "Issued & turnover" },
  { key: "receivables", label: "Paid · unpaid · overdue" },
  { key: "partners", label: "Partners" },
  { key: "vat", label: "Output VAT" },
];

const SCHEME_LABELS: Record<string, string> = {
  standard: "Standard VAT", reverse_charge: "Reverse charge",
  intra_eu: "Intra-EU (exempt)", exempt: "Exempt",
};

export default function IssuedReports() {
  const modules = useModules();
  const [tab, setTab] = useState<Tab>("summary");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [currency, setCurrency] = useState("");

  const params = () => {
    const p: Record<string, string> = {};
    if (from) p.from = from;
    if (to) p.to = to;
    if (currency) p.currency = currency;
    return p;
  };
  const qs = () => new URLSearchParams(params()).toString();

  if (modules.data && !modules.isEnabled("issuing")) {
    return (
      <Gate>
        The invoice issuing module isn't active. Activate it in{" "}
        <Link to="/settings" className="font-medium underline">Settings → Modules</Link>.
      </Gate>
    );
  }

  return (
    <div className="space-y-6">
      <PageHeader
        title="Invoice reports"
        description="Reporting on invoices you issue. Amounts are net of VAT unless labelled gross."
        actions={<Link to="/issue" className="btn-ghost">Back to issuing</Link>}
      />

      {/* Filters */}
      <div className="card flex flex-wrap items-end gap-4">
        <label className="text-sm">
          <span className="label">From</span>
          <input type="date" className="input" value={from} onChange={(e) => setFrom(e.target.value)} />
        </label>
        <label className="text-sm">
          <span className="label">To</span>
          <input type="date" className="input" value={to} onChange={(e) => setTo(e.target.value)} />
        </label>
        <label className="text-sm">
          <span className="label">Currency</span>
          <input className="input w-24" placeholder="all" value={currency}
                 onChange={(e) => setCurrency(e.target.value.toUpperCase())} maxLength={3} />
        </label>
        {(from || to || currency) && (
          <button className="btn-ghost" onClick={() => { setFrom(""); setTo(""); setCurrency(""); }}>
            Reset
          </button>
        )}
        <div className="ml-auto">
          <button
            className="btn-ghost"
            onClick={() => downloadFile(`/issued/reports/${tab === "summary" ? "summary" : tab}?format=csv&${qs()}`, `issued-${tab}.csv`)}
          >
            Export CSV
          </button>
        </div>
      </div>

      {/* Tabs */}
      <div className="flex flex-wrap gap-2 border-b border-slate-200">
        {TABS.map((t) => (
          <button
            key={t.key}
            onClick={() => setTab(t.key)}
            className={`-mb-px border-b-2 px-3 py-2 text-sm font-medium transition ${
              tab === t.key ? "border-brand-500 text-brand-600" : "border-transparent text-slate-500 hover:text-slate-700"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === "summary" && <SummaryTab params={params()} />}
      {tab === "receivables" && <ReceivablesTab params={params()} />}
      {tab === "partners" && <PartnersTab params={params()} />}
      {tab === "vat" && <VatTab params={params()} schemeLabels={SCHEME_LABELS} />}
    </div>
  );
}

function useReport<T>(name: string, params: Record<string, string>) {
  return useQuery<T>({
    queryKey: ["issued-report", name, params],
    queryFn: async () => (await api.get(`/issued/reports/${name}`, { params })).data,
  });
}

function Kpi({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="card">
      <div className="text-sm text-slate-500">{label}</div>
      <div className="mt-1 text-2xl font-semibold tracking-tight text-slate-800">{value}</div>
      {sub && <div className="text-xs text-text-secondary">{sub}</div>}
    </div>
  );
}

function SummaryTab({ params }: { params: Record<string, string> }) {
  const q = useReport<IssuedSummaryReport>("summary", params);
  return (
    <QueryState query={q} loading={<Loading />} errorTitle="Couldn’t load the turnover report">
      {(d) => {
        const rows = d.series.map((s) => ({ label: monthLabel(s.period), gross: Number(s.gross) }));
        return (
          <div className="space-y-6">
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
              <Kpi label="Invoices issued" value={String(d.count)} sub={`${d.currency}`} />
              <Kpi label="Net turnover" value={money(d.net, d.currency)} sub="VAT-exclusive" />
              <Kpi label="VAT charged" value={money(d.vat, d.currency)} />
              <Kpi label="Gross issued" value={money(d.gross, d.currency)} sub="VAT-inclusive" />
            </div>
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <Kpi label="Collected" value={money(d.collected, d.currency)} sub="payments received" />
              <Kpi label="Outstanding" value={money(d.outstanding, d.currency)} sub="still owed to you" />
            </div>

            <div className="card">
              <h2 className="mb-3 text-sm font-semibold text-slate-600">Turnover by month (gross)</h2>
              {rows.length === 0 ? <Empty /> : (
                <ResponsiveContainer width="100%" height={260}>
                  <BarChart data={rows} margin={{ top: 8, right: 8, left: 8, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="var(--color-chart-grid)" vertical={false} />
                    <XAxis dataKey="label" tick={{ fontSize: 12, fill: "var(--color-text-secondary)" }} />
                    <YAxis tickFormatter={(v) => compactMoney(v)} tick={{ fontSize: 12, fill: "var(--color-text-secondary)" }} width={60} />
                    <Tooltip formatter={(v) => money(Number(v), d.currency)} />
                    <Bar dataKey="gross" radius={[4, 4, 0, 0]} fill="var(--color-chart-1)" />
                  </BarChart>
                </ResponsiveContainer>
              )}
            </div>
          </div>
        );
      }}
    </QueryState>
  );
}

function ReceivablesTab({ params }: { params: Record<string, string> }) {
  const q = useReport<IssuedReceivablesReport>("receivables", params);
  const STATUS_COLOR: Record<string, string> = {
    paid: "bg-emerald-500", partial: "bg-sky-500", open: "bg-slate-400", overdue: "bg-rose-500",
  };
  return (
    <QueryState query={q} loading={<Loading />} errorTitle="Couldn’t load the receivables report">
      {(d) => {
        const maxAging = Math.max(1, ...d.aging.map((a) => Number(a.outstanding)));
        return (
          <div className="space-y-6">
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
              <Kpi label="Total outstanding" value={money(d.total_outstanding, d.currency)} />
              <Kpi label="Overdue" value={money(d.overdue_outstanding, d.currency)} sub="past due date" />
              <Kpi label="Late interest accrued" value={money(d.penalty_accrued, d.currency)} sub="on marked invoices" />
              <Kpi label="Avg days to pay" value={d.avg_days_to_pay == null ? "—" : `${d.avg_days_to_pay} days`} sub="settled invoices" />
            </div>

            <div className="card overflow-x-auto p-0">
              <table className="w-full text-sm">
                <thead className="border-b border-slate-200 bg-slate-50 text-left text-xs uppercase tracking-wide text-slate-500">
                  <tr>
                    <th className="px-4 py-3">Status</th>
                    <th className="px-4 py-3 text-right">Invoices</th>
                    <th className="px-4 py-3 text-right">Gross</th>
                    <th className="px-4 py-3 text-right">Outstanding</th>
                  </tr>
                </thead>
                <tbody>
                  {d.statuses.map((s) => (
                    <tr key={s.status} className="border-b border-slate-100 last:border-0">
                      <td className="px-4 py-3">
                        <span className="inline-flex items-center gap-2">
                          <span className={`h-2.5 w-2.5 rounded-full ${STATUS_COLOR[s.status]}`} />
                          {s.label}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-right tabular-nums">{s.count}</td>
                      <td className="px-4 py-3 text-right tabular-nums">{money(s.gross, d.currency)}</td>
                      <td className="px-4 py-3 text-right tabular-nums">{money(s.outstanding, d.currency)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="card">
              <h2 className="mb-3 text-sm font-semibold text-slate-600">Aging of outstanding balances</h2>
              <div className="space-y-2">
                {d.aging.map((a) => (
                  <div key={a.label} className="flex items-center gap-3 text-sm">
                    <span className="w-16 shrink-0 text-slate-500">{a.label}</span>
                    <div className="h-4 flex-1 rounded-sm bg-slate-100">
                      <div
                        className="h-4 rounded-sm bg-brand-400"
                        style={{ width: `${(Number(a.outstanding) / maxAging) * 100}%` }}
                      />
                    </div>
                    <span className="w-28 shrink-0 text-right tabular-nums text-slate-600">{money(a.outstanding, d.currency)}</span>
                    <span className="w-10 shrink-0 text-right text-xs text-text-secondary">{a.count}</span>
                  </div>
                ))}
              </div>
              <p className="mt-3 text-xs text-text-secondary">Days past due date. Only invoices with a balance still owed are counted.</p>
            </div>
          </div>
        );
      }}
    </QueryState>
  );
}

function PartnersTab({ params }: { params: Record<string, string> }) {
  const q = useReport<IssuedPartnerReport>("partners", params);
  return (
    <QueryState query={q} loading={<Loading />} errorTitle="Couldn’t load the partners report">
      {(d) =>
        d.partners.length === 0 ? (
          <Empty />
        ) : (
          <div className="card overflow-x-auto p-0">
            <table className="w-full text-sm">
              <thead className="border-b border-slate-200 bg-slate-50 text-left text-xs uppercase tracking-wide text-slate-500">
                <tr>
                  <th className="px-4 py-3">Partner</th>
                  <th className="px-4 py-3 text-right">Invoices</th>
                  <th className="px-4 py-3 text-right">Net turnover</th>
                  <th className="px-4 py-3 text-right">VAT</th>
                  <th className="px-4 py-3 text-right">Gross</th>
                  <th className="px-4 py-3 text-right">Outstanding</th>
                  <th className="px-4 py-3">Last invoice</th>
                </tr>
              </thead>
              <tbody>
                {d.partners.map((p, i) => (
                  <tr key={`${p.partner}-${i}`} className="border-b border-slate-100 last:border-0 hover:bg-slate-50">
                    <td className="px-4 py-3">
                      <div className="font-medium text-slate-700">{p.partner}</div>
                      {p.vat_number && <div className="text-xs text-text-secondary">{p.vat_number}</div>}
                    </td>
                    <td className="px-4 py-3 text-right tabular-nums">{p.count}</td>
                    <td className="px-4 py-3 text-right tabular-nums font-medium">{money(p.net, d.currency)}</td>
                    <td className="px-4 py-3 text-right tabular-nums text-slate-500">{money(p.vat, d.currency)}</td>
                    <td className="px-4 py-3 text-right tabular-nums">{money(p.gross, d.currency)}</td>
                    <td className="px-4 py-3 text-right tabular-nums">
                      {Number(p.outstanding) > 0
                        ? <span className="text-rose-600">{money(p.outstanding, d.currency)}</span>
                        : <span className="text-text-secondary">—</span>}
                    </td>
                    <td className="px-4 py-3 text-slate-500">{p.last_invoice ? shortDate(p.last_invoice) : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      }
    </QueryState>
  );
}

function VatTab({ params, schemeLabels }: { params: Record<string, string>; schemeLabels: Record<string, string> }) {
  const q = useReport<IssuedVatReport>("vat", params);
  return (
    <QueryState query={q} loading={<Loading />} errorTitle="Couldn’t load the output-VAT report">
      {(d) => {
        const rateRows = d.by_rate.map((r, i) => ({ name: `${Number(r.rate)}%`, value: Number(r.vat), i }));
        return (
          <div className="space-y-6">
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <Kpi label="Net (taxable base)" value={money(d.total_net, d.currency)} />
              <Kpi label="Output VAT due" value={money(d.total_vat, d.currency)} sub="on standard-rated supplies" />
            </div>

            <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
              <div className="card overflow-x-auto p-0">
                <div className="px-4 pt-4 text-sm font-semibold text-slate-600">By VAT rate</div>
                <table className="mt-2 w-full text-sm">
                  <thead className="border-b border-slate-200 bg-slate-50 text-left text-xs uppercase tracking-wide text-slate-500">
                    <tr>
                      <th className="px-4 py-3">Rate</th>
                      <th className="px-4 py-3 text-right">Base (net)</th>
                      <th className="px-4 py-3 text-right">VAT</th>
                    </tr>
                  </thead>
                  <tbody>
                    {d.by_rate.map((r) => (
                      <tr key={String(r.rate)} className="border-b border-slate-100 last:border-0">
                        <td className="px-4 py-3">{Number(r.rate)}%</td>
                        <td className="px-4 py-3 text-right tabular-nums">{money(r.base, d.currency)}</td>
                        <td className="px-4 py-3 text-right tabular-nums font-medium">{money(r.vat, d.currency)}</td>
                      </tr>
                    ))}
                    {d.by_rate.length === 0 && <tr><td colSpan={3} className="px-4 py-8 text-center text-text-secondary">No data.</td></tr>}
                  </tbody>
                </table>
              </div>

              <div className="card overflow-x-auto p-0">
                <div className="px-4 pt-4 text-sm font-semibold text-slate-600">By VAT scheme</div>
                <table className="mt-2 w-full text-sm">
                  <thead className="border-b border-slate-200 bg-slate-50 text-left text-xs uppercase tracking-wide text-slate-500">
                    <tr>
                      <th className="px-4 py-3">Scheme</th>
                      <th className="px-4 py-3 text-right">Net</th>
                      <th className="px-4 py-3 text-right">VAT</th>
                    </tr>
                  </thead>
                  <tbody>
                    {d.by_scheme.map((r) => (
                      <tr key={r.scheme} className="border-b border-slate-100 last:border-0">
                        <td className="px-4 py-3">{schemeLabels[r.scheme] ?? r.scheme}</td>
                        <td className="px-4 py-3 text-right tabular-nums">{money(r.net, d.currency)}</td>
                        <td className="px-4 py-3 text-right tabular-nums">{money(r.vat, d.currency)}</td>
                      </tr>
                    ))}
                    {d.by_scheme.length === 0 && <tr><td colSpan={3} className="px-4 py-8 text-center text-text-secondary">No data.</td></tr>}
                  </tbody>
                </table>
              </div>
            </div>

            {rateRows.length > 0 && (
              <div className="card">
                <h2 className="mb-3 text-sm font-semibold text-slate-600">Output VAT by rate</h2>
                <ResponsiveContainer width="100%" height={220}>
                  <BarChart data={rateRows} margin={{ top: 8, right: 8, left: 8, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="var(--color-chart-grid)" vertical={false} />
                    <XAxis dataKey="name" tick={{ fontSize: 12, fill: "var(--color-text-secondary)" }} />
                    <YAxis tickFormatter={(v) => compactMoney(v)} tick={{ fontSize: 12, fill: "var(--color-text-secondary)" }} width={60} />
                    <Tooltip formatter={(v) => money(Number(v), d.currency)} />
                    <Bar dataKey="value" radius={[4, 4, 0, 0]}>
                      {rateRows.map((r) => <Cell key={r.i} fill={PALETTE[r.i % PALETTE.length]} />)}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </div>
            )}
          </div>
        );
      }}
    </QueryState>
  );
}

function Loading() {
  return <div className="card text-sm text-text-secondary">Loading…</div>;
}
function Empty() {
  return <div className="py-8 text-center text-sm text-text-secondary">No invoices in this range yet.</div>;
}
function Gate({ children }: { children: React.ReactNode }) {
  return (
    <div className="space-y-4">
      <PageHeader title="Invoice reports" />
      <div className="card text-sm text-slate-600">{children}</div>
    </div>
  );
}
