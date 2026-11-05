"""WO-94 defect 2 (backlog **N3**) — one upload size cap, one source.

`docs/BACKLOG.md` N3: *"The route hard-codes `_MAX_UPLOAD = 15 MB`
(`routes/invoices.py`) which duplicates `filesec._max_bytes()`
(`settings.max_upload_mb`) … a config change silently doesn't take effect at
the route."* The recon reproduced both halves through the real HTTP path: with
`max_upload_mb = 50` a 20 MB PDF still got `413 "File too large (max 15 MB)"`,
and with `max_upload_mb = 1` a 5 MB PDF passed the route and was refused by
`filesec` as `415` — the wrong status for a size problem.

A sweep found SEVEN hard-coded caps in six route modules, of which the two
25 MB `_ATTACH_MAX` constants were dead: `reject_active_content` caps at the
configured 15 MB, so both routes advertised a limit they could not honour.

This file proves the cap is configurable at the route (both directions, both
sides of the boundary), that a tighter purpose cap is still tighter and can
never widen the general one, and — the real deliverable — that a SECOND cap
cannot reappear anywhere under `app/`. The scanner ships with its own
seeded-violation self-test, because a scan test that cannot fail proves
nothing (`WORK_ORDER_TEMPLATE.md`, "how to write good test requirements",
rule 6).
"""

from __future__ import annotations

import ast
import io
from pathlib import Path

import pytest

from app.services import filesec

APP = Path(__file__).resolve().parents[1] / "app"
# The one module allowed to define a byte cap.
CAP_OWNER = APP / "services" / "filesec.py"

# The identifiers this codebase binds raw uploaded bytes to — every upload
# path reads `content = await file.read()` or `data = await file.read()`.
# A `len(<one of these>) > X` comparison is a byte-size gate.
BYTE_BUFFERS = frozenset({"content", "contents", "data", "body", "payload", "raw", "blob"})


def _pdf(size_bytes: int) -> bytes:
    """A structurally valid PDF of exactly `size_bytes` — it has to survive
    `filesec.check`'s signature test so a rejection can only be about size."""
    head = b"%PDF-1.4\n"
    return head + b"0" * (size_bytes - len(head))


def _upload(size_bytes: int) -> dict:
    return {"file": ("scan.pdf", io.BytesIO(_pdf(size_bytes)), "application/pdf")}


# --------------------------------------------------------------------------- #
# The route enforces the CONFIGURED limit — both sides, both directions
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_wo94_an_upload_under_the_configured_cap_is_accepted(auth_client, monkeypatch):
    monkeypatch.setattr(filesec.settings, "max_upload_mb", 2)
    r = await auth_client.post("/api/v1/invoices/upload", files=_upload(1024 * 1024))
    assert r.status_code == 202, r.text


@pytest.mark.asyncio
async def test_wo94_an_upload_over_the_configured_cap_is_413(auth_client, monkeypatch):
    """The both-sides pair: 1 MB in, 3 MB out, on a 2 MB cap. 413 (a size
    refusal), not 415 (an unsupported TYPE), and the message quotes the cap
    that was actually applied."""
    monkeypatch.setattr(filesec.settings, "max_upload_mb", 2)
    r = await auth_client.post("/api/v1/invoices/upload", files=_upload(3 * 1024 * 1024))
    assert r.status_code == 413, r.text
    assert r.json()["detail"] == "File too large (max 2 MB)"


@pytest.mark.asyncio
async def test_wo94_raising_the_configured_cap_takes_effect_at_the_route(auth_client, monkeypatch):
    """N3's stated harm, inverted. Before WO-94 this returned
    `413 "File too large (max 15 MB)"` however high `max_upload_mb` was set —
    the route never read it."""
    monkeypatch.setattr(filesec.settings, "max_upload_mb", 50)
    r = await auth_client.post("/api/v1/invoices/upload", files=_upload(20 * 1024 * 1024))
    assert r.status_code == 202, r.text


@pytest.mark.asyncio
async def test_wo94_lowering_the_configured_cap_takes_effect_at_the_route(auth_client, monkeypatch):
    """The other direction. Before WO-94 a 5 MB file passed the route's own
    15 MB check and was refused downstream by `filesec` as
    `415 Unsupported Media Type` — a size problem reported as a type problem."""
    monkeypatch.setattr(filesec.settings, "max_upload_mb", 1)
    r = await auth_client.post("/api/v1/invoices/upload", files=_upload(5 * 1024 * 1024))
    assert r.status_code == 413, r.text
    assert r.json()["detail"] == "File too large (max 1 MB)"


