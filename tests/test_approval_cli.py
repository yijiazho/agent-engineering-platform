import json
from pathlib import Path

from aep.approval import ApprovalGate, approval_digest
from aep.inspection_cli import main
from aep.runtime_store import DurableJsonRuntimeObjectStore

ROOT = Path(__file__).parents[1]
WORKFLOW_ID = "workflowexecution-000000000001"
TASK_ID = "taskexecution-000000000001"


def _waiting_task():
    task = json.loads(
        (ROOT / "fixtures/runtime/valid/successful-taskexecution.json").read_text(
            encoding="utf-8"
        )
    )
    task["status"] = "AWAITING_APPROVAL"
    task.pop("completedAt", None)
    task["dispatchState"] = "CLAIMED"
    task["dispatchOwnerId"] = "scheduler-owner"
    return task


def _decision():
    return {
        "id": "policy-1",
        "kind": "PolicyDecision",
        "decision": "REQUIRE_APPROVAL",
        "traceId": "trace-issue-to-pr-0001",
        "taskExecutionId": TASK_ID,
        "action": "github.create_pr",
        "policyRefs": [{"kind": "Policy", "name": "review", "version": "1.0.0"}],
        "repositoryRevision": "a" * 40,
        "provenance": {
            "workflowExecutionId": WORKFLOW_ID,
            "repositoryRevision": "a" * 40,
        },
    }


def _pending_approval(tmp_path, *, expires_at=None):
    state = tmp_path / "objects.json"
    store = DurableJsonRuntimeObjectStore(state)
    store.create(_waiting_task(), deterministic_key="task")
    digest = approval_digest(
        action="github.create_pr",
        target={"repository": "acme/widgets", "branch": "main"},
        repository_revision="a" * 40,
        artifact_digests={"patch": "sha256:" + "b" * 64},
    )
    approval = ApprovalGate(store).request(
        approval_id="approval-1",
        policy_decision=_decision(),
        requester="runtime",
        timestamp="2026-01-01T00:00:00Z",
        digest=digest,
        authorized_reviewers=("alice",),
        expires_at=expires_at,
        target={"repository": "acme/widgets", "branch": "main"},
        artifact_digests={"patch": "sha256:" + "b" * 64},
    )
    return state, approval, digest


