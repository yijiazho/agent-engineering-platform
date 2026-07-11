import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aep.github_events import GitHubEventValidationError, normalize_github_issue_created


FIXTURE = Path(__file__).parents[1] / "fixtures" / "github" / "issue-created.json"
RECEIVED_AT = datetime(2026, 7, 11, 16, 1, 2, tzinfo=timezone.utc)


def load_payload() -> dict[str, object]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_normalizes_real_shaped_github_issue_created_payload() -> None:
    event = normalize_github_issue_created(
        load_payload(), delivery_id="72d3162e-cc78-11ea-9b6a-0242ac120002", received_at=RECEIVED_AT
    )

    assert set(event) == {
        "id", "source", "type", "repository", "issue", "sender", "receivedAt", "deduplicationKey"
    }
    assert event["source"] == "github"
    assert event["type"] == "github.issue.created"
    assert event["repository"]["full_name"] == "octo-org/octo-repo"
    assert event["issue"]["number"] == 1347
    assert event["sender"]["login"] == "octocat"
    assert event["receivedAt"] == "2026-07-11T16:01:02Z"


def test_duplicate_deliveries_have_the_same_deduplication_key() -> None:
    payload = load_payload()
    first = normalize_github_issue_created(payload, delivery_id="delivery-123", received_at=RECEIVED_AT)
    second = normalize_github_issue_created(
        payload,
        delivery_id="delivery-123",
        received_at=datetime(2026, 7, 11, 17, 0, tzinfo=timezone.utc),
    )

    assert first["deduplicationKey"] == second["deduplicationKey"]
    assert first["id"] == second["id"]


def test_invalid_payload_reports_structured_errors() -> None:
    payload = load_payload()
    payload["action"] = "edited"
    payload["issue"] = {"number": "1347"}

    with pytest.raises(GitHubEventValidationError) as caught:
        normalize_github_issue_created(payload, delivery_id="", received_at=RECEIVED_AT)

    details = caught.value.as_dict()
    assert details["code"] == "invalid_github_event"
    assert {error["field"] for error in details["errors"]} >= {
        "delivery_id", "action", "issue.id", "issue.number", "issue.title"
    }


def test_naive_received_timestamp_is_rejected() -> None:
    with pytest.raises(GitHubEventValidationError) as caught:
        normalize_github_issue_created(
            load_payload(), delivery_id="delivery-123", received_at=datetime(2026, 7, 11)
        )

    assert caught.value.issues[0].field == "received_at"


def test_boolean_values_are_rejected_for_integer_fields() -> None:
    payload = load_payload()
    payload["repository"]["id"] = False
    payload["issue"]["number"] = True
    payload["sender"]["id"] = True

    with pytest.raises(GitHubEventValidationError) as caught:
        normalize_github_issue_created(
            payload, delivery_id="delivery-123", received_at=RECEIVED_AT
        )

    assert {issue.field for issue in caught.value.issues} == {
        "repository.id",
        "issue.number",
        "sender.id",
    }
