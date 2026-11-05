"""Project originals: recorded admission bounds, integrity and fresh access."""

from __future__ import annotations

import asyncio
import io
import os
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core import storage
from app.core.database import get_session
from app.main import app
from app.models.costing import Project
from app.models.customer_portal_token import CustomerPortalToken
from app.models.document import Document
from app.models.membership import Membership
from app.models.project_link import ProjectDocument
from app.services import documents, filesec, project_profit
from tests.test_issued_attachment_read_bounds import ObservedBody
from tests.test_portal import _customer, _link, _project_for, _setup_issuing


@pytest_asyncio.fixture(params=["sqlite", "postgres"])
async def project_http(request, _db):
    engine = None
    if request.param == "postgres":
        url = os.environ.get("RLS_TEST_DATABASE_URL")
        if not url:
            pytest.skip("set RLS_TEST_DATABASE_URL for separate-connection revocation proofs")
        engine = create_async_engine(url, pool_size=3, max_overflow=0)
        sm = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    else:
        sm = _db

    async def sessions():
        async with sm() as db:
            yield db

    previous = app.dependency_overrides.get(get_session)
    app.dependency_overrides[get_session] = sessions
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/v1/auth/register",
                json={
                    "organization_name": "Synthetic project originals",
                    "name": "Owner",
                    "email": f"project-{uuid.uuid4().hex}@example.com",
                    "password": "synthetic-project-password",
                },
            )
            assert response.status_code == 201, response.text
            client.headers["Authorization"] = "Bearer " + response.json()["token"]["access_token"]
            await _setup_issuing(client)
            yield client, sm
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_session, None)
        else:
            app.dependency_overrides[get_session] = previous
        if engine is not None:
            await engine.dispose()


async def _document(project_http, *, data=b"Synthetic project original", portal=False):
    client, sm = project_http
    customer = await _customer(client)
    project = await _project_for(client, customer)
    response = await client.post(
        f"/api/v1/masters/projects/{project}/documents",
        files={"file": ("original.txt", data, "text/plain")},
    )
    assert response.status_code == 201, response.text
    document_id = response.json()["id"]
    async with sm() as db:
        row = await db.get(ProjectDocument, document_id)
        registry = await db.scalar(
            select(Document).where(
                Document.org_id == row.org_id,
                Document.kind == project_profit.PROJECT_DOCS,
                Document.sha256 == row.sha256,
            )
        )
    token = await _link(client, customer)
    assert (
        await client.put(
            f"/api/v1/masters/projects/{project}/documents/{document_id}/share",
            json={"shared": True},
        )
    ).status_code == 200
    url = (
        f"/api/v1/portal/{token}/documents/{document_id}"
        if portal
        else f"/api/v1/masters/projects/{project}/documents/{document_id}/download"
    )
    key = storage.content_key(project_profit.PROJECT_DOCS, row.org_id, row.sha256)
    return row, registry, url, key, token


def _refused(response):
    assert response.status_code == 503, response.text[:200]
    assert response.json()["code"] == "original_integrity_unverified"
    assert "Restore the original" in response.json()["detail"]
    assert "content-disposition" not in response.headers
    assert response.headers["cache-control"] == "private, no-store"


@pytest.mark.asyncio
@pytest.mark.parametrize("portal", [False, True])
async def test_sparse_growth_reads_only_recorded_size_plus_one_and_recovers(
    project_http, tmp_path, monkeypatch, portal
):
    client, _sm = project_http
    store = storage.LocalStorage(str(tmp_path))
    storage.set_storage(store)
    original = b"Synthetic project original"
    _row, registry, url, key, _token = await _document(project_http, data=original, portal=portal)
    path = store._path(key)
    with path.open("r+b") as source:
        source.truncate(32 * 1024 * 1024)
    original_open, observed = Path.open, []

    def observe(candidate, *args, **kwargs):
        source = original_open(candidate, *args, **kwargs)
        if candidate == path and args == ("rb",):
            source = ObservedBody(source)
            observed.append(source)
        return source

    monkeypatch.setattr(Path, "open", observe)
    _refused(await client.get(url))
    assert observed[0].consumed == registry.size + 1
    assert all(0 < size <= 65536 for size in observed[0].requests)
    assert observed[0].source.closed
    store.put(key, original)
    response = await client.get(url)
    assert response.status_code == 200 and response.content == original
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.asyncio
async def test_remote_short_reads_ignore_false_length_and_close(project_http):
    client, _sm = project_http
    original = b"Synthetic project original"
    _row, registry, url, _key, _token = await _document(project_http, data=original)
    body = ObservedBody(io.BytesIO(original + b"more bytes"), short_reads=True)
    store = object.__new__(storage.S3Storage)
    store._bucket, store._prefix = "synthetic", ""
    store._client = SimpleNamespace(get_object=lambda **kwargs: {"Body": body, "ContentLength": 1})
    storage.set_storage(store)
    _refused(await client.get(url))
    assert body.consumed == registry.size + 1 and body.source.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", [b"x" * 26, b"short"])
