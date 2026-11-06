from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.api.attachment_body_limit import (
    AttachmentBodyLimitMiddleware,
    upload_http_exception_handler,
)
from app.api.router import api_router
from app.core.config import log_production_warnings, settings
from app.core.database import engine, get_session
from app.core.development_schema import development_metadata
from app.core.errors import AppError
from app.core.gc_tuning import freeze_startup_heap
from app.core.observability import (
    RequestContextMiddleware,
    configure_logging,
    setup_metrics,
)
from app.core.ratelimit import RateLimitMiddleware
from app.core.security_headers import SecurityHeadersMiddleware
from app.core.storage import StorageReadError
from app.core.tenant import TenantScopeMiddleware
from app.models import Base
from app.services.scim import ERROR_SCHEMA as SCIM_ERROR_SCHEMA
from app.services.scim import ScimError

configure_logging(settings.structured_logs)
log = logging.getLogger("invoiceiq")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Dev/test: create tables directly for zero-setup. Production owns schema
    # evolution through Alembic (`alembic upgrade head`, run before boot), so we
    # do NOT create_all there — it would race the migrations' version tracking.
    if settings.environment != "production":
        async with engine.begin() as conn:
            await conn.run_sync(
                lambda connection: development_metadata(
                    Base.metadata, connection.dialect
                ).create_all(connection)
            )
    else:
        log.info("Production: schema is managed by Alembic (run `alembic upgrade head`)")
    log_production_warnings(log)

    # Seed the bundled ECB rate snapshot if the cache is empty, so FX conversion
    # works before the first live refresh. Best-effort — never blocks startup.
    from datetime import date

    from app.core.database import SessionLocal
    from app.services import fx

    try:
        async with SessionLocal() as db:
            seeded = await fx.ensure_seed_rates(db, date.today())
            if seeded:
                log.info("Seeded %d bundled ECB fallback rates", seeded)
            # Guarantee full European-currency coverage (incl. the non-ECB ones).
            covered = await fx.ensure_european_coverage(db, date.today())
            if covered:
                log.info("Seeded %d indicative rows for European currencies", covered)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("ECB rate seeding skipped: %s", exc)

    # Last, once every import and every startup write is done: park the static
    # heap outside the cyclic collector (PERF-016 — see app/core/gc_tuning.py
    # for the measurement; a full collection over the imported app costs
    # ~140 ms and fires on every read that hydrates a few thousand rows).
    freeze_startup_heap()

    log.info("%s %s ready (%s)", settings.app_name, __version__, settings.environment)
    yield
    await engine.dispose()


app = FastAPI(
    title=settings.app_name,
    version=__version__,
    summary="Invoice Data Analytics Platform",
    lifespan=lifespan,
    # Don't expose the interactive docs / OpenAPI schema in production — no reason
    # to hand an attacker the full API contract. Available in dev/test as usual.
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None if settings.is_production else "/redoc",
    openapi_url=None if settings.is_production else "/openapi.json",
)

# Keep CORS outside the body guard so early size errors remain readable by the UI.
app.add_middleware(AttachmentBodyLimitMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Capture-Review-Fingerprint"],
)
# Bound the tenant-scope ContextVar to each request.
app.add_middleware(TenantScopeMiddleware)
# Security response headers (HSTS on HTTPS, nosniff, frame-deny, referrer policy).
app.add_middleware(SecurityHeadersMiddleware)
# Coarse abuse / brute-force guard (before tenant + route work; after the
# request-context wrapper so a 429 still gets a request-id + access-log line).
app.add_middleware(RateLimitMiddleware)
# First to see the request / last to see the response: request-id + access log.
app.add_middleware(RequestContextMiddleware)


async def _refresh_queue_gauges() -> None:
    """OPS-014 — recompute the queue gauges for THIS scrape.

    `set_queue_metrics` writes the default Prometheus registry of whichever
    PROCESS called it, and there is no multiprocess mode here. So each uvicorn
    worker carried its own copy, showing whatever that worker last computed —
    i.e. whenever it last happened to serve `/health/queue`. A scrape landed on
    one worker at random and read a number that could be hours old, while the
    worker process (which refreshes every tick) serves no HTTP and is never
    scraped at all. A kind whose dead jobs had been cleared could keep reporting
    its old count on one API process indefinitely.

    Computing inside the scrape makes the answer belong to the moment it is
    asked, on whichever process answers. It costs three aggregate queries per
    scrape; `/metrics` is not routed by the edge on either lane (nginx publishes
    `/`, `/api/`, `/health*` and `/assets/`; the k8s ingress publishes `/api`
    and `/`), so this is not an unauthenticated path anyone outside the network
    can use to make the database work."""
    from app.core.database import SessionLocal
    from app.services import queue_health

    async with SessionLocal() as db:
        await queue_health.snapshot(db)


