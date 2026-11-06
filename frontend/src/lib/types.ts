import type { ReadingSummary } from "./readingFacts";

export type InvoiceStatus = "draft" | "pending" | "paid" | "overdue";

// Shape of every paginated list endpoint ({ items, total }).
export interface Paginated<T> {
  items: T[];
  total: number;
}

// The stored role vocabulary: the original four user groups (low → high
// privilege) plus the four business roles A1.5 made directly assignable
// (finance_manager/accountant/approver/auditor — see backend
// app/core/authz.py::Role, the 8-role permission matrix).
export type UserRoleName =
  | "user_free"
  | "user"
  | "admin"
  | "owner"
  | "finance_manager"
  | "accountant"
  | "approver"
  | "auditor";

// The subscription plan ladder (backend `app.services.plans.PLANS`).
export type PlanKey = "trial" | "starter" | "pro" | "enterprise";

// WO-47: the usage-limits matrix is keyed by PLAN, not by role — usage is
// metered per-organization, so the cap must be too.
export interface PlanPolicy {
  plan: PlanKey;
  label: string;
  paid: boolean;
  description: string;
  monthly_invoice_limit: number; // 0 = unlimited
  monthly_upload_limit: number;
}

export interface Usage {
  plan: PlanKey;
  invoices_used: number;
  invoice_limit: number;
  invoices_remaining: number | null;
  unlimited: boolean;
}

// Immutable, hash-chained audit trail (sysadmin-only).
export interface AuditEvent {
  id: string;
  seq: number;
  actor_email: string | null;
  action: string;
  target_type: string | null;
  target_id: string | null;
  meta: Record<string, unknown> | null;
  at: string;
}

export interface ChainStatus {
  ok: boolean;
  events: number;
  broken_at_seq: number | null;
  detail: string | null;
}

export interface User {
  id: string;
  email: string;
  name: string;
  role: UserRoleName;
  org_id: string;
  is_platform_admin?: boolean;
  is_expense_approver?: boolean;
  iban?: string | null;
  bic?: string | null;
}

export interface Organization {
  id: string;
  name: string;
  plan?: string;
  status?: string;
  region?: string;
}

export interface Member {
  id: string;
  email: string;
  name: string;
  role: UserRoleName;
  is_active: boolean;
  is_expense_approver: boolean;
  created_at: string;
}

export interface Invite {
  id: string;
  email: string;
  role: UserRoleName;
  token: string;
  accepted: boolean;
  created_at: string;
}

export interface PlanInfo {
  key: string;
  name: string;
  seats: number;
  price_eur: number | null;
  modules: string[];
  trial: boolean;
  /** WO-AD: a priced plan whose provider price id is not configured is NOT
   * purchasable — the SPA must never offer a checkout that can only fail.
   * Free/default plans are always switchable and report true. */
  purchasable: boolean;
  /** WO-AD (DECISIONS §1.B): how long this plan keeps archived invoices.
   * Business/Enterprise carry 7; everything else the included 3. */
  archive_retention_years: number;
}

export interface BillingInfo {
  plan: PlanInfo;
  status: string;
  seats_used: number;
  seats_limit: number;
  available_plans: PlanInfo[];
  billing_enabled: boolean;
  billing_provider: string;
  has_subscription: boolean;
}

export interface RetentionCategory {
  key: string;
  label: string;
  retain_days: number | null;
  purgeable_now: number;
}

export interface LegalHold {
  id: string;
  reason: string;
  active: boolean;
  placed_by: string | null;
  released_by: string | null;
  released_at: string | null;
  created_at: string;
}

export interface RetentionInfo {
  categories: RetentionCategory[];
  on_hold: boolean;
  holds: LegalHold[];
}

export interface ErasureLocation {
  key: string;
  label: string;
  matched: number;
  action: "erase" | "retain" | "blocked";
  reason: string | null;
}

export interface ErasureReport {
  email: string;
  on_hold: boolean;
  executed: boolean;
  locations: ErasureLocation[];
}

export interface SsoConnection {
  id: string;
  slug: string;
  protocol: string;
  enabled: boolean;
  issuer: string | null;
  client_id: string | null;
  allowed_domain: string | null;
  jit_enabled: boolean;
  default_role: string;
  groups_claim: string;
  role_mappings: Record<string, string>;
  role_sync: boolean;
  // WO-AE: the roles an IdP may assign, FROM THE SERVER. The settings screen's
  // selects render this list and never a page constant — the page used to
  // offer "processor", which is not a role, while omitting four that are.
  assignable_roles: string[];
  has_client_secret: boolean;
  scim_enabled: boolean;
  login_url: string | null;
  scim_base_url: string | null;
}

export interface Tenant {
  id: string;
  name: string;
  plan: string;
  status: string;
  seats_used: number;
  created_at: string;
}

export interface InvitePreview {
  email: string;
  organization_name: string;
  role: UserRoleName;
}

export const EXPENSE_CATEGORIES = [
  "travel", "meals", "accommodation", "transport", "supplies", "software", "other",
] as const;
export type ExpenseCategory = (typeof EXPENSE_CATEGORIES)[number];

export type ExpenseType = "standard" | "mileage" | "per_diem";

export interface ExpenseItemInput {
  spend_date: string;
  category: ExpenseCategory;
  description: string;
  merchant?: string | null;
  amount: string;
  vat_amount: string;
  reclaimable_tax?: boolean;
  payment_method: "personal" | "company_card";
  customer_billable?: boolean;
  billable_customer?: string | null;
  comment?: string;   // business purpose
  missing_receipt_declaration?: string | null;
  currency?: string | null;
  original_amount?: string | null;
  fx_rate?: string | null;
  fx_source?: string | null;
  expense_type?: ExpenseType;
  mileage_distance?: string | null;
  mileage_rate?: string | null;
  mileage_unit?: "km" | "mi" | null;
  per_diem_days?: string | null;
  per_diem_rate?: string | null;
}

export interface ExpenseItem {
  id: string;
  spend_date: string;
  category: string;
  description: string;
  merchant: string | null;
  amount: string;
  currency: string | null;
  original_amount: string | null;
  fx_rate: string | null;
  fx_source: string | null;
  vat_amount: string;
  reclaimable_tax: boolean;
  payment_method: string;
  customer_billable: boolean;
  billable_customer: string | null;
  comment: string | null;
  missing_receipt_declaration: string | null;
  expense_type: string;
  mileage_distance: string | null;
  mileage_rate: string | null;
  mileage_unit: string | null;
  per_diem_days: string | null;
  per_diem_rate: string | null;
  has_receipt: boolean;
  verified: boolean;
  bank_reference: string | null;
}

export interface ExpenseTransaction {
  id: string;
  version: number;
  import_batch_id?: string | null;
  currency_source?: string | null;
  txn_date: string;
  description: string;
  merchant: string | null;
  amount: string | null;
  amount_status?: "valid" | "invalid";
  currency: string;
  direction: string;
  source: string;
  status: string;
}

export interface ExpenseComment {
  id: string;
  author_name: string;
  body: string;
  created_at: string;
}

export interface ExpenseReport {
  id: string;
  employee_id: string;
  employee_name: string;
  title: string;
  status:
    | "draft"
    | "submitted"
    | "partially_approved"
    | "approved"
    | "rejected"
    | "returned"
    | "marked_for_reimbursement"
    | "reimbursed";
  currency: string;
  total: string;
  vat_total: string;
  total_eur: string | null;
  submitted_at: string | null;
  created_at: string;
  version: number;
}

export interface PolicyViolation {
  code: string;
  message: string;
  severity: "warn" | "block";
  item_id: string | null;
  category: string | null;
  amount: string | null;
  limit: string | null;
}

export const EXPENSE_POLICY_RULES = [
  "over_item_max",
  "over_category_cap",
  "missing_receipt",
  "out_of_policy_category",
  "unsupported_currency",
  "mileage_rate",
  "missing_business_purpose",
  "late_submission",
  "weekend_spend",
  "duplicate_receipt",
  "duplicate_amount_date_merchant",
] as const;

export interface ExpensePolicy {
  active: boolean;
  max_item_amount: string | null;
  receipt_required_over: string | null;
  category_caps: Record<string, string>;
  allowed_categories: string[];
  allowed_currencies: string[];
  warn_weekend: boolean;
  duplicate_detection: boolean;
  mileage_rate: string | null;
  mileage_rate_tolerance: string | null;
  require_purpose_over: string | null;
  late_submission_days: number | null;
  blocking_rules: string[];
  version: number;
}

export interface ApprovalStep {
  id: string;
  seq: number;
  kind: string;
  approver_id: string | null;
  approver_email: string | null;
  status: string;
  decided_by_email: string | null;
  decided_at: string | null;
  note: string | null;
}

export interface ExpenseReportDetail extends ExpenseReport {
  note: string | null;
  decided_at: string | null;
  decided_by: string | null;
  decision_note: string | null;
  items: ExpenseItem[];
  policy_violations?: PolicyViolation[];
  approval_steps?: ApprovalStep[];
}

export interface CustomerContact {
  id?: string;
  name: string;
  email?: string | null;
  phone?: string | null;
  role?: string | null;
}

export interface Customer {
  id: string;
  name: string;
  legal_name?: string | null;
  vat_number?: string | null;
  registration_number?: string | null;
  email?: string | null;
  phone?: string | null;
  address_line1?: string | null;
  address_line2?: string | null;
  city?: string | null;
  postal_code?: string | null;
  country?: string | null;
  ship_address_line1?: string | null;
  ship_city?: string | null;
  ship_postal_code?: string | null;
  ship_country?: string | null;
  payment_terms_days?: number | null;
  default_currency?: string | null;
  notes?: string | null;
  is_active: boolean;
  contacts: CustomerContact[];
}

export interface ExpenseApprovalPolicy {
  id: string;
  name: string;
  active: boolean;
  priority: number;
  min_amount: string | null;
  approver_ids: string[];
  finance_final: boolean;
  finance_approver_id: string | null;
  version: number;
}

export interface ExpenseSummary {
  my_draft: number;
  my_submitted: number;
  my_reimbursable: string;
  reclaimable_vat: string;
  pending_approvals: number;
  by_category: { category: string; total: string }[];
  currency: string;
}

