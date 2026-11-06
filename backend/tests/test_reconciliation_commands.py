"""Reviewed bank-line intentions commit once; old writes cannot erase a new cycle."""

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.bank_import import BankLine, BankStatement
from app.models.organization import Organization
from tests.test_reconciliation import _enable


async def source_line(auth_client, db_session, *, initial="unmatched"):
    await _enable(auth_client)
    org = await db_session.scalar(select(Organization.id))
    statement = BankStatement(
        org_id=org, filename="synthetic.csv", sha256=uuid4().hex * 2, line_count=1
    )
    db_session.add(statement)
    await db_session.flush()
    line = BankLine(
        org_id=org,
        statement_id=statement.id,
        line_date=date(2026, 11, 6),
        description="Synthetic bank evidence",
        amount=Decimal("10.00"),
        currency="EUR",
        direction="credit",
        status=initial,
    )
    db_session.add(line)
    await db_session.commit()
    return line.id


def intent(
    operation, version, *, state="unmatched", kind=None, target_id=None, reason=None, target=None
):
    return {
        "operation": operation,
        "expected_version": version,
        "expected": {"status": state, "matched_kind": kind, "matched_id": target_id},
        "target": target,
        "reason": reason,
    }


@pytest.mark.asyncio
async def test_ignore_commits_one_frozen_receipt_and_actor_owned_get(auth_client, db_session):
    line_id = await source_line(auth_client, db_session)
    response = await auth_client.post(
        f"/api/v1/reconciliation/lines/{line_id}/commands",
        headers={"Idempotency-Key": "rn-ignore-first"},
        json=intent("ignore", 1, reason="  Reviewed fee explanation\n  "),
    )
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt["status"] == "committed" and receipt["operation"] == "ignore"
    assert receipt["command_key"] == "rn-ignore-first"
    assert (receipt["before_version"], receipt["after_version"]) == (1, 2)
    assert receipt["reason"] == "Reviewed fee explanation"
    assert receipt["line"]["status"] == "ignored" and receipt["line"]["version"] == 2
    checked = await auth_client.get(
        f"/api/v1/reconciliation/lines/{line_id}/commands/rn-ignore-first"
    )
    assert checked.status_code == 200 and checked.json() == receipt
    current = await auth_client.get(f"/api/v1/reconciliation/lines/{line_id}")
    assert current.status_code == 200 and current.json()["version"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,tail,initial,body",
    [
        ("POST", "match", "unmatched", "match"),
        ("POST", "match", "unmatched", None),
        ("POST", "ignore", "unmatched", None),
        ("DELETE", "match", "matched", None),
        ("DELETE", "match", "ignored", None),
    ],
)
async def test_legacy_alias_never_infers_or_mutates_an_intention(
    auth_client, db_session, method, tail, initial, body
):
    line_id = await source_line(auth_client, db_session, initial=initial)
    receipt = (
        await auth_client.post(
            "/api/v1/receipts", json={"amount": "10.00", "received_on": "2026-11-06"}
        )
    ).json()
    line = await db_session.get(BankLine, line_id)
    if initial == "matched":
        line.matched_kind, line.matched_id = "receipt", receipt["id"]
        await db_session.commit()
    before = (line.status, line.matched_kind, line.matched_id, getattr(line, "version", None))
    response = await auth_client.request(
        method,
        f"/api/v1/reconciliation/lines/{line_id}/{tail}",
        json={"kind": "receipt", "target_id": receipt["id"]} if body == "match" else None,
    )
    await db_session.refresh(line)
    after = (line.status, line.matched_kind, line.matched_id, getattr(line, "version", None))
    assert response.status_code == 428, (
        f"legacy{method} returned{response.status_code}; state {before}->{after}"
    )
    assert response.json()["code"] == "reconciliation_command_preconditions_required"
    assert after == before


async def post_command(client, line_id, body, key=None):
    return await client.post(
        f"/api/v1/reconciliation/lines/{line_id}/commands",
        headers={"Idempotency-Key": key or f"rn-{uuid4().hex}"},
        json=body,
    )


