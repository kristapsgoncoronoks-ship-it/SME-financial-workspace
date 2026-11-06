import { useId, useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { useAuth } from "../auth/AuthContext";
import { Badge, Button, ErrorState } from "./ui";
import { api, apiError } from "../lib/api";
import { formatNumber, shortDate } from "../lib/format";
import { parseReadingFacts, parseReadingHistory, readingBase,
  type ReadingFacts, type ReadingOwner, type ReadingPage, type ReadingProfile, type ReadingSummary } from "../lib/readingFacts";

export function CaptureReadingDetails({ channel, ownerId, revision }: ReadingOwner & { revision: string }) {
  const { cacheIdentity } = useAuth();
  return <ReadingDetailsShell key={`${cacheIdentity}:${channel}:${ownerId}:${revision}`} channel={channel} ownerId={ownerId} />;
}

function ReadingDetailsShell(owner: ReadingOwner) {
  const [open, setOpen] = useState(false);
  const id = useId();
  return <section aria-label="Reading details" className="min-w-0 space-y-3 border-t border-slate-200 pt-4">
    <div className="flex min-w-0 flex-wrap items-center justify-between gap-3">
      <div className="min-w-0">
        <h2 className="text-sm font-semibold text-slate-700">Reading details</h2>
        <p className="text-sm text-text-secondary">Inspect the recorded full-document reading without re-reading the file.</p>
      </div>
      <Button variant="secondary" size="sm" aria-expanded={open} aria-controls={id} onClick={() => setOpen(!open)}>{open ? "Close reading details" : "View reading details"}</Button>
    </div>
    {open && <div id={id}><ReadingInspection owner={owner} /></div>}
  </section>;
}

function ReadingInspection({ owner }: { owner: ReadingOwner }) {
  const { user, org, cacheIdentity } = useAuth();
  const [cursor, setCursor] = useState<string | null>(null);
  const [selected, setSelected] = useState<ReadingSummary | null>(null);
  const base = readingBase(owner);
  const scope = ["capture-reading-details", user?.id, org?.id, cacheIdentity, owner.channel, owner.ownerId];
  const headers = { "X-Expected-Org-ID": org?.id, "X-Expected-User-ID": user?.id };
  const current = useQuery({
    queryKey: [...scope, "current"], enabled: !!user && !!org,
    queryFn: async ({ signal }) => {
      const value = (await api.get(`${base}/current`, { signal, headers })).data;
      return value === null ? null : parseReadingFacts(value, owner);
    },
    staleTime: 0, gcTime: 0, meta: { errorHandled: true },
  });
  const history = useQuery({
    queryKey: [...scope, "history", cursor], enabled: !!user && !!org,
    queryFn: async ({ signal }) => parseReadingHistory((await api.get(base, { signal, headers, params: cursor === null ? {} : { cursor } })).data),
    staleTime: 0, gcTime: 0, meta: { errorHandled: true },
  });
  const recorded = useQuery({
    queryKey: [...scope, "recorded", selected?.id, selected?.facts_sha256], enabled: !!user && !!org && selected !== null,
    queryFn: async ({ signal }) => parseReadingFacts((await api.get(`${base}/${selected!.id}`, { signal, headers })).data, owner, selected!),
    staleTime: 0, gcTime: 0, meta: { errorHandled: true },
  });
  const busy = current.isFetching || history.isFetching || recorded.isFetching;
  const shown = selected ? recorded : current;
  const refresh = async () => {
    await Promise.allSettled([current.refetch(), history.refetch(), ...(selected ? [recorded.refetch()] : [])]);
  };

  return <div className="min-w-0 space-y-5">
    <p className="text-sm text-text-secondary">These observations describe the full-document read. Manual corrections and selected-area re-reads are recorded separately. Output counts do not measure recognition accuracy.</p>
    <div className="flex flex-wrap gap-2">
      <Button size="sm" variant="secondary" disabled={busy} onClick={() => { void refresh(); }}>{busy ? "Loading reading details…" : "Refresh reading details"}</Button>
      {selected && <Button size="sm" variant="secondary" disabled={busy} onClick={() => { setSelected(null); void current.refetch(); }}>Show current reading</Button>}
    </div>
    <section aria-label={selected ? "Recorded full-document reading" : "Current full-document reading"} className="min-w-0 space-y-3">
      <h3 className="text-sm font-semibold">{selected ? "Recorded full-document reading" : "Current full-document reading when loaded"}</h3>
      {selected && <p className="text-sm text-amber-800">Selected from history. This observation is not presented as the current reading.</p>}
      {shown.isFetching || shown.isPending ? <p role="status" className="text-sm text-text-secondary">Loading recorded facts…</p>
        : shown.isError ? <ErrorState title="Couldn’t load reading facts" description={apiError(shown.error)} onRetry={() => shown.refetch()} />
          : shown.data === null ? <p className="text-sm text-text-secondary">No verified current reading observation is available. Older attempts may appear in history. This does not mean the document was read successfully.</p>
            : shown.data ? <Observation facts={shown.data} /> : null}
    </section>
    <section aria-label="Recorded reading history" className="min-w-0 space-y-3 border-t border-slate-200 pt-4">
      <h3 className="text-sm font-semibold">Recorded reading history</h3>
      <p className="text-xs text-text-secondary">Up to 20 summaries per page, in publication order. Publication order does not identify the current reading.</p>
      {history.isFetching || history.isPending ? <p role="status" className="text-sm text-text-secondary">Loading reading history…</p>
        : history.isError ? <ErrorState title="Couldn’t load reading history" description={apiError(history.error)} onRetry={() => history.refetch()} />
          : history.data.items.length === 0 ? <p className="text-sm text-text-secondary">No reading observations recorded on this page. Earlier documents may have no recorded facts.</p>
            : <ol className="divide-y divide-slate-200">{history.data.items.map((item) => <li key={item.id} className="flex min-w-0 flex-wrap items-center justify-between gap-3 py-3">
              <div className="min-w-0 space-y-1 text-sm">
                <p>Recorded {shortDate(item.published_at)} · {item.outcome === "success" ? "Reading completed" : "Reading failed"} · {item.attempt_seq === null ? "Attempt number not recorded" : `Attempt ${formatNumber(item.attempt_seq, 0)}`}</p>
                <p className="break-all text-xs text-text-secondary">Source: {item.source_sha256 ?? "Not recorded"}</p>
              </div>
              <Button size="sm" variant="secondary" aria-pressed={selected?.id === item.id} disabled={busy} onClick={() => setSelected(item)}>View recorded reading {item.attempt_seq === null ? "" : formatNumber(item.attempt_seq, 0)}</Button>
            </li>)}</ol>}
      <div className="flex flex-wrap gap-2">
        {cursor !== null && <Button size="sm" variant="secondary" disabled={busy} onClick={() => setCursor(null)}>First history page</Button>}
        {!history.isError && history.data?.has_more && <Button size="sm" variant="secondary" disabled={busy} onClick={() => setCursor(history.data.next_cursor)}>More recorded readings</Button>}
      </div>
    </section>
  </div>;
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return <div className="min-w-0"><dt className="text-xs text-text-secondary">{label}</dt><dd className="min-w-0 break-words text-sm">{children}</dd></div>;
}
const number = (value: number | null, places = 0) => value === null ? "Not recorded" : formatNumber(value, places);
const text = (value: string | null) => value || "Not recorded";
const profileName = (value: string | null) => value === "builtin-local-default-v1" ? "Built-in local reader (v1)" : "Not recorded";
function setting(mode: string, resolved: string | number | null): string {
  const value = resolved === null || resolved === "" ? null : String(resolved);
  return mode === "engine-default" ? `Engine default · ${value === null ? "selected value not observed" : `observed value ${value}`}`
    : mode === "explicit" ? `Explicit · ${value ?? "value not recorded"}` : `Selection mode unknown · ${value ?? "value not observed"}`;
}

function Observation({ facts }: { facts: ReadingFacts }) {
  return <div className="min-w-0 space-y-4">
    <dl className="grid min-w-0 grid-cols-1 gap-3 sm:grid-cols-2">
      <Fact label="Reading outcome"><Badge tone={facts.outcome === "success" ? "success" : "warning"}>{facts.outcome === "success" ? "Reading completed" : "Reading failed"}</Badge>{facts.failure_code && <span className="ml-2">{facts.failure_code.replace(/_/g, " ")}</span>}</Fact>
      <Fact label="Recorded"><time dateTime={facts.created_at}>{shortDate(facts.created_at)}</time></Fact>
      <Fact label="Requested full-document profile">{profileName(facts.requested_profile_id)}</Fact>
      <Fact label="Upload attempt">{facts.attempt_seq === null ? "Not recorded for this channel" : number(facts.attempt_seq)}</Fact>
      <Fact label="Source failure sequence">{number(facts.source_failure_seq)}</Fact>
      <Fact label="Observed page count">{number(facts.page_count)}</Fact>
      <Fact label="Complete selected output">{facts.selected_stream_complete ? `Recorded · ${number(facts.selected_stream_bytes)} bytes` : "Not recorded as complete"}</Fact>
    </dl>
    {facts.profile ? <Profile profile={facts.profile} /> : <p className="text-sm text-text-secondary">Reader profile and engine were not observed for this publication. Current installation or settings cannot fill these gaps.</p>}
    <section aria-label="Page reading outcomes" className="space-y-3">
      <h4 className="text-sm font-semibold">Page reading outcomes</h4>
      {facts.page_observation_state === "not_applicable" ? <p className="text-sm text-text-secondary">Per-page PDF observations do not apply to this source.</p>
        : facts.pages === null ? <p className="text-sm text-text-secondary">Per-page outcomes were not recorded. Missing observations are not successful reads.</p>
          : facts.pages.length === 0 ? <p className="text-sm text-text-secondary">The reader observed an empty page set.</p>
            : <ol className="divide-y divide-slate-200">{facts.pages.map((page) => <Page key={page.page} page={page} />)}</ol>}
    </section>
    <details className="min-w-0 text-sm">
      <summary className="cursor-pointer font-medium text-slate-700">Observation identity and fingerprints</summary>
      <dl className="mt-3 grid min-w-0 grid-cols-1 gap-3">
        <Fact label="Observation ID"><span className="break-all">{facts.observation_id}</span></Fact>
        <Fact label="Source file fingerprint (SHA-256)"><span className="break-all">{text(facts.source_sha256)}</span></Fact>
        <Fact label="Complete selected output fingerprint (SHA-256)"><span className="break-all">{text(facts.selected_stream_sha256)}</span></Fact>
      </dl>
      <p className="mt-2 text-xs text-text-secondary">A recording timestamp is not a measured database commit time. No extracted page text is included in these observations.</p>
    </details>
  </div>;
}

function Profile({ profile }: { profile: ReadingProfile }) {
  return <section aria-label="Observed reader profile" className="min-w-0 space-y-3 border-t border-slate-200 pt-3">
    <h4 className="text-sm font-semibold">Observed reader profile</h4>
    <dl className="grid min-w-0 grid-cols-1 gap-3 sm:grid-cols-2">
      <Fact label="Observed full-document profile">{profileName(profile.effective_profile_id)}</Fact>
      <Fact label="Reader adapter">{profile.adapter === "pdf" ? "PDF reader" : "Image reader"}</Fact>
      <Fact label="OCR use">{{ used: "OCR was used", not_needed: "OCR was not needed", unknown: "Not observed" }[profile.ocr_state]}</Fact>
      <Fact label="Observed engine">{profile.engine_name === "tesseract" ? "Tesseract" : "Not observed"}</Fact>
      <Fact label="Recorded engine version">{text(profile.engine_version)}</Fact>
      <Fact label="Selected language">{setting(profile.language_mode, profile.resolved_language)}</Fact>
      <Fact label="Page segmentation">{setting(profile.psm_mode, profile.resolved_psm)}</Fact>
      <Fact label="OCR engine mode">{setting(profile.oem_mode, profile.resolved_oem)}</Fact>
      <Fact label="Recorded preprocessing">{text(profile.preprocessing_version)}</Fact>
    </dl>
    <details className="min-w-0 text-sm">
      <summary className="cursor-pointer font-medium text-slate-700">Recorded installation inventory and reader limits</summary>
      <div className="mt-3 min-w-0 space-y-3">
        <p className="break-words">Installed language packs: {profile.installed_languages === null ? "Not observed" : profile.installed_languages.length === 0 ? "None reported" : profile.installed_languages.join(", ")} · Inventory {profile.inventory_status.replace(/_/g, " ")}.</p>
        <p className="text-xs text-text-secondary">Installed packs do not identify the selected language. These are facts from this reading, not the current worker installation.</p>
        {profile.policies.length === 0 ? <p className="text-sm text-text-secondary">No reader limit snapshot was recorded.</p> : <ul className="space-y-1">{profile.policies.map((policy) => <li key={policy.name} className="break-words text-xs">{policy.name.replace(/_/g, " ")}: {formatNumber(policy.value, 3)} {policy.unit} · {policy.scope.replace(/_/g, " ")}</li>)}</ul>}
        <p className="break-all text-xs text-text-secondary">Reader source fingerprint: {text(profile.reader_source_sha256)}</p>
      </div>
    </details>
  </section>;
}

const PAGE_LABEL = { native: "Native text used", ocr_complete: "OCR completed", timed_out: "Timed out", failed: "Reading failed", outcome_unknown: "Outcome unknown", not_attempted: "Not attempted" };
function Page({ page }: { page: ReadingPage }) {
  return <li className="min-w-0 space-y-3 py-3">
    <div className="flex flex-wrap items-center gap-2"><h5 className="text-sm font-semibold">Page {number(page.page)}</h5><Badge tone={page.status === "native" || page.status === "ocr_complete" ? "success" : "warning"}>{PAGE_LABEL[page.status]}</Badge></div>
    <dl className="grid min-w-0 grid-cols-1 gap-3 sm:grid-cols-2">
      <Fact label="Recorded cause">{page.cause ? page.cause.replace(/_/g, " ") : "Not recorded"}</Fact>
      <Fact label="Selected reading path">{page.selected_kind === "native" ? "Native text" : page.selected_kind === "ocr" ? "OCR" : "Not recorded"}</Fact>
      <Fact label="Native output">{page.native_chars === null ? "Not recorded" : `${number(page.native_chars)} characters · ${number(page.native_bytes)} bytes`}</Fact>
      <Fact label="OCR output">{page.ocr_chars === null ? "Not recorded" : `${number(page.ocr_chars)} characters · ${number(page.ocr_bytes)} bytes`}</Fact>
      <Fact label="Complete selected page output">{page.selected_chars === null ? "Not recorded as complete" : `${number(page.selected_chars)} characters · ${number(page.selected_bytes)} bytes`}</Fact>
      <Fact label="Source dimensions">{page.source_width === null ? "Not recorded" : `${number(page.source_width, 2)} × ${number(page.source_height, 2)} ${page.source_unit ?? "(unit not recorded)"}`}</Fact>
      <Fact label="Actual raster dimensions">{page.raster_width === null ? "Not recorded" : `${number(page.raster_width)} × ${number(page.raster_height)} pixels`}</Fact>
      <Fact label="Target raster density">{page.render_dpi === null ? "Not recorded" : `${number(page.render_dpi)} dpi target; achieved density is not recorded`}</Fact>
      <Fact label="Recorded render scale">{number(page.render_scale, 3)}</Fact>
      <Fact label="Recorded page time">{page.elapsed_ms === null ? "Not recorded" : `${number(page.elapsed_ms)} ms`}</Fact>
    </dl>
  </li>;
}
