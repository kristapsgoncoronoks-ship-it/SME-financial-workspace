"""Persisted reconciliation versions and tenant-safe historical receipts."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import IntegrityError

from app.models import Base
from tests.test_planning_ocr_migration_integration import _insert

MAX_VERSION = 2147483647


def _insert_command(connection, identity, *, actor=None, **overrides):
    payload = json.dumps({"operation": "ignore", "line_id": identity["line"]}, sort_keys=True)
    result = json.dumps({"status": "committed"}, sort_keys=True)
    values = dict(
        org_id=identity["org"],
        actor_user_id=actor or identity["user"],
        line_id=identity["line"],
        command_key=str(uuid4()),
        contract_version=1,
        operation="ignore",
        payload_json=payload,
        payload_hash=hashlib.sha256(payload.encode()).hexdigest(),
        result_json=result,
        result_hash=hashlib.sha256(result.encode()).hexdigest(),
        before_state_json='{"status":"unmatched"}',
        after_state_json='{"status":"ignored"}',
        expected_version=1,
        before_version=1,
        after_version=2,
        reason="Reviewed statement duplicate",
    )
    values.update(overrides)
    return _insert(connection, "reconciliation_commands", **values)


@pytest.fixture
def command_schema():
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def enable_fk(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    try:
        Base.metadata.create_all(engine)
        with engine.connect() as connection:
            ids = []
            for label in ("A", "B"):
                org, user, statement, line = (str(uuid4()) for _ in range(4))
                _insert(connection, "organizations", id=org, name="Synthetic bank " + label)
                _insert(
                    connection,
                    "users",
                    id=user,
                    org_id=org,
                    email=uuid4().hex + "@example.invalid",
                    name="Synthetic actor",
                    hashed_password="not-a-login",
                )
                _insert(
                    connection,
                    "bank_statements",
                    id=statement,
                    org_id=org,
                    filename="synthetic.csv",
                    sha256=label.lower() * 64,
                )
                _insert(
                    connection,
                    "bank_lines",
                    id=line,
                    org_id=org,
                    statement_id=statement,
                    line_date=date(2026, 11, 6),
                    amount="-12.34",
                    direction="debit",
                    status="unmatched",
                    description="Retained source",
                    balance="98.76",
                )
                ids.append(dict(org=org, user=user, statement=statement, line=line))
            connection.commit()
            yield connection, ids
    finally:
        engine.dispose()


def test_bank_line_persisted_version_is_a_positive_bounded_baseline(command_schema):
    connection, (a, _b) = command_schema
    columns = {column["name"]: column for column in inspect(connection).get_columns("bank_lines")}
    assert "version" in columns, "BankLine needs a persisted version, not a derived state token"
    assert not columns["version"]["nullable"]
    assert (
        connection.scalar(text("SELECT version FROM bank_lines WHERE id=:id"), {"id": a["line"]})
        == 1
    )
    for invalid in (0, -1, MAX_VERSION + 1):
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(
                text("UPDATE bank_lines SET version=:version WHERE id=:id"),
                {"id": a["line"], "version": invalid},
            )
    assert (
        connection.scalar(text("SELECT version FROM bank_lines WHERE id=:id"), {"id": a["line"]})
        == 1
    )


def test_command_receipts_exist_as_tenant_safe_history_not_membership_owned(command_schema):
    connection, ids = command_schema
    assert "reconciliation_commands" in inspect(connection).get_table_names(), (
        "A committed transition needs its own replay/history record"
    )
    columns = {
        column["name"] for column in inspect(connection).get_columns("reconciliation_commands")
    }
    assert (
        "recorded_at" in columns and "committed_at" not in columns and "updated_at" not in columns
    )
    foreign_keys = inspect(connection).get_foreign_keys("reconciliation_commands")
    actor = [fk for fk in foreign_keys if fk["constrained_columns"] == ["actor_user_id"]]
    assert (
        len(actor) == 1
        and actor[0]["referred_table"] == "users"
        and actor[0]["options"]["ondelete"] == "RESTRICT"
    )
    assert all(fk["referred_table"] != "memberships" for fk in foreign_keys)
    assert any(
        fk["constrained_columns"] == ["org_id", "line_id"]
        and fk["referred_columns"] == ["org_id", "id"]
        and fk["referred_table"] == "bank_lines"
        and fk["options"]["ondelete"] == "CASCADE"
        for fk in foreign_keys
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"expected_version": 0},
        {"before_version": -1},
        {"after_version": MAX_VERSION + 1},
        {"expected_version": 1.5},
        {"expected_version": 2},
        {"after_version": 3},
        {"contract_version": 2},
        {"operation": "reverse_cash"},
        {"command_key": ""},
        {"command_key": "x" * 121},
        {"payload_hash": "short"},
        {"result_hash": "short"},
    ],
)
def test_record_shape_and_transition_bounds_refuse_raw_invalid_rows(command_schema, overrides):
    connection, (a, _b) = command_schema
    with pytest.raises(IntegrityError), connection.begin_nested():
        _insert_command(connection, a, **overrides)
    assert connection.scalar(text("SELECT count(*) FROM reconciliation_commands")) == 0


@pytest.mark.parametrize("reason", [None, "", " \t\n\r", "\u00a0\u2007\u3000", "x" * 1001])
def test_ignore_requires_bounded_nonblank_reason_without_inventing_history(command_schema, reason):
    connection, (a, _b) = command_schema
    with pytest.raises(IntegrityError), connection.begin_nested():
        _insert_command(connection, a, reason=reason)
    assert connection.scalar(text("SELECT count(*) FROM reconciliation_commands")) == 0


def test_global_actor_can_record_another_company_but_line_must_match_its_company(command_schema):
    connection, (a, b) = command_schema
    command = _insert_command(connection, b, actor=a["user"])
    assert (
        connection.scalar(
            text("SELECT actor_user_id FROM reconciliation_commands WHERE id=:id"), {"id": command}
        )
        == a["user"]
    )
    with pytest.raises(IntegrityError), connection.begin_nested():
        _insert_command(connection, a, line_id=b["line"])
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(text("DELETE FROM users WHERE id=:id"), {"id": a["user"]})
    # The existing statement/line deletion lifecycle still owns its receipts.
    connection.execute(text("DELETE FROM bank_statements WHERE id=:id"), {"id": b["statement"]})
    assert connection.scalar(text("SELECT count(*) FROM reconciliation_commands")) == 0
    assert (
        connection.scalar(text("SELECT count(*) FROM users WHERE id=:id"), {"id": a["user"]}) == 1
    )


def test_key_scope_and_line_version_are_independent_unique_backstops(command_schema):
    connection, (a, b) = command_schema
    _insert_command(connection, a, command_key="same-intention")
    with pytest.raises(IntegrityError), connection.begin_nested():
        _insert_command(connection, a, actor=b["user"], command_key="other-intention")
    other_line = _insert(
        connection,
        "bank_lines",
        org_id=a["org"],
        statement_id=a["statement"],
        line_date=date(2026, 11, 6),
        amount="12.34",
    )
    with pytest.raises(IntegrityError), connection.begin_nested():
        _insert_command(connection, dict(a, line=other_line), command_key="same-intention")