@pytest.mark.asyncio
async def test_historical_replay_survives_a_later_cycle_and_target_changes(auth_client, db_session):
    from app.models.receipt import Receipt
    from app.models.reconciliation_command import ReconciliationCommand

    line_id = await source_line(auth_client, db_session)
    target = (
        await auth_client.post(
            "/api/v1/receipts", json={"amount": "10.00", "received_on": "2026-11-06"}
        )
    ).json()["id"]
    original = intent("match", 1, target={"kind": "receipt", "id": target.upper()})
    first = await post_command(auth_client, line_id, original, "match-old")
    assert first.status_code == 200, first.text
    assert first.json()["line"]["matched_id"] == target
    second = await post_command(
        auth_client,
        line_id,
        intent("unmatch", 2, state="matched", kind="receipt", target_id=target),
    )
    assert second.status_code == 200, second.text
    third = await post_command(auth_client, line_id, intent("ignore", 3, reason="Reviewed source"))
    assert third.status_code == 200, third.text
    restored = await post_command(auth_client, line_id, intent("restore", 4, state="ignored"))
    assert restored.status_code == 200, restored.text
    row = await db_session.get(Receipt, target)
    row.amount = Decimal("99.00")
    await db_session.commit()
    normalized = {**original, "target": {"kind": "receipt", "id": target}}
    replay = await post_command(auth_client, line_id, normalized, "match-old")
    assert replay.status_code == 200 and replay.content == first.content
    assert (await auth_client.get(f"/api/v1/reconciliation/lines/{line_id}")).json()["version"] == 5
    count = len(list(await db_session.scalars(select(ReconciliationCommand))))
    assert count == 4
    stale = await post_command(auth_client, line_id, normalized, "match-new")
    assert stale.status_code == 409 and stale.json()["code"] == "reconciliation_line_stale"
    changed = await post_command(
        auth_client, line_id, intent("ignore", 1, reason="Changed intention"), "match-old"
    )
    assert (
        changed.status_code == 409
        and changed.json()["code"] == "reconciliation_command_payload_conflict"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["none", "raise", "after_append"])
async def test_audit_failure_rolls_back_line_counter_receipt_and_event(
    auth_client, db_session, monkeypatch, fault
):
    from app.models.audit import AuditEvent
    from app.models.reconciliation_command import ReconciliationCommand
    from app.services import audit

    line_id = await source_line(auth_client, db_session)

    original_record = audit.record
    before_events = [row.id for row in await db_session.scalars(select(AuditEvent))]

    async def failed(*args, **kwargs):
        if fault == "after_append":
            event = await original_record(*args, **kwargs)
            assert event is not None
            raise RuntimeError("synthetic failure after actual command and audit flush")
        if fault == "raise":
            raise RuntimeError("synthetic audit failure with private source")
        return None

    monkeypatch.setattr(audit, "record", failed)
    result = await post_command(
        auth_client, line_id, intent("ignore", 1, reason="Secret explanation"), "audit-fault"
    )
    assert result.status_code == 503, result.text
    assert "private source" not in result.text and "Secret" not in result.text
    line = await db_session.get(BankLine, line_id, populate_existing=True)
    assert (line.status, line.version, line.matched_kind, line.matched_id) == (
        "unmatched",
        1,
        None,
        None,
    )
    assert not list(await db_session.scalars(select(ReconciliationCommand)))
    assert [row.id for row in await db_session.scalars(select(AuditEvent))] == before_events


@pytest.mark.asyncio
async def test_version_exhaustion_does_not_wrap_or_create_a_command(auth_client, db_session):
    from app.models.reconciliation_command import ReconciliationCommand

    line_id = await source_line(auth_client, db_session)
    line = await db_session.get(BankLine, line_id)
    line.version = 2147483647
    await db_session.commit()
    result = await post_command(
        auth_client, line_id, intent("ignore", 2147483647, reason="Reviewed")
    )
    assert result.status_code == 409 and result.json()["code"] == "reconciliation_version_exhausted"
    await db_session.refresh(line)
    assert (line.version, line.status) == (2147483647, "unmatched")
    assert not list(await db_session.scalars(select(ReconciliationCommand)))


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [True, "1", 1.0, 0, -1, 2147483648])
async def test_version_is_required_strict_bounded_integer(auth_client, db_session, version):
    line_id = await source_line(auth_client, db_session)
    result = await post_command(auth_client, line_id, intent("ignore", version, reason="Reviewed"))
    assert result.status_code == 422, result.text
    assert (await auth_client.get(f"/api/v1/reconciliation/lines/{line_id}")).json()["version"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason", [None, "", " \t\n\u2003", "x" * 1001, "bad\x00text", "bad\ud800text"]
)
async def test_ignore_requires_valid_nonblank_bounded_utf8_reason(auth_client, db_session, reason):
    import json

    line_id = await source_line(auth_client, db_session)
    # ensure_ascii emits a valid UTF-8 request even for an escaped lone surrogate.
    result = await auth_client.post(
        f"/api/v1/reconciliation/lines/{line_id}/commands",
        headers={"Idempotency-Key": "invalid-reason", "Content-Type": "application/json"},
        content=json.dumps(intent("ignore", 1, reason=reason)),
    )
    assert result.status_code == 422, result.text