# --------------------------------------------------------------------------- #
# The cap function itself
# --------------------------------------------------------------------------- #


def test_wo94_the_general_cap_is_the_configured_value(monkeypatch):
    monkeypatch.setattr(filesec.settings, "max_upload_mb", 7)
    assert filesec.max_bytes() == 7 * 1024 * 1024
    assert filesec.too_large_message() == "File too large (max 7 MB)"


def test_wo94_a_purpose_cap_is_tighter_than_the_general_cap(monkeypatch):
    monkeypatch.setattr(filesec.settings, "max_upload_mb", 15)
    assert filesec.max_bytes("receipt") == 5 * 1024 * 1024
    assert filesec.max_bytes("logo") == 2 * 1024 * 1024
    assert filesec.too_large_message("logo") == "File too large (max 2 MB)"


def test_wo94_a_purpose_cap_can_never_exceed_the_general_cap(monkeypatch):
    """The clamp is the reason a purpose cap is not a second source of truth:
    lowering `MAX_UPLOAD_MB` below a purpose's own figure still takes effect
    there. A purpose can only ever tighten."""
    monkeypatch.setattr(filesec.settings, "max_upload_mb", 1)
    for purpose in filesec.PURPOSE_MB:
        assert filesec.max_bytes(purpose) == 1024 * 1024
        assert filesec.too_large_message(purpose) == "File too large (max 1 MB)"


def test_wo94_a_lowered_general_cap_reaches_the_attachment_gate(monkeypatch):
    """`reject_active_content` — the gate behind the two former 25 MB
    attachment routes — reads the same source, so the 25 MB promise cannot
    come back by the side door."""
    monkeypatch.setattr(filesec.settings, "max_upload_mb", 1)
    with pytest.raises(filesec.FileRejected, match=r"max 1 MB"):
        filesec.reject_active_content(_pdf(2 * 1024 * 1024))


# --------------------------------------------------------------------------- #
# THE STRUCTURAL PROOF — a second cap cannot reappear
# --------------------------------------------------------------------------- #


def _cap_definitions(source: str, *, filename: str) -> list[str]:
    """Every place `source` defines an upload size cap of its own.

    Two independent signals, because either alone is evadable:

    1. **a megabyte-scale literal** — `N * 1024 * 1024` (or `N * 1048576`),
       anywhere in the module. Every one of the seven pre-WO-94 caps was
       written this way, whether as a module constant (`_MAX_UPLOAD`,
       `_ATTACH_MAX`) or inline in the comparison.
    2. **a byte-length gate that does not read `filesec.max_bytes(...)`** — a
       `len(<buffer>) > X` / `>= X` comparison over one of the names the
       upload paths bind their bytes to (`BYTE_BUFFERS`). This catches a cap
       whose number arrived some other way — a settings read, an int constant,
       a plan attribute — which signal 1 alone would not see.

    Signal 2 is deliberately keyed on the BUFFER name rather than on "any
    `len()` comparison": `len(parts) >= hops`, `len(items) >= limit` and
    `len(msg_id) > MSG_ID_MAX` are element and character counts, not byte
    caps, and flagging them would make the scan noise an author learns to
    ignore. The seeded-violation self-tests below are what keep signal 2
    honest about the shape it does catch.
    """
    tree = ast.parse(source, filename=filename)
    found: list[str] = []

    def const_product(node: ast.AST) -> int | None:
        """The value of a constant multiplication chain (`15 * 1024 * 1024`),
        or None if any factor is not an int literal. Folding the chain is what
        makes the signal robust: `N * 1024 * 1024` parses as `(N * 1024) *
        1024`, so pattern-matching a single `1024 * 1024` node misses every
        cap that was actually written in this codebase."""
        if isinstance(node, ast.Constant):
            return node.value if isinstance(node.value, int) else None
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
            left, right = const_product(node.left), const_product(node.right)
            if left is not None and right is not None:
                return left * right
        return None

    def is_mb_literal(node: ast.AST) -> bool:
        if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Mult):
            return False
        value = const_product(node)
        return value is not None and value >= 1024 * 1024 and value % (1024 * 1024) == 0

    def is_max_bytes_call(node: ast.AST) -> bool:
        if not isinstance(node, ast.Call):
            return False
        func = node.func
        if isinstance(func, ast.Attribute):
            return func.attr == "max_bytes"
        return isinstance(func, ast.Name) and func.id == "max_bytes"

    def is_central_processing_budget(node: ast.AST) -> bool:
        # Output/memory ceilings share the existing policy owner, but are not
        # upload acceptance limits. Accept only a direct known-key lookup;
        # local aliases, unknown keys and arbitrary dictionaries remain visible.
        return (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "filesec"
            and node.value.attr == "RESOURCE_BYTES"
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
            and node.slice.value in filesec.RESOURCE_BYTES
        )

    for node in ast.walk(tree):
        if is_mb_literal(node):
            found.append(
                f"{filename}:{getattr(node, 'lineno', 0)}: a megabyte cap literal — "
                "the upload cap lives in app/services/filesec.py (max_bytes)"
            )
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            left, op, right = node.left, node.ops[0], node.comparators[0]
            if (
                isinstance(left, ast.Call)
                and isinstance(left.func, ast.Name)
                and left.func.id == "len"
                and len(left.args) == 1
                and isinstance(left.args[0], ast.Name)
                and left.args[0].id in BYTE_BUFFERS
                and isinstance(op, (ast.Gt, ast.GtE))
                and not is_max_bytes_call(right)
                and not is_central_processing_budget(right)
            ):
                found.append(
                    f"{filename}:{node.lineno}: a byte-size gate not reading "
                    "filesec.max_bytes() — one cap, one source (WO-94/N3)"
                )
    return found


