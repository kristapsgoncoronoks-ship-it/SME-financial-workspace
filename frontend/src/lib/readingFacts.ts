/** Stored full-document observations only. This module never runs a reader. */
export const READING_PAGE_STATUSES = ["native", "ocr_complete", "timed_out", "failed", "outcome_unknown", "not_attempted"] as const;
export type ReadingPageStatus = typeof READING_PAGE_STATUSES[number];
type SettingMode = "engine-default" | "explicit" | "unknown";
export interface ReadingPolicy { name: string; value: number; unit: string; scope: string }
export interface ReadingProfile {
  schema_version: 1; requested_profile_id: string | null; effective_profile_id: string | null;
  adapter: "pdf" | "image"; ocr_state: "used" | "not_needed" | "unknown";
  engine_name: "tesseract" | null; engine_version: string | null;
  inventory_status: "not_probed" | "available" | "truncated" | "unavailable" | "unknown";
  installed_languages: string[] | null;
  language_mode: SettingMode; resolved_language: string | null;
  psm_mode: SettingMode; resolved_psm: number | null;
  oem_mode: SettingMode; resolved_oem: number | null;
  reader_source_sha256: string | null; preprocessing_version: string | null;
  observed_at: string | null; policies: ReadingPolicy[];
}
export interface ReadingPage {
  page: number; status: ReadingPageStatus; cause: string | null; selected_kind: "native" | "ocr" | null;
  native_chars: number | null; native_bytes: number | null; native_sha256: string | null;
  ocr_chars: number | null; ocr_bytes: number | null; ocr_sha256: string | null;
  selected_chars: number | null; selected_bytes: number | null; selected_sha256: string | null;
  source_width: number | null; source_height: number | null; source_unit: "points" | null;
  raster_width: number | null; raster_height: number | null;
  render_scale: number | null; render_dpi: 300 | null; elapsed_ms: number | null;
}
export interface ReadingFacts {
  schema_version: 1; observation_id: string; channel: "upload" | "inbound"; owner_id: string;
  source_sha256: string | null; attempt_seq: number | null; source_failure_seq: number | null;
  requested_profile_id: string | null; outcome: "success" | "failure"; failure_code: string | null;
  created_at: string; profile: ReadingProfile | null;
  page_observation_state: "observed" | "not_observed" | "not_applicable"; page_count: number | null;
  selected_stream_complete: boolean; selected_stream_bytes: number | null; selected_stream_sha256: string | null;
  pages: ReadingPage[] | null;
}
export interface ReadingSummary {
  id: string; source_sha256: string | null; attempt_seq: number | null; source_failure_seq: number | null;
  outcome: "success" | "failure"; failure_code: string | null; published_at: string;
  profile_id: string | null; requested_profile_id: string | null; adapter: "pdf" | "image" | null;
  profile_state: "available" | "unknown" | "not_applicable"; page_count: number | null;
  page_status_counts: Partial<Record<ReadingPageStatus, number>> | null; facts_sha256: string;
}
export interface ReadingHistory { items: ReadingSummary[]; has_more: boolean; next_cursor: string | null }
export type ReadingOwner = { channel: "upload" | "inbound"; ownerId: string };

