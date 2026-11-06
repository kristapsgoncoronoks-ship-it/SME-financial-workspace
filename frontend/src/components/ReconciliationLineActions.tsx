import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import axios from "axios";
import { Badge, Button, ErrorState, Textarea } from "./ui";
import { api, apiError, apiErrorCode, isCanceledRequest } from "../lib/api";
import { money, shortDate } from "../lib/format";
import type { BankLine, MatchCandidate } from "../lib/types";
import {
  clearReconciliationCommand, loadReconciliationCommand, lookupReconciliationCommand,
  normalizeReconciliationReason,
  postReconciliationCommand, readCurrentReconciliationLine, readReconciliationHistory, reconciliationContinuation,
  startReconciliationCommand, validReconciliationVersion,
  type ReconciliationHistory, type ReconciliationOperation, type ReconciliationReceipt,
  type ReconciliationScope,
} from "../lib/reconciliationCommands";

const LABEL = { match: "Match", ignore: "Ignore", unmatch: "Unmatch", restore: "Restore" };
const RECORDED = { match: "Match recorded", ignore: "Ignore recorded", unmatch: "Unmatch recorded", restore: "Restore recorded" };
type Action = { kind: "new"; operation: ReconciliationOperation; candidate?: MatchCandidate } | { kind: "check" | "retry" | "refresh" };

