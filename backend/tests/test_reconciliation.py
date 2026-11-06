"""Bank reconciliation (Phase 12): import a statement (both directions), refuse a
duplicate upload, suggest + confirm a credit→receipt match and a debit→paid-payout
match, refuse an amount mismatch, unmatch/ignore, the PAYMENT_* authz split, and a
tenant-safe composite FK bank_lines→bank_statements."""

import io
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.bank_import import BankLine, BankStatement
from app.models.expense import ReimbursementBatch
from app.models.organization import Organization
from tests.reconciliation_command_client import transition

CSV = (
    "date,description,credit,debit\n"
    "2026-02-05,Payment from Acme INV-1,1210.00,\n"
    "2026-02-10,Payroll payout PAYROLL-1,,250.00\n"
)


def _h(token):
    return {"Authorization": f"Bearer {token}"}


async def _enable(auth_client):
    await auth_client.put("/api/v1/modules/issuing", json={"enabled": True})


async def _upload(auth_client, text=CSV, name="stmt.csv"):
    files = {"file": (name, io.BytesIO(text.encode()), "text/csv")}
    return await auth_client.post("/api/v1/reconciliation/import", files=files)


async def _org_id(db_session):
    return await db_session.scalar(select(Organization.id))


async def _member(auth_client, client, email, role="user"):
    inv = await auth_client.post("/api/v1/team/invites", json={"email": email, "role": role})
    acc = await client.post(
        "/api/v1/auth/accept-invite",
        json={"token": inv.json()["token"], "name": "M", "password": "supersecret"},
    )
    return acc.json()["token"]["access_token"]


@pytest.mark.asyncio
async def test_import_parses_both_directions(auth_client):
    await _enable(auth_client)
    r = await _upload(auth_client)
    assert r.status_code == 201, r.text
    assert r.json()["imported"] == 2

    stmts = (await auth_client.get("/api/v1/reconciliation/statements")).json()
    assert len(stmts) == 1 and stmts[0]["unmatched"] == 2 and stmts[0]["matched"] == 0

    lines = (
        await auth_client.get(f"/api/v1/reconciliation/statements/{stmts[0]['id']}/lines")
    ).json()
    by_dir = {ln["direction"]: ln for ln in lines}
    assert by_dir["credit"]["amount"] == "1210.00"  # money in → positive
    assert by_dir["debit"]["amount"] == "-250.00"  # money out → negative


@pytest.mark.asyncio
async def test_duplicate_upload_refused(auth_client):
    await _enable(auth_client)
    assert (await _upload(auth_client)).status_code == 201
    dup = await _upload(auth_client)
    assert dup.status_code == 409 and "already imported" in dup.json()["detail"]


@pytest.mark.asyncio
async def test_credit_matches_receipt(auth_client):
    await _enable(auth_client)
    # A receipt for the same amount, referencing INV-1 (also in the bank line text).
    rec = (
        await auth_client.post(
            "/api/v1/receipts",
            json={"amount": "1210.00", "received_on": "2026-02-05", "reference": "INV-1"},
        )
    ).json()
    await _upload(auth_client)
    sid = (await auth_client.get("/api/v1/reconciliation/statements")).json()[0]["id"]
    lines = (await auth_client.get(f"/api/v1/reconciliation/statements/{sid}/lines")).json()
    credit = next(ln for ln in lines if ln["direction"] == "credit")

    cands = (
        await auth_client.get(f"/api/v1/reconciliation/lines/{credit['id']}/candidates")
    ).json()
    assert any(c["kind"] == "receipt" and c["id"] == rec["id"] for c in cands)

    m = await transition(
        auth_client, credit["id"], "match", target={"kind": "receipt", "id": rec["id"]}
    )
    assert m.status_code == 200, m.text
    assert m.json()["status"] == "matched" and m.json()["matched_id"] == rec["id"]

    # The statement now reports one matched, one unmatched.
    st = (await auth_client.get("/api/v1/reconciliation/statements")).json()[0]
    assert st["matched"] == 1 and st["unmatched"] == 1

    # Unmatch returns it to the pool.
    u = await transition(auth_client, credit["id"], "unmatch")
    assert u.status_code == 200 and u.json()["status"] == "unmatched"


