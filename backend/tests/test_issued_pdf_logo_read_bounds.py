"""The native invoice renderer sees only verified, metadata-bounded logo bytes."""

import io
from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfReader
from sqlalchemy import select

from app.core import storage
from app.models.issuer import IssuerProfile
from app.services import documents, invoice_pdf
from tests.test_issued_attachment_read_bounds import ObservedBody
from tests.test_project_profitability import _issuer_ready


async def issued_with_logo(client, db):
    await _issuer_ready(client)
    output = io.BytesIO()
    with Image.new("RGB", (8, 8), "white") as image:
        image.save(output, "PNG")
    data = output.getvalue()
    uploaded = await client.post(
        "/api/v1/issuer/logo", files={"file": ("synthetic-logo.png", data, "image/png")}
    )
    assert uploaded.status_code == 200
    profile = await db.scalar(select(IssuerProfile))
    created = await client.post(
        "/api/v1/issued",
        json={
            "buyer_name": "Synthetic native logo buyer",
            "lines": [
                {
                    "description": "Synthetic native invoice work",
                    "quantity": "1",
                    "unit_price": "10",
                    "vat_rate": "0",
                }
            ],
        },
    )
    assert created.status_code == 201, created.text
    return f"/api/v1/issued/{created.json()['id']}/pdf", profile, data


@pytest.mark.asyncio
async def test_grown_logo_stops_at_admitted_size_before_native_render_then_recovers(
    auth_client, db_session, tmp_path, monkeypatch
):
    store = storage.LocalStorage(str(tmp_path))
    storage.set_storage(store)
    url, profile, original = await issued_with_logo(auth_client, db_session)
    key = storage.content_key(documents.LOGOS, profile.org_id, profile.logo_sha256)
    filename = store._path(key)
    with filename.open("r+b") as stream:
        stream.truncate(16 * 1024 * 1024)
    real_open, render = Path.open, invoice_pdf.build_pdf
    observed = []

    def observe(candidate, *args, **kwargs):
        stream = real_open(candidate, *args, **kwargs)
        if candidate == filename and args == ("rb",):
            stream = ObservedBody(stream)
            observed.append(stream)
        return stream

    def forbidden(*args, **kwargs):
        pytest.fail("Unverified logo bytes reached the native PDF renderer")

    monkeypatch.setattr(Path, "open", observe)
    monkeypatch.setattr(invoice_pdf, "build_pdf", forbidden)
    refused = await auth_client.get(url)
    assert refused.status_code == 503
    assert "integrity" in refused.json()["detail"].lower()
    assert observed[0].consumed == len(original) + 1 and observed[0].source.closed
    assert filename.stat().st_size == 16 * 1024 * 1024
    store.put(key, original)
    monkeypatch.setattr(invoice_pdf, "build_pdf", render)
    recovered = await auth_client.get(url)
    assert recovered.status_code == 200 and recovered.content.startswith(b"%PDF")
    pdf = PdfReader(io.BytesIO(recovered.content))
    assert "Synthetic native logo buyer" in "\n".join(page.extract_text() for page in pdf.pages)
    assert "factur-x.xml" in pdf.attachments
    assert any(page.images for page in pdf.pages)


@pytest.mark.asyncio
async def test_same_size_logo_tampering_is_refused_before_render(
    auth_client, db_session, monkeypatch
):
    url, profile, original = await issued_with_logo(auth_client, db_session)
    key = storage.content_key(documents.LOGOS, profile.org_id, profile.logo_sha256)
    storage.get_storage().put(key, b"x" * len(original))

    def forbidden(*args, **kwargs):
        pytest.fail("Tampered logo reached the native PDF renderer")

    monkeypatch.setattr(invoice_pdf, "build_pdf", forbidden)
    response = await auth_client.get(url)
    assert response.status_code == 503
    assert "restore" in response.json()["detail"].lower()
