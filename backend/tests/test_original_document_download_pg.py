"""Original downloads honor access and source changes during actual I/O (PG)."""

import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, func, select, update

from app.models.audit import AuditEvent
from app.models.document import Document
from app.models.membership import Membership
from app.models.module import OrgModule
from app.models.organization import Organization
from app.models.session import Session
from app.models.user import User, UserRole
from app.services import audit, documents
from tests.test_capture_admission_pg import quota_workspace as quota_workspace
from tests.test_original_document_read_bounds import SURFACES, stored_original

pytestmark = pytest.mark.skipif(
    not os.environ.get("RLS_TEST_DATABASE_URL"),
    reason="requires migrated non-superuser PostgreSQL",
)


async def prepare(quota_workspace, surface):
    client, factory, org_id, _ = quota_workspace
    async with factory() as db:
        original = await stored_original(client, db, surface)
    assert original.org == org_id
    return client, factory, original


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("change", ["user", "membership", "session", "organization"])
async def test_revoked_original_access_is_rechecked_after_storage(
    quota_workspace, monkeypatch, surface, change
):
    client, factory, original = await prepare(quota_workspace, surface)
    load = documents.load
    budgets = []

    async def revoke(*args, **kwargs):
        content = await load(*args, **kwargs)
        budgets.append(kwargs.get("max_bytes"))
        model, condition, values = {
            "user": (User, User.id == original.user, {"is_active": False}),
            "membership": (
                Membership,
                Membership.user_id == original.user,
                {"status": "suspended"},
            ),
            "session": (
                Session,
                Session.user_id == original.user,
                {"revoked_at": datetime.now(UTC)},
            ),
            "organization": (
                Organization,
                Organization.id == original.org,
                {"status": "suspended"},
            ),
        }[change]
        async with factory() as db:
            result = await db.execute(update(model).where(condition).values(**values))
            assert result.rowcount == 1
            await db.commit()
        return content

    monkeypatch.setattr(documents, "load", revoke)
    response = await client.get(original.url)
    assert budgets == [original.size]
    assert response.status_code == 401, response.text
    assert response.headers["www-authenticate"] == "Bearer"
    assert "content-disposition" not in response.headers
    assert original.data not in response.content


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", SURFACES)
async def test_role_change_rechecks_actual_existing_download_permission(
    quota_workspace, monkeypatch, surface
):
    client, factory, original = await prepare(quota_workspace, surface)
    load = documents.load

    async def change_role(*args, **kwargs):
        content = await load(*args, **kwargs)
        async with factory() as db:
            await db.execute(
                update(Membership)
                .where(Membership.user_id == original.user)
                .values(role=UserRole.user)
            )
            await db.commit()
        return content

    monkeypatch.setattr(documents, "load", change_role)
    response = await client.get(original.url)
    if surface in {"logo", "archive", "statement"}:
        assert response.status_code == 403
        assert original.data not in response.content
    else:
        # Employee membership retains invoice/own-receipt reads. Freshness must
        # enforce that actual contract rather than invent an owner requirement.
        assert response.status_code == 200 and response.content == original.data


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("change", ["removed", "retargeted"])
async def test_original_owner_is_rechecked_after_storage(
    quota_workspace, monkeypatch, surface, change
):
    client, factory, original = await prepare(quota_workspace, surface)
    load = documents.load

    async def change_source(*args, **kwargs):
        content = await load(*args, **kwargs)
        async with factory() as db:
            if change == "removed":
                await db.execute(delete(original.model).where(original.model.id == original.row_id))
            else:
                field = {
                    "capture": "source_sha256",
                    "archive": "source_sha256",
                    "logo": "logo_sha256",
                    "receipt": "receipt_sha256",
                    "attachment": "sha256",
                    "statement": "size",
                }[surface]
                await db.execute(
                    update(original.model)
                    .where(original.model.id == original.row_id)
                    .values(**{field: original.size + 1 if field == "size" else "0" * 64})
                )
            await db.commit()
        return content

    monkeypatch.setattr(documents, "load", change_source)
    response = await client.get(original.url)
    assert response.status_code == 404, response.text
    assert original.data not in response.content
    assert "content-disposition" not in response.headers
    async with factory() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.org_id == original.org, AuditEvent.action == audit.A.DOC_DOWNLOAD)
            )
            == 0
        )


@pytest.mark.asyncio
async def test_receipt_module_disabled_during_storage_refuses_download(
    quota_workspace, monkeypatch
):
    client, factory, original = await prepare(quota_workspace, "receipt")
    load = documents.load

    async def disable(*args, **kwargs):
        content = await load(*args, **kwargs)
        async with factory() as db:
            await db.execute(
                update(OrgModule)
                .where(OrgModule.org_id == original.org, OrgModule.key == "expenses")
                .values(enabled=False)
            )
            await db.commit()
        return content

    monkeypatch.setattr(documents, "load", disable)
    response = await client.get(original.url)
    assert response.status_code == 403 and original.data not in response.content


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["capture", "archive"])
async def test_original_registry_metadata_is_rechecked_after_storage(
    quota_workspace, monkeypatch, surface
):
    client, factory, original = await prepare(quota_workspace, surface)
    load = documents.load

    async def change_size(*args, **kwargs):
        content = await load(*args, **kwargs)
        async with factory() as db:
            await db.execute(
                update(Document)
                .where(
                    Document.org_id == original.org,
                    Document.kind == original.prefix,
                    Document.sha256 == original.sha,
                )
                .values(size=original.size + 1)
            )
            await db.commit()
        return content

    monkeypatch.setattr(documents, "load", change_size)
    response = await client.get(original.url)
    assert response.status_code == 503
    assert "integrity" in response.json()["detail"].lower()
    assert original.data not in response.content
