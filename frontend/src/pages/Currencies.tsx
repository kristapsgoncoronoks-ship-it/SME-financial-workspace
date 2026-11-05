import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useAuth } from "../auth/AuthContext";
import { Badge, Button, Card, EmptyState, Modal, QueryState, Skeleton } from "../components/ui";
import { api, apiError } from "../lib/api";
import type { CurrencyEntry } from "../lib/types";

/** Tenant currency catalog: the currencies the invoice/issuing pickers offer.
 * Archived currencies still resolve for historical documents. */
export default function CurrenciesPage() {
  const qc = useQueryClient();
  const { hasPerm } = useAuth();
  const admin = hasPerm("settings.manage"); // cosmetic — the server enforces SETTINGS_MANAGE
  const [showInactive, setShowInactive] = useState(false);
  const [creating, setCreating] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [form, setForm] = useState({ code: "", name: "", symbol: "", decimal_places: "2" });

  const currencies = useQuery<CurrencyEntry[]>({
    queryKey: ["currencies", showInactive],
    queryFn: async () =>
      (await api.get(`/currencies${showInactive ? "?include_inactive=true" : ""}`)).data,
  });

  const refresh = () => {
    setErr(null);
    qc.invalidateQueries({ queryKey: ["currencies"] });
  };

  const create = useMutation({
    mutationFn: async () =>
      (
        await api.post("/currencies", {
          code: form.code.trim().toUpperCase(),
          name: form.name.trim(),
          symbol: form.symbol.trim() || null,
          decimal_places: Number(form.decimal_places),
        })
      ).data as CurrencyEntry,
    onSuccess: () => {
      setCreating(false);
      setForm({ code: "", name: "", symbol: "", decimal_places: "2" });
      refresh();
    },
    onError: (e) => setErr(apiError(e)),
  });

  const toggle = useMutation({
    mutationFn: async (c: CurrencyEntry) =>
      (await api.patch(`/currencies/${c.id}`, { active: !c.active, version: c.version })).data,
    onSuccess: refresh,
    onError: (e) => {
      setErr(apiError(e));
      // A 409 means our version is stale — refetch so the next click carries the fresh one.
      qc.invalidateQueries({ queryKey: ["currencies"] });
    },
  });

  const input = "rounded-lg border border-slate-300 px-2 py-1 text-sm w-full";
  return (
    <div className="min-w-0 space-y-6 [overflow-wrap:anywhere]">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1 basis-64">
          <h1 className="text-xl font-semibold">Currencies</h1>
          <p className="text-slate-500">
            The currencies this workspace invoices in. Amount handling stays exact — decimal
            places here only control display and rounding granularity.
          </p>
        </div>
        {admin && <Button onClick={() => setCreating(true)}>New currency</Button>}
      </div>

      {err && (
        <div role="alert" className="rounded-lg border border-rose-200 bg-rose-50 px-4 py-2 text-sm text-rose-700">
          {err}
        </div>
      )}

      <label className="flex items-center gap-2 text-sm text-slate-500">
        <input
          type="checkbox"
          checked={showInactive}
          onChange={(e) => setShowInactive(e.target.checked)}
        />
        Show archived currencies
      </label>

      <Card className="min-w-0">
        <QueryState
          query={currencies}
          loading={<Skeleton className="h-24 w-full" />}
          isEmpty={(rows) => rows.length === 0}
          empty={
            <EmptyState
              title="No currencies yet"
              description={
                admin
                  ? "Add the currencies your invoices use — they feed every currency picker."
                  : "An administrator hasn't configured any currencies yet."
              }
            />
          }
          errorTitle="Couldn’t load currencies"
        >
          {(rows) => (
            <div role="region" aria-label="Workspace currencies" tabIndex={0} className="relative max-w-full overflow-x-auto"
              onFocusCapture={(event) => {
                if (event.target !== event.currentTarget) event.target.scrollIntoView({ block: "nearest", inline: "nearest" });
              }}>

            <table className="w-full text-sm [overflow-wrap:normal]">
              <caption className="sr-only">Workspace currencies</caption>
              <thead>
                <tr className="text-left text-xs text-text-secondary">
                  <th scope="col" className="py-1">Code</th>
                  <th scope="col" className="py-1">Name</th>
                  <th scope="col" className="py-1">Symbol</th>
                  <th scope="col" className="py-1">Decimals</th>
                  <th scope="col" className="py-1">Status</th>
                  {admin && <th scope="col" className="py-1"><span className="sr-only">Actions</span></th>}
                </tr>
              </thead>
              <tbody>
                {rows.map((c) => (
                  <tr key={c.id} className="border-t border-slate-100">
                    <td className="py-1 font-mono text-xs font-medium">{c.code}</td>
                    <td className="py-1">{c.name}</td>
                    <td className="py-1">{c.symbol ?? "—"}</td>
                    <td className="py-1 tabular-nums">{c.decimal_places}</td>
                    <td className="py-1">
                      {c.active ? (
                        <Badge tone="success">active</Badge>
                      ) : (
                        <Badge tone="neutral">archived</Badge>
                      )}
                    </td>
                    {admin && (
                      <td className="py-1 pr-1 text-right sm:pr-0">
                        <Button
                          size="sm"
                          variant="secondary"
                          className="scroll-mx-1 sm:scroll-mx-0"
                          loading={toggle.isPending}
                          onClick={() => toggle.mutate(c)}
                        >
                          {c.active ? "Archive" : "Restore"}
                        </Button>
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
            </div>
          )}
        </QueryState>
      </Card>

      <Modal open={creating} onClose={() => setCreating(false)} title="New currency">
        <form
          className="space-y-3"
          onSubmit={(e) => {
            e.preventDefault();
            create.mutate();
          }}
        >
          <div className="grid grid-cols-2 gap-3">
            <label className="text-sm">
              ISO code
              <input
                className={input}
                required
                minLength={3}
                maxLength={3}
                value={form.code}
                onChange={(e) => setForm({ ...form, code: e.target.value.toUpperCase() })}
                placeholder="EUR"
              />
            </label>
            <label className="text-sm">
              Symbol (optional)
              <input
                className={input}
                maxLength={8}
                value={form.symbol}
                onChange={(e) => setForm({ ...form, symbol: e.target.value })}
                placeholder="€"
              />
            </label>
          </div>
          <label className="block text-sm">
            Name
            <input
              className={input}
              required
              maxLength={60}
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              placeholder="Euro"
            />
          </label>
          <label className="block text-sm">
            Decimal places
            <select
              className={input}
              value={form.decimal_places}
              onChange={(e) => setForm({ ...form, decimal_places: e.target.value })}
            >
              {["0", "1", "2", "3", "4"].map((d) => (
                <option key={d} value={d}>
                  {d}
                </option>
              ))}
            </select>
          </label>
          <div className="flex flex-wrap justify-end gap-2 pt-2">
            <Button type="button" variant="secondary" onClick={() => setCreating(false)}>
              Cancel
            </Button>
            <Button type="submit" loading={create.isPending}>
              Create currency
            </Button>
          </div>
        </form>
      </Modal>
    </div>
  );
}
