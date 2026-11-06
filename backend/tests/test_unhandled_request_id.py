"""Unexpected errors keep the request ID after the middleware stack unwinds.

Use the production handlers and middleware in FastAPI's real ServerErrorMiddleware
stack. Synthetic routes need no database or provider and do not alter app routing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

import pytest
from fastapi import FastAPI, HTTPException, Request
from httpx import ASGITransport, AsyncClient
from starlette.middleware import Middleware
from starlette.middleware.errors import ServerErrorMiddleware

from app.core.errors import ConflictError
from app.core.observability import RequestContextMiddleware, _JsonFormatter, request_id_ctx
from app.main import app as application

PRIVATE = "synthetic-internal-exception-detail"


@pytest.fixture
def request_probe():
    # Copy real middleware/handler registrations; do not unit-call the handler or
    # monkeypatch it into a response that bypasses ServerErrorMiddleware.
    unwound = {}

    class ObserveUnwind:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            before = request_id_ctx.get()
            try:
                await self.app(scope, receive, send)
            finally:
                unwound[scope["path"]] = {"before": before, "after": request_id_ctx.get()}

    middleware = list(application.user_middleware)
    context_index = next(
        index for index, item in enumerate(middleware) if item.cls is RequestContextMiddleware
    )
    # This passive observer runs in the very same task immediately outside the
    # real context middleware. An outer BaseHTTPMiddleware task boundary cannot
    # make the reset assertion pass merely by isolating a leaked ContextVar.
    middleware.insert(context_index, Middleware(ObserveUnwind))
    probe = FastAPI(
        debug=False,
        middleware=middleware,
        exception_handlers=dict(application.exception_handlers),
    )
    observations = {}
    concurrent_entered = set()
    both_entered = asyncio.Event()

    @probe.get("/unexpected/{label}")
    async def unexpected(request: Request, label: str):
        observations[label] = {
            "before": request_id_ctx.get(),
            "state": request.scope.get("state"),
        }
        if label.startswith("concurrent-"):
            concurrent_entered.add(label)
            if len(concurrent_entered) == 2:
                both_entered.set()
            await both_entered.wait()
            await asyncio.sleep(0)
        observations[label]["after"] = request_id_ctx.get()
        raise RuntimeError(PRIVATE)

    @probe.get("/ok")
    async def ok():
        return {"status": "ok"}

    @probe.get("/domain")
    async def domain_error():
        raise ConflictError("Synthetic conflict")

    @probe.get("/http")
    async def http_error():
        raise HTTPException(418, "Synthetic HTTP refusal")

    return probe, observations, unwound


@pytest.fixture
def request_error_logs():
    # Capture the real JSON formatter while each log call's request ContextVar
    # is live. Formatting caplog records after the response would lose that ID.
    entries = []

    class Capture(logging.Handler):
        def emit(self, record):
            entries.append(json.loads(self.format(record)))

    capture = Capture()
    capture.setFormatter(_JsonFormatter())
    logger = logging.getLogger("invoiceiq.access")
    logger.addHandler(capture)
    try:
        yield entries
    finally:
        logger.removeHandler(capture)


def _response_id(response, supplied):
    values = response.headers.get_list("x-request-id")
    assert len(values) == 1, "the outer error response must carry exactly one request ID"
    resolved = values[0]
    if supplied:
        assert resolved == supplied.decode()
    else:
        assert re.fullmatch(r"[0-9a-f]{32}", resolved)
    return resolved


def _error_id(entries, path):
    errors = [
        entry
        for entry in entries
        if entry["msg"] == "unhandled error" and entry.get("path") == path
    ]
    assert len(errors) == 1
    assert PRIVATE in errors[0]["exc"]
    return errors[0]["request_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "supplied",
    [b"upstream-correlation-01", None, b"", b"  preserved spaces  ", "trace-é".encode()],
    ids=["upstream", "generated", "empty-generates", "spaces-preserved", "utf8-preserved"],
)
async def test_unexpected_500_keeps_resolved_id_and_safe_body(
    request_probe, request_error_logs, supplied
):
    probe, observed, unwound = request_probe
    headers = [] if supplied is None else [(b"x-request-id", supplied)]
    outer = request_id_ctx.set("outside-error-request")
    try:
        async with AsyncClient(
            transport=ASGITransport(app=probe, raise_app_exceptions=False), base_url="http://test"
        ) as client:
            response = await client.get("/unexpected/single", headers=headers)
        assert isinstance(probe.middleware_stack, ServerErrorMiddleware)
        assert request_id_ctx.get() == "outside-error-request"
    finally:
        request_id_ctx.reset(outer)
    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error", "code": "internal_error"}
    assert PRIVATE not in response.text
    resolved = _response_id(response, supplied)
    assert resolved == _error_id(request_error_logs, "/unexpected/single")
    assert observed["single"]["before"] == observed["single"]["after"] == resolved
    assert observed["single"]["state"]["request_id"] == resolved
    assert unwound["/unexpected/single"] == {
        "before": "outside-error-request",
        "after": "outside-error-request",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("supplied", [b"handled-correlation", None], ids=["upstream", "generated"])
@pytest.mark.parametrize(
    ("path", "status", "body"),
    [
        ("/ok", 200, {"status": "ok"}),
        ("/domain", 409, {"detail": "Synthetic conflict", "code": "conflict"}),
        ("/http", 418, {"detail": "Synthetic HTTP refusal"}),
    ],
)
async def test_normal_and_handled_responses_have_one_id(
    request_probe, request_error_logs, supplied, path, status, body
):
    probe, _, unwound = request_probe
    headers = [] if supplied is None else [(b"x-request-id", supplied)]
    before = request_id_ctx.get()
    async with AsyncClient(
        transport=ASGITransport(app=probe, raise_app_exceptions=False), base_url="http://test"
    ) as client:
        response = await client.get(path, headers=headers)
    assert response.status_code == status
    assert response.json() == body
    _response_id(response, supplied)
    assert request_id_ctx.get() == before
    assert unwound[path] == {"before": before, "after": before}
    assert not [entry for entry in request_error_logs if entry["msg"] == "unhandled error"]


@pytest.mark.asyncio
async def test_overlapping_unexpected_errors_keep_distinct_ids_and_request_state(
    request_probe, request_error_logs
):
    probe, observed, unwound = request_probe
    outer = request_id_ctx.set("outside-concurrent-requests")
    try:
        async with AsyncClient(
            transport=ASGITransport(app=probe, raise_app_exceptions=False), base_url="http://test"
        ) as client:
            first, second = await asyncio.gather(
                client.get(
                    "/unexpected/concurrent-first", headers={"X-Request-ID": "concurrent-first-id"}
                ),
                client.get("/unexpected/concurrent-second"),
            )
        assert request_id_ctx.get() == "outside-concurrent-requests"
    finally:
        request_id_ctx.reset(outer)
    assert first.status_code == second.status_code == 500
    first_id = _response_id(first, b"concurrent-first-id")
    second_id = _response_id(second, None)
    assert first_id != second_id
    assert observed["concurrent-first"]["state"] is not observed["concurrent-second"]["state"]
    for label, resolved in [("concurrent-first", first_id), ("concurrent-second", second_id)]:
        assert resolved == _error_id(request_error_logs, "/unexpected/" + label)
        assert observed[label]["before"] == observed[label]["after"] == resolved
        assert observed[label]["state"]["request_id"] == resolved
        assert unwound["/unexpected/" + label] == {
            "before": "outside-concurrent-requests",
            "after": "outside-concurrent-requests",
        }
    for response in (first, second):
        assert response.json() == {"detail": "Internal server error", "code": "internal_error"}
        assert PRIVATE not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "incoming_state",
    [None, 17, {"request_id": ["invalid fixture value"], "retained": "unrelated"}],
    ids=["none", "non-dict", "invalid-state-id"],
)
async def test_unexpected_error_tolerates_malformed_incoming_state(
    request_probe, request_error_logs, incoming_state
):
    probe, observed, unwound = request_probe

    async def with_incoming_state(scope, receive, send):
        scope["state"] = incoming_state
        await probe(scope, receive, send)

    before = request_id_ctx.get()
    async with AsyncClient(
        transport=ASGITransport(app=with_incoming_state, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/unexpected/malformed", headers={"X-Request-ID": "malformed-state-correlation"}
        )
    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error", "code": "internal_error"}
    resolved = _response_id(response, b"malformed-state-correlation")
    assert _error_id(request_error_logs, "/unexpected/malformed") == resolved
    assert observed["malformed"]["state"]["request_id"] == resolved
    if isinstance(incoming_state, dict):
        assert observed["malformed"]["state"] is incoming_state
        assert observed["malformed"]["state"]["retained"] == "unrelated"
    assert unwound["/unexpected/malformed"] == {"before": before, "after": before}
    assert request_id_ctx.get() == before


@pytest.mark.asyncio
async def test_invalid_utf8_header_keeps_safe_fallback_before_context_is_resolved(
    request_probe, request_error_logs
):
    probe, observed, unwound = request_probe
    before = request_id_ctx.get()
    async with AsyncClient(
        transport=ASGITransport(app=probe, raise_app_exceptions=False), base_url="http://test"
    ) as client:
        response = await client.get(
            "/unexpected/not-admitted", headers=[(b"x-synthetic-unrelated", b"\xff")]
        )
    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error", "code": "internal_error"}
    _response_id(response, None)
    # Existing header decoding still refuses this request before route/context
    # admission. There is no resolved/logged request ID to recover in this case.
    assert observed == {}
    assert not [entry for entry in request_error_logs if entry["msg"] == "unhandled error"]
    assert unwound["/unexpected/not-admitted"] == {"before": before, "after": before}
    assert request_id_ctx.get() == before
