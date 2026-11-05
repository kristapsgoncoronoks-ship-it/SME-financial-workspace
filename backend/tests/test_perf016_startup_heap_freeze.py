"""PERF-016 — the startup heap is frozen before the service serves, and the
perf harness measures that same configuration.

CI #545 failed the R15 growth gate at 12.24× on `transport_reliability`; CI
#544 had read 7.47× on the same commit. Profiling put the tail on the cyclic
garbage collector: a gen-2 pass over the imported app (335,660 tracked
objects) costs ~140 ms and fires about every third request that hydrates a
few thousand ORM rows. Frozen, the same pass costs ~1 ms and the ratio reads
2.87×. Three things must stay true for that to hold:

1. `freeze_startup_heap` really parks the live heap (functional).
2. The app's lifespan calls it LAST — after every import and every startup
   write — so nothing hot is left outside the permanent generation and
   nothing dead is parked in it (structural, on the source).
3. The harness calls it before measuring, in BOTH modes, because
   ASGITransport runs no lifespan and an unfrozen measurement would report a
   tail production does not have (structural, on the source).
"""

from __future__ import annotations

import gc
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from app.core import gc_tuning

BACKEND = Path(__file__).resolve().parents[1]
MAIN = BACKEND / "app" / "main.py"
HARNESS = BACKEND / "scripts" / "perf_harness.py"


def test_freeze_parks_the_live_heap_in_the_permanent_generation(monkeypatch):
    before = gc.get_freeze_count()
    thresholds = gc.get_threshold()
    enabled = gc.isenabled()

    def forbidden():
        pytest.fail("The real heap-freeze proof must not mutate the pytest process heap")

    monkeypatch.setattr(gc, "freeze", forbidden)
    monkeypatch.setattr(gc, "unfreeze", forbidden)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import gc, json; from app.core.gc_tuning import freeze_startup_heap; "
            "before = gc.get_freeze_count(); keep = [[i] for i in range(1000)]; "
            "total = freeze_startup_heap(); "
            "after = gc.get_freeze_count(); assert total >= after; "
            "assert after - before >= len(keep); "
            "tracked = {id(obj) for obj in gc.get_objects()}; "
            "assert all(id(obj) not in tracked for obj in keep); "
            "print(json.dumps({'parked': after - before, 'live': len(keep)}))",
        ],
        cwd=BACKEND,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    observed = json.loads(result.stdout)
    assert observed["parked"] >= observed["live"] == 1000
    assert gc.get_freeze_count() == before
    assert gc.get_threshold() == thresholds
    assert gc.isenabled() == enabled


def test_the_lifespan_freezes_the_heap_after_every_startup_write():
    src = MAIN.read_text(encoding="utf-8")
    assert "from app.core.gc_tuning import freeze_startup_heap" in src
    lifespan = src[src.index("async def lifespan") : src.index("yield")]
    freeze_at = lifespan.index("freeze_startup_heap()")
    # After the schema step and the ECB seeding, before the "ready" line and the
    # yield — the last thing startup does.
    assert lifespan.index("create_all") < freeze_at
    assert lifespan.index("ensure_european_coverage") < freeze_at
    assert freeze_at < lifespan.index("ready (")


def test_the_harness_measures_the_frozen_configuration_in_both_modes():
    src = HARNESS.read_text(encoding="utf-8")
    assert "freeze_startup_heap" in src
    for mode in ("async def run(", "async def concurrency("):
        body = src[src.index(mode) :]
        body = body[: re.search(r"\n(async )?def ", body[1:]).start() + 1]
        head = body[: body.index("AsyncClient(")]
        assert "from app.main import app" in head, mode
        assert "_freeze_like_production()" in head, f"{mode} measures without the freeze"
        # …and the freeze comes AFTER the app import, i.e. after the heap it is
        # meant to park exists — and BEFORE the workspace is seeded, so the
        # seed's garbage is never parked with it.
        assert head.index("from app.main import app") < head.index("_freeze_like_production()")
        seeded_at = body.index("await _prepare_workspace(client, scale)")
        collected_at = body.index("_collect_seed_garbage()")
        assert seeded_at < collected_at, f"{mode} measures with the seed's garbage still pending"


def test_harness_initializes_once_even_when_some_heap_was_already_frozen(monkeypatch):
    from scripts import perf_harness

    # An embedding runtime can freeze objects before importing the application.
    # That count does not prove this application's startup configuration ran.
    monkeypatch.setattr(gc, "get_freeze_count", lambda: 375)
    monkeypatch.setattr(perf_harness, "_startup_heap_frozen", False, raising=False)
    calls = []
    monkeypatch.setattr(gc_tuning, "freeze_startup_heap", lambda: calls.append("freeze"))
    perf_harness._freeze_like_production()
    perf_harness._freeze_like_production()
    assert calls == ["freeze"]


def test_harness_retries_initialization_after_a_failed_freeze(monkeypatch):
    from scripts import perf_harness

    monkeypatch.setattr(gc, "get_freeze_count", lambda: 375)
    monkeypatch.setattr(perf_harness, "_startup_heap_frozen", False, raising=False)
    calls = []

    def freeze():
        calls.append("freeze")
        if len(calls) == 1:
            raise RuntimeError("startup initialization failed")

    monkeypatch.setattr(gc_tuning, "freeze_startup_heap", freeze)
    with pytest.raises(RuntimeError, match="startup initialization failed"):
        perf_harness._freeze_like_production()
    perf_harness._freeze_like_production()
    perf_harness._freeze_like_production()
    assert calls == ["freeze", "freeze"]


def test_the_growth_table_carries_the_median_beside_the_gated_tail():
    """The signal that separated PERF-016 from a real regression was the p50
    ratio disagreeing with the p95 ratio. The harness prints and records both;
    the gate stays on p95."""
    src = HARNESS.read_text(encoding="utf-8")
    assert re.search(r"^\s+p50_ratio: float", src, re.M)
    assert "p50 ratio" in src
    gate = src[src.index("async def shape(") : src.index("def _print_shape")]
    assert "within_ceiling=None if ceiling is None else ratio <= ceiling" in gate
    assert "p50_ratio <= ceiling" not in gate
