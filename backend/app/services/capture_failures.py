"""The failed-capture worklist (H-1) — a typed outcome contract for a capture
that did not become an invoice, and the queue an operator works it from.

WHY THIS EXISTS
---------------
Before this module a failed capture was reachable only by polling
`GET /invoices/upload/{run_id}` — which requires already knowing the id. Nothing
enumerated failures for a tenant. `extraction.pending_review_filters` covers the
*parsed* queue only. The result is the worst shape a document pipeline can take:
the customer believes the document was processed, and it was not, and no screen
disagrees.

THE CONTRACT
------------
A failure is recorded as a STABLE CODE from the closed vocabulary in `KINDS`,
never as free-form prose. The raw error message is still kept (it is what an
engineer needs) but it is not what the operator is shown: each code carries a
`summary` of what happened and a `remediation` sentence telling them what to DO.
An error a user cannot act on sends them to support, so `retry_helps` and
`user_fixable` are part of the contract rather than something a screen guesses.

`code_for` maps an exception to a code. It classifies on the exception *type*
(`CaptureError.code`, raised at the specific known sites), never by matching
words in a message — a classifier that reads prose breaks silently the day
someone rewords a message. What we genuinely cannot classify becomes
`internal_error`, and rows written before this module existed report
`unknown_failure`. Neither invents a cause.

WHAT SUCCEEDED
--------------
A failure statement that says only "failed" is incomplete: the uploaded document
IS retained (object storage, keyed by sha256) except when the stored copy is
missing or its bytes no longer match the original. `WorklistItem.document_retained`
says so explicitly, because "we lost your reading of the file" and "we lost your
file" are very different sentences to the person who sent it.

ACKNOWLEDGEMENT
---------------
Acknowledging is a RECORD (who, when, an optional note), not a boolean — an
append-only `capture_acknowledgements` row. It is also scoped in TIME: the ack
stores the failure timestamp it was made against, so a capture that fails AGAIN
after being acknowledged returns to the worklist instead of staying silently
dismissed.

Read-only over the two capture channels; this module never re-runs a capture and
never mutates a draft.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import case, exists, func, literal, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.capture_acknowledgement import CaptureAcknowledgement
from app.models.email_intake import InboundInvoice
from app.models.extraction_run import ExtractionRun
from app.services import bulk

# The two channels a document can arrive through. `upload` is a direct UI/API
# upload (an `extraction_runs` row); `email` is an inbound attachment (an
# `inbound_invoices` row). The worklist unions them because the operator's
# question — "what did we fail to read?" — does not care how it arrived.
CHANNEL_UPLOAD = "upload"
CHANNEL_EMAIL = "email"
CHANNELS = (CHANNEL_UPLOAD, CHANNEL_EMAIL)


class CaptureError(ValueError):
    """A capture failure with a classified cause.

    Subclasses `ValueError` deliberately: every existing `except ValueError`
    around the parse path keeps behaving exactly as it did, so raising this
    instead of a bare `ValueError` at a known site is additive.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class FailureKind:
    """One entry of the closed outcome vocabulary.

    `summary` and `remediation` are written for a finance operator, not an
    engineer: no library names, no stack vocabulary, and the remediation is an
    instruction rather than a description of the problem.
    """

    code: str
    summary: str
    remediation: str
    # Does re-running the SAME stored bytes stand any chance of succeeding?
    # False means the worklist must not offer a Retry button that cannot work.
    retry_helps: bool
    # Can the person looking at the worklist resolve this themselves, or does it
    # need an administrator / support?
    user_fixable: bool


UNSUPPORTED_FORMAT = "unsupported_format"
MALFORMED_DOCUMENT = "malformed_document"
UNREADABLE_SCAN = "unreadable_scan"
IMAGE_PROCESSING_LIMIT = "image_processing_limit"
IMAGE_READING_BUSY = "image_reading_busy"
CAPTURE_UNAVAILABLE = "capture_unavailable"
STORED_FILE_MISSING = "stored_file_missing"
STORED_FILE_TOO_LARGE = "stored_file_too_large"
STORED_FILE_CHANGED = "stored_file_changed"
SECURITY_REJECTED = "security_rejected"
PROCESSING_TIMEOUT = "processing_timeout"
INTERNAL_ERROR = "internal_error"
UNKNOWN_FAILURE = "unknown_failure"

