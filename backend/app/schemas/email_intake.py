from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.core.config import settings
from app.schemas.capture_reading import ReadingObservationSummary
from app.schemas.image_reading import ImageReadingPreparation
from app.schemas.invoice import InvoiceCreate, ParsedInvoiceDraft
from app.services.filesec import MAX_INBOUND_ATTACHMENTS


class EmailSettingsOut(BaseModel):
    address: str
    domain: str
    # Counts for the inbox badge.
    pending: int = 0
    total: int = 0


class ChannelHealthOut(BaseModel):
    """H-2 — the health of one inbound channel.

    `headline` is the sentence the UI shows. It is composed server-side and
    states the situation POSITIVELY ("last successful delivery: 4 days ago"),
    because absence is the whole signal here and an empty screen reads as fine.

    `overdue` is True only when a cadence was STATED and the gap exceeds it. With
    `expected_cadence_days` unset we report the elapsed time and make no claim —
    one customer's fortnight of quiet is another's outage.
    """

    channel: str
    state: str  # ok | failing | silent | never_used
    headline: str
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    days_since_success: int | None = None
    consecutive_failures: int = 0
    last_error_kind: str | None = None
    last_error_text: str | None = None
    last_error_at: datetime | None = None
    expected_cadence_days: int | None = None
    overdue: bool = False


class ChannelCadenceIn(BaseModel):
    """How often this workspace expects deliveries. `null` withdraws the
    expectation rather than resetting it to a default — there is no honest
    default, and a guessed threshold produces alarms nobody trusts."""

    expected_cadence_days: int | None = Field(default=None, ge=1, le=365)


#: BE-020 (audit 2026-09-05): the JSON webhook buffers every attachment in
#: memory before any is written (a malformed one must fail the whole message
#: cleanly), so the payload is BOUNDED where it is parsed. One attachment may
#: carry at most the same bytes a direct upload may (`settings.max_upload_mb`,
#: base64 grows them by 4/3), and a message may carry at most
#: MAX_INBOUND_ATTACHMENTS of them. The body as a whole is capped by nginx
#: (`client_max_body_size`) in front of this route.
MAX_ATTACHMENT_BASE64_CHARS = ((settings.max_upload_mb * 1024 * 1024 + 2) // 3) * 4


class InboundAttachment(BaseModel):
    filename: str = Field(min_length=1, max_length=300)
    content_type: str | None = Field(default=None, max_length=120)
    content_base64: str = Field(min_length=1, max_length=MAX_ATTACHMENT_BASE64_CHARS)


class InboundEmailIn(BaseModel):
    """Inbound webhook payload from an email provider's parse hook. `token` may be
    given explicitly or derived from the recipient (`to`) address."""

    to: str | None = None
    token: str | None = None
    from_addr: str | None = Field(default=None, alias="from")
    subject: str | None = None
    secret: str | None = None
    #: BE-009: the provider's Message-ID (RFC 5322). Optional; when present, a
    #: redelivery of the same message is acknowledged and stored once.
    message_id: str | None = Field(default=None, max_length=255)
    attachments: list[InboundAttachment] = Field(
        default_factory=list, max_length=MAX_INBOUND_ATTACHMENTS
    )

    model_config = ConfigDict(populate_by_name=True)


class InboundResult(BaseModel):
    received: int
    # Accepted for extraction (bytes stored, an extract job enqueued). Parsing
    # runs on the worker tier, so the parsed/failed outcome appears in the inbox
    # afterwards, not in this synchronous webhook response.
    queued: int
    rejected: int = 0  # blocked by the security gate (malware / bad type)
    #: BE-009: this delivery repeated a Message-ID already in the inbox; nothing
    #: was stored again. The provider gets its 200 and stops retrying.
    deduplicated: bool = False


class InboundInvoiceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    from_addr: str | None
    subject: str | None
    received_at: datetime
    filename: str
    content_type: str | None
    size: int
    status: str
    method: str | None
    error: str | None
    invoice_id: str | None
    created_at: datetime


class InboundInvoiceDetail(InboundInvoiceOut):
    draft: ParsedInvoiceDraft | None = None
    has_file: bool = False
    reading_preparation: ImageReadingPreparation | None = None
    reading_observation: ReadingObservationSummary | None = None


class InboundListOut(BaseModel):
    items: list[InboundInvoiceOut]
    total: int


class InboundConfirm(BaseModel):
    """Optional edited draft to confirm with. When omitted, the stored parsed
    draft is used as-is."""

    draft: InvoiceCreate | None = None
