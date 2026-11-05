"""Built-in job handlers. Importing this module registers them with the queue.

Kept separate from `jobs.py` (the queue mechanics) so the queue has no dependency
on any particular domain. `app.main` imports this at startup; the worker imports
it too, so both the API (which enqueues) and the worker (which runs) agree on the
set of known kinds.
"""

from __future__ import annotations

from app.core.errors import ValidationError
from app.models.job import Job
from app.services import (
    ap_alerts,
    archive,
    archive_export,
    audit,
    billing,
    billing_usage,
    costing,
    dunning,
    email_intake,
    export_artefacts,
    extraction,
    fx,
    integrity,
    invoice_delivery,
    jobs,
    platform_billing,
    recurring,
    retention,
    webhooks,
    workspace_export,
)
from app.services import bin as bin_svc
from app.services import invoices as invoice_service
from app.services.billing_provider import SubscriptionEvent
from app.services.transport import close as transport_close
from app.services.transport import receipt_control

RECURRING_GENERATE = "recurring.generate"
DUNNING_RUN = "dunning.run"
AP_DUE_ALERTS = "ap.due_alerts"
INTEGRITY_VERIFY = "integrity.verify_documents"
EVERYPAY_CHARGE = "everypay.charge_mit"
RETENTION_PURGE = "retention.purge"
BIN_PURGE = "invoice.bin_purge"
ARCHIVE_PURGE = "archive.purge_expired"
ARCHIVE_NOTICE = "archive.expiry_notice"
USAGE_REPORT = "billing.report_usage"
STRIPE_SUBSCRIPTION_EVENT = "billing.apply_subscription_event"
COSTING_BACKFILL = "costing.backfill_links"
INTEGRITY_LEDGER = "integrity.verify_ledger"
INTEGRITY_VERSIONS = "integrity.verify_versions"
FX_REFRESH = "fx.refresh"
PLATFORM_BILLING_RUN = "platform.bill_subscriptions"
RECEIPT_CONTROL_RUN = "transport.receipt_control"
ARCHIVE_EXPORT = "archive.export"
WORKSPACE_EXPORT = "workspace.export"
EXPORT_PURGE = "export.purge_expired"


@jobs.handler(invoice_delivery.KIND)
async def _invoice_delivery(db, payload: dict, job: Job) -> dict:
    return await invoice_delivery.deliver(db, job.org_id, payload["message_id"])


@jobs.handler(FX_REFRESH)
async def _fx_refresh(db, payload: dict, job: Job) -> dict:
    """Daily ECB reference-rate refresh (WO-8).

    ECB rates are GLOBAL reference data, not per-tenant, so the scheduler
    enqueues this ONCE per day total — carried by a single deterministic org
    (see scheduler.enqueue_daily) because the queue's tenant invariants (org_id
    NOT NULL + RLS) rule out an org-less row. A per-org daily job was rejected:
    it would fetch the same public feed once per tenant, and a handler-side
    cross-org dedupe cannot see sibling jobs through the tenant guard (by
    design). Graceful degradation is preserved end to end: `fx.refresh_from_ecb`
    never raises (12s timeout, returns ok=False on any failure) and a failed
    fetch leaves the cached rates serving; the write is an idempotent upsert, so
    even a duplicate run is harmless."""
    return await fx.refresh_from_ecb(db)


@jobs.handler(USAGE_REPORT)
async def _usage_report(db, payload: dict, job: Job) -> dict:
    """Report the tenant's frozen, retry-safe usage segments to Stripe
    (BILL-METER-001); the result is the quantity ACKNOWLEDGED this run."""
    return {"reported": await billing_usage.report_org_usage(db, job.org_id)}