KINDS: dict[str, FailureKind] = {
    UNSUPPORTED_FORMAT: FailureKind(
        code=UNSUPPORTED_FORMAT,
        summary="This kind of file cannot be read as an invoice.",
        remediation=(
            "Send the invoice as a PDF, an XML e-invoice, a CSV, or a JPG/PNG scan. "
            "Uploading the same file again will not help."
        ),
        retry_helps=False,
        user_fixable=True,
    ),
    MALFORMED_DOCUMENT: FailureKind(
        code=MALFORMED_DOCUMENT,
        summary="The file is a supported type, but no invoice could be read out of it.",
        remediation=(
            "Open the file to check it is the invoice itself and is not damaged or "
            "password-protected, then send it again. If it opens correctly for you, "
            "forward it to support."
        ),
        retry_helps=False,
        user_fixable=True,
    ),
    UNREADABLE_SCAN: FailureKind(
        code=UNREADABLE_SCAN,
        summary="No text could be recovered from this scan.",
        remediation=(
            "Rescan the page straight, in full, at 300 dpi or better and send it again — "
            "or enter this invoice by hand."
        ),
        retry_helps=False,
        user_fixable=True,
    ),
    IMAGE_PROCESSING_LIMIT: FailureKind(
        code=IMAGE_PROCESSING_LIMIT,
        summary="This image reached the safe reading limits.",
        remediation=(
            "Use a smaller image or read a smaller area from the original, "
            "or enter the invoice manually. Check any suggested fields against the original. "
            "The original is still stored. Retrying unchanged will not help."
        ),
        retry_helps=False,
        user_fixable=True,
    ),
    CAPTURE_UNAVAILABLE: FailureKind(
        code=CAPTURE_UNAVAILABLE,
        summary="Document reading is not switched on for this server.",
        remediation=(
            "Nothing is wrong with the file. Ask your administrator to enable document "
            "reading, then retry this capture — the original is still stored."
        ),
        retry_helps=True,
        user_fixable=False,
    ),
    IMAGE_READING_BUSY: FailureKind(
        code=IMAGE_READING_BUSY,
        summary="Image reading is busy with another document.",
        remediation=(
            "Wait for the current reading to finish, then retry this capture. "
            "Nothing is wrong with the file; the original is still stored."
        ),
        retry_helps=True,
        user_fixable=True,
    ),
    STORED_FILE_MISSING: FailureKind(
        code=STORED_FILE_MISSING,
        summary="The stored copy of this document could not be found.",
        remediation=(
            "Send the document again. Retrying this capture cannot work — there is "
            "nothing left to re-read."
        ),
        retry_helps=False,
        user_fixable=True,
    ),
    STORED_FILE_TOO_LARGE: FailureKind(
        code=STORED_FILE_TOO_LARGE,
        summary="The stored document exceeds this server's processing size limit.",
        remediation=(
            "Ask your administrator to check the stored original and the configured upload limit. "
            "The original is still stored. Retrying unchanged will not help."
        ),
        retry_helps=False,
        user_fixable=False,
    ),
    STORED_FILE_CHANGED: FailureKind(
        code=STORED_FILE_CHANGED,
        summary="The stored document could not be verified as the original.",
        remediation=(
            "Ask your administrator to restore the original document before retrying. "
            "Reading was stopped to protect the invoice. Retrying unchanged will not help."
        ),
        retry_helps=False,
        user_fixable=False,
    ),
    SECURITY_REJECTED: FailureKind(
        code=SECURITY_REJECTED,
        summary="The file was refused by the security check and was never read.",
        remediation=(
            "Check the attachment is the invoice you meant to send and came from the "
            "supplier. If you are certain it is safe, ask your administrator before "
            "sending it again."
        ),
        retry_helps=False,
        user_fixable=False,
    ),
    PROCESSING_TIMEOUT: FailureKind(
        code=PROCESSING_TIMEOUT,
        summary="Reading this document took longer than the server allows.",
        remediation=(
            "Retry the capture once. If it times out again, send a shorter or clearer "
            "scan, enter the invoice by hand, or contact support."
        ),
        # The SAME bytes may well succeed on a quieter worker — unlike an
        # unreadable scan, the budget, not the document, decided this outcome.
        retry_helps=True,
        user_fixable=True,
    ),
    INTERNAL_ERROR: FailureKind(
        code=INTERNAL_ERROR,
        summary="The capture stopped on an error on our side.",
        remediation=(
            "The document is still stored. Retry the capture once; if it fails again, "
            "contact support and quote the reference shown here."
        ),
        retry_helps=True,
        user_fixable=False,
    ),
    UNKNOWN_FAILURE: FailureKind(
        code=UNKNOWN_FAILURE,
        summary="This capture failed before the reason was being recorded.",
        remediation=(
            "The document is still stored. Retry the capture — a fresh attempt will "
            "record why if it fails again."
        ),
        retry_helps=True,
        user_fixable=False,
    ),
}