@pytest.mark.asyncio
async def test_debit_matches_paid_reimbursement(auth_client, db_session):
    await _enable(auth_client)
    org = await _org_id(db_session)
    db_session.add(
        ReimbursementBatch(
            org_id=org,
            reference="PAYROLL-1",
            status="paid",
            total_eur=Decimal("250.00"),
            paid_at=datetime(2026, 2, 10, tzinfo=UTC),
            version=1,
        )
    )
    await db_session.commit()
    batch = await db_session.scalar(select(ReimbursementBatch))

    await _upload(auth_client)
    sid = (await auth_client.get("/api/v1/reconciliation/statements")).json()[0]["id"]
    lines = (await auth_client.get(f"/api/v1/reconciliation/statements/{sid}/lines")).json()
    debit = next(ln for ln in lines if ln["direction"] == "debit")

    cands = (await auth_client.get(f"/api/v1/reconciliation/lines/{debit['id']}/candidates")).json()
    assert any(c["kind"] == "reimbursement" and c["id"] == batch.id for c in cands)

    m = await transition(
        auth_client, debit["id"], "match", target={"kind": "reimbursement", "id": batch.id}
    )
    assert m.status_code == 200 and m.json()["status"] == "matched"


@pytest.mark.asyncio
async def test_receipt_cannot_reconcile_to_two_lines(auth_client):
    """1:1 invariant (REC-1): a receipt already matched to one bank line is refused
    on a second. Under concurrency the target row is locked FOR UPDATE so two racing
    confirms serialize on it; this exercises the guard the lock protects."""
    await _enable(auth_client)
    rec = (
        await auth_client.post(
            "/api/v1/receipts", json={"amount": "1210.00", "received_on": "2026-02-05"}
        )
    ).json()
    # Two identical credit lines, same amount as the receipt.
    two_credits = (
        "date,description,credit,debit\n"
        "2026-02-05,Payment A,1210.00,\n"
        "2026-02-06,Payment B,1210.00,\n"
    )
    await _upload(auth_client, text=two_credits)
    sid = (await auth_client.get("/api/v1/reconciliation/statements")).json()[0]["id"]
    lines = (await auth_client.get(f"/api/v1/reconciliation/statements/{sid}/lines")).json()
    a, b = lines[0], lines[1]

    m1 = await transition(
        auth_client, a["id"], "match", target={"kind": "receipt", "id": rec["id"]}
    )
    assert m1.status_code == 200, m1.text

    m2 = await transition(
        auth_client, b["id"], "match", target={"kind": "receipt", "id": rec["id"]}
    )
    assert m2.status_code == 400 and "already reconciled" in m2.json()["detail"]

    # After unmatching the first, the receipt is free to reconcile to the second.
    await transition(auth_client, a["id"], "unmatch")
    m3 = await transition(
        auth_client, b["id"], "match", target={"kind": "receipt", "id": rec["id"]}
    )
    assert m3.status_code == 200, m3.text


@pytest.mark.asyncio
async def test_amount_mismatch_and_wrong_direction_refused(auth_client):
    await _enable(auth_client)
    rec = (
        await auth_client.post(
            "/api/v1/receipts", json={"amount": "999.00", "received_on": "2026-02-05"}
        )
    ).json()
    await _upload(auth_client)
    sid = (await auth_client.get("/api/v1/reconciliation/statements")).json()[0]["id"]
    lines = (await auth_client.get(f"/api/v1/reconciliation/statements/{sid}/lines")).json()
    credit = next(ln for ln in lines if ln["direction"] == "credit")  # 1210, ≠ 999
    debit = next(ln for ln in lines if ln["direction"] == "debit")

    bad = await transition(
        auth_client, credit["id"], "match", target={"kind": "receipt", "id": rec["id"]}
    )
    assert bad.status_code == 400 and "mismatch" in bad.json()["detail"]

    # A debit line cannot match a receipt (money received).
    wrong = await transition(
        auth_client, debit["id"], "match", target={"kind": "receipt", "id": rec["id"]}
    )
    assert wrong.status_code == 400 and "debit" in wrong.json()["detail"]