# Prometheus /metrics + per-request instrumentation (only if the lib is present).
if settings.metrics_enabled and setup_metrics(app, before_scrape=_refresh_queue_gauges):
    log.info("Prometheus metrics enabled at /metrics")


@app.exception_handler(StarletteHTTPException)
async def _http_error_handler(request, exc: StarletteHTTPException):
    return await upload_http_exception_handler(request, exc)


@app.exception_handler(ScimError)
async def _scim_error_handler(request, exc: ScimError):
    """Render SCIM service errors in the SCIM error schema."""
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=exc.status,
        media_type="application/scim+json",
        content={"schemas": [SCIM_ERROR_SCHEMA], "detail": exc.detail, "status": str(exc.status)},
    )


@app.exception_handler(AppError)
async def _app_error_handler(request, exc: AppError):
    """Map a framework-agnostic service error (app.core.errors) to the API's error
    shape — {"detail", "code"} plus whatever `exc.extra()` adds. The X-Request-ID
    header is added by the middleware.

    `detail` and `code` are written LAST so an error's extra payload can never
    shadow the two fields every client already relies on."""
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=exc.status,
        content={**exc.extra(), "detail": exc.message, "code": exc.code},
        headers=exc.headers,
    )


@app.exception_handler(StorageReadError)
async def _storage_read_error_handler(request, exc: StorageReadError):
    """An interrupted document read is retryable; never echo backend diagnostics."""
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=503,
        content={
            "detail": "Stored document could not be read. Check storage and retry.",
            "code": "storage_read_unavailable",
        },
        headers={"Cache-Control": "private, no-store"},
    )


@app.exception_handler(Exception)
async def _unhandled_error_handler(request, exc: Exception):
    """Last-resort 500: never leak an internal message or a bare text body. The
    exception is already logged with the request id by RequestContextMiddleware;
    the client gets a stable JSON envelope + the X-Request-ID header to quote."""
    from fastapi.responses import JSONResponse

    scope = getattr(request, "scope", {})
    state = scope.get("state")
    rid = state.get("request_id") if isinstance(state, dict) else None
    if not isinstance(rid, str) or not rid:
        # Header decode failures and middleware-bypassing fixtures have no
        # resolved ID to recover. Never reuse ambient context for the fallback.
        try:
            headers = {
                k.decode().lower(): v.decode()
                for k, v in scope.get("headers", [])
                if k.lower() == b"x-request-id"
            }
        except UnicodeDecodeError:
            headers = {}
        rid = headers.get("x-request-id") or uuid.uuid4().hex
    response = JSONResponse(
        status_code=500, content={"detail": "Internal server error", "code": "internal_error"}
    )
    response.raw_headers.append((b"x-request-id", rid.encode()))
    return response


@app.get("/health", tags=["meta"])
async def health() -> dict:
    """Liveness: the process is up. Cheap — no I/O. Used by load balancers / k8s
    livenessProbe to decide whether to restart the container."""
    return {"status": "ok", "version": __version__, "app": settings.app_name}


@app.get("/health/ready", tags=["meta"])
async def ready():
    """Readiness: the app can serve traffic (the database is reachable). Used by
    k8s readinessProbe / the LB to decide whether to route traffic here. Returns
    503 when the DB is unreachable so the replica is pulled from rotation."""
    from fastapi import Response

    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"status": "ready"}
    except Exception as exc:  # DB down / starting up → not ready
        log.warning("readiness check failed: %s", exc)
        return Response(
            content='{"status":"not-ready"}', status_code=503, media_type="application/json"
        )


@app.get("/health/queue", tags=["meta"])
async def queue_health(db: Annotated[AsyncSession, Depends(get_session)]):
    """Background-queue SLO probe: aggregate job counts + dead-letter depth +
    oldest-pending age. Returns 503 (degraded) when the SLO is breached so an
    uptime check pages. Runs unscoped (all tenants); aggregate numbers only — no
    tenant data. (Deep DB-down liveness is /health/ready's job.)"""
    import json as _json

    from fastapi import Response

    from app.services import queue_health as qh

    h = await qh.snapshot(db)
    body = {
        "status": "ok" if h.slo_ok else "degraded",
        "dead": h.dead,
        "pending": h.pending,
        "oldest_pending_seconds": h.oldest_pending_seconds,
        "by_status": h.counts,
        "dead_by_kind": h.dead_by_kind,
    }
    if not h.slo_ok:
        return Response(content=_json.dumps(body), status_code=503, media_type="application/json")
    return body


app.include_router(api_router, prefix=settings.api_v1_prefix)
