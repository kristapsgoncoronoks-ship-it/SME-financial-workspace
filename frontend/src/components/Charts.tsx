import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { CHART_PALETTE as PALETTE, compactMoney, money, monthLabel } from "../lib/format";
import type {
  BudgetTrendPoint,
  CashFlowPoint,
  CategorySpend,
  TimeBucket,
  VendorSpend,
} from "../lib/types";

export function CashFlowChart({ data }: { data: CashFlowPoint[] }) {
  const rows = data.map((d) => ({
    label: monthLabel(d.period),
    inflow: Number(d.inflow),
    outflow: Number(d.outflow),
    net: Number(d.net),
  }));
  return (
    <ResponsiveContainer width="100%" height={260}>
      <LineChart data={rows} margin={{ top: 8, right: 8, left: 8, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="var(--color-chart-grid)" />
        <XAxis dataKey="label" tick={{ fontSize: 12, fill: "var(--color-text-secondary)" }} />
        <YAxis tickFormatter={(v) => compactMoney(v)} tick={{ fontSize: 12, fill: "var(--color-text-secondary)" }} width={60} />
        <Tooltip formatter={(v) => money(Number(v))} />
        <Line type="monotone" dataKey="inflow" name="In" stroke="var(--color-chart-2)" strokeWidth={2.5} dot={false} />
        <Line type="monotone" dataKey="outflow" name="Out" stroke="var(--color-chart-4)" strokeWidth={2.5} dot={false} />
        <Line type="monotone" dataKey="net" name="Net" stroke="var(--color-chart-1)" strokeWidth={2.5} strokeDasharray="5 4" dot={{ r: 2 }} />
      </LineChart>
    </ResponsiveContainer>
  );
}

export function SpendChart({ data, currency = "EUR" }: { data: TimeBucket[]; currency?: string }) {
  const rows = data.map((d) => ({ ...d, label: monthLabel(d.period), value: Number(d.total) }));
  return (
    <ResponsiveContainer width="100%" height={260}>
      <LineChart data={rows} margin={{ top: 8, right: 8, left: 8, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="var(--color-chart-grid)" />
        <XAxis dataKey="label" tick={{ fontSize: 12, fill: "var(--color-text-secondary)" }} />
        <YAxis
          tickFormatter={(v) => compactMoney(v, currency)}
          tick={{ fontSize: 12, fill: "var(--color-text-secondary)" }}
          width={60}
        />
        <Tooltip formatter={(v) => money(Number(v), currency)} />
        <Line type="monotone" dataKey="value" stroke="var(--color-chart-1)" strokeWidth={2.5} dot={{ r: 3 }} />
      </LineChart>
    </ResponsiveContainer>
  );
}

export function BudgetTrendChart({ data }: { data: BudgetTrendPoint[] }) {
  const rows = data.map((d) => ({ label: monthLabel(d.month), actual: Number(d.actual), budget: Number(d.budget) }));
  return (
    <ResponsiveContainer width="100%" height={260}>
      <LineChart data={rows} margin={{ top: 8, right: 8, left: 8, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="var(--color-chart-grid)" />
        <XAxis dataKey="label" tick={{ fontSize: 12, fill: "var(--color-text-secondary)" }} />
        <YAxis tickFormatter={(v) => compactMoney(v)} tick={{ fontSize: 12, fill: "var(--color-text-secondary)" }} width={60} />
        <Tooltip formatter={(v) => money(Number(v))} />
        <Line type="monotone" dataKey="actual" name="Actual" stroke="var(--color-chart-1)" strokeWidth={2.5} dot={{ r: 3 }} />
        <Line type="monotone" dataKey="budget" name="Budget" stroke="var(--color-chart-budget)" strokeDasharray="5 4" strokeWidth={2} dot={false} />
      </LineChart>
    </ResponsiveContainer>
  );
}

export function VendorBar({ data, currency = "EUR" }: { data: VendorSpend[]; currency?: string }) {
  const rows = data.map((d) => ({ name: d.vendor_name, value: Number(d.total) }));
  return (
    <ResponsiveContainer width="100%" height={260}>
      <BarChart data={rows} layout="vertical" margin={{ top: 4, right: 16, left: 8, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="var(--color-chart-grid)" horizontal={false} />
        <XAxis
          type="number"
          tickFormatter={(v) => compactMoney(v, currency)}
          tick={{ fontSize: 12, fill: "var(--color-text-secondary)" }}
        />
        <YAxis type="category" dataKey="name" width={90} tick={{ fontSize: 12, fill: "var(--color-text-secondary)" }} />
        <Tooltip formatter={(v) => money(Number(v), currency)} />
        <Bar dataKey="value" radius={[0, 4, 4, 0]}>
          {rows.map((_, i) => (
            <Cell key={i} fill={PALETTE[i % PALETTE.length]} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

export function CategoryPie({
  data,
  currency = "EUR",
}: {
  data: CategorySpend[];
  currency?: string;
}) {
  const rows = data
    .map((d) => ({ name: d.category, value: Number(d.total) }))
    .filter((r) => r.value > 0);
  return (
    <ResponsiveContainer width="100%" height={260}>
      <PieChart>
        <Pie data={rows} dataKey="value" nameKey="name" innerRadius={55} outerRadius={95} paddingAngle={2}>
          {rows.map((_, i) => (
            <Cell key={i} fill={PALETTE[i % PALETTE.length]} />
          ))}
        </Pie>
        <Tooltip formatter={(v) => money(Number(v), currency)} />
      </PieChart>
    </ResponsiveContainer>
  );
}
