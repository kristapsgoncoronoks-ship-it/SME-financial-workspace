"""Production observability: structured logging, request tracing, and metrics.

- `configure_logging()` switches the root logger to single-line JSON in
  production (ready for Loki/CloudWatch/Datadog) and human-readable elsewhere.
- `RequestContextMiddleware` stamps every request with an `X-Request-ID`
  (propagated from an upstream proxy if present), logs one structured access
  line per request (method, path, status, duration), and returns timing headers.
  Uncaught errors are logged with the request id so a 500 is always traceable.
- `setup_metrics()` mounts a Prometheus `/metrics` endpoint and per-request
  counters/histograms — but ONLY if `prometheus-client` is installed, so the
  test/dev environment needs no extra dependency.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar

from starlette.types import ASGIApp, Message, Receive, Scope, Send

#: Awaited at the start of every /metrics scrape (OPS-014). See setup_metrics.
BeforeScrape = Callable[[], Awaitable[None]]

request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")
# PAT-030 (Paperless-ngx, reference integration 2026-09-07): a worker log line
# carried no request id (there is no request) and so nothing tied "OCR failed on
# page 3" to the job that ran it. `jobs.run_once` sets these around the handler
# and resets them after; the JSON formatter adds them only when set.
job_id_ctx: ContextVar[str | None] = ContextVar("job_id", default=None)
job_kind_ctx: ContextVar[str | None] = ContextVar("job_kind", default=None)
log = logging.getLogger("invoiceiq.access")


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_ctx.get(),
        }
        job_id = job_id_ctx.get()
        if job_id is not None:
            payload["job_id"] = job_id
        job_kind = job_kind_ctx.get()
        if job_kind is not None:
            payload["job_kind"] = job_kind
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # Merge structured extras attached via `logger.info(..., extra={...})`.
        for k, v in getattr(record, "__dict__", {}).items():
            if k in (
                "args",
                "msg",
                "levelname",
                "name",
                "exc_info",
                "exc_text",
                "stack_info",
                "created",
                "msecs",
                "relativeCreated",
                "levelno",
                "pathname",
                "filename",
                "module",
                "funcName",
                "lineno",
                "thread",
                "threadName",
                "processName",
                "process",
                "taskName",
            ):
                continue
            payload.setdefault(k, v)
        return json.dumps(payload, default=str)


def configure_logging(json_logs: bool, level: int = logging.INFO) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    handler = logging.StreamHandler()
    if json_logs:
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.handlers[:] = [handler]
    # Uvicorn's own access log duplicates ours — quiet it; keep error logs.
    logging.getLogger("uvicorn.access").handlers[:] = []
    logging.getLogger("uvicorn.access").propagate = False


# SEC-005 (audit 2026-09-05): the client-portal and calendar-feed tokens are
# CAPABILITY credentials carried in the URL path (a magic link is the whole
# design). Every access log line therefore used to write a live credential to
# disk. The path is redacted at the log boundary — the token still routes,
# the log still says which surface was hit, and nobody with log access holds
# the key. The same masking is applied by nginx in front (nginx.prod.conf).
_TOKEN_PATHS = (
    re.compile(r"(/portal/)[^/?#]+"),
    re.compile(r"(/calendar/feed/)[^/?#]+(\.ics)"),
)
_OAUTH_RETURN = re.compile(
    r"^(.*/(?:schedule/calendar-connections/(?:google|outlook)/callback|auth/sso/callback))/?(?:\?.*)?$"
)


def redact_path(path: str | None) -> str | None:
    """The request path with any capability token masked as `<redacted>`."""
    if not path:
        return path
    # Callers normally pass only a path. Also protect future callers that pass
    # a full callback URI containing a one-use code or state in the query.
    if _OAUTH_RETURN.match(path):
        return _OAUTH_RETURN.sub(r"\1?<redacted>", path)
    out = _TOKEN_PATHS[0].sub(r"\1<redacted>", path)
    return _TOKEN_PATHS[1].sub(r"\1<redacted>\2", out)


class RequestContextMiddleware:
    """Pure-ASGI: request id + one structured access log line + timing headers."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        rid = headers.get("x-request-id") or uuid.uuid4().hex
        state = scope.get("state")
        if not isinstance(state, dict):
            state = scope["state"] = {}
        # ServerErrorMiddleware builds its fallback after our context is reset.
        state["request_id"] = rid
        token = request_id_ctx.set(rid)
        start = time.perf_counter()
        status_code = {"code": 500}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_code["code"] = message["status"]
                elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
                hdrs = message.setdefault("headers", [])
                hdrs.append((b"x-request-id", rid.encode()))
                hdrs.append((b"x-response-time-ms", str(elapsed_ms).encode()))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            log.exception("unhandled error", extra={"path": redact_path(scope.get("path"))})
            raise
        finally:
            elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
            # Skip the noisy health probes from access logs.
            if not scope.get("path", "").startswith("/health"):
                log.info(
                    "request",
                    extra={
                        "method": scope.get("method"),
                        "path": redact_path(scope.get("path")),
                        "status": status_code["code"],
                        "duration_ms": elapsed_ms,
                        "client": (scope.get("client") or ["-"])[0],
                    },
                )
            request_id_ctx.reset(token)


def setup_metrics(app, *, before_scrape: BeforeScrape | None = None) -> bool:
    """Mount Prometheus /metrics + per-request instrumentation. No-op (returns
    False) when prometheus-client isn't installed.

    `before_scrape` is awaited at the START of every scrape (OPS-014). It exists
    as an injected callable rather than an import because this module is `core`,
    which must not know about `services` (test_boundaries); `app.main` — the
    composition root — supplies the queue-gauge refresh."""
    try:
        from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
    except Exception:
        return False

    from fastapi import Response

    requests = Counter("http_requests_total", "HTTP requests", ["method", "path", "status"])
    latency = Histogram("http_request_duration_seconds", "Request latency", ["method", "path"])

    @app.middleware("http")
    async def _instrument(request, call_next):
        # Use the route template (not the raw path) so ids don't explode cardinality.
        start = time.perf_counter()
        response = await call_next(request)
        route = request.scope.get("route")
        path = getattr(route, "path", request.url.path)
        requests.labels(request.method, path, response.status_code).inc()
        latency.labels(request.method, path).observe(time.perf_counter() - start)
        return response

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        if before_scrape is not None:
            try:
                await before_scrape()
            except Exception:  # noqa: BLE001 — a scrape must survive anything
                # OPS-014: the refresh is BEST EFFORT and this except is the
                # whole point of it. Metrics are most valuable exactly when the
                # database is not answering, so a failed refresh serves the
                # previous values rather than a 500 — losing the queue gauges
                # AND the HTTP counters, and blinding the dashboard at the
                # moment an operator is staring at it.
                # `invoiceiq_queue_metrics_updated_timestamp_seconds` stops
                # advancing when this happens, so the staleness is visible
                # instead of silent; alert on its age, not on its absence.
                log.warning("queue gauges not refreshed for this scrape", exc_info=True)
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return True
