import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Badge, Button, Card, ConfirmDialog, ErrorState, type Tone } from "../components/ui";
import { api, apiError, apiErrorCode, downloadFile, tokenStore } from "../lib/api";
import { decimalMoney, shortDate } from "../lib/format";
import { useConfirm } from "../components/ui/useConfirm";
import { useAuth } from "../auth/AuthContext";
import {
  checkPayoutCommand, clearPayoutCommand, PAYOUT_READ_TIMEOUT_MS,
  preparePayoutCommand, readPayoutBatch, readPayoutCommand, reportEUR,
  selectionTotal, sendPayoutCommand,
  type PayoutBatch as Batch, type PayoutBatchDetail, type PayoutReport as Report,
} from "../lib/reimbursementCreateCommand";
import {
  checkPaymentCommand, clearPaymentCommand, paymentReferences, preparePaymentCommand,
  readPaymentBatch, readPaymentCommand, reviewPaymentBatch, sendPaymentCommand,
  type SavedPaymentCommand,
} from "../lib/reimbursementPaymentCommand";
import {
  cancelReferences, checkCancelCommand, clearCancelCommand, prepareCancelCommand,
  readCancelCommand, readCancelledBatch, reviewCancellationBatch, sendCancelCommand,
  type CancellationResult, type SavedCancelCommand,
} from "../lib/reimbursementCancelCommand";

interface Page<T> { items: T[]; total: number; page: number; page_size: number }

const STATUS_TONE: Record<string, Tone> = {
  open: "info",
  paid: "success",
  cancelled: "neutral",
};
const BATCH_READ_TIMEOUT_MS = PAYOUT_READ_TIMEOUT_MS;
const PAGE_SIZE = 100;

function hasExportRecord(batch: Batch): boolean {
  return batch.export_count !== 0 || batch.exported_at !== null || batch.last_msg_id !== null;
}

type ExportKind = "csv" | "sepa";
interface PendingExport {
  batch: Batch;
  kind: ExportKind;
  confirmReexport: boolean;
  acknowledgeSkipped: boolean;
}
type PaymentStage = "preparing" | "recording" | "checking" | "verifying";
interface PaymentProgress { stage: PaymentStage; controller: AbortController; generation: number; token: string | null }

export default function ReimbursementsPage() {
  const { cacheIdentity } = useAuth();
  return <ReimbursementsContent key={cacheIdentity} />;
}