const METADATA_BYTES = 128 * 1024;
const SHA = /^[0-9a-f]{64}$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const PROFILE = "builtin-local-default-v1";
const FAILURES = ["unsupported_format", "malformed_document", "unreadable_scan", "image_processing_limit", "image_reading_busy", "capture_unavailable", "stored_file_missing", "stored_file_too_large", "stored_file_changed", "security_rejected", "processing_timeout", "internal_error", "unknown_failure"];
const CAUSES = ["native_text_selected", "short_native_text", "engine_timeout", "supervisor_timeout", "classified_native_failure", "resource_or_native_exit", "result_incomplete", "processing_stopped", "unknown"];
const POLICY_NAMES = ["tesseract_timeout", "pdf_wall", "pdf_cpu", "pdf_address_space", "pdf_pages", "pdf_render", "pdf_render_edge", "pdf_render_dpi", "decoded_text", "child_output", "image_source", "image_source_edge", "image_raster", "image_raster_edge", "image_address_space", "image_file", "image_text", "image_output", "image_wall", "image_cpu", "image_preparation_grace"];
const POLICY_SCOPES = ["per_tesseract_invocation", "per_pdf_operation", "per_native_process", "per_page_raster", "selected_text_aggregate", "per_image_source", "per_image_raster", "per_file", "per_image_operation", "per_image_preparation"];
const unavailable = () => new Error("These recorded reading details could not be verified. Try loading them again; the document and saved values have not changed.");
const count = (value: unknown): value is number => typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
const positive = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value) && value > 0;
const isString = (value: unknown, max: number): value is string => typeof value === "string" && value.length <= max;
const oneOf = (value: unknown, values: readonly string[]) => typeof value === "string" && values.includes(value);
const nullable = (value: unknown, check: (value: unknown) => boolean) => value === null || check(value);
const hash = (value: unknown) => typeof value === "string" && SHA.test(value);
const profileId = (value: unknown) => value === null || value === PROFILE;
const stamp = (value: unknown) => typeof value === "string" && /T.*(?:Z|[+-]\d{2}:\d{2})$/.test(value) && Number.isFinite(Date.parse(value));
function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw unavailable();
  return value as Record<string, unknown>;
}
function shape(value: unknown, keys: string[]): Record<string, unknown> {
  const row = object(value);
  if (Object.keys(row).some((key) => !keys.includes(key)) || keys.some((key) => !Object.prototype.hasOwnProperty.call(row, key))) throw unavailable();
  return row;
}
function bounded(value: unknown, max: number) {
  if (new TextEncoder().encode(JSON.stringify(value)).byteLength > max) throw unavailable();
}
function parseProfile(value: unknown): ReadingProfile {
  const p = shape(value, ["schema_version", "requested_profile_id", "effective_profile_id", "adapter", "ocr_state", "engine_name", "engine_version", "inventory_status", "installed_languages", "language_mode", "resolved_language", "psm_mode", "resolved_psm", "oem_mode", "resolved_oem", "reader_source_sha256", "preprocessing_version", "observed_at", "policies"]);
  if (p.schema_version !== 1 || !profileId(p.requested_profile_id) || !profileId(p.effective_profile_id) || !oneOf(p.adapter, ["pdf", "image"]) || !oneOf(p.ocr_state, ["used", "not_needed", "unknown"]) || !nullable(p.engine_name, (v) => v === "tesseract") || !nullable(p.engine_version, (v) => isString(v, 120)) || !oneOf(p.inventory_status, ["not_probed", "available", "truncated", "unavailable", "unknown"]) || !nullable(p.installed_languages, (v) => Array.isArray(v) && v.length <= 32 && v.every((item) => isString(item, 32))) || !nullable(p.reader_source_sha256, hash) || !nullable(p.preprocessing_version, (v) => isString(v, 64) && /^[\x20-\x7e]+$/.test(v)) || !nullable(p.observed_at, stamp) || !Array.isArray(p.policies) || p.policies.length > 32) throw unavailable();
  if (p.ocr_state !== "used" && p.engine_name !== null) throw unavailable();
  if (p.inventory_status === "available" && p.installed_languages === null) throw unavailable();
  for (const [mode, resolved, max] of [[p.language_mode, p.resolved_language, null], [p.psm_mode, p.resolved_psm, 13], [p.oem_mode, p.resolved_oem, 3]]) {
    if (!oneOf(mode, ["engine-default", "explicit", "unknown"]) || !nullable(resolved, max === null ? (v) => isString(v, 32) : (v) => count(v) && v <= Number(max))) throw unavailable();
  }
  const policies = p.policies.map((value) => {
    const item = shape(value, ["name", "value", "unit", "scope"]);
    if (!oneOf(item.name, POLICY_NAMES) || typeof item.value !== "number" || !Number.isFinite(item.value) || item.value < 0 || !oneOf(item.unit, ["seconds", "bytes", "pages", "pixels", "dpi"]) || !oneOf(item.scope, POLICY_SCOPES)) throw unavailable();
    return item as unknown as ReadingPolicy;
  });
  if (new Set(policies.map((policy) => policy.name)).size !== policies.length) throw unavailable();
  return { ...p, policies } as unknown as ReadingProfile;
}
function parsePage(value: unknown): ReadingPage {
  const page = shape(value, ["page", "status", "cause", "selected_kind", "native_chars", "native_bytes", "native_sha256", "ocr_chars", "ocr_bytes", "ocr_sha256", "selected_chars", "selected_bytes", "selected_sha256", "source_width", "source_height", "source_unit", "raster_width", "raster_height", "render_scale", "render_dpi", "elapsed_ms"]);
  if (!count(page.page) || page.page < 1 || page.page > 50 || !oneOf(page.status, READING_PAGE_STATUSES) || !nullable(page.cause, (v) => oneOf(v, CAUSES)) || !nullable(page.selected_kind, (v) => oneOf(v, ["native", "ocr"])) || !nullable(page.source_width, positive) || !nullable(page.source_height, positive) || !nullable(page.source_unit, (v) => v === "points") || !nullable(page.raster_width, (v) => count(v) && v > 0 && v <= 20000) || !nullable(page.raster_height, (v) => count(v) && v > 0 && v <= 20000) || !nullable(page.render_scale, positive) || !nullable(page.render_dpi, (v) => v === 300) || !nullable(page.elapsed_ms, count)) throw unavailable();
  if ((page.source_width === null) !== (page.source_height === null) || (page.raster_width === null) !== (page.raster_height === null) || (Number(page.raster_width) * Number(page.raster_height) > 40000000)) throw unavailable();
  for (const prefix of ["native", "ocr", "selected"]) {
    const chars = page[prefix + "_chars"], bytes = page[prefix + "_bytes"], digest = page[prefix + "_sha256"];
    if (!nullable(chars, count) || !nullable(bytes, count) || !nullable(digest, hash) || [chars, bytes, digest].some((v) => v === null) !== [chars, bytes, digest].every((v) => v === null)) throw unavailable();
  }
  if (["native", "ocr_complete"].includes(String(page.status))) {
    if (page.selected_kind !== (page.status === "native" ? "native" : "ocr") || page.selected_sha256 === null) throw unavailable();
  } else if (page.selected_sha256 !== null || page.selected_chars !== null || page.selected_bytes !== null) throw unavailable();
  return page as unknown as ReadingPage;
}
export function parseReadingFacts(value: unknown, owner: ReadingOwner, selected?: ReadingSummary): ReadingFacts {
  bounded(value, METADATA_BYTES);
  const facts = shape(value, ["schema_version", "observation_id", "channel", "owner_id", "source_sha256", "attempt_seq", "source_failure_seq", "requested_profile_id", "outcome", "failure_code", "created_at", "profile", "page_observation_state", "page_count", "selected_stream_complete", "selected_stream_bytes", "selected_stream_sha256", "pages"]);
  if (facts.schema_version !== 1 || typeof facts.observation_id !== "string" || !UUID.test(facts.observation_id) || facts.channel !== owner.channel || facts.owner_id !== owner.ownerId || !nullable(facts.source_sha256, hash) || !nullable(facts.attempt_seq, count) || !nullable(facts.source_failure_seq, count) || !profileId(facts.requested_profile_id) || !oneOf(facts.outcome, ["success", "failure"]) || !nullable(facts.failure_code, (v) => oneOf(v, FAILURES)) || !stamp(facts.created_at) || !oneOf(facts.page_observation_state, ["observed", "not_observed", "not_applicable"]) || !nullable(facts.page_count, count) || typeof facts.selected_stream_complete !== "boolean" || !nullable(facts.selected_stream_bytes, count) || !nullable(facts.selected_stream_sha256, hash) || !(facts.pages === null || (Array.isArray(facts.pages) && facts.pages.length <= 50))) throw unavailable();
  if (facts.selected_stream_complete ? facts.selected_stream_bytes === null || facts.selected_stream_sha256 === null : facts.selected_stream_bytes !== null || facts.selected_stream_sha256 !== null) throw unavailable();
  const pages = facts.pages === null ? null : (facts.pages as unknown[]).map(parsePage);
  if (pages !== null && (facts.page_count === null || (pages.length === 0 && facts.page_count !== 0) || pages.some((page, index) => page.page > Number(facts.page_count) || (index > 0 && page.page <= pages[index - 1].page)))) throw unavailable();
  if (selected && (facts.observation_id !== selected.id || facts.source_sha256 !== selected.source_sha256 || facts.attempt_seq !== selected.attempt_seq || facts.source_failure_seq !== selected.source_failure_seq || facts.outcome !== selected.outcome || facts.failure_code !== selected.failure_code)) throw unavailable();
  return { ...facts, profile: facts.profile === null ? null : parseProfile(facts.profile), pages } as unknown as ReadingFacts;
}
export function parseReadingHistory(value: unknown): ReadingHistory {
  bounded(value, 96 * 1024);
  const page = shape(value, ["items", "has_more", "next_cursor"]);
  if (!Array.isArray(page.items) || page.items.length > 20 || typeof page.has_more !== "boolean" || !nullable(page.next_cursor, (v) => typeof v === "string" && /^[A-Za-z0-9_-]{1,128}$/.test(v)) || page.has_more !== (page.next_cursor !== null)) throw unavailable();
  const items = page.items.map((value) => {
    const item = shape(value, ["id", "source_sha256", "attempt_seq", "source_failure_seq", "outcome", "failure_code", "published_at", "profile_id", "requested_profile_id", "adapter", "profile_state", "page_count", "page_status_counts", "facts_sha256"]);
    if (typeof item.id !== "string" || !UUID.test(item.id) || !nullable(item.source_sha256, hash) || !nullable(item.attempt_seq, count) || !nullable(item.source_failure_seq, count) || !oneOf(item.outcome, ["success", "failure"]) || !nullable(item.failure_code, (v) => oneOf(v, FAILURES)) || !stamp(item.published_at) || !profileId(item.profile_id) || !profileId(item.requested_profile_id) || !nullable(item.adapter, (v) => oneOf(v, ["pdf", "image"])) || !oneOf(item.profile_state, ["available", "unknown", "not_applicable"]) || !nullable(item.page_count, count) || !hash(item.facts_sha256)) throw unavailable();
    if (item.page_status_counts !== null) {
      const counts = object(item.page_status_counts);
      if (Object.entries(counts).some(([name, amount]) => !READING_PAGE_STATUSES.includes(name as ReadingPageStatus) || !count(amount))) throw unavailable();
    }
    return item as unknown as ReadingSummary;
  });
  if (new Set(items.map((item) => item.id)).size !== items.length) throw unavailable();
  return { items, has_more: page.has_more, next_cursor: page.next_cursor } as ReadingHistory;
}
export function readingBase(owner: ReadingOwner): string {
  return owner.channel === "upload" ? `/invoices/captures/${owner.ownerId}/reading-observations` : `/email/inbox/${owner.ownerId}/reading-observations`;
}
