# ADR-0020 — GDPR right-to-erasure (DSAR) that respects statutory retention

**Status:** Accepted — implemented (Phase 4).

## Context
GDPR Art. 17 gives a data subject the right to erasure. For a B2B invoice platform this is **not** an unconditional delete: Art. 17(3)(b) exempts data the controller must keep to meet a **legal obligation** (statutory accounting retention of tax invoices), and an active **legal hold** (litigation preservation, ADR-0019) overrides erasure entirely. Silently hard-deleting a required invoice — or silently ignoring the request — are both wrong. We also must not break the tamper-evident audit chain, and we must be able to *prove* we handled the request (accountability) without re-storing the subject's identity forever.

## Selected approach
A DSAR is keyed by the subject's **email**. Each personal-data location resolves to one of three outcomes, surfaced in a **preview** before anything runs:

| Location | Outcome | How |
|---|---|---|
| User accounts | **erase** | **pseudonymise, not delete** — email → `erased-<hash>@erased.invalid`, name redacted, account deactivated. The row stays so audit attribution + foreign keys remain intact. |
| Expense-report author name | **erase** | redact `employee_name` on that person's reports. |
| Inbound email records | **erase** | delete the row **and** its stored attachment bytes. |
| Issued tax invoices (buyer) | **retain** | statutory record; buyer identity is required content. Exempt under Art. 17(3)(b). Reported, untouched. |
| Audit trail (actor) | **retain** | tamper-evident integrity record; redacting it would break the hash chain. Reported, untouched. |
| Stored export snapshots | **retain** | Conservatively report every unpurged stored archive/workspace snapshot in this tenant as potentially affected, independent of requester. Report the existing link-expiry/download-grace purge eligibility range, unknown deadlines and legal-hold suspension. Dates are eligibility, not proof of deletion; contents are not scanned during erasure. No new artifact-purge policy. |

An active legal hold turns every erasable location into **blocked** and the execute call is a no-op. Each executed erasure is written to the audit trail as `privacy.erasure` with a **hashed** subject reference + per-location counts — never the cleartext email or any raw value — so the request is provably handled without re-storing the subject's identity. Endpoints are admin-gated and tenant-scoped; the same admin appears as the audit actor.

## Alternatives considered
- **Hard-delete everything matching the subject** — violates statutory retention, breaks FKs + the audit chain, and destroys the tenant's own books. Rejected.
- **Refuse erasure whenever any statutory record exists** — over-broad; the subject's *erasable* data (login PII, inbound email, expense author name) should still go. Rejected in favour of per-location classification.
- **Anonymise in place everywhere (no deletes)** — fine for structured rows, but leaves raw inbound attachment bytes; we delete those. Kept pseudonymisation only where a delete would break integrity/FKs.
- **A dedicated DSAR request table** — nice for a register, but the erasure is already provable via the hash-chained audit trail (and exportable via the audit export). Deferred to avoid a table until a formal DSAR register/SLA is needed.

## Risks
- **Incomplete coverage** (a personal-data field not in the location registry) → the registry is explicit and centralised; new personal-data columns must be added to it. A completeness review is a periodic control.
- **Pseudonymised-but-inferrable** (retained invoices still name the buyer) → that's the deliberate statutory-retention boundary, surfaced in the report, not a leak.
- **Irreversibility** → a preview-before-execute step, admin gating, and a UI confirm; a legal hold blocks it entirely.

## Revisit when
A formal DSAR register / response-SLA is required (add a request table + workflow), erasure must run as an async job for very large subjects, or new personal-data locations (e.g. free-text notes) need scanning/redaction.
