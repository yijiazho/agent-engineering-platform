from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from aep.github_webhook import GitHubWebhookIngress
from aep.webhook_dispatch import DispatchAcceptance


FIXTURE = Path(__file__).parents[1] / "fixtures/github/issue-created.json"
SECRET = b"fixed-test-webhook-secret"
NOW = datetime(2026, 8, 7, 12, 30, tzinfo=timezone.utc)


class RecordingDispatcher:
    def __init__(self, *, failures: int = 0) -> None:
        self.requests: list[tuple[Mapping[str, Any], str]] = []
        self._events: dict[str, Mapping[str, Any]] = {}
        self._failures = failures

    def submit(
        self, event: Mapping[str, Any], *, trace_id: str
    ) -> DispatchAcceptance:
        if self._failures:
            self._failures -= 1
            raise OSError("database unavailable")
        key = str(event["deduplicationKey"])
        prior = self._events.get(key)
        if prior is not None:
            return DispatchAcceptance(False, prior)
        self._events[key] = event
        self.requests.append((event, trace_id))
        return DispatchAcceptance(True, event)


def payload_bytes(
    *, repository: str = "octo-org/octo-repo", action: str = "opened"
) -> bytes:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["repository"]["full_name"] = repository
    payload["action"] = action
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def signed_headers(body: bytes, **overrides: str) -> dict[str, str]:
    headers = {
        "X-Hub-Signature-256": "sha256="
        + hmac.new(SECRET, body, hashlib.sha256).hexdigest(),
        "X-GitHub-Event": "issues",
        "X-GitHub-Delivery": "delivery-038",
    }
    headers.update(overrides)
    return headers


def ingress(
    dispatcher: RecordingDispatcher,
    *,
    evidence: list[Mapping[str, Any]] | None = None,
    max_body_bytes: int = 1_048_576,
) -> GitHubWebhookIngress:
    return GitHubWebhookIngress(
        secret=SECRET,
        repository_owner="octo-org",
        repository_name="octo-repo",
        dispatcher=dispatcher,
        evidence_sink=evidence.append if evidence is not None else None,
        max_body_bytes=max_body_bytes,
        clock=lambda: NOW,
    )


def test_signed_bound_issue_is_normalized_persisted_and_dispatched_once() -> None:
    dispatcher = RecordingDispatcher()
    evidence: list[Mapping[str, Any]] = []
    boundary = ingress(dispatcher, evidence=evidence)
    body = payload_bytes()

    response = boundary.handle(headers=signed_headers(body), raw_body=body)

    assert response.status_code == 202
    assert response.body["status"] == "accepted"
    assert response.body["eventId"].startswith("event-")
    assert len(dispatcher.requests) == 1
    event, trace_id = dispatcher.requests[0]
    assert event["deduplicationKey"] == "github:delivery:delivery-038"
    assert response.body["traceId"] == trace_id
    assert evidence[0]["outcome"] == "accepted"


def test_replay_returns_prior_event_identity_without_redispatch() -> None:
    dispatcher = RecordingDispatcher()
    boundary = ingress(dispatcher)
    body = payload_bytes()

    first = boundary.handle(headers=signed_headers(body), raw_body=body)
    replay = boundary.handle(headers=signed_headers(body), raw_body=body)

    assert replay.status_code == 200
    assert replay.body == {
        "status": "duplicate",
        "eventId": first.body["eventId"],
        "traceId": first.body["traceId"],
    }
    assert len(dispatcher.requests) == 1


@pytest.mark.parametrize(
    ("headers_update", "body", "status", "code"),
    [
        (
            {"X-Hub-Signature-256": "sha256=invalid"},
            payload_bytes(),
            401,
            "invalid_signature",
        ),
        ({"X-GitHub-Delivery": ""}, payload_bytes(), 400, "missing_delivery_id"),
        ({"X-GitHub-Event": "push"}, payload_bytes(), 422, "unsupported_event"),
        ({}, payload_bytes(action="edited"), 422, "unsupported_action"),
        ({}, payload_bytes(repository="someone/else"), 422, "repository_mismatch"),
        ({}, b"{not-json", 400, "malformed_json"),
    ],
)
def test_unsupported_deliveries_are_rejected_before_dispatch(
    headers_update: dict[str, str], body: bytes, status: int, code: str
) -> None:
    dispatcher = RecordingDispatcher()
    boundary = ingress(dispatcher)
    headers = signed_headers(body, **headers_update)

    response = boundary.handle(headers=headers, raw_body=body)

    assert (response.status_code, response.body["code"]) == (status, code)
    assert dispatcher.requests == []


def test_oversized_signed_body_is_rejected() -> None:
    dispatcher = RecordingDispatcher()
    body = b"12345"
    response = ingress(dispatcher, max_body_bytes=4).handle(
        headers=signed_headers(body), raw_body=body
    )

    assert (response.status_code, response.body["code"]) == (413, "body_too_large")
    assert dispatcher.requests == []


def test_persistence_failure_is_normalized_and_excludes_sensitive_input() -> None:
    dispatcher = RecordingDispatcher(failures=1)
    evidence: list[Mapping[str, Any]] = []
    boundary = ingress(dispatcher, evidence=evidence)
    body = payload_bytes()

    response = boundary.handle(headers=signed_headers(body), raw_body=body)

    assert (response.status_code, response.body["code"]) == (503, "delivery_unavailable")
    assert dispatcher.requests == []
    serialized = json.dumps(evidence)
    assert SECRET.decode() not in serialized
    assert body.decode() not in serialized
    assert "X-Hub-Signature-256" not in serialized
    assert evidence[0]["outcome"] == "failed"


def test_replay_retries_incomplete_dispatch_after_transient_failure() -> None:
    dispatcher = RecordingDispatcher(failures=1)
    boundary = ingress(dispatcher)
    body = payload_bytes()

    failed = boundary.handle(headers=signed_headers(body), raw_body=body)
    retried = boundary.handle(headers=signed_headers(body), raw_body=body)
    completed_replay = boundary.handle(headers=signed_headers(body), raw_body=body)

    assert (failed.status_code, failed.body["code"]) == (
        503,
        "delivery_unavailable",
    )
    assert retried.status_code == 202
    assert completed_replay.status_code == 200
    assert completed_replay.body["eventId"] == retried.body["eventId"]
    assert len(dispatcher.requests) == 1


def test_signature_comparison_uses_constant_time_primitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[bytes, bytes]] = []

    def compare(first: bytes, second: bytes) -> bool:
        calls.append((first, second))
        return False

    monkeypatch.setattr(hmac, "compare_digest", compare)
    body = payload_bytes()
    response = ingress(RecordingDispatcher()).handle(headers={}, raw_body=body)

    assert response.status_code == 401
    assert len(calls) == 1