export function ReconciliationLineActions({ line, scope, cacheIdentity, canWrite, canMatch, onReadLine }: {
  line: BankLine; scope: ReconciliationScope; cacheIdentity: string; canWrite: boolean; canMatch: boolean;
  onReadLine: (line: BankLine) => Promise<void>;
}) {
  const [initial] = useState(() => {
    try { return { saved: loadReconciliationCommand(scope, line.id), error: null }; }
    catch (error) { return { saved: null, error: apiError(error) }; }
  });
  const [saved, setSaved] = useState(initial.saved);
  const [error, setError] = useState<string | null>(initial.error);
  const [receipt, setReceipt] = useState<ReconciliationReceipt | null>(null);
  const [refused, setRefused] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState<"saving" | "checking" | "reading" | null>(null);
  const [editor, setEditor] = useState<"match" | "ignore" | null>(null);
  const [reviewed, setReviewed] = useState<BankLine | null>(null);
  const [reason, setReason] = useState("");
  const [historyOpen, setHistoryOpen] = useState(false);
  const active = useRef<AbortController | null>(null);
  useEffect(() => () => { active.current?.abort(); }, []);
  const usable = validReconciliationVersion(line.version) && line.version < 2147483647;
  const changed = editor !== null && reviewed !== null && line.version !== reviewed.version;
  const locked = !!busy || !!saved || !!initial.error || !canWrite || !usable || changed;
  const showEditor = (kind: "match" | "ignore") => {
    setEditor(editor === kind ? null : kind);
    setReviewed({ ...line });
  };

  const run = async (action: Action) => {
    if (active.current || (action.kind !== "check" && action.kind !== "refresh" && !canWrite)) return;
    const controller = new AbortController();
    active.current = controller;
    const current = reconciliationContinuation(controller.signal);
    let request = saved;
    let committed: ReconciliationReceipt | null = null;
    const readBack = async (result: ReconciliationReceipt | null) => {
      setBusy("reading");
      const fresh = await readCurrentReconciliationLine(request!, scope, controller.signal);
      current();
      if (result && fresh.version < result.after_version) throw new Error("The saved result is confirmed, but the current line has not been verified. Check again before another action.");
      await onReadLine(fresh);
      current();
      clearReconciliationCommand(scope, request!);
      setSaved(null); setRefused(false); setEditor(null); setReason("");
      setNote(result ? `${RECORDED[result.operation]} at version ${result.after_version}. Current line: version ${fresh.version}, ${fresh.status}.` : `Refreshed version ${fresh.version}. Review the current line before choosing another action.`);
    };
    setError(null); setNote(null);
    try {
      if (action.kind === "new") {
        if (locked) return;
        if (action.operation === "ignore" && (!normalizeReconciliationReason(reason) || reason.includes("\0") || reason.length > 1000)) throw new Error("Explain why this line is ignored, using up to 1,000 characters.");
        request = startReconciliationCommand(scope, action.operation === "match" || action.operation === "ignore" ? reviewed ?? line : line, action.operation,
          action.candidate ? { kind: action.candidate.kind, id: action.candidate.id } : null,
          action.operation === "ignore" ? reason : null);
        setSaved(request); setRefused(false); setReceipt(null);
      }
      if (!request) throw new Error("No saved request remains. Reload the statement before acting.");
      if (action.kind === "refresh") {
        if (!refused) throw new Error("Check the saved outcome before starting another action.");
        await readBack(null);
        return;
      }
      if (action.kind !== "new") {
        setBusy("checking");
        committed = await lookupReconciliationCommand(request, scope, controller.signal);
        current();
        if (!committed && action.kind === "check") {
          setNote("No committed record was found yet. This does not prove the request stopped. Check again or retry this same request.");
          return;
        }
      }
      if (!committed) {
        setBusy("saving");
        try { committed = await postReconciliationCommand(request, scope, controller.signal); }
        catch (failure) {
          current();
          const status = axios.isAxiosError(failure) ? failure.response?.status : undefined;
          if (status !== undefined && status >= 400 && status < 500 && apiErrorCode(failure) !== "reconciliation_command_payload_conflict") {
            setRefused(true);
            throw new Error(`${apiError(failure)} This request was refused. Refresh and review the line before choosing another action.`);
          }
          setBusy("checking");
          committed = await lookupReconciliationCommand(request, scope, controller.signal);
          current();
          if (!committed) throw new Error("The request outcome is unconfirmed. Keep this reference. Check the saved outcome or retry the same request; do not start a replacement action.");
        }
      }
      setReceipt(committed);
      await readBack(committed);
    } catch (failure) {
      if (!controller.signal.aborted && !isCanceledRequest(failure)) setError(committed
        ? `${RECORDED[committed.operation]}. The current view could not be refreshed: ${apiError(failure)} Check the saved outcome again; no new write is needed.` : apiError(failure));
    } finally {
      if (!controller.signal.aborted) setBusy(null);
      if (active.current === controller) active.current = null;
    }
  };

  return <div className="w-[min(32rem,100cqi)] min-w-0 max-w-lg space-y-2 whitespace-normal break-words text-left">
    <div className="flex flex-wrap gap-2">
      {line.status === "unmatched" && <>
        <Button size="sm" variant="secondary" disabled={(!canWrite || !usable || !!busy || !!saved || !!initial.error || !canMatch) && editor !== "match"} onClick={() => showEditor("match")}>{editor === "match" ? "Close" : "Find match"}</Button>
        <Button size="sm" variant="ghost" disabled={!!busy || !!saved || !!initial.error || !canWrite || !usable} onClick={() => showEditor("ignore")}>Ignore</Button>
      </>}
      {line.status === "matched" && <Button size="sm" variant="ghost" disabled={locked} onClick={() => { void run({ kind: "new", operation: "unmatch" }); }}>Unmatch</Button>}
      {line.status === "ignored" && <Button size="sm" variant="ghost" disabled={locked} onClick={() => { void run({ kind: "new", operation: "restore" }); }}>Restore</Button>}
      <Button size="sm" variant="secondary" onClick={() => setHistoryOpen(!historyOpen)}>{historyOpen ? "Hide history" : "History"}</Button>
    </div>
    {!usable && <p className="text-xs text-amber-800">{validReconciliationVersion(line.version) ? "This line has reached its version limit. Ask an administrator to review it." : "Reload the statement to obtain a usable line version before acting."}</p>}
    {editor && reviewed && <p className="text-xs text-text-secondary">Reviewing version {reviewed.version}.</p>}
    {changed && <p role="alert" className="text-sm text-amber-800">The line changed while you were reviewing it. Close this review and check the current line before another action.</p>}
    {editor === "ignore" && !saved && <div className="space-y-2">
      <Textarea label="Reason for ignoring" required maxLength={1000} value={reason} disabled={!!busy} onChange={(event) => setReason(event.target.value)} hint="Explain why this bank line should be excluded from matching. The reason is kept in its history." />
      <div className="flex flex-wrap gap-2">
        <Button size="sm" disabled={locked || !normalizeReconciliationReason(reason)} onClick={() => { void run({ kind: "new", operation: "ignore" }); }}>Confirm ignore</Button>
        <Button size="sm" variant="secondary" disabled={!!busy} onClick={() => setEditor(null)}>Cancel</Button>
      </div>
    </div>}
    {editor === "match" && !saved && canMatch && <Candidates line={reviewed ?? line} scope={scope} cacheIdentity={cacheIdentity} disabled={locked} onMatch={(candidate) => { void run({ kind: "new", operation: "match", candidate }); }} />}
    {saved && <div className="space-y-2 rounded-lg border border-amber-300 bg-amber-50 p-3 text-amber-900">
      <p className="text-sm font-medium">{receipt ? "Recorded action needs current readback" : refused ? "Request refused" : "Bank-line request needs checking"}</p>
      <p className="break-all text-xs">{LABEL[saved.payload.operation]} · reviewed version {saved.payload.expected_version} · Request {saved.key}</p>
      {saved.payload.reason && <p className="text-sm">Reason: {saved.payload.reason}</p>}
      <div className="flex flex-wrap gap-2">
        <Button size="sm" variant="secondary" disabled={!!busy} onClick={() => { void run({ kind: "check" }); }}>Check saved outcome</Button>
        {refused ? <Button size="sm" variant="secondary" disabled={!!busy} onClick={() => { void run({ kind: "refresh" }); }}>Refresh and review line</Button>
          : !receipt && <Button size="sm" variant="secondary" disabled={!!busy || !canWrite} onClick={() => { void run({ kind: "retry" }); }}>Retry same request</Button>}
      </div>
    </div>}
    {busy && <p role="status" className="text-sm text-text-secondary">{busy === "saving" ? "Saving the reviewed request…" : busy === "checking" ? "Checking the saved outcome…" : "Reading the current line and statement…"}</p>}
    {error && <p role="alert" className="text-sm text-rose-700">{error}</p>}
    {note && <p role="status" className="text-sm text-text-secondary">{note}</p>}
    {historyOpen && <LineHistory lineId={line.id} scope={scope} cacheIdentity={cacheIdentity} />}
  </div>;
}

