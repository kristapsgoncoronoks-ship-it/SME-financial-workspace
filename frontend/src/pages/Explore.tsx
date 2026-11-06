import { useQuery } from "@tanstack/react-query";
import { useEffect, useId, useMemo, useRef, useState } from "react";
import {
  Bar, BarChart, CartesianGrid, Cell, Line, LineChart, Pie, PieChart,
  ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import { api, apiError, downloadFile, isCanceledRequest, tokenStore } from "../lib/api";
import { CHART_PALETTE as PALETTE, compactMoney, formatNumber, money } from "../lib/format";
import type { ExploreCatalog, ExploreResult } from "../lib/types";
import { ErrorState } from "../components/ui";

type ChartType = "bar" | "line" | "pie" | "stacked" | "table";

export default function Explore() {
  const [measure, setMeasure] = useState("net");
  const [dim0, setDim0] = useState("category");
  const [dim1, setDim1] = useState("");
  const [chart, setChart] = useState<ChartType>("bar");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [status, setStatus] = useState("");
  const [limit, setLimit] = useState("100");
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);
  const exportPending = useRef(false);
  const mounted = useRef(false);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);

  const fields = useQuery<ExploreCatalog>({ queryKey: ["explore", "fields"], queryFn: async () => (await api.get("/analytics/fields")).data });

  const params = useMemo(() => {
    const p = new URLSearchParams({ measure, dim: dim0, limit });
    if (dim1) p.append("dim", dim1);
    if (start) p.set("start", start);
    if (end) p.set("end", end);
    if (status) p.set("status", status);
    return p;
  }, [measure, dim0, dim1, start, end, status, limit]);

  const result = useQuery<ExploreResult>({
    queryKey: ["explore", params.toString()],
    queryFn: async ({ signal }) => (await api.get(`/analytics/explore?${params.toString()}`, { signal })).data,
    enabled: !!fields.data,
  });

  const unit = result.data?.measure.unit ?? "money";
  const fmt = (v: number) => (unit === "money" ? money(v) : unit === "count" ? String(Math.round(v)) : formatNumber(v, 2));
  const fmtAxis = (v: number) => (unit === "money" ? compactMoney(v) : String(v));
  const dim0Temporal = fields.data?.dimensions.find((d) => d.key === dim0)?.temporal;
  const ready = !!result.data && !fields.isError && !result.isError && !result.isFetching;
  const canExport = ready && result.data!.rows.length > 0 && !exporting;

  async function exportResult(format: "csv" | "xlsx" | "pdf") {
    if (!canExport || exportPending.current) return;
    exportPending.current = true;
    const generation = tokenStore.generation();
    const token = tokenStore.get();
    const fresh = () => mounted.current && generation === tokenStore.generation() && token === tokenStore.get();
    setExporting(true);
    setExportError(null);
    try {
      await downloadFile(`/analytics/explore?${params.toString()}&format=${format}`, `explore-up-to-${limit}-groups.${format}`);
    } catch (error) {
      if (fresh() && !isCanceledRequest(error)) setExportError(apiError(error));
    } finally {
      exportPending.current = false;
      if (fresh()) setExporting(false);
    }
  }

  return (
    <div className="min-w-0 space-y-5">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Explore</h1>
        <p className="text-sm text-slate-500">Self-service analytics — pick a measure and dimensions, slice with filters, visualize. Aggregated in the database.</p>
      </div>

      {fields.isError && (
        <ErrorState title="Couldn’t load the field catalogue" onRetry={() => fields.refetch()} />
      )}

      {/* Field pickers */}
      <fieldset disabled={exporting} className="card grid min-w-0 grid-cols-1 gap-3 sm:grid-cols-2 md:grid-cols-4 lg:grid-cols-6">
        <Picker label="Measure" value={measure} onChange={setMeasure} options={(fields.data?.measures ?? []).map((m) => [m.key, m.label])} />
        <Picker label="Dimension" value={dim0} onChange={setDim0} options={(fields.data?.dimensions ?? []).map((d) => [d.key, d.label])} />
        <Picker label="Break by" value={dim1} onChange={setDim1} options={[["", "— none —"], ...(fields.data?.dimensions ?? []).filter((d) => d.key !== dim0).map((d) => [d.key, d.label] as [string, string])]} />
        <Picker label="Chart" value={chart} onChange={(v) => setChart(v as ChartType)} options={[["bar", "Bar"], ["line", "Line"], ["pie", "Pie"], ["stacked", "Stacked"], ["table", "Table"]]} />
        <div>
          <label className="label" htmlFor="from">From</label>
          <input id="from" type="date" className="input" value={start} onChange={(e) => setStart(e.target.value)} />
        </div>
        <div>
          <label className="label" htmlFor="to">To</label>
          <input id="to" type="date" className="input" value={end} onChange={(e) => setEnd(e.target.value)} />
        </div>
      </fieldset>

      <fieldset disabled={exporting} className="flex min-w-0 flex-wrap items-end gap-3">
        <Picker label="Status filter" value={status} onChange={setStatus} options={[["", "All"], ["paid", "paid"], ["pending", "pending"], ["overdue", "overdue"], ["draft", "draft"]]} className="w-40 max-w-full" />
        <Picker label="Group limit" value={limit} onChange={setLimit} options={[["100", "Up to 100 groups"], ["500", "Up to 500 groups"], ["1000", "Up to 1000 groups"]]} />
      </fieldset>
      <div className="space-y-2">
        <p className="text-sm text-text-secondary">Downloads rerun these filters on current data, up to the selected group limit.</p>
        <div className="flex flex-wrap gap-2">
          {([ ["csv", "CSV"], ["xlsx", "Excel"], ["pdf", "PDF"] ] as const).map(([format, label]) => (
            <button key={format} type="button" className="btn-ghost max-w-full whitespace-normal text-left" disabled={!canExport} onClick={() => void exportResult(format)}>Download {label} (up to {limit} groups)</button>
          ))}
        </div>
        {exporting && <p role="status" className="text-sm text-text-secondary">Preparing bounded download…</p>}
        {exportError && <p role="alert" className="text-sm text-red-600">{exportError} Try the download again.</p>}
      </div>

      <div className="card">
        <div className="mb-2 text-sm font-semibold text-slate-600">
          {result.data?.measure.label} by {result.data?.dimensions.map((d) => d.label).join(" × ")}
        </div>
        {fields.isError ? (
          // The catalogue failed, so this query never ran. The alert above says
          // so; an eternal "Loading…" here would say something else.
          <p className="py-10 text-center text-sm text-text-secondary">
            Nothing to draw until the field catalogue loads.
          </p>
        ) : result.isError ? (
          <ErrorState title="Couldn’t run this query" onRetry={() => result.refetch()} />
        ) : result.isFetching || !result.data ? (
          <div className="grid h-72 place-items-center text-text-secondary">Loading…</div>
        ) : result.data.rows.length === 0 ? (
          <div className="grid h-72 place-items-center text-text-secondary">No data for this selection.</div>
        ) : (
          <>
            <p role="status" className="mb-3 text-sm text-text-secondary">
              Showing {result.data.returned_rows} groups. {result.data.truncated ? (result.data.limit < 1000 ? "More groups match; narrow the filters or increase the group limit." : "More groups match; narrow the filters to see another bounded result.") : "All matching groups are included in this result."}
            </p>
            {chart !== "table" && <ChartView result={result.data} chart={chart} fmt={fmt} fmtAxis={fmtAxis} temporal={dim0Temporal} />}
          </>
        )}
      </div>

      {ready && result.data!.rows.length > 0 && <DataTable key={params.toString()} result={result.data!} fmt={fmt} />}
    </div>
  );
}

