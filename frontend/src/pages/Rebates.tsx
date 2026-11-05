import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useLocation, useSearchParams } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { ModuleInactive } from "../components/ModuleGate";
import { RefusalNotice } from "../components/RefusalNotice";
import {
  Badge,
  Button,
  Card,
  DateInput,
  EmptyState,
  ErrorState,
  PageHeader,
  QueryState,
  Skeleton,
  TextInput,
} from "../components/ui";
import { api, apiError, apiErrorCode } from "../lib/api";
import { decimalMoney, shortDate } from "../lib/format";
import { isDecimalShape, isPeriodShape } from "../lib/transportRecovery";
import { useModules } from "../lib/useModules";
import type { OffInvoiceRebate } from "../lib/types";

/**
 * The off-invoice rebate registry (WO-86) over the WO-84 routes.
 *
 * Some suppliers invoice at LIST price per country and pay the agreed discount
 * back on a SEPARATE rebate invoice, issued by a different party. No fuel-card
 * statement parser ever sees that second document, so unless it is recorded
 * here the platform prices those litres at list — and a supplier-overcharge
 * demand built on a list price asks for money the supplier has already paid.
 * Recording the document is the only way a rebate euro is ever allowed to reach
 * the effective net price.
 *
 * TWO BOUNDARIES THIS SCREEN MUST STATE PLAINLY, BECAUSE BOTH ARE SURPRISING:
 *
 * 1. **Recording a rebate changes no figure until the close runs.** The
 *    recompute of a transaction's effective net is a CLOSE stage, not a web
 *    request: an already-validated transaction's derived figure is product data
 *    the engine owns. There is therefore no "merge now" verb on this route and
 *    none is invented here. A read-only preview uses the engine's canonical
 *    planner; only the monthly close can write the proposed figures.
 *
 * 2. **A rebate that cannot reach EUR is refused, not stored.** The form has no
 *    EUR field at all: `RebateIn` carries none, and the server resolves the euro
 *    through the ECB rate at the DOCUMENT's own date (§4.10). With no rate for
 *    that date the whole POST refuses `fx_rate_unavailable` and writes nothing —
 *    never a guessed number, and never a half-recorded row (§4.15). The refusal
 *    copy says exactly that, because "it failed" and "it silently stored
 *    something approximate" are the two readings an operator must be able to
 *    tell apart.
 *
 * Permissions are mirrored, not enforced: the list is `transport.read`, the
 * recording verb is `vat.write` — exactly how `rebates.py` declares them. A
 * hidden form is never a security boundary; the server is the control.
 *
 * No money arithmetic: `amount_local` is posted as the typed string and
 * `amount_eur` is rendered as the server resolved it.
 */

const EMPTY = {
  supplier: "",
  country: "",
  period: "",
  source_ref: "",
  source_party: "",
  rebate_date: "",
  currency: "EUR",
  amount_local: "",
  note: "",
};

/** The FX provenance of a stored rebate. There is deliberately no "unknown"
 * branch: a rebate that could not be converted was refused rather than stored,
 * so an unknown provenance cannot exist in this table. */
const FX_SOURCE_COPY: Record<string, string> = {
  eur: "Already EUR — no conversion, no rate involved.",
  ecb: "Converted at the ECB reference rate for the document's own date.",
};

