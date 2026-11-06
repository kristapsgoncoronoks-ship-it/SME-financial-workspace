import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { Badge, Button, Card, ErrorState, type Tone } from "../components/ui";
import { api, apiError } from "../lib/api";
import { formatNumber, money, shortDate } from "../lib/format";
import type { BankLine, BankStatement, MatchCandidate } from "../lib/types";

const LINE_TONE: Record<BankLine["status"], Tone> = {
  matched: "success",
  ignored: "neutral",
  unmatched: "warning",
};

export default function ReconciliationPage() {
  const qc = useQueryClient();
  const [err, setErr] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);

  const statements = useQuery<BankStatement[]>({
    queryKey: ["reconciliation", "statements"],
    queryFn: async () => (await api.get("/reconciliation/statements")).data,
  });

  const invalidate = () => {
    setErr(null);
    qc.invalidateQueries({ queryKey: ["reconciliation"] });
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

      <ImportStatement onImported={invalidate} onErr={onErr} />

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
                {selected === s.id && (
                  <StatementLines statementId={s.id} onChanged={invalidate} onErr={onErr} />
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
}: {
  onImported: () => void;
  onErr: (e: unknown) => void;
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
    onSuccess: (r) => {
      setWarnings(r.warnings ?? []);
      if (fileRef.current) fileRef.current.value = "";
      onImported();
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
          disabled={upload.isPending}
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
  statementId,
  onChanged,
  onErr,
}: {
  statementId: string;
  onChanged: () => void;
  onErr: (e: unknown) => void;
}) {
  const qc = useQueryClient();
  const [openLine, setOpenLine] = useState<string | null>(null);
  const lines = useQuery<BankLine[]>({
    queryKey: ["reconciliation", "lines", statementId],
    queryFn: async () =>
      (await api.get(`/reconciliation/statements/${statementId}/lines`)).data,
  });

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["reconciliation", "lines", statementId] });
    onChanged();
  };

  const unmatch = useMutation({
    mutationFn: async (lineId: string) =>
      (await api.delete(`/reconciliation/lines/${lineId}/match`)).data,
    onSuccess: refresh,
    onError: onErr,
  });
  const ignore = useMutation({
    mutationFn: async (lineId: string) =>
      (await api.post(`/reconciliation/lines/${lineId}/ignore`)).data,
    onSuccess: refresh,
    onError: onErr,
  });

  return (
    <div role="region" aria-label="Statement lines" tabIndex={0} className="mt-3 max-w-full overflow-x-auto border-t border-slate-100 pt-3">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-xs text-text-secondary">
            <th className="py-1">Date</th>
            <th className="py-1">Description</th>
            <th className="py-1 text-right">Amount</th>
            <th className="py-1">Status</th>
            <th className="py-1"></th>
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
                <td className="py-1">
                  <Badge tone={LINE_TONE[ln.status]}>{ln.status}</Badge>
                </td>
                <td className="py-1 text-right">
                  {ln.status === "unmatched" && (
                    <div className="flex justify-end gap-2">
                      <Button
                        size="sm"
                        variant="secondary"
                        disabled={foreign || amount === null}
                        onClick={() => setOpenLine(openLine === ln.id ? null : ln.id)}
                      >
                        {openLine === ln.id ? "Close" : "Find match"}
                      </Button>
                      <Button size="sm" variant="ghost" onClick={() => ignore.mutate(ln.id)} disabled={ignore.isPending}>
                        Ignore
                      </Button>
                    </div>
                  )}
                  {ln.status === "matched" && (
                    <Button size="sm" variant="ghost" onClick={() => unmatch.mutate(ln.id)} disabled={unmatch.isPending}>
                      Unmatch
                    </Button>
                  )}
                  {ln.status === "ignored" && (
                    <Button size="sm" variant="ghost" onClick={() => unmatch.mutate(ln.id)} disabled={unmatch.isPending}>
                      Restore
                    </Button>
                  )}
                  {openLine === ln.id && amount !== null && !foreign && (
                    <Candidates lineId={ln.id} onMatched={() => { setOpenLine(null); refresh(); }} onErr={onErr} />
                  )}
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

function Candidates({
  lineId,
  onMatched,
  onErr,
}: {
  lineId: string;
  onMatched: () => void;
  onErr: (e: unknown) => void;
}) {
  const candidates = useQuery<MatchCandidate[]>({
    queryKey: ["reconciliation", "candidates", lineId],
    queryFn: async () => (await api.get(`/reconciliation/lines/${lineId}/candidates`)).data,
  });
  const match = useMutation({
    mutationFn: async (c: MatchCandidate) =>
      (await api.post(`/reconciliation/lines/${lineId}/match`, { kind: c.kind, target_id: c.id }))
        .data,
    onSuccess: onMatched,
    onError: onErr,
  });

  return (
    <div className="mt-2 rounded-lg border border-slate-200 bg-slate-50 p-2 text-left">
      {candidates.isError && (
        <ErrorState title="Couldn’t search for matches" description={apiError(candidates.error)} onRetry={() => candidates.refetch()} />
      )}
      {!candidates.isError && (candidates.data ?? []).length === 0 && (
        <p className="text-xs text-text-secondary">No matching receipt or payout found.</p>
      )}
      {(candidates.data ?? []).map((c) => (
        <div key={`${c.kind}-${c.id}`} className="flex items-center justify-between gap-2 py-1">
          <span className="text-xs text-slate-600">
            {{ receipt: "Receipt", reimbursement: "Reimbursement", payment_run: "Supplier payment run", issued_payment: "Invoice payment" }[c.kind]} · {money(c.amount, c.currency)} {c.currency} ·{" "}
            {shortDate(c.date)}
            {c.reference ? ` · ${c.reference}` : ""}
            {c.days_off > 0 ? ` · ${c.days_off}d off` : ""}
          </span>
          <Button size="sm" loading={match.isPending} onClick={() => match.mutate(c)}>
            Match
          </Button>
        </div>
      ))}
    </div>
  );
}