@jobs.handler(STRIPE_SUBSCRIPTION_EVENT)
async def _stripe_subscription_event(db, payload: dict, job: Job) -> dict:
    """Apply one verified Stripe subscription event from durable queue state
    (BILL-REL-001). The route verified the signature, resolved the tenant and
    persisted the reduced event as this job BEFORE answering Stripe 200, so a
    business fault here retries/dead-letters under the queue's rules instead of
    being swallowed on the request path. `apply_subscription_event` keeps the
    `processed_stripe_events` ledger, so a re-run applies once."""
    event = SubscriptionEvent(
        event_id=str(payload["event_id"]),
        event_type=str(payload["event_type"]),
        customer_id=payload.get("customer_id"),
        subscription_id=payload.get("subscription_id"),
        plan_key=payload.get("plan_key"),
        status=payload.get("status"),
        checkout_session_id=payload.get("checkout_session_id"),
    )
    # Re-resolve at execution time: a provider customer re-bound to another
    # tenant (or unbound) since enqueue must never mutate the queued tenant
    # merely because an old job still names it. Permanent — no retry cures it.
    resolved_org_id = await billing.subscription_event_org_id(db, event)
    if resolved_org_id != job.org_id:
        raise jobs.PermanentJobError(
            f"Stripe customer {event.customer_id} no longer resolves to the queued organization"
        )
    # Durable retry opens a re-ordering window the inline path never had: an
    # `updated{active,pro}` that failed transiently and retries after a later
    # `deleted{canceled}` already applied would put a cancelled tenant back on
    # a paid plan. A job is SUPERSEDED when a newer job for the same
    # subscription has already succeeded; it then applies nothing and records
    # why. (Stripe's `created` is not carried by the reduced event — the
    # order's do-not-change list covers the reducer — so queue order stands in.)
    if event.subscription_id and await _newer_subscription_event_succeeded(db, job, event):
        return {"applied": False, "reason": "superseded"}
    return {"applied": await billing.apply_subscription_event(db, event)}


async def _newer_subscription_event_succeeded(db, job: Job, event: SubscriptionEvent) -> bool:
    import json

    from sqlalchemy import select

    newer = await db.scalars(
        select(Job.payload_json).where(
            Job.org_id == job.org_id,
            Job.kind == STRIPE_SUBSCRIPTION_EVENT,
            Job.status == "succeeded",
            Job.created_at > job.created_at,
        )
    )
    return any(
        json.loads(raw or "{}").get("subscription_id") == event.subscription_id for raw in newer
    )


@jobs.handler(EVERYPAY_CHARGE)
async def _everypay_charge(db, payload: dict, job: Job) -> dict:
    """Charge one tenant's recurring EveryPay MIT for the current period."""
    return await billing.charge_renewal(db, job.org_id)


# BE-024: iterates every tenant on the platform, not one tenant's data.
@jobs.handler(PLATFORM_BILLING_RUN, deadline_seconds=3600.0)
async def _platform_billing_run(db, payload: dict, job: Job) -> dict:
    """Dogfood fallback (H1.6): generate this period's subscription invoice for
    every tenant that owes one and doesn't have it yet. A no-op unless
    `settings.dogfood_billing_enabled`."""
    res = await platform_billing.bill_subscriptions(db)
    return {"invoiced": len(res.invoiced)}


@jobs.handler(RETENTION_PURGE)
async def _retention_purge(db, payload: dict, job: Job) -> dict:
    """Purge one tenant's records past their retention window (unless on hold)."""
    return await retention.purge(db, job.org_id)


@jobs.handler(BIN_PURGE)
async def _bin_purge(db, payload: dict, job: Job) -> dict:
    """Empty one tenant's recycle bin of anything past its 30 days.

    Separate from RETENTION_PURGE on purpose. That one is an opt-in per-tenant
    policy over a record's AGE; this is the fixed promise made when a client
    deletes something, and it must run for every tenant whether or not they have
    configured retention at all — otherwise a binned record is invisible AND
    immortal, which is the worst of both.

    Audited with what was destroyed, not just how many: until the platform
    archive exists, this event is the only remaining trace of the record.
    """
    result = await invoice_service.purge_expired_bin(db, job.org_id)
    if result["purged"]:
        await audit.record(
            db,
            "invoice.bin_purge",
            org_id=job.org_id,
            meta={"purged": result["purged"], "records": result["records"]},
        )
        await db.commit()
    # WO-M: the generic bin (expense reports, inbox transactions, recurring
    # schedules, attachments) empties on the same 30-day promise.
    generic = await bin_svc.purge_expired(db, job.org_id)
    if generic["purged"]:
        await audit.record(
            db,
            "bin.purge",
            org_id=job.org_id,
            meta={"purged": generic["purged"], "records": generic["records"]},
        )
        await db.commit()
    return {
        "held": result["held"],
        "purged": result["purged"] + generic["purged"],
    }


