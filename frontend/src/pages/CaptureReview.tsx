import { CaptureReadingDetails } from "../components/CaptureReadingDetails";
import { useAuth } from "../auth/AuthContext";
import { CapturePagePane } from "../components/CapturePagePane";
import { CaptureProcessNav } from "../components/CaptureProcessNav";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import axios from "axios";
import { useDeferredValue, useEffect, useRef, useState } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
import { Button, ConfirmDialog, EmptyState, ErrorState, Skeleton, Spinner } from "../components/ui";
import { api, apiError } from "../lib/api";
import { captureStageLabel } from "../lib/captureStages";
import type { RegionHistory } from "../lib/captureRegions";
import { formatNumber, METHOD_STYLES, methodLabel, money } from "../lib/format";
import type {
  DuplicateReport,
  ExtractionResult,
  FieldProvenance,
  InvoiceCreate,
  InvoiceDetail,
  Vendor,
  VendorResolution,
} from "../lib/types";

/**
 * The extraction-review screen (E1.1): the source document side by side with what
 * was read from it — per field, with status (extracted/defaulted/missing),
 * confidence, original vs normalized vs reviewed values — plus duplicate warnings
 * and the confirm step that actually creates the invoice.
 *
 * §4.19: extraction output is ADVISORY. Nothing here writes to the invoice
 * record until the human confirms; corrections are recorded as provenance
 * (`reviewed_value`) through the audited review endpoint, never silently applied.
 */

const HEADER_FIELDS = [
  { key: "vendor_name", label: "Vendor", type: "text" },
  { key: "invoice_number", label: "Invoice number", type: "text" },
  { key: "issue_date", label: "Issue date", type: "date" },
  { key: "due_date", label: "Due date", type: "date" },
  { key: "currency", label: "Currency", type: "text" },
  // BT-13 (backlog N1). Mirrors the backend's `_HEADER_FIELDS`: capture records
  // provenance for it, so the reviewer must be able to see and correct it here
  // — otherwise the queue would show a row nobody can act on.
  { key: "po_reference", label: "PO reference", type: "text" },
  { key: "supplier_registration_number", label: "Supplier registration number", type: "text" },
  { key: "supplier_vat_id", label: "Supplier VAT ID", type: "text" },
  { key: "supplier_iban", label: "Captured supplier IBAN", type: "text" },
] as const;

type HeaderKey = (typeof HEADER_FIELDS)[number]["key"];

// Line-item provenance (E1.2): the eight per-line fields, mirroring the backend's
// extraction_fields rows with `line_index` set.
const LINE_FIELDS = [
  { key: "description", label: "Description", numeric: false },
  { key: "category", label: "Category", numeric: false },
  { key: "quantity", label: "Qty", numeric: true },
  { key: "unit_price", label: "Unit", numeric: true },
  { key: "amount", label: "Amount", numeric: true },
  { key: "tax_rate", label: "Tax %", numeric: true },
  { key: "captured_tax_amount", label: "Captured tax", numeric: true },
  { key: "captured_gross_amount", label: "Captured gross", numeric: true },
] as const;

type LineKey = (typeof LINE_FIELDS)[number]["key"];

/** One correction posted to the audited review endpoint — `line_index` targets a
 * line row; absent targets a header row. */
type Correction = { field: string; reviewed_value: string; line_index?: number };

const STATUS_STYLES: Record<string, string> = {
  extracted: "bg-slate-100 text-slate-600",
  defaulted: "bg-amber-100 text-amber-700",
  missing: "bg-rose-100 text-rose-700",
};

const STATUS_HELP: Record<string, string> = {
  extracted: "read from the document",
  defaulted: "not read from the document — filled with a default",
  missing: "not read from the document",
};

function draftValue(draft: InvoiceCreate, key: HeaderKey): string {
  const v = draft[key];
  return v == null ? "" : String(v);
}

function lineValue(draft: InvoiceCreate, i: number, key: LineKey): string {
  const v = draft.line_items[i]?.[key];
  return v == null ? "" : String(v);
}

function savedReview(value: unknown): RegionHistory {
  const snapshot = value as RegionHistory;
  if (!snapshot || !/^[0-9a-f]{64}$/.test(snapshot.review_fingerprint) ||
      !snapshot.current_draft || !Array.isArray(snapshot.current_draft.line_items) ||
      !Array.isArray(snapshot.items) || typeof snapshot.eligible !== "boolean") {
    throw new Error("The saved review could not be verified. Reload it before changing or confirming this capture.");
  }
  return snapshot;
}

function reviewDraft(snapshot: RegionHistory, runId: string): InvoiceCreate {
  return { ...snapshot.current_draft, extraction_run_id: runId,
    line_items: snapshot.current_draft.line_items.map((line) => ({ ...line })) };
}

function reviewedDraft(draft: InvoiceCreate, rows: FieldProvenance[]): InvoiceCreate {
  const next = { ...draft, line_items: draft.line_items.map((line) => ({ ...line })) };
  for (const field of rows) {
    if (field.reviewed_value == null) continue;
    if (field.line_index == null && HEADER_FIELDS.some((header) => header.key === field.field)) {
      (next as unknown as Record<string, string | null>)[field.field] = field.reviewed_value || null;
    } else if (field.line_index != null && next.line_items[field.line_index] && LINE_FIELDS.some((line) => line.key === field.field)) {
      (next.line_items[field.line_index] as unknown as Record<string, string | null>)[field.field] = field.field.startsWith("captured_") && field.reviewed_value === "" ? null : field.reviewed_value;
    }
  }
  return next;
}