function Candidates({ line, scope, cacheIdentity, disabled, onMatch }: {
  line: BankLine; scope: ReconciliationScope; cacheIdentity: string; disabled: boolean; onMatch: (candidate: MatchCandidate) => void;
}) {
  const candidates = useQuery<MatchCandidate[]>({
    queryKey: ["reconciliation", scope.userId, scope.orgId, cacheIdentity, "candidates", line.id, line.version],
    queryFn: async ({ signal }) => (await api.get(`/reconciliation/lines/${line.id}/candidates`, { signal })).data,
    meta: { errorHandled: true },
  });
  return <div className="mt-2 space-y-2 rounded-lg border border-slate-200 bg-slate-50 p-2">
    {candidates.isPending && <p role="status" className="text-xs text-text-secondary">Finding matching receipts and payouts…</p>}
    {candidates.isError && <ErrorState title="Couldn’t search for matches" description={apiError(candidates.error)} onRetry={() => candidates.refetch()} />}
    {candidates.isSuccess && candidates.data.length === 0 && <p className="text-xs text-text-secondary">No matching receipt or payout found.</p>}
    {(candidates.data ?? []).map((candidate) => <div key={`${candidate.kind}-${candidate.id}`} className="flex flex-wrap items-center justify-between gap-2 py-1">
      <span className="text-xs text-slate-600">{{ receipt: "Receipt", reimbursement: "Reimbursement", payment_run: "Supplier payment run", issued_payment: "Invoice payment" }[candidate.kind]} · {money(candidate.amount, candidate.currency)} {candidate.currency} · {shortDate(candidate.date)}{candidate.reference ? ` · ${candidate.reference}` : ""}{candidate.days_off > 0 ? ` · ${candidate.days_off}d off` : ""}</span>
      <Button size="sm" disabled={disabled} onClick={() => onMatch(candidate)}>Match</Button>
    </div>)}
  </div>;
}

function LineHistory({ lineId, scope, cacheIdentity }: { lineId: string; scope: ReconciliationScope; cacheIdentity: string }) {
  const [before, setBefore] = useState<number | null>(null);
  const history = useQuery<ReconciliationHistory>({
    queryKey: ["reconciliation", scope.userId, scope.orgId, cacheIdentity, "history", lineId, before],
    queryFn: async ({ signal }) => {
      const page = (await api.get<ReconciliationHistory>(`/reconciliation/lines/${lineId}/history`, { signal, params: { limit: 20, ...(before === null ? {} : { before_version: before }) } })).data;
      return readReconciliationHistory(page, before);
    },
    meta: { errorHandled: true },
  });
  return <section aria-label="Line command history" className="space-y-2 border-t border-slate-200 pt-2">
    <h3 className="text-sm font-semibold">Command history</h3>
    <p className="text-xs text-text-secondary">Recorded actions and their reviewed versions. Earlier changes may predate this history.</p>
    {history.isPending && <p role="status" className="text-xs text-text-secondary">Loading command history…</p>}
    {history.isError && <ErrorState title="Couldn’t load command history" description={apiError(history.error)} onRetry={() => history.refetch()} />}
    {history.isSuccess && history.data.events.length === 0 && <p className="text-xs text-text-secondary">No command history recorded on this page.</p>}
    <ol className="space-y-2">{(history.data?.events ?? []).map((event) => <li key={event.command_id} className="space-y-1 text-xs">
      <p><Badge tone="neutral">{LABEL[event.operation]}</Badge> · version {event.before_version} → {event.after_version} · {event.before.status} → {event.after.status}</p>
      {event.reason && <p className="whitespace-pre-wrap break-words">Reason: {event.reason}</p>}
      <p className="break-all text-text-secondary">Actor {event.actor_user_id} · Recorded {shortDate(event.recorded_at)}</p>
    </li>)}</ol>
    <div className="flex flex-wrap gap-2">
      {before !== null && <Button size="sm" variant="secondary" onClick={() => setBefore(null)} disabled={history.isFetching}>Newest events</Button>}
      {history.isSuccess && history.data.next_before_version !== null && <Button size="sm" variant="secondary" onClick={() => setBefore(history.data.next_before_version)} disabled={history.isFetching}>Earlier events</Button>}
    </div>
  </section>;
}
