from aep.approval import ApprovalGate, approval_digest
from aep.runtime_store import InMemoryRuntimeObjectStore, StatusConflictError
import json
from pathlib import Path


def _decision():
    return {
        "id": "policy-1", "kind": "PolicyDecision", "decision": "REQUIRE_APPROVAL",
        "traceId": "trace-issue-to-pr-0001", "taskExecutionId": "taskexecution-000000000001", "action": "github.create_pr",
        "policyRefs": [{"kind": "Policy", "name": "review", "version": "1.0.0"}],
        "repositoryRevision": "a" * 40,
        "provenance": {"workflowExecutionId": "workflowexecution-000000000001", "repositoryRevision": "a" * 40},
    }


def test_approval_is_artifact_and_target_bound_and_idempotent():
    store = InMemoryRuntimeObjectStore()
    _add_claimed_waiting_task(store)
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
    store = InMemoryRuntimeObjectStore()
    _add_claimed_waiting_task(store)
    gate = ApprovalGate(store)
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
    store = InMemoryRuntimeObjectStore()
    _add_claimed_waiting_task(store)
    gate = ApprovalGate(store)
    digest = "sha256:" + "e" * 64
    gate.request(approval_id="approval-1", policy_decision=_decision(), requester="runtime",
                 timestamp="2026-01-01T00:00:00Z", digest=digest,
                 authorized_reviewers=("alice",))
    result = gate.decide("approval-1", actor="alice", decision="REJECTED",
                         timestamp="2026-01-01T00:01:00Z", reason="Unsafe", current_digest=digest)
    assert gate.store.get("approval-1")["status"] == "REJECTED"
    assert result["decidedAt"] == "2026-01-01T00:01:00Z"


def _add_claimed_waiting_task(store):
    fixture = Path(__file__).parents[1] / "fixtures/runtime/valid/successful-taskexecution.json"
    task = json.loads(fixture.read_text(encoding="utf-8"))
    task["status"] = "AWAITING_APPROVAL"
    task.pop("completedAt", None)
    task["dispatchState"] = "CLAIMED"
    task["dispatchOwnerId"] = "scheduler-owner"
    store.create(task, deterministic_key="test-awaiting-task")


def test_approved_task_wakeup_clears_durable_dispatch_claim():
    store = InMemoryRuntimeObjectStore()
    _add_claimed_waiting_task(store)
    store.bind_approval_request("taskexecution-000000000001", "approval-1", updated_at="2026-01-01T00:00:00Z")
    store.wake_approved_task("taskexecution-000000000001", "approval-1", updated_at="2026-01-01T00:01:00Z")
    task = store.get("taskexecution-000000000001")
    assert task["status"] == "PENDING"
    assert task["approvalId"] == "approval-1"
    assert "dispatchState" not in task
    assert "dispatchOwnerId" not in task


def test_historical_approval_cannot_wake_a_new_approval_wait():
    store = InMemoryRuntimeObjectStore()
    _add_claimed_waiting_task(store)
    store.bind_approval_request("taskexecution-000000000001", "approval-old", updated_at="2026-01-01T00:00:00Z")
    store.bind_approval_request("taskexecution-000000000001", "approval-current", updated_at="2026-01-01T00:01:00Z")
    task = store.wake_approved_task("taskexecution-000000000001", "approval-old", updated_at="2026-01-01T00:02:00Z")
    assert task["status"] == "AWAITING_APPROVAL"
    assert task["approvalId"] == "approval-current"


def test_deduplicated_request_binds_persisted_approval_identity():
    store = InMemoryRuntimeObjectStore()
    _add_claimed_waiting_task(store)
    gate = ApprovalGate(store)
    args = {
        "policy_decision": _decision(), "requester": "runtime",
        "timestamp": "2026-01-01T00:00:00Z", "digest": "sha256:" + "2" * 64,
        "authorized_reviewers": ("alice",),
    }
    first = gate.request(approval_id="approval-1", **args)
    second = gate.request(approval_id="approval-2", **args)
    assert first["id"] == second["id"] == "approval-1"
    assert store.get("taskexecution-000000000001")["approvalId"] == "approval-1"


