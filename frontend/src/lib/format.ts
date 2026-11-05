import { STATE_CLASSES } from "./statusStyles";

// FE-018 (audit 2026-09-05): ONE locale for the whole SPA. Money was
// formatted with en-IE, dates with en-GB, and twelve call sites used the
// browser's own locale (`toLocaleString()` with no argument), so the same
// figure could read three ways on one screen. Every formatter below reads
// this constant and no page names a locale or calls `toLocale*()` itself
// (scripts/check-locale.mjs gates that). Whether the product should follow
// the USER's locale (i18n) is an owner decision (docs/DECISIONS-NEEDED.md
// §21); until it is taken the product is single-locale on purpose, and this
// constant is the one place that changes.
export const LOCALE = "en-IE";

export function formatNumber(value: number | string, digits = 0): string {
  const n = typeof value === "string" ? Number(value) : value;
  return new Intl.NumberFormat(LOCALE, { maximumFractionDigits: digits }).format(
    Number.isFinite(n) ? n : 0,
  );
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleDateString(LOCALE, { day: "2-digit", month: "short", year: "numeric" });
}

export function formatDateTime(iso: string | number | null | undefined): string {
  if (iso === null || iso === undefined || iso === "") return "—";
  return new Date(iso).toLocaleString(LOCALE, {
    day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

/** A full timestamp with seconds — the audit trail, where order matters. */
export function formatTimestamp(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString(LOCALE, {
    day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
}

export function formatTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" });
}

export function formatWeekday(iso: string): string {
  return new Date(iso).toLocaleDateString(LOCALE, { weekday: "short", day: "numeric", month: "short" });
}

/** The browser's IANA zone accompanies local work-planning input. */
export function browserTimeZone(): string {
  const zone = new Intl.DateTimeFormat(LOCALE).resolvedOptions().timeZone;
  if (!zone) throw new Error("Your browser could not identify its time zone. Reload before planning work.");
  return zone;
}

export function money(value: string | number, currency = "EUR"): string {
  const n = typeof value === "string" ? Number(value) : value;
  return new Intl.NumberFormat(LOCALE, {
    style: "currency",
    currency,
    maximumFractionDigits: 2,
  }).format(Number.isFinite(n) ? n : 0);
}

// The currency's display symbol, derived once from Intl. `formatToParts(0)` is
// used ONLY to read the symbol out of the locale data — the zero is never a
// money value and never reaches the caller's amount.
const SYMBOL_CACHE: Record<string, string> = {};
function currencySymbol(currency: string): string {
  const cached = SYMBOL_CACHE[currency];
  if (cached !== undefined) return cached;
  let symbol = currency;
  try {
    const parts = new Intl.NumberFormat(LOCALE, { style: "currency", currency }).formatToParts(0);
    symbol = parts.find((p) => p.type === "currency")?.value ?? currency;
  } catch {
    symbol = currency; // an unknown/invalid ISO code — show the code itself.
  }
  SYMBOL_CACHE[currency] = symbol;
  return symbol;
}

/**
 * Format a money amount that arrived on the wire as a DECIMAL STRING, without
 * ever turning it into a JavaScript number (master-context §4.9).
 *
 * The backend types every money field `Decimal` over `Numeric(14,2)`, and
 * pydantic v2 serializes it as a JSON string precisely so no float appears in a
 * money path. `money()` above re-parses with `Number()` — fine for the dashboard
 * aggregates it was written for, but a 14-digit `Numeric(14,2)` exceeds an IEEE
 * 754 double's 15–17 significant digits, so the last cents can change. This
 * formatter does string surgery instead: it groups the integer digits, keeps the
 * fraction digits the server sent, and performs NO arithmetic and NO rounding.
 *
 * `null`/`undefined`/`""` render as an em dash, never `0.00` — on a VAT claim a
 * null `vat_eur` means "not frozen yet", which is a different fact from zero.
 * A value that is not a plain decimal is returned unchanged (fail-open on
 * presentation: showing the server's own bytes beats swallowing them).
 */
export function decimalMoney(
  value: string | null | undefined,
  currency: string | null = "EUR",
): string {
  if (value === null || value === undefined || value === "") return "—";
  const m = /^([+-]?)(\d+)(?:\.(\d*))?$/.exec(value.trim());
  if (!m) return value;
  const [, rawSign, whole, frac = ""] = m;
  const sign = rawSign === "-" ? "-" : "";
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  // Pad to 2dp; NEVER truncate beyond it — more precision than the column can
  // hold is a fact worth showing, not a rounding decision for the UI to make.
  const cents = frac.length <= 2 ? (frac + "00").slice(0, 2) : frac;
  const symbol = currencySymbol(currency || "EUR");
  const gap = /^[A-Za-z]/.test(symbol) ? " " : "";
  return `${sign}${symbol}${gap}${grouped}.${cents}`;
}

export function compactMoney(value: string | number, currency = "EUR"): string {
  const n = typeof value === "string" ? Number(value) : value;
  return new Intl.NumberFormat(LOCALE, {
    style: "currency",
    currency,
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(Number.isFinite(n) ? n : 0);
}

export function shortDate(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleDateString(LOCALE, { day: "2-digit", month: "short", year: "numeric" });
}

export function monthLabel(period: string): string {
  // period is "YYYY-MM"
  const [y, m] = period.split("-");
  const d = new Date(Number(y), Number(m) - 1, 1);
  return d.toLocaleDateString(LOCALE, { month: "short", year: "2-digit" });
}

export const STATUS_STYLES: Record<string, string> = {
  paid: STATE_CLASSES.success,
  pending: STATE_CLASSES.warning,
  overdue: STATE_CLASSES.danger,
  draft: STATE_CLASSES.neutral,
};

// Accounts-receivable status for issued invoices.
export const ISSUED_STATUS_STYLES: Record<string, string> = {
  draft: STATE_CLASSES.neutral,
  approved: STATE_CLASSES.approved,
  sent: STATE_CLASSES.infoSubtle,
  viewed: STATE_CLASSES.info,
  paid: STATE_CLASSES.success,
  partial: STATE_CLASSES.info,
  open: STATE_CLASSES.neutral,
  overdue: STATE_CLASSES.danger,
  credited: STATE_CLASSES.credit,
  credit_note: STATE_CLASSES.credit,
  disputed: STATE_CLASSES.warning,
  written_off: STATE_CLASSES.muted,
  void: `${STATE_CLASSES.muted} line-through`,
};

export const ISSUED_STATUS_LABELS: Record<string, string> = {
  draft: "Draft", approved: "Approved", sent: "Sent", viewed: "Viewed",
  paid: "Paid", partial: "Partially paid", open: "Open", overdue: "Overdue",
  credited: "Credited", credit_note: "Credit note",
  disputed: "Disputed", written_off: "Written off", void: "Cancelled",
};

export const VALIDATION_STYLES: Record<string, string> = {
  none: STATE_CLASSES.neutral,
  passed: STATE_CLASSES.success,
  approved: STATE_CLASSES.success,
  flagged: STATE_CLASSES.danger,
  rejected: STATE_CLASSES.danger,
  pending: STATE_CLASSES.warning,
};

export const VALIDATION_LABELS: Record<string, string> = {
  none: "no validation",
  passed: "AI passed",
  flagged: "flagged",
  pending: "awaiting review",
  approved: "approved",
  rejected: "rejected",
};

// How an invoice was extracted — shown on received/uploaded invoices.
export const METHOD_LABELS: Record<string, string> = {
  "e-invoice-xml": "E-invoice XML (exact)",
  "text-layer": "PDF text layer",
  ocr: "OCR (scanned)",
  csv: "CSV",
  json: "JSON",
  unknown: "—",
};

export function methodLabel(method: string | null | undefined): string {
  return METHOD_LABELS[method ?? "unknown"] ?? method ?? "—";
}

export const METHOD_STYLES: Record<string, string> = {
  ocr: STATE_CLASSES.credit,
  "text-layer": STATE_CLASSES.info,
  "e-invoice-xml": STATE_CLASSES.success,
  csv: STATE_CLASSES.neutral,
  json: STATE_CLASSES.neutral,
};

// Categorical chart palette (was duplicated in Charts, Dashboard, Explore).
export const CHART_PALETTE = [
  "var(--color-chart-1)",
  "var(--color-chart-2)",
  "var(--color-chart-3)",
  "var(--color-chart-4)",
  "var(--color-chart-5)",
  "var(--color-chart-6)",
  "var(--color-chart-7)",
  "var(--color-chart-8)",
  "var(--color-chart-9)",
  "var(--color-chart-10)",
];

// Email-intake inbound review statuses.
export const INBOUND_STATUS_STYLES: Record<string, string> = {
  queued: STATE_CLASSES.info,       // stored, awaiting worker extraction
  pending: STATE_CLASSES.warning,
  confirmed: STATE_CLASSES.success,
  failed: STATE_CLASSES.danger,
  rejected: STATE_CLASSES.danger,
  discarded: STATE_CLASSES.neutral,
};

export const SEVERITY_STYLES: Record<string, string> = {
  error: STATE_CLASSES.danger,
  warning: STATE_CLASSES.warning,
  info: STATE_CLASSES.info,
};

export const EXPENSE_STATUS_STYLES: Record<string, string> = {
  draft: STATE_CLASSES.neutral,
  submitted: STATE_CLASSES.warning,
  partially_approved: STATE_CLASSES.warningStrong,
  approved: STATE_CLASSES.info,
  returned: STATE_CLASSES.returned,
  marked_for_reimbursement: STATE_CLASSES.approved,
  reimbursed: STATE_CLASSES.success,
  rejected: STATE_CLASSES.danger,
};