/** Compact provenance caption; missing scores do not establish accuracy. */
function lineCellNote(row: FieldProvenance): string {
  const parts: string[] = [row.status];
  if (row.status === "extracted") {
    parts.push(
      row.confidence == null
        ? noScoreNote(row)
        : scoreNote(row),
    );
  }
  if (row.reviewed_value != null) parts.push(`corrected to “${row.reviewed_value}”`);
  return parts.join(" · ");
}

function scoreNote(row: FieldProvenance): string {
  const pct = Math.round(Number(row.confidence) * 100);
  const builtIn = row.provider === "pdf-ocr" || row.provider === "image-ocr" || row.provider === "pdf-text";
  return `${builtIn ? "method-based review score" : "provider score"}: ${pct}%`;
}

function noScoreNote(row: FieldProvenance): string {
  const structured = row.provider === "csv" || row.provider === "json" || row.provider === "einvoice" || row.provider === "pdf-einvoice";
  return structured ? "structured source — no probabilistic score" : "no provider score supplied";
}

function ConfidenceNote({ f }: { f: FieldProvenance }) {
  if (f.status !== "extracted") return null;
  if (f.confidence == null) {
    return <span className="text-slate-500">{noScoreNote(f)}</span>;
  }
  if (f.low_confidence) {
    return (
      <span className="font-medium text-amber-600">{scoreNote(f)} — please verify</span>
    );
  }
  return <span className="text-slate-500">{scoreNote(f)}</span>;
}

function OriginalSourcePane({ runId, filename }: { runId: string; filename: string | null }) {
  const { user, org } = useAuth();
  const src = useQuery({
    queryKey: ["captures", user?.id, org?.id, runId, "source"],
    staleTime: Infinity,
    gcTime: 0,
    retry: false,
    queryFn: async (): Promise<{ url: string; mime: string } | null> => {
      try {
        const res = await api.get(`/invoices/captures/${runId}/source`, { responseType: "blob" });
        const blob = res.data as Blob;
        const mime = String(res.headers["content-type"] || "application/octet-stream");
        return { url: URL.createObjectURL(new Blob([blob], { type: mime })), mime };
      } catch (e) {
        if (axios.isAxiosError(e) && e.response?.status === 404) return null;
        throw e;
      }
    },
  });

  // Revoke the object URL when it changes or the pane unmounts (gcTime 0 above
  // keeps the cache from handing back a revoked URL on remount).
  const url = src.data?.url;
  useEffect(() => {
    return () => {
      if (url) URL.revokeObjectURL(url);
    };
  }, [url]);

  const name = filename || "document";
  return (
    <div className="card p-0">
      <div className="flex items-center justify-between border-b border-slate-200 px-4 py-3">
        <h2 className="text-sm font-semibold text-slate-600">Original document</h2>
        {url && (
          <a href={url} download={name} className="text-sm font-medium text-brand-600 hover:underline">
            Download
          </a>
        )}
      </div>
      {src.isLoading && <Skeleton className="m-4 h-96 w-auto" />}
      {src.isError && (
        <ErrorState
          title="Couldn’t load the document"
          description="The extracted fields on the right are unaffected."
          onRetry={() => src.refetch()}
        />
      )}
      {src.data === null && (
        <EmptyState
          title="No stored document"
          description="This capture has no retrievable source file — review the fields against your own copy."
        />
      )}
      {src.data && src.data.mime.startsWith("application/pdf") && (
        <object
          data={src.data.url}
          type="application/pdf"
          className="h-[38rem] w-full"
          aria-label={`Original document ${name}`}
        >
          <EmptyState
            title="Preview unavailable"
            description="Your browser can’t display this PDF inline — use Download above."
          />
        </object>
      )}
      {src.data && src.data.mime.startsWith("image/") && (
        <img
          src={src.data.url}
          alt={`Original document ${name}`}
          className="max-h-[38rem] w-full object-contain p-2"
        />
      )}
      {src.data && !src.data.mime.startsWith("application/pdf") && !src.data.mime.startsWith("image/") && (
        <EmptyState
          title={name}
          description={`This ${src.data.mime} file has no inline preview — use Download above to open it.`}
        />
      )}
    </div>
  );
}

export default function CaptureReview() {
  const { runId = "" } = useParams<{ runId: string }>();
  const { cacheIdentity } = useAuth();
  return <CaptureReviewWorkspace key={`${cacheIdentity}:${runId}`} runId={runId} />;
}

