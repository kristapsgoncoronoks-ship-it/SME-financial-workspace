"""Real bounded derivatives, evidence preservation and post-decode authority."""

import asyncio
import io
import threading

import pytest
from PIL import Image
from sqlalchemy import delete, select, update

from app.core import storage
from app.models.document import Document
from app.models.membership import Membership
from app.models.project_link import ProjectDocument
from app.services import documents, filesec, project_profit, project_thumbnails
from tests.test_project_document_read_bounds import project_http as project_http


def image_bytes(format="JPEG", size=(640, 480)):
    with Image.new("RGB", size, "navy") as image:
        out = io.BytesIO()
        exif = Image.Exif()
        exif[270] = "Private source evidence"
        exif[274] = 6
        image.save(out, format=format, exif=exif)
        return out.getvalue()


@pytest.mark.parametrize("format", ["JPEG", "PNG", "WEBP"])
def test_real_child_derivative_is_small_oriented_and_metadata_free(format):
    original = image_bytes(format)
    data = project_thumbnails.render(original)
    assert len(data) <= filesec.RESOURCE_BYTES["project_thumbnail"]
    with Image.open(io.BytesIO(data)) as preview:
        assert preview.format == "JPEG"
        assert max(preview.size) <= 320
        assert preview.height > preview.width  # EXIF orientation applied
        assert not preview.getexif()
        assert "icc_profile" not in preview.info
    assert b"Private source evidence" not in data


def test_large_textured_photo_has_measurably_smaller_derivative(record_property):
    # A small WebP can already be smaller than JPEG's fixed overhead. Measure
    # the bandwidth benefit on a large textured photo instead of promising it
    # universally for every accepted input format and geometry.
    with Image.effect_noise((2048, 1536), 48).convert("RGB") as image:
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=92)
        original = output.getvalue()
    derivative = project_thumbnails.render(original)
    record_property("original_bytes", len(original))
    record_property("derivative_bytes", len(derivative))
    assert len(original) > 1024 * 1024
    assert len(derivative) < len(original)
    assert len(derivative) <= filesec.RESOURCE_BYTES["project_thumbnail"]


@pytest.mark.parametrize("content", [b"not an image", b"\x00\x00\x00\x18ftypheic" + b"\x00" * 20])
def test_unsupported_native_preview_is_a_truthful_optional_refusal(content):
    with pytest.raises(project_thumbnails.AppError) as error:
        project_thumbnails.render(content)
    assert error.value.status == 422
    assert error.value.code == "project_photo_preview_unavailable"


def test_highly_compressed_excessive_geometry_is_refused_by_real_child():
    with pytest.raises(project_thumbnails.AppError):
        project_thumbnails.render(image_bytes("PNG", (4000, 3000)))


async def photo(project_http):
    client, sm = project_http
    created = await client.post(
        "/api/v1/masters/projects", json={"code": "PHOTO", "name": "Photo evidence"}
    )
    assert created.status_code == 201, created.text
    project = created.json()["id"]
    original = image_bytes()
    response = await client.post(
        f"/api/v1/masters/projects/{project}/documents?kind=photo",
        files={"file": ("private-photo.jpg", original, "image/jpeg")},
    )
    assert response.status_code == 201, response.text
    document_id = response.json()["id"]
    async with sm() as db:
        row = await db.get(ProjectDocument, document_id)
        registry = await db.scalar(
            select(Document).where(Document.org_id == row.org_id, Document.sha256 == row.sha256)
        )
    base = f"/api/v1/masters/projects/{project}/documents/{document_id}"
    return row, registry, base, original


@pytest.mark.asyncio
async def test_authenticated_thumbnail_preserves_original_and_source_registry(project_http):
    client, _sm = project_http
    row, registry, base, original = await photo(project_http)
    response = await client.get(base + "/thumbnail")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert len(response.content) < len(original)
    assert (await client.get(base + "/download")).content == original
    assert await documents.load(project_profit.PROJECT_DOCS, row.org_id, row.sha256) == original
    assert registry.size == len(original)


@pytest.mark.asyncio
async def test_unknown_historical_size_refuses_preview_before_io_but_original_remains(
    project_http, monkeypatch
):
    client, sm = project_http
    row, registry, base, original = await photo(project_http)
    async with sm() as db:
        await db.execute(delete(Document).where(Document.id == registry.id))
        await db.commit()

    def forbidden(*args, **kwargs):
        pytest.fail("Metadata-unbounded preview must not read source")

    with monkeypatch.context() as patch:
        patch.setattr(storage.get_storage(), "get", forbidden)
        response = await client.get(base + "/thumbnail")
    assert response.status_code == 422
    assert (await client.get(base + "/download")).content == original


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["membership", "source", "registry", "kind"])
async def test_actual_post_render_change_refuses_preview(project_http, monkeypatch, change):
    client, sm = project_http
    row, registry, base, original = await photo(project_http)
    entered, release = threading.Event(), threading.Event()
    renderer = project_thumbnails.render

    def delayed(content):
        entered.set()
        assert release.wait(15), "test did not release decoder"
        return renderer(content)

    monkeypatch.setattr(project_thumbnails, "render", delayed)
    pending = asyncio.create_task(client.get(base + "/thumbnail"))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        async with sm() as db:
            if change == "membership":
                await db.execute(
                    update(Membership)
                    .where(Membership.org_id == row.org_id)
                    .values(status="suspended")
                )
            elif change == "source":
                await db.execute(delete(ProjectDocument).where(ProjectDocument.id == row.id))
            elif change == "kind":
                await db.execute(
                    update(ProjectDocument)
                    .where(ProjectDocument.id == row.id)
                    .values(kind="contract")
                )
            else:
                await db.execute(
                    update(Document)
                    .where(Document.id == registry.id)
                    .values(size=registry.size + 1)
                )
            await db.commit()
    finally:
        release.set()
    response = await pending
    assert response.status_code in (401, 403, 404, 503), response.text[:200]
    assert response.headers.get("content-type") != "image/jpeg"


@pytest.mark.asyncio
async def test_decoder_slots_bound_source_admission_and_release_on_cancellation():
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold():
        async with project_thumbnails._decoder_slot():
            entered.set()
            await release.wait()

    first = asyncio.create_task(hold())
    await entered.wait()
    second = asyncio.create_task(hold())
    await asyncio.sleep(0)
    assert not second.done()
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    release.set()
    await first
    assert not project_thumbnails._locks
