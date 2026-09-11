import json
from pathlib import Path

from aep.inspection_cli import main
from aep.workflow_execution import _runtime_validator


def _checkpoint(tmp_path, objects):
    path = tmp_path / "objects.json"
    path.write_text(json.dumps({"objects": {item["id"]: item for item in objects}, "deterministicKeys": {}, "claims": {}}), encoding="utf-8")
    return path


def _records(status="SUCCEEDED", policy="ALLOW", approval=None):
    workflow = {"id": "wf-1", "kind": "WorkflowExecution", "status": status, "createdAt": "2026-01-01T00:00:00Z", "startedAt": "2026-01-01T00:00:00Z", "completedAt": "2026-01-01T00:00:01Z", "workflowRef": {"kind": "Workflow", "name": "issue-to-pr", "version": "1.0.0"}, "repositoryRevision": "a" * 40, "taskExecutionIds": ["task-1"]}
    task = {"id": "task-1", "kind": "TaskExecution", "workflowExecutionId": "wf-1", "status": "FAILED" if status == "FAILED" else "SUCCEEDED", "taskRef": {"kind": "Task", "name": "validate", "version": "1.0.0"}, "createdAt": "2026-01-01T00:00:00Z"}
    context = {"id": "ctx-1", "kind": "ContextPackage", "workflowExecutionId": "wf-1", "taskExecutionId": "task-1", "createdAt": "2026-01-01T00:00:00Z", "elements": [{"type": "repository", "content": "private source", "provenance": {"source": "test"}, "tokenCount": 4}], "selection": {"selected": ["repository"], "discarded": []}, "tokenEstimate": {"count": 4}, "truncation": "NONE"}
    artifact = {"id": "artifact-1", "kind": "GeneratedArtifact", "workflowExecutionId": "wf-1", "taskExecutionId": "task-1", "artifactType": "PATCH", "mediaType": "text/x-diff", "contentAddress": "sha256:" + "b" * 64, "repositoryRevision": "a" * 40, "createdAt": "2026-01-01T00:00:00Z"}
    decision = {"id": "policy-1", "kind": "PolicyDecision", "workflowExecutionId": "wf-1", "taskExecutionId": "task-1", "decision": policy, "reason": "publication denied", "createdAt": "2026-01-01T00:00:00Z"}
    records = [workflow, task, context, artifact, decision]
    if status == "FAILED":
        records.append({"id": "evaluation-1", "kind": "EvaluationResult", "workflowExecutionId": "wf-1", "taskExecutionId": "task-1", "status": "SUCCEEDED", "outcome": "FAIL", "evaluationRef": {"kind": "Evaluation", "name": "tests", "version": "1.0.0"}, "createdAt": "2026-01-01T00:00:00Z"})
    if approval:
        records.append({"id": "approval-1", "kind": "Approval", "workflowExecutionId": "wf-1", "policyDecisionId": "policy-1", "status": approval, "requester": "operator", "requestedAt": "2026-01-01T00:00:00Z", "createdAt": "2026-01-01T00:00:00Z"})
    return records


def test_execution_json_summary_and_context_redaction(tmp_path, capsys):
    state = _checkpoint(tmp_path, _records())
    assert main(["--state-file", str(state), "--output", "json", "executions", "show", "wf-1"]) == 0
    execution = json.loads(capsys.readouterr().out)
    assert execution["workflowExecution"]["workflowRef"]["version"] == "1.0.0"
    assert execution["elapsed"]["milliseconds"] == 1000
    assert execution["tasks"][0]["status"] == "SUCCEEDED"
    assert main(["--state-file", str(state), "--output", "json", "contexts", "show", "ctx-1"]) == 0
    context = json.loads(capsys.readouterr().out)
    assert context["elements"][0]["content"] == "[REDACTED]"
    assert context["elements"][0]["tokenCount"] == 4


def test_execution_list_discovers_ids_in_deterministic_order_and_filters_status(tmp_path, capsys):
    records = _records()
    later = dict(records[0])
    later.update({"id": "wf-2", "status": "FAILED", "createdAt": "2026-01-02T00:00:00Z"})
    records.append(later)
    state = _checkpoint(tmp_path, records)
    assert main(["--state-file", str(state), "--output", "json", "executions", "list"]) == 0
    listed = json.loads(capsys.readouterr().out)["workflowExecutions"]
    assert [item["id"] for item in listed] == ["wf-1", "wf-2"]
    assert main(["--state-file", str(state), "--output", "json", "executions", "list", "--status", "FAILED"]) == 0
    assert [item["id"] for item in json.loads(capsys.readouterr().out)["workflowExecutions"]] == ["wf-2"]