export interface AuthResponse {
  token: { access_token: string; token_type: string };
  user: User;
  organization: Organization;
  /** PROD-003: the caller's effective permissions (`authz.permissions_for`).
   * Absent only from an older API; the SPA then falls back to its mirror. */
  permissions?: string[];
}

export interface MeResponse {
  user: User;
  organization: Organization;
  permissions?: string[];
}

export interface VendorChangeRequest {
  id: string;
  vendor_id: string;
  vendor_name?: string | null;
  field: string;
  old_value: string | null;
  new_value: string | null;
  status: "pending" | "approved" | "rejected";
  requested_by: string;
  requested_by_email: string | null;
  requested_at: string;
  decided_by: string | null;
  decided_by_email: string | null;
  decided_at: string | null;
  decision_note: string | null;
  source_document_id: string | null;
  /** DB-014: for a pending IBAN request, the other suppliers already paying
   * into the requested account — shown to the approver, never refused. */
  shared_with?: VendorRef[];
}

export interface VendorRef {
  id: string;
  name: string;
}

export interface Vendor {
  id: string;
  name: string;
  tax_id: string | null;
  country: string | null;
  category: string | null;
  iban?: string | null;
  bic?: string | null;
  status?: "active" | "provisional";
  version?: number;
  pending_changes?: VendorChangeRequest[];
  /** DB-014: the other suppliers of this workspace on the same account. */
  iban_shared_with?: VendorRef[];
}

export interface ReceivedLineAmounts {
  tax_amount?: string | null;
  gross_amount?: string | null;
  captured_tax_amount?: string | null;
  captured_gross_amount?: string | null;
  source_amounts_match?: boolean | null;
}

export interface LineItem extends ReceivedLineAmounts {
  id: string;
  description: string;
  category: string;
  quantity: string;
  unit_price: string;
  amount: string;
  tax_rate: string;
}

export type ValidationStatus =
  | "none" | "passed" | "flagged" | "pending" | "approved" | "rejected";

export interface ValidationFinding {
  severity: "info" | "warning" | "error";
  code: string;
  message: string;
  field: string | null;
}

export interface ValidationSettings {
  ai_validation_enabled: boolean;
  human_validation_enabled: boolean;
  overcharge_block_enabled: boolean;
}

// Cost-allocation dimensions shared by invoices and expense items.
export interface Dimensions {
  cost_center: string | null;
  department: string | null;
  project: string | null;
  vehicle: string | null;
  property_ref: string | null;
}

export const DIMENSION_LABELS: Record<keyof Dimensions, string> = {
  cost_center: "Cost center",
  department: "Department",
  project: "Project",
  vehicle: "Vehicle",
  property_ref: "Property",
};

export interface Invoice extends Dimensions {
  id: string;
  vendor_id: string;
  /** Current authorized supplier-master name on the paginated list; not a historical invoice snapshot. */
  supplier_name?: string | null;
  invoice_number: string;
  issue_date: string;
  due_date: string | null;
  currency: string;
  status: InvoiceStatus;
  subtotal: string;
  tax_amount: string;
  total: string;
  total_eur?: string | null;
  validation_status: ValidationStatus;
  /** AP review-&-approval lifecycle state (WO-16 — the approvals worklist). */
  workflow_state?: string | null;
  source_filename: string | null;
  /**
   * The purchase-order reference the supplier quoted (EN-16931 BT-13). On the
   * list shape as well as the detail, because matching an invoice to a purchase
   * order is something an approver does while scanning the queue.
   */
  po_reference?: string | null;
  supplier_registration_number?: string | null;
  supplier_vat_id?: string | null;

}

export interface ReceivedVatBreakdown {
  rates: { tax_rate: string; net: string; tax: string | null; gross: string | null; line_count: number }[];
  complete: boolean;
  matches_header: boolean | null;
}

export interface InvoiceDetail extends Invoice {
  supplier_iban_masked?: string | null;
  vat_breakdown?: ReceivedVatBreakdown;
  vendor_name: string;
  notes: string | null;
  line_items: LineItem[];
  validation_findings: ValidationFinding[];
  validated_by: string | null;
  validated_at: string | null;
  // AP settlement (Phase 13).
  workflow_state: string | null;
  amount_paid: string;
  paid_date: string | null;
  outstanding: string;
  payment_status: "paid" | "partial" | "open" | "overdue";
}

export interface SupplierPayment {
  id: string;
  invoice_id: string;
  amount: string;
  paid_on: string;
  method: string;
  reference: string | null;
  note: string | null;
  created_at: string;
}

// Supplier payment runs (Phase 14).
export interface RunInvoice {
  id: string;
  invoice_number: string;
  vendor_name: string | null;
  total: string;
  currency: string;
  total_eur: string | null;
  workflow_state: string;
}

export interface PaymentRun {
  id: string;
  reference: string | null;
  method: string;
  status: "open" | "approved" | "paid" | "cancelled";
  note: string | null;
  total_eur: string;
  paid_at: string | null;
  created_by: string | null;
  approved_by: string | null;
  approved_at: string | null;
  exported_at: string | null;
  export_count: number;
  last_msg_id: string | null;
  version: number;
  created_at: string;
  invoice_count: number;
}

export interface PaymentRunDetail extends PaymentRun {
  invoices: RunInvoice[];
}

// Cash-flow trend (Phase 19).
export interface CashFlowPoint {
  period: string;
  inflow: string;
  outflow: string;
  net: string;
}

// AP aging worklist (Phase 16b).
export interface ApWorklistItem {
  id: string;
  invoice_number: string;
  vendor_name: string | null;
  due_date: string | null;
  currency: string;
  total: string;
  outstanding: string;
  status: "open" | "partial" | "paid" | "overdue";
  days_overdue: number;
  bucket: string;
}

export interface ApAging {
  // Amounts are single-currency (`currency`) — currencies that could not be
  // folded in are listed in `other_currencies`, never silently summed (WO-8).
  currency: string;
  due_soon_count: number;
  due_soon_amount: string;
  overdue_count: number;
  overdue_amount: string;
  other_currencies: string[];
  items: ApWorklistItem[];
  // PERF-005/010 (2026-09-05): `items` is the soonest-due `items_limit` rows;
  // `items_total` is every open payable. The summary figures cover all of them.
  items_total?: number;
  items_limit?: number;
  truncated?: boolean;
}

// Dunning ladder (Phase 16).
export interface DunningLevel {
  level: number;
  days_overdue: number;
  tone: "reminder" | "firm" | "final";
  active: boolean;
}

export interface DunningPolicy {
  is_default: boolean;
  levels: DunningLevel[];
}

// Cash-position dashboard (Phase 15).
export interface CashPosition {
  currency: string;
  receivables: {
    currency: string;
    outstanding: string;
    overdue: string;
    avg_days_to_pay: number | null;
    aging: { label: string; count: number; outstanding: string }[];
  };
  payables: {
    outstanding: string;
    overdue: string;
    count: number;
    scheduled: number;
    in_run: number;
    // PERF-002 (2026-09-05): amounts are in the report currency only; payables
    // held in any other currency are named here, never summed in.
    other_currencies: string[];
  };
  reconciliation: {
    unmatched: number;
    matched: number;
    ignored: number;
    unmatched_amount: string;
  };
  net_position: string;
}

export interface InvoiceList {
  items: Invoice[];
  total: number;
  page: number;
  page_size: number;
}

export interface DimensionSpend {
  value: string;
  total: string;
  invoice_count: number;
}

export interface DimensionBreakdown {
  dimension: string;
  label: string;
  rows: DimensionSpend[];
  total: string;
  // C1.7/WO-24: `total` sums ONLY `currency`; other currencies present are
  // listed here, never folded in.
  currency: string;
  available_currencies: string[];
}

export interface LineItemInput {
  description: string;
  category: string;
  quantity: string;
  unit_price: string;
  amount?: string | null;
  tax_rate: string;
  captured_tax_amount?: string | null;
  captured_gross_amount?: string | null;
}

export interface InvoiceCreate {
  vendor_id?: string | null;
  vendor_name?: string | null;
  invoice_number: string;
  issue_date: string;
  due_date?: string | null;
  currency: string;
  status: InvoiceStatus;
  notes?: string | null;
  /** EN-16931 BT-13 — the supplier's purchase-order reference, when the
   * document carried one. Free text: it is the buyer's own identifier. */
  po_reference?: string | null;
  supplier_registration_number?: string | null;
  supplier_vat_id?: string | null;
  supplier_iban?: string | null;

  source_filename?: string | null;
  // The capture run this draft came from — echoed back on save so the invoice's
  // extraction lineage links up (Slice 5b). Absent for manual entry.
  extraction_run_id?: string | null;
  /** Saved capture/review revision that the confirmed values came from. */
  capture_review_fingerprint?: string | null;
  line_items: LineItemInput[];
}

// Per-field capture provenance (Slice 5f + E1.2): how a field was obtained.
// `line_index` null = one of the six header fields; n = one field of
// line_items[n]. `confidence` is null for deterministic structured parsers —
// null means "exact", NOT "unknown". `reviewed_value` is a human correction
// (null until someone reviews the capture).
export interface FieldProvenance {
  field: string;
  value?: string | null;
  status: "extracted" | "defaulted" | "missing" | string;
  confidence?: string | null;
  original_value?: string | null;
  normalized_value?: string | null;
  reviewed_value?: string | null;
  provider?: string | null;
  low_confidence: boolean;
  line_index?: number | null;
}

export interface ParsedDraft {
  draft: InvoiceCreate;
  warnings: string[];
  method?: string;
  extraction_run_id?: string | null;
  fields?: FieldProvenance[];
}

// Async direct-upload capture (Stage B): the parse/OCR runs on the worker tier.
export interface UploadAccepted {
  extraction_run_id: string;
  status: string; // queued | running
}

export interface ExtractionResult {
  reading_observation?: ReadingSummary | null;
  extraction_run_id: string;
  status: "queued" | "running" | "parsed" | "failed" | "saved";
  method?: string | null;
  draft?: ParsedDraft | null;
  error?: string | null;
  // WO-X — how far along the parse is. `stage` is a code the server owns (see
  // lib/captureStages.ts for the wording); `percent` is present only where
  // something measured it, so an indeterminate state is a real answer.
  stage?: string | null;
  pages_done?: number;
  pages_total?: number | null;
  percent?: number | null;
}

