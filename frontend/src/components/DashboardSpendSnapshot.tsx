import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { CategoryPie, SpendChart, VendorBar } from "./Charts";
import { QueryState, Skeleton } from "./ui";
import { api } from "../lib/api";
import { CHART_PALETTE as PALETTE, money } from "../lib/format";
import { DIMENSION_LABELS, type ByCategoryOut, type DimensionBreakdown, type Dimensions, type SpendOverTimeOut, type TopVendorsOut } from "../lib/types";

function useAnalytics<T>(path: string, key: string) {
  return useQuery<T>({
    queryKey: ["analytics", key],
    queryFn: async ({ signal }) => (await api.get(`/analytics/${path}`, { signal })).data,
    meta: { errorHandled: true },
  });
}

export default function DashboardSpendSnapshot() {
  const sot = useAnalytics<SpendOverTimeOut>("spend-over-time", "sot");
  const vendors = useAnalytics<TopVendorsOut>("top-vendors?limit=8", "vendors");
  const categories = useAnalytics<ByCategoryOut>("by-category", "categories");
  // C1.7/WO-24: each envelope names the single currency its `rows` are
  // scoped to (the AR-reports pattern) — pass it into money()/the charts
  // instead of relying on their EUR default, which used to silently mislabel
  // a non-EUR tenant's spend. Optional-chained throughout: defensive against
  // a still-loading/empty response, not just the happy path.
  const sotRows = sot.data?.rows ?? [];
  const sotCurrency = sot.data?.currency;
  const vendorRows = vendors.data?.rows ?? [];
  const vendorCurrency = vendors.data?.currency;
  const catRows = categories.data?.rows ?? [];
  const catCurrency = categories.data?.currency ?? "EUR";

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <div className="card">
          <h3 className="mb-3 text-sm font-semibold text-slate-600">
            Spend over time {sotCurrency && <span className="text-text-secondary">· {sotCurrency}</span>}
          </h3>
          <QueryState query={sot} loading={<Skeleton className="h-[220px] w-full" />} errorTitle="Couldn’t load spend over time">{() => sotRows.length > 0 ? (
            <SpendChart data={sotRows} currency={sotCurrency} />
          ) : (
            <Empty />
          )}</QueryState>
        </div>
        <div className="card">
          <h3 className="mb-3 text-sm font-semibold text-slate-600">
            Top vendors{" "}
            {vendorCurrency && <span className="text-text-secondary">· {vendorCurrency}</span>}
          </h3>
          <QueryState query={vendors} loading={<Skeleton className="h-[220px] w-full" />} errorTitle="Couldn’t load top vendors">{() => vendorRows.length > 0 ? (
            <VendorBar data={vendorRows} currency={vendorCurrency} />
          ) : (
            <Empty />
          )}</QueryState>
        </div>
      </div>
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <div className="card">
          <h3 className="mb-3 text-sm font-semibold text-slate-600">
            Spend by category{" "}
            {categories.data && <span className="text-text-secondary">· {catCurrency}</span>}
          </h3>
          <QueryState query={categories} loading={<Skeleton className="h-[220px] w-full" />} errorTitle="Couldn’t load spend by category">{() => catRows.length > 0 ? (
            <div className="flex items-center gap-4">
              <div className="flex-1">
                <CategoryPie data={catRows} currency={catCurrency} />
              </div>
              <ul className="w-40 space-y-1 text-sm">
                {catRows.slice(0, 8).map((c, i) => (
                  <li key={c.category} className="flex items-center gap-2">
                    <span
                      className="h-2.5 w-2.5 rounded-full"
                      style={{ background: PALETTE[i % PALETTE.length] }}
                    />
                    <span className="flex-1 truncate text-slate-600">{c.category}</span>
                    <span className="text-text-secondary">{money(c.total, catCurrency)}</span>
                  </li>
                ))}
              </ul>
            </div>
          ) : (
            <Empty />
          )}</QueryState>
        </div>
        <SpendByDimension />
      </div>
    </div>
  );
}

// Spend grouped by a cost-allocation dimension (cost center, vehicle, …). A
// simple selector switches dimension; bars are proportional to the top value.
function SpendByDimension() {
  const dims = Object.keys(DIMENSION_LABELS) as (keyof Dimensions)[];
  const [dim, setDim] = useState<keyof Dimensions>("cost_center");
  const q = useQuery<DimensionBreakdown>({
    queryKey: ["analytics", "by-dimension", dim],
    queryFn: async ({ signal }) => (await api.get(`/analytics/by-dimension?dimension=${dim}`, { signal })).data,
    meta: { errorHandled: true },
  });
  const rows = q.data?.rows ?? [];
  const currency = q.data?.currency ?? "EUR";
  const max = rows.reduce((m, r) => Math.max(m, Number(r.total)), 0) || 1;

  return (
    <div className="card">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-sm font-semibold text-slate-600">
          Spend by dimension {q.data && <span className="text-text-secondary">· {currency}</span>}
        </h3>
        <select aria-label="Spend by dimension"
          className="input w-40 py-1"
          value={dim}
          onChange={(e) => setDim(e.target.value as keyof Dimensions)}
        >
          {dims.map((dKey) => (
            <option key={dKey} value={dKey}>
              {DIMENSION_LABELS[dKey]}
            </option>
          ))}
        </select>
      </div>
      <QueryState query={q} loading={<Skeleton className="h-[220px] w-full" />} errorTitle="Couldn’t load spend by dimension">{() => rows.length > 0 ? (
        <ul className="space-y-1.5">
          {rows.map((r) => (
            <li key={r.value} className="flex items-center gap-3 text-sm">
              <span className="w-40 truncate text-slate-600" title={r.value}>
                {r.value}
              </span>
              <span className="relative h-5 flex-1 overflow-hidden rounded-sm bg-slate-100">
                <span
                  className="absolute inset-y-0 left-0 rounded-sm bg-brand-400/70"
                  style={{ width: `${(Number(r.total) / max) * 100}%` }}
                />
              </span>
              <span className="w-24 text-right tabular-nums text-slate-500">
                {money(r.total, currency)}
              </span>
              <span className="w-16 text-right text-xs text-text-secondary">{r.invoice_count} inv</span>
            </li>
          ))}
        </ul>
      ) : (
        <Empty />
      )}</QueryState>
    </div>
  );
}

function Empty() {
  return (
    <div className="grid h-[220px] place-items-center text-sm text-text-secondary">
      No data yet — upload or add an invoice.
    </div>
  );
}