def test_the_cap_scanner_accepts_only_known_central_processing_budgets():
    compliant = (
        "from app.services import filesec\n"
        "if len(content) > filesec.RESOURCE_BYTES['capture_pages']:\n"
        "    raise ValueError('provider response too large')\n"
    )
    assert _cap_definitions(compliant, filename="provider.py") == []
    for other in (
        "filesec.RESOURCE_BYTES['unregistered_resource']",
        "other.RESOURCE_BYTES['capture_pages']",
        "RESOURCE_BYTES['capture_pages']",
        "filesec.OTHER_BYTES['capture_pages']",
        "filesec.RESOURCE_BYTES[purpose]",
        "MAX_RESPONSE_BYTES",
        "256 * 1024",
    ):
        source = compliant.replace("filesec.RESOURCE_BYTES['capture_pages']", other)
        found = _cap_definitions(source, filename="seeded.py")
        assert len(found) == 1 and "byte-size gate" in found[0], (other, found)


#: Byte ceilings that are NOT the upload cap, keyed by `file:line`, each with
#: its reason. The scanner's signal is "a megabyte literal", which is a proxy
#: for "a second upload cap" and cannot tell one quantity from another — so a
#: genuinely different ceiling is named here rather than written as an opaque
#: integer to slip past a gate. Adding an entry is a decision; the test below
#: refuses a stale one, so a moved line cannot leave a silent hole.
NOT_AN_UPLOAD_CAP: dict[str, str] = {
    "app/core/storage.py:33": (
        "S3 file multipart transfer buffer: one provider part is 8MiB, and a "
        "larger file is sent as successive parts. This never rejects an upload "
        "based on total size or replaces the caller's filesec admission limit."
    ),
    "app/core/config.py:207": (
        "PROD-009 `workspace_export_max_bytes` — the ceiling on ONE produced "
        "whole-workspace export zip, sized from the smallest pod that must "
        "hold it. Nothing uploads through it; it bounds what the worker writes "
        "and what the API pod serves."
    ),
}


