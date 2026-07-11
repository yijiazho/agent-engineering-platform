"""Normalize supported GitHub webhooks into AEP platform events."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import NAMESPACE_URL, uuid5


@dataclass(frozen=True)
class ValidationIssue:
    """One machine-readable problem with an inbound webhook."""

    field: str
    code: str
    message: str


class GitHubEventValidationError(ValueError):
    """Raised when a GitHub webhook cannot be normalized."""

    def __init__(self, issues: list[ValidationIssue]) -> None:
        self.issues = tuple(issues)
        super().__init__("; ".join(f"{issue.field}: {issue.message}" for issue in issues))

    def as_dict(self) -> dict[str, object]:
        return {
            "code": "invalid_github_event",
            "message": "GitHub event payload is invalid",
            "errors": [
                {"field": issue.field, "code": issue.code, "message": issue.message}
                for issue in self.issues
            ],
        }


def normalize_github_issue_created(
    payload: Mapping[str, Any],
    *,
    delivery_id: str,
    received_at: datetime | None = None,
) -> dict[str, Any]:
    """Return a normalized ``github.issue.created`` event.

    This function only validates and transforms input. Workflow selection and
    execution deliberately belong to downstream controllers.
    """
    issues: list[ValidationIssue] = []
    if not isinstance(payload, Mapping):
        raise GitHubEventValidationError(
            [ValidationIssue("payload", "invalid_type", "must be an object")]
        )
    if not isinstance(delivery_id, str) or not delivery_id.strip():
        issues.append(
            ValidationIssue("delivery_id", "required", "must be a non-empty string")
        )
    if payload.get("action") != "opened":
        issues.append(ValidationIssue("action", "unsupported", "must be 'opened'"))

    repository = _require_object(payload, "repository", issues)
    issue = _require_object(payload, "issue", issues)
    sender = _require_object(payload, "sender", issues)
    _require_fields(repository, "repository", (("id", int), ("full_name", str)), issues)
    _require_fields(
        issue,
        "issue",
        (("id", int), ("number", int), ("title", str)),
        issues,
    )
    _require_fields(sender, "sender", (("id", int), ("login", str)), issues)

    timestamp = received_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        issues.append(
            ValidationIssue("received_at", "invalid", "must include a timezone")
        )
    if issues:
        raise GitHubEventValidationError(issues)

    normalized_delivery_id = delivery_id.strip()
    deduplication_key = f"github:delivery:{normalized_delivery_id}"
    return {
        "id": f"event-{uuid5(NAMESPACE_URL, deduplication_key)}",
        "source": "github",
        "type": "github.issue.created",
        "repository": deepcopy(repository),
        "issue": deepcopy(issue),
        "sender": deepcopy(sender),
        "receivedAt": timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "deduplicationKey": deduplication_key,
    }


def _require_object(
    payload: Mapping[str, Any], field: str, issues: list[ValidationIssue]
) -> Mapping[str, Any]:
    value = payload.get(field)
    if not isinstance(value, Mapping):
        issues.append(ValidationIssue(field, "invalid_type", "must be an object"))
        return {}
    return value


def _require_fields(
    value: Mapping[str, Any],
    prefix: str,
    fields: tuple[tuple[str, type[int] | type[str]], ...],
    issues: list[ValidationIssue],
) -> None:
    for field, expected_type in fields:
        candidate = value.get(field)
        is_valid = isinstance(candidate, expected_type)
        if expected_type is int and isinstance(candidate, bool):
            is_valid = False
        if expected_type is str and not candidate:
            is_valid = False
        if not is_valid:
            issues.append(
                ValidationIssue(
                    f"{prefix}.{field}",
                    "required",
                    f"must be a non-empty {expected_type.__name__}",
                )
            )