function ChartView({ result, chart, fmt, fmtAxis, temporal }: {
  result: ExploreResult; chart: ChartType; fmt: (v: number) => string; fmtAxis: (v: number) => string; temporal?: boolean;
}) {
  const [d0, d1] = result.dimensions;
  const single = result.dimensions.length === 1;

  // 2-dim → pivot into stacked series
  const { data, series } = useMemo(() => {
    if (single) {
      return { data: result.rows.map((r) => ({ name: r[d0.key], value: Number(r.value) })), series: [] as string[] };
    }
    const seriesSet: string[] = [];
    const byX: Record<string, any> = {};
    for (const r of result.rows) {
      const x = r[d0.key], s = r[d1!.key];
      if (!seriesSet.includes(s)) seriesSet.push(s);
      byX[x] = byX[x] || { name: x };
      byX[x][s] = Number(r.value);
    }
    return { data: Object.values(byX), series: seriesSet };
  }, [result, single, d0, d1]);

  // Temporal single-dimension defaults to a line unless the user picked a chart.
  const singleChart = single && chart === "stacked" ? (temporal ? "line" : "bar") : chart;

  if (single && singleChart === "line") {
    return (
      <ResponsiveContainer width="100%" height={320}>
        <LineChart data={data} margin={{ top: 8, right: 12, left: 8, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#eef2f7" />
          <XAxis dataKey="name" tick={{ fontSize: 12, fill: "#94a3b8" }} />
          <YAxis tickFormatter={fmtAxis} tick={{ fontSize: 12, fill: "#94a3b8" }} width={64} />
          <Tooltip formatter={(v) => fmt(Number(v))} />
          <Line type="monotone" dataKey="value" stroke="#3b6ef2" strokeWidth={2.5} dot={{ r: 2 }} />
        </LineChart>
      </ResponsiveContainer>
    );
  }
  if (single && singleChart === "pie") {
    return (
      <ResponsiveContainer width="100%" height={320}>
        <PieChart>
          <Pie data={data} dataKey="value" nameKey="name" innerRadius={60} outerRadius={110} paddingAngle={2}>
            {data.map((_, i) => <Cell key={i} fill={PALETTE[i % PALETTE.length]} />)}
          </Pie>
          <Tooltip formatter={(v) => fmt(Number(v))} />
        </PieChart>
      </ResponsiveContainer>
    );
  }
  if (single) {
    return (
      <ResponsiveContainer width="100%" height={320}>
        <BarChart data={data} margin={{ top: 8, right: 12, left: 8, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#eef2f7" />
          <XAxis dataKey="name" tick={{ fontSize: 12, fill: "#94a3b8" }} />
          <YAxis tickFormatter={fmtAxis} tick={{ fontSize: 12, fill: "#94a3b8" }} width={64} />
          <Tooltip formatter={(v) => fmt(Number(v))} />
          <Bar dataKey="value" radius={[4, 4, 0, 0]}>
            {data.map((_, i) => <Cell key={i} fill={PALETTE[i % PALETTE.length]} />)}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    );
  }
  // two dimensions → stacked bar
  return (
    <>
    <ResponsiveContainer width="100%" height={340}>
      <BarChart data={data} margin={{ top: 8, right: 12, left: 8, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#eef2f7" />
        <XAxis dataKey="name" tick={{ fontSize: 12, fill: "#94a3b8" }} />
        <YAxis tickFormatter={fmtAxis} tick={{ fontSize: 12, fill: "#94a3b8" }} width={64} />
        <Tooltip formatter={(v) => fmt(Number(v))} />
        {series.map((s, i) => (
          <Bar key={s} dataKey={s} stackId="a" fill={PALETTE[i % PALETTE.length]} />
        ))}
      </BarChart>
    </ResponsiveContainer>
    <div role="region" aria-label="Chart series" tabIndex={0} className="max-h-48 overflow-y-auto rounded focus-visible:outline-2 focus-visible:outline-primary">
      <ul className="flex flex-wrap gap-x-4 gap-y-2 text-xs text-text-secondary">
        {series.map((name, index) => <li key={name} className="flex min-w-0 items-start gap-2"><span aria-hidden="true" className="mt-1 h-2 w-2 shrink-0" style={{ backgroundColor: PALETTE[index % PALETTE.length] }} /><span className="break-all">{name}</span></li>)}
      </ul>
    </div>
    </>
  );
}

function DataTable({ result, fmt }: { result: ExploreResult; fmt: (v: number) => string }) {
  const [page, setPage] = useState(0);
  const pageSize = 100;
  const lastPage = Math.max(0, Math.ceil(result.rows.length / pageSize) - 1);
  const currentPage = Math.min(page, lastPage);
  const first = currentPage * pageSize;
  return (
    <div className="min-w-0 space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3" aria-label="Result group pages">
        <p className="text-sm text-text-secondary">Groups {first + 1}–{Math.min(first + pageSize, result.rows.length)} of {result.rows.length} returned</p>
        <div className="flex flex-wrap gap-2">
          <button type="button" className="btn-ghost" disabled={currentPage === 0} onClick={() => setPage(currentPage - 1)}>Previous groups</button>
          <button type="button" className="btn-ghost" disabled={currentPage === lastPage} onClick={() => setPage(currentPage + 1)}>Next groups</button>
        </div>
      </div>
      <div role="region" aria-label="Explore result table" tabIndex={0} className="card min-w-0 overflow-x-auto p-0 focus-visible:outline-2 focus-visible:outline-primary">
      <table className="w-full text-sm">
        <thead className="border-b border-slate-200 bg-slate-50 text-left text-xs uppercase tracking-wide text-slate-500">
          <tr>
            {result.dimensions.map((d) => <th key={d.key} className="px-4 py-3">{d.label}</th>)}
            <th className="px-4 py-3 text-right">{result.measure.label}</th>
          </tr>
        </thead>
        <tbody>
          {result.rows.slice(first, first + pageSize).map((r, i) => (
            <tr key={i} className="border-b border-slate-100 last:border-0 hover:bg-slate-50">
              {result.dimensions.map((d) => <td key={d.key} className="px-4 py-2">{r[d.key]}</td>)}
              <td className="px-4 py-2 text-right font-medium">{fmt(Number(r.value))}</td>
            </tr>
          ))}
        </tbody>
      </table>
      </div>
    </div>
  );
}

function Picker({ label, value, onChange, options, className }: {
  label: string; value: string; onChange: (v: string) => void; options: [string, string][]; className?: string;
}) {
  const id = useId();
  return (
    <div className={`min-w-0 ${className ?? ""}`}>
      <label className="label" htmlFor={id}>{label}</label>
      <select id={id} className="input" value={value} onChange={(e) => onChange(e.target.value)}>
        {options.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
      </select>
    </div>
  );
}