@pytest.mark.asyncio
async def test_history_is_bounded_and_has_no_command_payload_or_key(auth_client, db_session):
    line_id = await source_line(auth_client, db_session)
    for version in range(1, 5):
        ignored = version % 2 == 0
        result = await post_command(
            auth_client,
            line_id,
            intent(
                "restore" if ignored else "ignore",
                version,
                state="ignored" if ignored else "unmatched",
                reason=None if ignored else f"Reviewed explanation {version}",
            ),
            f"private-key-{version}",
        )
        assert result.status_code == 200, result.text
    path = f"/api/v1/reconciliation/lines/{line_id}/history"
    page = (await auth_client.get(path, params={"limit": 2})).json()
    assert [e["after_version"] for e in page["events"]] == [5, 4]
    assert page["next_before_version"] == 4
    assert [e["reason"] for e in page["events"]] == [None, "Reviewed explanation 3"]
    assert "command_key" not in str(page) and "payload" not in str(page)
    last = (await auth_client.get(path, params={"limit": 2, "before_version": 4})).json()
    assert [e["after_version"] for e in last["events"]] == [3, 2]
    assert last["next_before_version"] is None
    assert [e["reason"] for e in last["events"]] == [None, "Reviewed explanation 1"]
    assert (await auth_client.get(path, params={"limit": 101})).status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field", ["payload_hash", "result_hash", "result_json", "before_state_json", "after_state_json"]
)
async def test_corrupted_receipt_is_unavailable_not_reexecuted(auth_client, db_session, field):
    from app.models.reconciliation_command import ReconciliationCommand

    line_id = await source_line(auth_client, db_session)
    body = intent("ignore", 1, reason="Reviewed")
    result = await post_command(auth_client, line_id, body, "corrupt-key")
    assert result.status_code == 200, result.text
    row = await db_session.scalar(select(ReconciliationCommand))
    setattr(row, field, "0" * 64 if field.endswith("hash") else "{}")
    await db_session.commit()
    result = await post_command(auth_client, line_id, body, "corrupt-key")
    assert (
        result.status_code == 503 and result.json()["code"] == "reconciliation_command_unavailable"
    )
    own = await auth_client.get(f"/api/v1/reconciliation/lines/{line_id}/commands/corrupt-key")
    assert own.status_code == 503
    assert (await auth_client.get(f"/api/v1/reconciliation/lines/{line_id}")).json()["version"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("after_commit", [False, True])
async def test_commit_transport_failure_is_recovered_only_by_the_durable_key(
    auth_client, db_session, monkeypatch, after_commit
):
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.reconciliation_command import ReconciliationCommand

    line_id = await source_line(auth_client, db_session)
    body = intent("ignore", 1, reason="Reviewed once")
    original_commit = AsyncSession.commit

    async def uncertain_commit(db):
        if after_commit:
            await original_commit(db)
        raise OSError("synthetic transport loss; private connection detail")

    with monkeypatch.context() as patch:
        patch.setattr(AsyncSession, "commit", uncertain_commit)
        response = await post_command(auth_client, line_id, body, "transport-key")
    assert response.status_code == 503 and "private connection" not in response.text
    stored = await auth_client.get(f"/api/v1/reconciliation/lines/{line_id}/commands/transport-key")
    assert stored.status_code == (200 if after_commit else 404), stored.text
    row = await db_session.get(BankLine, line_id, populate_existing=True)
    assert (row.status, row.version) == (("ignored", 2) if after_commit else ("unmatched", 1))
    records = list(await db_session.scalars(select(ReconciliationCommand)))
    assert len(records) == int(after_commit)
    retry = await post_command(auth_client, line_id, body, "transport-key")
    assert retry.status_code == 200, retry.text
    if after_commit:
        assert retry.content == stored.content
    assert (await auth_client.get(f"/api/v1/reconciliation/lines/{line_id}")).json()["version"] == 2


@pytest.mark.asyncio
async def test_audit_contains_hashes_not_the_reason_or_bank_source(auth_client, db_session):
    import json

    from app.models.audit import AuditEvent
    from app.models.reconciliation_command import ReconciliationCommand
    from app.services.reconciliation_commands import fingerprint

    line_id = await source_line(auth_client, db_session)
    reason = "Source-owned private explanation"
    response = await post_command(auth_client, line_id, intent("ignore", 1, reason=reason))
    assert response.status_code == 200, response.text
    event = await db_session.scalar(
        select(AuditEvent).where(AuditEvent.action == "reconciliation.command_committed")
    )
    meta = json.loads(event.meta)
    assert meta["reason_hash"] == fingerprint(reason)
    assert meta["payload_hash"] == response.json()["payload_hash"]
    assert meta["result_hash"] == response.json()["result_hash"]
    assert "private" not in event.meta and "Synthetic bank" not in event.meta
    assert not {"command_key", "payload", "reason", "amount", "description"}.intersection(meta)
    row = await db_session.scalar(select(ReconciliationCommand))
    assert row.reason == reason


@pytest.mark.asyncio
async def test_own_outcome_wrong_actor_or_line_is_opaque_and_reader_cannot_write(
    auth_client, client, db_session
):
    from app.models.membership import Membership
    from app.models.user import UserRole
    from tests.test_reconciliation import _h, _member

    line_id = await source_line(auth_client, db_session)
    other_line = await source_line(auth_client, db_session)
    result = await post_command(
        auth_client, line_id, intent("ignore", 1, reason="Reviewed"), "owned-key"
    )
    assert result.status_code == 200, result.text
    wrong_line = await auth_client.get(
        f"/api/v1/reconciliation/lines/{other_line}/commands/owned-key"
    )
    assert wrong_line.status_code == 404
    reader = await _member(auth_client, client, "reader@rn.example", role="user_free")
    hidden = await client.get(
        f"/api/v1/reconciliation/lines/{line_id}/commands/owned-key", headers=_h(reader)
    )
    assert hidden.status_code == 404 and hidden.json() == wrong_line.json()
    history = await client.get(
        f"/api/v1/reconciliation/lines/{line_id}/history", headers=_h(reader)
    )
    assert history.status_code == 200 and len(history.json()["events"]) == 1
    denied = await client.post(
        f"/api/v1/reconciliation/lines/{other_line}/commands",
        headers={**_h(reader), "Idempotency-Key": "owned-key"},
        json=intent("ignore", 1, reason="Reviewed"),
    )
    assert denied.status_code == 403
    # The original actor retains READ after a role downgrade and can recover its
    # own historical result; replaying through POST still needs WRITE.
    row = await db_session.scalar(
        select(Membership).where(Membership.user_id == result.json()["actor_user_id"])
    )
    row.role = UserRole.user_free
    await db_session.commit()
    own = await auth_client.get(f"/api/v1/reconciliation/lines/{line_id}/commands/owned-key")
    assert own.status_code == 200 and own.content == result.content
    assert (
        await post_command(
            auth_client, line_id, intent("ignore", 1, reason="Reviewed"), "owned-key"
        )
    ).status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        intent("restore", 1),
        intent("unmatch", 1, state="ignored"),
        intent("ignore", 1, reason="Valid", target={"kind": "receipt", "id": str(uuid4())}),
        intent("match", 1),
        {**intent("ignore", 1, reason="Valid"), "unexpected": True},
        intent("ignore", 1, state="matched", reason="Valid"),
    ],
)
async def test_invalid_intentions_never_infer_a_current_state(auth_client, db_session, body):
    line_id = await source_line(auth_client, db_session)
    result = await post_command(auth_client, line_id, body)
    assert result.status_code == 422 and result.json()["code"] == "reconciliation_command_invalid"
    assert (await auth_client.get(f"/api/v1/reconciliation/lines/{line_id}")).json()["version"] == 1