def kind_for(code: str | None) -> FailureKind:
    """The typed outcome for a stored code. An unrecognised or absent code is
    `unknown_failure` — deliberately NOT `internal_error`: "we did not record
    why" and "something broke on our side" are different claims, and only one of
    them is true of a row written before this contract existed."""
    return KINDS.get(code or "", KINDS[UNKNOWN_FAILURE])


def code_for(exc: BaseException) -> str:
    """Classify a capture exception.

    Type-driven on purpose. A `CaptureError` states its own code at the site that
    knows the cause. A plain `ValueError` is the parse layer's documented "bad
    input" signal, so it means the bytes could not be read as an invoice. Anything
    else escaped a provider unclassified and is ours, not the document's.
    """
    if isinstance(exc, CaptureError):
        return exc.code if exc.code in KINDS else INTERNAL_ERROR
    if isinstance(exc, ValueError):
        return MALFORMED_DOCUMENT
    return INTERNAL_ERROR


@dataclass
class WorklistItem:
    """One failed capture, as an operator sees it."""

    channel: str
    ref_id: str
    code: str
    summary: str
    remediation: str
    retry_helps: bool
    user_fixable: bool
    # The engineer's copy of the error. Shown behind a disclosure, never as THE
    # explanation — it is the message a library produced, not advice.
    detail: str | None
    source_filename: str | None
    sha256: str | None
    # False for missing/mismatched originals: a reference is not proof that
    # the original can be read back. Changed bytes are never deleted here.
    document_retained: bool
    failed_at: datetime
    # How many failed captures in this tenant share these exact bytes. 1 means
    # this document failed once; more means the same file keeps coming back and
    # re-sending it is not the answer.
    repeat_count: int
    acknowledged_at: datetime | None
    acknowledged_by: str | None
    acknowledgement_note: str | None


@dataclass
class FailureGroup:
    """Repeats grouped by cause, so a single systemic breakage reads as one line
    rather than as forty identical rows the operator has to infer a pattern from."""

    code: str
    summary: str
    remediation: str
    count: int
    unacknowledged: int


@dataclass
class Worklist:
    items: list[WorklistItem]
    groups: list[FailureGroup]
    total: int
    unacknowledged: int


def _ack_key(channel: str, ref_id: str) -> tuple[str, str]:
    return (channel, ref_id)


async def _latest_acks(
    db: AsyncSession, org_id: str, keys: list[tuple[str, str]]
) -> dict[tuple[str, str], CaptureAcknowledgement]:
    """The most recent acknowledgement per (channel, ref). The table is
    append-only — every ack is kept — so "current" is a read-time decision, which
    is what makes the history survivable."""
    if not keys:
        return {}
    rows = list(
        await db.scalars(
            select(CaptureAcknowledgement)
            .where(
                CaptureAcknowledgement.org_id == org_id,
                CaptureAcknowledgement.ref_id.in_([k[1] for k in keys]),
            )
            .order_by(CaptureAcknowledgement.created_at.asc())
        )
    )
    out: dict[tuple[str, str], CaptureAcknowledgement] = {}
    for r in rows:
        out[_ack_key(r.channel, r.ref_id)] = r  # ascending order → last write wins
    return out


