"""Explicit review -> new command -> current read for incumbent financial scenarios.

This is test setup, never an application compatibility adapter. Tests about stale
intentions/replay keep their original body/key and call the command API directly.
The returned successful transition response is the separate current-line GET.
"""

from uuid import uuid4


def intention(line, operation, *, target=None, reason=None):
    expected_status = {
        "match": "unmatched",
        "ignore": "unmatched",
        "unmatch": "matched",
        "restore": "ignored",
    }[operation]
    return {
        "operation": operation,
        "expected_version": line["version"],
        "expected": {
            "status": expected_status,
            "matched_kind": line["matched_kind"] if operation == "unmatch" else None,
            "matched_id": line["matched_id"] if operation == "unmatch" else None,
        },
        "target": target,
        "reason": reason or ("Reviewed synthetic fee" if operation == "ignore" else None),
    }


async def submit(client, line_id, body, key, *, headers=None):
    return await client.post(
        f"/api/v1/reconciliation/lines/{line_id}/commands",
        headers={**(headers or {}), "Idempotency-Key": key},
        json=body,
    )


async def transition(client, line_id, operation, *, target=None, reason=None, headers=None):
    path = f"/api/v1/reconciliation/lines/{line_id}"
    reviewed = await client.get(path, headers=headers)
    if reviewed.status_code != 200:
        return reviewed
    line = reviewed.json()
    body = intention(line, operation, target=target, reason=reason)
    result = await submit(client, line_id, body, f"test-{uuid4().hex}", headers=headers)
    if result.status_code != 200:
        return result
    receipt = result.json()
    assert receipt["status"] == "committed"
    assert receipt["before_version"] == line["version"]
    assert receipt["after_version"] == line["version"] + 1
    current = await client.get(path, headers=headers)
    assert current.status_code == 200, current.text
    assert {
        name: current.json()[name]
        for name in ("id", "statement_id", "version", "status", "matched_kind", "matched_id")
    } == receipt["line"]
    return current