function CaptureReviewWorkspace({ runId }: { runId: string }) {
  const { user, org, cacheIdentity, hasPerm } = useAuth();
  const { pathname } = useLocation();
  const scope = `${cacheIdentity}:${runId}`;
  const live = useRef(scope);
  useEffect(() => { live.current = scope; return () => { live.current = ""; }; }, [scope]);
  const [reviewEpoch, setReviewEpoch] = useState<string>(() => crypto.randomUUID());
  const liveReview = useRef(reviewEpoch);
  // A lazy destination can leave this page mounted after the browser has
  // navigated. Do not continue a held save into final creation in that gap.
  const active = (epoch = reviewEpoch) => live.current === scope && liveReview.current === epoch && window.location.pathname === pathname;
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [draft, setDraft] = useState<InvoiceCreate | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [dupeAck, setDupeAck] = useState(false);
  const [regionBusy, setRegionBusy] = useState(false);
  const [reviewFingerprint, setReviewFingerprint] = useState<string | null>(null);
  const [reviewConflict, setReviewConflict] = useState(false);
  const [loadingReview, setLoadingReview] = useState(false);

  // Poll the capture while the worker parses it (queued → running → parsed|failed).
  const poll = useQuery<ExtractionResult>({
    queryKey: ["captures", user?.id, org?.id, runId, "run", reviewEpoch],
    queryFn: async ({ signal }) => (await api.get(`/invoices/upload/${runId}`, { signal })).data,
    refetchInterval: (q) => {
      const s = q.state.data?.status;
      return s === "queued" || s === "running" || s === undefined ? 1500 : false;
    },
  });
  const parsed = poll.data?.status === "parsed";

  // The LIVE provenance rows — including reviewed values recorded earlier, so a
  // reload never loses sight of a correction.
  const fields = useQuery<FieldProvenance[]>({
    queryKey: ["captures", user?.id, org?.id, runId, "fields", reviewEpoch],
    queryFn: async ({ signal }) => (await api.get(`/invoices/captures/${runId}/fields`, { signal })).data,
    enabled: parsed,
  });

  // The fingerprint belongs to this exact saved draft. Background reads never
  // replace a local draft or advance its write precondition.
  const snapshot = useQuery<RegionHistory>({
    queryKey: ["capture-regions", cacheIdentity, runId, reviewEpoch], enabled: parsed, retry: false,
    queryFn: async ({ signal }) => savedReview((await api.get(`/invoices/captures/${runId}/region-reads`, { signal })).data),
  });

  // Seed only from one authoritative saved-review snapshot, including its base
  // fingerprint. Mixing an old parse with a newer fields read would create a
  // draft that no server revision ever contained.
  useEffect(() => {
    if (draft || !parsed || !snapshot.isSuccess || !fields.isSuccess || !snapshot.data || !fields.data) return;
    try {
      const verified = savedReview(snapshot.data);
      setDraft(reviewDraft(verified, runId));
      setReviewFingerprint(verified.review_fingerprint);
    } catch {
      setReviewConflict(true);
      setError("The saved review could not be verified. Reload it before changing or confirming this capture.");
    }
  }, [draft, parsed, snapshot.data, snapshot.isSuccess, fields.data, fields.isSuccess, runId]);

  // Duplicate candidates for the CURRENT (possibly corrected) invoice number.
  // The draft carries a vendor NAME; resolve it against the vendor master so the
  // backend can split exact (same supplier) from cross-supplier candidates.
  const vendors = useQuery<Vendor[]>({
    queryKey: ["vendors", user?.id, org?.id],
    queryFn: async () => (await api.get("/vendors")).data,
    enabled: parsed,
  });
  const vendorName = draft?.vendor_name?.trim().toLowerCase() ?? "";
  const vendorId = vendorName
    ? vendors.data?.find((v) => v.name.trim().toLowerCase() === vendorName)?.id
    : undefined;
  // H-3: say WHY this supplier was matched — or, when nothing matched exactly
  // and something nearly did, that nothing was chosen and confirming unchanged
  // will fork the master data. The server owns both the rule and the wording;
  // this screen renders them rather than composing advice of its own.
  //
  // Keyed off a DEFERRED copy of the name, not the live one. The vendor input
  // calls setDraft on every keystroke, and each resolve scans the supplier master
  // server-side — without this, typing a 20-character supplier name fires 20 of
  // those. The answer is only useful once the user stops typing anyway.
  const deferredVendorName = useDeferredValue(draft?.vendor_name ?? "");
  const resolution = useQuery<VendorResolution>({
    queryKey: ["vendors", user?.id, org?.id, "resolve", deferredVendorName],
    queryFn: async () =>
      (await api.get("/vendors/resolve", { params: { name: deferredVendorName } })).data,
    enabled: parsed && !!deferredVendorName.trim(),
  });

  const number = draft?.invoice_number?.trim() ?? "";
  // Gross total estimate (net + per-line tax), for the E1.4 scored-candidate
  // signal only — an advisory heuristic, not the server's authoritative total
  // (which is recomputed on save regardless — §4.10).
  const estimatedTotal = draft
    ? draft.line_items.reduce((sum, li) => {
        const net = Number(li.amount ?? Number(li.quantity) * Number(li.unit_price));
        const rate = Number(li.tax_rate ?? 0);
        return sum + net * (1 + rate / 100);
      }, 0)
    : 0;
  const dupes = useQuery<DuplicateReport>({
    queryKey: [
      "captures",
      user?.id,
      org?.id,
      runId,
      "dupes",
      number,
      vendorId ?? null,
      estimatedTotal,
      draft?.issue_date ?? null,
      draft?.currency ?? null,
    ],
    queryFn: async () =>
      (
        await api.get("/invoices/duplicate-candidates", {
          params: {
            invoice_number: number,
            ...(vendorId ? { vendor_id: vendorId } : {}),
            ...(vendorId && estimatedTotal > 0 && draft?.currency && draft?.issue_date
              ? {
                  total: estimatedTotal.toFixed(2),
                  currency: draft.currency,
                  issue_date: draft.issue_date,
                }
              : {}),
          },
        })
      ).data,
    enabled: parsed && number.length > 0,
  });

  const fieldRow = (key: HeaderKey) =>
    fields.data?.find((f) => f.field === key && f.line_index == null);
  const lineRow = (i: number, key: LineKey) =>
    fields.data?.find((f) => f.field === key && f.line_index === i);

  /** Fields (header AND line) whose edited value differs from the captured
   * record — recorded as `reviewed_value` provenance through the audited
   * endpoint, never silently applied. */
  const corrections = (): Correction[] => {
    if (!draft || !fields.data) return [];
    const out: Correction[] = [];
    for (const h of HEADER_FIELDS) {
      const row = fieldRow(h.key);
      if (!row) continue;
      const current = draftValue(draft, h.key);
      const captured = row.reviewed_value ?? row.value ?? "";
      if (current !== (captured || "")) out.push({ field: h.key, reviewed_value: current });
    }
    draft.line_items.forEach((_, i) => {
      for (const l of LINE_FIELDS) {
        const row = lineRow(i, l.key);
        if (!row) continue;
        const current = lineValue(draft, i, l.key);
        const captured = row.reviewed_value ?? row.value ?? "";
        if (current !== (captured || "")) {
          out.push({ field: l.key, line_index: i, reviewed_value: current });
        }
      }
    });
    return out;
  };
  const pending = corrections();

  function writeError(e: unknown, epoch = reviewEpoch) {
    if (!active(epoch)) return;
    const conflict = axios.isAxiosError(e) && e.response?.status === 409;
    const uncertain = axios.isAxiosError(e) && (!e.response || e.response.status >= 500);
    if (conflict || uncertain) setReviewConflict(true);
    setError(conflict
      ? "The saved review changed. Your local draft is still shown. Reload the saved review before saving or confirming again."
      : uncertain ? "The save outcome is unconfirmed. Your local draft is still shown. Reload the saved review to check before requesting another change."
      : apiError(e));
  }

  async function recordCorrections(fix: Correction[], fingerprint: string, epoch: string) {
    const response = await api.post<FieldProvenance[]>(`/invoices/captures/${runId}/review`, {
      fields: fix, expected_review_fingerprint: fingerprint,
    });
    if (!active(epoch)) throw new Error("This capture review is no longer open.");
    const nextFingerprint = response.headers["x-capture-review-fingerprint"];
    if (typeof nextFingerprint !== "string" || !/^[0-9a-f]{64}$/.test(nextFingerprint) || !Array.isArray(response.data)) {
      setReviewConflict(true);
      throw new Error("The corrections were accepted, but their saved revision could not be verified. Reload the saved review before continuing.");
    }
    setReviewFingerprint(nextFingerprint);
    qc.setQueryData(["captures", user?.id, org?.id, runId, "fields", epoch], response.data);
    setDraft((current) => current ? reviewedDraft(current, response.data) : current);
    return { fingerprint: nextFingerprint, fields: response.data };
  }

  const saveCorrections = useMutation({
    mutationFn: async (epoch: string) => {
      if (!active(epoch) || !reviewFingerprint || reviewConflict) throw new Error("Reload the saved review before recording corrections.");
      return recordCorrections(corrections(), reviewFingerprint, epoch);
    },
    onSuccess: (_data, epoch) => {
      if (!active(epoch)) return;
      setError(null);
      setNotice("Corrections recorded — the original capture is kept alongside them.");
      qc.invalidateQueries({ queryKey: ["captures", "queue"] });
    },
    onError: (e, epoch) => writeError(e, epoch),
  });

  const confirm = useMutation({
    mutationFn: async (epoch: string) => {
      if (!active(epoch) || !draft || !reviewFingerprint || reviewConflict) throw new Error("Reload the saved review before confirming this capture.");
      let fingerprint = reviewFingerprint;
      let submitted = { ...draft, line_items: draft.line_items.map((line) => ({ ...line })) };
      // Record any unsaved corrections first so the provenance trail matches
      // what is about to be saved, then create the invoice from the draft.
      const fix = corrections();
      if (fix.length > 0) {
        const saved = await recordCorrections(fix, fingerprint, epoch);
        fingerprint = saved.fingerprint;
        submitted = reviewedDraft(submitted, saved.fields);
      }
      if (!active(epoch)) throw new Error("This capture review is no longer open.");
      return (await api.post<InvoiceDetail>("/invoices", { ...submitted, extraction_run_id: runId, capture_review_fingerprint: fingerprint }))
        .data;
    },
    onSuccess: (inv, epoch) => {
      if (!active(epoch)) return;
      qc.invalidateQueries({ queryKey: ["captures", "queue"] });
      navigate(`/invoices/${inv.id}`);
    },
    onError: (e, epoch) => {
      if (!active(epoch)) return;
      setDupeAck(false);
      writeError(e, epoch);
    },
  });

  const retry = useMutation({
    mutationFn: async (epoch: string) => {
      if (!active(epoch)) throw new Error("This capture review is no longer open.");
      return (await api.post(`/invoices/upload/${runId}/retry`, {})).data;
    },
    onSuccess: (_data, epoch) => {
      if (!active(epoch)) return;
      // Old requests may still finish, but their cache keys and callbacks
      // belong to the retired review. The new epoch starts with fresh reads.
      liveReview.current = crypto.randomUUID();
      setReviewEpoch(liveReview.current);
      void qc.cancelQueries({ queryKey: ["captures", user?.id, org?.id, runId, "run", epoch], exact: true });
      void qc.cancelQueries({ queryKey: ["captures", user?.id, org?.id, runId, "fields", epoch], exact: true });
      void qc.cancelQueries({ queryKey: ["capture-regions", cacheIdentity, runId, epoch], exact: true });
      confirm.reset(); saveCorrections.reset();
      setError(null);
      setNotice(null); setDupeAck(false); setLoadingReview(false); setRegionBusy(false);
      setDraft(null);
      setReviewFingerprint(null); setReviewConflict(false);
      qc.invalidateQueries({ queryKey: ["captures", user?.id, org?.id, runId] });
      qc.invalidateQueries({ queryKey: ["capture-regions", cacheIdentity, runId] });
    },
    onError: (e, epoch) => { if (active(epoch)) setError(apiError(e)); },
  });

  const hasRequiredFields = !!draft && !!draft.vendor_name?.trim() && !!draft.invoice_number.trim() && !!draft.issue_date;
  const reviewBlocked = !reviewFingerprint || snapshot.isError || loadingReview || reviewConflict;
  const canConfirm = hasRequiredFields && !reviewBlocked;

  const onConfirmClick = () => {
    if (regionBusy || confirm.isPending || saveCorrections.isPending || loadingReview || reviewConflict || snapshot.isError || !reviewFingerprint) return;
    if ((dupes.data?.exact.length ?? 0) > 0) setDupeAck(true);
    else confirm.mutate(reviewEpoch);
  };

  async function reloadRegionApplication() {
    const [nextRun, nextFields, nextSnapshot] = await Promise.all([poll.refetch({ throwOnError: true }), fields.refetch({ throwOnError: true }), snapshot.refetch({ throwOnError: true })]);
    if (!active()) return;
    if (nextRun.data?.status !== "parsed" || !nextFields.data || !nextSnapshot.data) throw new Error("The saved capture could not be verified.");
    const verified = savedReview(nextSnapshot.data);
    setDraft(reviewDraft(verified, runId)); setReviewFingerprint(verified.review_fingerprint);
    setReviewConflict(false); setError(null);
  }

  async function reloadSavedReview() {
    setLoadingReview(true);
    try {
      await reloadRegionApplication();
      if (active()) { setNotice("Saved review loaded. Check its values before recording or confirming changes."); setDupeAck(false); }
    } catch {
      if (active()) setError("Couldn’t reload the saved review. Your local draft is still shown; try again before saving or confirming.");
    } finally { if (active()) setLoadingReview(false); }
  }

  const heading = (
    <>
      <CaptureProcessNav />
    <div className="flex flex-wrap items-end justify-between gap-4">
      <div className="min-w-0 flex-1">
        <nav aria-label="Breadcrumb" className="text-xs text-text-secondary">
          <Link to="/captures" className="hover:text-brand-600 hover:underline">
            Captures
          </Link>{" "}
          / review
        </nav>
        <h1 className="break-words text-2xl font-semibold tracking-tight">
          {poll.data?.draft?.draft.source_filename || "Capture review"}
        </h1>
        <p className="text-sm text-slate-500">
          Check what was read from the document before it becomes an invoice. Captured supplier IBAN is document evidence; payments use approved supplier bank details.
        </p>
      </div>
      {poll.data?.method && poll.data.method !== "unknown" && (
        <span className={`badge max-w-full shrink-0 whitespace-normal ${METHOD_STYLES[poll.data.method] ?? "bg-slate-100 text-slate-600"}`}>
          Read via {methodLabel(poll.data.method)}
        </span>
      )}
    </div>
      {poll.data && <CaptureReadingDetails channel="upload" ownerId={runId} revision={`${reviewEpoch}:${poll.data.status}:${poll.data.reading_observation?.id ?? "unknown"}`} />}
    </>
  );

  if (poll.isError && !poll.data) {
    return (
      <div className="space-y-6">
        {heading}
        <div className="card p-0">
          <ErrorState title="Couldn’t load this capture" onRetry={() => poll.refetch()} />
        </div>
      </div>
    );
  }
  if (poll.isLoading || !poll.data) {
    return (
      <div className="space-y-6">
        {heading}
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  const run = poll.data;

  if (run.status === "queued" || run.status === "running") {
    return (
      <div className="space-y-6">
        {heading}
        <div className="card flex items-center gap-3 py-8" role="status" aria-live="polite">
          <Spinner />
          <div className="min-w-0 flex-1">
            {/* WO-X — the phase the parser is actually in, and on a scan the page
                it has reached. A 40-page document used to say exactly what a
                3-page one said, so there was no way to tell a long job from a
                stuck one. */}
            <p className="text-sm font-medium text-slate-700">
              {captureStageLabel(run.stage, run.pages_done ?? 0, run.pages_total)}…
            </p>
            <p className="text-sm text-slate-500">
              Parsing runs on the server — this page updates itself when the draft is ready.
            </p>
            {typeof run.percent === "number" && (
              <div
                className="mt-2 h-1.5 w-full max-w-sm overflow-hidden rounded-full bg-slate-100"
                role="progressbar"
                aria-valuenow={run.percent}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-label="Pages read"
              >
                <div
                  className="h-full rounded-full bg-brand-500 transition-[width]"
                  style={{ width: `${run.percent}%` }}
                />
              </div>
            )}
          </div>
        </div>
      </div>
    );
  }

  if (run.status === "failed") {
    return (
      <div className="space-y-6">
        {heading}
        {error && (
          <div role="alert" className="rounded-lg border border-rose-200 bg-rose-50 px-4 py-2 text-sm text-rose-700">
            {error}
          </div>
        )}
        <div className="card p-0">
          <ErrorState
            title="We couldn’t read this document"
            description={run.error || "The file could not be parsed."}
            onRetry={() => retry.mutate(reviewEpoch)}
            retryLabel={retry.isPending ? "Re-queuing…" : "Retry extraction"}
          />
        </div>
      </div>
    );
  }

  if (run.status === "saved") {
    return (
      <div className="space-y-6">
        {heading}
        <div className="card">
          <p className="text-sm text-slate-600">
            This capture has already been confirmed and saved as an invoice.
          </p>
          <Link to="/invoices" className="mt-2 inline-block text-sm font-medium text-brand-600 hover:underline">
            Go to invoices →
          </Link>
        </div>
      </div>
    );
  }

  const warnings = run.draft?.warnings ?? [];
  const exact = dupes.data?.exact ?? [];
  const crossSupplier = dupes.data?.cross_supplier ?? [];
  const scored = dupes.data?.scored ?? [];
  const subtotal = draft
    ? draft.line_items.reduce(
        (sum, li) => sum + Number(li.amount ?? Number(li.quantity) * Number(li.unit_price)),
        0,
      )
    : 0;
  // This is source-currency review money, not an EUR conversion. Incomplete
  // currency edits must remain readable without defaulting to EUR or throwing.
  const subtotalCurrency = typeof draft?.currency === "string" ? draft.currency.trim() : "";
  const unknownCurrency = subtotalCurrency.toUpperCase() === "XXX";
  const subtotalLabel = /^[A-Za-z]{3}$/.test(subtotalCurrency) && !unknownCurrency
    ? money(subtotal, subtotalCurrency.toUpperCase())
    : `${formatNumber(subtotal, 2)} · ${!subtotalCurrency ? "Currency not set" : unknownCurrency ? "Unknown currency (XXX)" : "Invalid currency"}`;

  return (
    <div className="space-y-6">
      {heading}
      {poll.isError && <ErrorState title="Couldn’t refresh the saved capture"
        description="The displayed values are the last loaded copy. Editing is paused; check any pending apply outcome before continuing."
        onRetry={() => void poll.refetch()} />}
      {snapshot.isPending && <p role="status" className="text-sm text-slate-500">Loading the saved review before editing…</p>}
      {snapshot.isError && <ErrorState title="Couldn’t load the saved review"
        description="Your displayed draft is kept. Reload the saved review before recording or confirming changes."
        onRetry={() => void reloadSavedReview()} />}
      {(reviewConflict || snapshot.isError) && <div className="card space-y-2 border-amber-200 bg-amber-50 p-4">
        <p className="text-sm text-amber-800">The local draft remains visible. Reloading replaces it with the current saved review.</p>
        <Button variant="secondary" size="sm" disabled={loadingReview || regionBusy || confirm.isPending || saveCorrections.isPending} onClick={() => void reloadSavedReview()}>Reload saved review</Button>
      </div>}

      <section aria-label="Capture review summary" className="card space-y-3">
        <h2 className="text-sm font-semibold text-slate-700">Review summary</h2>
        {fields.isError ? (
          <p role="alert" className="text-sm text-rose-600">Review counts unavailable. Reload field provenance in Extracted fields below.</p>
        ) : !fields.data ? (
          <p role="status" className="text-sm text-slate-500">Loading field review status…</p>
        ) : fields.data.length === 0 ? (
          <p className="text-sm text-slate-500">No field provenance was recorded for this capture. Check every value against the original.</p>
        ) : (
          <dl className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
            {[
              ["Read from source", fields.data.filter((f) => f.status === "extracted").length],
              ["Filled defaults", fields.data.filter((f) => f.status === "defaulted").length],
              ["Not read", fields.data.filter((f) => f.status === "missing").length],
              ["Corrections saved", fields.data.filter((f) => f.reviewed_value != null).length],
            ].map(([label, value]) => (
              <div key={label}><dt className="text-slate-500">{label}</dt><dd className="text-lg font-semibold tabular-nums">{value}</dd></div>
            ))}
          </dl>
        )}
        <p className="text-xs text-slate-500">Counts include header and line fields. Saved corrections overlap the source/default/missing counts; they do not measure accuracy.</p>
        {pending.length > 0 && <p className="text-sm font-medium text-brand-600">{pending.length} unsaved field edit(s) — use Save corrections to record them.</p>}
        {fields.data?.some((f) => f.provider === "pdf-ocr" || f.provider === "image-ocr" || f.provider === "pdf-text") && (
          <p className="text-xs text-amber-700">The built-in OCR reader uses a 55% method-based review score; text interpretation uses 85%. These are review indicators, not measured recognition accuracy. Verify the values against the original document.</p>
        )}
      </section>

      {error && (
        <div role="alert" className="rounded-lg border border-rose-200 bg-rose-50 px-4 py-2 text-sm text-rose-700">
          {error}
        </div>
      )}
      {notice && !error && (
        <div className="rounded-lg border border-emerald-200 bg-emerald-50 px-4 py-2 text-sm text-emerald-700">
          {notice}
        </div>
      )}
      {warnings.length > 0 && (
        <ul className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-700">
          {warnings.map((w, i) => (
            <li key={i}>• {w}</li>
          ))}
        </ul>
      )}
      {resolution.data && (
        <div
          className={
            "rounded-lg border px-4 py-3 text-sm " +
            (resolution.data.needs_decision
              ? "border-amber-200 bg-amber-50 text-amber-800"
              : "border-slate-200 bg-slate-50 text-slate-600")
          }
        >
          <p>{resolution.data.reason}</p>
          <p className="mt-1">{resolution.data.outcome}</p>
          {resolution.data.candidates.length > 0 && (
            <ul className="mt-2 space-y-1">
              {resolution.data.candidates.map((c) => (
                <li key={c.vendor_id} className="flex flex-wrap items-center gap-2">
                  <span>{c.reason}</span>
                  <button
                    type="button"
                    disabled={!hasPerm("invoice.write") || regionBusy || confirm.isPending || saveCorrections.isPending || poll.isError || fields.isError || reviewBlocked}
                    className="font-medium underline"
                    onClick={() =>
                      setDraft((d) => (d ? { ...d, vendor_name: c.name } : d))
                    }
                  >
                    Use “{c.name}” instead
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      {(exact.length > 0 || crossSupplier.length > 0) && (
        <div role="alert" className="rounded-lg border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">
          <p className="font-medium">
            {exact.length > 0
              ? `Possible duplicate: ${exact.length === 1 ? "an invoice" : `${exact.length} invoices`} with number ${number} already ${exact.length === 1 ? "exists" : "exist"} for this supplier.`
              : `Heads up: ${crossSupplier.length === 1 ? "another supplier has" : `${crossSupplier.length} other suppliers have`} an invoice numbered ${number}.`}
          </p>
          <ul className="mt-1 space-y-0.5">
            {[...exact, ...crossSupplier].slice(0, 5).map((c) => (
              <li key={c.invoice_id}>
                <Link to={`/invoices/${c.invoice_id}`} className="underline">
                  {c.vendor_name} · {c.invoice_number} · {money(c.total, c.currency)}
                </Link>
              </li>
            ))}
          </ul>
        </div>
      )}
      {scored.length > 0 && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-700">
          <p className="font-medium">
            Worth a look: {scored.length === 1 ? "a similar invoice" : `${scored.length} similar invoices`} from
            this vendor — same amount and a nearby date, under a different number. Advisory only; nothing
            is blocked.
          </p>
          <ul className="mt-1 space-y-0.5">
            {scored.slice(0, 5).map((c) => (
              <li key={c.invoice_id}>
                <Link to={`/invoices/${c.invoice_id}`} className="underline">
                  {c.vendor_name} · {c.invoice_number} · {money(c.total, c.currency)}
                </Link>{" "}
                <span className="text-amber-600">— {c.reason}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-2">
        <CapturePagePane runId={runId} filename={run.draft?.draft.source_filename ?? null} fallback={<OriginalSourcePane runId={runId} filename={run.draft?.draft.source_filename ?? null} />}
          region={draft && fields.data && hasPerm("invoice.write") ? { draft, fields: fields.data, disabled: confirm.isPending || saveCorrections.isPending || regionBusy || poll.isError || fields.isError || !reviewFingerprint || loadingReview || reviewConflict,
            dirty: pending.length > 0, onBusyChange: (busy) => { if (active()) setRegionBusy(busy); }, onApplied: reloadRegionApplication } : undefined} />

        <fieldset disabled={!hasPerm("invoice.write") || regionBusy || confirm.isPending || saveCorrections.isPending || poll.isError || fields.isError || reviewBlocked} className="min-w-0 space-y-6">
          {!hasPerm("invoice.write") && <p className="text-sm text-slate-500">Read-only access. You can inspect the original and captured values; recording changes requires invoice editing permission.</p>}
          <div className="card space-y-4">
            <h2 className="text-sm font-semibold text-slate-600">Extracted fields</h2>
            {fields.isError && (
              <ErrorState
                title="Couldn’t load field provenance"
                onRetry={() => fields.refetch()}
              />
            )}
            {(fields.isLoading || !draft) && !fields.isError && <Skeleton className="h-64 w-full" />}
            {draft &&
              fields.data &&
              HEADER_FIELDS.map((h) => {
                const row = fieldRow(h.key);
                const changed = pending.some((p) => p.field === h.key);
                return (
                  <div key={h.key}>
                    <label className="label" htmlFor={`fld-${h.key}`}>
                      {h.label}
                    </label>
                    <input
                      id={`fld-${h.key}`}
                      className={`input ${row?.low_confidence ? "border-amber-400 bg-amber-50/50" : ""}`}
                      type={h.type}
                      value={draftValue(draft, h.key)}
                      aria-describedby={row ? `prov-${h.key}` : undefined}
                      onChange={(e) =>
                        setDraft({
                          ...draft,
                          [h.key]: e.target.value === "" ? null : e.target.value,
                        } as InvoiceCreate)
                      }
                    />
                    {row && (
                      <p id={`prov-${h.key}`} className="mt-1 flex flex-wrap items-center gap-2 text-xs">
                        <span className={`badge ${STATUS_STYLES[row.status] ?? "bg-slate-100 text-slate-600"}`}>
                          {row.status}
                        </span>
                        <span className="text-text-secondary">{STATUS_HELP[row.status] ?? ""}</span>
                        <ConfidenceNote f={row} />
                        {row.provider && <span className="text-text-secondary">via {row.provider}</span>}
                        {row.original_value != null &&
                          row.normalized_value != null &&
                          row.original_value !== row.normalized_value && (
                            <span className="text-slate-500">
                              read “{row.original_value}” → normalized “{row.normalized_value}”
                            </span>
                          )}
                        {row.reviewed_value != null && (
                          <span className="text-emerald-600">
                            corrected to “{row.reviewed_value}” (original kept)
                          </span>
                        )}
                        {changed && <span className="font-medium text-brand-600">edited — not yet recorded</span>}
                      </p>
                    )}
                  </div>
                );
              })}
            {draft && fields.data && (
              <div className="flex items-center justify-between border-t border-slate-100 pt-3">
                <p className="text-xs text-text-secondary">
                  Corrections are recorded next to the machine’s values — the capture itself is never
                  rewritten.
                </p>
                <Button
                  variant="secondary"
                  size="sm"
                  disabled={pending.length === 0 || saveCorrections.isPending || regionBusy}
                  loading={saveCorrections.isPending}
                  onClick={() => saveCorrections.mutate(reviewEpoch)}
                >
                  Save corrections
                </Button>
              </div>
            )}
          </div>

          {draft && (
            <div className="card space-y-3">
              <h2 className="text-sm font-semibold text-slate-600">Line items</h2>
              <p className="text-xs text-slate-500">Captured tax and gross are source evidence, separate from calculated totals. Leave absent values blank; calculation is not extraction.</p>
              <div className="overflow-x-auto rounded-lg border border-slate-200">
                <table className="w-full text-sm">
                  <thead className="bg-slate-50 text-left text-xs uppercase text-slate-500">
                    <tr>
                      {LINE_FIELDS.map((l) => (
                        <th
                          key={l.key}
                          scope="col"
                          className={`px-2 py-2 ${l.numeric ? "text-right" : ""}`}
                        >
                          {l.label}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {draft.line_items.map((_, i) => (
                      <tr key={i} className="border-t border-slate-100">
                        {LINE_FIELDS.map((l) => {
                          const row = lineRow(i, l.key);
                          return (
                            <td
                              key={l.key}
                              className={`px-1 py-1 ${l.key === "description" ? "min-w-[10rem]" : ""}`}
                            >
                              <input
                                className={`input px-2 py-1 text-sm ${l.numeric ? "text-right" : ""} ${
                                  row?.low_confidence ? "border-amber-400 bg-amber-50/50" : ""
                                }`}
                                aria-label={`Line ${i + 1} ${l.label}`}
                                title={row ? lineCellNote(row) : undefined}
                                value={lineValue(draft, i, l.key)}
                                onChange={(e) => {
                                  const items = draft.line_items.map((x) => ({ ...x }));
                                  (items[i] as unknown as Record<string, string | null>)[l.key] =
                                    (l.key === "amount" || l.key.startsWith("captured_")) && e.target.value === ""
                                      ? null
                                      : e.target.value;
                                  setDraft({ ...draft, line_items: items });
                                }}
                              />
                            </td>
                          );
                        })}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {(() => {
                // Cells that deserve a human look: low-confidence reads and
                // recorded corrections — same vocabulary as the header fields.
                const notes = draft.line_items.flatMap((_, i) =>
                  LINE_FIELDS.flatMap((l) => {
                    const row = lineRow(i, l.key);
                    if (!row || (!row.low_confidence && row.reviewed_value == null)) return [];
                    return [{ i, label: l.label, row }];
                  }),
                );
                if (notes.length === 0) return null;
                return (
                  <ul className="space-y-0.5 text-xs">
                    {notes.map(({ i, label, row }) => (
                      <li key={`${i}-${row.field}`} className="flex flex-wrap items-center gap-2">
                        <span className="text-slate-500">
                          Line {i + 1} · {label}
                        </span>
                        {row.low_confidence && (
                          <span className="font-medium text-amber-600">
                            {row.confidence != null
                              ? `${scoreNote(row)} — please verify`
                              : "needs a look"}
                          </span>
                        )}
                        {row.reviewed_value != null && (
                          <span className="text-emerald-600">
                            corrected to “{row.reviewed_value}” (original “{row.value ?? ""}” kept)
                          </span>
                        )}
                      </li>
                    ))}
                  </ul>
                );
              })()}
              <p className="text-xs text-text-secondary">
                Each cell carries the same provenance as the fields above — amber means it needs
                verification against the document. Edits are recorded as
                corrections next to the machine’s values; totals are recomputed on the server when
                you confirm.
              </p>
              <div className="flex items-center justify-between">
                <span className="text-sm text-slate-500">Net of tax · subtotal {subtotalLabel}</span>
                <Button
                  disabled={!canConfirm || confirm.isPending || saveCorrections.isPending || regionBusy}
                  loading={confirm.isPending}
                  onClick={onConfirmClick}
                >
                  Confirm &amp; create invoice
                </Button>
              </div>
              {!hasRequiredFields && (
                <p className="text-xs text-amber-600">
                  Set a vendor, an invoice number and an issue date before confirming.
                </p>
              )}
            </div>
          )}
        </fieldset>
      </div>

      <ConfirmDialog
        open={dupeAck}
        onClose={() => setDupeAck(false)}
        onConfirm={() => { if (!regionBusy && !saveCorrections.isPending && !reviewBlocked) confirm.mutate(reviewEpoch); }}
        title="Possible duplicate invoice"
        confirmLabel="Create anyway"
        tone="danger"
        loading={confirm.isPending}
      >
        <p className="text-sm text-slate-600">
          {exact.length === 1 ? "An invoice" : `${exact.length} invoices`} with number{" "}
          <span className="font-medium">{number}</span> from this supplier already{" "}
          {exact.length === 1 ? "exists" : "exist"}. Duplicate detection is advisory — you can still
          create this invoice if it is genuinely a different document.
        </p>
      </ConfirmDialog>
    </div>
  );
}