// WO-X — several invoices captured in one drop. One outcome per file, in the
// order they were sent; a batch is partial by design.
export interface BatchUploadOutcome {
  filename: string;
  accepted: boolean;
  extraction_run_id?: string | null;
  code?: string | null;
  message?: string | null;
}

export interface BatchUploadAccepted {
  accepted: number;
  rejected: number;
  outcomes: BatchUploadOutcome[];
}

// One parsed-but-unconfirmed capture in the human-review queue (E1.1).
export interface CaptureReviewItem {
  extraction_run_id: string;
  method: string;
  status: string;
  source_filename?: string | null;
  invoice_number?: string | null;
  vendor_name?: string | null;
  warning_count: number;
  total_fields: number;
  low_confidence_fields: number;
  duplicate_candidate: boolean;
  created_at: string;
}

export interface CaptureReviewQueue {
  items: CaptureReviewItem[];
  total: number;
}

/**
 * H-1 — one capture that FAILED. `summary`/`remediation` come from a closed
 * server-side vocabulary keyed by `code`; the UI renders them, it never invents
 * advice of its own. `detail` is the raw library message, for support only.
 *
 * `retry_helps` is why the Retry button is conditional: offering a retry that
 * provably cannot work (an unreadable format, a lost original) wastes the
 * operator's time and teaches them the button is meaningless.
 */
/**
 * H-3 — how a captured supplier name resolves, and why.
 *
 * `needs_decision` means the machine ABSTAINED: nothing matched exactly but
 * something nearly did, so nothing was chosen. The UI must not pick one for the
 * user — that is the whole point. `outcome` states what confirming unchanged
 * will do, including "a NEW supplier will be created".
 */
export interface VendorCandidate {
  vendor_id: string;
  name: string;
  near_kind: string;
  reason: string;
}

export interface VendorResolution {
  captured_name: string;
  basis: "exact_name" | "none";
  reason: string;
  vendor_id?: string | null;
  vendor_name?: string | null;
  needs_decision: boolean;
  candidates: VendorCandidate[];
  outcome: string;
}

export interface CaptureFailure {
  channel: "upload" | "email";
  ref_id: string;
  code: string;
  summary: string;
  remediation: string;
  retry_helps: boolean;
  user_fixable: boolean;
  detail?: string | null;
  source_filename?: string | null;
  sha256?: string | null;
  document_retained: boolean;
  failed_at: string;
  repeat_count: number;
  acknowledged_at?: string | null;
  acknowledged_by?: string | null;
  acknowledgement_note?: string | null;
}

/**
 * H-2 — the health of one inbound channel. `headline` is composed server-side
 * and states the situation POSITIVELY ("last successful delivery: 4 days ago"),
 * because absence is the whole signal and an empty screen reads as fine.
 *
 * `overdue` is true ONLY when a cadence was stated and the gap exceeds it. With
 * `expected_cadence_days` null we show elapsed time and make no claim.
 */
export interface ChannelHealth {
  channel: string;
  state: "ok" | "failing" | "silent" | "never_used";
  headline: string;
  last_attempt_at?: string | null;
  last_success_at?: string | null;
  days_since_success?: number | null;
  consecutive_failures: number;
  last_error_kind?: string | null;
  last_error_text?: string | null;
  last_error_at?: string | null;
  expected_cadence_days?: number | null;
  overdue: boolean;
}

export interface CaptureFailureGroup {
  code: string;
  summary: string;
  remediation: string;
  count: number;
  unacknowledged: number;
}

/**
 * L-4 — the result of a bulk action. `outcomes` carries one entry per record:
 * a SKIP is an ordinary result with a reason, not a failure, so the UI must not
 * render skips as errors. `applied_ids` is derived from what the write actually
 * did, which is what an undo would be built from.
 */
export interface BulkOutcome {
  ref_id: string;
  result: "applied" | "skipped" | "failed";
  reason?: string | null;
}

export interface BulkAcknowledgeResult {
  applied: number;
  skipped: number;
  failed: number;
  outcomes: BulkOutcome[];
  applied_ids: string[];
  worklist: CaptureFailureWorklist;
}

export interface CaptureFailureWorklist {
  items: CaptureFailure[];
  groups: CaptureFailureGroup[];
  total: number;
  unacknowledged: number;
}

export interface DuplicateCandidate {
  invoice_id: string;
  vendor_id: string;
  vendor_name: string;
  invoice_number: string;
  issue_date?: string | null;
  total: string;
  currency: string;
  status: string;
}

// E1.4: a same-VENDOR invoice with a DIFFERENT number but a close amount and
// issue date — a weaker, advisory signal beyond exact-number matching.
export interface ScoredCandidate extends DuplicateCandidate {
  score: number; // 0-100, advisory only — never a gate
  reason: string;
}

// Same-number invoices split by supplier: `exact` (same supplier — likely a true
// duplicate) vs `cross_supplier` (usually a coincidence). `scored` (E1.4) is
// empty unless the caller supplies vendor/total/currency/issue_date. Advisory,
// never blocks.
export interface DuplicateReport {
  invoice_number: string;
  exact: DuplicateCandidate[];
  cross_supplier: DuplicateCandidate[];
  scored: ScoredCandidate[];
}

export interface Summary {
  total_invoices: number;
  total_spend: string;
  total_tax: string;
  unpaid_amount: string;
  avg_invoice: string;
  vendor_count: number;
  currency: string;
  // C1.7/WO-24: every other currency the tenant has invoices in — `currency`
  // above is the one `total_spend` etc. are scoped to, never a blend of both.
  available_currencies: string[];
}

export interface TimeBucket {
  period: string;
  total: string;
  invoice_count: number;
}

export interface VendorSpend {
  vendor_id: string;
  vendor_name: string;
  total: string;
  invoice_count: number;
}

export interface CategorySpend {
  category: string;
  total: string;
}

export interface StatusBucket {
  status: string;
  count: number;
  total: string;
}

// C1.7/WO-24: `/analytics/spend-over-time`, `/top-vendors`, `/by-category` and
// `/by-status` used to return a bare array aggregated across EVERY currency
// with no label — a EUR+USD tenant's numbers silently blended both. Each is
// now wrapped: `currency` names what `rows` is scoped to, `available_currencies`
// lists what else exists (never folded in).
export interface SpendOverTimeOut {
  currency: string;
  available_currencies: string[];
  rows: TimeBucket[];
}

export interface TopVendorsOut {
  currency: string;
  available_currencies: string[];
  rows: VendorSpend[];
}

export interface ByCategoryOut {
  currency: string;
  available_currencies: string[];
  rows: CategorySpend[];
}

export interface ByStatusOut {
  currency: string;
  available_currencies: string[];
  rows: StatusBucket[];
}

export interface ExploreField {
  key: string;
  label: string;
  temporal?: boolean;
  unit?: "money" | "number" | "count";
}

export interface ExploreCatalog {
  dimensions: ExploreField[];
  measures: ExploreField[];
}

export interface ExploreResult {
  measure: { key: string; label: string; unit: "money" | "number" | "count" };
  dimensions: { key: string; label: string; temporal: boolean }[];
  rows: Record<string, string>[];
}

export interface SupplierBenchmark {
  vendor_id: string;
  vendor_name: string;
  country: string | null;
  invoice_count: number;
  total_spend: string;
  total_tax: string;
  avg_invoice: string;
  effective_tax_rate: string;
  paid_ratio: string;
  category_count: number;
  first_invoice: string | null;
  last_invoice: string | null;
  spend_share: string;
}

export interface SupplierPricePoint {
  vendor_id: string;
  vendor_name: string;
  unit_price: string;
  quantity: string;
  spend: string;
  deviation_pct: string;
  overspend_vs_cheapest: string;
  is_cheapest: boolean;
}

export interface CategoryBenchmark {
  category: string;
  supplier_count: number;
  total_spend: string;
  total_quantity: string;
  combined_avg_unit: string;
  cheapest_vendor_id: string | null;
  cheapest_vendor_name: string | null;
  cheapest_unit: string;
  savings_opportunity: string;
  suppliers: SupplierPricePoint[];
}

export interface BenchmarkSummary {
  supplier_count: number;
  total_spend: string;
  categories_analyzed: number;
  multi_supplier_categories: number;
  total_savings_opportunity: string;
  // C1.7/WO-24: resolved by the AR-reports pattern — no longer a hard-coded
  // "EUR". Every amount above sums ONLY this currency.
  currency: string;
  available_currencies: string[];
}

export interface CombinedBenchmark {
  summary: BenchmarkSummary;
  categories: CategoryBenchmark[];
}

// C1.7/WO-24: `/analytics/supplier-benchmark` used to return a bare
// `SupplierBenchmark[]` aggregated with no currency filter — a vendor billing
// in both EUR and USD got one blended `total_spend`. Wrapped the same way as
// the analytics row-shaped endpoints.
export interface SupplierBenchmarkListOut {
  currency: string;
  available_currencies: string[];
  rows: SupplierBenchmark[];
}

export interface FxRate {
  currency: string;
  rate: string;
  rate_date: string;
  approximate: boolean;
}

export interface FxRates {
  base: string;
  as_of: string | null;
  rates: FxRate[];
}

export interface FxCurrency {
  code: string;
  name: string;
  ecb: boolean;
  rate: string | null;
  rate_date: string | null;
  indicative: boolean;
}

export interface FxCurrencies {
  base: string;
  region: string;
  currencies: FxCurrency[];
}

export interface FxConvert {
  amount: string;
  from_currency: string;
  to_currency: string;
  converted: string;
  rate: string;
  rate_date: string;
  approximate: boolean;
}

export interface FxComparisonRow {
  invoice_id: string;
  invoice_number: string;
  vendor_name: string;
  currency: string;
  issue_date: string;
  total: string;
  ecb_rate: string | null;
  ecb_rate_date: string | null;
  eur_at_ecb: string | null;
  stated_rate: string | null;
  eur_at_stated: string | null;
  markup_eur: string | null;
  deviation_pct: string | null;
}

export interface FxComparison {
  summary: {
    non_eur_invoices: number;
    with_stated_rate: number;
    total_eur_at_ecb: string;
    total_markup_eur: string;
    currencies: string[];
  };
  rows: FxComparisonRow[];
}

export interface ModuleInfo {
  key: string;
  name: string;
  description: string;
  core: boolean;
  enabled: boolean;
  requires_issuer: boolean;
  ready: boolean;
}