@pytest.mark.asyncio
async def test_missing_key_is_428_and_new_key_on_finished_state_is_stale(auth_client, db_session):
    from app.models.reconciliation_command import ReconciliationCommand

    line_id = await source_line(auth_client, db_session)
    body = intent("ignore", 1, reason="Reviewed")
    missing = await auth_client.post(f"/api/v1/reconciliation/lines/{line_id}/commands", json=body)
    assert missing.status_code == 428
    assert (await post_command(auth_client, line_id, body)).status_code == 200
    stale = await post_command(auth_client, line_id, intent("ignore", 2, reason="Reviewed"))
    assert stale.status_code == 409 and stale.json()["code"] == "reconciliation_line_stale"
    assert len(list(await db_session.scalars(select(ReconciliationCommand)))) == 1


@pytest.mark.asyncio
async def test_actual_unique_key_collision_rolls_back_before_fresh_authorized_lookup(
    auth_client, db_session, monkeypatch
):
    """A missed fast-path read cannot turn a DB uniqueness failure into a second write."""
    from app.models.reconciliation_command import ReconciliationCommand
    from app.services import reconciliation_commands as commands

    first_line = await source_line(auth_client, db_session)
    second_line = await source_line(auth_client, db_session)
    first = await post_command(
        auth_client, first_line, intent("ignore", 1, reason="First source"), "collision-key"
    )
    assert first.status_code == 200
    original_lookup = commands.lookup
    reads = []

    async def miss_once(*args):
        reads.append(len(reads))
        if len(reads) == 1:
            return None
        # This actual query would raise PendingRollbackError if the rejected
        # INSERT's transaction had been reused without rollback.
        return await original_lookup(*args)

    monkeypatch.setattr(commands, "lookup", miss_once)
    conflict = await post_command(
        auth_client, second_line, intent("ignore", 1, reason="Second source"), "collision-key"
    )
    assert (
        conflict.status_code == 409
        and conflict.json()["code"] == "reconciliation_command_payload_conflict"
    ), conflict.text
    assert len(reads) == 2
    second = await db_session.get(BankLine, second_line, populate_existing=True)
    assert (second.status, second.version) == ("unmatched", 1)
    assert len(list(await db_session.scalars(select(ReconciliationCommand)))) == 1
    assert (
        await auth_client.get(f"/api/v1/reconciliation/lines/{first_line}/commands/collision-key")
    ).content == first.content


