"""Public export redemption is exclusive and rollback preserves the same link (PG)."""

from __future__ import annotations

import asyncio
import contextvars
import io
import os
import time
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core import storage
from app.core.database import get_session
from app.main import app
from app.models.archive_export import ArchiveExport
from app.models.audit import AuditEvent
from app.models.email_token import EmailToken
from app.models.organization import Organization
from app.models.user import User, UserRole
from app.models.workspace_export import WorkspaceExport
from app.services import audit, documents, verification

PG_URL = os.environ.get("RLS_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not PG_URL, reason="requires migrated non-superuser PostgreSQL")
_request_label = contextvars.ContextVar("export_redemption_test_request", default=None)


class _CommitRefused(RuntimeError):
    pass


class _RequestSession(AsyncSession):
    async def commit(self):
        if self.info.pop("refuse_export_commit", False):
            raise _CommitRefused("Synthetic final export commit refused")
        return await super().commit()


class _Objects(storage.MemoryStorage):
    def __init__(self):
        super().__init__()
        self.key = None
        self.fault = None
        self.reads = []

    def get(self, key, *, max_bytes=None):
        label = _request_label.get()
        if key == self.key:
            self.reads.append(label)
            if label == "winner" and self.fault == "read":
                raise storage.StorageReadError("Synthetic export read unavailable")
            if label == "winner" and self.fault in {"missing", "corrupt"}:
                original = self._data[key]
                if self.fault == "missing":
                    self.delete(key)
                else:
                    self.put(key, b"Synthetic corrupted ZIP bytes")
                try:
                    return super().get(key, max_bytes=max_bytes)
                finally:
                    self.put(key, original)
        return super().get(key, max_bytes=max_bytes)


class _Redemption:
    def __init__(self, factory, client, objects):
        self.factory = factory
        self.client = client
        self.objects = objects
        self.pids = {}
        self.pid_ready = {label: asyncio.Event() for label in ("winner", "contender")}
        self.first_at_storage = asyncio.Event()
        self.release_first = asyncio.Event()
        self.storage_entries = []
        self.commit_failure = False
        self.staged_paths = []

    async def seed(self, kind, *, second_token=False):
        self.kind = kind
        self.model = WorkspaceExport if kind == "workspace" else ArchiveExport
        self.action = (
            audit.A.WORKSPACE_EXPORT_DOWNLOADED
            if kind == "workspace"
            else audit.A.ARCHIVE_EXPORT_DOWNLOADED
        )
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("fixture.txt", "Synthetic ready export")
        self.data = output.getvalue()
        async with self.factory() as db:
            org = Organization(name="Synthetic export redemption")
            db.add(org)
            await db.flush()
            user = User(
                org_id=org.id,
                email=f"redeem-{uuid4().hex}@example.com",
                name="Synthetic owner",
                hashed_password="synthetic-not-a-login-hash",
                role=UserRole.owner,
            )
            db.add(user)
            await db.flush()
            self.raw, token = await verification.issue_token(
                db, user, f"{kind}_export", timedelta(hours=1)
            )
            self.token_id = token.id
            self.second_raw = self.raw
            self.second_token_id = token.id
            if second_token:
                self.second_raw, another = await verification.issue_token(
                    db, user, f"{kind}_export", timedelta(hours=1)
                )
                self.second_token_id = another.id
            self.sha, self.size = await documents.store(
                documents.EXPORTS, org.id, self.data, "application/zip", db=db
            )
            now = datetime.now(UTC)
            values = dict(
                org_id=org.id,
                requested_email=user.email,
                requested_by_user_id=user.id,
                status="ready",
                sha256=self.sha,
                size=self.size,
                ready_at=now,
                link_expires_at=now + timedelta(hours=1),
                missing_documents=0,
            )
            if kind == "workspace":
                values.update(download_token_id=token.id, rows=0, tables=0, documents=0)
            else:
                values["records"] = 0
            exported = self.model(**values)
            db.add(exported)
            await db.commit()
            self.org_id, self.export_id = org.id, exported.id
        self.objects.key = storage.content_key(documents.EXPORTS, self.org_id, self.sha)

    async def request(self, label, raw=None):
        marker = _request_label.set(label)
        try:
            return await self.client.get(f"/api/v1/{self.kind}/export/download/{raw or self.raw}")
        finally:
            _request_label.reset(marker)

    async def contender_wait(self):
        await asyncio.wait_for(self.pid_ready["contender"].wait(), timeout=10)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if "contender" in self.storage_entries:
                return False
            async with self.factory() as db:
                row = (
                    await db.execute(
                        text(
                            "SELECT wait_event_type, pg_blocking_pids(pid) "
                            "FROM pg_stat_activity WHERE pid=:pid"
                        ),
                        {"pid": self.pids["contender"]},
                    )
                ).one()
            if row[0] == "Lock" and self.pids["winner"] in row[1]:
                return True
            await asyncio.sleep(0.01)
        raise AssertionError("Contender neither waited on the winner nor reached storage")

    async def race(self, *, distinct_token=False):
        first = asyncio.create_task(self.request("winner"))
        tasks = [first]
        try:
            await asyncio.wait_for(self.first_at_storage.wait(), timeout=10)
            second = asyncio.create_task(
                self.request("contender", self.second_raw if distinct_token else None)
            )
            tasks.append(second)
            waited = await self.contender_wait()
            async with self.factory() as db:
                token = await db.get(EmailToken, self.token_id)
                exported = await db.get(self.model, self.export_id)
                assert token.used_at is None
                assert exported.status == "ready" and exported.downloaded_at is None
            self.release_first.set()
            responses = await asyncio.wait_for(asyncio.gather(*tasks), timeout=15)
            assert self.pids["winner"] != self.pids["contender"]
            assert all(not path.exists() for path in self.staged_paths)
            return responses, waited
        finally:
            self.release_first.set()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def assert_committed_once(self):
        async with self.factory() as db:
            token = await db.get(EmailToken, self.token_id)
            exported = await db.get(self.model, self.export_id)
            assert token.used_at is not None
            assert exported.status == "downloaded" and exported.downloaded_at is not None
            assert (exported.sha256, exported.size) == (self.sha, self.size)
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(AuditEvent)
                    .where(
                        AuditEvent.org_id == self.org_id,
                        AuditEvent.target_id == self.export_id,
                        AuditEvent.action == self.action,
                    )
                )
                == 1
            )
            assert (await audit.verify_chain(db, self.org_id)).ok


