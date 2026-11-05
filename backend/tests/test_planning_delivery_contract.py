"""Planning's published response contract describes its existing runtime output."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.api.routes import schedule
from app.core.config import settings
from app.main import app


def response_schema(path, method, code):
    document = app.openapi()
    value = document["paths"][path][method]["responses"][str(code)]["content"]["application/json"][
        "schema"
    ]
    while "$ref" in value:
        value = document["components"]["schemas"][value["$ref"].rsplit("/", 1)[1]]
    return value


def test_calendar_callback_documents_the_browser_redirect_without_a_json_success():
    responses = app.openapi()["paths"]["/api/v1/schedule/calendar-connections/{provider}/callback"][
        "get"
    ]["responses"]
    assert "303" in responses
    assert "200" not in responses
    assert "application/json" not in responses["303"].get("content", {})


def test_delivery_list_and_retry_contracts_describe_concrete_runtime_shapes():
    calendar = response_schema("/api/v1/schedule/calendar-connections", "get", 200)
    assert calendar["properties"]["providers"]["type"] == "array"
    retry = response_schema("/api/v1/schedule/calendar-connections/{provider}/retry", "post", 202)
    assert retry["properties"]["status"]["type"] == "string"
    notices = response_schema(
        "/api/v1/schedule/assignments/{assignment_id}/notifications", "get", 200
    )
    assert notices["properties"]["smtp_configured"]["type"] == "boolean"
    assert notices["properties"]["items"]["type"] == "array"
    notice = response_schema(
        "/api/v1/schedule/assignments/{assignment_id}/notifications/{message_id}/retry",
        "post",
        200,
    )
    assert set(notice["properties"]) == {
        "id",
        "kind",
        "status",
        "error",
        "sent_at",
        "created_at",
        "can_retry",
    }
    assert notice["properties"]["can_retry"]["type"] == "boolean"


def test_typed_notice_preserves_existing_wire_dates_and_future_state_labels(monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", None)
    message = SimpleNamespace(
        id="synthetic-notice",
        kind="future-kind",
        status="future-state",
        error=None,
        sent_at=None,
        created_at=datetime(2026, 11, 5, tzinfo=UTC),
        command_key=None,
    )
    row = SimpleNamespace(id="synthetic-assignment", revision=1, notify_assignee=True)
    wire = schedule._notice_out(message, row)
    assert schedule.AssignmentNotificationOut.model_validate(wire).model_dump() == wire
    assert wire["created_at"] == "2026-11-05T00:00:00+00:00"


@pytest.mark.asyncio
async def test_calendar_list_remains_usable_and_contains_no_credentials(auth_client):
    response = await auth_client.get("/api/v1/schedule/calendar-connections")
    assert response.status_code == 200
    assert {row["provider"] for row in response.json()["providers"]} == {"google", "outlook"}
    assert all(
        set(row) == {"provider", "configured", "reason", "connection"}
        for row in response.json()["providers"]
    )
    assert not any(
        key in response.text for key in ("access_token", "refresh_token", "sealed_tokens")
    )