@pytest.mark.asyncio
async def test_legacy_ignored_baseline_has_no_fabricated_history(auth_client, db_session):
    line_id = await source_line(auth_client, db_session, initial="ignored")
    current = await auth_client.get(f"/api/v1/reconciliation/lines/{line_id}")
    assert current.json()["version"] == 1 and current.json()["status"] == "ignored"
    history = await auth_client.get(f"/api/v1/reconciliation/lines/{line_id}/history")
    assert history.json() == {"events": [], "next_before_version": None}
    result = await post_command(auth_client, line_id, intent("restore", 1, state="ignored"))
    assert result.status_code == 200 and result.json()["reason"] is None
    event = (await auth_client.get(f"/api/v1/reconciliation/lines/{line_id}/history")).json()[
        "events"
    ]
    assert len(event) == 1 and event[0]["operation"] == "restore"
    assert event[0]["before_version"] == 1 and event[0]["before"]["status"] == "ignored"


@pytest.mark.asyncio
async def test_delayed_unmatch_cannot_erase_an_aba_same_visible_association(
    auth_client, db_session
):
    line_id = await source_line(auth_client, db_session)
    target = (
        await auth_client.post(
            "/api/v1/receipts", json={"amount": "10.00", "received_on": "2026-11-06"}
        )
    ).json()["id"]
    match_body = intent("match", 1, target={"kind": "receipt", "id": target})
    assert (await post_command(auth_client, line_id, match_body)).status_code == 200
    old_unmatch = intent("unmatch", 2, state="matched", kind="receipt", target_id=target)
    assert (await post_command(auth_client, line_id, old_unmatch)).status_code == 200
    assert (
        await post_command(auth_client, line_id, {**match_body, "expected_version": 3})
    ).status_code == 200
    before = (await auth_client.get(f"/api/v1/reconciliation/lines/{line_id}")).json()
    delayed = await post_command(auth_client, line_id, old_unmatch, "held-old-unmatch")
    assert delayed.status_code == 409 and delayed.json()["code"] == "reconciliation_line_stale"
    assert (await auth_client.get(f"/api/v1/reconciliation/lines/{line_id}")).json() == before
    assert (
        before["status"] == "matched" and before["matched_id"] == target and before["version"] == 4
    )


@pytest.mark.asyncio
async def test_command_flush_failure_rolls_back_without_logging_source_payload(
    auth_client, db_session, monkeypatch, caplog
):
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.reconciliation_command import ReconciliationCommand

    line_id = await source_line(auth_client, db_session)
    private_reason = "Synthetic source-owned explanation must stay out of SQL error logs"
    body = intent("ignore", 1, reason=private_reason)
    original_flush = AsyncSession.flush

    async def failed_flush(db, *args, **kwargs):
        if any(isinstance(row, ReconciliationCommand) for row in db.new):
            raise OperationalError(
                "INSERT reconciliation_commands",
                {"reason": private_reason},
                RuntimeError("synthetic connection loss"),
            )
        return await original_flush(db, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(AsyncSession, "flush", failed_flush)
        result = await post_command(auth_client, line_id, body, "flush-fault")
    assert result.status_code == 503, result.text
    assert private_reason not in caplog.text
    current = (await auth_client.get(f"/api/v1/reconciliation/lines/{line_id}")).json()
    assert (current["status"], current["version"]) == ("unmatched", 1)
    assert not list(await db_session.scalars(select(ReconciliationCommand)))
    assert (await post_command(auth_client, line_id, body, "flush-fault")).status_code == 200
