import axios from "axios";
import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Button, Modal } from "./ui";
import { api } from "../lib/api";
import type { ProjectDocument } from "../lib/types";

// The preview decoder admits one render per backend process. Queue this
// browser's reads without delaying cancellation or replaying failed requests.
let previewTurn: Promise<void> = Promise.resolve();
async function readPreview(path: string, signal: AbortSignal): Promise<Blob> {
  const before = previewTurn;
  let release!: () => void;
  const finished = new Promise<void>(resolve => { release = resolve; });
  previewTurn = before.then(() => finished);
  let onAbort!: () => void;
  const aborted = new Promise<never>((_, reject) => {
    onAbort = () => reject(new axios.CanceledError("Photo preview is no longer needed."));
    signal.addEventListener("abort", onAbort, { once: true });
    if (signal.aborted) onAbort();
  });
  try {
    await Promise.race([before, aborted]);
    if (signal.aborted) throw new axios.CanceledError("Photo preview is no longer needed.");
    return (await api.get(path, { responseType: "blob", signal })).data;
  } finally {
    signal.removeEventListener("abort", onAbort);
    release();
  }
}

// Object URLs belong to the mounted consumer, never to the query cache.
function useBlobUrl(blob?: Blob) {
  const [owned, setOwned] = useState<{ blob: Blob; url: string }>();
  useEffect(() => {
    if (!blob) return;
    const url = URL.createObjectURL(blob);
    setOwned({ blob, url });
    return () => URL.revokeObjectURL(url);
  }, [blob]);
  return owned?.blob === blob ? owned?.url : undefined;
}

function photoError(error: unknown, preview: boolean) {
  const status = axios.isAxiosError(error) ? error.response?.status : undefined;
  if (status === 403) return "You don’t currently have access to this photo.";
  if (status === 404) return "This photo is missing or no longer available to you.";
  if (preview && status === 422) return "Preview unavailable for this photo. You can still open or download the original.";
  return preview ? "The preview could not be loaded. Try again, or open the original." : "The original could not be loaded. Try again.";
}

function OriginalPhoto({ projectId, doc, onClose }: { projectId: string; doc: ProjectDocument; onClose: () => void }) {
  const [decodeFailed, setDecodeFailed] = useState(false);
  const original = useQuery<Blob>({
    queryKey: ["project-photo-original", projectId, doc.id],
    queryFn: async ({ signal }) => (await api.get(`/masters/projects/${projectId}/documents/${doc.id}/download`, { responseType: "blob", signal })).data,
    retry: false,
    gcTime: 0,
    meta: { errorHandled: true },
  });
  const url = useBlobUrl(original.data);
  return (
    <Modal open onClose={onClose} title={doc.filename} size="lg">
      {original.isError ? (
        <div role="alert" className="space-y-3 text-sm">
          <p>{photoError(original.error, false)}</p>
          <Button variant="secondary" onClick={() => void original.refetch()}>Retry original</Button>
        </div>
      ) : url ? (
        <div className="space-y-3">
          {decodeFailed ? <p role="status" className="text-sm text-text-secondary">Your browser could not display this original. Download the file to open it in a compatible viewer.</p> : <img src={url} alt={doc.filename} className="max-h-[60vh] max-w-full object-contain" onError={() => setDecodeFailed(true)} />}
          <a className="btn-ghost" href={url} download={doc.filename}>Download original</a>
          <p className="text-xs text-text-secondary">Original file, with its stored metadata unchanged.</p>
        </div>
      ) : <p role="status" className="text-sm text-text-secondary">Loading original…</p>}
    </Modal>
  );
}

function VisiblePhoto({ projectId, doc }: { projectId: string; doc: ProjectDocument }) {
  const [opened, setOpened] = useState(false);
  const [decodeFailed, setDecodeFailed] = useState(false);
  const preview = useQuery<Blob>({
    queryKey: ["project-photo-thumbnail", projectId, doc.id],
    queryFn: ({ signal }) => readPreview(`/masters/projects/${projectId}/documents/${doc.id}/thumbnail`, signal),
    retry: false,
    gcTime: 0,
    meta: { errorHandled: true },
  });
  const url = useBlobUrl(preview.data);
  const failed = preview.isError || decodeFailed;
  return (
    <>
      <button type="button" onClick={() => setOpened(true)} aria-label={`Open original photo ${doc.filename}`} aria-busy={preview.isFetching || undefined} className="block h-28 w-28 overflow-hidden rounded-md border border-slate-200 bg-slate-100 text-xs text-slate-600 focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus focus-visible:ring-offset-2">
        {url && !failed ? <img src={url} alt={doc.filename} className="h-full w-full object-cover" onError={() => setDecodeFailed(true)} /> : <span>{failed ? "Open original" : "Loading preview…"}</span>}
      </button>
      {failed && (
        <div className="mt-1 space-y-1 text-xs text-slate-600">
          <p role="alert">{decodeFailed ? "This preview could not be displayed. You can open the original." : photoError(preview.error, true)}</p>
          <Button size="sm" variant="secondary" onClick={() => { setDecodeFailed(false); void preview.refetch(); }}>Retry preview</Button>
        </div>
      )}
      {opened && <OriginalPhoto projectId={projectId} doc={doc} onClose={() => setOpened(false)} />}
    </>
  );
}

/** Only near-visible consumers fetch derivatives; leaving the area aborts reads,
 * removes cached blobs and releases URLs. Originals require explicit activation. */
export function ProjectPhoto({ projectId, doc }: { projectId: string; doc: ProjectDocument }) {
  const target = useRef<HTMLDivElement>(null);
  const [near, setNear] = useState(false);
  useEffect(() => {
    const element = target.current;
    if (!element) return;
    const observer = new IntersectionObserver(([entry]) => setNear(entry.isIntersecting), { rootMargin: "160px" });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  return (
    <div ref={target} className="w-28 shrink-0 [overflow-wrap:anywhere]" data-photo-id={doc.id}>
      {near ? <VisiblePhoto projectId={projectId} doc={doc} /> : <div className="h-28 w-28 rounded-md bg-slate-100" aria-hidden="true" />}
      <p className="mt-1 text-xs text-text-secondary">{doc.filename}</p>
    </div>
  );
}