def test_historical_request_retry_cannot_replace_a_new_approval_wait():
    store = InMemoryRuntimeObjectStore()
    _add_claimed_waiting_task(store)
    gate = ApprovalGate(store)
    digest_a = "sha256:" + "3" * 64
    args_a = {
        "policy_decision": _decision(), "requester": "runtime",
        "timestamp": "2026-01-01T00:00:00Z", "digest": digest_a,
        "authorized_reviewers": ("alice",),
    }
    gate.request(approval_id="approval-1", **args_a)
    gate.decide("approval-1", actor="alice", decision="APPROVED",
                timestamp="2026-01-01T00:01:00Z", reason="Reviewed", current_digest=digest_a)
    store.update_status("taskexecution-000000000001", "AWAITING_APPROVAL",
                        expected_status="PENDING", updated_at="2026-01-01T00:02:00Z")
    decision_b = _decision()
    decision_b["id"] = "policydecision-000000000003"
    decision_b["action"] = "git.push"
    digest_b = "sha256:" + "4" * 64
    gate.request(
        approval_id="approval-2", policy_decision=decision_b, requester="runtime",
        timestamp="2026-01-01T00:02:00Z", digest=digest_b,
        authorized_reviewers=("alice",), expected_current_approval_id="approval-1",
    )
    import pytest
    with pytest.raises(StatusConflictError, match="binding changed"):
        gate.request(approval_id="approval-1", **args_a)
    gate.decide("approval-1", actor="alice", decision="APPROVED",
                timestamp="2026-01-01T00:03:00Z", reason="Duplicate", current_digest=digest_a)
    gate.decide("approval-2", actor="alice", decision="REJECTED",
                timestamp="2026-01-01T00:04:00Z", reason="Changed evidence", current_digest=digest_b)
    assert store.get("taskexecution-000000000001")["status"] == "AWAITING_APPROVAL"
    assert store.get("taskexecution-000000000001")["approvalId"] == "approval-2"


def test_duplicate_approval_repairs_interrupted_task_wakeup():
    store = InMemoryRuntimeObjectStore()
    _add_claimed_waiting_task(store)
    gate = ApprovalGate(store)
    digest = "sha256:" + "f" * 64
    gate.request(approval_id="approval-1", policy_decision=_decision(), requester="runtime",
                 timestamp="2026-01-01T00:00:00Z", digest=digest,
                 authorized_reviewers=("alice",))
    store.update_status("approval-1", "APPROVED", expected_status="PENDING",
                        updated_at="2026-01-01T00:01:00Z",
                        changes={"approver": "alice", "decidedAt": "2026-01-01T00:01:00Z", "reason": "Reviewed"})
    gate.decide("approval-1", actor="alice", decision="APPROVED",
                timestamp="2026-01-01T00:02:00Z", reason="Reviewed", current_digest=digest)
    assert store.get("taskexecution-000000000001")["status"] == "PENDING"


def test_duplicate_decision_rejects_changed_digest_after_approval():
    store = InMemoryRuntimeObjectStore()
    _add_claimed_waiting_task(store)
    gate = ApprovalGate(store)
    digest = "sha256:" + "a" * 64
    gate.request(approval_id="approval-1", policy_decision=_decision(), requester="runtime",
                 timestamp="2026-01-01T00:00:00Z", digest=digest,
                 authorized_reviewers=("alice",))
    gate.decide("approval-1", actor="alice", decision="APPROVED",
                timestamp="2026-01-01T00:01:00Z", reason="Reviewed", current_digest=digest)
    import pytest
    with pytest.raises(ValueError, match="stale"):
        gate.decide("approval-1", actor="alice", decision="APPROVED",
                    timestamp="2026-01-01T00:02:00Z", reason="Reviewed",
                    current_digest="sha256:" + "b" * 64)


def test_expired_approval_is_recorded_and_cannot_wake_task():
    store = InMemoryRuntimeObjectStore()
    _add_claimed_waiting_task(store)
    gate = ApprovalGate(store)
    digest = "sha256:" + "1" * 64
    gate.request(approval_id="approval-1", policy_decision=_decision(), requester="runtime",
                 timestamp="2026-07-09T00:00:00Z", digest=digest,
                 authorized_reviewers=("alice",), expires_at="2026-07-10T00:00:00Z")
    import pytest
    with pytest.raises(ValueError, match="expired"):
        gate.decide("approval-1", actor="alice", decision="APPROVED",
                    timestamp="2026-07-11T00:00:00Z", reason="Late approval", current_digest=digest)
    assert store.get("approval-1")["status"] == "EXPIRED"
    assert store.get("taskexecution-000000000001")["status"] == "AWAITING_APPROVAL"