def _sources(org_id: str):
    """The two failure sources projected onto ONE column set, as a UNION ALL.

    Doing the union in SQL (rather than loading both tables and concatenating in
    Python) is what lets the count, the grouping and the page all be computed by
    the database over the SAME definition. The previous version derived them from
    a fully-materialised list, which is correct and does not scale — and the load
    grows with exactly the failure this screen exists to surface.
    """
    runs = select(
        literal(CHANNEL_UPLOAD).label("channel"),
        ExtractionRun.id.label("ref_id"),
        func.coalesce(ExtractionRun.failure_code, literal(UNKNOWN_FAILURE)).label("code"),
        ExtractionRun.note.label("detail"),
        ExtractionRun.source_filename.label("filename"),
        ExtractionRun.source_sha256.label("sha"),
        ExtractionRun.updated_at.label("failed_at"),
        ExtractionRun.failure_seq.label("failure_seq"),
    ).where(ExtractionRun.org_id == org_id, ExtractionRun.status == "failed")

    inbound = select(
        literal(CHANNEL_EMAIL).label("channel"),
        InboundInvoice.id.label("ref_id"),
        func.coalesce(
            InboundInvoice.failure_code,
            # A `rejected` row predating the failure-code column was refused by
            # the security gate; that is knowable from its status alone.
            case(
                (InboundInvoice.status == "rejected", literal(SECURITY_REJECTED)),
                else_=literal(UNKNOWN_FAILURE),
            ),
        ).label("code"),
        InboundInvoice.error.label("detail"),
        InboundInvoice.filename.label("filename"),
        InboundInvoice.sha256.label("sha"),
        InboundInvoice.updated_at.label("failed_at"),
        InboundInvoice.failure_seq.label("failure_seq"),
    ).where(
        InboundInvoice.org_id == org_id,
        InboundInvoice.status.in_(("failed", "rejected")),
    )
    return union_all(runs, inbound).subquery("failures")


def _unacknowledged(src, org_id: str):
    """The predicate for "no acknowledgement currently covers this failure".

    An ack covers a failure only when it was made AT OR AFTER it — the same rule
    `_superseded` applies in Python, expressed once more in SQL because the
    database has to filter and count on it. If the two ever disagree the page and
    its header disagree, so a test drives both.
    """
    return ~exists(
        select(literal(1)).where(
            CaptureAcknowledgement.org_id == org_id,
            CaptureAcknowledgement.ref_id == src.c.ref_id,
            CaptureAcknowledgement.channel == src.c.channel,
            # F-06: integers, so a re-failure can never collide with the
            # acknowledgement that preceded it.
            CaptureAcknowledgement.failure_seq >= src.c.failure_seq,
        )
    )


