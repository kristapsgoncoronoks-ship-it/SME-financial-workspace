"""Docs truth-up insurance (WO-10, deliberately cheap; extended by WO-40/R12).

`README.md` and `ARCHITECTURE.md` once described a ~12-test analytics MVP
against a ~32k-LOC platform — with a bus factor of one, a lying document is
worse than no document. This guard asserts the regenerated front-door docs
exist, don't reassert the known-stale claims, and keep pointing at the real
specification. It is string-level on purpose: anything deeper would itself
drift.

WO-10's truth-up made the docs' prose accurate *once*; by WO-40 (R12) the
hand-written scale numbers (module/page/revision/ADR/CI-job counts) had
silently drifted stale again after ~30 further work orders. The tests below
close that gap: each recomputes its figure straight from the live tree
(filesystem globs, `alembic/versions/`, the ADR directory, the CI workflow
file) and asserts `README.md`'s embedded numbers still match, so a third
drift fails CI instead of waiting for the next manual audit.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
BACKEND = REPO / "backend"

STALE_CLAIMS = ("12 tests", "analytics MVP", "minimal but scalable production MVP")


def test_front_door_docs_carry_no_stale_claims():
    for name in ("README.md", "ARCHITECTURE.md"):
        text = (REPO / name).read_text(encoding="utf-8")
        for claim in STALE_CLAIMS:
            assert claim not in text, f"{name} still contains the stale claim {claim!r}"


def test_readme_points_at_the_real_specification():
    text = (REPO / "README.md").read_text(encoding="utf-8")
    for pointer in ("docs/architecture/adr", "docs/product", "docs/M0-exit-gate.md"):
        assert pointer in text, f"README.md lost its pointer to {pointer}"


def test_architecture_md_is_a_pointer_not_a_fork():
    """ARCHITECTURE.md must stay a short pointer to docs/architecture/ — a
    second long-form architecture document is how the last one rotted."""
    text = (REPO / "ARCHITECTURE.md").read_text(encoding="utf-8")
    assert "docs/architecture/overview.md" in text
    assert len(text.splitlines()) < 60, "ARCHITECTURE.md is growing back into a fork"


def _py_module_count(dir_path: Path) -> int:
    return len([p for p in dir_path.glob("*.py") if p.name != "__init__.py"])


def _ci_job_names() -> list[str]:
    """Top-level job keys under `jobs:` in the (hand-maintained, stable-shape)
    CI workflow file — a two-space-indented `name:` line, matched without a
    YAML dependency."""
    text = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    lines = text.splitlines()
    jobs_idx = next(i for i, line in enumerate(lines) if line.rstrip() == "jobs:")
    names = []
    for line in lines[jobs_idx + 1 :]:
        if line and not line.startswith(" "):
            break  # dedented past the jobs: block
        m = re.match(r"^  ([a-z][a-z0-9_-]*):\s*$", line)
        if m:
            names.append(m.group(1))
    return names


def test_readme_scale_numbers_match_the_live_tree():
    """Every count in README.md's "Scale of the codebase" sentence is
    recomputed from the live tree and must still match. This is the
    regression test for R12 (WO-40): these exact numbers drifted silently
    once already (WO-10 -> this WO), because nothing machine-checked them."""
    model_modules = _py_module_count(BACKEND / "app" / "models")
    service_modules = _py_module_count(BACKEND / "app" / "services")
    route_modules = _py_module_count(BACKEND / "app" / "api" / "routes")
    spa_pages = len(list((REPO / "frontend" / "src" / "pages").glob("*.tsx")))
    alembic_revisions = len(list((BACKEND / "alembic" / "versions").glob("*.py")))
    adr_count = len(
        [p for p in (REPO / "docs" / "architecture" / "adr").glob("*.md") if p.name != "README.md"]
    )
    ci_jobs = _ci_job_names()

    readme = (REPO / "README.md").read_text(encoding="utf-8")

    scale_line = re.search(
        r"\*\*Scale of the codebase \(verified against this tree\):\*\*\s*"
        r"(\d+) database tables\s*\((\d+) Alembic revisions, single head\),\s*"
        r"(\d+) model modules,\s*(\d+) service modules,\s*"
        r"(\d+) route modules,\s*(\d+) SPA pages,\s*"
        r"(\d+) collected backend tests,\s*(\d+) CI jobs\.",
        readme,
    )
    assert scale_line, (
        "README.md's 'Scale of the codebase' sentence changed shape — update this regex"
    )
    (
        readme_tables,
        readme_revisions,
        readme_models,
        readme_services,
        readme_routes,
        readme_pages,
        _readme_tests,  # most volatile figure (grows every WO) — not asserted for equality here
        readme_ci_jobs,
    ) = (int(g) for g in scale_line.groups())

    assert readme_revisions == alembic_revisions, (
        f"README says {readme_revisions} Alembic revisions, tree has {alembic_revisions}"
    )
    assert readme_models == model_modules, (
        f"README says {readme_models} model modules, tree has {model_modules}"
    )
    assert readme_services == service_modules, (
        f"README says {readme_services} service modules, tree has {service_modules}"
    )
    assert readme_routes == route_modules, (
        f"README says {readme_routes} route modules, tree has {route_modules}"
    )
    assert readme_pages == spa_pages, f"README says {readme_pages} SPA pages, tree has {spa_pages}"
    assert readme_ci_jobs == len(ci_jobs), (
        f"README says {readme_ci_jobs} CI jobs, workflow file defines {len(ci_jobs)}: {ci_jobs}"
    )
    # Calendar integration and region OCR coexist with the durable command
    # registries and actor-bound planning receipts: 120 declarations and metadata tables.
    # Preserve the literal tripwire; the ERD and migration gates independently
    # compare the generated documentation and schema with the live metadata.
    assert readme_tables == 120, "table count claim moved — recount __tablename__ across app/models"

    adr_pointer = re.search(r"docs/architecture/adr/README\.md\)\s*\((\d+) ADRs\)", readme)
    assert adr_pointer, "README.md's ADR-count pointer changed shape — update this regex"
    assert int(adr_pointer.group(1)) == adr_count, (
        f"README says {adr_pointer.group(1)} ADRs, docs/architecture/adr/ has {adr_count}"
    )


def test_readme_ci_job_list_names_every_job():
    """README.md's prose CI-job list (which names each job for a human
    reader) must not silently drop a job that exists in the workflow file —
    the omission of `frontend-e2e` was exactly this failure mode (R12)."""
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    ci_section = readme.split("## Commands", 1)[1]
    for job in _ci_job_names():
        assert f"`{job}`" in ci_section, f"README.md's CI section stopped naming the {job!r} job"


def test_architecture_md_scale_numbers_match_the_live_tree():
    text = (REPO / "ARCHITECTURE.md").read_text(encoding="utf-8")
    m = re.search(
        r"drifted\s*\nfatally from the codebase \((\d+) tables, (\d+) backend tests, (\d+) route modules\)\.",
        text,
    )
    assert m, "ARCHITECTURE.md's drift-comparison sentence changed shape — update this regex"
    tables, _tests, routes = (int(g) for g in m.groups())
    assert tables == 64
    assert routes == _py_module_count(BACKEND / "app" / "api" / "routes")

    adr_m = re.search(r"—\s*(\d+) Architecture Decision\s*\n\s*Records", text)
    assert adr_m, "ARCHITECTURE.md's ADR-count claim changed shape — update this regex"
    adr_count = len(
        [p for p in (REPO / "docs" / "architecture" / "adr").glob("*.md") if p.name != "README.md"]
    )
    assert int(adr_m.group(1)) == adr_count


# QA-009 (audit 2026-09-05): the go/no-go document was left at its 2026-08-11
# state for a month — "CI has no runners" and a live prohibition on reconciling
# bank statements, both long closed. String-level, like the guards above: a
# claim that has been struck through or marked CLOSED is allowed to remain as
# history; the same words as a live sentence are not.
RELEASE_GATE_STALE_CLAIMS = (
    "**3.2 CI has no runners.**",
    "Until (b) closes, **this release must not be used",
    "| **open** |",
)


def test_release_gate_carries_no_stale_live_claims():
    text = (REPO / "docs" / "RELEASE-READINESS.md").read_text(encoding="utf-8")
    for claim in RELEASE_GATE_STALE_CLAIMS:
        assert claim not in text, f"RELEASE-READINESS.md still carries the stale claim {claim!r}"
    assert "Re-verified 2026-09-05" in text