export interface EmailSettings {
  address: string;
  domain: string;
  pending: number;
  total: number;
}

export type InboundStatus = "pending" | "confirmed" | "failed" | "discarded" | "rejected";

export interface InboundInvoice {
  id: string;
  from_addr: string | null;
  subject: string | null;
  received_at: string;
  filename: string;
  content_type: string | null;
  size: number;
  status: InboundStatus;
  method: string | null;
  error: string | null;
  invoice_id: string | null;
  created_at: string;
}

export interface InboundInvoiceDetail extends InboundInvoice {
  reading_observation?: ReadingSummary | null;
  draft: ParsedDraft | null;
  has_file: boolean;
}

export interface InboundList {
  items: InboundInvoice[];
  total: number;
}

export interface BudgetTarget {
  category: string;
  monthly_limit: string;
}

export interface BudgetRow {
  category: string;
  budget: string;
  actual: string;
  remaining: string;
  pct: number | null;
  over: boolean;
  untargeted: boolean;
}

export interface BudgetTrendPoint {
  month: string;
  actual: string;
  budget: string;
}

export interface BudgetOverview {
  month: string;
  currency: string;
  total_budget: string;
  total_actual: string;
  total_remaining: string;
  over_budget: boolean;
  rows: BudgetRow[];
  trend: BudgetTrendPoint[];
  // C1.7/WO-24: invoices this month that could not be converted to EUR (no
  // rate ever resolved) and were EXCLUDED from every total above, rather
  // than guessed at a 1:1 parity. 0 = every invoice is accounted for.
  excluded_unconverted: number;
}

export interface IssuerProfile {
  id: string;
  name: string;
  is_default: boolean;
  legal_name: string | null;
  trade_name: string | null;
  vat_number: string | null;
  registration_number: string | null;
  /** Art. 11 Dir. 2008/9/EC's business activity, read by the transport
   * submission checklist's `nace` rule (WO-AB). Presence only. */
  nace_code: string | null;
  address_line1: string | null;
  address_line2: string | null;
  city: string | null;
  postal_code: string | null;
  country: string | null;
  email: string | null;
  phone: string | null;
  iban: string | null;
  bic: string | null;
  default_currency: string;
  invoice_prefix: string;
  credit_note_prefix: string;
  next_number: number;
  payment_terms_days: number;
  default_penalty_rate: string | null;
  payment_instructions: string | null;
  notes: string | null;
  is_complete: boolean;
  missing_fields: string[];
  has_logo: boolean;
}

export type VatScheme = "standard" | "reverse_charge" | "intra_eu" | "exempt";

export interface IssuedLineInput {
  description: string;
  quantity: string;
  unit?: string;
  unit_price: string;
  discount_percent?: string;
  vat_rate: string;
}

export interface IssuedAttachment {
  deleted_at?: string | null;
  id: string;
  filename: string;
  mime: string | null;
  size: number;
  note: string | null;
  uploaded_by_email: string | null;
  created_at: string;
}

export interface VatBucket {
  rate: string;
  base: string;
  vat: string;
}

export type IssuedStatus =
  | "draft"
  | "approved"
  | "sent"
  | "viewed"
  | "paid"
  | "partial"
  | "open"
  | "overdue"
  | "credited"
  | "credit_note"
  | "disputed"
  | "written_off"
  | "void";

export type IssuedLifecycle =
  | "draft"
  | "approved"
  | "issued"
  | "disputed"
  | "written_off"
  | "cancelled";

// --- Partners (counterparties) with a pre-invoicing document workflow ---
export type PartnerDocKind = "contract" | "acceptance_act";

export interface PartnerDocument {
  id: string;
  kind: PartnerDocKind;
  title: string;
  reference: string | null;
  status: "draft" | "signed";
  signed_by: string | null;
  signed_date: string | null;
  note: string | null;
}

export interface PartnerReadiness {
  ready: boolean;
  required: string[];
  signed: string[];
  missing: string[];
}

export interface Partner {
  id: string;
  name: string;
  email: string | null;
  vat_number: string | null;
  address_line1: string | null;
  city: string | null;
  postal_code: string | null;
  country: string | null;
  requires_contract: boolean;
  requires_acceptance: boolean;
  penalty_enabled: boolean;
  penalty_rate: string | null;
  is_active: boolean;
}

export interface PartnerDetail extends Partner {
  documents: PartnerDocument[];
  readiness: PartnerReadiness;
  has_signed_contract: boolean;
}

export interface PenaltySummary {
  currency: string;
  total_penalty: string;
  total_outstanding: string;
  max_days_overdue: number;
  lines: { invoice_id: string; number: string; days_overdue: number; outstanding: string; penalty: string }[];
  can_generate: boolean;
  blocked_reason: string | null;
}

export interface IssuedInvoice {
  id: string;
  number: string | null;
  lifecycle: IssuedLifecycle;
  kind: "standard" | "penalty";
  doc_type: "invoice" | "credit_note";
  corrected_invoice_id: string | null;
  corrected_invoice_number: string | null;
  credited_total: string;
  issuer_id: string | null;
  partner_id: string | null;
  issue_date: string;
  supply_date: string | null;
  due_date: string | null;
  currency: string;
  buyer_name: string;
  buyer_vat_number: string | null;
  vat_scheme: string;
  note: string | null;
  po_reference: string | null;
  tax_exemption_reason: string | null;
  subtotal: string;
  tax_total: string;
  total: string;
  buyer_email: string | null;
  amount_paid: string;
  paid_date: string | null;
  status: IssuedStatus;
  outstanding: string;
  penalty_rate: string | null;
  penalty_accrued: string;
  days_overdue: number;
  reminder_count: number;
  last_reminder_at: string | null;
  sent_at: string | null;
  viewed_at: string | null;
  voided_at: string | null;
  void_reason: string | null;
  disputed_at: string | null;
  dispute_reason: string | null;
  written_off_at: string | null;
  writeoff_reason: string | null;
  approved_at: string | null;
  issued_at: string | null;
}

// Accounts-receivable cash: a receipt (money received) allocated across invoices.
export interface ReceiptAllocation {
  id: string;
  issued_invoice_id: string;
  amount: string;
  paid_on: string;
  reference: string | null;
  note: string | null;
}

export interface Receipt {
  id: string;
  amount: string;
  received_on: string;
  method: string;
  reference: string | null;
  note: string | null;
  allocated: string;
  unallocated: string;
  created_at: string;
}

export interface ReceiptDetail extends Receipt {
  allocations: ReceiptAllocation[];
}

// Bank reconciliation (Phase 12): imported statements + their lines.
export interface BankStatement {
  id: string;
  filename: string;
  source_format: string;
  method: string;
  line_count: number;
  created_by: string | null;
  created_at: string;
  unmatched: number;
  matched: number;
  ignored: number;
}

export interface BankLine {
  id: string;
  statement_id: string;
  version: number; // persisted command version; never inferred from time or status
  line_date: string;
  description: string;
  amount: string | null; // signed when valid; unavailable is never zero
  amount_status: "valid" | "invalid";
  currency: string | null; // bank-stated; null is unknown, not EUR
  direction: "credit" | "debit";
  balance: string | null;
  balance_status: "valid" | "invalid" | "unprovided";
  status: "unmatched" | "matched" | "ignored";
  matched_kind: "receipt" | "reimbursement" | "payment_run" | "issued_payment" | null;
  matched_id: string | null;
}

export interface MatchCandidate {
  kind: "receipt" | "reimbursement" | "payment_run" | "issued_payment";
  id: string;
  amount: string;
  currency: string;
  date: string;
  reference: string | null;
  days_off: number;
  score: number;
}

export interface EmailMessage {
  id: string;
  invoice_id: string | null;
  kind: "invoice" | "reminder";
  to_email: string;
  subject: string;
  status: "recorded" | "blocked" | "queued" | "sending" | "sent" | "failed" | "unknown";
  sent_at?: string | null;
  command_key?: string | null;
  error: string | null;
  created_at: string;
}

export interface SendResult {
  message: EmailMessage;
  delivered: boolean;
}

export interface BulkReminderResult {
  sent: number;
  skipped_no_email: number;
  messages: EmailMessage[];
}

export type RecurringFrequency = "weekly" | "monthly" | "quarterly" | "yearly";

export interface RecurringSchedule {
  id: string;
  title: string | null;
  partner_id: string | null;
  frequency: RecurringFrequency;
  interval: number;
  start_date: string;
  next_run_date: string;
  end_date: string | null;
  active: boolean;
  payment_terms_days: number | null;
  last_generated_at: string | null;
  generated_count: number;
  created_at: string;
}

export interface GenerateResult {
  generated: number;
  numbers: string[];
}

export interface WebhookEndpoint {
  id: string;
  url: string;
  events: string;
  description: string | null;
  active: boolean;
  created_at: string;
}

export interface WebhookCreated extends WebhookEndpoint {
  secret: string;
}

export interface WebhookDelivery {
  id: string;
  event_type: string;
  status: "pending" | "delivered" | "failed";
  attempts: number;
  response_code: number | null;
  last_error: string | null;
  created_at: string;
  delivered_at: string | null;
}

export interface IntegrityReport {
  checked: number;
  ok: number;
  healthy: boolean;
  issues: { kind: string; entity_id: string; problem: string; detail: string }[];
}

// HTTP 202 from the integrity routes: the sweep was too large for one request
// and the matching background job was queued instead (PERF-009).
export interface IntegrityQueued {
  queued: true;
  job_id: string;
  job_kind: string;
  references: number;
  sync_limit: number;
  created: boolean;
}

// --- Issuing reports ---
export interface IssuedSummaryReport {
  currency: string;
  available_currencies: string[];
  count: number;
  net: string;
  vat: string;
  gross: string;
  collected: string;
  outstanding: string;
  series: { period: string; net: string; gross: string; count: number }[];
}

export interface IssuedReceivablesReport {
  currency: string;
  available_currencies: string[];
  statuses: { status: IssuedStatus; label: string; count: number; gross: string; outstanding: string }[];
  aging: { label: string; count: number; outstanding: string }[];
  total_outstanding: string;
  overdue_outstanding: string;
  penalty_accrued: string;
  avg_days_to_pay: number | null;
}

export interface IssuedPartnerReport {
  currency: string;
  available_currencies: string[];
  partners: {
    partner: string; vat_number: string | null; count: number;
    net: string; vat: string; gross: string; outstanding: string; last_invoice: string | null;
  }[];
}

