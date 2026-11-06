# InvoiceIQ — Generated ER Diagrams (by domain)

> **GENERATED FILE — do not edit by hand.** Derived from the live
> SQLAlchemy metadata by `backend/scripts/gen_erd.py`; the backend CI
> job fails when this file drifts from the models
> (`tests/test_erd_truth.py`). Regenerate with:
> `cd backend && python scripts/gen_erd.py`.
>
> One diagram per domain — the full schema in one picture would be
> illegible. Edges shown inside a diagram are foreign keys whose both
> ends are in the domain; **cross-domain foreign keys** are listed as
> text under each diagram (that coupling reads better as a list). The
> tenancy FK — every tenant table → `organizations` via `org_id`, the
> RLS/guard backbone — is universal and therefore never drawn.
> Companion: [data-model](./data-model.md) (the annotated logical
> model), [diagram-matrix](./diagram-matrix.md) (what we diagram and
> why).

_122 tables across 10 domains._

## Transport VAT recovery (23 tables)

```mermaid
erDiagram
  fuel_extraction_baselines {
  }
  fuel_tieout_expectations {
  }
  supplier_vat_registrations {
  }
  vat_checklist_rules {
  }
  vat_claimant_documents {
  }
  vat_country_activations {
  }
  vat_country_requirements {
  }
  vat_customer_lifecycles {
  }
  vat_excise_rates {
  }
  vat_fee_rates {
  }
  vat_note_invoice_overrides {
  }
  vat_off_invoice_rebates {
  }
  vat_overcharge_claims {
  }
  vat_receipt_controls {
  }
  vat_reliability_thresholds {
  }
  vat_statement_findings {
  }
  vat_supplier_cadences {
  }
  vat_supplier_contract_terms {
  }
  fuel_transactions ||--o{ vat_claimed_invoices : "fuel_transaction_id"
  vat_refund_claims ||--o{ vat_claim_lines : "claim_id"
  vat_refund_claims ||--o{ vat_claimed_invoices : "claim_id"
  vat_refund_claims ||--o{ vat_receipt_waivers : "claim_id"
```

Cross-domain foreign keys:
- `fuel_tieout_expectations.entity_id` → `issuer_profiles` (Issued invoices (AR) & partners)
- `fuel_transactions.entity_id` → `issuer_profiles` (Issued invoices (AR) & partners)
- `fuel_transactions.invoice_id` → `invoices` (Received invoices (AP) & capture)
- `vat_claim_lines.invoice_id` → `invoices` (Received invoices (AP) & capture)
- `vat_claimant_documents.entity_id` → `issuer_profiles` (Issued invoices (AR) & partners)
- `vat_claimed_invoices.entity_id` → `issuer_profiles` (Issued invoices (AR) & partners)
- `vat_country_activations.entity_id` → `issuer_profiles` (Issued invoices (AR) & partners)
- `vat_customer_lifecycles.entity_id` → `issuer_profiles` (Issued invoices (AR) & partners)
- `vat_fee_rates.entity_id` → `issuer_profiles` (Issued invoices (AR) & partners)
- `vat_note_invoice_overrides.entity_id` → `issuer_profiles` (Issued invoices (AR) & partners)
- `vat_note_invoice_overrides.target_invoice_id` → `invoices` (Received invoices (AP) & capture)
- `vat_receipt_controls.entity_id` → `issuer_profiles` (Issued invoices (AR) & partners)
- `vat_refund_claims.entity_id` → `issuer_profiles` (Issued invoices (AR) & partners)

## Received invoices (AP) & capture (15 tables)

```mermaid
erDiagram
  archived_invoices {
  }
  capture_acknowledgements {
  }
  capture_field_memory {
  }
  email_intakes {
  }
  inbound_channel_health {
  }
  capture_commands ||--o{ extraction_runs : "admission_command_id"
  extraction_runs ||--o{ capture_reading_observations : "extraction_run_id"
  extraction_runs ||--o{ capture_region_reads : "extraction_run_id"
  extraction_runs ||--o{ extraction_fields : "extraction_run_id"
  inbound_invoices ||--o{ capture_reading_observations : "inbound_invoice_id"
  invoices ||--o{ extraction_runs : "invoice_id"
  invoices ||--o{ inbound_invoices : "invoice_id"
  invoices ||--o{ line_items : "invoice_id"
  vendors ||--o{ invoices : "vendor_id"
  vendors ||--o{ vendor_change_requests : "vendor_id"
```

Cross-domain foreign keys:
- `capture_region_reads.applied_by` → `users` (Identity & tenancy)
- `capture_region_reads.requested_by` → `users` (Identity & tenancy)
- `invoices.cost_center_id` → `cost_centers` (Analytics & catalogs)
- `invoices.department_id` → `departments` (Analytics & catalogs)
- `invoices.project_id` → `projects` (Projects & scheduling)
- `vendor_change_requests.source_document_id` → `documents` (Platform & compliance)

## Issued invoices (AR) & partners (15 tables)

