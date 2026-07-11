import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aep.github_events import (
    EventDeduplicator,
    GitHubEventValidationError,
    normalize_github_issue_created,
)
from aep.runtime_store import InMemoryRuntimeObjectStore


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


def test_duplicate_event_is_linked_to_the_first_accepted_event() -> None:
    store = InMemoryRuntimeObjectStore()
    first_controller = EventDeduplicator(store)
    second_controller = EventDeduplicator(store)
    first_event = normalize_github_issue_created(
        load_payload(), delivery_id="delivery-123", received_at=RECEIVED_AT
    )
    duplicate_event = dict(first_event)
    duplicate_event["receivedAt"] = "2026-07-11T17:00:00Z"

    first = first_controller.accept(first_event)
    duplicate = second_controller.accept(duplicate_event)

    assert first.accepted is True
    assert duplicate.accepted is False
    assert duplicate.event == first.event


def test_near_duplicate_and_distinct_events_are_accepted() -> None:
    deduplicator = EventDeduplicator(InMemoryRuntimeObjectStore())
    payload = load_payload()
    near_duplicate = load_payload()
    near_duplicate["issue"]["title"] = "A revised title"

    results = [
        deduplicator.accept(
            normalize_github_issue_created(
                candidate, delivery_id=delivery_id, received_at=RECEIVED_AT
            )
        )
        for candidate, delivery_id in (
            (payload, "delivery-123"),
            (near_duplicate, "delivery-124"),
            (payload, "delivery-125"),
        )
    ]

    assert all(result.accepted for result in results)
    assert len({result.event["id"] for result in results}) == 3


def test_concurrent_duplicates_have_exactly_one_accepted_result() -> None:
    deduplicator = EventDeduplicator(InMemoryRuntimeObjectStore())
    event = normalize_github_issue_created(
        load_payload(), delivery_id="delivery-123", received_at=RECEIVED_AT
    )

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(deduplicator.accept, [event] * 40))

    assert sum(result.accepted for result in results) == 1
    assert {result.event["id"] for result in results} == {event["id"]}


def test_event_without_a_deduplication_key_is_rejected() -> None:
    with pytest.raises(ValueError, match="deduplicationKey"):
        EventDeduplicator(InMemoryRuntimeObjectStore()).accept({"id": "event-123"})


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
