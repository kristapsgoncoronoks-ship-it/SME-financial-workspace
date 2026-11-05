"""Characterize identical request writes and the separate export admission rails."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select
from sqlalchemy.exc import SQLAlchemyError

from app.main import app
from app.models import Base
from app.models.archive_export import ArchiveExport
from app.models.audit import AuditEvent
from app.models.email_token import PURPOSE_PASSWORD_RESET
from app.models.job import Job
from app.models.user import User
from app.models.workspace_export import WorkspaceExport
from app.services import (
    archive_export,
    audit,
    documents,
    export_artefacts,
    jobs,
    mailer,
    verification,
    workspace_export,
)


def _service(kind):
    return archive_export if kind == "archive" else workspace_export


def _model(kind):
    return ArchiveExport if kind == "archive" else WorkspaceExport


def _action(kind):
    return (
        audit.A.ARCHIVE_EXPORT_REQUESTED
        if kind == "archive"
        else audit.A.WORKSPACE_EXPORT_REQUESTED
    )


async def _identity(auth_client):
    identity = await auth_client.get("/api/v1/auth/me")
    assert identity.status_code == 200, identity.text
    return identity.json()["user"]


async def _state(factory, org_id):
    async with factory() as db:
        result = {}
        for name in (
            "archive_exports",
            "workspace_exports",
            "jobs",
            "audit_events",
            "auth_tokens",
            "email_messages",
        ):
            table = Base.metadata.tables[name]
            result[name] = [
                dict(row)
                for row in (
                    await db.execute(
                        select(table).where(table.c.org_id == org_id).order_by(table.c.id)
                    )
                ).mappings()
            ]
        return result


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["archive", "workspace"])
async def test_request_preserves_exact_row_job_audit_and_caller_commit(
    auth_client, db_session, monkeypatch, kind
):
    user = await _identity(auth_client)

    async def forbidden(*args, **kwargs):
        pytest.fail("Queueing an export performed worker/artifact/mail I/O")

    monkeypatch.setattr(_service(kind), "build_zip_file", forbidden)
    for name in ("store", "store_file", "load", "load_file"):
        monkeypatch.setattr(documents, name, forbidden)
    monkeypatch.setattr(mailer, "send", forbidden)
    monkeypatch.setattr(verification, "issue_token", forbidden)
    commits = []

    def committed(session):
        if not session.in_nested_transaction():
            commits.append(True)

    event.listen(db_session.sync_session, "after_commit", committed)
    row = await _service(kind).request_for_org(
        db_session,
        user["org_id"],
        email=f"  {user['email'].upper()}  ",
        user_id=user["id"],
    )
    assert commits == [], "Request rails committed the caller-owned transaction"
    assert type(row) is _model(kind)
    assert (row.org_id, row.requested_by_user_id, row.requested_email, row.status) == (
        user["org_id"],
        user["id"],
        user["email"].lower(),
        "queued",
    )
    job = await db_session.get(Job, row.job_id)
    assert (job.org_id, job.kind, job.status) == (user["org_id"], f"{kind}.export", "queued")
    assert json.loads(job.payload_json) == {"export_id": row.id}
    recorded = await db_session.scalar(
        select(AuditEvent).where(AuditEvent.action == _action(kind), AuditEvent.target_id == row.id)
    )
    assert recorded is not None
    assert (recorded.org_id, recorded.target_type, recorded.actor_id, recorded.actor_email) == (
        user["org_id"],
        f"{kind}_export",
        user["id"],
        user["email"].lower(),
    )
    assert json.loads(recorded.meta) == {"to": user["email"].lower()}
    assert (await audit.verify_chain(db_session, user["org_id"])).ok
    await db_session.commit()
    assert commits == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["archive", "workspace"])
@pytest.mark.parametrize("fault", ["enqueue", "audit"])
async def test_request_failure_leaves_outer_transaction_to_roll_back_all_writes(
    auth_client, _db, monkeypatch, kind, fault
):
    user = await _identity(auth_client)
    before = await _state(_db, user["org_id"])
    original = audit.record

    async def enqueue_failure(*args, **kwargs):
        raise SQLAlchemyError("Synthetic export request enqueue failure")

    async def audit_failure(db, action, **kwargs):
        if action == _action(kind):
            raise SQLAlchemyError("Synthetic export request audit failure")
        return await original(db, action, **kwargs)

    if fault == "enqueue":
        monkeypatch.setattr(jobs, "enqueue", enqueue_failure)
    else:
        monkeypatch.setattr(audit, "record", audit_failure)
    with pytest.raises(SQLAlchemyError, match=f"Synthetic export request {fault} failure"):
        async with _db() as caller, caller.begin():
            await _service(kind).request_for_org(
                caller, user["org_id"], email=user["email"], user_id=user["id"]
            )
    assert await _state(_db, user["org_id"]) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["archive", "workspace"])
async def test_ready_coalescing_keeps_the_existing_export_kind_difference(
    auth_client, db_session, _db, kind
):
    user = await _identity(auth_client)
    first = await _service(kind).request_for_org(
        db_session, user["org_id"], email=user["email"], user_id=user["id"]
    )
    first.status = "ready"
    first.ready_at = datetime.now(UTC)
    await db_session.commit()
    first_id = first.id
    before = await _state(_db, user["org_id"])
    second = await _service(kind).request_for_org(
        db_session, user["org_id"], email=user["email"], user_id=user["id"]
    )
    await db_session.commit()
    after = await _state(_db, user["org_id"])
    if kind == "archive":
        assert second.id == first_id and second.status == "ready"
        assert after == before
    else:
        assert second.id != first_id and second.status == "queued"
        assert len(after["workspace_exports"]) == len(before["workspace_exports"]) + 1
        assert len(after["jobs"]) == len(before["jobs"]) + 1
        assert len(after["audit_events"]) == len(before["audit_events"]) + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["archive", "workspace"])
async def test_missing_audit_result_preserves_current_request_semantics(
    auth_client, db_session, monkeypatch, kind
):
    user = await _identity(auth_client)
    original = audit.record

    async def absent(db, action, **kwargs):
        return None if action == _action(kind) else await original(db, action, **kwargs)

    monkeypatch.setattr(audit, "record", absent)
    row = await _service(kind).request_for_org(
        db_session, user["org_id"], email=user["email"], user_id=user["id"]
    )
    await db_session.commit()
    await db_session.refresh(row)
    assert row.status == "queued" and row.job_id is not None
    assert (await db_session.get(Job, row.job_id)).kind == f"{kind}.export"
    assert (
        await db_session.scalar(
            select(AuditEvent.id).where(
                AuditEvent.action == _action(kind), AuditEvent.target_id == row.id
            )
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["archive", "workspace"])
@pytest.mark.parametrize("refusal", ["expired", "wrong_purpose"])
async def test_public_download_refuses_expired_or_wrong_purpose_before_artifact_io(
    auth_client, db_session, _db, monkeypatch, kind, refusal
):
    identity = await _identity(auth_client)
    user = await db_session.get(User, identity["id"])
    raw, token = await verification.issue_token(
        db_session,
        user,
        PURPOSE_PASSWORD_RESET if refusal == "wrong_purpose" else f"{kind}_export",
        timedelta(seconds=-1) if refusal == "expired" else timedelta(days=7),
    )
    values = dict(
        org_id=user.org_id,
        requested_email=user.email,
        requested_by_user_id=user.id,
        status="ready",
        sha256="a" * 64,
        size=32,
        ready_at=datetime.now(UTC),
        link_expires_at=datetime.now(UTC) + timedelta(days=7),
    )
    # Change only the credential's purpose/expiry: its otherwise matching row
    # ensures a weakened purpose check cannot hide behind a token-id miss.
    if kind == "workspace":
        values["download_token_id"] = token.id
    db_session.add(_model(kind)(**values))
    await db_session.commit()
    before = await _state(_db, user.org_id)

    async def forbidden(*args, **kwargs):
        pytest.fail("An expired or wrong-purpose export token reached artifact I/O")

    monkeypatch.setattr(export_artefacts, "prepare_file", forbidden)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as public:
        response = await public.get(f"/api/v1/{kind}/export/download/{raw}")
    assert response.status_code == 404, response.text
    assert response.json() == {"detail": "Invalid, used or expired export link"}
    assert "content-disposition" not in response.headers
    assert await _state(_db, user.org_id) == before