@pytest_asyncio.fixture
async def redemption_pg(monkeypatch):
    engine = create_async_engine(PG_URL, pool_size=6, max_overflow=0)
    factory = async_sessionmaker(
        engine, class_=_RequestSession, expire_on_commit=False, autoflush=False
    )
    objects = _Objects()
    storage.set_storage(objects)
    overrides = dict(app.dependency_overrides)
    original_load = documents.load_file
    harness = None

    async def session():
        async with factory() as db:
            label = _request_label.get()
            if label in harness.pid_ready:
                harness.pids[label] = await db.scalar(text("SELECT pg_backend_pid()"))
                harness.pid_ready[label].set()
                db.info["refuse_export_commit"] = harness.commit_failure and label == "winner"
            yield db

    async def held_load(prefix, org_id, sha, **kwargs):
        if (prefix, org_id, sha) == (documents.EXPORTS, harness.org_id, harness.sha):
            label = _request_label.get()
            harness.storage_entries.append(label)
            if label == "winner":
                harness.first_at_storage.set()
            if label in {"winner", "contender"}:
                await asyncio.wait_for(harness.release_first.wait(), timeout=15)
        staged = await original_load(prefix, org_id, sha, **kwargs)
        if staged is not None:
            assert staged.path.stat().st_mode & 0o077 == 0
            harness.staged_paths.append(Path(staged.path))
        return staged

    try:
        async with factory() as db:
            assert (
                await db.execute(
                    text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user")
                )
            ).one() == (False, False)
            rows = (
                await db.execute(
                    text(
                        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE relname IN ('auth_tokens','archive_exports','workspace_exports')"
                    )
                )
            ).all()
            assert len(rows) == 3 and all(enabled and forced for enabled, forced in rows)
        app.dependency_overrides[get_session] = session
        monkeypatch.setattr(documents, "load_file", held_load)
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
        ) as client:
            harness = _Redemption(factory, client, objects)
            yield harness
    finally:
        if harness is not None:
            harness.release_first.set()
            assert all(not path.exists() for path in harness.staged_paths)
        app.dependency_overrides.clear()
        app.dependency_overrides.update(overrides)
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["workspace", "archive"])
async def test_same_export_token_redeems_once_across_two_http_transactions(redemption_pg, kind):
    h = redemption_pg
    await h.seed(kind)
    responses, waited = await h.race()
    assert [response.status_code for response in responses] == [200, 404]
    assert waited and h.storage_entries == ["winner"] and h.objects.reads == ["winner"]
    assert responses[0].content == h.data
    assert "content-disposition" not in responses[1].headers
    await h.assert_committed_once()
    assert (await h.request("later")).status_code == 404
    assert h.objects.reads == ["winner"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["workspace", "archive"])