@pytest.mark.asyncio
async def test_ignore_line(auth_client):
    await _enable(auth_client)
    await _upload(auth_client)
    sid = (await auth_client.get("/api/v1/reconciliation/statements")).json()[0]["id"]
    line = (await auth_client.get(f"/api/v1/reconciliation/statements/{sid}/lines")).json()[0]
    ig = await transition(auth_client, line["id"], "ignore")
    assert ig.status_code == 200 and ig.json()["status"] == "ignored"
    st = (await auth_client.get("/api/v1/reconciliation/statements")).json()[0]
    assert st["ignored"] == 1


@pytest.mark.asyncio
async def test_payment_authz_split(auth_client, client):
    await _enable(auth_client)
    await _upload(auth_client)

    # EMPLOYEE (role 'user') has no PAYMENT_READ → the router gate 403s everything.
    emp = await _member(auth_client, client, "emp@corp.io", role="user")
    assert (
        await client.get("/api/v1/reconciliation/statements", headers=_h(emp))
    ).status_code == 403

    # READ_ONLY (role 'user_free') has PAYMENT_READ but not PAYMENT_WRITE.
    ro = await _member(auth_client, client, "ro@corp.io", role="user_free")
    assert (
        await client.get("/api/v1/reconciliation/statements", headers=_h(ro))
    ).status_code == 200
    files = {"file": ("x.csv", io.BytesIO(b"date,amount\n2026-01-01,5.00\n"), "text/csv")}
    assert (
        await client.post("/api/v1/reconciliation/import", headers=_h(ro), files=files)
    ).status_code == 403


@pytest.mark.asyncio
async def test_cross_tenant_bank_line_blocked_at_db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _fk_on(dbapi_conn, _rec):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sm() as db:
            a, b = Organization(name="A"), Organization(name="B")
            db.add_all([a, b])
            await db.flush()
            stmt_a = BankStatement(org_id=a.id, filename="a.csv", sha256="x" * 64, line_count=0)
            db.add(stmt_a)
            await db.flush()
            # org B line pointing at org A's statement → composite FK violation.
            db.add(
                BankLine(
                    org_id=b.id,
                    statement_id=stmt_a.id,
                    line_date=date(2026, 1, 1),
                    amount=Decimal("1.00"),
                    direction="credit",
                )
            )
            with pytest.raises(IntegrityError):
                await db.commit()
    finally:
        await engine.dispose()


# --- DB-004 (audit 2026-09-05): the duplicate-import guard is a DATABASE invariant


@pytest.mark.asyncio
async def test_db004_two_statements_with_one_sha_are_refused_by_the_database(
    auth_client, db_session
):
    """The SELECT-then-INSERT guard let two racing uploads of the same file both
    import. The unique index (org_id, sha256) holds the invariant below the
    application; a second row is an IntegrityError however it arrives."""
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    from app.models.bank_import import BankStatement
    from app.models.organization import Organization

    org_id = await db_session.scalar(select(Organization.id).limit(1))
    for _ in range(2):
        db_session.add(
            BankStatement(
                org_id=org_id,
                filename="camt.xml",
                source_format="camt",
                method="camt.053",
                sha256="a" * 64,
                line_count=1,
            )
        )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_db004_a_race_past_the_select_is_answered_like_a_sequential_duplicate(
    auth_client, db_session, monkeypatch
):
    """Simulate the race: the fast-path SELECT sees nothing (as it does for the
    second of two concurrent uploads), so only the unique index stands between
    the org and a second copy of its bank data. The caller gets the same 409
    sentence the sequential duplicate gets, not a 500."""
    from app.services import reconciliation

    await _enable(auth_client)
    assert (await _upload(auth_client)).status_code == 201

    async def _never_seen(db, org_id, sha):
        return False

    monkeypatch.setattr(reconciliation, "_already_imported", _never_seen)
    dup = await _upload(auth_client)
    assert dup.status_code == 409, dup.text
    assert "already imported" in dup.json()["detail"]