async def worklist_page(
    db: AsyncSession,
    org_id: str,
    *,
    page: int = 1,
    page_size: int = 50,
    include_acknowledged: bool = False,
) -> Worklist:
    """One PAGE of the worklist, with the totals and grouping computed in SQL.

    `total`, `unacknowledged` and `groups` describe the whole filtered set, not
    the page — a header that only counted the rows in front of you would say
    "3 problems" on page one of forty. The items are the page; the numbers are
    the truth about all of it.
    """
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    src = _sources(org_id)
    where = [] if include_acknowledged else [_unacknowledged(src, org_id)]

    total = int(await db.scalar(select(func.count()).select_from(src).where(*where)) or 0)
    unack = int(
        await db.scalar(
            select(func.count()).select_from(src).where(*where, _unacknowledged(src, org_id))
        )
        or 0
    )

    group_rows = list(
        await db.execute(
            select(src.c.code, func.count().label("n"))
            .where(*where)
            .group_by(src.c.code)
            .order_by(func.count().desc(), src.c.code.asc())
        )
    )
    unack_by_code = {
        r.code: r.n
        for r in await db.execute(
            select(src.c.code, func.count().label("n"))
            .where(*where, _unacknowledged(src, org_id))
            .group_by(src.c.code)
        )
    }
    groups = []
    for r in group_rows:
        k = kind_for(r.code)
        groups.append(
            FailureGroup(
                code=k.code,
                summary=k.summary,
                remediation=k.remediation,
                count=r.n,
                unacknowledged=int(unack_by_code.get(r.code, 0)),
            )
        )

    rows = list(
        await db.execute(
            select(src)
            .where(*where)
            .order_by(src.c.failed_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )

    # Enrichment is bounded to the PAGE: acknowledgement details and repeat
    # counts are looked up for these rows only, never for the whole table.
    acks = await _latest_acks(db, org_id, [_ack_key(r.channel, r.ref_id) for r in rows])
    shas = [r.sha for r in rows if r.sha]
    repeats: dict[str, int] = {}
    if shas:
        repeats = {
            r.sha: r.n
            for r in await db.execute(
                select(src.c.sha, func.count().label("n"))
                .where(src.c.sha.in_(shas))
                .group_by(src.c.sha)
            )
        }

    items = [
        _item(
            channel=r.channel,
            ref_id=r.ref_id,
            code=r.code,
            detail=r.detail,
            filename=r.filename,
            sha=r.sha,
            failed_at=r.failed_at,
            failure_seq=r.failure_seq,
            repeats=repeats,
            acks=acks,
        )
        for r in rows
    ]
    return Worklist(items=items, groups=groups, total=total, unacknowledged=unack)


async def items_for_refs(
    db: AsyncSession, org_id: str, ref_ids: list[str]
) -> dict[str, WorklistItem]:
    """The worklist state of SPECIFIC references only.

    The bulk path needs to know, per requested id, whether it is still a failure
    and whether an acknowledgement already covers it. It used to get that by
    materialising the entire worklist, which made a 20-record batch cost a full
    table scan. This asks about the ids in hand.
    """
    if not ref_ids:
        return {}
    src = _sources(org_id)
    rows = list(await db.execute(select(src).where(src.c.ref_id.in_(ref_ids))))
    acks = await _latest_acks(db, org_id, [_ack_key(r.channel, r.ref_id) for r in rows])
    return {
        r.ref_id: _item(
            channel=r.channel,
            ref_id=r.ref_id,
            code=r.code,
            detail=r.detail,
            filename=r.filename,
            sha=r.sha,
            failed_at=r.failed_at,
            failure_seq=r.failure_seq,
            repeats={},
            acks=acks,
        )
        for r in rows
    }


async def worklist(
    db: AsyncSession, org_id: str, *, include_acknowledged: bool = False
) -> Worklist:
    """Every capture in this tenant that failed, newest first, from BOTH channels.

    `include_acknowledged=False` (the default) hides items whose acknowledgement
    is still current — but an item that failed AGAIN after being acknowledged is
    NOT current and comes back. The counts in `groups` always describe what is
    returned, so the header cannot disagree with the list under it.
    """
    runs = list(
        await db.scalars(
            select(ExtractionRun).where(
                ExtractionRun.org_id == org_id, ExtractionRun.status == "failed"
            )
        )
    )
    inbound = list(
        await db.scalars(
            select(InboundInvoice).where(
                InboundInvoice.org_id == org_id,
                InboundInvoice.status.in_(("failed", "rejected")),
            )
        )
    )

    # Repeats are keyed on the CONTENT hash, not the filename: the same invoice
    # re-sent under a different name is the same document coming back.
    repeats: dict[str, int] = {}
    for sha in [r.source_sha256 for r in runs] + [i.sha256 for i in inbound]:
        if sha:
            repeats[sha] = repeats.get(sha, 0) + 1

    acks = await _latest_acks(
        db,
        org_id,
        [_ack_key(CHANNEL_UPLOAD, r.id) for r in runs]
        + [_ack_key(CHANNEL_EMAIL, i.id) for i in inbound],
    )

    raw: list[WorklistItem] = []
    for r in runs:
        code = r.failure_code or (UNKNOWN_FAILURE if r.status == "failed" else UNKNOWN_FAILURE)
        raw.append(
            _item(
                channel=CHANNEL_UPLOAD,
                ref_id=r.id,
                code=code,
                detail=r.note,
                filename=r.source_filename,
                sha=r.source_sha256,
                failed_at=r.updated_at,
                failure_seq=r.failure_seq,
                repeats=repeats,
                acks=acks,
            )
        )
    for i in inbound:
        code = i.failure_code or (SECURITY_REJECTED if i.status == "rejected" else UNKNOWN_FAILURE)
        raw.append(
            _item(
                channel=CHANNEL_EMAIL,
                ref_id=i.id,
                code=code,
                detail=i.error,
                filename=i.filename,
                sha=i.sha256,
                failed_at=i.updated_at,
                failure_seq=i.failure_seq,
                repeats=repeats,
                acks=acks,
            )
        )

    raw.sort(key=lambda it: it.failed_at, reverse=True)
    items = raw if include_acknowledged else [it for it in raw if it.acknowledged_at is None]

    groups: dict[str, FailureGroup] = {}
    for it in items:
        g = groups.get(it.code)
        if g is None:
            k = KINDS[it.code]
            g = FailureGroup(
                code=k.code, summary=k.summary, remediation=k.remediation, count=0, unacknowledged=0
            )
            groups[it.code] = g
        g.count += 1
        if it.acknowledged_at is None:
            g.unacknowledged += 1

    return Worklist(
        items=items,
        groups=sorted(groups.values(), key=lambda g: (-g.count, g.code)),
        total=len(items),
        unacknowledged=sum(1 for it in items if it.acknowledged_at is None),
    )


def _item(
    *,
    channel: str,
    ref_id: str,
    code: str,
    detail: str | None,
    filename: str | None,
    sha: str | None,
    failed_at: datetime,
    failure_seq: int,
    repeats: dict[str, int],
    acks: dict[tuple[str, str], CaptureAcknowledgement],
) -> WorklistItem:
    kind = kind_for(code)
    ack = acks.get(_ack_key(channel, ref_id))
    # An ack made BEFORE the most recent failure does not cover it. Compared
    # naively (`ack is not None`) a re-failure would inherit the old dismissal
    # and vanish, which is the exact silence this worklist exists to break.
    current = ack if (ack is not None and not _superseded(ack, failure_seq)) else None
    return WorklistItem(
        channel=channel,
        ref_id=ref_id,
        code=kind.code,
        summary=kind.summary,
        remediation=kind.remediation,
        retry_helps=kind.retry_helps,
        user_fixable=kind.user_fixable,
        detail=detail,
        source_filename=filename,
        sha256=sha,
        # Do not promise a retained original when the fetched bytes differ.
        document_retained=bool(sha) and kind.code not in {STORED_FILE_MISSING, STORED_FILE_CHANGED},
        failed_at=failed_at,
        repeat_count=repeats.get(sha or "", 1),
        acknowledged_at=current.created_at if current else None,
        acknowledged_by=current.acknowledged_by if current else None,
        acknowledgement_note=current.note if current else None,
    )


def _superseded(ack: CaptureAcknowledgement, failure_seq: int) -> bool:
    """True when the record has failed AGAIN since this acknowledgement was made.

    Compares failure SEQUENCES, not timestamps (F-06). The previous rule was
    `failed_at > ack.failure_seen_at`, which meant a re-failure recorded in the
    same clock tick as the acknowledgement compared equal and was treated as
    already covered — so a genuine new failure stayed hidden. Integers cannot
    collide that way.
    """
    return int(failure_seq or 0) > int(ack.failure_seq or 0)


def _as_utc(dt: datetime) -> datetime:
    from datetime import UTC

    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


async def acknowledge(
    db: AsyncSession,
    org_id: str,
    *,
    channel: str,
    ref_id: str,
    actor: str | None,
    note: str | None,
) -> CaptureAcknowledgement | None:
    """Record that a human has seen this failure and decided what to do about it.

    Returns None when the reference is not a failed capture in this tenant — the
    caller renders that as an opaque 404 (§4.4), so acknowledging cannot be used
    to probe which ids exist in another org.

    Does NOT commit: the caller commits it in the same transaction as its audit
    event (§4.16).
    """
    if channel not in CHANNELS:
        return None
    state = await _failure_state(db, org_id, channel, ref_id)
    if state is None:
        return None
    failed_at, failure_seq = state
    row = CaptureAcknowledgement(
        org_id=org_id,
        channel=channel,
        ref_id=ref_id,
        acknowledged_by=(actor[:320] if actor else None),
        note=(note[:1000] if note else None),
        # WHICH failure this covers (F-06). The timestamp rides along as
        # information; the sequence is what the coverage decision reads.
        failure_seq=failure_seq,
        failure_seen_at=failed_at,
    )
    db.add(row)
    return row


async def bulk_acknowledge(
    db: AsyncSession,
    org_id: str,
    *,
    items: list[tuple[str, str]],
    selection: bulk.Selection,
    agreed_count: int | None,
    actor: str | None,
    note: str | None,
) -> bulk.BulkResult:
    """Acknowledge many failed captures at once, under the L-4 guards.

    `items` are (channel, ref_id) pairs — a worklist spans both channels, and the
    operator selects from one list, so a batch may mix them.

    Acknowledging is REVERSIBLE (the table is append-only; the record stands and a
    later failure resurfaces the item regardless), so a filter-based selection is
    permitted here. `require_explicit_selection` is deliberately NOT called — it
    is reserved for operations that cannot be undone, and calling it everywhere
    would make it meaningless where it matters.

    Does NOT commit: the caller commits the whole batch with its audit event, so a
    bulk acknowledge is one transaction and cannot half-apply.
    """
    ids = bulk.normalise_ids([ref for _, ref in items])
    bulk.check_agreed_count(ids, agreed_count)
    channel_of = {ref: ch for ch, ref in items}

    # The state of THESE ids only. Materialising the whole worklist to answer a
    # question about twenty records made a small batch cost a full scan, and the
    # tenants with the most failures are exactly the ones who reach for bulk.
    current = await items_for_refs(db, org_id, ids)

    result = bulk.BulkResult()
    for ref_id in ids:
        item = current.get(ref_id)
        if item is None:
            # Not a failed capture in this workspace. Reported as a SKIP with a
            # reason rather than an error: for the operator this is information,
            # not a fault, and a cross-tenant id is indistinguishable from a
            # nonexistent one exactly as it is on the single-record route.
            result.outcomes.append(
                bulk.Outcome(ref_id, bulk.SKIPPED, "No longer a failed capture in this workspace.")
            )
            continue
        if item.acknowledged_at is not None:
            result.outcomes.append(
                bulk.Outcome(ref_id, bulk.SKIPPED, "Already acknowledged since this failure.")
            )
            continue
        ack = await acknowledge(
            db,
            org_id,
            channel=channel_of.get(ref_id, item.channel),
            ref_id=ref_id,
            actor=actor,
            note=note,
        )
        if ack is None:
            result.outcomes.append(
                bulk.Outcome(ref_id, bulk.SKIPPED, "No longer a failed capture in this workspace.")
            )
        else:
            result.outcomes.append(bulk.Outcome(ref_id, bulk.APPLIED))
    return result


async def _failure_state(
    db: AsyncSession, org_id: str, channel: str, ref_id: str
) -> tuple[datetime, int] | None:
    """(when it failed, which failure it is) for a record that IS currently
    failed, or None when it is not one in this tenant."""
    if channel == CHANNEL_UPLOAD:
        run = await db.scalar(
            select(ExtractionRun).where(
                ExtractionRun.org_id == org_id,
                ExtractionRun.id == ref_id,
                ExtractionRun.status == "failed",
            )
        )
        return (run.updated_at, run.failure_seq or 0) if run else None
    row = await db.scalar(
        select(InboundInvoice).where(
            InboundInvoice.org_id == org_id,
            InboundInvoice.id == ref_id,
            InboundInvoice.status.in_(("failed", "rejected")),
        )
    )
    return (row.updated_at, row.failure_seq or 0) if row else None
