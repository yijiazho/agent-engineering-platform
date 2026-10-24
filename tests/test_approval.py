from aep.approval import ApprovalGate, approval_digest
from aep.runtime_store import InMemoryRuntimeObjectStore


def _decision():
    return {
        "id": "policy-1", "kind": "PolicyDecision", "decision": "REQUIRE_APPROVAL",
        "traceId": "trace-1", "taskExecutionId": "task-1", "action": "github.create_pr",
        "policyRefs": [{"kind": "Policy", "name": "review", "version": "1.0.0"}],
        "repositoryRevision": "a" * 40,
        "provenance": {"workflowExecutionId": "workflow-1", "repositoryRevision": "a" * 40},
    }


def test_approval_is_artifact_and_target_bound_and_idempotent():
    store = InMemoryRuntimeObjectStore()
    gate = ApprovalGate(store)
    digest = approval_digest(action="github.create_pr", target={"repository": "acme/widgets", "branch": "main"},
                             repository_revision="a" * 40, artifact_digests={"patch": "sha256:" + "b" * 64})
    request = gate.request(approval_id="approval-1", policy_decision=_decision(), requester="runtime",
                           timestamp="2026-01-01T00:00:00Z", digest=digest,
                           authorized_reviewers=("alice",))
    assert request["status"] == "PENDING"
    approved = gate.decide("approval-1", actor="alice", decision="APPROVED",
                           timestamp="2026-01-01T00:01:00Z", reason="Reviewed",
                           current_digest=digest)
    assert approved["approver"] == "alice"
    assert gate.decide("approval-1", actor="alice", decision="APPROVED",
                       timestamp="2026-01-01T00:02:00Z", reason="Reviewed",
                       current_digest=digest) == approved


def test_approval_rejects_unauthorized_and_changed_artifact():
    gate = ApprovalGate(InMemoryRuntimeObjectStore())
    digest = "sha256:" + "c" * 64
    gate.request(approval_id="approval-1", policy_decision=_decision(), requester="runtime",
                 timestamp="2026-01-01T00:00:00Z", digest=digest,
                 authorized_reviewers=("alice",))
    import pytest
    with pytest.raises(PermissionError):
        gate.decide("approval-1", actor="mallory", decision="APPROVED",
                    timestamp="2026-01-01T00:01:00Z", reason="No", current_digest=digest)
    with pytest.raises(ValueError, match="stale"):
        gate.decide("approval-1", actor="alice", decision="APPROVED",
                    timestamp="2026-01-01T00:01:00Z", reason="Yes", current_digest="sha256:" + "d" * 64)


def test_rejection_is_durable():
    gate = ApprovalGate(InMemoryRuntimeObjectStore())
    digest = "sha256:" + "e" * 64
    gate.request(approval_id="approval-1", policy_decision=_decision(), requester="runtime",
                 timestamp="2026-01-01T00:00:00Z", digest=digest,
                 authorized_reviewers=("alice",))
    result = gate.decide("approval-1", actor="alice", decision="REJECTED",
                         timestamp="2026-01-01T00:01:00Z", reason="Unsafe", current_digest=digest)
    assert gate.store.get("approval-1")["status"] == "REJECTED"
    assert result["decidedAt"] == "2026-01-01T00:01:00Z"