@pytest.mark.parametrize("fault", ["missing", "read", "corrupt", "commit"])
async def test_waiting_redemption_recovers_when_winner_transaction_rolls_back(
    redemption_pg, kind, fault
):
    h = redemption_pg
    await h.seed(kind)
    h.objects.fault = fault
    h.commit_failure = fault == "commit"
    responses, waited = await h.race()
    expected = {"missing": 404, "read": 503, "corrupt": 503, "commit": 500}[fault]
    assert [response.status_code for response in responses] == [expected, 200]
    assert waited and h.objects.reads == ["winner", "contender"]
    assert responses[1].content == h.data
    assert "content-disposition" not in responses[0].headers
    await h.assert_committed_once()
    assert (await h.request("later")).status_code == 404


@pytest.mark.asyncio
async def test_two_archive_tokens_cannot_both_redeem_one_ready_artifact(redemption_pg):
    h = redemption_pg
    await h.seed("archive", second_token=True)
    responses, waited = await h.race(distinct_token=True)
    assert [response.status_code for response in responses] == [200, 404]
    assert waited and h.objects.reads == ["winner"]
    await h.assert_committed_once()
    async with h.factory() as db:
        assert (await db.get(EmailToken, h.second_token_id)).used_at is None


@pytest.mark.asyncio
async def test_archive_redemption_locks_only_newest_ready_artifact_and_keeps_older_download(
    redemption_pg,
):
    h = redemption_pg
    await h.seed("archive", second_token=True)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("fixture.txt", "Synthetic older archive with distinct bytes")
    older_data = output.getvalue()
    assert older_data != h.data
    async with h.factory() as db:
        newest = await db.get(ArchiveExport, h.export_id)
        older_sha, older_size = await documents.store(
            documents.EXPORTS, h.org_id, older_data, "application/zip", db=db
        )
        older = ArchiveExport(
            org_id=h.org_id,
            requested_email=newest.requested_email,
            requested_by_user_id=newest.requested_by_user_id,
            status="ready",
            sha256=older_sha,
            size=older_size,
            ready_at=newest.ready_at - timedelta(hours=1),
            link_expires_at=newest.link_expires_at,
            records=0,
            missing_documents=0,
        )
        db.add(older)
        await db.commit()
        older_id = older.id

    first = asyncio.create_task(h.request("winner"))
    try:
        await asyncio.wait_for(h.first_at_storage.wait(), timeout=10)
        # LIMIT 1 matters: FOR UPDATE without it locks every ready archive,
        # even though scalar() exposes only the newest row to the application.
        async with h.factory() as observer:
            independent = await asyncio.wait_for(
                observer.scalar(
                    select(ArchiveExport)
                    .where(ArchiveExport.id == older_id, ArchiveExport.org_id == h.org_id)
                    .with_for_update(nowait=True)
                ),
                timeout=5,
            )
            assert independent is not None and independent.status == "ready"
            await observer.rollback()
        h.release_first.set()
        newest_response = await asyncio.wait_for(first, timeout=10)
    finally:
        h.release_first.set()
        await asyncio.gather(first, return_exceptions=True)
    assert newest_response.status_code == 200 and newest_response.content == h.data
    assert h.objects.reads == ["winner"]
    await h.assert_committed_once()

    # Preserve the existing archive policy: another unused token may redeem
    # the remaining ready artifact; this is not token-to-artifact rebinding.
    h.objects.key = storage.content_key(documents.EXPORTS, h.org_id, older_sha)
    older_response = await h.request("later", h.second_raw)
    assert older_response.status_code == 200 and older_response.content == older_data
    assert h.objects.reads == ["winner", "later"]
    async with h.factory() as db:
        assert (await db.get(EmailToken, h.second_token_id)).used_at is not None
        assert (await db.get(ArchiveExport, older_id)).status == "downloaded"
        assert (
            await db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(
                    AuditEvent.org_id == h.org_id,
                    AuditEvent.target_id == older_id,
                    AuditEvent.action == h.action,
                )
            )
            == 1
        )
        assert (await audit.verify_chain(db, h.org_id)).ok