async def test_corrupt_or_truncated_project_original_is_refused(project_http, replacement):
    client, _sm = project_http
    _row, _registry, url, key, _token = await _document(project_http)
    storage.get_storage().put(key, replacement)
    _refused(await client.get(url))


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_historical_original_survives_lower_upload_policy(project_http, monkeypatch, legacy):
    client, sm = project_http
    monkeypatch.setattr(filesec.settings, "max_upload_mb", 3)
    original = b"x" * (2 * 1024 * 1024)
    row, registry, url, _key, _token = await _document(project_http, data=original)
    if legacy:
        async with sm() as db:
            await db.execute(delete(Document).where(Document.id == registry.id))
            await db.commit()
    monkeypatch.setattr(filesec.settings, "max_upload_mb", 1)
    response = await client.get(url)
    assert response.status_code == 200 and response.content == original
    assert storage.sha256_hex(response.content) == row.sha256


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["membership", "deleted", "shared", "token", "registry", "sha", "customer"]
)
async def test_access_and_source_revocation_during_read_never_returns_bytes(
    project_http, monkeypatch, change
):
    client, sm = project_http
    portal = change in {"shared", "token", "customer"}
    row, registry, url, _key, token = await _document(project_http, portal=portal)
    other_customer = await _customer(client, "Other customer") if change == "customer" else None
    started, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    store = storage.get_storage()
    original_get = store.get

    def blocked(*args, **kwargs):
        loop.call_soon_threadsafe(started.set)
        if not release.wait(timeout=10):
            raise RuntimeError("test storage barrier did not resume")
        return original_get(*args, **kwargs)

    monkeypatch.setattr(store, "get", blocked)
    task = asyncio.create_task(client.get(url))
    try:
        await asyncio.wait_for(started.wait(), timeout=10)
        async with sm() as db:
            if change == "membership":
                await db.execute(
                    update(Membership)
                    .where(Membership.org_id == row.org_id)
                    .values(status="suspended")
                )
            elif change == "deleted":
                await db.execute(delete(ProjectDocument).where(ProjectDocument.id == row.id))
            elif change == "shared":
                await db.execute(
                    update(ProjectDocument)
                    .where(ProjectDocument.id == row.id)
                    .values(shared_with_customer=False)
                )
            elif change == "token":
                await db.execute(
                    update(CustomerPortalToken)
                    .where(CustomerPortalToken.token == token)
                    .values(revoked_at=datetime.now(UTC))
                )
            elif change == "sha":
                await db.execute(
                    update(ProjectDocument)
                    .where(ProjectDocument.id == row.id)
                    .values(sha256="a" * 64)
                )
            elif change == "customer":
                await db.execute(
                    update(Project)
                    .where(Project.id == row.project_id)
                    .values(customer_id=other_customer)
                )
            else:
                await db.execute(
                    update(Document)
                    .where(Document.id == registry.id)
                    .values(size=registry.size + 1)
                )
            await db.commit()
        release.set()
        response = await asyncio.wait_for(task, timeout=10)
        assert response.status_code == (
            401 if change == "membership" else 503 if change == "registry" else 404
        )
        assert b"Synthetic project original" not in response.content
        assert "content-disposition" not in response.headers
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["missing", "wrong_project", "unshared", "token_revoked"])
async def test_invisible_project_original_never_reads_storage(project_http, monkeypatch, change):
    client, sm = project_http
    row, _registry, url, _key, token = await _document(
        project_http, portal=change in {"unshared", "token_revoked"}
    )
    if change == "missing":
        url = url.replace(row.id, str(uuid.uuid4()))
    elif change == "wrong_project":
        url = url.replace(row.project_id, str(uuid.uuid4()))
    else:
        async with sm() as db:
            if change == "unshared":
                await db.execute(
                    update(ProjectDocument)
                    .where(ProjectDocument.id == row.id)
                    .values(shared_with_customer=False)
                )
            else:
                await db.execute(
                    update(CustomerPortalToken)
                    .where(CustomerPortalToken.token == token)
                    .values(revoked_at=datetime.now(UTC))
                )
            await db.commit()

    async def forbidden(*args, **kwargs):
        raise AssertionError("invisible project original reached object storage")

    monkeypatch.setattr(documents, "load_verified_original", forbidden)
    assert (await client.get(url)).status_code == 404
