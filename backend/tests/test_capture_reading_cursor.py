"""Opaque history cursors are exact typed data, never query authority."""

import base64
import json

import pytest

from app.core.errors import ValidationError
from app.services import capture_reading


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", [42, True, None, [], {}])
async def test_invalid_identity_has_typed_cursor_error_without_querying(identity):
    token = (
        base64.urlsafe_b64encode(json.dumps([1, "2026-11-05T00:00:00Z", identity]).encode())
        .decode()
        .rstrip("=")
    )
    with pytest.raises(ValidationError) as caught:
        await capture_reading.history(None, "owner-org", "owner-id", channel="upload", cursor=token)
    assert caught.value.code == "invalid_cursor" and caught.value.status == 422


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "version,stamp",
    [
        (True, "2026-11-05T00:00:00Z"),
        ("1", "2026-11-05T00:00:00Z"),
        (1, True),
        (1, 42),
        (1, None),
        (1, []),
        (1, {}),
        (1, "2026-11-05T00:00:00"),
    ],
)
async def test_cursor_version_and_aware_timestamp_are_exact_types(version, stamp):
    token = (
        base64.urlsafe_b64encode(
            json.dumps([version, stamp, "00000000-0000-0000-0000-000000000001"]).encode()
        )
        .decode()
        .rstrip("=")
    )
    with pytest.raises(ValidationError) as caught:
        await capture_reading.history(None, "owner-org", "owner-id", channel="upload", cursor=token)
    assert caught.value.code == "invalid_cursor" and caught.value.status == 422