export interface IssuedVatReport {
  currency: string;
  available_currencies: string[];
  by_rate: { rate: string; base: string; vat: string }[];
  by_scheme: { scheme: string; net: string; vat: string }[];
  total_net: string;
  total_vat: string;
}

export interface IssuedInvoiceDetail extends IssuedInvoice {
  lines: {
    position: number;
    description: string;
    quantity: string;
    unit: string;
    unit_price: string;
    discount_percent: string;
    vat_rate: string;
    net_amount: string;
  }[];
  vat_breakdown: VatBucket[];
}

// --- Master data & document registry (WO-14 / F1.1) ------------------------

export interface TaxCode {
  id: string;
  code: string;
  name: string;
  rate: string; // Decimal serialised as string — never parse into a float for math
  category: "standard" | "reduced" | "zero" | "exempt" | "reverse_charge" | string;
  country: string | null;
  active: boolean;
  version: number;
}

export interface CurrencyEntry {
  id: string;
  code: string;
  name: string;
  symbol: string | null;
  decimal_places: number;
  active: boolean;
  version: number;
}

export type MasterStatus = "active" | "closed" | "archived";

export interface CostMaster {
  id: string;
  code: string;
  name: string;
  status: MasterStatus;
  version: number;
  department_id?: string | null; // cost centers only
  start_date?: string | null; // projects only
  end_date?: string | null; // projects only
}

export interface DocumentEntry {
  id: string;
  sha256: string;
  size: number;
  mime: string | null;
  kind: string;
  filename: string | null;
  uploaded_by: string | null;
  created_at: string;
}

// --- Composed home dashboard (WO-16 / I1.1) --------------------------------
// Sections are null when the caller lacks that surface's permission (or its
// module is off) — the SPA hides the card; it never fans out N calls.

export interface DashboardApInboxItem {
  invoice_id: string;
  invoice_number: string;
  vendor_name: string | null;
  total: string;
  currency: string;
}

export interface DashboardApprovals {
  invoices: DashboardApInboxItem[] | null;
  invoice_count: number | null;
  expense_reports: number | null;
  payment_runs: number | null;
  vendor_changes: number | null;
  total: number;
}

export interface DashboardCaptures {
  pending: number;
  low_confidence_fields: number;
}

export interface DashboardPayables {
  currency: string;
  due_soon_count: number;
  due_soon_amount: string;
  overdue_count: number;
  overdue_amount: string;
  other_currencies: string[];
}

export interface DashboardReceivables {
  currency: string;
  outstanding: string;
  overdue: string;
  avg_days_to_pay: number | null;
}

export interface DashboardCash {
  currency: string;
  receivables_outstanding: string;
  payables_outstanding: string;
  net_position: string;
}

export interface DashboardData {
  as_of: string;
  approvals: DashboardApprovals | null;
  captures: DashboardCaptures | null;
  payables: DashboardPayables | null;
  receivables: DashboardReceivables | null;
  cash: DashboardCash | null;
}

// ---------------------------------------------------------------------------
// Transport vertical — EU cross-border VAT refund claims (WO-76/WO-77 routes).
// Field-for-field from `backend/app/schemas/transport_claim.py` and
// `transport_admin.py`. Money is `string` on purpose: the backend types those
// columns `Decimal` over `Numeric(14,2)` and pydantic v2 serializes them as JSON
// strings so no float ever appears in a money path (master-context §4.9).
// Render them with `decimalMoney`; never with `Number()`.
// ---------------------------------------------------------------------------

export interface VatClaim {
  id: string;
  entity_id: string;
  refund_country: string;
  ref_period: string;
  /** The coarse ENGINE status: draft/submitted/approved/paid/withdrawn/rejected. */
  status: string;
  /** The workflow code (1A/1B/1C/1E system-derived, 2/2A…5 manual). */
  status_code: string | null;
  status_note: string | null;
  decision_date: string | null;
  action_deadline: string | null;
  submitted_date: string | null;
  approved_date: string | null;
  paid_date: string | null;
  paid_amount: string | null;
  /** Frozen at submission — `null` means "not yet frozen", NOT zero. */
  vat_eur: string | null;
  vat_local: string | null;
  currency: string | null;
  fee_pct: string | null;
  fee_min: string | null;
  fee_eur: string | null;
  created_at: string;
}

export interface VatClaimLine {
  id: string;
  claim_id: string;
  invoice_ref: string;
  vat_id: string | null;
  invoice_id: string | null;
  goods_code: string | null;
  product_group: string | null;
  net_eur: string;
  vat_eur: string;
  net_local: string | null;
  vat_local: string | null;
  currency: string | null;
  frozen_at: string | null;
  /** WO-L §13: set when the member state's decision rejected this line. */
  rejected_at: string | null;
  /** WO-L §11: the suppliers behind an UNMATCHED bucket — a work-item hint,
   * never a filable attribute. Null on resolved lines. */
  unmatched_suppliers: string[] | null;
}

export interface VatChecklistItem {
  key: string;
  label: string;
  scope: string;
  ok: boolean;
  reason: string | null;
}

export interface VatStage {
  stage: string;
}

export interface VatStatusCodes {
  auto: string[];
  manual: string[];
}

export interface VatSubmitInvoice {
  supplier: string;
  invoice_ref: string;
  fuel_transaction_id: string;
}

// ---------------------------------------------------------------------------
// Transport vertical — fuel transactions (WO-79's `GET
// /transport/fuel-transactions`). Field-for-field from
// `backend/app/schemas/transport_fuel.py`, which is itself field-for-field from
// `app/models/transport/fuel_transaction.py`.
//
// Every amount is `string` for the same reason the claim types are: the backend
// types those columns `Decimal` and pydantic v2 serializes them as JSON strings
// (master-context §4.9). `qty` is a string too — it is litres, not money, but it
// crosses the wire as an exact decimal and three decimal places survive only if
// nothing turns it into a double. Render with `decimalMoney`; never `Number()`.
// ---------------------------------------------------------------------------

export interface FuelTransaction {
  id: string;
  entity_id: string;
  /** Fuel-card issuer / toll-network code — free text, admin-curated. */
  supplier: string;
  /** "YYYY-MM" — the accounting month. */
  period: string;
  line_seq: number;
  /** ISO 3166-1 alpha-2 of the country of SUPPLY = the VAT jurisdiction. */
  country: string;
  vehicle_ref: string;
  txn_date: string;
  txn_time: string;
  station: string;
  product: string;
  product_group: string;
  /** Litres / units — NOT money, and deliberately not quantized. */
  qty: string;
  currency: string;
  net_local: string;
  vat_local: string;
  gross_local: string;
  net_eur: string;
  vat_eur: string;
  net_eur_eff: string;
  /**
   * The RAW matching reference off the source document — nullable by design
   * (often unresolved at ingestion). NOT an AP invoice number: the resolved
   * number lives on `VatClaimLine.invoice_ref`.
   */
  invoice_ref: string | null;
  provenance_note: string | null;
  /** The RESOLVED AP invoice — always null today; no service populates it. */
  invoice_id: string | null;
  fx_rate: string | null;
  fx_ecb_rate: string | null;
  fx_ecb_date: string | null;
  fx_source: string | null;
  created_at: string;
}

export interface FuelTransactionList {
  items: FuelTransaction[];
  total: number;
  page: number;
  page_size: number;
}

// ---------------------------------------------------------------------------
// Transport vertical — the ADMIN/CONFIG surfaces (WO-77's `admin.py` +
// `customers.py`, given screens by WO-80). Field-for-field from
// `backend/app/schemas/transport_admin.py`.
//
// Every `Decimal` field on that schema is `string` here for the reason the
// claim types already record: pydantic v2 serializes `Decimal` as a JSON string
// and nothing in the UI may round-trip a figure through a double
// (master-context §4.9). Render with `decimalMoney`; post back the typed string.
// ---------------------------------------------------------------------------

/** `ChecklistRuleOut` — an adjustable submission-checklist rule as DATA (R45).
 * The claim-scoped `VatChecklistItem` is its EVALUATION against one claim. */
export interface VatChecklistRule {
  id: string;
  key: string;
  label: string;
  /** "customer" | "claim" — the grain the rule is evaluated at. */
  scope: string;
  /** "data" today; the evaluator refuses anything it has no verifier for. */
  check_type: string;
  reference: string | null;
  active: boolean;
  sort: number;
}

/** `CadenceOut` — an ADMIN cadence assignment. A supplier with no row still
 * resolves through the service's harvested per-network default, so absence is
 * not "no cadence". */
export interface VatCadence {
  id: string;
  supplier: string;
  cadence: string;
}

/** `FeeRateOut` — one rung of the contingency-fee chain (R13/C11). The chain
 * resolves MOST SPECIFIC FIRST: (customer, country), then customer, then the
 * workspace standard; until one resolves, `lock.submit_claim` refuses
 * `fee_rate_not_configured` rather than invent a charge.
 *
 * `kind` and the two discounts are DERIVED on the way out by comparing this row
 * against the standard — never stored. A stored discount would be a live
 * reference, so raising the standard would silently re-rate every negotiated
 * client. `null` where there is no standard to compare with, which is a
 * different statement from "no discount". Money and percentages are exact
 * decimal STRINGS (§4.9). */
export interface FeeRate {
  id: string;
  /** `null` is the workspace standard — the rung that applies to everyone. */
  entity_id: string | null;
  country: string;
  fee_pct: string;
  fee_min: string;
  kind: "standard" | "discount" | "premium" | "no_standard";
  standard_pct: string | null;
  standard_min: string | null;
  pct_discount: string | null;
  min_discount: string | null;
}

/** `ReceiptControlOut` — one persisted slot of the cadence × activity grid the
 * close's `run_control` stage wrote. ADVISORY: a `missing` slot is a chase-list
 * row, never a gate (master-context §4.19). */
export interface VatReceiptControl {
  id: string;
  entity_id: string;
  supplier: string;
  /** "YYYY-MM". */
  period: string;
  /** "M" | "H1" | "H2" — the cadence's slot key. */
  slot: string;
  /** ISO country, or "" for a cadence that is not per-country. */
  country: string;
  /** "no_activity" | "received_doc" | "received_no_doc" | "missing". */
  status: string;
  txn_count: number;
  /** A worklist MUTE ("stop chasing this slot") — NOT the claim-level legal
   * waiver (R15), which is `VatWaiver` and lives on a claim. */
  waived: boolean;
  note: string | null;
}

