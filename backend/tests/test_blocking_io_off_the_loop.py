"""Blocking work runs off the event loop (audit 2026-09-05: ARCH-003/004/014,
BE-006/007/008, PERF-007).

The app is one asyncio loop per uvicorn worker. A synchronous socket call or a
CPU-bound hash executed inside a coroutine stalls EVERY request on that
worker — health probes included — for its duration. Five such calls sat in
request paths: the ClamAV scan at every intake door (untimed socket I/O),
the synchronous Stripe SDK (80 s default timeout), a urllib fetch in the FX
refresh route, openpyxl/reportlab in the analytics export, and bcrypt in the
auth routes. Each now runs through `run_in_threadpool`; these tests assert
the call lands on a thread that is NOT the loop's, and — structurally — that
no coroutine calls the synchronous `filesec.check` again.
"""

from __future__ import annotations

import pathlib
import re
import threading

import pytest

from app.services import filesec, fx

APP = pathlib.Path(__file__).resolve().parent.parent / "app"


def test_no_coroutine_calls_the_synchronous_filesec_check():
    """Every intake door must use `filesec.check_async`. The synchronous name
    may appear only in filesec.py itself."""
    offenders = []
    for path in APP.rglob("*.py"):
        if path.name == "filesec.py":
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r"(?<![\w.])filesec\.check\(", line):
                offenders.append(f"{path.relative_to(APP)}:{lineno}")
    assert offenders == [], offenders


@pytest.mark.asyncio
async def test_filesec_check_async_runs_the_gate_on_a_worker_thread(monkeypatch):
    seen: dict = {}

    def _fake_check(filename, content, allowed):
        seen["thread"] = threading.get_ident()
        return "pdf"

    monkeypatch.setattr(filesec, "check", _fake_check)
    assert await filesec.check_async("a.pdf", b"%PDF-1.4", filesec.INVOICE_KINDS) == "pdf"
    assert seen["thread"] != threading.get_ident()


def test_clamd_sockets_carry_a_timeout():
    """A hung clamd used to park the scan for ever; both socket constructors
    now receive the configured timeout so the gate fails closed in time."""
    src = (APP / "services" / "filesec.py").read_text()
    for ctor in ("ClamdUnixSocket", "ClamdNetworkSocket"):
        # One level of nested parentheses: `int(settings.clamav_port)` sits inside.
        call = re.search(ctor + r"\(((?:[^()]|\([^()]*\))*)\)", src, re.DOTALL)
        assert call, f"{ctor} is no longer constructed in filesec.py"
        assert "timeout=timeout" in call.group(1), f"{ctor} is constructed without a timeout"


@pytest.mark.asyncio
async def test_fx_refresh_fetches_off_the_loop(auth_client, db_session, monkeypatch):
    seen: dict = {}

    def _fake_fetch(url, timeout=12):
        seen["thread"] = threading.get_ident()
        return b"<not-xml>"

    monkeypatch.setattr(fx, "_fetch", _fake_fetch)
    res = await fx.refresh_from_ecb(db_session, history=False)
    assert res["ok"] is False  # the fake body does not parse — the fetch still ran
    assert seen["thread"] != threading.get_ident()


@pytest.mark.asyncio
async def test_analytics_writers_render_off_the_loop(auth_client, monkeypatch):
    from app.api.routes import analytics as analytics_routes

    seen: dict = {}

    def _fake_xlsx(result):
        seen["xlsx"] = threading.get_ident()
        return b"PK\x03\x04"

    def _fake_pdf(result):
        seen["pdf"] = threading.get_ident()
        return b"%PDF-1.4"

    monkeypatch.setattr(analytics_routes.report_writers, "to_xlsx", _fake_xlsx)
    monkeypatch.setattr(analytics_routes.report_writers, "to_pdf", _fake_pdf)
    x = await auth_client.get(
        "/api/v1/analytics/explore?currency=EUR&export_contract=2&measure=net&dim=category&format=xlsx"
    )
    p = await auth_client.get(
        "/api/v1/analytics/explore?currency=EUR&export_contract=2&measure=net&dim=category&format=pdf"
    )
    assert x.status_code == 200 and p.status_code == 200
    assert seen["xlsx"] != threading.get_ident() and seen["pdf"] != threading.get_ident()


@pytest.mark.asyncio
async def test_login_verifies_the_password_off_the_loop(auth_client, monkeypatch):
    from app.api.routes import auth as auth_routes

    seen: dict = {}
    real = auth_routes.verify_password

    def _spy(plain, hashed):
        seen["thread"] = threading.get_ident()
        return real(plain, hashed)

    monkeypatch.setattr(auth_routes, "verify_password", _spy)
    r = await auth_client.post(
        "/api/v1/auth/login", json={"email": "owner@acme.io", "password": "supersecret"}
    )
    assert r.status_code == 200, r.text
    assert seen["thread"] != threading.get_ident()


@pytest.mark.asyncio
async def test_stripe_sdk_calls_leave_the_loop_thread():
    from app.services.billing_provider import StripeProvider

    seen: dict = {}

    class _Customer:
        @staticmethod
        def create(**kw):
            seen["thread"] = threading.get_ident()
            return type("C", (), {"id": "cus_1"})()

    class _FakeSDK:
        Customer = _Customer

    provider = StripeProvider.__new__(StripeProvider)
    provider._stripe = _FakeSDK()
    assert (
        await provider.ensure_customer(org_id="o", name="Haulage Co", email="x@y.example")
        == "cus_1"
    )
    assert seen["thread"] != threading.get_ident()


@pytest.mark.asyncio
async def test_r2t2_every_stripe_sdk_call_leaves_the_loop_thread(monkeypatch):
    """R2-T2 (Phase 12 review): the threadpool proof covered `ensure_customer`
    only. `start_checkout`, `create_portal_url` and `report_usage` each call
    the synchronous SDK too — asserted here one by one."""
    from app.core.config import settings
    from app.services.billing_provider import StripeProvider

    seen: dict[str, int] = {}

    def _record(name):
        def _f(**kw):
            seen[name] = threading.get_ident()
            return type("S", (), {"id": "cs_1", "url": "https://stripe.example/s"})()

        return _f

    class _FakeSDK:
        class checkout:
            class Session:
                create = staticmethod(_record("checkout"))

        class billing_portal:
            class Session:
                create = staticmethod(_record("portal"))

        class billing:
            class MeterEvent:
                create = staticmethod(_record("meter"))

    monkeypatch.setattr(type(settings), "stripe_price_for", lambda self, key: "price_pro")
    provider = StripeProvider.__new__(StripeProvider)
    provider._stripe = _FakeSDK()

    out = await provider.start_checkout(
        org_id="o", plan_key="pro", amount_eur=99, order_reference="r", customer_id="cus_1"
    )
    assert out.reference == "cs_1"
    assert await provider.create_portal_url(customer_id="cus_1") == "https://stripe.example/s"
    await provider.report_usage(customer_id="cus_1", meter_event="m", quantity=3, identifier="i")

    here = threading.get_ident()
    assert set(seen) == {"checkout", "portal", "meter"}
    assert all(t != here for t in seen.values()), seen