@jobs.handler(ARCHIVE_PURGE)
async def _archive_purge(db, payload: dict, job: Job) -> dict:
    """Destroy one tenant's archive rows past `expires_at`, then their bytes.

    The end of the deletion chain, and until it existed the chain had no end:
    `expires_at` was stamped, published and printed on the client screen while
    nothing enforced it — "kept for three years, then removed" was true only up
    to the comma. In DAILY_KINDS for every tenant, like BIN_PURGE: expiry is a
    promise stated on every archived record, not an opt-in policy.

    Order matters here. The rows are destroyed and AUDITED in one commit (after
    which that event is the only remaining trace of the records), and only then
    are the document bytes collected, best-effort — so a rollback can never
    leave surviving rows pointing at bytes that are already gone. The service
    has already excluded every sha still referenced by a surviving archive row
    or a live invoice's extraction run.
    """
    result = await archive.purge_expired(db, job.org_id)
    if result["purged"]:
        await audit.record(
            db,
            "archive.purge",
            org_id=job.org_id,
            meta={"purged": result["purged"], "records": result["records"]},
        )
        await db.commit()
        collected = await archive.collect_bytes(job.org_id, result["collectable_shas"])
    else:
        collected = 0
    return {"held": result["held"], "purged": result["purged"], "bytes_collected": collected}


@jobs.handler(ARCHIVE_NOTICE)
async def _archive_notice(db, payload: dict, job: Job) -> dict:
    """Warn one tenant's owners about archive records inside the notice window.

    Its own daily kind rather than a rider on ARCHIVE_PURGE: the purge destroys
    and the notice warns, and a failure emailing must never be able to delay a
    purge (or the reverse). Audited with WHICH records were covered — the stamp
    on the rows says "told", the event says told about what, when, to how many
    addresses. `skipped_no_email` surfaces the one silent failure mode this has:
    a tenant whose owners have no address is owed a notice nothing can deliver,
    and the rows stay unstamped so it keeps being owed rather than marked done.
    """
    result = await archive.send_expiry_notices(db, job.org_id)
    if result["sent"] or result["skipped_no_email"]:
        await audit.record(
            db,
            "archive.expiry_notice",
            org_id=job.org_id,
            meta={
                "records": result["records"],
                "sent": result["sent"],
                "skipped_no_email": result["skipped_no_email"],
                "record_ids": result.get("record_ids", []),
                "earliest": result.get("earliest"),
            },
        )
        await db.commit()
    return {
        "sent": result["sent"],
        "records": result["records"],
        "skipped_no_email": result["skipped_no_email"],
    }


@jobs.handler(RECURRING_GENERATE)
async def _recurring_generate(db, payload: dict, job: Job) -> dict:
    """Materialise every recurring invoice due for the job's tenant."""
    res = await recurring.generate_due(db, job.org_id)
    return {"generated": len(res.generated), "numbers": [n for _, n in res.generated]}


@jobs.handler(DUNNING_RUN)
async def _dunning_run(db, payload: dict, job: Job) -> dict:
    """Send a reminder for every overdue invoice for the job's tenant."""
    res = await dunning.run_overdue(db, job.org_id)
    return {"sent": res.sent, "skipped_no_email": res.skipped_no_email}


@jobs.handler(AP_DUE_ALERTS)
async def _ap_due_alerts(db, payload: dict, job: Job) -> dict:
    """Email the tenant a digest of supplier invoices due soon / overdue."""
    return await ap_alerts.send_digest(db, job.org_id)


@jobs.handler(webhooks.WEBHOOK_DELIVER)
async def _webhook_deliver(db, payload: dict, job: Job) -> dict:
    """Deliver one recorded webhook event (signed POST; retries via the queue)."""
    return await webhooks.deliver(db, payload["delivery_id"])


@jobs.handler(email_intake.EXTRACT_KIND)
async def _email_extract(db, payload: dict, job: Job) -> dict:
    """Parse one queued inbound email attachment off the API tier (ADR-0009)."""
    claim = jobs.capture_publication_claim(job)
    return await email_intake.extract_inbound(
        db,
        payload["inbound_id"],
        requested_profile_id=payload.get("reading_profile_id"),
        publication_claim=claim,
    )