/** `NoteOverrideOut` — an admin-curated note→invoice-ref override (R16/C4). It
 * changes only the invoice ASSOCIATION, never an amount. */
export interface VatNoteOverride {
  id: string;
  entity_id: string;
  supplier: string;
  refund_country: string;
  invoice_ref: string;
  target_invoice_id: string;
}

/** `TieOutExpectationOut` — the figures a HUMAN typed off a supplier's invoice
 * PDF for a period. Once typed they HALT the close when the engine disagrees
 * (R25 regime 2); absence is fail-open. */
export interface VatTieOutExpectation {
  id: string;
  entity_id: string;
  supplier: string;
  /** "YYYY-MM". */
  period: string;
  currency: string;
  expected_lines: number;
  expected_gross_local: string | null;
  gross_local_tolerance: string;
  expected_net_eur: string | null;
  expected_gross_eur: string | null;
  expected_diesel_litres: string | null;
}

/** `CountryActivationOut` — one refund country's activation state for an
 * entity. `status` is "requested" | "active"; the absence of a row is the
 * third, unnamed state ((none)) and the R44 gate refuses it. */
/** WO-AG — F3's informational verdict: is every required document kind on
 * file and valid today? Never a gate; the activate click is not refused on it. */
export interface VatCountryReadiness {
  ready: boolean;
  required: string[];
  missing: string[];
  expired: string[];
  /** True when the workspace has configured no set for this country (the default applies). */
  is_default: boolean;
}

export interface VatCountryActivation {
  id: string;
  country: string;
  status: string;
  readiness?: VatCountryReadiness | null;
}

export interface VatCountryRequirement {
  country: string;
  kinds: string[];
}

/** `LifecycleOut` — the customer lifecycle row plus every country activation.
 * `id`/`status` are `null` when the entity was NEVER onboarded — a meaningful
 * absence the R44 gate treats exactly like not-active, never a 404. */
export interface VatLifecycle {
  entity_id: string;
  id: string | null;
  /** "prospect" | "pending" | "active" | "inactive", or null. */
  status: string | null;
  countries: VatCountryActivation[];
}

/** `CountryEstimateOut` — one refund country's share of an uploaded statement
 * (WO-AC, G4.8 / R43). */
export interface VatCountryEstimate {
  country: string;
  lines: number;
  litres: string;
  /** Invoiced VAT in EUR. EXCLUDES any line with no exchange rate. */
  vat_eur: string;
  /** Present only when every line for this country shares one currency. */
  vat_local: string | null;
  currency: string | null;
  /** THREE states: true = below the Art. 17 threshold, false = clears it,
   * null = could not be compared in the country's own currency. `null` is not
   * `false` — see `estimate.py`'s own module docstring. */
  below_minimum: boolean | null;
  threshold: string;
  threshold_currency: string;
  unconverted_lines: number;
}

/** `EstimateOut` — the refund-estimate funnel's whole answer. Nothing behind
 * it was stored: R43's rule is in-memory only, no product-DB write. */
export interface VatEstimate {
  network: string;
  period: string;
  lines: number;
  countries: VatCountryEstimate[];
  recoverable_eur: string;
  unconverted_lines: number;
  warnings: string[];
  /** R53's framing for this analysis. Render it WITH the number, never
   * separately and never omitted. */
  caveat: string;
}

/** `ClaimantDocumentOut` — one document held for a claimant (WO-AB). The
 * `check_type="document"` checklist rules read exactly these rows.
 * `country` is `""` for a customer-scope document (contract, trade register)
 * and an ISO-2 code for a country-scope one (a power of attorney). */
export interface VatClaimantDocument {
  id: string;
  entity_id: string;
  kind: string;
  country: string;
  sha256: string;
  size: number;
  mime: string | null;
  filename: string | null;
  valid_from: string | null;
  /** `null` means NO STATED EXPIRY — which is not the same as expired. */
  valid_until: string | null;
  uploaded_by: string | null;
}

export interface VatClaimantDocumentList {
  entity_id: string;
  documents: VatClaimantDocument[];
  /** The server's own catalogue, so the screen can never offer a kind the
   * CHECK constraint would refuse. */
  kinds: string[];
}

/** `WaiverOut` — a claim-scoped receipt-control waiver (R15): a supplier that
 * genuinely never invoiced for this claim's country. */
export interface VatWaiver {
  id: string;
  claim_id: string;
  supplier: string;
}

/** `RemovedOut` — `removed: false` is the services' idempotent no-op, not a
 * failure. */
export interface VatRemoved {
  removed: boolean;
}

// ---------------------------------------------------------------------------
// Transport vertical — the RECOVERY INTELLIGENCE surfaces (WO-81's
// `recovery.py`, WO-82/WO-83's `overcharges.py`, WO-84's `rebates.py`, given
// screens by WO-86). Field-for-field from `backend/app/schemas/
// transport_recovery.py`, `transport_overcharge.py` and `transport_rebate.py`.
//
// Same money rule as every transport type above: each `Decimal` on those
// schemas is `string` here, rendered with `decimalMoney` and posted back as the
// typed string. Nothing on these screens is summed, re-rounded or converted —
// the server already totalled it (master-context §4.9/§4.10).
// ---------------------------------------------------------------------------

/** `RecoveryBucketOut` — one of the six harvested readiness states
 * (`ready · deadline · missing · below · submitted · paid`). All six always
 * arrive, including the empty ones: a bucket that vanished at zero would make
 * "nothing is at deadline risk" and "the dashboard forgot" identical. */
export interface RecoveryBucket {
  state: string;
  claims: number;
  vat_eur: string;
}

/** `RecoveryExcludedOut` — claims matching none of the six states, named by
 * their engine status (`withdrawn`/`rejected`). Reported rather than dropped so
 * `Σ buckets + Σ excluded == total_claims` holds exactly. */
export interface RecoveryExcluded {
  reason: string;
  claims: number;
}

/** `RecoveryDashboardOut` — one refund year's portfolio. Basis: NET EUR.
 *
 * Two fields must never be rendered as a zero. `median_days_to_refund` is
 * `null` until a paid claim carries BOTH a submitted and a paid date —
 * `days_to_refund_sample` is what makes that legible instead of mysterious, and
 * 0 would claim refunds arrive the same day they are filed.
 * `currency_mismatch_claims` counts drafts whose lines span currencies: they
 * are excluded from every euro below (§4.14), so a non-zero count is the
 * explanation for a total that looks short. */
export interface RecoveryDashboard {
  year: number;
  /** Always "EUR" — the basis of every figure here, stated not assumed. */
  currency: string;
  total_claims: number;
  buckets: RecoveryBucket[];
  excluded: RecoveryExcluded[];
  recovered_eur: string;
  awaiting_eur: string;
  claimable_eur: string;
  /** The SECOND cash stream — booked cash recovered from suppliers for contract
   * breaches. Deliberately OUTSIDE the recovered+awaiting+claimable identity,
   * which is about this year's VAT. */
  overcharges_eur: string;
  /** Counted across every unfiled claim REGARDLESS of its bucket: the buckets
   * say what to do, this says how urgent. Not a seventh bucket. */
  deadline_risk_claims: number;
  currency_mismatch_claims: number;
  median_days_to_refund: string | null;
  days_to_refund_sample: number;
}

/** `ContractTermOut` — one agreed commercial term. Exactly two term types
 * exist, both €/L; at least one figure is always present. */
export interface ContractTerm {
  id: string;
  supplier: string;
  country: string;
  /** A LIKE pattern over the station column; "" = every station. */
  station_like: string;
  product_group: string;
  expected_discount_eur_l: string | null;
  max_net_eur_l: string | null;
  active: boolean;
}

/** `BreachOut` — one fuel line that breaches one agreed term.
 * Basis: NET EUR/L, final — VAT excluded, rebates applied. */
export interface OverchargeBreach {
  fuel_transaction_id: string;
  entity_id: string;
  supplier: string;
  country: string;
  station: string;
  product_group: string;
  txn_date: string;
  qty: string;
  /** PROVENANCE only — never summed, never the unit of any amount here. */
  document_currency: string;
  eur_l_doc: string;
  eur_l_eff: string;
  /** "short discount" | "over ceiling" — the service's own vocabulary. */
  flag: string;
  agreed_eur_l: string;
  actual_eur_l: string;
  gap_eur_l: string;
  recover_eur: string;
}

/** `ContractAuditOut` — the read-only detection half of R41. Reading it changes
 * nothing and gates nothing (§4.19). `price_basis` and `legal_framing` ride the
 * response because the basis must be stated wherever the euro is shown. */
export interface ContractAudit {
  period: string;
  supplier: string | null;
  currency: string;
  price_basis: string;
  legal_framing: string;
  breaches: OverchargeBreach[];
  /** The €-exposure DETECTED — NOT cash recovered. */
  recover_eur: string;
  lines_audited: number;
  lines_without_terms: number;
  lines_skipped_zero_qty: number;
  /** ADVISORY (§4.19): a `(supplier, country)` priced at LIST because its
   * recorded rebate document is missing for this period. Never a gate. */
  source_warnings: string[];
}

/** `OverchargeClaimOut` — one per-(supplier × period) claim-back.
 * `detected_eur` is FROZEN at open (the euro the demand quotes) and never
 * re-derived; `recovered_eur` is booked cash and stays null until the claim-back
 * reaches `recovered`. */
export interface OverchargeClaim {
  id: string;
  supplier: string;
  period: string;
  /** One of `overcharge.OVERCHARGE_STATES`. */
  status: string;
  detected_eur: string;
  lines_count: number;
  recovered_eur: string | null;
  note: string | null;
  currency: string;
  created_at: string;
}

/** `OverchargeTotalOut` — the booked-cash north star alone on the wire. */
export interface OverchargeTotal {
  year: number | null;
  currency: string;
  recovered_eur: string;
}

/** `RebateOut` — one recorded off-invoice rebate document.
 *
 * `amount_eur` is SERVER-RESOLVED through the ECB rate at the document's own
 * date (§4.10): there is no client-supplied EUR figure, and a rebate that could
 * not reach EUR was refused rather than stored (§4.15), which is why
 * `fx_source` here is only ever "eur" or "ecb" — never "unknown". */
