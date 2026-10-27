"""Durable, evidence-bound human approval decisions."""

from __future__ import annotations

from hashlib import sha256
import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from aep.runtime_store import RuntimeObject, RuntimeObjectStore


class ApprovalError(ValueError):
    """A stable, operator-safe approval failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ApprovalAuthorizationError(PermissionError):
    """Raised when a decision actor is not an authorized reviewer."""

    code = "APPROVAL_UNAUTHORIZED"


def approval_digest(*, action: str, target: Mapping[str, Any], repository_revision: str,
                    artifact_digests: Mapping[str, str] | None = None) -> str:
    """Return a canonical digest for the exact action and evidence being approved."""
    payload = {"action": action, "target": dict(target),
               "repositoryRevision": repository_revision,
               "artifactDigests": dict(artifact_digests or {})}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + sha256(encoded.encode("utf-8")).hexdigest()


class ApprovalGate:
    """Create approval requests and record authorized, immutable decisions."""

    def __init__(self, store: RuntimeObjectStore) -> None:
        self.store = store

    def request(self, *, approval_id: str, policy_decision: Mapping[str, Any],
                requester: str, timestamp: str, digest: str,
                authorized_reviewers: tuple[str, ...], expires_at: str | None = None,
                target: Mapping[str, Any] | None = None,
                artifact_digests: Mapping[str, str] | None = None,
                approval_policy_refs: tuple[Mapping[str, Any], ...] = (),
                expected_current_approval_id: str | None = None) -> RuntimeObject:
        if policy_decision.get("decision") != "REQUIRE_APPROVAL":
            raise ApprovalError(
                "APPROVAL_POLICY_INVALID",
                "approval requires a REQUIRE_APPROVAL PolicyDecision",
            )
        if not authorized_reviewers:
            raise ApprovalError(
                "APPROVAL_REVIEWERS_MISSING",
                "approval policy must resolve at least one reviewer",
            )
        record = {
            "apiVersion": "aep.dev/v1alpha1", "kind": "Approval", "id": approval_id,
            "traceId": policy_decision["traceId"], "createdAt": timestamp,
            "updatedAt": timestamp,
            "provenance": dict(policy_decision.get("provenance", {})),
            "workflowExecutionId": policy_decision["provenance"]["workflowExecutionId"],
            "taskExecutionId": policy_decision["taskExecutionId"],
            "policyDecisionId": policy_decision["id"], "requester": requester,
            "status": "PENDING", "requestedAt": timestamp,
            "governingPolicyRefs": _unique_refs(
                (*policy_decision["policyRefs"], *approval_policy_refs)
            ),
            "targetAction": policy_decision["action"],
            "repositoryRevision": policy_decision.get("repositoryRevision", policy_decision["provenance"].get("repositoryRevision")),
            "artifactRequestDigest": digest,
            "authorizedReviewers": list(authorized_reviewers),
        }
        if expires_at is not None:
            record["expiresAt"] = expires_at
        if target is not None:
            record["target"] = dict(target)
        if artifact_digests is not None:
            record["artifactDigests"] = dict(artifact_digests)
        return self.store.create_approval_request(
            record,
            deterministic_key=f"approval:{policy_decision['id']}:{digest}",
            expected_current_approval_id=expected_current_approval_id,
            updated_at=timestamp,
        )

    def decide(self, approval_id: str, *, actor: str, decision: str,
               timestamp: str, reason: str, current_digest: str) -> RuntimeObject:
        approval = self.store.get(approval_id)
        if approval is None or approval.get("kind") != "Approval":
            raise ApprovalError("APPROVAL_NOT_FOUND", "approval request was not found")
        if actor not in approval.get("authorizedReviewers", ()):
            self._record_unauthorized_attempt(approval, actor, decision, timestamp)
            raise ApprovalAuthorizationError(
                "identity is not authorized by the resolved approval policy"
            )
        if current_digest != approval.get("artifactRequestDigest"):
            raise ApprovalError(
                "APPROVAL_STALE",
                "approval is stale because its bound artifact or action changed",
            )
        expires_at = approval.get("expiresAt")
        if approval.get("status") == "PENDING" and expires_at is not None and _timestamp(timestamp) >= _timestamp(expires_at):
            self.store.update_status(
                approval_id, "EXPIRED", expected_status="PENDING", updated_at=timestamp,
                changes={"decidedAt": timestamp, "reason": "Approval request expired."},
            )
            raise ApprovalError("APPROVAL_EXPIRED", "approval request has expired")
        if approval.get("status") != "PENDING":
            if approval.get("approver") == actor and approval.get("status") == decision:
                if decision == "APPROVED":
                    self._wake_task(approval, approval_id, timestamp)
                return approval
            raise ApprovalError(
                "APPROVAL_ALREADY_DECIDED",
                "approval request already has a terminal decision",
            )
        if decision not in {"APPROVED", "REJECTED"}:
            raise ApprovalError(
                "APPROVAL_DECISION_INVALID",
                "decision must be APPROVED or REJECTED",
            )
        if not isinstance(reason, str) or not reason.strip():
            raise ApprovalError(
                "APPROVAL_REASON_REQUIRED",
                "a non-empty decision reason is required",
            )
        decided = self.store.update_status(
            approval_id, decision, expected_status="PENDING", updated_at=timestamp,
            changes={"approver": actor, "decidedAt": timestamp, "reason": reason.strip()},
        )
        if decision == "APPROVED":
            self._wake_task(approval, approval_id, timestamp)
        return decided

    def _wake_task(self, approval: Mapping[str, Any], approval_id: str, timestamp: str) -> None:
        task_id = approval.get("taskExecutionId")
        if isinstance(task_id, str):
            task = self.store.get(task_id)
            if task is not None and task.get("status") in {"AWAITING_APPROVAL", "PENDING"}:
                self.store.wake_approved_task(task_id, approval_id, updated_at=timestamp)

    def _record_unauthorized_attempt(
        self, approval: Mapping[str, Any], actor: str, decision: str, timestamp: str
    ) -> None:
        attempt_key = f"{approval['id']}:{actor}:{decision}:{timestamp}"
        event_id = "executionevent-" + sha256(attempt_key.encode("utf-8")).hexdigest()[:24]
        event = {
            "apiVersion": "aep.dev/v1alpha1", "kind": "ExecutionEvent", "id": event_id,
            "traceId": approval["traceId"], "createdAt": timestamp, "updatedAt": timestamp,
            "provenance": {
                "actor": "approval-gate", "workflowExecutionId": approval["workflowExecutionId"],
                "taskExecutionId": approval["taskExecutionId"], "resourceRefs": approval.get("governingPolicyRefs", []),
            },
            "eventType": "ApprovalAuthorizationDenied",
            "subject": {"kind": "Approval", "id": approval["id"]},
            "sequence": 1, "emittedAt": timestamp,
            "payload": {"actorIdentity": actor, "requestedDecision": decision, "reasonCode": "UNAUTHORIZED_REVIEWER"},
        }
        self.store.append_event(event)


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ApprovalError(
            "APPROVAL_TIMESTAMP_INVALID",
            "approval timestamps must be RFC3339 values",
        ) from error
    if parsed.tzinfo is None:
        raise ApprovalError(
            "APPROVAL_TIMESTAMP_INVALID",
            "approval timestamps must include a timezone",
        )
    return parsed


def _unique_refs(values: Any) -> list[dict[str, Any]]:
    result: dict[tuple[str, str, str], dict[str, Any]] = {}
    for value in values:
        ref = dict(value)
        key = (str(ref.get("kind")), str(ref.get("name")), str(ref.get("version")))
        result[key] = ref
    return [result[key] for key in sorted(result)]
