"""Durable, evidence-bound human approval decisions."""

from __future__ import annotations

from hashlib import sha256
import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from aep.runtime_store import RuntimeObject, RuntimeObjectStore


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
                authorized_reviewers: tuple[str, ...], expires_at: str | None = None) -> RuntimeObject:
        if policy_decision.get("decision") != "REQUIRE_APPROVAL":
            raise ValueError("approval requires a REQUIRE_APPROVAL PolicyDecision")
        if not authorized_reviewers:
            raise ValueError("approval policy must resolve at least one reviewer")
        record = {
            "apiVersion": "aep.dev/v1alpha1", "kind": "Approval", "id": approval_id,
            "traceId": policy_decision["traceId"], "createdAt": timestamp,
            "updatedAt": timestamp,
            "provenance": dict(policy_decision.get("provenance", {})),
            "workflowExecutionId": policy_decision["provenance"]["workflowExecutionId"],
            "taskExecutionId": policy_decision["taskExecutionId"],
            "policyDecisionId": policy_decision["id"], "requester": requester,
            "status": "PENDING", "requestedAt": timestamp,
            "governingPolicyRefs": list(policy_decision["policyRefs"]),
            "targetAction": policy_decision["action"],
            "repositoryRevision": policy_decision.get("repositoryRevision", policy_decision["provenance"].get("repositoryRevision")),
            "artifactRequestDigest": digest,
            "authorizedReviewers": list(authorized_reviewers),
        }
        if expires_at is not None:
            record["expiresAt"] = expires_at
        approval = self.store.create(record, deterministic_key=f"approval:{policy_decision['id']}:{digest}")
        self.store.bind_approval_request(str(record["taskExecutionId"]), approval_id, updated_at=timestamp)
        return approval

    def decide(self, approval_id: str, *, actor: str, decision: str,
               timestamp: str, reason: str, current_digest: str) -> RuntimeObject:
        approval = self.store.get(approval_id)
        if approval is None or approval.get("kind") != "Approval":
            raise ValueError("approval request was not found")
        if actor not in approval.get("authorizedReviewers", ()):
            raise PermissionError("identity is not authorized by the resolved approval policy")
        if current_digest != approval.get("artifactRequestDigest"):
            raise ValueError("approval is stale because its bound artifact or action changed")
        expires_at = approval.get("expiresAt")
        if approval.get("status") == "PENDING" and expires_at is not None and _timestamp(timestamp) >= _timestamp(expires_at):
            self.store.update_status(
                approval_id, "EXPIRED", expected_status="PENDING", updated_at=timestamp,
                changes={"decidedAt": timestamp, "reason": "Approval request expired."},
            )
            raise ValueError("approval request has expired")
        if approval.get("status") != "PENDING":
            if approval.get("approver") == actor and approval.get("status") == decision:
                if decision == "APPROVED":
                    self._wake_task(approval, approval_id, timestamp)
                return approval
            raise ValueError("approval request already has a terminal decision")
        if decision not in {"APPROVED", "REJECTED"}:
            raise ValueError("decision must be APPROVED or REJECTED")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("a non-empty decision reason is required")
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


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ValueError("approval timestamps must be RFC3339 values") from error
    if parsed.tzinfo is None:
        raise ValueError("approval timestamps must include a timezone")
    return parsed