function ReimbursementsContent() {
  const { confirm, dialog } = useConfirm();
  const qc = useQueryClient();
  const { user, org, hasPerm } = useAuth();
  const scope = { userId: user?.id ?? "", orgId: org?.id ?? "" };
  const canApprove = !!user && !!org && hasPerm("expense.approve");
  const [err, setErr] = useState<string | null>(null);
  const [picked, setPicked] = useState<Record<string, Report>>({});
  const [eligiblePage, setEligiblePage] = useState(1);
  const [batchPage, setBatchPage] = useState(1);
  const [createError, setCreateError] = useState<string | null>(null);
  const [commandStage, setCommandStage] = useState<"sending" | "checking" | "reading" | null>(null);
  const [checkedMissing, setCheckedMissing] = useState(false);
  const [committed, setCommitted] = useState<PayoutBatchDetail | null>(null);
  const committedBatchId = useRef<string | null>(null);
  const [created, setCreated] = useState<Batch | null>(null);
  const commandBusy = useRef(false);
  const commandController = useRef<AbortController | null>(null);
  const commandGeneration = useRef<number | null>(null);
  const commandToken = useRef<string | null>(null);
  const active = useRef(true);
  useEffect(() => {
    active.current = true;
    return () => { active.current = false; commandController.current?.abort(); };
  }, []);
  const [refs, setRefs] = useState<Record<string, string>>({});
  const busyRef = useRef(new Set<string>());
  const refreshingRef = useRef(new Set<string>());
  const [busyBatches, setBusyBatches] = useState(new Set<string>());
  const [refreshRequired, setRefreshRequired] = useState(new Set<string>());
  const paymentOperations = useRef(new Map<string, PaymentProgress>());
  const [paymentStages, setPaymentStages] = useState<Record<string, PaymentStage>>({});
  const [paymentErrors, setPaymentErrors] = useState<Record<string, string>>({});
  const [paymentMissing, setPaymentMissing] = useState(new Set<string>());
  const [paymentKnown, setPaymentKnown] = useState(new Set<string>());
  const [paymentSaved, setPaymentSaved] = useState(new Set<string>());
  const cancelOperations = useRef(new Map<string, PaymentProgress>());
  const [cancelStages, setCancelStages] = useState<Record<string, PaymentStage>>({});
  const [cancelErrors, setCancelErrors] = useState<Record<string, string>>({});
  const [cancelMissing, setCancelMissing] = useState(new Set<string>());
  const [cancelKnown, setCancelKnown] = useState(new Set<string>());
  const [cancelSaved, setCancelSaved] = useState(new Set<string>());
  const confirmationOwner = useRef<string | null>(null);
  const [confirmationBatchId, setConfirmationBatchId] = useState<string | null>(null);
  useEffect(() => () => {
    for (const operation of paymentOperations.current.values()) operation.controller.abort();
    for (const operation of cancelOperations.current.values()) operation.controller.abort();
  }, []);
  // WO-9 export flows: a re-export needs explicit confirmation (the bank sees a
  // NEW message id), and employees without an IBAN must be acknowledged BY NAME.
  const [reexportAsk, setReexportAsk] = useState<PendingExport | null>(null);
  const [skippedAsk, setSkippedAsk] = useState<{ pending: PendingExport; detail: string } | null>(
    null,
  );

  const eligibleKey = ["reimbursements", "eligible", scope.userId, scope.orgId];
  const batchKey = ["reimbursements", "batches", scope.userId, scope.orgId];
  const approvedQ = useQuery<Page<Report>>({
    queryKey: [...eligibleKey, eligiblePage],
    queryFn: async ({ signal }) => (await api.get(`/reimbursements/eligible?page=${eligiblePage}&page_size=${PAGE_SIZE}`, { signal, timeout: BATCH_READ_TIMEOUT_MS })).data,
    enabled: canApprove,
  });
  const approved = approvedQ.data;
  const batchesQ = useQuery<Page<Batch>>({
    queryKey: [...batchKey, batchPage],
    queryFn: async ({ signal }) => (await api.get(`/reimbursements/page?page=${batchPage}&page_size=${PAGE_SIZE}`, { signal, timeout: BATCH_READ_TIMEOUT_MS })).data,
    enabled: canApprove,
  });
  const batches = batchesQ.data?.items;
  useEffect(() => {
    if (!approved) return;
    const lastPage = Math.max(1, Math.ceil(approved.total / PAGE_SIZE));
    if (eligiblePage > lastPage) setEligiblePage(lastPage);
  }, [approved, eligiblePage]);
  useEffect(() => {
    if (!batchesQ.data) return;
    const lastPage = Math.max(1, Math.ceil(batchesQ.data.total / PAGE_SIZE));
    if (batchPage > lastPage) setBatchPage(lastPage);
  }, [batchesQ.data, batchPage]);

  let savedPayments: ReturnType<typeof paymentReferences> = [];
  let paymentStorageError: string | null = null;
  try { savedPayments = paymentReferences(scope); }
  catch (error) { paymentStorageError = apiError(error); }
  const hasSavedPayment = (id: string) => {
    try { return readPaymentCommand(scope, id) !== null; }
    catch { return true; }
  };
  let savedCancellations: ReturnType<typeof cancelReferences> = [];
  let cancelStorageError: string | null = null;
  try { savedCancellations = cancelReferences(scope); }
  catch (error) { cancelStorageError = apiError(error); }
  const hasSavedCancellation = (id: string) => {
    try { return readCancelCommand(scope, id) !== null; }
    catch { return true; }
  };
  const exportConfirmationVisible = () =>
    (reexportAsk !== null && !busyRef.current.has(reexportAsk.batch.id) && !hasSavedPayment(reexportAsk.batch.id) && !hasSavedCancellation(reexportAsk.batch.id) && committedBatchId.current !== reexportAsk.batch.id) ||
    (skippedAsk !== null && !busyRef.current.has(skippedAsk.pending.batch.id) && !hasSavedPayment(skippedAsk.pending.batch.id) && !hasSavedCancellation(skippedAsk.pending.batch.id) && committedBatchId.current !== skippedAsk.pending.batch.id);

  const beginBatchAction = (id: string) => {
    if (busyRef.current.has(id) || committedBatchId.current === id || hasSavedPayment(id) || hasSavedCancellation(id) || paymentStorageError || cancelStorageError) return false;
    busyRef.current.add(id);
    setBusyBatches(new Set(busyRef.current));
    return true;
  };

  const refreshBatchStatus = async (id: string, owns: () => boolean = () => active.current, signal?: AbortSignal) => {
    if (!active.current) return;
    if (refreshingRef.current.has(id)) return;
    refreshingRef.current.add(id);
    setRefreshRequired((current) => new Set([...current].filter((key) => key !== id)));
    try {
      // A fresh scoped detail read cannot reuse a list request started before
      // this command. Cancel that old list before publishing the current row.
      await qc.cancelQueries({ queryKey: batchKey });
      const { data } = await api.get<Batch>(`/reimbursements/${id}`, {
        timeout: BATCH_READ_TIMEOUT_MS, signal,
        headers: { "X-Expected-Org-ID": scope.orgId, "X-Expected-User-ID": scope.userId },
      });
      if (
        !data || data.id !== id || !Object.prototype.hasOwnProperty.call(STATUS_TONE, data.status) ||
        !Number.isInteger(data.version) || !Number.isInteger(data.export_count) ||
        !("exported_at" in data) || !("last_msg_id" in data)
      ) throw new Error("The current batch status could not be verified.");
      if (!owns()) return;
      qc.setQueriesData<Page<Batch>>({ queryKey: batchKey }, (current) =>
        current ? { ...current, items: current.items.map((batch) => batch.id === id ? data : batch) } : current,
      );
      await qc.invalidateQueries({ queryKey: eligibleKey }, { throwOnError: true });
      if (!owns()) return;
      busyRef.current.delete(id);
      setBusyBatches(new Set(busyRef.current));
    } catch {
      if (owns()) setRefreshRequired((current) => new Set(current).add(id));
    } finally {
      refreshingRef.current.delete(id);
    }
  };

  let pending = null;
  let referenceError: string | null = null;
  try { pending = readPayoutCommand(scope); }
  catch (error) { referenceError = apiError(error); }
  const frozen = commandStage !== null || pending !== null || referenceError !== null;

  const beginCommand = (stage: "sending" | "checking") => {
    if (commandBusy.current || !canApprove) return null;
    commandBusy.current = true;
    const controller = new AbortController();
    commandController.current = controller;
    commandGeneration.current = tokenStore.generation();
    commandToken.current = tokenStore.get();
    setCommandStage(stage);
    setCreateError(null);
    setCheckedMissing(false);
    return controller;
  };
  const ownsCommand = (signal: AbortSignal) => active.current && !signal.aborted &&
    signal === commandController.current?.signal && commandGeneration.current === tokenStore.generation() && commandToken.current === tokenStore.get();
  const finishCommand = (signal: AbortSignal) => {
    commandBusy.current = false;
    if (ownsCommand(signal)) setCommandStage(null);
  };
  const readCreatedBatch = async (original: PayoutBatchDetail, key: string, signal: AbortSignal) => {
    if (!ownsCommand(signal)) return;
    committedBatchId.current = original.id;
    setCommitted(original);
    setCommandStage("reading");
    const current = await readPayoutBatch(scope, original, signal);
    if (!ownsCommand(signal)) return;
    await Promise.all([
      qc.cancelQueries({ queryKey: eligibleKey }), qc.cancelQueries({ queryKey: batchKey }),
    ]);
    if (!ownsCommand(signal)) return;
    await Promise.all([
      qc.invalidateQueries({ queryKey: eligibleKey }, { throwOnError: true }),
      qc.invalidateQueries({ queryKey: batchKey }, { throwOnError: true }),
    ]);
    if (!ownsCommand(signal)) return;
    // The created reports may have removed the final page. Verify the page we
    // are about to show, rather than clearing the reference against an empty
    // former page while eligible reports still exist on an earlier one.
    const refreshClampedPage = async <T,>(prefix: string[], page: number, endpoint: string, setPage: (page: number) => void) => {
      const latest = qc.getQueryData<Page<T>>([...prefix, page]);
      if (!latest) throw new Error("The refreshed payout list could not be verified. Check the saved batch again.");
      const lastPage = Math.max(1, Math.ceil(latest.total / PAGE_SIZE));
      if (page <= lastPage) return;
      setPage(lastPage);
      await qc.fetchQuery<Page<T>>({ queryKey: [...prefix, lastPage], staleTime: 0, retry: false,
        queryFn: async ({ signal: querySignal }) => (await api.get(`/reimbursements/${endpoint}?page=${lastPage}&page_size=${PAGE_SIZE}`, {
          signal: querySignal, timeout: BATCH_READ_TIMEOUT_MS,
          headers: { "X-Expected-Org-ID": scope.orgId, "X-Expected-User-ID": scope.userId },
        })).data });
    };
    await Promise.all([
      refreshClampedPage<Report>(eligibleKey, eligiblePage, "eligible", setEligiblePage),
      refreshClampedPage<Batch>(batchKey, batchPage, "page", setBatchPage),
    ]);
    if (!ownsCommand(signal)) return;
    clearPayoutCommand(scope, key);
    setPicked({});
    setCreated(current);
    committedBatchId.current = null;
    setCommitted(null);
    setCreateError(null);
  };
  const createBatch = async () => {
    if (pending || referenceError) return;
    const controller = beginCommand("sending");
    if (!controller) return;
    // Copy the observed selection before the first await can yield.
    const reports = Object.values(picked).map((report) => ({ ...report }));
    setCreated(null);
    try {
      const request = await preparePayoutCommand(scope, reports, controller.signal);
      const original = await sendPayoutCommand(scope, request, controller.signal);
      await readCreatedBatch(original, request.key, controller.signal);
    } catch (error) {
      if (ownsCommand(controller.signal)) setCreateError(apiError(error));
    } finally { finishCommand(controller.signal); }
  };
  const checkBatch = async (retry = false) => {
    const controller = beginCommand(retry ? "sending" : "checking");
    if (!controller) return;
    try {
      const request = readPayoutCommand(scope);
      if (!request) throw new Error("No saved payout request remains. Refresh and review the eligible reports.");
      const original = retry
        ? await sendPayoutCommand(scope, request, controller.signal, true)
        : await checkPayoutCommand(scope, request, controller.signal);
      if (!ownsCommand(controller.signal)) return;
      if (!original) {
        setCheckedMissing(true);
        setCreateError("No committed batch is visible for this reference yet. Check again or retry the original request with the same inputs and key; do not create a replacement batch.");
        return;
      }
      await readCreatedBatch(original, request.key, controller.signal);
    } catch (error) {
      if (ownsCommand(controller.signal)) setCreateError(apiError(error));
    } finally { finishCommand(controller.signal); }
  };
  const publishPaymentStages = () => setPaymentStages(Object.fromEntries(
    [...paymentOperations.current].map(([id, operation]) => [id, operation.stage]),
  ));
  const beginPayment = (id: string, stage: PaymentStage) => {
    if (!canApprove || paymentOperations.current.has(id) || cancelOperations.current.has(id) || hasSavedCancellation(id) || committedBatchId.current === id || paymentStorageError || cancelStorageError) return null;
    const operation: PaymentProgress = { stage, controller: new AbortController(), generation: tokenStore.generation(), token: tokenStore.get() };
    paymentOperations.current.set(id, operation);
    busyRef.current.add(id);
    setBusyBatches(new Set(busyRef.current));
    setPaymentErrors((current) => { const next = { ...current }; delete next[id]; return next; });
    setPaymentMissing((current) => new Set([...current].filter((key) => key !== id)));
    publishPaymentStages();
    return operation;
  };
  const ownsPayment = (id: string, operation: PaymentProgress) => active.current &&
    paymentOperations.current.get(id) === operation && !operation.controller.signal.aborted &&
    operation.generation === tokenStore.generation() && operation.token === tokenStore.get();
  const paymentStage = (id: string, operation: PaymentProgress, stage: PaymentStage) => {
    if (!ownsPayment(id, operation)) return;
    operation.stage = stage; publishPaymentStages();
  };
  const finishPayment = (id: string, operation: PaymentProgress) => {
    if (!ownsPayment(id, operation)) return;
    paymentOperations.current.delete(id); publishPaymentStages();
  };
  const releasePaymentBusy = (id: string) => {
    busyRef.current.delete(id); setBusyBatches(new Set(busyRef.current));
    setRefreshRequired((current) => new Set([...current].filter((key) => key !== id)));
  };
  const verifyPayment = async (original: PayoutBatchDetail, saved: SavedPaymentCommand, operation: PaymentProgress) => {
    const id = saved.batchId;
    if (!ownsPayment(id, operation)) return;
    setPaymentKnown((current) => new Set(current).add(id));
    paymentStage(id, operation, "verifying");
    await readPaymentBatch(scope, original, saved, operation.controller.signal);
    if (!ownsPayment(id, operation)) return;
    await Promise.all([qc.cancelQueries({ queryKey: batchKey }), qc.cancelQueries({ queryKey: eligibleKey })]);
    if (!ownsPayment(id, operation)) return;
    await Promise.all([
      qc.invalidateQueries({ queryKey: batchKey }, { throwOnError: true }),
      qc.invalidateQueries({ queryKey: eligibleKey }, { throwOnError: true }),
    ]);
    if (!ownsPayment(id, operation)) return;
    clearPaymentCommand(scope, id, saved.key);
    releasePaymentBusy(id);
    setPaymentSaved((current) => new Set(current).add(id));
    setPaymentKnown((current) => new Set([...current].filter((key) => key !== id)));
  };
  const restoreReviewFocus = (control: HTMLElement | null, operation: PaymentProgress) => {
    // Preparation disables the initiating button before the dialog can capture
    // it. Restore that explicit target after the declined review releases it.
    requestAnimationFrame(() => {
      if (active.current && operation.generation === tokenStore.generation() && operation.token === tokenStore.get() &&
          control?.isConnected && !control.matches(":disabled") && document.activeElement === document.body) control.focus();
    });
  };
  const recordPayment = async (batch: Batch) => {
    if (busyRef.current.has(batch.id) || hasSavedPayment(batch.id) || confirmationOwner.current !== null || exportConfirmationVisible()) return;
    const reviewControl = document.activeElement as HTMLElement | null;
    const operation = beginPayment(batch.id, "preparing");
    if (!operation) return;
    confirmationOwner.current = batch.id; setConfirmationBatchId(batch.id);
    // Capture the optional reference from the initiating view, before any GET.
    const reference = refs[batch.id] || null;
    setPaymentSaved((current) => new Set([...current].filter((key) => key !== batch.id)));
    try {
      const reviewed = await reviewPaymentBatch(scope, { ...batch }, operation.controller.signal);
      if (!ownsPayment(batch.id, operation)) return;
      const agreed = await confirm({ title: "Record this payment?", confirmLabel: "Record payment", tone: "primary",
        body: <div className="min-w-0 space-y-3 [overflow-wrap:anywhere]">
          <p>Batch {reviewed.id}</p>
          <p>Record your statement that {decimalMoney(reviewed.total_eur, "EUR")} was paid for {reviewed.report_count} report{reviewed.report_count === 1 ? "" : "s"}?</p>
          <p>Method: {reviewed.method}. Reference: {reference || "Not provided (optional)"}.</p>
          <p>This records an existing payment. It does not send money or confirm bank processing.</p>
        </div> });
      if (!ownsPayment(batch.id, operation)) return;
      if (!agreed) { releasePaymentBusy(batch.id); restoreReviewFocus(reviewControl, operation); return; }
      confirmationOwner.current = null; setConfirmationBatchId(null);
      paymentStage(batch.id, operation, "recording");
      const saved = await preparePaymentCommand(scope, reviewed, reference, operation.controller.signal);
      if (ownsPayment(batch.id, operation)) publishPaymentStages();
      const original = await sendPaymentCommand(scope, saved, operation.controller.signal);
      await verifyPayment(original, saved, operation);
    } catch (error) {
      if (!ownsPayment(batch.id, operation)) return;
      setPaymentErrors((current) => ({ ...current, [batch.id]: apiError(error) }));
      if (!hasSavedPayment(batch.id)) {
        await refreshBatchStatus(batch.id, () => ownsPayment(batch.id, operation), operation.controller.signal);
      }
    } finally {
      if (ownsPayment(batch.id, operation) && confirmationOwner.current === batch.id) {
        confirmationOwner.current = null; setConfirmationBatchId(null);
      }
      finishPayment(batch.id, operation);
    }
  };
  const checkPayment = async (id: string, retry = false) => {
    const operation = beginPayment(id, retry ? "recording" : "checking");
    if (!operation) return;
    try {
      const saved = readPaymentCommand(scope, id);
      if (!saved) throw new Error("No saved payment request remains. Refresh and review the batch before another action.");
      const original = retry
        ? await sendPaymentCommand(scope, saved, operation.controller.signal, true)
        : await checkPaymentCommand(scope, saved, operation.controller.signal);
      if (!ownsPayment(id, operation)) return;
      if (!original) {
        setPaymentMissing((current) => new Set(current).add(id));
        setPaymentErrors((current) => ({ ...current, [id]: "No committed payment record is visible for this reference yet. Check again or explicitly retry the original request with the same key; do not record a replacement payment." }));
        return;
      }
      await verifyPayment(original, saved, operation);
    } catch (error) {
      if (!ownsPayment(id, operation)) return;
      setPaymentErrors((current) => ({ ...current, [id]: apiError(error) }));
      if (!hasSavedPayment(id)) await refreshBatchStatus(id, () => ownsPayment(id, operation), operation.controller.signal);
    } finally { finishPayment(id, operation); }
  };
  const publishCancelStages = () => setCancelStages(Object.fromEntries(
    [...cancelOperations.current].map(([id, operation]) => [id, operation.stage]),
  ));
  const beginCancellation = (id: string, stage: PaymentStage) => {
    if (!canApprove || cancelOperations.current.has(id) || paymentOperations.current.has(id) ||
        hasSavedPayment(id) || committedBatchId.current === id || paymentStorageError || cancelStorageError) return null;
    const operation: PaymentProgress = { stage, controller: new AbortController(), generation: tokenStore.generation(), token: tokenStore.get() };
    cancelOperations.current.set(id, operation);
    busyRef.current.add(id); setBusyBatches(new Set(busyRef.current));
    setCancelErrors((current) => { const next = { ...current }; delete next[id]; return next; });
    setCancelMissing((current) => new Set([...current].filter((key) => key !== id)));
    publishCancelStages();
    return operation;
  };
  const ownsCancellation = (id: string, operation: PaymentProgress) => active.current &&
    cancelOperations.current.get(id) === operation && !operation.controller.signal.aborted &&
    operation.generation === tokenStore.generation() && operation.token === tokenStore.get();
  const cancellationStage = (id: string, operation: PaymentProgress, stage: PaymentStage) => {
    if (!ownsCancellation(id, operation)) return;
    operation.stage = stage; publishCancelStages();
  };
  const finishCancellation = (id: string, operation: PaymentProgress) => {
    if (!ownsCancellation(id, operation)) return;
    cancelOperations.current.delete(id); publishCancelStages();
  };
  const verifyCancellation = async (original: CancellationResult, saved: SavedCancelCommand, operation: PaymentProgress) => {
    const id = saved.batchId;
    if (!ownsCancellation(id, operation)) return;
    setCancelKnown((current) => new Set(current).add(id));
    cancellationStage(id, operation, "verifying");
    await readCancelledBatch(scope, original, saved, operation.controller.signal);
    if (!ownsCancellation(id, operation)) return;
    await Promise.all([qc.cancelQueries({ queryKey: batchKey }), qc.cancelQueries({ queryKey: eligibleKey })]);
    if (!ownsCancellation(id, operation)) return;
    await Promise.all([
      qc.invalidateQueries({ queryKey: batchKey }, { throwOnError: true }),
      qc.invalidateQueries({ queryKey: eligibleKey }, { throwOnError: true }),
    ]);
    if (!ownsCancellation(id, operation)) return;
    clearCancelCommand(scope, id, saved.key);
    releasePaymentBusy(id);
    setCancelSaved((current) => new Set(current).add(id));
    setCancelKnown((current) => new Set([...current].filter((key) => key !== id)));
  };
  const cancelBatch = async (batch: Batch) => {
    if (hasExportRecord(batch) || busyRef.current.has(batch.id) || hasSavedCancellation(batch.id) ||
        confirmationOwner.current !== null || exportConfirmationVisible()) return;
    const reviewControl = document.activeElement as HTMLElement | null;
    const operation = beginCancellation(batch.id, "preparing");
    if (!operation) return;
    confirmationOwner.current = batch.id; setConfirmationBatchId(batch.id);
    let reason = "";
    try {
      const reviewed = await reviewCancellationBatch(scope, { ...batch }, operation.controller.signal);
      if (!ownsCancellation(batch.id, operation)) return;
      const agreed = await confirm({ title: "Cancel this reimbursement batch?", confirmLabel: "Cancel batch",
        body: <div className="min-w-0 space-y-3 [overflow-wrap:anywhere]">
          <p>Cancel this unexported batch of {decimalMoney(reviewed.total_eur, "EUR")} and return its {reviewed.report_count} report{reviewed.report_count === 1 ? "" : "s"} to the payable pool?</p>
          <p>The server will refuse cancellation if an export or payment has been recorded.</p>
          <label className="block space-y-1 text-sm">
            <span>Cancellation reason (optional)</span>
            <textarea aria-label="Cancellation reason (optional)" maxLength={1000} rows={3}
              className="block w-full rounded-lg border border-slate-300 px-3 py-2 text-base"
              onChange={(event) => { reason = event.target.value; }} />
          </label>
        </div> });
      if (!ownsCancellation(batch.id, operation)) return;
      if (!agreed) { releasePaymentBusy(batch.id); restoreReviewFocus(reviewControl, operation); return; }
      confirmationOwner.current = null; setConfirmationBatchId(null);
      cancellationStage(batch.id, operation, "recording");
      const saved = await prepareCancelCommand(scope, reviewed, reason.trim() || null, operation.controller.signal);
      if (ownsCancellation(batch.id, operation)) publishCancelStages();
      const original = await sendCancelCommand(scope, saved, operation.controller.signal);
      await verifyCancellation(original, saved, operation);
    } catch (error) {
      if (!ownsCancellation(batch.id, operation)) return;
      setCancelErrors((current) => ({ ...current, [batch.id]: apiError(error) }));
      if (!hasSavedCancellation(batch.id)) await refreshBatchStatus(batch.id, () => ownsCancellation(batch.id, operation), operation.controller.signal);
    } finally {
      if (ownsCancellation(batch.id, operation) && confirmationOwner.current === batch.id) {
        confirmationOwner.current = null; setConfirmationBatchId(null);
      }
      finishCancellation(batch.id, operation);
    }
  };
  const checkCancellation = async (id: string, retry = false) => {
    const operation = beginCancellation(id, retry ? "recording" : "checking");
    if (!operation) return;
    try {
      const saved = readCancelCommand(scope, id);
      if (!saved) throw new Error("No saved cancellation remains. Refresh and review the batch before another action.");
      const original = retry
        ? await sendCancelCommand(scope, saved, operation.controller.signal, true)
        : await checkCancelCommand(scope, saved, operation.controller.signal);
      if (!ownsCancellation(id, operation)) return;
      if (!original) {
        setCancelMissing((current) => new Set(current).add(id));
        setCancelErrors((current) => ({ ...current, [id]: "No committed cancellation is visible for this reference yet. Check again or explicitly retry the original request with the same key." }));
        return;
      }
      await verifyCancellation(original, saved, operation);
    } catch (error) {
      if (!ownsCancellation(id, operation)) return;
      setCancelErrors((current) => ({ ...current, [id]: apiError(error) }));
      if (!hasSavedCancellation(id)) await refreshBatchStatus(id, () => ownsCancellation(id, operation), operation.controller.signal);
    } finally { finishCancellation(id, operation); }
  };

  const doExport = async (p: PendingExport) => {
    if (!beginBatchAction(p.batch.id)) return;
    const params = new URLSearchParams();
    if (p.confirmReexport) params.set("confirm_reexport", "true");
    if (p.acknowledgeSkipped) params.set("acknowledge_skipped", "true");
    const q = params.toString() ? `?${params.toString()}` : "";
    const path = p.kind === "csv" ? "export" : "sepa";
    const ext = p.kind === "csv" ? "csv" : "xml";
    try {
      setErr(null);
      await downloadFile(
        `/reimbursements/${p.batch.id}/${path}${q}`,
        `reimbursement-${p.batch.reference || p.batch.id}.${ext}`,
        { method: "post" },
      );
    } catch (e) {
      const code = apiErrorCode(e);
      if (code === "already_exported") {
        setReexportAsk({ ...p, confirmReexport: true });
      } else if (code === "skipped_payees") {
        setSkippedAsk({ pending: { ...p, acknowledgeSkipped: true }, detail: apiError(e) });
      } else {
        setErr(apiError(e));
      }
    } finally {
      await refreshBatchStatus(p.batch.id);
    }
  };

  const items = approved?.items ?? [];
  const selectedReports = Object.values(picked);
  let selectedTotal: string | null = null;
  let amountError: string | null = null;
  try { selectedTotal = selectionTotal(selectedReports); }
  catch (error) { amountError = apiError(error); }
  const batchIsBusy = (id: string) => busyBatches.has(id) || committed?.id === id || hasSavedPayment(id) || hasSavedCancellation(id) || paymentStorageError !== null || cancelStorageError !== null;
  const staleVisibleSelection = items.some((report) => picked[report.id] && picked[report.id].version !== report.version);

  return (
    <div className="min-w-0 space-y-6 [overflow-wrap:anywhere]">
      {dialog}
      <div className="flex min-w-0 flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-xl font-semibold">Reimbursements</h1>
          <p className="text-slate-500">Pay approved expense reports in a batch.</p>
        </div>
        <Link to="/expenses" className="text-sm text-brand-600 hover:underline">
          ← Expenses
        </Link>
      </div>

      {err && (
        <div role="alert" className="rounded-lg border border-rose-200 bg-rose-50 px-4 py-2 text-sm text-rose-700">
          {err}
        </div>
      )}

      {/* Approved reports → new batch */}
      <Card className="min-w-0">
        <div className="mb-3 flex min-w-0 flex-wrap items-center justify-between gap-3">
          <h2 className="text-sm font-semibold text-slate-700">Approved — awaiting payout</h2>
          <Button
            size="sm"
            className="max-w-full"
            disabled={!canApprove || selectedReports.length === 0 || selectedTotal === null || frozen || staleVisibleSelection || approvedQ.isPending || approvedQ.isError}
            loading={commandStage === "sending"}
            onClick={() => void createBatch()}
          >
            Create payout batch ({decimalMoney(selectedTotal, "EUR")})
          </Button>
        </div>
        <p className="mb-3 max-w-prose text-sm text-text-secondary">
          Select approved, unbatched reports. Reports marked for reimbursement are included.
          A batch groups payment instructions; creating it does not confirm payment.
        </p>
        {!canApprove && <p className="mb-3 text-sm text-text-secondary">Expense approval permission is required to create payout batches.</p>}
        {createError && <p role="alert" className="mb-3 text-sm text-rose-700">{createError}</p>}
        {referenceError && <p role="alert" className="mb-3 text-sm text-rose-700">{referenceError}</p>}
        {pending && (
          <div className="mb-4 space-y-2 rounded-lg border border-slate-200 bg-slate-50 p-3">
            <p className="text-sm font-medium">A payout batch request needs checking.</p>
            <p className="max-w-prose text-sm text-text-secondary">
              Reference {pending.key} · {pending.payload.report_ids.length} selected report{pending.payload.report_ids.length === 1 ? "" : "s"} · {decimalMoney(pending.expectedTotal, "EUR")}.
              Keep this reference until its committed result and current batch status can be verified.
            </p>
            <div className="flex min-w-0 flex-wrap gap-2">
              <Button size="sm" variant="secondary" disabled={!canApprove || commandStage !== null}
                onClick={() => void checkBatch()}>
                {committed ? "Refresh saved batch" : "Check saved batch"}
              </Button>
              {checkedMissing && <Button size="sm" variant="secondary" disabled={!canApprove || commandStage !== null}
                onClick={() => void checkBatch(true)}>Retry original request</Button>}
            </div>
          </div>
        )}
        {commandStage && <p role="status" className="mb-3 text-sm text-text-secondary">
          {commandStage === "sending" ? "Saving the original payout request…" : commandStage === "checking" ? "Checking the saved payout request…" : "Verifying the current batch and refreshing the reports…"}
        </p>}
        {created && <p role="status" className="mb-3 text-sm text-text-secondary">
          Payout batch created: {created.id}. Current status: {created.status}. {decimalMoney(created.total_eur, "EUR")} recorded; payment is not confirmed by this action.
        </p>}
        {selectedReports.length > 0 && (
          <div className="mb-3 flex min-w-0 flex-wrap items-center justify-between gap-2">
            <p className="text-sm text-text-secondary">{selectedReports.length} selected across all pages · {decimalMoney(selectedTotal, "EUR")}</p>
            <Button size="sm" variant="ghost" disabled={frozen} onClick={() => setPicked({})}>Clear selection</Button>
          </div>
        )}
        {amountError && <p role="alert" className="mb-3 text-sm text-rose-700">{amountError}</p>}
        {staleVisibleSelection && <p role="alert" className="mb-3 text-sm text-rose-700">A selected report changed. Unselect it and review the current report before selecting it again.</p>}
        {selectedReports.length >= 1000 && <p className="mb-3 text-sm text-text-secondary">The selection limit is 1,000 reports. Create this batch before selecting more.</p>}
        <div className="mb-3 flex flex-wrap gap-2">
          <Button size="sm" variant="ghost" disabled={frozen || approvedQ.isFetching} onClick={() => void approvedQ.refetch()}>Refresh eligible reports</Button>
        </div>
        <div className="max-w-full overflow-x-auto" role="region" aria-label="Eligible expense reports" tabIndex={0}>
        <table className="w-full min-w-[36rem] text-sm">
          <thead>
            <tr className="text-left text-xs text-text-secondary">
              <th className="py-1"><span className="sr-only">Select report</span></th>
              <th scope="col" className="py-1 pr-3">Report</th>
              <th scope="col" className="py-1 pr-3">Employee</th>
              <th scope="col" className="py-1 pr-3 text-right">Original amount</th>
              <th scope="col" className="py-1 text-right">EUR payout</th>
            </tr>
          </thead>
          <tbody>
            {items.map((r) => (
              <tr key={r.id} className="border-t border-slate-100">
                <td className="py-1">
                  <input
                    type="checkbox"
                    checked={!!picked[r.id]}
                    disabled={frozen || !canApprove || (!picked[r.id] && (reportEUR(r) === null || !Number.isInteger(r.version) || r.version < 1 || selectedReports.length >= 1000))}
                    onChange={(e) => setPicked((current) => {
                      const next = { ...current };
                      if (e.target.checked) next[r.id] = { ...r };
                      else delete next[r.id];
                      return next;
                    })}
                    aria-label={`Select ${r.title}`}
                    aria-describedby={reportEUR(r) === null ? `eur-missing-${r.id}` : undefined}
                  />
                </td>
                <td className="py-1 pr-3"><Link to={`/expenses/${r.id}`} className="text-brand-600 hover:underline">{r.title}</Link></td>
                <td className="py-1 pr-3 text-slate-500">{r.employee_name}</td>
                <td className="py-1 pr-3 text-right tabular-nums">{decimalMoney(r.total, r.currency)}</td>
                <td className="py-1 text-right tabular-nums">{reportEUR(r) === null
                  ? <span id={`eur-missing-${r.id}`} className="text-text-secondary">EUR conversion unavailable</span>
                  : decimalMoney(reportEUR(r), "EUR")}</td>
              </tr>
            ))}
            {approvedQ.isPending && canApprove && <tr><td colSpan={5} className="py-3 text-text-secondary" role="status">Loading eligible reports…</td></tr>}
            {approvedQ.isError && (
              <tr>
                <td colSpan={5} className="py-3">
                  <ErrorState title="Couldn’t load the approved reports" onRetry={() => approvedQ.refetch()} />
                </td>
              </tr>
            )}
            {!approvedQ.isError && !approvedQ.isPending && items.length === 0 && (
              <tr>
                <td colSpan={5} className="py-3 text-text-secondary">
                  {approved && approved.total > 0 ? "No eligible reports on this page. Use Previous to review earlier reports." : "No approved reports awaiting payout."}
                </td>
              </tr>
            )}
          </tbody>
        </table>
        </div>
        {approved && <PayoutPages label="Eligible report pages" page={eligiblePage} total={approved.total} disabled={commandStage !== null || approvedQ.isFetching} onChange={setEligiblePage} />}
      </Card>

      {/* Existing batches */}
      <Card className="min-w-0">
        <h2 className="mb-3 text-sm font-semibold text-slate-700">Payout batches</h2>
        {paymentStorageError && <p role="alert" className="mb-3 text-sm text-rose-700">{paymentStorageError}</p>}
        {savedPayments.map(({ batchId, command, error }) => (
          <div key={batchId} role="group" aria-label={`Saved payment request ${batchId}`} className="mb-4 min-w-0 space-y-2 rounded-lg border border-slate-200 bg-slate-50 p-3">
            <p className="text-sm font-medium">A payment request needs checking.</p>
            {command && <p className="max-w-prose text-sm text-text-secondary">
              Batch {batchId} · Request reference {command.key} · {decimalMoney(command.payload.expected_total_eur, "EUR")}.
              Payment reference: {command.payload.reference || "Not provided (optional)"}.
              Keep this request until its receipt and current batch can be verified. It does not send money or establish bank confirmation.
            </p>}
            {(error || paymentErrors[batchId]) && <p role="alert" className="text-sm text-rose-700">{error || paymentErrors[batchId]}</p>}
            {command && <div className="flex min-w-0 flex-wrap gap-2">
              <Button size="sm" variant="secondary" disabled={!canApprove || !!paymentStages[batchId]}
                onClick={() => void checkPayment(batchId)}>
                {paymentKnown.has(batchId) ? "Refresh payment record" : "Check payment record"}
              </Button>
              {paymentMissing.has(batchId) && <Button size="sm" variant="secondary" disabled={!canApprove || !!paymentStages[batchId]}
                onClick={() => void checkPayment(batchId, true)}>Retry original payment request</Button>}
            </div>}
            {paymentStages[batchId] && <p role="status" className="text-sm text-text-secondary">
              {paymentStages[batchId] === "recording" ? "Recording the original payment statement…" : paymentStages[batchId] === "checking" ? "Checking the saved payment record…" : "Verifying the payment record and current batch…"}
            </p>}
          </div>
        ))}
        {cancelStorageError && <p role="alert" className="mb-3 text-sm text-rose-700">{cancelStorageError}</p>}
        {savedCancellations.map(({ batchId, command, error }) => (
          <div key={batchId} role="group" aria-label={`Saved cancellation ${batchId}`} className="mb-4 min-w-0 space-y-2 rounded-lg border border-slate-200 bg-slate-50 p-3 [overflow-wrap:anywhere]">
            <p className="text-sm font-medium">A cancellation needs checking.</p>
            <p className="text-sm">Batch: {batchId}.</p>
            {command && <p className="text-sm">Command reference: {command.key}. Reason: {command.payload.reason || "Not provided (optional)"}.</p>}
            <p className="text-sm text-text-secondary">Check the original request before another batch action. Checking only reads its outcome.</p>
            {(error || cancelErrors[batchId]) && <p role="alert" className="text-sm text-rose-700">{error || cancelErrors[batchId]}</p>}
            {command && <div className="flex flex-wrap gap-2">
              <Button size="sm" variant="secondary" disabled={!canApprove || !!cancelStages[batchId]}
                onClick={() => void checkCancellation(batchId)}>
                {cancelKnown.has(batchId) ? "Refresh cancellation" : "Check cancellation"}
              </Button>
              {cancelMissing.has(batchId) && <Button size="sm" variant="secondary" disabled={!canApprove || !!cancelStages[batchId]}
                onClick={() => void checkCancellation(batchId, true)}>Retry original cancellation</Button>}
            </div>}
            {cancelStages[batchId] && <p role="status" className="text-sm text-text-secondary">
              {cancelStages[batchId] === "recording" ? "Saving the original cancellation…" : cancelStages[batchId] === "checking" ? "Checking the saved cancellation…" : "Verifying the cancellation and current batch…"}
            </p>}
          </div>
        ))}
        <div className="space-y-3">
          {(batches ?? []).map((b) => (
            <div key={b.id} role="group" aria-label={`Payout batch ${b.id}`} className="min-w-0 rounded-lg border border-slate-200 p-3 [overflow-wrap:anywhere]">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex min-w-0 flex-wrap items-center gap-2">
                  <Badge tone={STATUS_TONE[b.status] ?? "neutral"}>{b.status}</Badge>
                  <span className="font-medium">{decimalMoney(b.total_eur, "EUR")}</span>
                  <span className="text-xs text-text-secondary">
                    {b.report_count} report{b.report_count === 1 ? "" : "s"} · {b.method}
                    {b.reference ? ` · ${b.reference}` : ""}
                  </span>
                </div>
                <div className="flex min-w-0 max-w-full flex-wrap items-center gap-2">
                  {b.status === "open" && (
                    <>
                      <input
                        className="w-40 max-w-full rounded-lg border border-slate-300 px-2 py-1 text-sm"
                        placeholder="Payment reference…" aria-label="Payment reference"
                        maxLength={200}
                        value={refs[b.id] ?? ""}
                        onChange={(e) => setRefs({ ...refs, [b.id]: e.target.value })}
                        disabled={batchIsBusy(b.id)}
                      />
                      <Button size="sm" loading={!!paymentStages[b.id]} disabled={!canApprove || batchIsBusy(b.id) || confirmationBatchId !== null || exportConfirmationVisible()} onClick={() => void recordPayment(b)}>
                        Record payment
                      </Button>
                      <Button size="sm" variant="ghost" aria-describedby={hasExportRecord(b) ? `cancel-export-${b.id}` : undefined}
                        onClick={() => void cancelBatch(b)} loading={!!cancelStages[b.id]}
                        disabled={!canApprove || hasExportRecord(b) || batchIsBusy(b.id) || confirmationBatchId !== null || exportConfirmationVisible()}>
                        Cancel
                      </Button>
                    </>
                  )}
                  {b.status !== "cancelled" && (
                    <>
                      <Button
                        size="sm"
                        variant="secondary"
                        disabled={batchIsBusy(b.id)}
                        onClick={() =>
                          doExport({
                            batch: b,
                            kind: "csv",
                            confirmReexport: false,
                            acknowledgeSkipped: false,
                          })
                        }
                      >
                        Export CSV
                      </Button>
                      <Button
                        size="sm"
                        variant="secondary"
                        disabled={batchIsBusy(b.id)}
                        title="ISO 20022 pain.001 credit-transfer file (employees with an IBAN on file)"
                        onClick={() =>
                          doExport({
                            batch: b,
                            kind: "sepa",
                            confirmReexport: false,
                            acknowledgeSkipped: false,
                          })
                        }
                      >
                        Export SEPA
                      </Button>
                    </>
                  )}
                </div>
              </div>
              {!hasSavedPayment(b.id) && paymentErrors[b.id] && <p role="alert" className="mt-2 text-sm text-rose-700">{paymentErrors[b.id]}</p>}
              {!hasSavedCancellation(b.id) && cancelErrors[b.id] && <p role="alert" className="mt-2 text-sm text-rose-700">{cancelErrors[b.id]}</p>}
              {cancelSaved.has(b.id) && <p role="status" className="mt-2 text-sm text-text-secondary">Cancellation saved. The reviewed reports were released from this batch.</p>}
              {paymentSaved.has(b.id) && <p role="status" className="mt-2 text-sm text-text-secondary">Payment record saved. Bank confirmation is unknown.</p>}
              {b.status === "paid" && <p className="mt-2 max-w-prose text-sm text-text-secondary">
                {b.payment_evidence?.basis === "operator_recorded"
                  ? "Operator-recorded payment. Bank confirmation is unknown."
                  : b.payment_evidence?.basis === "unavailable"
                    ? "Payment evidence could not be verified. Review the record before relying on it. Bank confirmation is unknown."
                    : "Legacy payment record; its evidence basis is unknown. Bank confirmation is unknown."}
              </p>}
              {b.status === "open" && hasExportRecord(b) && (
                <p id={`cancel-export-${b.id}`} className="mt-2 max-w-prose text-sm text-text-secondary">
                  Export information is recorded for this batch, so cancellation is blocked.
                  Check the bank file’s status with your bank or accountant before arranging
                  another payment. An export does not confirm payment.
                </p>
              )}
              {committed?.id === b.id ? (
                <p role="status" className="mt-2 text-sm text-text-secondary">
                  This batch’s creation result is being verified. Check the saved request before another batch action.
                </p>
              ) : cancelStages[b.id] && !hasSavedCancellation(b.id) ? (
                <p role="status" className="mt-2 text-sm text-text-secondary">
                  {cancelStages[b.id] === "preparing" ? "Preparing the current batch for cancellation review…" : "Saving the original cancellation…"}
                </p>
              ) : paymentStages[b.id] && !hasSavedPayment(b.id) ? (
                <p role="status" className="mt-2 text-sm text-text-secondary">
                  {paymentStages[b.id] === "preparing" ? "Preparing the current batch for payment review…" : "Recording the original payment statement…"}
                </p>
              ) : refreshRequired.has(b.id) ? (
                <div className="mt-2 space-y-2">
                  <p className="text-sm text-text-secondary">
                    The current batch status could not be verified. Refresh it before another
                    batch action; this check does not repeat the previous action.
                  </p>
                  <Button size="sm" variant="secondary" onClick={() => void refreshBatchStatus(b.id)}>
                    Refresh batch status
                  </Button>
                </div>
              ) : batchIsBusy(b.id) && !hasSavedPayment(b.id) && !hasSavedCancellation(b.id) && (
                <p role="status" className="mt-2 text-sm text-text-secondary">
                  Batch action in progress. Wait for its updated status.
                </p>
              )}
              <div className="mt-1 text-xs text-text-secondary">
                Created {shortDate(b.created_at)}
                {b.created_by ? ` by ${b.created_by}` : ""}
                {b.paid_at ? ` · recorded paid ${shortDate(b.paid_at)}` : ""}
                {b.export_count > 0
                  ? ` · exported ${b.export_count}× (first ${shortDate(b.exported_at!)}${
                      b.last_msg_id ? `, MsgId ${b.last_msg_id}` : ""
                    })`
                  : ""}
              </div>
            </div>
          ))}
          {batchesQ.isError && (
            <ErrorState title="Couldn’t load the payout batches" onRetry={() => batchesQ.refetch()} />
          )}
          {batchesQ.isPending && canApprove && <p role="status" className="text-sm text-text-secondary">Loading payout batches…</p>}
          {!batchesQ.isError && !batchesQ.isPending && (batches ?? []).length === 0 && (
            <p className="text-sm text-text-secondary">{batchesQ.data && batchesQ.data.total > 0 ? "No payout batches on this page. Use Previous to review earlier batches." : "No payout batches yet."}</p>
          )}
        </div>
        {batchesQ.data && <PayoutPages label="Payout batch pages" page={batchPage} total={batchesQ.data.total} disabled={batchesQ.isFetching} onChange={setBatchPage} />}
      </Card>

      <ConfirmDialog
        open={reexportAsk !== null && !batchIsBusy(reexportAsk.batch.id) && confirmationBatchId === null}
        onClose={() => setReexportAsk(null)}
        onConfirm={() => {
          const p = reexportAsk!;
          setReexportAsk(null);
          void doExport(p);
        }}
        title="Re-export bank file?"
        confirmLabel="Re-export with a new message id"
        tone="danger"
      >
        {reexportAsk && (
          <p className="text-sm text-slate-600">
            This batch was already exported{" "}
            {reexportAsk.batch.export_count > 0
              ? `${reexportAsk.batch.export_count}× (first ${shortDate(reexportAsk.batch.exported_at!)})`
              : ""}
            . Re-exporting produces a <strong>new file with a new message id</strong> — the bank
            will treat it as a separate payment instruction. Only continue if the previous file
            was never submitted.
          </p>
        )}
      </ConfirmDialog>

      <ConfirmDialog
        open={skippedAsk !== null && !batchIsBusy(skippedAsk.pending.batch.id) && confirmationBatchId === null && reexportAsk === null}
        onClose={() => setSkippedAsk(null)}
        onConfirm={() => {
          const p = skippedAsk!.pending;
          setSkippedAsk(null);
          void doExport(p);
        }}
        title="Some employees will not be paid"
        confirmLabel="I understand these will not be paid"
        tone="danger"
      >
        {skippedAsk && <p className="text-sm text-slate-600">{skippedAsk.detail}</p>}
      </ConfirmDialog>
    </div>
  );
}

function PayoutPages({ label, page, total, disabled, onChange }: {
  label: string; page: number; total: number; disabled: boolean; onChange: (page: number) => void;
}) {
  return <nav aria-label={label} className="mt-4 flex min-w-0 flex-wrap items-center justify-between gap-3 border-t border-slate-100 pt-3">
    <p className="text-sm text-text-secondary" aria-live="polite">{total} {label === "Eligible report pages" ? "eligible reports" : "payout batches"} · Page {page} of {Math.max(1, Math.ceil(total / PAGE_SIZE))}</p>
    <div className="flex flex-wrap gap-2">
      <Button size="sm" variant="secondary" disabled={disabled || page <= 1} onClick={() => onChange(page - 1)}>Previous</Button>
      <Button size="sm" variant="secondary" disabled={disabled || page * PAGE_SIZE >= total} onClick={() => onChange(page + 1)}>Next</Button>
    </div>
  </nav>;
}