export default function RebatesPage() {
  const qc = useQueryClient();
  const { hasPerm } = useAuth();
  const modules = useModules();
  const canWrite = hasPerm("vat.write"); // cosmetic — the server enforces it
  const [form, setForm] = useState(EMPTY);
  const [refusal, setRefusal] = useState<{
    code: string | null;
    detail: string;
  } | null>(null);
  const [recorded, setRecorded] = useState<OffInvoiceRebate | null>(null);

  const enabled = modules.isEnabled("transport");

  const rebates = useQuery<OffInvoiceRebate[]>({
    queryKey: ["transport", "rebates"],
    queryFn: async () => (await api.get("/transport/rebates")).data,
    enabled,
    retry: false,
  });

  const record = useMutation({
    mutationFn: async () =>
      (
        await api.post("/transport/rebates", {
          supplier: form.supplier.trim(),
          country: form.country.trim().toUpperCase(),
          period: form.period.trim(),
          source_ref: form.source_ref.trim(),
          source_party: form.source_party.trim(),
          rebate_date: form.rebate_date,
          currency: form.currency.trim().toUpperCase(),
          amount_local: form.amount_local.trim(),
          ...(form.note.trim() ? { note: form.note.trim() } : {}),
        })
      ).data as OffInvoiceRebate,
    onSuccess: (row) => {
      setRefusal(null);
      setRecorded(row);
      setForm({ ...EMPTY });
      qc.invalidateQueries({ queryKey: ["transport", "rebates"] });
    },
    onError: (e) => {
      setRecorded(null);
      setRefusal({ code: apiErrorCode(e), detail: apiError(e) });
    },
  });

  if (modules.isLoading) return <Skeleton className="h-24 w-full" />;
  if (!enabled) {
    return (
      <div className="space-y-6">
        <PageHeader title="Off-invoice rebates" />
        <ModuleInactive name={"Transport & VAT refunds"} />
      </div>
    );
  }

  const formReady =
    form.supplier.trim() !== "" &&
    form.country.trim().length === 2 &&
    isPeriodShape(form.period) &&
    form.source_ref.trim() !== "" &&
    form.source_party.trim() !== "" &&
    form.rebate_date !== "" &&
    form.currency.trim().length === 3 &&
    isDecimalShape(form.amount_local);

  return (
    <div className="space-y-6">
      <PageHeader
        title="Off-invoice rebates"
        description="The second discount tier: a rebate paid back on its own document rather than deducted on the fuel invoice. Recording it is what lets the platform price those litres at the real effective net."
      />

      {refusal && (
        <RefusalNotice
          code={refusal.code}
          detail={refusal.detail}
          onDismiss={() => setRefusal(null)}
          periodShape="month"
        />
      )}

      {recorded && (
        <div
          role="status"
          className="rounded-lg border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-900"
        >
          <p className="font-semibold">
            Recorded {recorded.source_ref} —{" "}
            {decimalMoney(recorded.amount_eur, "EUR")}
          </p>
          <p className="mt-1 text-emerald-800">
            No transaction figure has changed yet. The effective net price is
            recomputed by the monthly close, so this rebate applies at the next
            close for {recorded.period}.
          </p>
        </div>
      )}

      {/* The close owns writes; preview and recording keep that boundary. */}
      <div className="rounded-lg border border-slate-200 bg-slate-50 px-4 py-3 text-sm text-slate-600">
        <p className="font-medium text-slate-700">
          Recording a rebate does not change any figure until the close runs
        </p>
        <p className="mt-1">
          This registry stores the rebate document. The effective net price on
          the fuel transactions it adjusts is recomputed by the monthly close,
          which is the only thing allowed to write a validated transaction's
          derived figure. The read-only preview below shows the current
          calculation without applying it. Reports continue to use stored
          effective net amounts until the close updates them;{" "}
          <Link to="/overcharges" className="font-medium underline">
            supplier overcharges
          </Link>{" "}
          will say so.
        </p>
      </div>

      {hasPerm("transport.read") && <RebatePreview />}

      {canWrite && (
        <Card title="Record a rebate document">
          <div className="grid gap-4 sm:grid-cols-3 lg:grid-cols-4">
            <TextInput
              label="Supplier"
              hint="The fuel-card or toll-network code"
              required
              value={form.supplier}
              onChange={(e) => setForm({ ...form, supplier: e.target.value })}
            />
            <TextInput
              label="Country"
              hint="ISO code, e.g. LV"
              required
              maxLength={2}
              value={form.country}
              onChange={(e) => setForm({ ...form, country: e.target.value })}
            />
            <TextInput
              label="Period adjusted"
              hint="YYYY-MM — the fuel month, not the document's month"
              required
              value={form.period}
              onChange={(e) => setForm({ ...form, period: e.target.value })}
              placeholder="2026-05"
            />
            <DateInput
              label="Document date"
              hint="The rebate document's own date — what the FX rate is taken at"
              required
              value={form.rebate_date}
              onValueChange={(v) => setForm({ ...form, rebate_date: v })}
            />
            <TextInput
              label="Document number"
              required
              value={form.source_ref}
              onChange={(e) => setForm({ ...form, source_ref: e.target.value })}
            />
            <TextInput
              label="Issued by"
              hint="The rebate partner, often not the fuel supplier"
              required
              value={form.source_party}
              onChange={(e) =>
                setForm({ ...form, source_party: e.target.value })
              }
            />
            <TextInput
              label="Currency"
              hint="Three-letter ISO code"
              required
              maxLength={3}
              value={form.currency}
              onChange={(e) => setForm({ ...form, currency: e.target.value })}
            />
            <TextInput
              label="Amount on the document"
              hint="In the currency above"
              required
              value={form.amount_local}
              onChange={(e) =>
                setForm({ ...form, amount_local: e.target.value })
              }
              placeholder="0.00"
            />
            <TextInput
              label="Note"
              hint="Optional"
              value={form.note}
              onChange={(e) => setForm({ ...form, note: e.target.value })}
              className="sm:col-span-2"
            />
            <div className="flex items-end">
              <Button
                onClick={() => record.mutate()}
                loading={record.isPending}
                disabled={!formReady}
              >
                Record rebate
              </Button>
            </div>
          </div>
          <p className="mt-3 text-xs text-text-secondary">
            There is no euro field: the amount is stated in the currency the
            document was issued in, and the server converts it at the ECB rate
            for the document's own date. If no rate exists for that date the
            rebate is refused and nothing is recorded — a rebate is never stored
            at a guessed rate. Re-posting the same document number for the same
            supplier, country and period corrects that document in place.
          </p>
        </Card>
      )}

      <Card title="Recorded rebate documents" padded={false}>
        <QueryState
          query={rebates}
          loading={<Skeleton className="m-5 h-24 w-full" />}
          isEmpty={(rows) => rows.length === 0}
          empty={
            <EmptyState
              title="No rebate documents recorded"
              description={
                canWrite
                  ? "Until a rebate is recorded, the litres it covers are priced at list — and any overcharge demand built on them could ask for money already paid."
                  : "Nobody has recorded an off-invoice rebate for this workspace yet."
              }
            />
          }
          errorTitle="Couldn’t load the recorded rebates"
        >
          {(rows) => (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <caption className="sr-only">
                  Recorded off-invoice rebate documents
                </caption>
                <thead>
                  <tr className="text-left text-xs text-text-secondary">
                    <th className="px-5 py-2">Supplier</th>
                    <th className="px-5 py-2">Country</th>
                    <th className="px-5 py-2">Period adjusted</th>
                    <th className="px-5 py-2">Document</th>
                    <th className="px-5 py-2">Issued by</th>
                    <th className="px-5 py-2">Document date</th>
                    <th className="px-5 py-2 text-right">On the document</th>
                    <th className="px-5 py-2 text-right">In EUR</th>
                    <th className="px-5 py-2">Rate used</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.id} className="border-t border-slate-100">
                      <td className="px-5 py-2">{r.supplier}</td>
                      <td className="px-5 py-2 font-mono text-xs">
                        {r.country}
                      </td>
                      <td className="px-5 py-2 font-mono text-xs">
                        {r.period}
                      </td>
                      <td className="px-5 py-2 font-mono text-xs">
                        {r.source_ref}
                      </td>
                      <td className="px-5 py-2 text-xs text-slate-500">
                        {r.source_party}
                      </td>
                      <td className="px-5 py-2 text-xs text-slate-500">
                        {shortDate(r.rebate_date)}
                      </td>
                      <td className="px-5 py-2 text-right tabular-nums">
                        {decimalMoney(r.amount_local, r.currency)}
                      </td>
                      <td className="px-5 py-2 text-right font-medium tabular-nums">
                        {decimalMoney(r.amount_eur, "EUR")}
                      </td>
                      <td className="px-5 py-2">
                        {r.fx_source ? (
                          <>
                            <Badge
                              tone={r.fx_source === "eur" ? "neutral" : "info"}
                            >
                              {r.fx_source}
                            </Badge>
                            <span className="mt-0.5 block text-xs text-text-secondary">
                              {FX_SOURCE_COPY[r.fx_source] ?? ""}
                              {r.fx_ecb_rate && r.fx_ecb_date
                                ? ` ${r.fx_ecb_rate} on ${r.fx_ecb_date}.`
                                : ""}
                            </span>
                          </>
                        ) : (
                          <span className="text-text-secondary">—</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </QueryState>
        <p className="border-t border-slate-100 px-5 py-3 text-xs text-text-secondary">
          The document amount and its EUR value are shown side by side and are
          never added together — they are one figure in two currencies, not two
          figures.
        </p>
      </Card>
    </div>
  );
}

const PREVIEW_PAGE_SIZE = 100;
interface PreviewAllocation {
  fuel_transaction_id: string;
  supplier: string;
  country: string;
  qty: string;
  net_eur: string;
  share_eur: string;
  old_net_eur_eff: string;
  new_net_eur_eff: string;
}
interface PreviewResult {
  period: string;
  supplier: string | null;
  rebate_total_eur: string;
  lines_allocated: number;
  changed: number;
  source_warnings: string[];
  allocations: PreviewAllocation[];
  page: number;
  page_size: number;
}
function previewPage(raw: string | null): number {
  // Parse an integer UI page only; every monetary value stays a server string.
  const value = raw && /^[1-9]\d*$/.test(raw) ? parseInt(raw, 10) : 1;
  return Number.isSafeInteger(value) ? value : 1;
}
function previewPageCount(count: number): number {
  if (count <= PREVIEW_PAGE_SIZE) return 1;
  const remainder = count % PREVIEW_PAGE_SIZE;
  return (count - remainder) / PREVIEW_PAGE_SIZE + (remainder === 0 ? 0 : 1);
}
function previewRowsOnPage(count: number, page: number): number {
  const remaining = count - (page - 1) * PREVIEW_PAGE_SIZE;
  return remaining <= 0 ? 0 : remaining > PREVIEW_PAGE_SIZE ? PREVIEW_PAGE_SIZE : remaining;
}
function previewResult(value: unknown, period: string, supplier: string | null, page: number): PreviewResult {
  const invalid = () => { throw new Error("The preview response did not match the requested scope. Try the preview again; no transaction figure was changed."); };
  if (!value || typeof value !== "object") return invalid();
  const result = value as PreviewResult;
  const decimal = (item: unknown) => typeof item === "string" && /^[+-]?\d+(?:\.\d+)?$/.test(item);
  if (result.period !== period || result.supplier !== supplier || result.page !== page || result.page_size !== PREVIEW_PAGE_SIZE ||
    !Number.isSafeInteger(result.lines_allocated) || result.lines_allocated < 0 || !Number.isSafeInteger(result.changed) ||
    result.changed < 0 || result.changed > result.lines_allocated || !decimal(result.rebate_total_eur) ||
    !Array.isArray(result.source_warnings) || result.source_warnings.some((item) => typeof item !== "string") ||
    !Array.isArray(result.allocations) || result.allocations.length > PREVIEW_PAGE_SIZE ||
    result.allocations.length !== previewRowsOnPage(result.lines_allocated, page)) return invalid();
  for (const row of result.allocations) {
    if (!row || typeof row !== "object" || typeof row.fuel_transaction_id !== "string" || typeof row.supplier !== "string" ||
      typeof row.country !== "string" || (supplier !== null && row.supplier !== supplier) ||
      ![row.qty, row.net_eur, row.share_eur, row.old_net_eur_eff, row.new_net_eur_eff].every(decimal)) return invalid();
  }
  if (new Set(result.allocations.map((row) => row.fuel_transaction_id)).size !== result.allocations.length) return invalid();
  return result;
}

function RebatePreview() {
  const { user, org } = useAuth();
  const location = useLocation();
  const [params, setParams] = useSearchParams();
  const period = params.get("preview_period")?.trim() ?? "";
  const supplier = params.get("preview_supplier")?.trim() || null;
  const rawPage = params.get("preview_page");
  const page = previewPage(rawPage);
  const [draftPeriod, setDraftPeriod] = useState(period);
  const [draftSupplier, setDraftSupplier] = useState(supplier ?? "");
  const [warningPage, setWarningPage] = useState(1);
  useEffect(() => { setDraftPeriod(period); setDraftSupplier(supplier ?? ""); setWarningPage(1); }, [period, supplier]);
  const validScope = isPeriodShape(period) && (supplier?.length ?? 0) <= 60;
  const validDraft = isPeriodShape(draftPeriod) && draftSupplier.trim().length <= 60;
  const dirty = draftPeriod.trim() !== period || (draftSupplier.trim() || null) !== supplier;
  const query = useQuery<PreviewResult>({
    queryKey: ["transport", "rebates", "preview", user?.id, org?.id, period, supplier, page, PREVIEW_PAGE_SIZE],
    enabled: !!user?.id && !!org?.id && validScope && !dirty,
    queryFn: async ({ signal }) => {
      const search = new URLSearchParams({ period });
      if (supplier !== null) search.set("supplier", supplier);
      search.set("page", String(page)); search.set("page_size", String(PREVIEW_PAGE_SIZE));
      const response = await api.get(`/transport/rebates/preview?${search}`, { signal, timeout: 15_000 });
      return previewResult(response.data, period, supplier, page);
    },
    staleTime: 0,
    retry: false,
    meta: { errorHandled: true },
  });
  const lastPage = query.data ? previewPageCount(query.data.lines_allocated) : null;
  const clamping = !dirty && query.isSuccess && !query.isFetching && lastPage !== null && page > lastPage;
  useEffect(() => {
    const next = clamping ? lastPage : page;
    if (!validScope || dirty || next === null || (!clamping && (rawPage === null || rawPage === String(page)))) return;
    setParams((current) => {
      if ((current.get("preview_period")?.trim() ?? "") !== period || (current.get("preview_supplier")?.trim() || null) !== supplier || previewPage(current.get("preview_page")) !== page) return current;
      const updated = new URLSearchParams(current); updated.set("preview_page", String(next)); return updated;
    }, { replace: true, state: location.state });
  }, [clamping, lastPage, page, rawPage, period, supplier, dirty, validScope, setParams, location.state]);
  const navigate = (nextPage: number, nextPeriod = period, nextSupplier = supplier) => {
    setWarningPage(1);
    setParams((current) => {
      const updated = new URLSearchParams(current);
      updated.set("preview_period", nextPeriod); updated.set("preview_page", String(nextPage));
      if (nextSupplier) updated.set("preview_supplier", nextSupplier); else updated.delete("preview_supplier");
      return updated;
    }, { state: location.state });
  };
  const result = validScope && !dirty && !query.isFetching && !query.isError && !clamping ? query.data : undefined;
  const warningPages = result ? previewPageCount(result.source_warnings.length) : 1;
  const visibleWarningPage = warningPage > warningPages ? warningPages : warningPage;
  return <Card title="Read-only rebate preview" className="min-w-0">
    <p className="mb-4 text-sm text-slate-600">Preview the monthly close calculation for every country and entity in the selected month and supplier scope. No transaction figure has changed.</p>
    <form aria-label="Rebate preview" className="grid items-end gap-4 sm:grid-cols-2" onSubmit={(event) => {
      event.preventDefault();
      if (!validDraft || (query.isFetching && !dirty)) return;
      if (!dirty && validScope && page === 1) { setWarningPage(1); void query.refetch(); }
      else navigate(1, draftPeriod.trim(), draftSupplier.trim() || null);
    }}>
      <TextInput label="Preview period" hint="YYYY-MM" placeholder="2026-05" required maxLength={7} pattern="\d{4}-(0[1-9]|1[0-2])" value={draftPeriod} onChange={(event) => setDraftPeriod(event.target.value)} />
      <TextInput label="Preview network code (optional)" hint="Exact supplier code; leave blank for all suppliers." maxLength={60} value={draftSupplier} onChange={(event) => setDraftSupplier(event.target.value)} />
      <div className="sm:col-span-2"><Button type="submit" disabled={!validDraft} loading={query.isFetching && !dirty}>Preview rebate effect</Button></div>
    </form>
    {dirty && <p className="mt-4 text-sm text-slate-600" aria-live="polite">Preview inputs changed. Preview again to review this scope.</p>}
    {!dirty && validScope && (query.isFetching || clamping) && <p className="mt-4 text-sm text-slate-600" aria-live="polite">Loading read-only preview…</p>}
    {!dirty && validScope && query.isError && <ErrorState
      title={apiErrorCode(query.error) === "rebate_preview_too_large" ? "Preview scope is too large" : "Preview unavailable"}
      description={<span className="[overflow-wrap:anywhere]">{apiError(query.error)} No transaction figure was changed.</span>}
      onRetry={() => { void query.refetch(); }}
    />}
    {result && <section aria-label="Rebate preview result" className="mt-5 min-w-0 space-y-4">
      <p className="text-sm text-slate-600 [overflow-wrap:anywhere]">{result.period} · {result.supplier ?? "All suppliers"} · All countries and entities</p>
      <dl className="flex flex-wrap gap-x-6 gap-y-2 text-sm">
        <div><dt className="text-slate-500">Recorded rebate total</dt><dd className="font-semibold tabular-nums">{decimalMoney(result.rebate_total_eur, "EUR")}</dd></div>
        <div><dt className="sr-only">Allocated lines</dt><dd>Allocated lines: {result.lines_allocated}</dd></div>
        <div><dt className="sr-only">Would change</dt><dd>Would change: {result.changed}</dd></div>
      </dl>
      <p className="text-xs text-slate-500">Amounts below are net EUR totals per transaction, VAT excluded. Proposed effective net includes the rebate. This preview reflects current records; a later close can see changed records. Only the monthly close writes these figures.</p>
      {result.source_warnings.length > 0 && <div className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
        <h3 className="font-semibold">Missing-source warnings ({result.source_warnings.length})</h3>
        <ul className="mt-2 space-y-2">{result.source_warnings.slice((visibleWarningPage - 1) * PREVIEW_PAGE_SIZE, visibleWarningPage * PREVIEW_PAGE_SIZE).map((warning, index) => <li key={index} className="[overflow-wrap:anywhere]">{warning}</li>)}</ul>
        {warningPages > 1 && <div className="mt-3 flex flex-wrap items-center gap-2">
          <Button size="sm" variant="secondary" disabled={visibleWarningPage === 1} onClick={() => setWarningPage(visibleWarningPage - 1)}>Previous warnings</Button>
          <span>Warnings page {visibleWarningPage} of {warningPages}</span>
          <Button size="sm" variant="secondary" disabled={visibleWarningPage >= warningPages} onClick={() => setWarningPage(visibleWarningPage + 1)}>Next warnings</Button>
        </div>}
      </div>}
      <nav aria-label="Rebate allocation pages" className="flex flex-wrap items-center gap-2 text-sm text-slate-600">
        <span aria-live="polite" aria-atomic="true">Showing {result.allocations.length ? `${(page - 1) * PREVIEW_PAGE_SIZE + 1}–${(page - 1) * PREVIEW_PAGE_SIZE + result.allocations.length}` : "0"} of {result.lines_allocated} allocations</span>
        <Button size="sm" variant="secondary" disabled={page <= 1} onClick={() => navigate(page - 1)}>Previous allocations</Button>
        <span>Page {page} of {lastPage}</span>
        <Button size="sm" variant="secondary" disabled={lastPage === null || page >= lastPage} onClick={() => navigate(page + 1)}>Next allocations</Button>
      </nav>
      {result.allocations.length === 0 ? <p className="text-sm text-slate-600">No fuel lines are allocated in this preview.</p> : <div className="min-w-0 overflow-x-auto">
        <table className="w-full text-sm">
          <caption className="sr-only">Read-only rebate allocations</caption>
          <thead><tr className="text-left text-xs text-slate-500">
            <th className="px-3 py-2">Fuel transaction</th><th className="px-3 py-2">Supplier / country</th><th className="px-3 py-2 text-right">Litres</th><th className="px-3 py-2 text-right">Invoiced net (EUR)</th><th className="px-3 py-2 text-right">Rebate share (EUR)</th><th className="px-3 py-2 text-right">Current effective net (EUR)</th><th className="px-3 py-2 text-right">Proposed effective net (EUR)</th>
          </tr></thead>
          <tbody>{result.allocations.map((row) => <tr key={row.fuel_transaction_id} className="border-t border-slate-100">
            <td className="max-w-48 px-3 py-2 font-mono text-xs [overflow-wrap:anywhere]">{row.fuel_transaction_id}</td>
            <td className="max-w-48 px-3 py-2 [overflow-wrap:anywhere]">{row.supplier} / {row.country}</td>
            <td className="px-3 py-2 text-right tabular-nums">{row.qty}</td>
            {[row.net_eur, row.share_eur, row.old_net_eur_eff, row.new_net_eur_eff].map((amount, index) => <td key={index} className="whitespace-nowrap px-3 py-2 text-right tabular-nums">{decimalMoney(amount, "EUR")}</td>)}
          </tr>)}</tbody>
        </table>
      </div>}
    </section>}
  </Card>;
}