export interface OffInvoiceRebate {
  id: string;
  supplier: string;
  country: string;
  /** "YYYY-MM" — the fuel period this rebate ADJUSTS. */
  period: string;
  source_ref: string;
  source_party: string;
  /** The rebate document's OWN date — what the FX conversion is audited
   * against, routinely a later month than `period`. */
  rebate_date: string;
  currency: string;
  amount_local: string;
  amount_eur: string;
  fx_rate: string | null;
  fx_ecb_rate: string | null;
  fx_ecb_date: string | null;
  fx_source: string | null;
  note: string | null;
}

// ---------------------------------------------------------------------------
// The overpay / benchmark analyses (WO-90's screen over the WO-87 routes).
// Field-for-field from `app/schemas/transport_savings.py`.
//
// Two things about these shapes are deliberate and load-bearing.
//
// R52 — THE TWO GRAINS DO NOT RECONCILE. `SameDayOverpay.avoidable_eur` and
// `InternalBenchmark.benchmark_gap_eur` measure different things over the same
// rows at different grains. They are typed under DIFFERENT NAMES for the reason
// the schema module states: a consumer that wanted to add them would have to
// write two different field names to do it. Nothing in this file gives them a
// common supertype, and no helper sums them.
//
// R53 — NO CLAIM-BACK VOCABULARY. Not one field below contains `recover`,
// `owed`, `owes`, `claim`, `demand`, `due`, `debt` or `payable`, because the
// backend has none — an overpay figure is negotiation evidence and not a debt,
// and a field named otherwise would be a false assertion the moment a client
// rendered it. The savings page's spec scans these interfaces for exactly that.
//
// Every money field, every €/L rate and `litres` itself arrive as decimal
// STRINGS (§4.9). `litres` is a string because it is the €/L denominator: a
// float round-trip of it would move the prices computed from it.
// ---------------------------------------------------------------------------

/** One (country × day × supplier) cell that paid above the cheapest RIVAL
 * network trading that day. Basis: NET EUR/L, final. */
export interface SameDayOverpayLine {
  country: string;
  txn_date: string;
  supplier: string;
  litres: string;
  eur_l_eff: string;
  cheapest_rival_supplier: string;
  cheapest_rival_eur_l_eff: string;
  delta_eur_l: string;
  avoidable_eur: string;
  /** Never below 2 — a cell only exists where a real comparison was possible. */
  suppliers_that_day: number;
  lines: number;
}

/** The attribution the analysis is defined to produce: the country of supply and
 * the supplier that charged the premium. */
export interface SupplierOverpayTotal {
  country: string;
  supplier: string;
  litres: string;
  avoidable_eur: string;
  days: number;
}

/** R52 grain (a) — same-day, same-country cheapest rival, diesel only.
 *
 * `days_without_a_rival` is a FACT, not a zero: a day on which only one supplier
 * traded produces no finding because there was no rival to compare against,
 * which is materially different from "you were competitive that day". The screen
 * renders it as its own count and never as a €0.00 line. */
export interface SameDayOverpay {
  period: string;
  country: string | null;
  currency: string;
  price_basis: string;
  legal_framing: string;
  /** The service's own grain label — rendered, never restated (R52). */
  grain: string;
  product_group: string;
  findings: SameDayOverpayLine[];
  by_supplier: SupplierOverpayTotal[];
  avoidable_eur: string;
  lines_compared: number;
  days_compared: number;
  days_without_a_rival: number;
  lines_skipped_zero_qty: number;
}

/** One (country × supplier) cell of the month against the best of your own
 * suppliers in that country — including itself, so the best supplier appears
 * with a zero gap: it is the supplier volume would be routed to. */
export interface InternalBenchmarkRow {
  country: string;
  supplier: string;
  litres: string;
  eur_l_eff: string;
  best_supplier: string;
  best_eur_l_eff: string;
  gap_eur_l: string;
  benchmark_gap_eur: string;
  lines: number;
}

/** R52 grain (b) — country × month, best-of-your-own-suppliers. Self-sourced:
 * every price in it is one this organization itself paid. */
export interface InternalBenchmark {
  period: string;
  country: string | null;
  currency: string;
  price_basis: string;
  legal_framing: string;
  grain: string;
  product_group: string;
  rows: InternalBenchmarkRow[];
  benchmark_gap_eur: string;
  countries_compared: number;
  suppliers_compared: number;
  lines_compared: number;
  lines_skipped_zero_qty: number;
}

/** What a (supplier, country) pair's rebate usually looks like.
 * `learned_from_lines` is the sample behind it — an expectation formed from two
 * lines deserves less trust than one formed from two hundred, and the service
 * states that rather than encoding a minimum it was never given. */
export interface LearnedRebate {
  supplier: string;
  country: string;
  typical_eur_l: string;
  learned_from_lines: number;
}

/** One line whose pair has a learned rebate and which carries none. Both prices
 * are exposed (R49) because their difference is the subject. */
export interface MissingRebateLine {
  fuel_transaction_id: string;
  entity_id: string;
  supplier: string;
  country: string;
  station: string;
  txn_date: string;
  litres: string;
  eur_l_doc: string;
  eur_l_eff: string;
  applied_eur_l: string;
  typical_eur_l: string;
  /** An advisory MAGNITUDE — typical €/L × litres. Never an entitlement. */
  expected_rebate_eur: string;
  learned_from_lines: number;
}

/** The per-LINE half of the rebate analysis (the per-pair existence half is
 * `ContractAudit.source_warnings`). A pair with no rebate-bearing history is
 * SILENT: with no history there is no expectation, and
 * `lines_without_an_expectation` reports how many lines that was. */
export interface ExpectedRebate {
  period: string;
  supplier: string | null;
  currency: string;
  price_basis: string;
  legal_framing: string;
  /** The €/L threshold under which a line counts as carrying no rebate. */
  tolerance_eur_l: string;
  expectations: LearnedRebate[];
  findings: MissingRebateLine[];
  expected_rebate_eur: string;
  lines_examined: number;
  lines_with_a_rebate: number;
  lines_without_an_expectation: number;
  lines_skipped_zero_qty: number;
}

// ---------------------------------------------------------------------------
// The diesel excise-duty refund (WO-92's screen over the WO-91 routes).
// Field-for-field from `app/schemas/transport_excise.py`.
//
// THE CAVEAT FIELDS ARE REQUIRED, AND THAT IS THE POINT. `eligibility`,
// `rate_caveat`, `legal_framing` and `eligibility_asserted` ride every shape
// that carries a euro or a rate, because the conditions that decide whether a
// haulier qualifies for this refund — vehicle weight and carrier registration —
// are deliberately NOT modelled by this product. They are typed non-optional
// here so a panel physically cannot render the number without having received
// the denial beside it, and they are rendered VERBATIM: the strings live in
// `app/services/transport/excise.py` and nowhere in this codebase, so no
// wording can be softened on the way to a screen.
//
// `litres` is a STRING like every euro and every rate. It is the figure's
// multiplicand (`litres / 1000 × rate`), so a float round-trip of a three-
// decimal litre total would move the euro the server computed from it (§4.9).
// Nothing in the SPA re-derives that product — the server has already done it
// once, with one ROUND_HALF_UP, and the screen renders the result (§4.10).
// ---------------------------------------------------------------------------

/** One state's rate as the figure will actually use it. `is_override`
 * distinguishes a rate an operator verified with customs from the harvested
 * EUR 30.00 placeholder — a rate shown without it presents a placeholder as a
 * statutory figure. */
export interface ExciseRate {
  country: string;
  rate_eur_per_1000l: string;
  is_override: boolean;
  rate_caveat: string;
}

/** Every state this product records as operating the regime, each with its
 * resolved rate. A state absent from `countries` has no rate at all, which is
 * not the same as a rate of zero. */
export interface ExciseRates {
  countries: string[];
  default_rate_eur_per_1000l: string;
  rates: ExciseRate[];
  eligibility: string;
  eligibility_asserted: boolean;
  rate_caveat: string;
  legal_framing: string;
  currency: string;
}

/** One (entity × country) cell — the grain of the whole analysis. */
export interface ExciseCell {
  entity_id: string;
  entity_name: string;
  country: string;
  litres: string;
  rate_eur_per_1000l: string;
  rate_is_override: boolean;
  indicative_excise_eur: string;
  lines: number;
}

/** A country with validated diesel litres in scope for which this product holds
 * no rate. It carries litres and a line count and **no euro field at all** —
 * there is no figure, which is a different fact from a figure of zero, and the
 * shape makes rendering one impossible. */
export interface ExciseSkippedCountry {
  country: string;
  litres: string;
  lines: number;
}

/** The diesel excise-duty analysis over one accounting month.
 *
 * `filed_with` names the addressee because this is a SEPARATE regime from the
 * VAT refund, handled by a customs authority; a surface that lost it would
 * invite the figure onto a VAT filing. */
export interface ExciseReport {
  period: string;
  entity_id: string | null;
  country: string | null;
  currency: string;
  product_group: string;
  litre_basis: string;
  legal_framing: string;
  eligibility: string;
  eligibility_asserted: boolean;
  rate_caveat: string;
  filed_with: string;
  rows: ExciseCell[];
  skipped_countries: ExciseSkippedCountry[];
  litres: string;
  indicative_excise_eur: string;
  lines_examined: number;
}

// ---------------------------------------------------------------------------
// The client claim-status portal (WO-93, G4.4/R39) — field-for-field from
// `app/schemas/transport_client_status.py`.
//
// THE FIELD NAMES ARE THE DELIVERABLE. R39's acceptance line is an absence —
// "a client-role session cannot see a status code or a fee anywhere" — and a
// wire type is where that gets undone first: a `status_code` here would be
// rendered by the next person who wrote a column heading. There is no internal
// code, no fee figure and no action verb in any name below, and the backend
// scans both layers for exactly that (`test_wo93_client_surface.py`).
//
// `label` and `description` are the SERVER's plain-language wording, carried on
// the wire. The SPA holds no stage label of its own — a string the SPA owned
// would be a string the SPA could re-word, and this surface's vocabulary is the
// spec's (`BA_fleet_fuel.md` §3.D), not ours.
// ---------------------------------------------------------------------------

