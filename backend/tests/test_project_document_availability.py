"""Project downloads distinguish unavailable storage from access and integrity."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core import storage
from app.main import app
from app.models import Base
from app.models.project_link import ProjectDocument
from app.services import documents, project_profit

ORIGINAL = b"Synthetic retained project contract.\n"
UNAVAILABLE = {
    "detail": "Stored document could not be read. Check storage and retry.",
    "code": "storage_read_unavailable",
}


@pytest_asyncio.fixture
async def project_file(auth_client, db_session):
    identity = (await auth_client.get("/api/v1/auth/me")).json()
    project = await auth_client.post(
        "/api/v1/masters/projects", json={"code": "AVAIL-1", "name": "Synthetic original"}
    )
    assert project.status_code == 201, project.text
    project_id = project.json()["id"]
    response = await auth_client.post(
        f"/api/v1/masters/projects/{project_id}/documents",
        files={"file": ("contract.txt", ORIGINAL, "text/plain")},
    )
    assert response.status_code == 201, response.text
    row = await db_session.get(ProjectDocument, response.json()["id"])
    assert row.kind == "contract"
    return {
        "url": f"/api/v1/masters/projects/{project_id}/documents/{row.id}/download",
        "project": project_id,
        "document": row.id,
        "sha": row.sha256,
        "org": identity["organization"]["id"],
        "key": storage.content_key(project_profit.PROJECT_DOCS, row.org_id, row.sha256),
    }


@pytest_asyncio.fixture
async def download_client(auth_client):
    # Observe the actual HTTP error envelope even for the baseline's unhandled
    # exception. An ordinary ASGITransport rethrows it after emitting HTTP500.
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
        headers={"Authorization": auth_client.headers["Authorization"]},
    ) as client:
        yield client


async def domain_state(db, org):
    result = {}
    for table in sorted(Base.metadata.tables.values(), key=lambda item: item.name):
        if "org_id" in table.c:
            result[table.name] = [
                dict(row)
                for row in (
                    await db.execute(
                        select(table)
                        .where(table.c.org_id == org)
                        .order_by(*table.primary_key.columns)
                    )
                ).mappings()
            ]
    return json.dumps(result, default=str, sort_keys=True)


def availability(response):
    assert response.status_code == 503, response.text
    assert response.json() == UNAVAILABLE
    assert response.headers["cache-control"] == "private, no-store"
    assert "content-disposition" not in response.headers


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["lost-object", "adapter-base", "interrupted"])
async def test_unavailable_project_original_recovers_without_changing_records(
    project_file, download_client, db_session, monkeypatch, caplog, fault
):
    store = storage.get_storage()
    assert isinstance(store, storage.MemoryStorage)
    before = await domain_state(db_session, project_file["org"])
    objects = dict(store._data)
    if fault == "lost-object":
        store.delete(project_file["key"])
    else:
        original_get = store.get

        def failed(key, **kwargs):
            assert key == project_file["key"]
            assert kwargs["max_bytes"] == len(ORIGINAL)
            error = storage.StorageReadError if fault == "interrupted" else storage.StorageError
            raise error(f"synthetic private backend location: {key}")

        monkeypatch.setattr(store, "get", failed)
    # The shared loader still exposes its intentional storage distinctions;
    # only the HTTP download boundary translates plain read availability.
    with pytest.raises(storage.StorageError) as original_error:
        await documents.load_verified_original(
            project_profit.PROJECT_DOCS,
            project_file["org"],
            project_file["sha"],
            size=len(ORIGINAL),
        )
    assert type(original_error.value) is (
        storage.StorageReadError if fault == "interrupted" else storage.StorageError
    )
    response = await download_client.get(project_file["url"])
    availability(response)
    assert project_file["key"] not in response.text
    assert project_file["key"] not in caplog.text
    assert "synthetic private backend" not in response.text
    assert await domain_state(db_session, project_file["org"]) == before
    if fault == "lost-object":
        store.put(project_file["key"], ORIGINAL)
    else:
        monkeypatch.setattr(store, "get", original_get)
    recovered = await download_client.get(project_file["url"])
    assert recovered.status_code == 200 and recovered.content == ORIGINAL
    assert recovered.headers["x-content-type-options"] == "nosniff"
    assert "attachment" in recovered.headers["content-disposition"]
    assert await domain_state(db_session, project_file["org"]) == before
    assert store._data == objects


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", [b"x" * len(ORIGINAL), b"short", ORIGINAL + b"oversize"])
async def test_digest_truncation_and_size_failures_keep_integrity_response(
    project_file, download_client, db_session, replacement
):
    before = await domain_state(db_session, project_file["org"])
    storage.get_storage().put(project_file["key"], replacement)
    response = await download_client.get(project_file["url"])
    assert response.status_code == 503, response.text
    assert response.json()["code"] == "original_integrity_unverified"
    assert "Restore the original" in response.json()["detail"]
    assert response.headers["cache-control"] == "private, no-store"
    assert "content-disposition" not in response.headers
    assert await domain_state(db_session, project_file["org"]) == before


class UnknownStorageFault(storage.StorageError):
    """A new unsupported adapter category is not silently classified as missing."""


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError, storage.StorageWriteError, UnknownStorageFault])
async def test_unexpected_errors_are_not_disguised_as_retryable_reads(
    project_file, download_client, db_session, monkeypatch, error
):
    before = await domain_state(db_session, project_file["org"])
    store = storage.get_storage()

    def broken(*args, **kwargs):
        raise error("Synthetic unsupported adapter invariant")

    monkeypatch.setattr(store, "get", broken)
    response = await download_client.get(project_file["url"])
    assert response.status_code == 500, response.text
    assert response.json() == {"detail": "Internal server error", "code": "internal_error"}
    assert await domain_state(db_session, project_file["org"]) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["document", "project"])
async def test_missing_metadata_stays_opaque_not_a_storage_availability_failure(
    project_file, download_client, monkeypatch, missing
):
    calls = []

    def forbidden_read(*args, **kwargs):
        calls.append(args)
        raise AssertionError("Missing metadata must not touch object storage")

    monkeypatch.setattr(storage.get_storage(), "get", forbidden_read)
    project = str(uuid4()) if missing == "project" else project_file["project"]
    document = str(uuid4()) if missing == "document" else project_file["document"]
    response = await download_client.get(
        f"/api/v1/masters/projects/{project}/documents/{document}/download"
    )
    assert response.status_code == 404, response.text
    assert "content-disposition" not in response.headers and calls == []
    assert project_file["key"] not in response.text


@pytest.mark.asyncio
async def test_foreign_owner_and_anonymous_do_not_read_storage(
    project_file, download_client, role_client, monkeypatch
):
    foreign = await role_client("owner")
    calls = []

    def forbidden_read(*args, **kwargs):
        calls.append(args)
        raise AssertionError("Unrelated actors must not touch object storage")

    monkeypatch.setattr(storage.get_storage(), "get", forbidden_read)
    denied = await download_client.get(
        project_file["url"], headers={"Authorization": foreign.headers["Authorization"]}
    )
    assert denied.status_code == 404, denied.text
    anonymous = await download_client.get(project_file["url"], headers={"Authorization": ""})
    assert anonymous.status_code == 401, anonymous.text
    assert calls == []


@pytest.mark.asyncio
async def test_same_org_reader_keeps_read_retry_and_write_permission_split(
    project_file, auth_client, download_client, db_session
):
    invitation = await auth_client.post(
        "/api/v1/team/invites", json={"email": "project-reader@example.com", "role": "user_free"}
    )
    assert invitation.status_code == 201, invitation.text
    accepted = await auth_client.post(
        "/api/v1/auth/accept-invite",
        json={
            "token": invitation.json()["token"],
            "name": "Reader",
            "password": "synthetic-reader-password",
        },
    )
    assert accepted.status_code == 201, accepted.text
    headers = {"Authorization": "Bearer " + accepted.json()["token"]["access_token"]}
    store = storage.get_storage()
    store.delete(project_file["key"])
    before = await domain_state(db_session, project_file["org"])
    availability(await download_client.get(project_file["url"], headers=headers))
    store.put(project_file["key"], ORIGINAL)
    response = await download_client.get(project_file["url"], headers=headers)
    assert response.status_code == 200 and response.content == ORIGINAL
    # INVOICE_READ grants every supported business role this download. The
    # reader still lacks the separate INVOICE_WRITE upload capability.
    denied = await download_client.post(
        f"/api/v1/masters/projects/{project_file['project']}/documents",
        headers=headers,
        files={"file": ("new.txt", b"Must not publish", "text/plain")},
    )
    assert denied.status_code == 403, denied.text
    assert await domain_state(db_session, project_file["org"]) == before


@pytest.mark.asyncio
async def test_post_read_access_refusal_remains_403(project_file, download_client, monkeypatch):
    from fastapi import HTTPException

    from app.api.routes import costing

    async def revoked(*args, **kwargs):
        raise HTTPException(403, "Synthetic fresh-access refusal")

    monkeypatch.setattr(costing, "refresh_current_identity", revoked)
    response = await download_client.get(project_file["url"])
    assert (
        response.status_code == 403
        and response.json()["detail"] == "Synthetic fresh-access refusal"
    )
    assert "content-disposition" not in response.headers
