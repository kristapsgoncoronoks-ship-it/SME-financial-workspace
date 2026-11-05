"""Erasure reports disclose retained snapshots without changing their lifecycle."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models.archive_export import ArchiveExport
from app.models.audit import AuditEvent
from app.models.organization import Organization
from app.models.user import User
from app.models.workspace_export import WorkspaceExport
from app.services import documents, privacy, retention


async def seed(db):
    user = await db.scalar(select(User))
    expires = datetime(2026, 11, 20, tzinfo=UTC)
    content = b"snapshot containing personal information"
    sha, size = await documents.store(documents.EXPORTS, user.org_id, content, "application/zip")
    common = dict(
        org_id=user.org_id,
        requested_email="another-owner@example.test",
        requested_by_user_id=user.id,
        sha256=sha,
        size=size,
    )
    rows = [
        WorkspaceExport(**common, status="ready", link_expires_at=expires),
        ArchiveExport(
            **common,
            status="downloaded",
            link_expires_at=expires,
            downloaded_at=expires - timedelta(days=3),
        ),
        WorkspaceExport(**common, status="failed"),
        ArchiveExport(**common, status="ready", purged_at=expires),
        WorkspaceExport(
            org_id=user.org_id,
            requested_email=user.email,
            requested_by_user_id=user.id,
            status="queued",
        ),
    ]
    db.add_all(rows)
    other = Organization(name="Unrelated workspace")
    db.add(other)
    await db.flush()
    db.add(WorkspaceExport(**{**common, "org_id": other.id}, status="ready"))
    await db.commit()
    return user, rows, sha, content


@pytest.mark.asyncio
async def test_preview_reports_all_potential_snapshots_and_existing_deadlines(
    auth_client, db_session
):
    user, rows, sha, content = await seed(db_session)
    response = await auth_client.post("/api/v1/privacy/erasure/preview", json={"email": user.email})
    assert response.status_code == 200
    location = next(row for row in response.json()["locations"] if row["key"] == "export_artifacts")
    assert location["action"] == "retain"
    assert location["matched"] == 3
    assert "potentially" in location["reason"]
    assert "2026-11-18T00:00:00+00:00" in location["reason"]
    assert "2026-11-20T00:00:00+00:00" in location["reason"]
    assert "1" in location["reason"] and "unknown" in location["reason"]
    assert "not confirmation of deletion" in location["reason"]
    assert await documents.load(documents.EXPORTS, user.org_id, sha) == content
    await db_session.refresh(rows[0])
    assert rows[0].purged_at is None


@pytest.mark.asyncio
async def test_erasure_retains_export_bytes_and_audits_only_the_count(auth_client, db_session):
    user, rows, sha, content = await seed(db_session)
    email = user.email
    report = await privacy.erase(db_session, user.org_id, email, actor_email=email)
    location = next(row for row in report.locations if row.key == "export_artifacts")
    assert report.executed and location.action == "retain" and location.matched == 3
    assert await documents.load(documents.EXPORTS, user.org_id, sha) == content
    event = await db_session.scalar(
        select(AuditEvent).where(AuditEvent.action == "privacy.erasure")
    )
    metadata = json.loads(event.meta)
    assert metadata["retained"]["export_artifacts"] == 3
    assert email not in event.meta and sha not in event.meta
    assert rows[0].id not in event.meta


@pytest.mark.asyncio
async def test_hold_reports_suspended_purge_instead_of_a_guaranteed_expiry(auth_client, db_session):
    user, rows, sha, content = await seed(db_session)
    await retention.place_hold(db_session, user.org_id, reason="Matter", actor_email=user.email)
    report = await privacy.erase(db_session, user.org_id, user.email, actor_email=user.email)
    location = next(row for row in report.locations if row.key == "export_artifacts")
    assert not report.executed and report.on_hold
    assert location.action == "retain" and location.matched == 3
    assert "legal hold" in location.reason.lower() and "suspended" in location.reason
    assert await documents.load(documents.EXPORTS, user.org_id, sha) == content
