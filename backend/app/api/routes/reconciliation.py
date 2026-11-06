"""Bank reconciliation — import a statement and match its lines to receipts / paid
reimbursement payouts (Phase 12). Gated on the issuing module and the PAYMENT_*
permissions (cash application): router-level PAYMENT_READ, inline PAYMENT_WRITE on
the import + match mutations.
"""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute

from app.api.deps import CurrentUser, DbSession, refresh_current_identity, require_perm
from app.api.upload_read import read_upload
from app.core import authz
from app.core.errors import AppError, ValidationError
from app.schemas.reconciliation import (
    BankLineOut,
    BankStatementOut,
    ImportResult,
    MatchCandidateOut,
)
from app.schemas.reconciliation_commands import (
    CommandIntent,
    CommandReceipt,
    CurrentLine,
    HistoryPage,
)
from app.services import bank_statement, filesec, modules, reconciliation, reconciliation_commands
from app.services.pdf_ocr import OCR_TIMED_OUT_DETAIL

# Structural authorization (ADR-0024): viewing reconciliation needs PAYMENT_READ
# (router-level); import + match declare PAYMENT_WRITE per-route below.
log = logging.getLogger(__name__)


class CommandValidationRoute(APIRoute):
    """Do not echo invalid command text through FastAPI's validation response."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def validated(request: Request):
            try:
                return await handler(request)
            except RequestValidationError:
                if self.name != "command":
                    raise
                # An escaped lone surrogate is valid JSON but not UTF-8 text.
                # Default validation echoes that input and cannot encode its 422.
                raise ValidationError(
                    "Invalid reconciliation intention. Review the required fields.",
                    code="reconciliation_command_invalid",
                ) from None

        return validated


router = APIRouter(
    prefix="/reconciliation",
    tags=["reconciliation"],
    route_class=CommandValidationRoute,
    dependencies=[Depends(require_perm(authz.Permission.PAYMENT_READ))],
)
_WRITE = [Depends(require_perm(authz.Permission.PAYMENT_WRITE))]


async def _guard(db: DbSession, org_id: str) -> None:
    await modules.require_enabled(db, org_id, "issuing")


async def _statement_out(db: DbSession, org_id: str, s) -> BankStatementOut:
    counts = await reconciliation.statement_counts(db, org_id, s.id)
    return BankStatementOut(
        id=s.id,
        filename=s.filename,
        source_format=s.source_format,
        method=s.method,
        line_count=s.line_count,
        created_by=s.created_by,
        created_at=s.created_at,
        **counts,
    )


async def _load_line(db: DbSession, org_id: str, line_id: str):
    line = await reconciliation.get_line(db, org_id, line_id)
    if line is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bank line not found")
    return line


@router.post(
    "/import", response_model=ImportResult, status_code=status.HTTP_201_CREATED, dependencies=_WRITE
)
async def import_statement(current: CurrentUser, db: DbSession, file: UploadFile):
    await _guard(db, current.org_id)
    content = await read_upload(file)
    # Security gate before any parsing/OCR of the (untrusted) statement.
    try:
        await filesec.check_async(
            file.filename or "statement",
            content,
            allowed=frozenset({"pdf", "csv", "xml", "mt940"}),
        )
    except filesec.FileRejected as exc:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, str(exc))
    try:
        result = await run_in_threadpool(
            bank_statement.parse, file.filename or "statement", content
        )
    except bank_statement.pdf_ocr.OcrUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"OCR unavailable: {exc}")
    except bank_statement.pdf_ocr.OcrTimedOut as exc:
        # Operator-facing sentence; the budget and page live in the log (STIR-P2-01).
        log.warning("statement OCR timed out: %s", exc)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, OCR_TIMED_OUT_DETAIL)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))
    try:
        stmt = await reconciliation.import_statement(
            db,
            current.org_id,
            filename=file.filename or "statement",
            sha=reconciliation.digest(content),
            result=result,
            created_by=current.email,
        )
    except reconciliation.ReconError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    return ImportResult(
        statement_id=stmt.id,
        method=stmt.method,
        imported=stmt.line_count,
        warnings=result.warnings,
    )


@router.get("/statements", response_model=list[BankStatementOut])
async def list_statements(current: CurrentUser, db: DbSession):
    await _guard(db, current.org_id)
    rows = await reconciliation.list_statements(db, current.org_id)
    return [await _statement_out(db, current.org_id, s) for s in rows]


@router.get("/statements/{statement_id}/lines", response_model=list[BankLineOut])
async def statement_lines(statement_id: str, current: CurrentUser, db: DbSession):
    await _guard(db, current.org_id)
    if await reconciliation.get_statement(db, current.org_id, statement_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Statement not found")
    rows = await reconciliation.lines_for(db, current.org_id, statement_id)
    return [BankLineOut.model_validate(ln) for ln in rows]


@router.get("/lines/{line_id}/candidates", response_model=list[MatchCandidateOut])
async def match_candidates(line_id: str, current: CurrentUser, db: DbSession):
    await _guard(db, current.org_id)
    line = await _load_line(db, current.org_id, line_id)
    cands = await reconciliation.suggest_matches(db, current.org_id, line)
    return [
        MatchCandidateOut(
            kind=c.kind,
            id=c.id,
            amount=c.amount,
            currency=c.currency,
            date=c.date,
            reference=c.reference,
            days_off=c.days_off,
            score=c.score,
        )
        for c in cands
    ]


async def _legacy(line_id: str, current: CurrentUser, db: DbSession):
    await _guard(db, current.org_id)
    await _load_line(db, current.org_id, line_id)
    raise AppError(
        "Review the current bank line and submit a versioned command.",
        status=428,
        code="reconciliation_command_preconditions_required",
    )


@router.post(
    "/lines/{line_id}/match",
    dependencies=_WRITE,
    deprecated=True,
    responses={428: {"description": "Use a versioned command with an Idempotency-Key."}},
)
async def match_line(line_id: str, current: CurrentUser, db: DbSession):
    return await _legacy(line_id, current, db)


@router.delete(
    "/lines/{line_id}/match",
    dependencies=_WRITE,
    deprecated=True,
    responses={428: {"description": "Use a versioned command with an Idempotency-Key."}},
)
async def unmatch_line(line_id: str, current: CurrentUser, db: DbSession):
    return await _legacy(line_id, current, db)


@router.post(
    "/lines/{line_id}/ignore",
    dependencies=_WRITE,
    deprecated=True,
    responses={428: {"description": "Use a versioned command with an Idempotency-Key."}},
)
async def ignore_line(line_id: str, current: CurrentUser, db: DbSession):
    return await _legacy(line_id, current, db)


def _refresh(request: Request, db: DbSession, current: CurrentUser):
    async def fresh():
        identity, _org = await refresh_current_identity(request, db, current)
        return identity

    return fresh


@router.post("/lines/{line_id}/commands", response_model=CommandReceipt, dependencies=_WRITE)
async def command(
    line_id: UUID,
    body: CommandIntent,
    request: Request,
    current: CurrentUser,
    db: DbSession,
    key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    return await reconciliation_commands.execute(
        db,
        current,
        str(line_id),
        reconciliation_commands.command_key(key),
        body,
        _refresh(request, db, current),
    )


@router.get("/lines/{line_id}/commands/{key}", response_model=CommandReceipt)
async def own_command(
    line_id: UUID, key: str, request: Request, current: CurrentUser, db: DbSession
):
    return await reconciliation_commands.own_outcome(
        db,
        current,
        str(line_id),
        reconciliation_commands.command_key(key),
        _refresh(request, db, current),
    )


@router.get("/lines/{line_id}/history", response_model=HistoryPage)
async def line_history(
    line_id: UUID,
    request: Request,
    current: CurrentUser,
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    before_version: Annotated[int | None, Query(ge=1, le=2147483647)] = None,
):
    return await reconciliation_commands.history(
        db,
        current,
        str(line_id),
        _refresh(request, db, current),
        limit=limit,
        before_version=before_version,
    )


@router.get("/lines/{line_id}", response_model=CurrentLine)
async def get_current_line(line_id: UUID, request: Request, current: CurrentUser, db: DbSession):
    return await reconciliation_commands.current_line(
        db, current, str(line_id), _refresh(request, db, current)
    )