def _storage_parameter_gates(source: str) -> set[int]:
    """Two generic read boundaries consume a caller limit, not an upload policy.

    Scope this exception to the existing core storage functions. A literal,
    non-None default, or reassignment of the parameter must stay detectable.
    Capture callers still obtain the value from filesec (route regressions
    lower that owner and prove both workers refuse the stored input).
    """
    tree = ast.parse(source)
    functions = [(None, node) for node in tree.body if isinstance(node, ast.FunctionDef)]
    functions += [
        (owner.name, node)
        for owner in tree.body
        if isinstance(owner, ast.ClassDef)
        for node in owner.body
        if isinstance(node, ast.FunctionDef)
    ]
    allowed = set()
    for owner, function in functions:
        if (owner, function.name) not in {(None, "_read"), ("MemoryStorage", "get")}:
            continue
        positional = function.args.posonlyargs + function.args.args
        defaults = [None] * (len(positional) - len(function.args.defaults))
        parameters = list(zip(positional, defaults + function.args.defaults, strict=True))
        parameters += list(zip(function.args.kwonlyargs, function.args.kw_defaults, strict=True))
        bound = next((default for arg, default in parameters if arg.arg == "max_bytes"), False)
        if bound is not None and not (isinstance(bound, ast.Constant) and bound.value is None):
            continue
        if any(
            isinstance(node, ast.Name)
            and node.id == "max_bytes"
            and isinstance(node.ctx, ast.Store)
            for node in ast.walk(function)
        ):
            continue
        for node in ast.walk(function):
            if (
                isinstance(node, ast.Compare)
                and len(node.ops) == 1
                and isinstance(node.ops[0], ast.Gt)
                and isinstance(node.left, ast.Call)
                and isinstance(node.left.func, ast.Name)
                and node.left.func.id == "len"
                and isinstance(node.comparators[0], ast.Name)
                and node.comparators[0].id == "max_bytes"
            ):
                allowed.add(node.lineno)
    return allowed


def _cap_policy_violations(source: str, *, filename: str) -> list[str]:
    caller_gates = _storage_parameter_gates(source) if filename == "app/core/storage.py" else set()
    violations = []
    for item in _cap_definitions(source, filename=filename):
        if item.split(": ", 1)[0] in NOT_AN_UPLOAD_CAP:
            continue
        # A literal on the same line remains forbidden. Exempt only the
        # comparison signal, not every finding at this source location.
        if int(item.split(":")[1]) in caller_gates and ": a byte-size gate" in item:
            continue
        violations.append(item)
    return violations


def test_storage_parameter_exception_rejects_seeded_literal_default_and_reassignment():
    source = (APP / "core/storage.py").read_text()
    assert len(_storage_parameter_gates(source)) == 2
    for seeded in (
        source.replace("len(data) > max_bytes", "len(data) > 9999999"),
        source.replace("max_bytes: int | None = None", "max_bytes: int | None = 9999999"),
        source.replace(
            "if len(data) > max_bytes:",
            "if len(data) > max_bytes and (25 * 1024 * 1024):",
        ),
        source.replace(
            "        if len(data) > max_bytes:",
            "        max_bytes = 9999999\n        if len(data) > max_bytes:",
        ),
    ):
        assert _cap_policy_violations(seeded, filename="app/core/storage.py")


def test_multipart_buffer_exception_does_not_permit_an_upload_gate():
    source = (APP / "core/storage.py").read_text()
    seeded = source + (
        "\ndef accidental_upload_gate(content):\n"
        "    if len(content) > _MULTIPART_BYTES:\n"
        "        raise ValueError('unapproved upload cap')\n"
    )
    found = _cap_policy_violations(seeded, filename="app/core/storage.py")
    assert any("byte-size gate" in finding for finding in found), found


def test_the_not_an_upload_cap_list_points_at_real_megabyte_literals():
    """The exemptions cannot rot into a hole: each must still name a line the
    scanner actually flags. A moved or deleted literal fails here."""
    flagged = set()
    for path in sorted(APP.rglob("*.py")):
        if path == CAP_OWNER:
            continue
        for violation in _cap_definitions(
            path.read_text(encoding="utf-8"), filename=str(path.relative_to(APP.parent))
        ):
            flagged.add(violation.split(": ", 1)[0])
    stale = sorted(k for k in NOT_AN_UPLOAD_CAP if k not in flagged)
    assert stale == [], f"exemptions naming nothing the scanner flags — remove them: {stale}"
    assert all(len(v) > 40 for v in NOT_AN_UPLOAD_CAP.values()), "an exemption without a reason"