@jobs.handler(extraction.UPLOAD_EXTRACT_KIND)
async def _upload_extract(db, payload: dict, job: Job) -> dict:
    """Parse one queued UI direct upload off the API tier (Stage B). Keeps
    CPU-heavy OCR out of the web request path."""
    claim = jobs.capture_publication_claim(job)
    return await extraction.extract_upload(
        db,
        payload["run_id"],
        payload.get("attempt_seq", 0),
        requested_profile_id=payload.get("reading_profile_id"),
        publication_claim=claim,
    )


@jobs.handler("capture.region_ocr")
async def _capture_region_ocr(db, payload: dict, job: Job) -> dict:
    from app.services import capture_region

    return await capture_region.run_region_read(db, payload, job)


@jobs.handler(transport_close.CLOSE_KIND)
async def _transport_close(db, payload: dict, job: Job) -> dict:
    """G1.3 — the transport-vertical monthly close: (re)build live claim
    lines for every draft claim whose scope includes `payload["period"]`
    (`"YYYY-MM"`). See `app.services.transport.close`'s module docstring for
    why this fully satisfies R31/R60's durability requirements on the
    existing job framework, with no new mechanism."""
    return await transport_close.run_close(db, job.org_id, payload["period"])


@jobs.handler(COSTING_BACKFILL)
async def _costing_backfill(db, payload: dict, job: Job) -> dict:
    """Link this tenant's free-text dimensions (invoices + expense items) to
    cost-allocation master rows (dual-read backfill, idempotent)."""
    return {
        "invoices": await costing.backfill_invoice_links(db, job.org_id),
        "expense_items": await costing.backfill_expense_item_links(db, job.org_id),
    }


def _report_dict(report) -> dict:
    return {
        "checked": report.checked,
        "ok": report.ok,
        "issues": len(report.issues),
        "healthy": report.healthy,
    }


# BE-024: re-hashes every stored document; the cost is the tenant's whole
# document corpus, not a fixed batch.
@jobs.handler(INTEGRITY_VERIFY, deadline_seconds=3600.0)
async def _integrity_verify(db, payload: dict, job: Job) -> dict:
    """Re-hash the tenant's stored documents against their recorded sha256."""
    return _report_dict(await integrity.verify_documents(db, job.org_id))


@jobs.handler(INTEGRITY_LEDGER)
async def _integrity_ledger(db, payload: dict, job: Job) -> dict:
    """Verify the tenant's AR-ledger invariants (amount_paid == SUM(payments);
    receipts not over-allocated)."""
    return _report_dict(await integrity.verify_ledger(db, job.org_id))


@jobs.handler(INTEGRITY_VERSIONS)
async def _integrity_versions(db, payload: dict, job: Job) -> dict:
    """Verify the tenant's document-version chain (one current per slot; current
    sha matches the owner cache; no file without a history)."""
    return _report_dict(await integrity.verify_versions(db, job.org_id))


@jobs.handler("assignment.reminder")
async def _assignment_reminder(db, payload: dict, job: Job) -> dict:
    """One upcoming-work reminder to the assignee (WO-B). Armed at exact time
    via run_after when the assignment is created/updated; the service re-checks
    current state so stale jobs (rescheduled/cancelled) no-op or re-arm."""
    from app.services import scheduling

    return await scheduling.send_due_reminder(db, job.org_id, payload["assignment_id"])


@jobs.handler("calendar.sync")
async def _calendar_sync(db, payload: dict, job: Job) -> dict:
    from app.services import calendar_sync

    return await calendar_sync.run_calendar_sync(db, payload, job)


@jobs.handler("schedule.email")
async def _schedule_email(db, payload: dict, job: Job) -> dict:
    from app.services import schedule_email

    return await schedule_email.deliver(db, job.org_id, payload)


@jobs.handler("automation.sweep")
async def _automation_sweep(db, payload: dict, job: Job) -> dict:
    """One tenant's daily automation sweep (WO-J): every published rule
    evaluated over current state, fire-policy ledger consulted, results in
    the visible runs log."""
    from app.services import automation

    result = await automation.sweep(db, job.org_id)
    await db.commit()
    return result