/** One of the six harvested client stages: `prep · ready · filed · awaiting ·
 * refunded · needs_attention`. All six always arrive, including the empty ones
 * — a stage that vanished at zero would make "you have nothing in preparation"
 * and "the page forgot" identical. */
export interface ClientClaimStage {
  stage: string;
  /** The server's plain-language name. Rendered verbatim; never re-worded. */
  label: string;
  /** The server's one-sentence explanation. Rendered verbatim. */
  description: string;
  claims: number;
  vat_eur: string;
}

/** One claim, as its owner sees it. No id — a read-only view offers no
 * drill-down, and (entity × country × period) already identifies the row.
 *
 * `vat_eur` is `null` when no single-currency figure can be stated for the
 * claim yet. It is NEVER `"0.00"` as a stand-in, and the page must render the
 * null as a dash rather than as a zero: a client reading €0.00 learns something
 * false. */
export interface ClientClaimRow {
  entity_name: string;
  refund_country: string;
  period: string;
  stage: string;
  vat_eur: string | null;
}

/** `ClientClaimStatusOut` — "where are my claims?", for one refund year.
 * Basis: NET EUR, stated in `currency`.
 *
 * `not_shown_claims` counts claims in an engine state outside the six-stage
 * ladder. It is a COUNT only: the arithmetic stays honest (Σ stages +
 * not_shown === total) while the reason stays internal, which is the whole
 * point of this surface. */
export interface ClientClaimStatus {
  year: number;
  currency: string;
  total_claims: number;
  stages: ClientClaimStage[];
  claims: ClientClaimRow[];
  not_shown_claims: number;
}

/** `BinnedInvoiceOut` — one row of the recycle bin.
 *
 * `days_left` is floored at 0 by the server: a record the purge has not
 * collected yet is still restorable, and a negative number would read as
 * already gone. */
export interface BinnedInvoice {
  invoice_id: string;
  invoice_number: string | null;
  vendor_name: string | null;
  issue_date: string | null;
  currency: string | null;
  total: string | null;
  deleted_at: string;
  deleted_by: string | null;
  days_left: number;
}

/** `ArchivedInvoiceOut` — one sealed record in the platform archive.
 *
 * Deliberately NOT the live `Invoice` shape: an archived record is a historical
 * fact, not a workflow object, so there is no status, no approval and nothing to
 * pay. The field names say whose deletion `original_deleted_at` describes — on
 * this table a bare `deleted_at` would read as "this archive row is deleted",
 * the opposite of what it means. */
export interface ArchivedInvoice {
  id: string;
  original_invoice_id: string;
  invoice_number: string | null;
  vendor_name: string | null;
  issue_date: string | null;
  currency: string | null;
  total: string | null;
  line_items: Record<string, unknown>[];
  has_document: boolean;
  source_filename: string | null;
  original_deleted_at: string | null;
  original_deleted_by: string | null;
  archived_at: string;
  expires_at: string;
}

/** `ArchiveListOut`. Both windows come off the server for the same reason
 * `BinList.retention_days` does — the screen must not be able to promise a
 * retention period the archive does not keep. */
export interface ArchiveList {
  items: ArchivedInvoice[];
  total: number;
  retention_years: number;
  expiry_notice_days: number;
  /** WO-AD: the longest retention any plan offers. When it exceeds
   * `retention_years`, the screen can say that longer keeping is a plan
   * upgrade away — from the server, so the promise has one source. */
  longest_plan_retention_years: number;
}

/** `BinListOut`. `retention_days` comes from the server rather than being
 * hardcoded here, so the promise shown on screen has exactly one source. */
export interface BinList {
  items: BinnedInvoice[];
  total: number;
  retention_days: number;
}

/** One schema-certain reason a deletion is consequential — `services/
 * deletion_consent.py::Consequence`. `code` is stable for branching; `message`
 * is what the person reads. */
export interface DeletionConsequence {
  code: string;
  message: string;
}

/** The warning the server returns with a 409 `deletion_consent_required`.
 *
 * `text` is rendered verbatim and never composed client-side: the audit trail
 * records these exact words as what was accepted, so a locally-written version
 * would make that record false. `version` is echoed back on the retry — consent
 * is to a version, and a stale one is refused. */
export interface DeletionWarning {
  version: string;
  text: string;
  consequences: DeletionConsequence[];
}

/** `BulkDeleteOut` — the result of a multi-select delete.
 *
 * `deleted` counts what moved to the recycle bin, not what was destroyed. Skips
 * carry a reason and are INFORMATION, not errors: an approved or paid invoice
 * being left alone is the system working. */
export interface BulkDeleteResult {
  deleted: number;
  skipped: number;
  failed: number;
  outcomes: BulkOutcome[];
  deleted_records: Record<string, unknown>[];
}

/** `CostEntryOut` — one uninvoiced cost booked onto a project by hand.
 * Deliberately not payroll: a labelled amount, nothing more. */
export interface CostEntry {
  id: string;
  label: string;
  category: string;
  amount: string;
  currency: string;
  entry_date: string | null;
  note: string | null;
  created_by: string | null;
  created_at: string;
}

/** `ProjectDocumentOut` — the signed contract (or other papers) on a project. */
export interface ProjectDocument {
  id: string;
  kind: string;
  filename: string;
  content_type: string | null;
  uploaded_by: string | null;
  created_at: string;
  shared_with_customer: boolean;
}

/** `ProjectPnlOut`. Money is decimal STRINGS off the wire, and `basis` is the
 * server's statement of what the figures are — the screen renders it, never
 * asserts its own. */
export interface ProjectPnl {
  project_id: string;
  code: string;
  name: string;
  status: string;
  revenue: string | null;
  credited: string | null;
  costs: string | null;
  invoice_costs: string | null;
  expense_costs: string | null;
  manual_costs: string | null;
  profit: string | null;
  margin_pct: string | null;
  basis: string;
  basis_version: number;
  reporting_currency: string;
  complete: boolean;
  issues: { code: string; detail: string; currency?: string; source_id?: string; kind?: string }[];
  fx_trace: { source_id: string; kind: string; document_number?: string | null; currency: string; original_amount: string; amount_eur: string | null; rate: string | null; rate_date: string | null; approximate: boolean; allocated_eur?: string }[];
  legacy_figures: Record<string, string>;
  adjustments_complete: boolean;
  /** Non-empty only on a frozen P&L: per-figure deltas for what arrived after
   * the close — displayed drift, never silent drift. */
  adjustments: Record<string, string>;
  pnl_frozen_at: string | null;
  /** The latest ACCEPTED offer's total — null until an offer is accepted. */
  estimated_revenue: string | null;
  /** Acceptance & handover (WO-D) — all null until acceptance is recorded. */
  accepted_at: string | null;
  accepted_by: string | null;
  acceptance_document_id: string | null;
  acceptance_note: string | null;
}

/** One version of one offer — a revision is a new row, history survives. */
export interface ProjectOffer {
  id: string;
  number: string;
  version: number;
  status: string;
  title: string | null;
  currency: string;
  total: string;
  lines: { description?: string; amount?: string }[];
  note: string | null;
  created_by: string | null;
  created_at: string;
}

/** The contracted schedule vs. what was actually issued. */
export interface PlanTracking {
  project_id: string;
  rows: { id: string; label: string; amount: string; position: number }[];
  contracted_total: string;
  issued_total: string | null;
  remaining: string | null;
  basis_version: number;
  reporting_currency: string;
  complete: boolean;
  issues: {code: string; detail: string; currency?: string}[];
}

/** GET/PUT /invoices/{id}/allocation — the same shape both ways, so the editor
 * round-trips without translation. */
export interface InvoiceAllocation {
  invoice_id: string;
  project_id: string | null;
  splits: { project_id: string; percent: string }[];
  lines: Record<string, string>;
}

/** GET /templates — the platform's masters + this org's saved versions. */
export interface TemplateList {
  platform: {
    id: string;
    key: string;
    kind: string;
    name: string;
    description: string | null;
    body: string;
    active: boolean;
  }[];
  own: {
    id: string;
    kind: string;
    name: string;
    body: string;
    active: boolean;
    source_platform_id: string | null;
    created_by: string | null;
    created_at: string;
  }[];
}

// WO-AA — anomaly detection (G4.7 row 7, R54). Every bound the server applied
// was learned from the workspace's own data; nothing here is a fixed price.
export interface Anomaly {
  rule: string;
  subject: string;
  country?: string | null;
  observed: string;
  expected?: string | null;
  deviation?: string | null;
  litres: string;
  detail: string;
  line_seq?: number | null;
  txn_date?: string | null;
}

export interface AnomalyReport {
  period: string;
  anomalies: Anomaly[];
  /** Rules that could not run, with the reason — not the same as finding none. */
  suppressed: { rule: string; reason: string }[];
  count: number;
  rules: string[];
}

// WO-AH — open claims whose R12 action_deadline falls inside a horizon.
export interface DueSoonItem {
  claim_id: string;
  entity_id: string;
  refund_country: string;
  ref_period: string;
  status: string;
  status_code: string | null;
  action_deadline: string;
  /** Negative once the date has passed. */
  days_left: number;
  overdue: boolean;
  /** The claim's own frozen figure as a decimal string; null on a draft. */
  vat_eur: string | null;
}

export interface DueSoon {
  days: number;
  today: string;
  items: DueSoonItem[];
  overdue_claims: number;
}

// WO-AI — one whole-archive export request; the one-time link goes by email only.
export interface ArchiveExportRequest {
  id: string;
  status: "queued" | "ready" | "failed" | "downloaded" | string;
  requested_email: string;
  created_at: string;
  ready_at: string | null;
  link_expires_at: string | null;
  downloaded_at: string | null;
  records: number | null;
  missing_documents: number | null;
  size: number | null;
}

// PROD-009 — one whole-WORKSPACE export request. Same posture as the archive
// export above (the one-time link goes by email only, never over this wire),
// but it covers every table rather than the archive alone, and `purged_at`
// records the day its bytes were destroyed after the link died.
export interface WorkspaceExportRequest {
  id: string;
  status: "queued" | "ready" | "failed" | "downloaded" | string;
  requested_email: string;
  created_at: string;
  ready_at: string | null;
  link_expires_at: string | null;
  downloaded_at: string | null;
  purged_at: string | null;
  rows: number | null;
  tables: number | null;
  documents: number | null;
  missing_documents: number | null;
  size: number | null;
  error: string | null;
}