def test_wo94_no_second_upload_cap_is_defined_anywhere_in_the_app():
    """N3's real deliverable. Every module under `app/` except the cap's owner
    must reach the limit through `filesec.max_bytes(...)`.

    Routes are the modules that carried all seven pre-WO-94 caps, but the scan
    is over the WHOLE package deliberately: a service or a core module growing
    its own `25 * 1024 * 1024` would fork the truth exactly as effectively, and
    a routes-only scan would not see it."""
    violations: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        if path == CAP_OWNER:
            continue
        source = path.read_text(encoding="utf-8")
        violations.extend(
            _cap_policy_violations(source, filename=str(path.relative_to(APP.parent)))
        )
    assert violations == [], "a second upload cap:\n" + "\n".join(violations)


def test_wo94_the_cap_scanner_detects_a_seeded_violation():
    """The self-test. The scanner runs over source that re-introduces the real
    defect — the exact two lines deleted from `routes/invoices.py` — and must
    name both signals; then over the compliant rewrite, and report nothing."""
    seeded = (
        "_MAX_UPLOAD = 15 * 1024 * 1024  # 15 MB\n"
        "async def upload_invoice(file):\n"
        "    content = await file.read()\n"
        "    if len(content) > _MAX_UPLOAD:\n"
        "        raise HTTPException(413, 'File too large (max 15 MB)')\n"
    )
    found = _cap_definitions(seeded, filename="seeded.py")
    assert len(found) == 2, found
    assert "megabyte cap literal" in found[0]
    assert "byte-size gate not reading filesec.max_bytes()" in found[1]

    compliant = (
        "from app.services import filesec\n"
        "async def upload_invoice(file):\n"
        "    content = await file.read()\n"
        "    if len(content) > filesec.max_bytes():\n"
        "        raise HTTPException(413, filesec.too_large_message())\n"
    )
    assert _cap_definitions(compliant, filename="compliant.py") == []


def test_wo94_the_cap_scanner_sees_a_purpose_cap_fork_too():
    """The tighter caps are the easier fork to reintroduce, because 5 MB for a
    receipt photo looks obviously reasonable. It is still a second definition:
    it escapes the clamp, so lowering MAX_UPLOAD_MB stops taking effect there."""
    seeded = (
        "async def receipt_scan(file):\n"
        "    content = await file.read()\n"
        "    if len(content) > 5 * 1024 * 1024:\n"
        "        raise HTTPException(413, 'Receipt too large (max 5 MB)')\n"
    )
    found = _cap_definitions(seeded, filename="seeded.py")
    assert len(found) == 2, found

    compliant = (
        "from app.services import filesec\n"
        "async def receipt_scan(file):\n"
        "    content = await file.read()\n"
        "    if len(content) > filesec.max_bytes('receipt'):\n"
        "        raise HTTPException(413, filesec.too_large_message('receipt'))\n"
    )
    assert _cap_definitions(compliant, filename="compliant.py") == []


def test_wo94_the_cap_scanner_catches_a_fork_carrying_no_megabyte_literal():
    """Signal 2 alone, proven alone. A cap read straight out of settings has
    no `1024 * 1024` anywhere and slips past signal 1 entirely — and it is the
    most plausible next fork, because it looks like it is honouring the config
    while quietly applying its own number."""
    seeded = (
        "from app.core.config import settings\n"
        "async def upload_invoice(file):\n"
        "    content = await file.read()\n"
        "    if len(content) > settings.max_upload_mb * 1000000:\n"
        "        raise HTTPException(413, 'File too large')\n"
    )
    found = _cap_definitions(seeded, filename="seeded.py")
    assert len(found) == 1, found
    assert "byte-size gate not reading filesec.max_bytes()" in found[0]


def test_wo94_the_cap_scanner_does_not_flag_element_or_character_counts():
    """The narrowing is deliberate, so it is asserted rather than assumed.
    `len(parts) >= hops` (`core/ratelimit.py`), `len(items) >= limit`
    (`services/approval_policy.py`) and `len(msg_id) > MSG_ID_MAX`
    (`services/sepa.py`) are all real lines in this tree and none of them is a
    byte cap. A scanner that shouted about them would be one an author learns
    to route around."""
    benign = (
        "def pick(parts, hops, items, limit, msg_id):\n"
        "    if len(parts) >= hops:\n"
        "        return parts[-hops]\n"
        "    if len(items) >= limit:\n"
        "        return items[:limit]\n"
        "    if len(msg_id) > MSG_ID_MAX:\n"
        "        raise SepaError('too long')\n"
        "    return None\n"
    )
    assert _cap_definitions(benign, filename="benign.py") == []
