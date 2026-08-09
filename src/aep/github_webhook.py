"""Authenticated GitHub webhook ingress for the repository-bound MVP."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from aep.github_events import (
    GitHubEventValidationError,
    normalize_github_issue_created,
)
from aep.observability import redact
from aep.webhook_dispatch import ReconciliationDispatcher


WEBHOOK_PATH = "/v1/webhooks/github"
DEFAULT_MAX_BODY_BYTES = 1_048_576


@dataclass(frozen=True, slots=True)
class WebhookResponse:
    status_code: int
    body: Mapping[str, Any]


class GitHubWebhookIngress:
    """Authenticate, validate, deduplicate, and dispatch GitHub issue deliveries."""

    def __init__(
        self,
        *,
        secret: bytes,
        repository_owner: str,
        repository_name: str,
        dispatcher: ReconciliationDispatcher,
        evidence_sink: Callable[[Mapping[str, Any]], None] | None = None,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(secret, bytes) or not secret:
            raise ValueError("secret must be non-empty bytes")
        if not repository_owner.strip() or not repository_name.strip():
            raise ValueError("repository identity must not be empty")
        if isinstance(max_body_bytes, bool) or max_body_bytes <= 0:
            raise ValueError("max_body_bytes must be a positive integer")
        self._secret = secret
        self._repository = f"{repository_owner.strip()}/{repository_name.strip()}"
        self._dispatcher = dispatcher
        self._evidence_sink = evidence_sink
        self._max_body_bytes = max_body_bytes
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def max_body_bytes(self) -> int:
        return self._max_body_bytes

    def handle(self, *, headers: Mapping[str, str], raw_body: bytes) -> WebhookResponse:
        normalized_headers = {key.casefold(): value for key, value in headers.items()}
        delivery_id = normalized_headers.get("x-github-delivery", "").strip()
        trace_id = _trace_id(delivery_id)

        if len(raw_body) > self._max_body_bytes:
            return self._reject(413, "body_too_large", trace_id, delivery_id)
        signature = normalized_headers.get("x-hub-signature-256", "")
        if not _valid_signature(self._secret, raw_body, signature):
            return self._reject(401, "invalid_signature", trace_id, delivery_id)
        if not delivery_id:
            return self._reject(400, "missing_delivery_id", trace_id, delivery_id)
        if normalized_headers.get("x-github-event", "").strip() != "issues":
            return self._reject(422, "unsupported_event", trace_id, delivery_id)

        try:
            payload = json.loads(raw_body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._reject(400, "malformed_json", trace_id, delivery_id)
        if not isinstance(payload, Mapping):
            return self._reject(400, "malformed_json", trace_id, delivery_id)
        if payload.get("action") != "opened":
            return self._reject(422, "unsupported_action", trace_id, delivery_id)
        repository = payload.get("repository")
        actual_repository = (
            repository.get("full_name") if isinstance(repository, Mapping) else None
        )
        if not isinstance(actual_repository, str) or (
            actual_repository.casefold() != self._repository.casefold()
        ):
            return self._reject(422, "repository_mismatch", trace_id, delivery_id)

        try:
            event = normalize_github_issue_created(
                payload,
                delivery_id=delivery_id,
                received_at=self._clock(),
            )
        except GitHubEventValidationError:
            return self._reject(422, "invalid_payload", trace_id, delivery_id)

        try:
            acceptance = self._dispatcher.submit(event, trace_id=trace_id)
        except Exception:  # the edge must normalize persistence/adapter failures
            self._emit("failed", trace_id, delivery_id, event_id=event["id"])
            return WebhookResponse(
                503,
                {"status": "failed", "code": "delivery_unavailable", "traceId": trace_id},
            )

        status = "accepted" if acceptance.accepted else "duplicate"
        self._emit(status, trace_id, delivery_id, event_id=acceptance.event["id"])
        return WebhookResponse(
            202 if acceptance.accepted else 200,
            {
                "status": status,
                "eventId": acceptance.event["id"],
                "traceId": trace_id,
            },
        )

    def _reject(
        self, status_code: int, code: str, trace_id: str, delivery_id: str
    ) -> WebhookResponse:
        self._emit("rejected", trace_id, delivery_id, code=code)
        return WebhookResponse(
            status_code,
            {"status": "rejected", "code": code, "traceId": trace_id},
        )

    def _emit(
        self,
        outcome: str,
        trace_id: str,
        delivery_id: str,
        *,
        event_id: str | None = None,
        code: str | None = None,
    ) -> None:
        if self._evidence_sink is None:
            return
        evidence = {
            "schemaVersion": "aep.dev/ingress/v1alpha1",
            "eventName": "GitHubWebhookDelivery",
            "emittedAt": self._clock()
            .astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "service": "event-controller",
            "traceId": trace_id,
            "outcome": outcome,
            "deliveryId": delivery_id or None,
            "eventId": event_id,
            "code": code,
        }
        self._evidence_sink(redact(evidence))


def _valid_signature(secret: bytes, raw_body: bytes, supplied: str) -> bool:
    expected = "sha256=" + hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
    # compare_digest is intentionally reached even for malformed/missing values.
    return hmac.compare_digest(expected.encode("ascii"), supplied.encode("utf-8"))


def _trace_id(delivery_id: str) -> str:
    identity = delivery_id or "missing-delivery"
    return str(uuid5(NAMESPACE_URL, f"aep:github-webhook:{identity}"))
