import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { useAuth } from "../auth/AuthContext";
import { ReconciliationLineActions } from "../components/ReconciliationLineActions";
import type { ReconciliationScope } from "../lib/reconciliationCommands";
import { Badge, Button, Card, ErrorState, type Tone } from "../components/ui";
import { api, apiError } from "../lib/api";
import { formatNumber, money, shortDate } from "../lib/format";
import type { BankLine, BankStatement } from "../lib/types";

const LINE_TONE: Record<BankLine["status"], Tone> = {
  matched: "success",
  ignored: "neutral",
  unmatched: "warning",
};

export default function ReconciliationPage() {
  const qc = useQueryClient();
  const { user, org, cacheIdentity, hasPerm } = useAuth();
  const canWrite = hasPerm("payment.write");
  const scope = user && org ? { userId: user.id, orgId: org.id } : null;
  const statementsKey = ["reconciliation", user?.id, org?.id, cacheIdentity, "statements"];
  const [err, setErr] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);

  const statements = useQuery<BankStatement[]>({
    queryKey: statementsKey,
    enabled: !!scope,
    queryFn: async ({ signal }) => (await api.get("/reconciliation/statements", { signal })).data,
  });

  const invalidate = async () => {
    setErr(null);
    await qc.invalidateQueries({ queryKey: statementsKey }, { throwOnError: true });
  };
  const onErr = (e: unknown) => setErr(apiError(e));

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Bank reconciliation</h1>
        <p className="text-slate-500">
          Import a bank statement, then match each line to a receipt or a paid expense payout.
        </p>
      </div>

      {err && (
        <div role="alert" className="rounded-lg border border-rose-200 bg-rose-50 px-4 py-2 text-sm text-rose-700">
          {err}
        </div>
      )}

      {!canWrite && <p className="text-sm text-text-secondary">You can review bank lines and their history. Payment write permission is required to import or change reconciliation.</p>}
      <ImportStatement onImported={invalidate} onErr={onErr} canWrite={canWrite} />

      <Card>
        <h2 className="mb-3 text-sm font-semibold text-slate-700">Statements</h2>
        {statements.isError && (
          <ErrorState title="Couldn’t load the statements" onRetry={() => statements.refetch()} />
        )}
        <div className="space-y-2">
          {(statements.data ?? []).map((s) => {
            const done = s.matched + s.ignored;
            const complete = done >= s.line_count && s.line_count > 0;
            return (
              <div key={s.id} className="rounded-lg border border-slate-200 p-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex min-w-0 max-w-full flex-wrap items-center gap-2">
                    <Badge tone={complete ? "success" : "info"}>
                      {done}/{s.line_count} reconciled
                    </Badge>
                    <span className="min-w-0 break-all font-medium">{s.filename}</span>
                    <span className="text-xs text-text-secondary">
                      {shortDate(s.created_at)} · {s.method}
                      {s.unmatched > 0 ? ` · ${s.unmatched} to review` : ""}
                    </span>
                  </div>
                  <Button
                    size="sm"
                    variant="secondary"
                    onClick={() => setSelected(selected === s.id ? null : s.id)}
                  >
                    {selected === s.id ? "Hide lines" : "Reconcile"}
                  </Button>
                </div>
                {selected === s.id && scope && (
                  <StatementLines key={`${cacheIdentity}:${s.id}`} statementId={s.id} scope={scope} canWrite={canWrite} cacheIdentity={cacheIdentity} onChanged={invalidate} />
                )}
              </div>
            );
          })}
          {!statements.isError && (statements.data ?? []).length === 0 && (
            <p className="text-sm text-text-secondary">No statements imported yet.</p>
          )}
        </div>
      </Card>
    </div>
  );
}