def test_approvals_decide_approves_and_wakes_the_task(tmp_path, capsys):
    state, approval, digest = _pending_approval(tmp_path)

    assert main([
        "--state-file", str(state), "--output", "json",
        "approvals", "decide", approval["id"],
        "--actor", "alice", "--decision", "APPROVED",
        "--reason", "Reviewed the exact target and digest.",
        "--digest", digest,
    ]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "APPROVED"
    assert output["approver"] == "alice"
    assert output["reason"] == "Reviewed the exact target and digest."

    reopened = DurableJsonRuntimeObjectStore(state)
    assert reopened.get(approval["id"])["status"] == "APPROVED"
    task = reopened.get(TASK_ID)
    assert task["status"] == "PENDING"
    assert task["approvalId"] == approval["id"]
    assert "dispatchState" not in task
    assert "dispatchOwnerId" not in task


def test_approvals_decide_rejects_durably_without_waking_the_task(tmp_path, capsys):
    state, approval, _ = _pending_approval(tmp_path)

    assert main([
        "--state-file", str(state), "--output", "json",
        "approvals", "decide", approval["id"],
        "--actor", "alice", "--decision", "REJECTED",
        "--reason", "The change is too broad.",
    ]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "REJECTED"
    assert DurableJsonRuntimeObjectStore(state).get(TASK_ID)["status"] == "AWAITING_APPROVAL"


def test_approvals_decide_denies_unauthorized_actor_and_records_evidence(tmp_path, capsys):
    state, approval, _ = _pending_approval(tmp_path)

    assert main([
        "--state-file", str(state), "--output", "json",
        "approvals", "decide", approval["id"],
        "--actor", "mallory", "--decision", "APPROVED",
        "--reason", "Not actually authorized.",
    ]) == 2

    error = json.loads(capsys.readouterr().err)["error"]
    assert error["code"] == "APPROVAL_UNAUTHORIZED"
    store = DurableJsonRuntimeObjectStore(state)
    assert store.get(approval["id"])["status"] == "PENDING"
    denied = [
        event for event in store.list_by_workflow_execution(WORKFLOW_ID)
        if event.get("eventType") == "ApprovalAuthorizationDenied"
    ]
    assert len(denied) == 1
    assert denied[0]["payload"]["reasonCode"] == "UNAUTHORIZED_REVIEWER"


def test_approvals_decide_fails_closed_on_a_stale_digest(tmp_path, capsys):
    state, approval, _ = _pending_approval(tmp_path)

    assert main([
        "--state-file", str(state), "--output", "json",
        "approvals", "decide", approval["id"],
        "--actor", "alice", "--decision", "APPROVED",
        "--reason", "Approving the wrong thing.",
        "--digest", "sha256:" + "d" * 64,
    ]) == 2

    error = json.loads(capsys.readouterr().err)["error"]
    assert error["code"] == "APPROVAL_STALE"
    assert DurableJsonRuntimeObjectStore(state).get(approval["id"])["status"] == "PENDING"


def test_approvals_decide_expires_an_approval_without_waking_the_task(tmp_path, capsys):
    state, approval, _ = _pending_approval(
        tmp_path, expires_at="2020-01-01T00:00:00Z"
    )

    assert main([
        "--state-file", str(state), "--output", "json",
        "approvals", "decide", approval["id"],
        "--actor", "alice", "--decision", "APPROVED",
        "--reason", "Too late.",
    ]) == 2

    error = json.loads(capsys.readouterr().err)["error"]
    assert error["code"] == "APPROVAL_EXPIRED"
    store = DurableJsonRuntimeObjectStore(state)
    assert store.get(approval["id"])["status"] == "EXPIRED"
    assert store.get(TASK_ID)["status"] == "AWAITING_APPROVAL"


def test_approvals_decide_is_idempotent_and_terminal_is_not_reversible(tmp_path, capsys):
    state, approval, digest = _pending_approval(tmp_path)
    arguments = [
        "--state-file", str(state), "--output", "json",
        "approvals", "decide", approval["id"],
        "--actor", "alice", "--decision", "APPROVED",
        "--reason", "Reviewed.", "--digest", digest,
    ]
    assert main(arguments) == 0
    capsys.readouterr()
    assert main(arguments) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "APPROVED"

    assert main([
        "--state-file", str(state), "--output", "json",
        "approvals", "decide", approval["id"],
        "--actor", "alice", "--decision", "REJECTED",
        "--reason", "Changed my mind.",
    ]) == 2
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["code"] == "APPROVAL_ALREADY_DECIDED"


def test_approvals_decide_reports_a_missing_request(tmp_path, capsys):
    state, _, _ = _pending_approval(tmp_path)

    assert main([
        "--state-file", str(state), "--output", "json",
        "approvals", "decide", "approval-missing",
        "--actor", "alice", "--decision", "APPROVED",
        "--reason", "Reviewed.",
    ]) == 2

    error = json.loads(capsys.readouterr().err)["error"]
    assert error["code"] == "APPROVAL_NOT_FOUND"


def test_approvals_list_discovers_and_filters_requests(tmp_path, capsys):
    state, approval, _ = _pending_approval(tmp_path)

    assert main([
        "--state-file", str(state), "--output", "json",
        "approvals", "list", "--status", "PENDING",
    ]) == 0
    listed = json.loads(capsys.readouterr().out)["approvals"]
    assert [item["id"] for item in listed] == [approval["id"]]
    assert listed[0]["targetAction"] == "github.create_pr"
    assert listed[0]["artifactRequestDigest"].startswith("sha256:")

    assert main([
        "--state-file", str(state), "--output", "json",
        "approvals", "list", "--status", "APPROVED",
    ]) == 0
    assert json.loads(capsys.readouterr().out)["approvals"] == []


def test_approvals_decide_human_output_reports_the_decision(tmp_path, capsys):
    state, approval, _ = _pending_approval(
        tmp_path, expires_at="2030-01-01T00:00:00Z"
    )

    assert main([
        "--state-file", str(state),
        "approvals", "decide", approval["id"],
        "--actor", "alice", "--decision", "APPROVED",
        "--reason", "Reviewed.",
    ]) == 0

    output = capsys.readouterr().out
    assert "Status: APPROVED" in output
    assert "Action: github.create_pr" in output
    assert "Target: " in output
    assert '"repository": "acme/widgets"' in output
    assert '"branch": "main"' in output
    assert "Artifact digests: " in output
    assert "Governing policies: Policy:review:1.0.0" in output
    assert "Expires at: 2030-01-01T00:00:00Z" in output
    assert "Decided by: alice" in output
    assert "Reason: Reviewed." in output


def test_approvals_decide_requires_actor_decision_and_reason(tmp_path, capsys):
    state, approval, _ = _pending_approval(tmp_path)

    assert main([
        "--state-file", str(state), "--output", "json",
        "approvals", "decide", approval["id"],
        "--actor", "alice",
    ]) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "INVALID_ARGUMENT"