def test_explain_has_deterministic_policy_and_approval_precedence(tmp_path, capsys):
    state = _checkpoint(tmp_path, _records(status="FAILED", policy="DENY"))
    assert main(["--state-file", str(state), "--output", "json", "explain", "wf-1"]) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "DENIED"
    state = _checkpoint(tmp_path, _records(status="FAILED", policy="REQUIRE_APPROVAL", approval="PENDING"))
    assert main(["--state-file", str(state), "--output", "json", "explain", "wf-1"]) == 0
    explanation = json.loads(capsys.readouterr().out)
    assert explanation["outcome"] == "APPROVAL_PENDING"
    assert explanation["decisiveEvidence"]["id"] == "approval-1"


def test_explain_denial_outranks_approval_and_approved_requirement_is_not_pending(tmp_path, capsys):
    records = _records(status="FAILED", policy="DENY", approval="PENDING")
    records.append({"id": "policy-2", "kind": "PolicyDecision", "workflowExecutionId": "wf-1", "taskExecutionId": "task-1", "decision": "REQUIRE_APPROVAL", "reason": "review", "createdAt": "2026-01-01T00:00:01Z"})
    state = _checkpoint(tmp_path, records)
    assert main(["--state-file", str(state), "--output", "json", "explain", "wf-1"]) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "DENIED"
    records = _records(policy="REQUIRE_APPROVAL", approval="APPROVED")
    state = _checkpoint(tmp_path, records)
    assert main(["--state-file", str(state), "--output", "json", "explain", "wf-1"]) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "SUCCEEDED"


def test_explain_ignores_failed_superseded_task_attempt(tmp_path, capsys):
    records = _records()
    failed = dict(records[1])
    failed.update({"id": "task-old", "attempt": 1, "status": "FAILED"})
    records[1].update({"attempt": 2, "status": "SUCCEEDED"})
    records.append(failed)
    state = _checkpoint(tmp_path, records)
    assert main(["--state-file", str(state), "--output", "json", "explain", "wf-1"]) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "SUCCEEDED"


def test_explain_identifies_validation_failure(tmp_path, capsys):
    state = _checkpoint(tmp_path, _records(status="FAILED"))
    assert main(["--state-file", str(state), "--output", "json", "explain", "wf-1"]) == 0
    explanation = json.loads(capsys.readouterr().out)
    assert explanation["outcome"] == "FAILED"
    assert explanation["decisiveEvidence"]["id"] == "evaluation-1"


def test_missing_or_wrong_kind_identifier_has_stable_json_error(tmp_path, capsys):
    state = _checkpoint(tmp_path, _records())
    assert main(["--state-file", str(state), "tasks", "show", "missing"]) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "RUNTIME_OBJECT_NOT_FOUND"
    assert main(["--state-file", str(state), "artifacts", "show", "task-1"]) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "RUNTIME_KIND_MISMATCH"
    assert main(["--state-file", str(state), "--output", "json", "tasks", "show"]) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "INVALID_ARGUMENT"


def test_artifact_metadata_is_safe_and_unsafe_debug_is_explicit(tmp_path, capsys):
    state = _checkpoint(tmp_path, _records())
    assert main(["--state-file", str(state), "--output", "json", "artifacts", "show", "artifact-1"]) == 0
    artifact = json.loads(capsys.readouterr().out)
    assert artifact["mediaType"] == "text/x-diff"
    assert artifact["contentAddress"].startswith("sha256:")
    assert main(["--state-file", str(state), "--unsafe-debug", "--output", "json", "contexts", "show", "ctx-1"]) == 0
    assert json.loads(capsys.readouterr().out)["elements"][0]["content"] == "private source"


def test_execution_checks_task_references_and_redacts_evaluation_logs(tmp_path, capsys):
    records = _records()
    records[1]["contextPackageId"] = "missing-context"
    state = _checkpoint(tmp_path, records)
    assert main(["--state-file", str(state), "executions", "show", "wf-1"]) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "RUNTIME_REFERENCE_MALFORMED"
    records = _records(status="FAILED")
    records[-1]["logs"] = ["private rejected output"]
    state = _checkpoint(tmp_path, records)
    assert main(["--state-file", str(state), "--output", "json", "evaluations", "show", "evaluation-1"]) == 0
    assert json.loads(capsys.readouterr().out)["logs"] == "[REDACTED]"


def test_runtime_checkpoint_fixture_is_schema_valid():
    fixture = Path(__file__).parents[1] / "fixtures" / "runtime" / "inspection-cli-checkpoint.json"
    for value in json.loads(fixture.read_text(encoding="utf-8"))["objects"].values():
        validator = _runtime_validator(f"{str(value['kind']).lower()}.schema.json")
        assert not list(validator.iter_errors(value))