function ImportStatement({
  onImported,
  onErr,
  canWrite,
}: {
  onImported: () => Promise<void>;
  onErr: (e: unknown) => void;
  canWrite: boolean;
}) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [warnings, setWarnings] = useState<string[]>([]);

  const upload = useMutation({
    mutationFn: async (file: File) => {
      const form = new FormData();
      form.append("file", file);
      return (await api.post("/reconciliation/import", form)).data as {
        imported: number;
        warnings: string[];
      };
    },
    onSuccess: async (r) => {
      setWarnings(r.warnings ?? []);
      if (fileRef.current) fileRef.current.value = "";
      await onImported();
    },
    onError: onErr,
  });

  return (
    <Card>
      <h2 className="mb-3 text-sm font-semibold text-slate-700">Import a statement</h2>
      <div className="flex flex-wrap items-center gap-3">
        <input aria-label="Statement file"
          ref={fileRef}
          type="file"
          accept=".csv,.pdf"
          className="min-w-0 max-w-full text-sm"
          disabled={upload.isPending || !canWrite}
          onChange={(e) => {
            const f = e.target.files?.[0];
            e.target.value = "";
            if (f) upload.mutate(f);
          }}
        />
        {upload.isPending && <span className="text-sm text-text-secondary">Importing…</span>}
      </div>
      <p className="mt-2 text-xs text-text-secondary">
        CSV (date, description, amount — or debit/credit columns; optional currency code) or a PDF statement.
      </p>
      {warnings.length > 0 && (
        <ul className="mt-2 list-disc pl-5 text-xs text-amber-600">
          {warnings.map((w, i) => (
            <li key={i}>{w}</li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function StatementLines({
  statementId, scope, canWrite, cacheIdentity, onChanged,
}: {
  statementId: string;
  scope: ReconciliationScope;
  canWrite: boolean;
  cacheIdentity: string;
  onChanged: () => Promise<void>;
}) {
  const qc = useQueryClient();
  const linesKey = ["reconciliation", scope.userId, scope.orgId, cacheIdentity, "lines", statementId];
  const lines = useQuery<BankLine[]>({
    queryKey: linesKey,
    queryFn: async ({ signal }) => (await api.get(`/reconciliation/statements/${statementId}/lines`, { signal })).data,
  });
  const readLine = async (line: BankLine) => {
    qc.setQueryData<BankLine[]>(linesKey, (rows) => rows?.map((row) => row.id === line.id ? line : row));
    await onChanged();
    await qc.invalidateQueries({ queryKey: ["reconciliation", scope.userId, scope.orgId, cacheIdentity, "history", line.id] });
  };

  return (
    <div role="region" aria-label="Statement lines" tabIndex={0} className="mt-3 max-w-full overflow-x-auto border-t border-slate-100 pt-3 [container-type:inline-size]">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-xs text-text-secondary">
            <th className="py-1">Date</th>
            <th className="py-1">Description</th>
            <th className="py-1 text-right">Amount</th>
            <th className="px-2 py-1">Status</th>
            <th className="py-1">Review</th>
          </tr>
        </thead>
        <tbody>
          {(lines.data ?? []).map((ln) => {
            const amount = typeof ln.amount === "string" && ln.amount.trim() !== "" && Number.isFinite(Number(ln.amount)) && ln.amount_status !== "invalid" ? ln.amount : null;
            const negative = amount !== null && Number(amount) < 0;
            const currency = ln.currency?.trim().toUpperCase() || null;
            const foreign = currency !== null && currency !== "EUR";
            return (
              <tr key={ln.id} className="border-t border-slate-100 align-top">
                <td className="py-1 text-slate-500">{shortDate(ln.line_date)}</td>
                <td className="py-1">{ln.description || "—"}</td>
                <td
                  className={`py-1 text-right tabular-nums ${amount === null ? "text-amber-800" : negative ? "text-rose-600" : "text-emerald-700"}`}
                >
                  {amount === null ? <>
                    <span>Amount unavailable · {currency || "Currency unknown"}</span>
                    <p className="text-xs text-amber-700">This stored amount cannot be used for matching. Check your original bank statement with your accountant.</p>
                  </> : currency && /^[A-Z]{3}$/.test(currency)
                    ? `${money(amount, currency)} ${currency}`
                    : `${formatNumber(amount, 2)} · Currency unknown`}
                  {ln.balance_status === "invalid" && <p className="text-xs text-amber-700">Balance unavailable. Check your original bank statement with your accountant.</p>}
                  {!currency && <p className="text-xs text-amber-700">Source did not state currency. Matching assumes EUR; verify before confirming.</p>}
                  {foreign && <p className="text-xs text-amber-700">Matching supports EUR cash records. Reconcile this {currency} line separately.</p>}
                </td>
                <td className="px-2 py-1">
                  <Badge tone={LINE_TONE[ln.status]}>{ln.status}</Badge>
                </td>
                <td className="py-1">
                  <ReconciliationLineActions line={ln} scope={scope} cacheIdentity={cacheIdentity} canWrite={canWrite} canMatch={!foreign && amount !== null} onReadLine={readLine} />
                </td>
              </tr>
            );
          })}
          {lines.isError && (
            <tr>
              <td colSpan={5} className="py-2">
                <ErrorState title="Couldn’t load the statement lines" onRetry={() => lines.refetch()} />
              </td>
            </tr>
          )}
          {!lines.isError && (lines.data ?? []).length === 0 && (
            <tr>
              <td colSpan={5} className="py-2 text-text-secondary">
                No lines.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