@jobs.handler("assignment.client_notice")
async def _assignment_client_notice(db, payload: dict, job: Job) -> dict:
    """One arrival notice to the project's CUSTOMER (WO-E). Same exact-time
    arming and staleness discipline as the assignee reminder; the recipient
    (project → customer → email) is resolved at send time."""
    from app.services import scheduling

    return await scheduling.send_due_client_notice(db, job.org_id, payload["assignment_id"])


# BE-024: a control pass over one period's statement lines for the whole
# workspace — a large haulier's month is hundreds of thousands of rows.
@jobs.handler(RECEIPT_CONTROL_RUN, deadline_seconds=3600.0)
async def _receipt_control_run(db, payload: dict, job: Job) -> dict:
    """One tenant's receipt-control grid for ONE period (WO-AJ; G3.5 / §3.J).

    `run_receipt_control` was never routed by design — R60: a whole-period
    expectation walk never runs inline in a request — and the period close
    is the only caller today. An operator who wants the chase list refreshed
    between closes had no door. This is that door, on the existing rails:
    enqueue `transport.receipt_control` with `{"period": "YYYY-MM"}`, the
    worker runs the engine, the summary is the job's result. Advisory
    throughout: a `missing` slot is a worklist row, never a gate, and
    overrides survive the re-run (the service's own §3.J item 4 contract).
    """
    period = payload.get("period") if isinstance(payload, dict) else None
    if not isinstance(period, str) or not period:
        raise ValidationError(
            "payload.period (YYYY-MM) is required for a receipt-control run",
            code="invalid_period",
        )
    return await receipt_control.run_receipt_control(db, job.org_id, period)


# BE-024: reads the whole archive and every stored document into one zip, so
# it scales with the tenant's history rather than with a fixed unit of work.
@jobs.handler(ARCHIVE_EXPORT, deadline_seconds=3600.0)
async def _archive_export(db, payload: dict, job: Job) -> dict:
    """WO-AI — build one owner's whole-archive zip and email the one-time link.
    Internal kind only: a request row is created by the archive routes (live
    owner) or the public email form (ex-client), never by the jobs API."""
    export_id = payload.get("export_id") if isinstance(payload, dict) else None
    if not isinstance(export_id, str) or not export_id:
        raise ValidationError("payload.export_id is required", code="invalid_payload")
    return await archive_export.run_export(db, job.org_id, export_id)


# BE-024: the largest job the product has — every table in TENANT_MODELS plus
# every stored document, bounded only by WORKSPACE_EXPORT_MAX_BYTES (128 MB).
@jobs.handler(WORKSPACE_EXPORT, deadline_seconds=3600.0)
async def _workspace_export(db, payload: dict, job: Job) -> dict:
    """PROD-009 — build one workspace's whole-data zip and email the one-time
    link. Internal kind only: the request row is created by an authenticated
    OWNER on the workspace-export route, never by the jobs API."""
    export_id = payload.get("export_id") if isinstance(payload, dict) else None
    if not isinstance(export_id, str) or not export_id:
        raise ValidationError("payload.export_id is required", code="invalid_payload")
    return await workspace_export.run_export(db, job.org_id, export_id)


@jobs.handler(EXPORT_PURGE)
async def _export_purge(db, payload: dict, job: Job) -> dict:
    """PROD-009 — destroy the bytes of every export whose one-time link is
    dead. Daily, every tenant: an expiring link that leaves the file behind is
    not an expiring link."""
    return await export_artefacts.purge_expired(db, job.org_id)


# Kinds an authenticated user is allowed to enqueue via the API (safe, tenant
# -scoped periodic work). Other kinds can only be created internally.
USER_ENQUEUEABLE = (
    RECURRING_GENERATE,
    DUNNING_RUN,
    AP_DUE_ALERTS,
    INTEGRITY_VERIFY,
    INTEGRITY_LEDGER,
    INTEGRITY_VERSIONS,
    COSTING_BACKFILL,
    RECEIPT_CONTROL_RUN,
)


@jobs.handler("automation.deliver")
async def _automation_deliver(db, payload: dict, job: Job) -> dict:
    """One frozen message; sending is committed before SMTP, reclaim is unknown."""
    from app.services import automation_actions

    return await automation_actions.deliver(
        db, job.org_id, payload["run_id"], payload["message_id"]
    )
