"""Transport routes slice 4 — statement INTAKE, the front door (WO-S).

WHAT WAS ACTUALLY WRONG
------------------------
`app/services/transport/statement_ingest.py` has existed since WO-62 and is
the only way a fuel-card statement can become `fuel_transactions` rows. Until
this module, **no route imported it**: seven shipped network parsers (Eurowag,
E100, Q8, DKV, TFC, Moeve, BP — WO-62…65, 67, 68, 69), the nine-rule capture
review gate (WO-66/R25), the deterministic post-capture checks (WO-71/R26) and
the anti-drift extraction baseline (WO-70) were all reachable only from a
Python prompt. A product whose data can only be loaded by its own authors has
no front door, and everything downstream of it — claims, the close, the
recovery board, the reliability board — was standing on a step nobody could
climb.

WHAT THIS MODULE IS, AND IS NOT
---------------------------------
Thin controller (engineering-rules §3): resolve → security-gate the bytes →
call the already-gated service → shape the reply. It adds no parsing, no money
arithmetic, no idempotency logic. Every refusal a client sees is raised BY THE
SERVICE as an `app.core.errors.AppError` and rendered by the one `app.main`
handler, so this module maps nothing and the wire vocabulary cannot drift
(master-context §4.20).

THE NETWORK IS NEVER A PARAMETER
----------------------------------
`fuel_card_parser.select` detects the network from the file's own marker line
and raises when none matches — "fail-closed network detection", its words. A
route that let the uploader assert `network=eurowag` would hand an operator a
way to have E100 bytes parsed as Eurowag, which is precisely the
miscategorisation that design prevents. So there is no network field here, and
`GET /networks` exists to tell the screen what is supported rather than to give
it something to send back.

AUTHORIZATION
---------------
`GET /networks` is a read of the registry: router-level `VAT_READ`. The upload
is a WRITE — it registers fuel transactions and teaches the supplier-entity
registry — so it overrides to `VAT_WRITE`, matching every other transport
mutation (ADR-0024, structural; no permission member added, §10).

THE FILE IS UNTRUSTED, AND IS TREATED THAT WAY
------------------------------------------------
`filesec.check` runs on the bytes BEFORE the parser ever sees them, allowing
`csv` only — every shipped parser reads UTF-8 CSV with a marker first line, so
a wider allow-list would only widen the attack surface without enabling a
single real statement. Size is checked first (413), then kind + content + the
malware scan (415). This is the same intake choke-point discipline as the bank
statement and invoice upload paths; a renamed script does not become a
statement by acquiring a `.csv` extension.

THE BYTES ARE KEPT (WO-AF)
---------------------------
Until WO-AF the upload digested the file, keyed every finding, baseline and
audit event on that digest — and then dropped the bytes. A review finding
pointed at a document that no longer existed, so "which line failed" could
not be checked against the file it came from. The statement is now vaulted
through `documents.store` (prefix `statements`, content-addressed, tenant-
prefixed, registered in the document catalog) BEFORE ingestion, so a refused
statement is kept too — the refusal is exactly when the operator needs the
file. `GET /{sha}/file` serves it back, inert, to anyone with `VAT_READ`, and
is audited like every other original's download.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import Response

from app.api.deps import CurrentUser, DbSession, refresh_current_identity, require_perm
from app.api.upload_read import read_upload
from app.core import authz
from app.core.errors import NotFoundError
from app.core.security_headers import content_disposition
from app.core.storage import StorageError, StorageReadError
from app.schemas.transport_statement import (
    SAMPLE_LINES,
    FindingCloseIn,
    FuelCardNetworkListOut,
    FuelCardNetworkOut,
    StatementEntityOut,
    StatementFindingListOut,
    StatementFindingOut,
    StatementIngestOut,
    StatementLineSample,
)
from app.services import audit, document_registry, documents, filesec, issuer
from app.services.transport import (
    extraction_baseline,
    fuel_card_parser,
    statement_ingest,
    statement_review,
)

router = APIRouter(
    prefix="/transport/statements",
    tags=["transport"],
    dependencies=[Depends(require_perm(authz.Permission.VAT_READ))],
)

_WRITE = [Depends(require_perm(authz.Permission.VAT_WRITE))]

#: Only CSV. Every shipped parser's `handles()` decodes UTF-8 and matches a
#: literal marker on line 1, so no other kind can produce a statement — and an
#: allow-list wider than the parsers is an attack surface with no user.
STATEMENT_KINDS = frozenset({"csv"})


@router.get("/networks", response_model=FuelCardNetworkListOut)
async def list_supported_networks(current: CurrentUser, db: DbSession):
    """The fuel-card networks this deployment can parse, from the LIVE registry.

    Read from `fuel_card_parser.parsers()` rather than a hand-kept list, so the
    screen cannot advertise a network whose parser was never registered, and a
    newly registered one appears without a second edit.
    """
    return FuelCardNetworkListOut(
        networks=[FuelCardNetworkOut(network=p.network) for p in fuel_card_parser.parsers()]
    )


@router.post("", response_model=StatementIngestOut, dependencies=_WRITE)
async def upload_statement(
    current: CurrentUser,
    db: DbSession,
    file: UploadFile,
    entity_id: str = Form(description="The claimant's own legal entity (issuer profile id)"),
    period: str = Form(description="YYYY-MM — the accounting month this statement covers"),
    coversheet_total: str | None = Form(
        default=None,
        description="The statement coversheet's own net total, if the file carries one. "
        "Supplied, it arms the batch tie-out (R25) and a mismatch REFUSES the upload.",
    ),
):
    """Register one fuel-card statement: parse, review, convert, write, learn.

    The entity is resolved FIRST, before the file is read. That ordering is
    deliberate: a caller who names an entity this workspace does not own should
    be refused without this process parsing bytes on their behalf. It is also
    the only guard that holds for a statement with zero data lines, where no
    per-line ingest would ever reach `fuel_ingest`'s own check.

    `coversheet_total` arrives as a STRING and is parsed with `Decimal`, never
    `float` — a tie-out that compared a double against the sum of exact line
    amounts would fail on files that balance perfectly (§4.9).
    """
    entity = await issuer.get_by_id(db, current.org_id, entity_id)
    if entity is None:
        raise NotFoundError("Entity not found", code="entity_not_found")

    total: Decimal | None = None
    if coversheet_total is not None and coversheet_total.strip() != "":
        try:
            total = Decimal(coversheet_total.strip())
        except (InvalidOperation, ValueError):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"'{coversheet_total}' is not a valid coversheet total",
            )

    content = await read_upload(file)
    # The security gate, before any parsing of the untrusted statement.
    try:
        await filesec.check_async(
            file.filename or "statement.csv", content, allowed=STATEMENT_KINDS
        )
    except filesec.FileRejected as exc:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, str(exc))

    sha = extraction_baseline.digest(content)
    name = file.filename or "statement.csv"
    # WO-AF — vault the original FIRST, outside the ingest transaction: the
    # object store is content-addressed and idempotent, so a re-upload is a
    # no-op, and a refusal below (which rolls the session back) still leaves
    # the bytes the finding will point at. The catalog row is written in each
    # branch, inside whichever transaction that branch commits.
    await documents.store(documents.STATEMENTS, current.org_id, content, "text/csv")
    # Read the caller's identity BEFORE the attempt. A rollback expires every
    # instance in the session — `expire_on_commit=False` does not cover it —
    # so `current.org_id` in the except branch below would try to reload the
    # User row lazily and raise `MissingGreenlet` instead of recording the
    # refusal. The identity was established before any of this ran; re-reading
    # it afterwards was never necessary.
    org_id = current.org_id
    actor_email = current.email  # same reason: read before any rollback can expire it
    try:
        result = await statement_ingest.ingest_statement(
            db,
            current.org_id,
            entity_id=entity_id,
            period=period,
            filename=name,
            content=content,
            coversheet_total=total,
        )
    except fuel_card_parser.StatementReadingBusy as busy:
        # Vault-before-parse is deliberate. Admission failed before import
        # work; the object exists, but no catalog/refusal/audit/DML follows.
        raise fuel_card_parser.StatementReadingBusy(original_stored=True) from busy
    except statement_ingest.StatementRefused as refused:
        # WO-Z — a refusal used to leave nothing behind: the findings went into
        # a message string and the transaction was discarded. The rollback here
        # is what makes recording them safe rather than a way to smuggle a
        # partial ingest past the two-phase guarantee — it discards whatever the
        # attempt had staged, and only then is the queue written and committed.
        await db.rollback()
        await _catalog_statement(
            db, org_id, sha=sha, size=len(content), filename=name, by=actor_email
        )
        await statement_review.record_refusal(
            db,
            org_id,
            statement_sha256=sha,
            filename=name,
            network=None,  # a refused batch never reached a registered network
            period=period,
            entity_id=entity_id,
            findings=refused.findings,
            tie=refused.tie,
        )
        await audit.record(
            db,
            audit.A.TRANSPORT_STATEMENT_REFUSED,
            target_type="fuel_statement",
            target_id=sha,
            meta={
                "filename": name,
                "period": period,
                "entity_id": entity_id,
                "errors": len(refused.extra()["findings"]),
                "tie_out_failed": bool(refused.tie is not None and not refused.tie.ok),
            },
        )
        await db.commit()
        raise
    # One STATEMENT-level event. `fuel_ingest` already audits each inserted row
    # and `supplier_entity` each learned registration, but neither knows the
    # filename or the digest — so without this, the trail could say which rows
    # appeared and never which file an operator uploaded to produce them.
    await _catalog_statement(db, org_id, sha=sha, size=len(content), filename=name, by=actor_email)
    await statement_review.record_registered(
        db,
        current.org_id,
        statement_sha256=sha,
        filename=name,
        network=result.network,
        period=result.period,
        entity_id=entity_id,
        review_findings=result.review_findings,
        warnings=result.queue_warnings,
    )
    await audit.record(
        db,
        audit.A.TRANSPORT_STATEMENT_INGEST,
        target_type="fuel_statement",
        target_id=sha,
        meta={
            "filename": file.filename,
            "network": result.network,
            "period": result.period,
            "entity_id": entity_id,
            "lines": len(result.lines),
            "entities_learned": len(result.entities),
            "warnings": len(result.warnings),
            "coversheet_total": str(total) if total is not None else None,
        },
    )
    await db.commit()

    return StatementIngestOut(
        network=result.network,
        period=result.period,
        filename=file.filename or "statement.csv",
        statement_sha256=sha,
        lines_registered=len(result.lines),
        entities_learned=[
            StatementEntityOut(
                country=e.country, vat_number=e.vat_number, entity_name=e.entity_name
            )
            for e in result.entities
        ],
        warnings=result.warnings,
        sample=[
            StatementLineSample(
                line_seq=t.line_seq,
                txn_date=t.txn_date,
                country=t.country,
                station=t.station,
                product=t.product,
                qty=t.qty,
                currency=t.currency,
                net_local=t.net_local,
                vat_local=t.vat_local,
                net_eur=t.net_eur,
                vat_eur=t.vat_eur,
                fx_source=t.fx_source,
            )
            for t in result.lines[:SAMPLE_LINES]
        ],
    )


async def _catalog_statement(
    db, org_id: str, *, sha: str, size: int, filename: str, by: str | None
) -> None:
    """The document-catalog row for a vaulted statement (WO-AF). Separate from
    the byte write because the refusal branch rolls the session back after the
    bytes are stored and must re-record the row in the transaction it commits."""
    await document_registry.register(
        db,
        org_id,
        sha256=sha,
        size=size,
        kind=documents.STATEMENTS,
        mime="text/csv",
        filename=filename,
        uploaded_by=by,
    )


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@router.get("/{statement_sha256}/file")
async def download_statement(
    statement_sha256: str, current: CurrentUser, db: DbSession, request: Request
):
    """The original statement bytes a finding points at (WO-AF).

    Read-permission only, like the queue: looking at the file behind a finding
    is not a change to anything. The lookup goes through the tenant's document
    catalog, so a digest this workspace never vaulted — another tenant's, or a
    statement from before vaulting existed — is an opaque 404 and never a read
    of the object store on the caller's behalf. Served inert (forced download,
    no MIME sniffing) and audited as a document download, exactly like every
    other stored original."""
    if not _SHA256.match(statement_sha256):
        raise NotFoundError("Statement not found", code="statement_not_found")
    row = await document_registry.find(
        db, current.org_id, sha256=statement_sha256, kind=documents.STATEMENTS
    )
    if row is None:
        raise NotFoundError("Statement not found", code="statement_not_found")
    identity = (row.id, row.size, row.filename)
    try:
        content = await documents.load_verified_original(
            documents.STATEMENTS, current.org_id, statement_sha256, size=row.size
        )
    except StorageReadError:
        raise
    except StorageError:
        # Catalogued but the object is gone (a purged volume, a restore from an
        # older backup): a 404 that says so, never an empty 200 (Phase 12, R2-S1).
        raise NotFoundError("Statement not found", code="statement_not_found")
    if content is None:
        raise NotFoundError("Statement not found", code="statement_not_found")
    fresh, _ = await refresh_current_identity(request, db, current)
    authz.require(fresh, authz.Permission.VAT_READ)
    row = await document_registry.find(
        db, current.org_id, sha256=statement_sha256, kind=documents.STATEMENTS
    )
    if row is None or (row.id, row.size, row.filename) != identity:
        raise NotFoundError(
            "Statement changed while preparing the download", code="statement_not_found"
        )
    await audit.record(
        db,
        audit.A.DOC_DOWNLOAD,
        target_type="fuel_statement",
        target_id=statement_sha256,
        meta={"filename": row.filename, "kind": documents.STATEMENTS},
    )
    await db.commit()
    # RFC 5987 helper (Phase 12, R2-S1): a hand-rolled header stripped only `"`
    # — a CR/LF in the stored name could split the response and a non-latin-1
    # name (Polish, Lithuanian statement files are ordinary here) made the
    # header encode raise a 500. Every other download route uses this helper.
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": content_disposition(row.filename, fallback="statement.csv"),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


# --------------------------------------------------------------------------- #
# WO-Z — the review queue
# --------------------------------------------------------------------------- #


@router.get("/findings", response_model=StatementFindingListOut)
async def list_findings(current: CurrentUser, db: DbSession, status_filter: str = "open"):
    """The statement review worklist.

    Read-permission only: seeing what a file was flagged for is not a change to
    anything, and gating the QUEUE behind write rights would mean the people
    who most need to look — whoever reconciles the month — could not.

    `status_filter` defaults to `open`, because a queue is what is left to do.
    The closed statuses are reachable for the same reason the resolution note
    is stored: what somebody decided, and why, is worth reading back."""
    rows = await statement_review.worklist(db, current.org_id, status=status_filter)
    # WO-AF: offer the file only where one is on file — findings recorded
    # before vaulting existed have no bytes behind them.
    on_file = await document_registry.vaulted(
        db,
        current.org_id,
        kind=documents.STATEMENTS,
        shas={r.statement_sha256 for r in rows},
    )
    findings = []
    for r in rows:
        out = StatementFindingOut.model_validate(r, from_attributes=True)
        out.file_available = r.statement_sha256 in on_file
        findings.append(out)
    return StatementFindingListOut(
        findings=findings,
        open_count=await statement_review.count(db, current.org_id, status="open"),
        refused_count=await statement_review.count(
            db, current.org_id, status="open", outcome="refused"
        ),
    )


@router.post(
    "/findings/{finding_id}/close", response_model=StatementFindingOut, dependencies=_WRITE
)
async def close_finding(finding_id: str, body: FindingCloseIn, current: CurrentUser, db: DbSession):
    """Take one finding out of the queue, as `resolved` or `dismissed`.

    Write-gated, unlike the list: this is a person putting their name to a
    judgement about a document, and the audit event records WHICH of the two
    they claimed — the part a later reader cannot reconstruct from the row's
    absence from the queue."""
    row = await statement_review.close_finding(
        db,
        current.org_id,
        finding_id,
        status=body.status,
        actor=current.email,
        note=body.note,
    )
    await audit.record(
        db,
        audit.A.TRANSPORT_STATEMENT_FINDING_CLOSED,
        target_type="statement_finding",
        target_id=row.id,
        meta={
            "resolution": row.status,
            "statement_sha256": row.statement_sha256,
            "filename": row.filename,
            "code": row.code,
            "line_seq": row.line_seq,
            "outcome": row.outcome,
            "note": row.resolution_note,
        },
    )
    await db.commit()
    return StatementFindingOut.model_validate(row, from_attributes=True)