```mermaid
erDiagram
  dunning_policies {
  }
  offer_stage_events {
  }
  customers ||--o{ customer_contacts : "customer_id"
  customers ||--o{ customer_notes : "customer_id"
  customers ||--o{ customer_portal_tokens : "customer_id"
  customers ||--o{ issued_invoices : "customer_id"
  issued_invoices ||--o{ issued_invoice_attachments : "invoice_id"
  issued_invoices ||--o{ issued_invoice_lines : "invoice_id"
  issued_invoices ||--o{ payments : "issued_invoice_id"
  issuer_profiles ||--o{ issued_invoices : "issuer_id"
  partners ||--o{ issued_invoices : "partner_id"
  partners ||--o{ partner_documents : "partner_id"
  partners ||--o{ recurring_invoices : "partner_id"
  receipts ||--o{ payments : "receipt_id"
```

Cross-domain foreign keys:
- `issued_invoices.project_id` → `projects` (Projects & scheduling)
- `issued_invoices.subscription_org_id` → `organizations` (Identity & tenancy)
- `offer_stage_events.offer_id` → `project_offers` (Projects & scheduling)

## Payments & settlement (5 tables)

```mermaid
erDiagram
  payment_runs {
  }
  reimbursement_batches {
  }
  supplier_payments {
  }
  bank_statements ||--o{ bank_lines : "statement_id"
```

Cross-domain foreign keys:
- `supplier_payments.invoice_id` → `invoices` (Received invoices (AP) & capture)

## Expenses (9 tables)

```mermaid
erDiagram
  expense_approval_policies {
  }
  expense_commands {
  }
  expense_policies {
  }
  expense_import_batches ||--o{ expense_transactions : "import_batch_id"
  expense_items ||--o{ expense_transactions : "item_id"
  expense_reports ||--o{ expense_approval_steps : "report_id"
  expense_reports ||--o{ expense_comments : "report_id"
  expense_reports ||--o{ expense_items : "report_id"
```

Cross-domain foreign keys:
- `expense_commands.employee_id` → `users` (Identity & tenancy)
- `expense_import_batches.employee_id` → `users` (Identity & tenancy)
- `expense_items.cost_center_id` → `cost_centers` (Analytics & catalogs)
- `expense_items.department_id` → `departments` (Analytics & catalogs)
- `expense_items.project_id` → `projects` (Projects & scheduling)
- `expense_reports.employee_id` → `users` (Identity & tenancy)
- `expense_transactions.employee_id` → `users` (Identity & tenancy)

## Projects & scheduling (15 tables)

```mermaid
erDiagram
  action_dismissals {
  }
  calendar_feed_tokens {
  }
  org_deadlines {
  }
  org_templates {
  }
  platform_templates {
  }
  calendar_connections ||--o{ calendar_event_mappings : "connection_id"
  calendar_connections ||--o{ calendar_oauth_attempts : "connection_id"
  projects ||--o{ invoice_project_splits : "project_id"
  projects ||--o{ invoicing_plan_rows : "project_id"
  projects ||--o{ project_assignments : "project_id"
  projects ||--o{ project_cost_entries : "project_id"
  projects ||--o{ project_documents : "project_id"
  projects ||--o{ project_offers : "project_id"
```

Cross-domain foreign keys:
- `calendar_connections.user_id` → `users` (Identity & tenancy)
- `calendar_oauth_attempts.session_id` → `sessions` (Identity & tenancy)
- `calendar_oauth_attempts.user_id` → `users` (Identity & tenancy)
- `invoice_project_splits.invoice_id` → `invoices` (Received invoices (AP) & capture)

## Automation (3 tables)

```mermaid
erDiagram
  automation_rules ||--o{ automation_rule_versions : "rule_id"
  automation_rules ||--o{ automation_runs : "rule_id"
```

## Analytics & catalogs (7 tables)

```mermaid
erDiagram
  budget_targets {
  }
  currencies {
  }
  ecb_rates {
  }
  supplier_agreed_prices {
  }
  tax_codes {
  }
  departments ||--o{ cost_centers : "department_id"
```

Cross-domain foreign keys:
- `supplier_agreed_prices.vendor_id` → `vendors` (Received invoices (AP) & capture)

## Identity & tenancy (8 tables)

```mermaid
erDiagram
  invitations {
  }
  organizations {
  }
  sso_connections {
  }
  sso_login_attempts {
  }
  users ||--o{ auth_tokens : "user_id"
  users ||--o{ memberships : "user_id"
  users ||--o{ sessions : "user_id"
```

## Platform & compliance (22 tables)

```mermaid
erDiagram
  approval_policies {
  }
  approval_steps {
  }
  archive_exports {
  }
  audit_events {
  }
  billing_payments {
  }
  document_versions {
  }
  documents {
  }
  email_messages {
  }
  invoice_attachments {
  }
  invoice_comments {
  }
  jobs {
  }
  legal_holds {
  }
  org_modules {
  }
  plan_policies {
  }
  planning_commands {
  }
  processed_stripe_events {
  }
  reconciliation_commands {
  }
  retention_policies {
  }
  usage_counters {
  }
  workspace_exports {
  }
  webhook_endpoints ||--o{ webhook_deliveries : "endpoint_id"
```

Cross-domain foreign keys:
- `approval_steps.invoice_id` → `invoices` (Received invoices (AP) & capture)
- `invoice_attachments.invoice_id` → `invoices` (Received invoices (AP) & capture)
- `invoice_comments.invoice_id` → `invoices` (Received invoices (AP) & capture)
- `planning_commands.actor_user_id` → `users` (Identity & tenancy)
- `reconciliation_commands.actor_user_id` → `users` (Identity & tenancy)
- `reconciliation_commands.line_id` → `bank_lines` (Payments & settlement)

