import { useQuery } from "@tanstack/react-query";
import { useEffect } from "react";
import { useLocation, useSearchParams } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { Badge, Button, Card, EmptyState, QueryState, Skeleton } from "../components/ui";
import { api } from "../lib/api";
import type { DocumentEntry } from "../lib/types";
import { formatDate } from "../lib/format";

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

const KIND_TONE: Record<string, "success" | "warning" | "neutral"> = {
  invoice: "success",
  receipt: "warning",
};

const PAGE_SIZE = 100;
interface DocumentPage { items: DocumentEntry[]; total: number; kinds: string[] }
function registryPage(raw: string | null): number {
  const value = raw && /^[1-9]\d*$/.test(raw) ? Number(raw) : 1;
  return Number.isSafeInteger(value) && value <= Math.floor(Number.MAX_SAFE_INTEGER / PAGE_SIZE) ? value : 1;
}

/** Read-only registry of every stored original in the workspace (admin-gated on
 * the server — it spans every user's uploads). Metadata only: the bytes are
 * served by each owner's surface (invoice, expense, partner…), never from here. */
export default function DocumentsPage() {
  const { user, org } = useAuth();
  const location = useLocation();
  const [params, setParams] = useSearchParams();
  const kind = params.get("kind") ?? "";
  const rawPage = params.get("documents_page");
  const page = registryPage(rawPage);

  const docs = useQuery<DocumentPage>({
    queryKey: ["documents", "page", user?.id, org?.id, kind, page],
    enabled: !!user?.id && !!org?.id,
    queryFn: async ({ signal }) => {
      const result = (await api.get("/documents/page", {
        params: { limit: PAGE_SIZE, offset: (page - 1) * PAGE_SIZE, ...(kind ? { kind } : {}) }, signal,
      })).data as DocumentPage;
      if (!result || !Array.isArray(result.items) || result.items.length > PAGE_SIZE ||
        !Number.isSafeInteger(result.total) || result.total < 0 ||
        !Array.isArray(result.kinds) || !result.kinds.every((entry) => typeof entry === "string")) {
        throw new Error("Invalid document registry page");
      }
      return result;
    },
    retry: false,
  });
  const kinds = Array.from(new Set([...(docs.data?.kinds ?? []), ...(kind ? [kind] : [])])).sort();
  const lastPage = docs.data ? Math.max(1, Math.ceil(docs.data.total / PAGE_SIZE)) : null;
  const clamping = docs.isSuccess && !docs.isFetching && lastPage !== null && page > lastPage;
  const changePage = (next: number) => setParams((current) => {
    const updated = new URLSearchParams(current); updated.set("documents_page", String(next)); return updated;
  }, { state: location.state });
  useEffect(() => {
    const next = clamping ? lastPage : page;
    if (next === null || (!clamping && (rawPage === null || rawPage === String(page)))) return;
    setParams((current) => {
      if (registryPage(current.get("documents_page")) !== page) return current;
      const updated = new URLSearchParams(current); updated.set("documents_page", String(next)); return updated;
    }, { replace: true, state: location.state });
  }, [clamping, lastPage, page, rawPage, setParams, location.state]);

  return (
    <div className="min-w-0 space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Documents</h1>
        <p className="text-text-secondary">
          Every stored original this workspace holds, with its integrity hash. This registry is
          read-only — documents are added by uploads and downloaded from their owning record.
        </p>
      </div>

      <div className="flex items-center gap-2 text-sm text-text-secondary">
        <label htmlFor="document-kind">Kind</label>
        <select
          id="document-kind"
          className="min-h-11 rounded-lg border border-slate-300 px-2 py-1 text-sm focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus sm:min-h-8"
          value={kind}
          disabled={docs.isFetching || clamping}
          onChange={(e) => {
            const selected = e.target.value;
            setParams((current) => {
              const updated = new URLSearchParams(current);
              if (selected) updated.set("kind", selected); else updated.delete("kind");
              updated.set("documents_page", "1"); return updated;
            }, { state: location.state });
          }}
        >
          <option value="">All kinds</option>
          {kinds.map((k) => (
            <option key={k} value={k}>
              {k}
            </option>
          ))}
        </select>
      </div>

      <Card>
        <nav aria-label="Document registry pages" className="mb-4 flex flex-wrap items-center gap-2 text-sm text-text-secondary">
          {docs.isSuccess && !docs.isFetching && !clamping && <span aria-live="polite" aria-atomic="true">
            Showing {docs.data.items.length ? `${(page - 1) * PAGE_SIZE + 1}–${(page - 1) * PAGE_SIZE + docs.data.items.length}` : "0"} of {docs.data.total} documents
          </span>}
          <Button size="sm" variant="secondary" disabled={page <= 1 || docs.isFetching || clamping} onClick={() => changePage(page - 1)}>Previous documents</Button>
          <span>Page {page}{lastPage !== null && !docs.isError ? ` of ${lastPage}` : ""}</span>
          <Button size="sm" variant="secondary" disabled={docs.isError || docs.isFetching || clamping || lastPage === null || page >= lastPage} onClick={() => changePage(page + 1)}>Next documents</Button>
        </nav>
        <QueryState
          query={{ ...docs, isLoading: docs.isLoading || docs.isFetching || clamping }}
          loading={<Skeleton className="h-24 w-full" />}
          isEmpty={(data) => data.total === 0}
          empty={
            <EmptyState
              title="No documents yet"
              description={kind ? "No originals match this kind. Choose All kinds to view the registry." : "Originals appear here as invoices, receipts and attachments are uploaded."}
            />
          }
          errorTitle="Couldn’t load the document registry"
        >
          {(data) => (
            <div>
              {data.items.length === 0 && <p className="mb-3 text-sm text-text-secondary">This page has no documents. <button type="button" className="underline" disabled={docs.isFetching} onClick={() => docs.refetch()}>Refresh this page</button> to check the current registry.</p>}
            <div role="region" aria-label="Stored original metadata" tabIndex={0} className="relative max-w-full scroll-mt-20 overflow-x-auto rounded focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus">
            <table className="w-full min-w-[640px] text-sm">
              <caption className="sr-only">Stored original metadata for the current registry page</caption>
              <thead>
                <tr className="text-left text-xs text-text-secondary">
                  <th className="py-1">Filename</th>
                  <th className="py-1">Kind</th>
                  <th className="py-1">Type</th>
                  <th className="py-1">Size</th>
                  <th className="py-1">SHA-256</th>
                  <th className="py-1">Uploaded</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((d) => (
                  <tr key={d.id} className="border-t border-slate-100">
                    <td className="max-w-xs py-1 font-medium wrap-anywhere">{d.filename ?? "—"}</td>
                    <td className="py-1">
                      <Badge tone={KIND_TONE[d.kind] ?? "neutral"}>{d.kind}</Badge>
                    </td>
                    <td className="py-1 text-xs text-text-secondary">{d.mime ?? "—"}</td>
                    <td className="py-1 tabular-nums text-xs">{formatSize(d.size)}</td>
                    <td className="py-1 font-mono text-xs" title={d.sha256}>
                      {d.sha256.slice(0, 12)}…
                    </td>
                    <td className="py-1 text-xs text-text-secondary">
                      {formatDate(d.created_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            </div>
            </div>
          )}
        </QueryState>
      </Card>
    </div>
  );
}
